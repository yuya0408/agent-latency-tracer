"""The agent loop: ask the LLM, run the tool calls it asks for, repeat until it answers.

This is the corrected baseline the latency cases are built against:
- tool calls the LLM asks for in one response run concurrently (asyncio.gather);
- one MCP connection is reused for every call, and the tool list is fetched once;
- no system prompt;
- a conversation is kept in memory for its session (on AgentCore, a session is
  one agent process), so a follow-up question carries the earlier ones, trimmed
  to the last HISTORY_WINDOW messages. With a ConversationMemory (on AWS,
  AgentCore Memory: system/memory.py) each message is also written there, and
  a process that doesn't have the conversation reads it back from there.

Each run is one invoke_agent span; under it sit the chat spans (one per LLM
call) and the execute_tool spans (one per tool call). Tool calls from the
same response overlap in time, which is how the trace shows they ran
concurrently.
"""
from __future__ import annotations
import asyncio, json, time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from mcp import types
from opentelemetry import trace

from system.llm.client import LlmClient, to_openai_tools
from system.mcp_client import McpToolClient
from tracing.instrumentation import invoke_agent_span, mark_error

# OpenAI Agents SDK's DEFAULT_MAX_TURNS. Counts LLM calls, as the SDK does.
MAX_TURNS = 10
# Strands Agents' default conversation manager keeps the last 40 messages
# (SlidingWindowConversationManager(window_size=40)).
HISTORY_WINDOW = 40
# Not part of the GenAI conventions: when the first piece of text was passed on.
TIME_TO_FIRST_TEXT_MS = "agent.time_to_first_text_ms"


class ConversationMemory(Protocol):
    async def load(self, conversation_id: str) -> list[dict[str, Any]]: ...
    async def append(self, conversation_id: str, message: dict[str, Any]) -> None: ...


@dataclass
class AgentResult:
    answer: str
    messages: list[dict[str, Any]]  # the whole conversation, for inspection
    turns: int  # LLM calls made
    stop_reason: str  # "answer" or "max_turns"
    trace_id: str  # of this run's invoke_agent span


class Agent:
    def __init__(
        self,
        llm: LlmClient,
        mcp: McpToolClient,
        tracer: trace.Tracer,
        *,
        name: str = "agent",
        max_turns: int = MAX_TURNS,
        sequential_tools: bool = False,
        time_in_prompt: bool = False,
        memory: ConversationMemory | None = None,
    ) -> None:
        """sequential_tools=True is pitfall #1's switch (system/pitfalls.py):
        the tool calls from one response are awaited one after another.

        time_in_prompt=True is pitfall #10's: every LLM call starts with a
        system prompt holding the current time to the second, made afresh for
        that call, as a prompt template filled in at each model call does.
        The chat template puts the system prompt before the tool definitions,
        so the prompt differs from the first tokens on and the server's prefix
        cache cannot reuse anything, not even within one question."""
        self._llm = llm
        self._mcp = mcp
        self._tracer = tracer
        self._name = name
        self._max_turns = max_turns
        self._sequential_tools = sequential_tools
        self._time_in_prompt = time_in_prompt
        self._memory = memory
        self._tools: list[dict[str, Any]] | None = None
        self._histories: dict[str, list[dict[str, Any]]] = {}

    async def run(
        self, question: str, *, conversation_id: str | None = None, on_text: Callable[[str], None] | None = None
    ) -> AgentResult:
        """With a conversation_id, the question continues that conversation;
        without one, it starts a new conversation that is not kept. A run that
        fails (an exception) leaves the process's copy of the conversation as
        it was; the memory keeps the messages written before the failure.

        on_text gets the LLM's text as it is written, from every LLM call: a
        call that asks for tools may write a little first ("I'll search..."),
        which a chat that streams would show as well."""
        result = await self._run(question, conversation_id, on_text)
        if conversation_id:
            self._histories[conversation_id] = _last_turns(result.messages, HISTORY_WINDOW)
        return result

    async def _run(
        self, question: str, conversation_id: str | None, on_text: Callable[[str], None] | None
    ) -> AgentResult:
        with invoke_agent_span(self._tracer, agent_name=self._name, conversation_id=conversation_id) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            start = time.perf_counter()
            first_text_seen = False
            # Inside the span, so a read from the memory shows up in this run's trace.
            messages = [*await self._history(conversation_id)]
            await self._add(messages, conversation_id, {"role": "user", "content": question})

            def on_text_timed(text: str) -> None:
                # When the first piece left the agent: set against the caller's
                # time to the first piece, it shows whether the platform held
                # the stream back on the way.
                nonlocal first_text_seen
                if not first_text_seen:
                    first_text_seen = True
                    span.set_attribute(TIME_TO_FIRST_TEXT_MS, (time.perf_counter() - start) * 1000)
                on_text(text)

            try:
                if self._tools is None:  # first run only, so it shows up in that run's trace
                    self._tools = to_openai_tools(await self._mcp.list_tools())
                for turn in range(1, self._max_turns + 1):
                    result = await self._llm.chat(self._with_time(messages), self._tools, on_text and on_text_timed)
                    await self._add(messages, conversation_id, result.as_message())
                    if not result.tool_calls:
                        return AgentResult(result.content, messages, turn, "answer", trace_id)
                    if self._sequential_tools:
                        for c in result.tool_calls:
                            await self._add(messages, conversation_id, await self._call_tool(c))
                    else:
                        for m in await asyncio.gather(*(self._call_tool(c) for c in result.tool_calls)):
                            await self._add(messages, conversation_id, m)
            except asyncio.CancelledError:
                # The span would otherwise end as if the run went fine.
                mark_error(span, "cancelled", "the caller went away")
                raise
            mark_error(span, "max_turns_exceeded")
            return AgentResult("", messages, self._max_turns, "max_turns", trace_id)

    async def _history(self, conversation_id: str | None) -> list[dict[str, Any]]:
        """The conversation so far: from the process while it has it, else from
        the memory (a new session, or a new process for the same session)."""
        if conversation_id is None:
            return []
        if conversation_id in self._histories:
            return self._histories[conversation_id]
        if self._memory is None:
            return []
        return _last_turns(await self._memory.load(conversation_id), HISTORY_WINDOW)

    async def _add(self, messages: list[dict[str, Any]], conversation_id: str | None, message: dict[str, Any]) -> None:
        """Add one message, and write it to the memory as it is added, as
        Strands Agents' AgentCoreMemorySessionManager does by default."""
        messages.append(message)
        if self._memory is not None and conversation_id is not None:
            await self._memory.append(conversation_id, message)

    def _with_time(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self._time_in_prompt:
            return messages
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [{"role": "system", "content": f"現在の日時: {now}"}, *messages]

    async def _call_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call and return the tool message for it. A tool's error
        goes back to the LLM as text, as it would in a real agent, so it can
        correct the call."""
        try:
            arguments = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError as e:
            content = f"Error: the arguments are not valid JSON ({e})."
        else:
            result = await self._mcp.call_tool(call["function"]["name"], arguments, tool_call_id=call["id"])
            content = "\n".join(c.text for c in result.content if isinstance(c, types.TextContent))
        return {"role": "tool", "tool_call_id": call["id"], "content": content}


def _last_turns(messages: list[dict[str, Any]], window: int) -> list[dict[str, Any]]:
    """The last `window` messages or fewer, starting at a user message, so no
    tool result is kept without the assistant message that asked for it. If
    the last question alone runs longer than the window, all of it is kept."""
    if len(messages) <= window:
        return list(messages)
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    start = next((i for i in users if i >= len(messages) - window), users[-1])
    return messages[start:]
