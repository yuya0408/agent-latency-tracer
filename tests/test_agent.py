"""Tests for system/agent.py: a scripted LLM against the real MCP server, in-process.

The LLM is a stand-in that returns scripted responses in order (and opens a
chat span like the real client), so each test can steer the loop: ask for
tools, answer, or never stop. The MCP server is the real one, connected
without HTTP as in test_mcp.py.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from system.agent import Agent
from system.llm.client import ChatResult
from system.mcp_client import McpToolClient
from system.mcp_server.server import build_server
from system.tools import mock_server
from tracing.instrumentation import chat_span, get_tracer

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
TRACER = get_tracer(_PROVIDER)

SMALL_CORPUS = 1_000


class ScriptedLlm:
    """Stands in for LlmClient: returns the next scripted response."""

    def __init__(self, responses: list[ChatResult]) -> None:
        self._responses = list(responses)
        self.requests: list[list[dict]] = []  # the messages sent on each call

    async def chat(self, messages: list[dict], tools: list[dict]) -> ChatResult:
        self.requests.append(list(messages))
        with chat_span(TRACER, request_model="scripted", provider_name="test"):
            return self._responses.pop(0)


def _call(call_id: str, name: str, arguments: dict | str) -> dict:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _asks(*calls: dict) -> ChatResult:
    return ChatResult(content="", tool_calls=list(calls), finish_reason="tool_calls")


def _answers(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop")


@pytest.fixture(autouse=True)
def _no_tool_latency_and_clean_spans(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.0)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.0)
    _EXPORTER.clear()


def _run(llm: ScriptedLlm, questions: list[str], **agent_kwargs):
    """Run the questions one after another on one agent; returns the results."""
    async def run():
        server = build_server(otel=False, corpus_n=SMALL_CORPUS)
        async with McpToolClient(server, TRACER, agent_name="test") as mcp:
            agent = Agent(llm, mcp, TRACER, name="test", **agent_kwargs)
            return [await agent.run(q) for q in questions]

    return asyncio.run(run())


def _spans(name_prefix: str):
    return [s for s in _EXPORTER.get_finished_spans() if s.name.startswith(name_prefix)]


def test_stops_when_llm_answers():
    llm = ScriptedLlm([_asks(_call("c1", "find_records", {"type": "item"})), _answers("品目は4件です。")])
    (result,) = _run(llm, ["品目は何件?"])
    assert result.stop_reason == "answer"
    assert result.turns == 2
    assert result.answer == "品目は4件です。"
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]
    # The second LLM call saw the tool's result.
    tool_message = llm.requests[1][-1]
    assert tool_message["tool_call_id"] == "c1"
    assert json.loads(tool_message["content"])["status"] == "ok"


def test_tool_calls_in_one_response_run_concurrently(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.2)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.2)
    llm = ScriptedLlm([
        _asks(_call("c1", "find_records", {"type": "item"}), _call("c2", "describe_data", {})),
        _answers("done"),
    ])
    _run(llm, ["q"])
    a, b = _spans("execute_tool")
    # Concurrent: each started before the other ended.
    assert a.start_time < b.end_time and b.start_time < a.end_time


def test_spans_nest_under_one_invoke_agent_span():
    llm = ScriptedLlm([_asks(_call("c1", "find_records", {"type": "item"})), _answers("done")])
    _run(llm, ["q"])
    (root,) = _spans("invoke_agent")
    children = _spans("chat") + _spans("execute_tool")
    assert len(children) == 3  # two LLM calls, one tool call
    for span in children:
        assert span.context.trace_id == root.context.trace_id
        assert span.parent.span_id == root.context.span_id


def test_stops_at_max_turns():
    llm = ScriptedLlm([_asks(_call(f"c{i}", "describe_data", {})) for i in range(3)])
    (result,) = _run(llm, ["q"], max_turns=3)
    assert result.stop_reason == "max_turns"
    assert result.turns == len(llm.requests) == 3
    (root,) = _spans("invoke_agent")
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "max_turns_exceeded"


def test_tool_errors_go_back_to_the_llm():
    bad_filter = {"type": "item", "filter": [{"field": "code", "op": "eq", "value": "X001"}]}
    llm = ScriptedLlm([
        _asks(_call("c1", "find_records", bad_filter), _call("c2", "find_records", "{not json")),
        _answers("done"),
    ])
    (result,) = _run(llm, ["q"])
    assert result.stop_reason == "answer"
    tool_error, json_error = result.messages[2], result.messages[3]
    assert json.loads(tool_error["content"])["status"] == "error"
    assert json_error["content"].startswith("Error: the arguments are not valid JSON")


def test_tool_list_is_fetched_once(monkeypatch):
    fetched = 0
    original = McpToolClient.list_tools

    async def counting(self):
        nonlocal fetched
        fetched += 1
        return await original(self)

    monkeypatch.setattr(McpToolClient, "list_tools", counting)
    _run(ScriptedLlm([_answers("a"), _answers("b")]), ["q1", "q2"])
    assert fetched == 1
