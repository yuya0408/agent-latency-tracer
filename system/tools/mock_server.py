"""Mock execution of business-system tool calls with realistic latency.

Simulates a slow legacy API's response time (200-800ms per call). This is
the body of each tool; the MCP server (system/mcp_server/) exposes it over
the protocol.

Answers come from the small synthetic dataset in mock_data.json, filtered
and aggregated in memory. The data only has to be plausible enough for the
LLM to finish a conversation: the latency being studied is the sleep above,
not the lookup. Errors say what to do next (e.g. which field names exist),
so a model that guessed wrong can recover instead of retrying blindly.
"""
from __future__ import annotations
import asyncio, json, pathlib, random
from typing import Any

from system.tools.loader import load_data_schema

MIN_LATENCY_S = 0.2
MAX_LATENCY_S = 0.8
DEFAULT_LIMIT = 100  # same as find_records' default in catalog.json

_default_rng = random.Random()
_DATA_SCHEMA = load_data_schema()
_MOCK_DATA = json.loads(
    (pathlib.Path(__file__).resolve().parent / "mock_data.json").read_text(encoding="utf-8")
)
_ROWS: dict[str, list[dict[str, Any]]] = _MOCK_DATA["rows"]
_LINES: dict[str, list[dict[str, Any]]] = _MOCK_DATA["lines"]

_COMPARE = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "contains": lambda a, b: str(b) in str(a),
}


class _ToolError(Exception):
    pass


def _check_type(type_name: Any) -> str:
    if type_name not in _DATA_SCHEMA:
        raise _ToolError(f"unknown type: {type_name}. Call describe_data without type to list the types.")
    return type_name


def _check_fields(type_name: str, names: list[str]) -> None:
    known = [f["name"] for f in _DATA_SCHEMA[type_name]["fields"]]
    for name in names:
        if name not in known:
            raise _ToolError(f"unknown field for {type_name}: {name}. Fields: {', '.join(known)}")


def _matches(row: dict[str, Any], cond: dict[str, Any]) -> bool:
    try:
        return _COMPARE[cond["op"]](row[cond["field"]], cond["value"])
    except TypeError:  # e.g. comparing a date string with a number
        return False


def _filtered(type_name: str, conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _check_fields(type_name, [c["field"] for c in conditions])
    return [r for r in _ROWS[type_name] if all(_matches(r, c) for c in conditions)]


def _describe_data(arguments: dict[str, Any]) -> dict[str, Any]:
    type_name = arguments.get("type")
    if type_name is None:
        return {
            "result": [
                {"type": name, "label": t["label"], "area": t["area"], "description": t["description"]}
                for name, t in _DATA_SCHEMA.items()
            ]
        }
    return {"result": _DATA_SCHEMA[_check_type(type_name)]}


def _find_records(arguments: dict[str, Any]) -> dict[str, Any]:
    rows = _filtered(_check_type(arguments.get("type")), arguments.get("filter", []))
    return {"total": len(rows), "records": rows[: arguments.get("limit", DEFAULT_LIMIT)]}


def _get_record(arguments: dict[str, Any]) -> dict[str, Any]:
    type_name = _check_type(arguments.get("type"))
    id_field = _DATA_SCHEMA[type_name]["id_field"]
    record_id = arguments.get("id")
    for row in _ROWS[type_name]:
        if row[id_field] == record_id:
            record = dict(row)
            if record_id in _LINES:
                record["lines"] = _LINES[record_id]
            return {"record": record}
    raise _ToolError(f"{type_name} with {id_field}={record_id} not found. Use find_records to search.")


def _aggregate_records(arguments: dict[str, Any]) -> dict[str, Any]:
    type_name = _check_type(arguments.get("type"))
    metric = arguments.get("metric", {})
    op, field = metric.get("op"), metric.get("field")
    group_by = arguments.get("group_by", [])
    if op in ("sum", "avg") and field is None:
        raise _ToolError(f"metric.field is required for {op}.")
    _check_fields(type_name, [*group_by, *([field] if field else [])])
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in _filtered(type_name, arguments.get("filter", [])):
        groups.setdefault(tuple(row[g] for g in group_by), []).append(row)
    label = "count" if op == "count" else f"{op}_{field}"
    result = []
    for key, rows in groups.items():
        if op == "count":
            value = len(rows)
        else:
            total = sum(r[field] for r in rows)
            value = total if op == "sum" else total / len(rows)
        result.append({**dict(zip(group_by, key)), label: value})
    return {"groups": result}


_HANDLERS = {
    "describe_data": _describe_data,
    "find_records": _find_records,
    "get_record": _get_record,
    "aggregate_records": _aggregate_records,
}


async def call_tool(
    name: str, arguments: dict[str, Any], *, rng: random.Random = _default_rng, contentless: bool = False
) -> dict[str, Any]:
    """contentless=True is pitfall #3's switch (system/pitfalls.py): every tool
    but describe_data answers only that the call went through, with no
    records, as this mock did before 2026-10-08. The small model then made up
    answers or repeated the same call until the turn limit (docs/DESIGN.md §7)."""
    delay = rng.uniform(MIN_LATENCY_S, MAX_LATENCY_S)
    await asyncio.sleep(delay)
    if contentless and name != "describe_data":
        return {"tool": name, "arguments": arguments, "status": "ok"}
    try:
        return {"status": "ok", **_HANDLERS[name](arguments)}
    except _ToolError as e:
        return {"status": "error", "message": str(e)}
