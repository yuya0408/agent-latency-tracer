"""Mock execution of business-system tool calls with realistic latency.

Simulates a slow legacy API's response time (200-800ms per call). This is
the body of each tool; the MCP server (system/mcp_server/) exposes it over
the protocol.

Results carry no business data: each call echoes the tool name and arguments.
The one exception is describe_data, which returns data_schema.json so the LLM
can learn the field names it needs to build find_records/aggregate_records
arguments.
"""
from __future__ import annotations
import asyncio, random
from typing import Any

from system.tools.loader import load_data_schema

MIN_LATENCY_S = 0.2
MAX_LATENCY_S = 0.8

_default_rng = random.Random()
_DATA_SCHEMA = load_data_schema()


def _describe_data(arguments: dict[str, Any]) -> dict[str, Any]:
    type_name = arguments.get("type")
    if type_name is None:
        return {
            "status": "ok",
            "result": [
                {"type": name, "label": t["label"], "area": t["area"], "description": t["description"]}
                for name, t in _DATA_SCHEMA.items()
            ],
        }
    if type_name not in _DATA_SCHEMA:
        return {"status": "error", "message": f"unknown type: {type_name}. Call describe_data without type to list the types."}
    return {"status": "ok", "result": _DATA_SCHEMA[type_name]}


async def call_tool(
    name: str, arguments: dict[str, Any], *, rng: random.Random = _default_rng
) -> dict[str, Any]:
    delay = rng.uniform(MIN_LATENCY_S, MAX_LATENCY_S)
    await asyncio.sleep(delay)
    if name == "describe_data":
        return {"tool": name, "arguments": arguments, **_describe_data(arguments)}
    return {"tool": name, "arguments": arguments, "status": "ok"}
