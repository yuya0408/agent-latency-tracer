"""Tests for tracing/viewer/waterfall.py.

Same synthetic-pipeline approach as test_tracing.py (no dependency on
system/). Checks structural invariants of the generated HTML rather than
pixel-level rendering -- there's no headless browser in this project's
tooling, so "does it look right" is a manual step (see dataviz skill's
"render it and look at it").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tracing.exporter import SQLiteSpanExporter
from tracing.instrumentation import (
    build_tracer_provider,
    chat_span,
    execute_tool_span,
    get_tracer,
    invoke_agent_span,
    mark_error,
    retrieval_span,
)
from tracing.viewer.waterfall import (
    _OPERATION_COLOR_VARS,
    _order_spans_depth_first,
    render_index_html,
    render_trace_html,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "traces.db"


def _run_pipeline(db_path: Path, *, agent_name: str = "erp-chat", with_error: bool = False) -> str:
    exporter = SQLiteSpanExporter(db_path)
    provider = build_tracer_provider(exporter)
    tracer = get_tracer(provider)

    with invoke_agent_span(tracer, agent_name=agent_name) as root:
        trace_id = f"{root.get_span_context().trace_id:032x}"
        with retrieval_span(tracer, data_source_id="erp-docs", top_k=5):
            pass
        with execute_tool_span(tracer, tool_name="check_inventory", tool_call_id="call_1") as tspan:
            if with_error:
                mark_error(tspan, "timeout", "tool call timed out")
        with chat_span(tracer, request_model="qwen3-8b", provider_name="local"):
            pass
    provider.shutdown()
    return trace_id


def test_order_spans_depth_first_puts_children_immediately_after_parent():
    spans = [
        {"span_id": "root", "parent_span_id": None, "start_time_unix_nano": 0},
        {"span_id": "c2", "parent_span_id": "root", "start_time_unix_nano": 200},
        {"span_id": "c1", "parent_span_id": "root", "start_time_unix_nano": 100},
        {"span_id": "gc1", "parent_span_id": "c1", "start_time_unix_nano": 150},
    ]
    ordered = _order_spans_depth_first(spans)
    ids_and_depths = [(s["span_id"], depth) for s, depth in ordered]
    # c1 starts before c2, so it (and its child) must come right after root,
    # even though c2 appears earlier in the input list.
    assert ids_and_depths == [("root", 0), ("c1", 1), ("gc1", 2), ("c2", 1)]


def test_render_trace_html_contains_one_bar_and_row_per_span(db_path):
    trace_id = _run_pipeline(db_path)

    out = render_trace_html(db_path, trace_id)

    assert out.count("<rect") == 4  # invoke_agent + retrieval + execute_tool + chat
    assert out.count("<tr>") == 5  # 1 header row + 4 span rows in the table view
    assert "<svg" in out
    assert "prefers-color-scheme: dark" in out
    # without this, browsers guess the encoding and non-ASCII text (⚠, 日本語
    # query text) can render as mojibake -- caught by manual review 2026-09-15
    assert out.startswith('<meta charset="utf-8">')


def test_render_trace_html_uses_fixed_palette_for_each_operation(db_path):
    trace_id = _run_pipeline(db_path)
    out = render_trace_html(db_path, trace_id)

    # every operation present in this pipeline must show up in the legend
    for operation in ("invoke_agent", "retrieval", "execute_tool", "chat"):
        assert operation in out
        assert _OPERATION_COLOR_VARS[operation] in out


def test_render_trace_html_marks_error_spans_without_recoloring_the_bar(db_path):
    trace_id = _run_pipeline(db_path, with_error=True)
    out = render_trace_html(db_path, trace_id)

    assert "bar-error" in out
    assert "⚠" in out  # status carried by icon + label, not color alone
    # the erroring span is still execute_tool -- same categorical color var
    # as a non-erroring execute_tool span would get, not swapped for a status color
    assert "var(--op-execute-tool)" in out


def test_render_trace_html_raises_for_unknown_trace_id(db_path):
    _run_pipeline(db_path)
    with pytest.raises(ValueError):
        render_trace_html(db_path, "0" * 32)


def test_render_index_html_lists_every_trace(db_path):
    trace_id_1 = _run_pipeline(db_path, agent_name="erp-chat-1")
    trace_id_2 = _run_pipeline(db_path, agent_name="erp-chat-2")

    out = render_index_html(db_path)

    assert out.count("<tr>") == 3  # 1 header row + 2 trace rows
    assert trace_id_1 in out
    assert trace_id_2 in out
