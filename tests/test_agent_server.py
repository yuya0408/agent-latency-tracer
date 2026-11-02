"""Tests for system/agent_server.py: the routes, with a stand-in for the agent.

The agent itself is tested in test_agent.py; here it only opens a span, so a
test can see which trace the invocation joined.
"""
from __future__ import annotations

import asyncio

import httpx2
from opentelemetry.sdk.trace import TracerProvider

from system.agent import AgentResult
from system.agent_server import app
from tracing.instrumentation import get_tracer

TRACER = get_tracer(TracerProvider())
TRACE_ID = "6ae70de7647c4bd306e2ff190b0924d6"


class StubAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, question: str, *, conversation_id: str | None = None) -> AgentResult:
        self.calls.append((question, conversation_id))
        with TRACER.start_as_current_span("invoke_agent") as span:
            trace_id = f"{span.get_span_context().trace_id:032x}"
        return AgentResult("120個です。", [], 2, "answer", trace_id)


def _request(method: str, path: str, **kwargs) -> tuple[httpx2.Response, StubAgent]:
    agent = StubAgent()
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
