"""Tests for system/memory.py against botocore's Stubber (no AWS calls).

The Stubber checks each request against the service model, so these also
catch a parameter AgentCore Memory would reject.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from botocore.stub import ANY, Stubber
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from system.memory import ACTOR_ID, AgentCoreMemory
from tracing.instrumentation import get_tracer

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
TRACER = get_tracer(_PROVIDER)

MEMORY_ID = "agent_latency_tracer-abcdefghij"
# Real responses carry it; the Stubber's don't unless given.
META = {"ResponseMetadata": {"RetryAttempts": 0}}


@pytest.fixture
def memory(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    _EXPORTER.clear()
    m = AgentCoreMemory(MEMORY_ID, TRACER)
    with Stubber(m._client) as stubber:
        m.stubber = stubber
        yield m
        stubber.assert_no_pending_responses()


def _event(seq: int, message: dict) -> dict:
    return {
        "memoryId": MEMORY_ID, "actorId": ACTOR_ID, "sessionId": "s1", "eventId": f"e{seq}",
        "eventTimestamp": datetime(2026, 11, 9, tzinfo=timezone.utc),
        "payload": [{"conversational": {"role": "USER", "content": {"text": json.dumps(message)}}}],
        "metadata": {"seq": {"stringValue": str(seq)}},
    }


def test_load_reads_every_page_and_orders_by_seq(memory):
    first, second, third = ({"role": "user", "content": f"q{i}"} for i in range(3))
    params = {"memoryId": MEMORY_ID, "actorId": ACTOR_ID, "sessionId": "s1", "maxResults": 100}
    memory.stubber.add_response("list_events", {**META, "events": [_event(2, third), _event(0, first)], "nextToken": "t"}, params)
    memory.stubber.add_response("list_events", {**META, "events": [_event(1, second)]}, {**params, "nextToken": "t"})
    assert asyncio.run(memory.load("s1")) == [first, second, third]
    (span,) = _EXPORTER.get_finished_spans()
    assert span.name == "BedrockAgentCore.ListEvents"
    assert span.attributes["agent.memory.events"] == 3


def test_append_writes_one_event_per_message_after_the_loaded_ones(memory):
    memory.stubber.add_response("list_events", {**META, "events": [_event(0, {"role": "user", "content": "q0"})]})
    tool_message = {"role": "tool", "tool_call_id": "c1", "content": "結果"}
    memory.stubber.add_response(
        "create_event",
        {**META, "event": _event(1, tool_message)},
        {
            "memoryId": MEMORY_ID, "actorId": ACTOR_ID, "sessionId": "s1", "eventTimestamp": ANY,
            "payload": [{"conversational": {
                "role": "TOOL", "content": {"text": json.dumps(tool_message, ensure_ascii=False)},
            }}],
            "metadata": {"seq": {"stringValue": "1"}},
        },
    )

    async def run():
        await memory.load("s1")
        await memory.append("s1", tool_message)

    asyncio.run(run())
    assert [s.name for s in _EXPORTER.get_finished_spans()] == ["BedrockAgentCore.ListEvents", "BedrockAgentCore.CreateEvent"]
