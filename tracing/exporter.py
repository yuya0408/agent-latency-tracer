"""Self-built OTel SpanExporter that persists spans to SQLite.

"収集・保存・可視化レイヤーは自前実装する" -- this implements the standard
OTel SDK `SpanExporter` interface (the seam collectors/vendors normally sit
behind) but writes structured rows to a local SQLite file instead of shipping
to an OTel Collector or a SaaS backend. One generic `spans` table (not one
table per span kind) so a changed set of span names never requires a schema
migration -- only the attributes_json payload varies by span kind.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL PRIMARY KEY,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_time_unix_nano INTEGER NOT NULL,
    end_time_unix_nano INTEGER NOT NULL,
    status_code TEXT NOT NULL,
    status_description TEXT,
    attributes_json TEXT NOT NULL,
    resource_json TEXT NOT NULL
);
"""
_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);"
_INSERT_SQL = """
INSERT OR REPLACE INTO spans (
    trace_id, span_id, parent_span_id, name, kind,
    start_time_unix_nano, end_time_unix_nano,
    status_code, status_description, attributes_json, resource_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""


class SQLiteSpanExporter(SpanExporter):
    def __init__(self, db_path: str | Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_SCHEMA_SQL)
        self._conn.execute(_INDEX_SQL)
        self._conn.commit()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            rows = [_span_to_row(span) for span in spans]
            self._conn.executemany(_INSERT_SQL, rows)
            self._conn.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._conn.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def _span_to_row(span: ReadableSpan) -> tuple[Any, ...]:
    ctx = span.context
    parent_span_id = f"{span.parent.span_id:016x}" if span.parent else None
    attributes = dict(span.attributes) if span.attributes else {}
    resource_attrs = dict(span.resource.attributes) if span.resource else {}
    status = span.status
    return (
        f"{ctx.trace_id:032x}",
        f"{ctx.span_id:016x}",
        parent_span_id,
        span.name,
        span.kind.name,
        span.start_time,
        span.end_time,
        status.status_code.name,
        status.description,
        json.dumps(attributes, default=str),
        json.dumps(resource_attrs, default=str),
    )


def list_traces(db_path: str | Path) -> list[dict[str, Any]]:
    """Root spans (no parent) with their trace_id, name, and duration -- one row per trace."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT trace_id, name, start_time_unix_nano, end_time_unix_nano
            FROM spans
            WHERE parent_span_id IS NULL
            ORDER BY start_time_unix_nano
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "trace_id": trace_id,
            "name": name,
            "start_time_unix_nano": start,
            "end_time_unix_nano": end,
            "duration_ms": (end - start) / 1_000_000,
        }
        for trace_id, name, start, end in rows
    ]


def read_trace(db_path: str | Path, trace_id: str) -> list[dict[str, Any]]:
    """All spans belonging to one trace, ordered by start time, attributes decoded."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time_unix_nano",
            (trace_id,),
        ).fetchall()
    finally:
        conn.close()
    spans = []
    for row in rows:
        span = dict(row)
        span["attributes"] = json.loads(span.pop("attributes_json"))
        span["resource"] = json.loads(span.pop("resource_json"))
        spans.append(span)
    return spans
