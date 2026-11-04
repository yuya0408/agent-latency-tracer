"""The agent loop: ask the LLM, run the tool calls it asks for, repeat until it answers.

This is the corrected baseline the latency cases are built against:
- tool calls the LLM asks for in one response run concurrently (asyncio.gather);
- one MCP connection is reused for every call, and the tool list is fetched once;
- no system prompt.

Each run is one invoke_agent span; under it sit the chat spans (one per LLM
call) and the execute_tool spans (one per tool call). Tool calls from the
same response overlap in time, which is how the trace shows they ran
concurrently.
"""
from __future__ import annotations
import asyncio, json
from dataclasses import dataclass
from typing import Any

from mcp import types
from opentelemetry import trace

from system.llm.client import LlmClient, to_openai_tools
from system.mcp_client import McpToolClient
from tracing.instrumentation import invoke_agent_span, mark_error

# OpenAI Agents SDK's DEFAULT_MAX_TURNS. Counts LLM calls, as the SDK does.
MAX_TURNS = 10


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
    ) -> None:
        """sequential_tools=True is pitfall #1's switch (system/pitfalls.py):
        the tool calls from one response are awaited one after another."""
        self._llm = llm
        self._mcp = mcp
        self._tracer = tracer
        self._name = name
        self._max_turns = max_turns
        self._sequential_tools = sequential_tools
        self._tools: list[dict[str, Any]] | None = None

    async def run(self, question: str, *, conversation_id: str | None = None) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        with invoke_agent_span(self._tracer, agent_name=self._name, conversation_id=conversation_id) as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            if self._tools is None:  # first run only, so it shows up in that run's trace
                self._tools = to_openai_tools(await self._mcp.list_tools())
            for turn in range(1, self._max_turns + 1):
                result = await self._llm.chat(messages, self._tools)
                messages.append(result.as_message())
                if not result.tool_calls:
                    return AgentResult(result.content, messages, turn, "answer", trace_id)
                if self._sequential_tools:
                    for c in result.tool_calls:
                        messages.append(await self._call_tool(c))
                else:
                    messages.extend(await asyncio.gather(*(self._call_tool(c) for c in result.tool_calls)))
            mark_error(span, "max_turns_exceeded")
            return AgentResult("", messages, self._max_turns, "max_turns", trace_id)

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
