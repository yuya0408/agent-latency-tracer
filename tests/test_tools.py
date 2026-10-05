"""Tests for system/tools/: the tool catalog, the data schema behind it, and
mock tool execution."""
from __future__ import annotations

import asyncio
import random

from system.tools.loader import load_catalog, load_data_schema
from system.tools import mock_server

BUSINESS_TOOLS = ["describe_data", "find_records", "get_record", "aggregate_records"]
FIELD_TYPES = {"string", "integer", "number", "date", "boolean"}


def _call(name, arguments):
    return asyncio.run(mock_server.call_tool(name, arguments, rng=random.Random(0)))


def test_load_catalog_has_the_business_tools_keyed_by_name():
    catalog = load_catalog()
    assert list(catalog) == BUSINESS_TOOLS
    for name, tool in catalog.items():
        assert tool["name"] == name


def test_every_tool_offers_the_data_types_as_type_choices():
    type_names = list(load_data_schema())
    assert len(type_names) == 15
    for tool in load_catalog().values():
        assert tool["inputSchema"]["properties"]["type"]["enum"] == type_names


def test_filter_is_defined_the_same_in_find_and_aggregate():
    # catalog.json writes the filter schema out twice; keep the copies in step.
    catalog = load_catalog()
    find = catalog["find_records"]["inputSchema"]["properties"]["filter"]
    aggregate = catalog["aggregate_records"]["inputSchema"]["properties"]["filter"]
    assert find == aggregate


def test_data_schema_fields_use_known_types():
    for type_name, t in load_data_schema().items():
        for field in t["fields"]:
            assert field["type"] in FIELD_TYPES, (type_name, field["name"])


def test_describe_data_without_type_lists_the_types():
    result = _call("describe_data", {})
    assert result["status"] == "ok"
    assert [r["type"] for r in result["result"]] == list(load_data_schema())
    assert result["result"][0] == {
        "type": "partner", "label": "取引先", "area": "共通", "description": "販売先・仕入先",
    }


def test_describe_data_with_type_returns_its_fields():
    result = _call("describe_data", {"type": "sales_order"})
    assert result["status"] == "ok"
    assert result["result"]["label"] == "受注"
    status = next(f for f in result["result"]["fields"] if f["name"] == "status")
    assert status["enum"] == ["未出荷", "一部出荷", "出荷済", "取消"]


def test_describe_data_with_unknown_type_says_what_to_do():
    result = _call("describe_data", {"type": "no_such_type"})
    assert result["status"] == "error"
    assert "without type" in result["message"]


def test_other_tools_echo_the_call():
    assert _call("find_records", {"type": "item"}) == {
        "tool": "find_records",
        "arguments": {"type": "item"},
        "status": "ok",
    }


def test_call_tool_awaits_a_delay_within_configured_bounds():
    async def run():
        loop = asyncio.get_event_loop()
        start = loop.time()
        await mock_server.call_tool("find_records", {}, rng=random.Random(0))
        return loop.time() - start

    elapsed = asyncio.run(run())
    assert mock_server.MIN_LATENCY_S <= elapsed < mock_server.MAX_LATENCY_S + 0.05
