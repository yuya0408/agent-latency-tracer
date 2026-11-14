"""Turn a trace into the masked JSON kept under traces/ in the repository.

The files are OTLP/JSON, the body an OTLP/HTTP endpoint takes, so Jaeger or
any other OTLP backend loads them as they are (README). In between, a span is
a flat dict: from_otel_document reads CloudWatch's documents into it,
mask_span works on it, and to_otlp / from_otlp convert it to and from the files.

CloudWatch adds attributes of its own, some naming the AWS account (account
ID, ARNs), the VPC (peer IP) and the endpoint (host, URL). Only the keys
below are kept, and any account ID or ARN left in a kept string value is
replaced as well.
"""

from __future__ import annotations

import json
import re
from typing import Any

_KEEP_PREFIXES = ("gen_ai.", "mcp.", "vllm.", "llamacpp.", "llm.", "db.", "rpc.")
_KEEP_KEYS = frozenset({
    "agent.memory.events",
    "botocore.retry_attempts",
    "caller.time_to_first_text_ms",
    "caller.cut_after_s",
    "caller.warmup",
    "agent.time_to_first_text_ms",
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
_KEEP_RESOURCE_KEYS = frozenset({"service.name", "scenario.pitfalls", "process.creation.time"})

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


# OTLP's enums, by the names the flat dict uses.
_KINDS = ["UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER"]
_STATUS_CODES = ["UNSET", "OK", "ERROR"]


def _any_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):  # before int: bool is an int
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_any_value(v) for v in value]}}
    return {"stringValue": str(value)}


def _from_any_value(value: dict[str, Any]) -> Any:
    if "intValue" in value:
        return int(value["intValue"])
    if "arrayValue" in value:
        return [_from_any_value(v) for v in value["arrayValue"].get("values", [])]
    (inner,) = value.values()
    return inner


def _key_values(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _any_value(v)} for k, v in attributes.items()]


def to_otlp(spans: list[dict[str, Any]]) -> dict[str, Any]:
    """OTLP/JSON for the spans, one resourceSpans entry per distinct resource."""
    by_resource: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for span in spans:
        key = json.dumps(span["resource"], sort_keys=True)
        by_resource.setdefault(key, (span["resource"], []))[1].append({
            "traceId": span["trace_id"],
            "spanId": span["span_id"],
            **({"parentSpanId": span["parent_span_id"]} if span["parent_span_id"] else {}),
            "name": span["name"],
            "kind": _KINDS.index(span["kind"]),
            "startTimeUnixNano": str(span["start_time_unix_nano"]),
            "endTimeUnixNano": str(span["end_time_unix_nano"]),
            "attributes": _key_values(span["attributes"]),
            "status": {
                "code": _STATUS_CODES.index(span["status_code"]),
                **({"message": span["status_description"]} if span["status_description"] else {}),
            },
        })
    return {"resourceSpans": [
        {"resource": {"attributes": _key_values(resource)}, "scopeSpans": [{"spans": otlp_spans}]}
        for resource, otlp_spans in by_resource.values()
    ]}


def from_otlp(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The flat dicts for every span in an OTLP/JSON document, by start time."""
    spans = []
    for resource_spans in doc["resourceSpans"]:
        resource = {a["key"]: _from_any_value(a["value"]) for a in resource_spans["resource"].get("attributes", [])}
        for scope_spans in resource_spans["scopeSpans"]:
            for span in scope_spans["spans"]:
                status = span.get("status") or {}
                spans.append({
                    "trace_id": span["traceId"],
                    "span_id": span["spanId"],
                    "parent_span_id": span.get("parentSpanId") or None,
                    "name": span["name"],
                    "kind": _KINDS[span["kind"]],
                    "start_time_unix_nano": int(span["startTimeUnixNano"]),
                    "end_time_unix_nano": int(span["endTimeUnixNano"]),
                    "status_code": _STATUS_CODES[status.get("code", 0)],
                    "status_description": status.get("message"),
                    "attributes": {a["key"]: _from_any_value(a["value"]) for a in span.get("attributes", [])},
                    "resource": resource,
                })
    return sorted(spans, key=lambda s: s["start_time_unix_nano"])
