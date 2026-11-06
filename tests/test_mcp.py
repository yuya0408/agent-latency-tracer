"""Tests for system/mcp_server + system/mcp_client, connected in-process (no HTTP).

The SDK emits its spans through the global tracer provider, which OTel lets
a process set only once -- so this module installs one in-memory exporter
for all its tests and clears it before each.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from system.mcp_client import McpToolClient
from system.mcp_server.server import RETRIEVAL_TOOL, build_server
from system.tools import mock_server
from tracing.instrumentation import get_tracer, invoke_agent_span

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)

SMALL_CORPUS = 1_000


@pytest.fixture(autouse=True)
def _fast_mock_and_clean_spans(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.0)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.0)
    _EXPORTER.clear()


def _with_client(coro_fn, *, otel: bool = True, pitfalls: frozenset[str] = frozenset()):
    async def run():
        server = build_server(otel=otel, corpus_n=SMALL_CORPUS, pitfalls=pitfalls)
        async with McpToolClient(server, get_tracer(_PROVIDER), agent_name="test") as client:
            return await coro_fn(client)

    return asyncio.run(run())


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_lists_four_business_tools_plus_retrieval():
    tools = _with_client(lambda c: c.list_tools())
    names = [t.name for t in tools]
    assert names == ["describe_data", "find_records", "get_record", "aggregate_records", RETRIEVAL_TOOL]


def test_business_tool_returns_mock_result():
    result = _with_client(lambda c: c.call_tool("find_records", {"type": "item"}))
    assert not result.is_error
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["total"] == len(payload["records"]) == 4


def test_business_tool_error_sets_is_error():
    result = _with_client(lambda c: c.call_tool(
        "find_records", {"type": "item", "filter": [{"field": "code", "op": "eq", "value": "X001"}]}))
    assert result.is_error
    assert _payload(result)["status"] == "error"


def test_pitfall_3_answers_without_records_even_for_a_wrong_field():
    async def calls(client):
        found = await client.call_tool("find_records", {"type": "item"})
        wrong = await client.call_tool(
            "find_records", {"type": "item", "filter": [{"field": "code", "op": "eq", "value": "X001"}]})
        described = await client.call_tool("describe_data", {"type": "item"})
        return found, wrong, described

    found, wrong, described = _with_client(calls, pitfalls=frozenset({"3"}))
    assert _payload(found) == {"tool": "find_records", "arguments": {"type": "item"}, "status": "ok"}
    assert not wrong.is_error and "records" not in _payload(wrong)
    assert "fields" in _payload(described)["result"]  # describe_data keeps its real answer, as before 2026-10-08


def _two_calls_at_once(pitfalls: frozenset[str]) -> float:
    async def calls(client):
        start = time.perf_counter()
        await asyncio.gather(
            client.call_tool("find_records", {"type": "item"}),
            client.call_tool("describe_data", {"type": "item"}),
        )
        return time.perf_counter() - start

    return _with_client(calls, pitfalls=pitfalls)


def test_pitfall_m1_lists_one_tool_per_data_type_and_answers_like_the_baseline():
    async def per_type(client):
        names = [t.name for t in await client.list_tools()]
        found = await client.call_tool("find_sales_order", {"partner_id": "P001"})
        one = await client.call_tool("get_sales_order", {"id": "SO-1003"})
        total = await client.call_tool("aggregate_sales_order", {"op": "sum", "field": "amount", "group_by": ["partner_id"]})
        return names, found, one, total

    async def baseline(client):
        found = await client.call_tool(
            "find_records", {"type": "sales_order", "filter": [{"field": "partner_id", "op": "eq", "value": "P001"}]}
        )
        one = await client.call_tool("get_record", {"type": "sales_order", "id": "SO-1003"})
        total = await client.call_tool(
            "aggregate_records", {"type": "sales_order", "metric": {"op": "sum", "field": "amount"}, "group_by": ["partner_id"]}
        )
        return found, one, total

    names, *m1 = _with_client(per_type, pitfalls=frozenset({"M1"}))
    base = _with_client(baseline)
    assert len(names) == 15 * 3 + 1  # find/get/aggregate per data type, plus retrieval
    assert {"find_sales_order", "get_sales_order", "aggregate_sales_order", RETRIEVAL_TOOL} <= set(names)
    assert "find_records" not in names
    for got, want in zip(m1, base):
        assert not got.is_error
        assert _payload(got) == _payload(want)


def test_concurrent_tool_calls_overlap_on_the_server(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.2)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.2)
    assert _two_calls_at_once(frozenset()) < 0.35


def test_pitfall_2_blocking_wait_serializes_concurrent_tool_calls(monkeypatch):
    monkeypatch.setattr(mock_server, "MIN_LATENCY_S", 0.2)
    monkeypatch.setattr(mock_server, "MAX_LATENCY_S", 0.2)
    assert _two_calls_at_once(frozenset({"2"})) >= 0.4


def test_retrieval_same_query_hits_same_documents():
    async def two_calls(c):
        a = await c.call_tool(RETRIEVAL_TOOL, {"query": "返品の手順", "top_k": 3})
        b = await c.call_tool(RETRIEVAL_TOOL, {"query": "返品の手順", "top_k": 3})
        return a, b

    a, b = _with_client(two_calls)
    assert len(_payload(a)["document_ids"]) == 3
    assert _payload(a) == _payload(b)


def test_unknown_tool_is_an_error_result():
    result = _with_client(lambda c: c.call_tool("no_such_tool", {}))
    assert result.is_error


def _spans_by_name():
    return {s.name: s for s in _EXPORTER.get_finished_spans()}


def test_server_span_nests_under_agent_execute_tool_span():
    async def call_under_root(c):
        with invoke_agent_span(get_tracer(_PROVIDER), agent_name="test"):
            await c.call_tool("find_records", {"type": "item"})

    _with_client(call_under_root)
    spans = _spans_by_name()
    agent = spans["execute_tool find_records"]
    server = spans["tools/call find_records"]
    # In-process there is no JSON-RPC framing, so the SDK's "MCP send" client
    # span is skipped and the server span hangs directly off ours. Over HTTP
    # the client span sits in between and traceparent carries the context;
    # that path is exercised by scripts/try_mcp.py, not here.
    assert server.context.trace_id == agent.context.trace_id
    assert server.parent.span_id == agent.context.span_id


def test_otel_off_server_emits_no_server_spans():
    _with_client(lambda c: c.call_tool("find_records", {"type": "item"}), otel=False)
    names = set(_spans_by_name())
    assert "execute_tool find_records" in names
    assert "tools/call find_records" not in names
