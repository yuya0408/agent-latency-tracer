"""Pitfall M1's tool list: one find/get/aggregate tool per data type.

The shape a server takes when it wraps a REST API one endpoint per resource
(OpenAPI-to-MCP generators and many hand-written servers): `find_sales_order`
with each field as its own argument, instead of one `find_records` taking the
type as an argument. With the 15 data types this is 45 tools, about 9,300
tokens of definitions against the baseline's 1,100 (Qwen3's tokenizer, as the
chat template writes them). The calls are translated to the baseline tools,
so the data and the answers stay the same.
"""
from __future__ import annotations
from typing import Any

from system.tools.loader import load_data_schema

_JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "date": "string"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100}


def _field_property(field: dict[str, Any]) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": _JSON_TYPES[field["type"]], "description": field["description"]}
    if field["type"] == "date":
        prop["format"] = "date"
    if "enum" in field:
        prop["enum"] = field["enum"]
    return prop


def per_type_tools() -> list[dict[str, Any]]:
    """In MCP's Tool shape, like catalog.json."""
    tools = []
    for name, t in load_data_schema().items():
        fields = {f["name"]: _field_property(f) for f in t["fields"]}
        numeric = [f["name"] for f in t["fields"] if f["type"] in ("number", "integer")]
        tools.append({
            "name": f"find_{name}",
            "description": f"{t['label']}({t['description']})を条件で検索する。指定した項目がすべて一致するものだけを返す。",
            "inputSchema": {
                "type": "object", "additionalProperties": False,
                "properties": {**fields, "limit": _LIMIT},
            },
        })
        tools.append({
            "name": f"get_{name}",
            "description": f"{t['label']}を1件、{t['id_field']}で返す。",
            "inputSchema": {
                "type": "object", "required": ["id"], "additionalProperties": False,
                "properties": {"id": {"type": "string", "description": t["id_field"]}},
            },
        })
        metric = {
            "op": {"type": "string", "enum": ["sum", "count", "avg"]},
            "group_by": {"type": "array", "items": {"type": "string", "enum": list(fields)}},
        }
        if numeric:
            metric["field"] = {"type": "string", "enum": numeric, "description": "集計する項目。countのときは省略できる"}
        tools.append({
            "name": f"aggregate_{name}",
            "description": f"{t['label']}を集計する。group_byの項目ごとに合計・件数・平均を返す。ほかの項目は条件(一致)。",
            "inputSchema": {
                "type": "object", "required": ["op"], "additionalProperties": False,
                "properties": {**metric, **fields},
            },
        })
    return tools


def to_baseline(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The baseline tool and arguments that a per-type call stands for."""
    op, _, type_name = name.partition("_")
    args = dict(arguments)
    if op == "get":
        return "get_record", {"type": type_name, "id": args.get("id")}
    if op == "find":
        limit = args.pop("limit", None)
        call = {"type": type_name, "filter": _equalities(args)}
        if limit is not None:
            call["limit"] = limit
        return "find_records", call
    if op == "aggregate":
        metric = {"op": args.pop("op", None)}
        if "field" in args:
            metric["field"] = args.pop("field")
        group_by = args.pop("group_by", [])
        return "aggregate_records", {"type": type_name, "metric": metric, "group_by": group_by, "filter": _equalities(args)}
    raise ValueError(f"not a per-type tool: {name}")


def _equalities(args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"field": k, "op": "eq", "value": v} for k, v in args.items()]
