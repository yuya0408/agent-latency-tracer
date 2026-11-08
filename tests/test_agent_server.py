"""Tests for system/agent_server.py: the routes, with a stand-in for the agent.

The agent itself is tested in test_agent.py; here it only opens a span, so a
test can see which trace the invocation joined.
"""
from __future__ import annotations

import asyncio
import json

import httpx2
from opentelemetry.sdk.trace import TracerProvider

from system.agent import AgentResult
from system import agent_server
from system.agent_server import app
from tracing.instrumentation import get_tracer

TRACER = get_tracer(TracerProvider())
TRACE_ID = "6ae70de7647c4bd306e2ff190b0924d6"


class StubAgent:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self._fail = fail

    async def run(self, question: str, *, conversation_id: str | None = None, on_text=None) -> AgentResult:
        self.calls.append((question, conversation_id))
        with TRACER.start_as_current_span("invoke_agent") as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
            for piece in ("120個", "です。"):
                if on_text:
                    on_text(piece)
                await asyncio.sleep(0)
            if self._fail:
                raise RuntimeError("LLM went away")
        return AgentResult("120個です。", [], 2, "answer", trace_id)


def _request(method: str, path: str, agent: StubAgent | None = None, **kwargs) -> tuple[httpx2.Response, StubAgent]:
    agent = agent or StubAgent()
    app.state.agent = agent

    async def run():
        # The ASGI transport doesn't run the lifespan, so no MCP connection opens.
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run()), agent


def test_ping():
    response, _ = _request("GET", "/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "Healthy"}


def test_invocation_runs_the_agent_once():
    response, agent = _request("POST", "/invocations", json={"question": "在庫は?"})
    assert response.status_code == 200
    assert agent.calls == [("在庫は?", None)]
    body = response.json()
    assert body["answer"] == "120個です。"
    assert body["turns"] == 2
    assert body["stop_reason"] == "answer"


def test_invocation_joins_the_callers_trace():
    response, _ = _request(
        "POST",
        "/invocations",
        json={"question": "在庫は?"},
        headers={"traceparent": f"00-{TRACE_ID}-1b1f587049a6de4d-01"},
    )
    assert response.json()["trace_id"] == TRACE_ID


def test_invocation_without_a_traceparent_starts_its_own_trace():
    response, _ = _request("POST", "/invocations", json={"question": "在庫は?"})
    assert response.json()["trace_id"] != TRACE_ID


def test_session_id_becomes_the_conversation_id():
    _, agent = _request(
        "POST",
        "/invocations",
        json={"question": "在庫は?"},
        headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "session-1"},
    )
    assert agent.calls == [("在庫は?", "session-1")]


def test_bad_bodies_are_rejected():
    for kwargs in ({"content": b"not json"}, {"json": {"text": "在庫は?"}}, {"json": ["在庫は?"]}):
        response, agent = _request("POST", "/invocations", **kwargs)
        assert response.status_code == 400
        assert agent.calls == []


def _events(response: httpx2.Response) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]


def test_streamed_invocation_sends_the_pieces_then_the_summary():
    response, agent = _request(
        "POST",
        "/invocations",
        json={"question": "在庫は?"},
        headers={"accept": "text/event-stream", "traceparent": f"00-{TRACE_ID}-1b1f587049a6de4d-01"},
    )
    assert response.headers["content-type"].startswith("text/event-stream")
    assert agent.calls == [("在庫は?", None)]
    *pieces, summary = _events(response)
    assert pieces == [{"text": "120個"}, {"text": "です。"}]
    assert summary == {"answer": "120個です。", "turns": 2, "stop_reason": "answer", "trace_id": TRACE_ID}


def test_streamed_invocation_ends_with_the_error_when_the_agent_fails():
    response, _ = _request(
        "POST", "/invocations", StubAgent(fail=True), json={"question": "在庫は?"}, headers={"accept": "text/event-stream"}
    )
    assert response.status_code == 200  # sent before the agent failed
    assert _events(response)[-1] == {"error": "RuntimeError: LLM went away"}


def test_a_ping_line_goes_out_while_there_is_nothing_to_send(monkeypatch):
    monkeypatch.setattr(agent_server, "PING_INTERVAL_S", 0.01)

    async def run(on_text):
        await asyncio.sleep(0.1)  # tools running
        on_text("120個です。")
        return AgentResult("120個です。", [], 2, "answer", TRACE_ID)

    async def collect():
        return [line async for line in agent_server._events(run)]

    lines = asyncio.run(collect())
    assert ": ping\n\n" in lines
    assert lines[-2:] == [
        'data: {"text": "120個です。"}\n\n',
        f'data: {{"answer": "120個です。", "turns": 2, "stop_reason": "answer", "trace_id": "{TRACE_ID}"}}\n\n',
    ]


def test_closing_the_stream_stops_the_agent(monkeypatch):
    monkeypatch.setattr(agent_server, "PING_INTERVAL_S", 0.01)
    stopped = asyncio.Event()

    async def run(on_text):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            stopped.set()
            raise

    async def go():
        events = agent_server._events(run)
        assert await anext(events) == ": ping\n\n"
        await events.aclose()  # what the server does when the caller goes away
        await asyncio.wait_for(stopped.wait(), 1.0)

    asyncio.run(go())
