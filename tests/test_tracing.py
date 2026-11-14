"""Tests for tracing/instrumentation.py.

These run a synthetic retrieval -> tool-call -> generation pipeline, kept
independent of system/ here so tracing/'s own span taxonomy can be verified
in isolation, including the signal used to tell sequential from parallel tool
calls: parallel calls must produce spans whose time ranges overlap,
sequential ones must not.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from tracing.instrumentation import (
    build_tracer_provider,
    chat_span,
    execute_tool_span,
    get_tracer,
    invoke_agent_span,
    mark_error,
    retrieval_span,
    set_chat_usage,
    set_retrieval_results,
    set_tool_call_io,
)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def _make_tracer(exporter: InMemorySpanExporter):
    provider = build_tracer_provider(exporter)
    return provider, get_tracer(provider)


def _spans(exporter: InMemorySpanExporter, trace_id: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if f"{s.context.trace_id:032x}" == trace_id]


def _run_synthetic_pipeline(tracer, *, capture_content: bool) -> str:
    with invoke_agent_span(tracer, agent_name="erp-chat", conversation_id="conv-1") as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"

        with retrieval_span(
            tracer, data_source_id="erp-docs", top_k=5, query_text="在庫は?", capture_content=capture_content
        ) as rspan:
            set_retrieval_results(rspan, [{"id": "doc_1", "score": 0.9}], capture_content=capture_content)

        with execute_tool_span(tracer, tool_name="check_inventory", tool_call_id="call_1") as tspan:
            set_tool_call_io(
                tspan, arguments={"sku": "A1"}, result={"qty": 120}, capture_content=capture_content
            )

        with chat_span(tracer, request_model="qwen3-8b", provider_name="local") as cspan:
            set_chat_usage(
                cspan, input_tokens=1200, output_tokens=80, response_model="qwen3-8b-0912", finish_reasons=["stop"]
            )
    return trace_id


def test_pipeline_persists_expected_span_tree(exporter):
    provider, tracer = _make_tracer(exporter)
    trace_id = _run_synthetic_pipeline(tracer, capture_content=False)
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    assert len(spans) == 4  # invoke_agent + retrieval + execute_tool + chat
    assert all(f"{s.context.trace_id:032x}" == trace_id for s in spans)

    by_name = {s.name: s for s in spans}
    root = by_name["invoke_agent erp-chat"]
    assert root.parent is None
    assert root.kind == SpanKind.INTERNAL
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.agent.name"] == "erp-chat"
    assert root.attributes["gen_ai.conversation.id"] == "conv-1"

    retrieval = by_name["retrieval erp-docs"]
    assert retrieval.kind == SpanKind.CLIENT
    assert retrieval.parent.span_id == root.context.span_id
    assert retrieval.attributes["gen_ai.operation.name"] == "retrieval"
    assert retrieval.attributes["gen_ai.retrieval.top_k"] == 5

    tool = by_name["execute_tool check_inventory"]
    assert tool.kind == SpanKind.INTERNAL
    assert tool.parent.span_id == root.context.span_id
    assert tool.attributes["gen_ai.tool.name"] == "check_inventory"
    assert tool.attributes["gen_ai.tool.call.id"] == "call_1"
    assert tool.attributes["gen_ai.tool.type"] == "function"

    chat = by_name["chat qwen3-8b"]
    assert chat.kind == SpanKind.CLIENT
    assert chat.parent.span_id == root.context.span_id
    assert chat.attributes["gen_ai.request.model"] == "qwen3-8b"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 1200
    assert chat.attributes["gen_ai.usage.output_tokens"] == 80
    assert chat.attributes["gen_ai.response.finish_reasons"] == ("stop",)


def test_capture_content_false_omits_sensitive_attributes(exporter):
    provider, tracer = _make_tracer(exporter)
    trace_id = _run_synthetic_pipeline(tracer, capture_content=False)
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    retrieval = next(s for s in spans if s.name == "retrieval erp-docs")
    tool = next(s for s in spans if s.name == "execute_tool check_inventory")

    assert "gen_ai.retrieval.query.text" not in retrieval.attributes
    assert "gen_ai.retrieval.documents" not in retrieval.attributes
    assert "gen_ai.tool.call.arguments" not in tool.attributes
    assert "gen_ai.tool.call.result" not in tool.attributes


def test_capture_content_true_records_sensitive_attributes(exporter):
    provider, tracer = _make_tracer(exporter)
    trace_id = _run_synthetic_pipeline(tracer, capture_content=True)
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    retrieval = next(s for s in spans if s.name == "retrieval erp-docs")
    tool = next(s for s in spans if s.name == "execute_tool check_inventory")

    assert retrieval.attributes["gen_ai.retrieval.query.text"] == "在庫は?"
    documents = json.loads(retrieval.attributes["gen_ai.retrieval.documents"])
    assert documents == [{"id": "doc_1", "score": 0.9}]

    arguments = json.loads(tool.attributes["gen_ai.tool.call.arguments"])
    result = json.loads(tool.attributes["gen_ai.tool.call.result"])
    assert arguments == {"sku": "A1"}
    assert result == {"qty": 120}


def test_mark_error_sets_status_and_error_type(exporter):
    provider, tracer = _make_tracer(exporter)
    with invoke_agent_span(tracer, agent_name="erp-chat") as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        with execute_tool_span(tracer, tool_name="check_inventory") as tspan:
            mark_error(tspan, "timeout", "tool call timed out")
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    tool = next(s for s in spans if s.name == "execute_tool check_inventory")
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.status.description == "tool call timed out"
    assert tool.attributes["error.type"] == "timeout"


async def _call_tool(tracer, name: str, call_id: str, delay_s: float) -> None:
    with execute_tool_span(tracer, tool_name=name, tool_call_id=call_id) as span:
        await asyncio.sleep(delay_s)
        set_tool_call_io(span, arguments={}, result={}, capture_content=False)


def test_parallel_tool_calls_produce_overlapping_spans(exporter):
    """This is the mechanism classifying pitfall #1 relies on: fan-out
    tool calls must show overlapping time ranges under the same parent."""
    provider, tracer = _make_tracer(exporter)

    async def run() -> str:
        with invoke_agent_span(tracer, agent_name="erp-chat") as root:
            trace_id = f"{root.get_span_context().trace_id:032x}"
            await asyncio.gather(
                _call_tool(tracer, "check_inventory", "call_1", 0.05),
                _call_tool(tracer, "get_price", "call_2", 0.05),
            )
        return trace_id

    trace_id = asyncio.run(run())
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    root = next(s for s in spans if s.name == "invoke_agent erp-chat")
    tool_spans = [s for s in spans if s.name.startswith("execute_tool ")]
    assert len(tool_spans) == 2
    assert all(s.parent.span_id == root.context.span_id for s in tool_spans)

    a, b = tool_spans
    overlaps = a.start_time < b.end_time and b.start_time < a.end_time
    assert overlaps


def test_sequential_tool_calls_do_not_overlap(exporter):
    """Contrast case for the above: awaiting one call after another must not
    overlap -- this is the 'before' shape of pitfall #1 (the direct-await bug)."""
    provider, tracer = _make_tracer(exporter)

    async def run() -> str:
        with invoke_agent_span(tracer, agent_name="erp-chat") as root:
            trace_id = f"{root.get_span_context().trace_id:032x}"
            await _call_tool(tracer, "check_inventory", "call_1", 0.02)
            await _call_tool(tracer, "get_price", "call_2", 0.02)
        return trace_id

    trace_id = asyncio.run(run())
    provider.shutdown()

    spans = _spans(exporter, trace_id)
    tool_spans = sorted(
        (s for s in spans if s.name.startswith("execute_tool ")),
        key=lambda s: s.start_time,
    )
    assert len(tool_spans) == 2
    first, second = tool_spans
    assert first.end_time <= second.start_time


def test_trace_ids_start_with_the_start_time(exporter):
    # X-Ray format: the CloudWatch console reads the first 8 hex digits as the
    # trace's start time (docs/DESIGN.md §5).
    provider, tracer = _make_tracer(exporter)
    before = int(time.time())
    trace_id = _run_synthetic_pipeline(tracer, capture_content=False)
    after = int(time.time())
    provider.shutdown()

    assert before <= int(trace_id[:8], 16) <= after


def test_spans_carry_when_the_process_started(exporter):
    """Every span's resource has process.creation.time, so a span's start minus
    it says how long the process had been up (a fresh microVM or not)."""
    provider, tracer = _make_tracer(exporter)
    trace_id = _run_synthetic_pipeline(tracer, capture_content=False)
    provider.shutdown()

    for span in _spans(exporter, trace_id):
        started = datetime.fromisoformat(span.resource.attributes["process.creation.time"])
        assert started.tzinfo is not None
        assert started <= datetime.now(timezone.utc)
