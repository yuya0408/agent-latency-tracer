"""Tests for system/tools/: the tool catalog snapshot + mock tool execution.

The catalog is a byte-identical snapshot of toolcall-serving-bench's
schema/tools.json, not a live import.
"""
from __future__ import annotations

import asyncio
import random

from system.tools.loader import load_catalog
from system.tools import mock_server


def test_load_catalog_has_forty_tools():
    catalog = load_catalog()
    assert len(catalog) == 40
    assert "search_master" in catalog
    assert catalog["search_master"]["module"] == "master"


def test_load_catalog_keyed_by_own_name():
    catalog = load_catalog()
    for name, tool in catalog.items():
        assert tool["name"] == name


def test_call_tool_returns_expected_shape():
    async def run():
        return await mock_server.call_tool(
            "search_master", {"entity": "supplier"}, rng=random.Random(0)
        )

    result = asyncio.run(run())
    assert result == {
        "tool": "search_master",
        "arguments": {"entity": "supplier"},
        "status": "ok",
    }


def test_call_tool_awaits_a_delay_within_configured_bounds():
    async def run():
        loop = asyncio.get_event_loop()
        start = loop.time()
        await mock_server.call_tool("search_master", {}, rng=random.Random(0))
        return loop.time() - start

    elapsed = asyncio.run(run())
    assert mock_server.MIN_LATENCY_S <= elapsed < mock_server.MAX_LATENCY_S + 0.05
