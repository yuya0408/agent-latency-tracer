"""Tests for system/mcp_server + system/mcp_client, connected in-process (no HTTP).

The SDK emits its spans through the global tracer provider, which OTel lets
a process set only once -- so this module installs one in-memory exporter
for all its tests and clears it before each.
"""
from __future__ import annotations

import asyncio
import json

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


def _with_client(coro_fn, *, otel: bool = True):
    async def run():
        server = build_server(otel=otel, corpus_n=SMALL_CORPUS)
        async with McpToolClient(server, get_tracer(_PROVIDER), agent_name="test") as client:
            return await coro_fn(client)

    return asyncio.run(run())


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


def test_lists_forty_business_tools_plus_retrieval():
    tools = _with_client(lambda c: c.list_tools())
    names = [t.name for t in tools]
    assert len(names) == 41
    assert names[-1] == RETRIEVAL_TOOL
    assert "x-scoring" not in json.dumps([t.input_schema for t in tools])


def test_business_tool_returns_mock_result():
    result = _with_client(lambda c: c.call_tool("search_master", {"entity": "item"}))
    assert not result.is_error
    assert _payload(result) == {"tool": "search_master", "arguments": {"entity": "item"}, "status": "ok"}


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
            await c.call_tool("search_master", {"entity": "item"})

    _with_client(call_under_root)
    spans = _spans_by_name()
    agent = spans["execute_tool search_master"]
    server = spans["tools/call search_master"]
    # In-process there is no JSON-RPC framing, so the SDK's "MCP send" client
    # span is skipped and the server span hangs directly off ours. Over HTTP
    # the client span sits in between and traceparent carries the context;
    # that path is exercised by scripts/try_mcp.py, not here.
    assert server.context.trace_id == agent.context.trace_id
    assert server.parent.span_id == agent.context.span_id


def test_otel_off_server_emits_no_server_spans():
    _with_client(lambda c: c.call_tool("search_master", {"entity": "item"}), otel=False)
    names = set(_spans_by_name())
    assert "execute_tool search_master" in names
    assert "tools/call search_master" not in names
