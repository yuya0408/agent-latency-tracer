"""Tests for system/llm/client.py against a mock OpenAI-compatible server.

httpx2's MockTransport stands in for llama.cpp: each test hands it the
server-sent event lines llama.cpp would stream, then checks what the client
sent, how it assembled the pieces, and what it left on the chat span.
"""
from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from mcp import types
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from system.llm.client import LlmClient, to_openai_tools
from tracing.instrumentation import get_tracer

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))

TOOL = types.Tool(
    name="find_records",
    description="Find records of one type.",
    input_schema={"type": "object", "properties": {"type": {"type": "string"}}, "required": ["type"]},
)

# A response that calls two tools, each call split across chunks the way
# llama.cpp streams them: id and name first, then the arguments in pieces.
TOOL_CALL_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "find_records", "arguments": ""}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"type": '}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"item"}'}}]}}]},
    {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 1, "id": "call_b", "type": "function", "function": {"name": "find_records", "arguments": '{"type": "order"}'}}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    {
        "choices": [],
        "model": "Qwen3.5-2B-Q4_K_M",
        "usage": {"prompt_tokens": 1400, "completion_tokens": 40},
        "timings": {"prompt_n": 200, "cache_n": 1200, "prompt_ms": 2100.0, "predicted_n": 40, "predicted_ms": 900.0},
    },
]

TEXT_CHUNKS = [
    {"choices": [{"index": 0, "delta": {"content": "在庫は"}}]},
    {"choices": [{"index": 0, "delta": {"content": "120個です。"}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
]


@pytest.fixture(autouse=True)
def _clean_spans():
    _EXPORTER.clear()


def _sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def _chat(handler, **client_kwargs):
    """Run one chat call against `handler`; returns the result."""
    async def run():
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        async with LlmClient(get_tracer(_PROVIDER), http=http, **client_kwargs) as client:
            return await client.chat([{"role": "user", "content": "在庫は?"}], to_openai_tools([TOOL]))

    return asyncio.run(run())


def _streaming(chunks: list[dict], sent: list[dict] | None = None):
    def handler(request: httpx2.Request) -> httpx2.Response:
        if sent is not None:
            sent.append(json.loads(request.content))
        return httpx2.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})

    return handler


def _chat_span():
    (span,) = _EXPORTER.get_finished_spans()
    return span


def test_request_body():
    sent: list[dict] = []
    _chat(_streaming(TEXT_CHUNKS, sent))
    (body,) = sent
    assert body["stream"] is True
    assert body["temperature"] == 0.0
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["tools"] == [{
        "type": "function",
        "function": {"name": "find_records", "description": "Find records of one type.", "parameters": TOOL.input_schema},
    }]


def test_traceparent_points_at_the_chat_span():
    sent: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.headers["traceparent"])
        return httpx2.Response(200, content=_sse(TEXT_CHUNKS), headers={"content-type": "text/event-stream"})

    _chat(handler)
    context = _chat_span().get_span_context()
    assert sent == [f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}"]


def test_temperature_is_passed_through():
    sent: list[dict] = []
    _chat(_streaming(TEXT_CHUNKS, sent), temperature=0.8)
    assert sent[0]["temperature"] == 0.8
    assert _chat_span().attributes["gen_ai.request.temperature"] == 0.8


def test_assembles_tool_calls_split_across_chunks():
    result = _chat(_streaming(TOOL_CALL_CHUNKS))
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {"id": "call_a", "type": "function", "function": {"name": "find_records", "arguments": '{"type": "item"}'}},
        {"id": "call_b", "type": "function", "function": {"name": "find_records", "arguments": '{"type": "order"}'}},
    ]
    assert result.as_message()["tool_calls"] == result.tool_calls


def test_text_answer_has_no_tool_calls():
    result = _chat(_streaming(TEXT_CHUNKS))
    assert result.content == "在庫は120個です。"
    assert result.finish_reason == "stop"
    assert "tool_calls" not in result.as_message()


def test_span_attributes():
    _chat(_streaming(TOOL_CALL_CHUNKS))
    attrs = _chat_span().attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.temperature"] == 0.0
    assert attrs["gen_ai.usage.input_tokens"] == 1400
    assert attrs["gen_ai.usage.output_tokens"] == 40
    assert list(attrs["gen_ai.response.finish_reasons"]) == ["tool_calls"]
    assert attrs["llm.time_to_first_token_ms"] >= 0
    assert attrs["llamacpp.prompt_n"] == 200
    assert attrs["llamacpp.cache_n"] == 1200
    assert attrs["llamacpp.prompt_ms"] == 2100.0
    assert attrs["llamacpp.predicted_n"] == 40
    assert attrs["llamacpp.predicted_ms"] == 900.0


def test_span_attributes_from_vllm():
    _chat(_streaming([
        {"choices": [{"index": 0, "delta": {"content": "在庫"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {"prompt_tokens": 162, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 160}},
            "metrics": {
                "queue_time_ms": 0.03,
                "time_to_first_token_ms": 65.0,
                "generation_time_ms": 1200.0,
                "mean_itl_ms": 60.0,
                "tokens_per_second": 16.0,
            },
        },
    ]))
    attrs = _chat_span().attributes
    assert attrs["vllm.queue_time_ms"] == 0.03
    assert attrs["vllm.time_to_first_token_ms"] == 65.0
    assert attrs["vllm.generation_time_ms"] == 1200.0
    assert attrs["vllm.mean_itl_ms"] == 60.0
    assert attrs["vllm.tokens_per_second"] == 16.0
    assert attrs["vllm.cached_tokens"] == 160


def test_no_vllm_attributes_from_llama_cpp():
    _chat(_streaming(TOOL_CALL_CHUNKS))
    assert not [name for name in _chat_span().attributes if name.startswith("vllm.")]


def test_no_cached_tokens_when_the_server_leaves_them_empty():
    _chat(_streaming([{"choices": [], "usage": {"prompt_tokens": 162, "prompt_tokens_details": None}}]))
    assert "vllm.cached_tokens" not in _chat_span().attributes


def test_no_ttft_when_nothing_is_generated():
    # Only a finish chunk: there is no first token to time.
    _chat(_streaming([{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]))
    assert "llm.time_to_first_token_ms" not in _chat_span().attributes


def test_http_error_marks_span_and_raises():
    with pytest.raises(httpx2.HTTPStatusError):
        _chat(lambda request: httpx2.Response(500, content=b"model not loaded"))
    span = _chat_span()
    assert span.status.status_code == StatusCode.ERROR
    assert span.attributes["error.type"] == "HTTPStatusError"
