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


class ScriptedLlm:
    """Stands in for LlmClient: returns the next scripted response."""

    def __init__(self, responses: list[ChatResult]) -> None:
        self._responses = list(responses)
        self.requests: list[list[dict]] = []  # the messages sent on each call

    async def chat(self, messages: list[dict], tools: list[dict], on_text=None) -> ChatResult:
        self.requests.append(list(messages))
        with chat_span(TRACER, request_model="scripted", provider_name="test"):
            response = self._responses.pop(0)
            if on_text and response.content:
                on_text(response.content)
            return response


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


def _run(llm: ScriptedLlm, questions: list[str], on_text=None, **agent_kwargs):
    """Run the questions one after another on one agent; returns the results."""
    async def run():
        server = build_server(otel=False)
        async with McpToolClient(server, TRACER, agent_name="test") as mcp:
            agent = Agent(llm, mcp, TRACER, name="test", **agent_kwargs)
            return [await agent.run(q, on_text=on_text) for q in questions]

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


def test_text_from_every_llm_call_goes_to_on_text():
    asks_with_text = ChatResult(
        content="検索します。", tool_calls=[_call("c1", "find_records", {"type": "item"})], finish_reason="tool_calls"
    )
    llm = ScriptedLlm([asks_with_text, _answers("品目は4件です。")])
    pieces: list[str] = []
    _run(llm, ["品目は何件?"], on_text=pieces.append)
    assert pieces == ["検索します。", "品目は4件です。"]


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


def test_pitfall_1_awaits_tool_calls_one_after_another(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.2)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.2)
    llm = ScriptedLlm([
        _asks(_call("c1", "find_records", {"type": "item"}), _call("c2", "describe_data", {})),
        _answers("done"),
    ])
    _run(llm, ["q"], sequential_tools=True)
    a, b = sorted(_spans("execute_tool"), key=lambda s: s.start_time)
    assert a.end_time <= b.start_time
    # Both results still go back, in the order asked.
    assert [m["tool_call_id"] for m in llm.requests[1] if m["role"] == "tool"] == ["c1", "c2"]


def test_no_system_prompt_by_default():
    llm = ScriptedLlm([_asks(_call("c1", "describe_data", {})), _answers("done")])
    _run(llm, ["q"])
    assert all(r[0]["role"] == "user" for r in llm.requests)


def test_pitfall_10_starts_every_llm_call_with_the_time():
    llm = ScriptedLlm([_asks(_call("c1", "describe_data", {})), _answers("done")])
    (result,) = _run(llm, ["q"], time_in_prompt=True)
    for request in llm.requests:
        assert request[0]["role"] == "system"
        assert request[0]["content"].startswith("現在の日時: ")
        assert request[1] == {"role": "user", "content": "q"}
    # Made per call, not kept in the conversation.
    assert [m["role"] for m in result.messages] == ["user", "assistant", "tool", "assistant"]


def _run_conversation(llm: ScriptedLlm, turns: list[tuple[str, str | None]], **agent_kwargs):
    """(question, conversation_id) pairs, run in order on one agent."""
    async def run():
        server = build_server(otel=False)
        async with McpToolClient(server, TRACER, agent_name="test") as mcp:
            agent = Agent(llm, mcp, TRACER, name="test", **agent_kwargs)
            return [await agent.run(q, conversation_id=c) for q, c in turns]

    return asyncio.run(run())


def test_a_session_keeps_its_conversation():
    llm = ScriptedLlm([
        _asks(_call("c1", "describe_data", {})), _answers("a1"),
        _answers("a2"),
        _answers("other"),
    ])
    _run_conversation(llm, [("q1", "s1"), ("q2", "s1"), ("q3", "s2")])
    # q2 carries q1 with its tool call; s2 starts empty.
    assert [m["role"] for m in llm.requests[2]] == ["user", "assistant", "tool", "assistant", "user"]
    assert llm.requests[2][0]["content"] == "q1"
    assert llm.requests[3] == [{"role": "user", "content": "q3"}]


def test_without_a_conversation_id_nothing_is_kept():
    llm = ScriptedLlm([_answers("a1"), _answers("a2")])
    _run_conversation(llm, [("q1", None), ("q2", None)])
    assert llm.requests[1] == [{"role": "user", "content": "q2"}]


def test_history_is_trimmed_at_a_question(monkeypatch):
    monkeypatch.setattr("system.agent.HISTORY_WINDOW", 5)
    # Each question: user, assistant(tool call), tool, assistant = 4 messages.
    llm = ScriptedLlm([r for i in range(3) for r in (_asks(_call(f"c{i}", "describe_data", {})), _answers(f"a{i}"))])
    _run_conversation(llm, [("q0", "s"), ("q1", "s"), ("q2", "s")])
    # Before q2, the history was q0 and q1 (8 messages); the last 5 start mid-q0,
    # so only q1 is kept.
    sent = llm.requests[4]
    assert sent[0] == {"role": "user", "content": "q1"}
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]


class FakeMemory:
    """Stands in for AgentCoreMemory (system/memory.py), shared between agents
    the way one AgentCore Memory is shared between processes."""

    def __init__(self) -> None:
        self.messages: dict[str, list[dict]] = {}
        self.loads: list[str] = []

    async def load(self, conversation_id: str) -> list[dict]:
        self.loads.append(conversation_id)
        return list(self.messages.get(conversation_id, []))

    async def append(self, conversation_id: str, message: dict) -> None:
        self.messages.setdefault(conversation_id, []).append(message)


def test_every_message_is_written_to_the_memory_as_it_is_added():
    memory = FakeMemory()
    llm = ScriptedLlm([
        _asks(_call("c1", "find_records", {"type": "item"}), _call("c2", "describe_data", {})),
        _answers("done"),
    ])
    (result,) = _run_conversation(llm, [("q", "s1")], memory=memory)
    assert memory.messages["s1"] == result.messages
    assert [m["role"] for m in memory.messages["s1"]] == ["user", "assistant", "tool", "tool", "assistant"]


def test_a_new_process_reads_the_conversation_back_from_the_memory():
    memory = FakeMemory()
    _run_conversation(ScriptedLlm([_asks(_call("c1", "describe_data", {})), _answers("a1")]), [("q1", "s1")], memory=memory)
    # Another agent, as a new process for the same session after the old one is gone.
    llm = ScriptedLlm([_answers("a2")])
    _run_conversation(llm, [("q2", "s1")], memory=memory)
    assert [m["role"] for m in llm.requests[0]] == ["user", "assistant", "tool", "assistant", "user"]
    assert llm.requests[0][0]["content"] == "q1"


def test_the_memory_is_read_only_when_the_process_lacks_the_conversation():
    memory = FakeMemory()
    _run_conversation(ScriptedLlm([_answers("a1"), _answers("a2"), _answers("b1")]), [("q1", "s1"), ("q2", "s1"), ("q3", "s2")], memory=memory)
    assert memory.loads == ["s1", "s2"]


def test_without_a_conversation_id_the_memory_is_not_used():
    memory = FakeMemory()
    _run_conversation(ScriptedLlm([_answers("a1")]), [("q1", None)], memory=memory)
    assert memory.loads == [] and memory.messages == {}


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


def test_time_to_first_text_goes_on_the_invoke_agent_span():
    _run(ScriptedLlm([_answers("品目は4件です。")]), ["品目は何件?"], on_text=lambda text: None)
    (root,) = _spans("invoke_agent")
    assert root.attributes["agent.time_to_first_text_ms"] >= 0


def test_no_time_to_first_text_without_on_text():
    _run(ScriptedLlm([_answers("品目は4件です。")]), ["品目は何件?"])
    (root,) = _spans("invoke_agent")
    assert "agent.time_to_first_text_ms" not in root.attributes


class StuckLlm:
    """Never answers, like a long generation the caller gives up on."""

    async def chat(self, messages: list[dict], tools: list[dict], on_text=None) -> ChatResult:
        await asyncio.sleep(3600)


def test_a_cancelled_run_marks_its_span():
    async def run():
        server = build_server(otel=False)
        async with McpToolClient(server, TRACER, agent_name="test") as mcp:
            task = asyncio.create_task(Agent(StuckLlm(), mcp, TRACER, name="test").run("q"))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    (root,) = _spans("invoke_agent")
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["error.type"] == "cancelled"
