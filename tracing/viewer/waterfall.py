"""Self-built waterfall (Gantt-style) viewer for traces stored by exporter.py.

Renders a single, dependency-free HTML file (inline SVG, no JS framework, no
CDN, no server) -- "サインアップ不要で誰でも再現できる状態を維持する" per
README. Open the file directly in a browser.

Palette/layout choices follow the dataviz skill's method (2026-09-14): fixed
categorical color order per span operation (never reassigned per view),
validated with scripts/validate_palette.js, legend + table view present
(>= 2 series), status (error) encoded as an outline + icon rather than a
color swap so it never impersonates a categorical series.
"""

from __future__ import annotations

import html
from pathlib import Path
from string import Template
from typing import Any

from tracing.exporter import list_traces, read_trace

# Fixed order, validated for adjacent-pair CVD separation in both light and
# dark mode (dataviz skill's validate_palette.js, 2026-09-14). Do not reorder
# per-trace -- the same operation must always get the same color.
_OPERATION_COLOR_VARS: dict[str, str] = {
    "invoke_agent": "--op-invoke-agent",
    "retrieval": "--op-retrieval",
    "execute_tool": "--op-execute-tool",
    "chat": "--op-chat",
}
_UNKNOWN_COLOR_VAR = "--op-unknown"

_ROW_HEIGHT = 28
_BAR_HEIGHT = 20
_LABEL_COL_WIDTH = 260
_CHART_WIDTH = 640
_MARGIN = 16
_INDENT_PER_DEPTH = 14
_AXIS_HEIGHT = 24


def _order_spans_depth_first(spans: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """(span, depth) pairs, root first, each span's children immediately
    following it (sorted by start time among siblings) -- so concurrent
    siblings land on adjacent rows even though their time ranges overlap.
    """
    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        children.setdefault(span["parent_span_id"], []).append(span)
    for kids in children.values():
        kids.sort(key=lambda s: s["start_time_unix_nano"])

    ordered: list[tuple[dict[str, Any], int]] = []

    def visit(parent_id: str | None, depth: int) -> None:
        for child in children.get(parent_id, []):
            ordered.append((child, depth))
            visit(child["span_id"], depth + 1)

    visit(None, 0)
    return ordered


def _operation_of(span: dict[str, Any]) -> str:
    return span["attributes"].get("gen_ai.operation.name", "unknown")


def _color_var(operation: str) -> str:
    return _OPERATION_COLOR_VARS.get(operation, _UNKNOWN_COLOR_VAR)


def _fmt_ms(value_ms: float) -> str:
    return f"{value_ms:.0f}ms" if value_ms >= 10 else f"{value_ms:.1f}ms"


def render_trace_html(db_path: str | Path, trace_id: str) -> str:
    spans = read_trace(db_path, trace_id)
    if not spans:
        raise ValueError(f"no spans found for trace_id={trace_id!r} in {db_path}")
    return render_spans_html(spans)


def render_spans_html(spans: list[dict[str, Any]]) -> str:
    """The waterfall for one trace's spans, in the dict shape read_trace returns."""
    ordered = _order_spans_depth_first(spans)
    trace_start = min(s["start_time_unix_nano"] for s in spans)
    trace_end = max(s["end_time_unix_nano"] for s in spans)
    total_ms = max((trace_end - trace_start) / 1_000_000, 1e-6)

    chart_height = len(ordered) * _ROW_HEIGHT
    svg_height = _AXIS_HEIGHT + chart_height + _MARGIN
    svg_width = _LABEL_COL_WIDTH + _CHART_WIDTH + _MARGIN * 2

    rows_svg: list[str] = []
    table_rows: list[str] = []
    used_operations: list[str] = []

    for i, (span, depth) in enumerate(ordered):
        y = _AXIS_HEIGHT + i * _ROW_HEIGHT
        start_offset_ms = (span["start_time_unix_nano"] - trace_start) / 1_000_000
        duration_ms = (span["end_time_unix_nano"] - span["start_time_unix_nano"]) / 1_000_000
        bar_x = _LABEL_COL_WIDTH + (start_offset_ms / total_ms) * _CHART_WIDTH
        bar_w = max((duration_ms / total_ms) * _CHART_WIDTH, 2.0)

        operation = _operation_of(span)
        if operation not in used_operations:
            used_operations.append(operation)
        color_var = _color_var(operation)
        is_error = span["status_code"] == "ERROR"

        label_x = _MARGIN + depth * _INDENT_PER_DEPTH
        row_label = ("⚠ " if is_error else "") + span["name"]
        rows_svg.append(
            f'<text x="{label_x}" y="{y + _BAR_HEIGHT / 2 + 4}" class="row-label">'
            f"{html.escape(row_label)}</text>"
        )

        tip_text = _fmt_ms(duration_ms)
        tip_x = bar_x + bar_w + 6
        tip_fits = tip_x + len(tip_text) * 6.5 < svg_width - _MARGIN
        tip_svg = (
            f'<text x="{tip_x:.1f}" y="{y + _BAR_HEIGHT / 2 + 4}" class="bar-tip-label">{tip_text}</text>'
            if tip_fits
            else ""
        )

        tooltip = (
            f"{span['name']}\n{operation}\nstart +{start_offset_ms:.1f}ms  duration {duration_ms:.1f}ms"
            + ("\nERROR: " + (span["status_description"] or "") if is_error else "")
        )
        bar_class = "bar bar-error" if is_error else "bar"
        rows_svg.append(
            f'<g class="bar-row" tabindex="0">'
            f'<rect class="{bar_class}" x="{bar_x:.1f}" y="{y}" width="{bar_w:.1f}" height="{_BAR_HEIGHT}" '
            f'rx="4" ry="4" fill="var({color_var})">'
            f"<title>{html.escape(tooltip)}</title></rect>"
            f"{tip_svg}"
            f"</g>"
        )

        table_rows.append(
            "<tr>"
            f'<td style="padding-left: calc(10px + {depth} * 14px)">'
            f'{html.escape(("[ERROR] " if is_error else "") + span["name"])}</td>'
            f"<td>{html.escape(operation)}</td>"
            f'<td class="num">{start_offset_ms:.1f}</td>'
            f'<td class="num">{duration_ms:.1f}</td>'
            "</tr>"
        )

    # 5 evenly-spaced time-axis ticks. Not "nice round numbers" (out of scope
    # for this MVP viewer) -- just proportional fractions of the trace span.
    ticks_svg = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        tick_ms = total_ms * frac
        tick_x = _LABEL_COL_WIDTH + frac * _CHART_WIDTH
        ticks_svg.append(
            f'<line x1="{tick_x:.1f}" y1="{_AXIS_HEIGHT}" x2="{tick_x:.1f}" y2="{svg_height - _MARGIN}" class="gridline" />'
            f'<text x="{tick_x:.1f}" y="{_AXIS_HEIGHT - 8}" class="axis-label">{_fmt_ms(tick_ms)}</text>'
        )

    legend_svg = []
    for operation in used_operations:
        legend_svg.append(
            f'<span class="legend-item">'
            f'<span class="legend-swatch" style="background:var({_color_var(operation)})"></span>'
            f"{html.escape(operation)}</span>"
        )

    root_span = next(s for s in spans if s["parent_span_id"] is None)
    title = f"{root_span['name']} ({total_ms:.1f}ms, {len(spans)} spans)"

    return _PAGE_TEMPLATE.substitute(
        title=html.escape(title),
        svg_width=svg_width,
        svg_height=svg_height,
        ticks=" ".join(ticks_svg),
        rows="".join(rows_svg),
        legend="".join(legend_svg),
        table_rows="".join(table_rows),
    )


def render_index_html(db_path: str | Path) -> str:
    """A minimal, dependency-free listing of every trace in db_path."""
    traces = list_traces(db_path)
    rows = "".join(
        "<tr>"
        f'<td>{html.escape(t["trace_id"])}</td>'
        f'<td>{html.escape(t["name"])}</td>'
        f'<td class="num">{t["duration_ms"]:.1f}</td>'
        "</tr>"
        for t in traces
    )
    return _INDEX_TEMPLATE.substitute(rows=rows, count=len(traces))


_STYLE = """
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --surface-page: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --gridline: #e1e0d9;
  --border: rgba(11,11,11,0.10);
  --status-critical: #d03b3b;
  --op-invoke-agent: #2a78d6;
  --op-retrieval: #eb6834;
  --op-execute-tool: #1baf7a;
  --op-chat: #eda100;
  --op-unknown: #898781;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --surface-page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --gridline: #2c2c2a;
    --border: rgba(255,255,255,0.10);
    --status-critical: #e66767;
    --op-invoke-agent: #3987e5;
    --op-retrieval: #d95926;
    --op-execute-tool: #199e70;
    --op-chat: #c98500;
    --op-unknown: #898781;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px;
  background: var(--surface-page); color: var(--text-primary);
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 15px; font-weight: 600; margin: 0 0 12px; }
.viz-root {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; margin-bottom: 24px;
}
.legend { margin: 0 0 12px; }
.legend-item {
  display: inline-flex; align-items: center; gap: 6px; margin-right: 16px;
  color: var(--text-secondary); font-size: 12px;
}
.legend-swatch { width: 12px; height: 12px; border-radius: 2px; display: inline-block; }
.gridline { stroke: var(--gridline); stroke-width: 1; }
.axis-label { fill: var(--text-muted); font-size: 10px; }
.row-label { fill: var(--text-primary); font-size: 12px; dominant-baseline: middle; }
.bar-tip-label { fill: var(--text-secondary); font-size: 11px; font-variant-numeric: tabular-nums; }
.bar { transition: filter 0.1s ease; }
.bar-row:hover .bar, .bar-row:focus .bar { filter: brightness(1.15); }
.bar-row:focus { outline: none; }
.bar-row:focus .bar { outline: 2px solid var(--text-primary); outline-offset: 1px; }
.bar-error { stroke: var(--status-critical); stroke-width: 2px; }
table { border-collapse: collapse; width: 100%; font-size: 12px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 600; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
"""

_PAGE_TEMPLATE = Template(
    '<meta charset="utf-8"><title>$title</title><style>' + _STYLE + """</style>
<h1>$title</h1>
<div class="viz-root">
  <div class="legend">$legend</div>
  <svg width="$svg_width" height="$svg_height" role="img" aria-label="trace waterfall">
    $ticks
    $rows
  </svg>
</div>
<div class="viz-root">
  <table>
    <thead><tr><th>span</th><th>operation</th><th class="num">start (ms)</th><th class="num">duration (ms)</th></tr></thead>
    <tbody>$table_rows</tbody>
  </table>
</div>
"""
)

_INDEX_TEMPLATE = Template(
    '<meta charset="utf-8"><title>traces ($count)</title><style>' + _STYLE + """</style>
<h1>traces ($count)</h1>
<div class="viz-root">
  <table>
    <thead><tr><th>trace_id</th><th>root span</th><th class="num">duration (ms)</th></tr></thead>
    <tbody>$rows</tbody>
  </table>
</div>
"""
)
