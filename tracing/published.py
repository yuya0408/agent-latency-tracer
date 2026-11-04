"""Turn a trace into the masked JSON kept under traces/ in the repository.

Spans come from the local SQLite file (exporter.read_trace) or from
CloudWatch, which returns each span as an OTel JSON document. Both end up in
the read_trace dict shape, so the waterfall renders either.

CloudWatch adds attributes of its own, some naming the AWS account (account
ID, ARNs), the VPC (peer IP) and the endpoint (host, URL). Only the keys
below are kept, and any account ID or ARN left in a kept string value is
replaced as well.
"""

from __future__ import annotations

import re
from typing import Any

_KEEP_PREFIXES = ("gen_ai.", "mcp.", "vllm.", "llamacpp.", "llm.", "db.")
_KEEP_KEYS = frozenset({
    "error.type",
    "jsonrpc.request.id",
    "latency_ms",
    "session.id",
    "aws.operation.name",
    "http.method",
    "http.target",
    "http.status_code",
    "http.response.status_code",
})
_KEEP_RESOURCE_KEYS = frozenset({"service.name", "scenario.pitfalls"})

_ARN = re.compile(r"arn:aws[\w-]*:[^\s\"',]+")
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _kept(key: str) -> bool:
    return key in _KEEP_KEYS or key.startswith(_KEEP_PREFIXES)


def _mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return _ACCOUNT_ID.sub("<acct>", _ARN.sub("<arn>", value))
    if isinstance(value, list):
        return [_mask_value(v) for v in value]
    return value


def mask_span(span: dict[str, Any]) -> dict[str, Any]:
    """A copy of span with only the kept attribute and resource keys."""
    return {
        **span,
        "status_description": _mask_value(span["status_description"]),
        "attributes": {k: _mask_value(v) for k, v in span["attributes"].items() if _kept(k)},
        "resource": {k: _mask_value(v) for k, v in span["resource"].items() if k in _KEEP_RESOURCE_KEYS},
    }


def from_otel_document(doc: dict[str, Any]) -> dict[str, Any]:
    """One span document from X-Ray's ListRetrievedTraces (TraceFormat OTEL)."""
    status = doc.get("status") or {}
    return {
        "trace_id": doc["traceId"],
        "span_id": doc["spanId"],
        "parent_span_id": doc.get("parentSpanId") or None,
        "name": doc["name"],
        "kind": doc["kind"],
        "start_time_unix_nano": int(doc["startTimeUnixNano"]),
        "end_time_unix_nano": int(doc["endTimeUnixNano"]),
        "status_code": status.get("code", "UNSET"),
        "status_description": status.get("message"),
        "attributes": doc.get("attributes") or {},
        "resource": (doc.get("resource") or {}).get("attributes") or {},
    }
