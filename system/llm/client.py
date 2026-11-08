"""OpenAI-compatible chat client (llama.cpp's server or vLLM), one `chat` span per call.

Streams the response so the client can see when the first token arrives
(time to first token, TTFT): that is what a chat user waits for, and it is
measurable against any OpenAI-compatible server. llama.cpp additionally
reports its own server-side timings in the last chunk (tokens read from the
prompt cache, prefill and decode time). Both go on the span, so a long
prefill (#8), a long output (#9) and a missed prompt cache (#10) can be told
apart, and client TTFT minus server prefill time shows what was spent outside
the model (network, queueing).
"""
from __future__ import annotations
import json, time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp import types
from opentelemetry import propagate, trace

from tracing.instrumentation import chat_span, mark_error, set_chat_usage

DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "Qwen3.5-2B-Q4_K_M"
DEFAULT_PROVIDER_NAME = "llama.cpp"
# Prefill of the ~1400-token tool definitions alone takes ~25s on this CPU
# (docs/DESIGN.md §8), far past httpx's 5s default.
READ_TIMEOUT_S = 600.0

# Not part of the GenAI conventions: client-side TTFT, and llama.cpp's own
# per-request timings.
TTFT_MS = "llm.time_to_first_token_ms"
TOOL_COUNT = "llm.request.tool_count"  # tool definitions sent with the call
LLAMACPP_PROMPT_N = "llamacpp.prompt_n"  # prompt tokens actually processed
LLAMACPP_CACHE_N = "llamacpp.cache_n"  # prompt tokens reused from the cache
LLAMACPP_PROMPT_MS = "llamacpp.prompt_ms"
LLAMACPP_PREDICTED_N = "llamacpp.predicted_n"
LLAMACPP_PREDICTED_MS = "llamacpp.predicted_ms"
_TIMINGS = {
    "prompt_n": LLAMACPP_PROMPT_N,
    "cache_n": LLAMACPP_CACHE_N,
    "prompt_ms": LLAMACPP_PROMPT_MS,
    "predicted_n": LLAMACPP_PREDICTED_N,
    "predicted_ms": LLAMACPP_PREDICTED_MS,
}
# vLLM's per-request breakdown (--enable-per-request-metrics). Its TTFT leaves
# out the time in the queue, so it is not the same number as TTFT_MS.
_VLLM_METRICS = {
    "queue_time_ms": "vllm.queue_time_ms",
    "time_to_first_token_ms": "vllm.time_to_first_token_ms",
    "generation_time_ms": "vllm.generation_time_ms",
    "mean_itl_ms": "vllm.mean_itl_ms",
    "tokens_per_second": "vllm.tokens_per_second",
}
VLLM_CACHED_TOKENS = "vllm.cached_tokens"  # needs --enable-prompt-tokens-details


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # OpenAI shape
    finish_reason: str | None = None

    def as_message(self) -> dict[str, Any]:
        """The assistant message to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


def to_openai_tools(tools: list[types.Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
        }
        for t in tools
    ]


class LlmClient:
    def __init__(
        self,
        tracer: trace.Tracer,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        provider_name: str = DEFAULT_PROVIDER_NAME,
        thinking: bool = False,
        temperature: float = 0.0,
        http: httpx2.AsyncClient | None = None,
    ) -> None:
        """`temperature` defaults to 0 (greedy): left unset, llama.cpp samples at
        0.8 and the same question can come back with a different number of tool
        calls. `http` is for tests (a client on a MockTransport); by default one
        is made here."""
        self._tracer = tracer
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._provider_name = provider_name
        self._thinking = thinking
        self._temperature = temperature
        self._http = http or httpx2.AsyncClient(timeout=httpx2.Timeout(5.0, read=READ_TIMEOUT_S))

    async def __aenter__(self) -> LlmClient:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_text: Callable[[str], None] | None = None,
    ) -> ChatResult:
        """`on_text` is called with each piece of text as it arrives, for passing
        the answer on to the user while it is still being written."""
        body = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self._temperature,
            # Qwen3.5 writes its reasoning before the answer unless told not to.
            "chat_template_kwargs": {"enable_thinking": self._thinking},
        }
        with chat_span(
            self._tracer, request_model=self._model, provider_name=self._provider_name, temperature=self._temperature
        ) as span:
            span.set_attribute(TOOL_COUNT, len(tools))
            try:
                return await self._stream(body, span, on_text)
            except httpx2.HTTPError as e:
                mark_error(span, type(e).__name__, str(e))
                raise

    async def _stream(
        self, body: dict[str, Any], span: trace.Span, on_text: Callable[[str], None] | None
    ) -> ChatResult:
        start = time.perf_counter()
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}  # tool calls arrive in pieces, keyed by index
        result = ChatResult(content="")
        first_token_seen = False
        # The chat span is current here, so a server that traces (vLLM) parents
        # its span on it and joins this trace.
        headers: dict[str, str] = {}
        propagate.inject(headers)
        async with self._http.stream("POST", self._url, json=body, headers=headers) as response:
            if response.is_error:
                # The body says why (vLLM: the input is longer than the context);
                # raise_for_status alone would leave only the status on the span.
                await response.aread()
                raise httpx2.HTTPStatusError(
                    f"{response.status_code} from {self._url}: {response.text[:500]}",
                    request=response.request, response=response,
                )
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[len("data: "):]
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                for choice in chunk.get("choices", []):
                    delta = choice.get("delta", {})
                    if (delta.get("content") or delta.get("tool_calls")) and not first_token_seen:
                        first_token_seen = True
                        span.set_attribute(TTFT_MS, (time.perf_counter() - start) * 1000)
                    if delta.get("content"):
                        content.append(delta["content"])
                        if on_text:
                            on_text(delta["content"])
                    for piece in delta.get("tool_calls", []):
                        call = calls.setdefault(
                            piece["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                        )
                        call["id"] = piece.get("id") or call["id"]
                        call["function"]["name"] += piece.get("function", {}).get("name") or ""
                        call["function"]["arguments"] += piece.get("function", {}).get("arguments") or ""
                    if choice.get("finish_reason"):
                        result.finish_reason = choice["finish_reason"]
                if chunk.get("usage"):
                    set_chat_usage(
                        span,
                        input_tokens=chunk["usage"].get("prompt_tokens"),
                        output_tokens=chunk["usage"].get("completion_tokens"),
                        response_model=chunk.get("model"),
                    )
                    details = chunk["usage"].get("prompt_tokens_details") or {}
                    if details.get("cached_tokens") is not None:
                        span.set_attribute(VLLM_CACHED_TOKENS, details["cached_tokens"])
                for key, attribute in _TIMINGS.items():
                    if key in chunk.get("timings", {}):
                        span.set_attribute(attribute, chunk["timings"][key])
                for key, attribute in _VLLM_METRICS.items():
                    if (chunk.get("metrics") or {}).get(key) is not None:
                        span.set_attribute(attribute, chunk["metrics"][key])
        if result.finish_reason:
            set_chat_usage(span, finish_reasons=[result.finish_reason])
        result.content = "".join(content)
        result.tool_calls = [calls[i] for i in sorted(calls)]
        return result
