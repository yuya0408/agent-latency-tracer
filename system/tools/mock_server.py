"""Mock execution of business-system tool calls with realistic latency.

Simulates a slow legacy API's response time (200-800ms per call). This is
the body of each tool; the MCP server (system/mcp_server/) exposes it over
the protocol.
"""
from __future__ import annotations
import asyncio, random
from typing import Any

MIN_LATENCY_S = 0.2
MAX_LATENCY_S = 0.8

_default_rng = random.Random()


async def call_tool(
    name: str, arguments: dict[str, Any], *, rng: random.Random = _default_rng
) -> dict[str, Any]:
    delay = rng.uniform(MIN_LATENCY_S, MAX_LATENCY_S)
    await asyncio.sleep(delay)
    return {"tool": name, "arguments": arguments, "status": "ok"}
