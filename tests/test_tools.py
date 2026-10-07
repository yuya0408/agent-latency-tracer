"""Tests for system/tools/: the tool catalog, the data schema behind it, and
mock tool execution."""
from __future__ import annotations

import asyncio
import random

from system.tools.loader import load_catalog, load_data_schema
from system.tools import mock_server

BUSINESS_TOOLS = ["describe_data", "find_records", "get_record", "aggregate_records"]
FIELD_TYPES = {"string", "integer", "number", "date", "boolean"}


class _NoDelay:
    """Stands in for the rng so these tests skip the 200-800ms sleep."""

    def uniform(self, a, b):
        return 0.0


def _call(name, arguments):
    return asyncio.run(mock_server.call_tool(name, arguments, rng=_NoDelay()))


def _cond(field, op, value):
    return {"field": field, "op": op, "value": value}


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


def test_mock_data_matches_the_schema():
    for type_name, t in load_data_schema().items():
        names = [f["name"] for f in t["fields"]]
        rows = mock_server._ROWS[type_name]
        assert rows, type_name
        for row in rows:
            assert list(row) == names, (type_name, row)
        ids = [row[t["id_field"]] for row in rows]
        assert len(ids) == len(set(ids)), type_name


def test_order_lines_add_up_to_the_header_amount():
    headers = {**{r["order_id"]: r for r in mock_server._ROWS["sales_order"]},
               **{r["po_id"]: r for r in mock_server._ROWS["purchase_order"]}}
    for order_id, lines in mock_server._LINES.items():
        assert sum(l["quantity"] * l["unit_price"] for l in lines) == headers[order_id]["amount"], order_id


def test_find_records_filters_with_and():
    result = _call("find_records", {"type": "sales_order", "filter": [
        _cond("partner_name", "eq", "A社"), _cond("status", "eq", "未出荷")]})
    assert result["status"] == "ok"
    assert result["total"] == 2
    assert [r["order_id"] for r in result["records"]] == ["SO-1042", "SO-1057"]


def test_find_records_filters_a_date_range():
    result = _call("find_records", {"type": "purchase_invoice", "filter": [
        _cond("partner_name", "eq", "B社"),
        _cond("due_date", "gte", "2026-10-01"), _cond("due_date", "lte", "2026-10-31")]})
    assert [r["invoice_id"] for r in result["records"]] == ["PI-7001"]


def test_find_records_limit_keeps_the_total():
    result = _call("find_records", {"type": "stock_balance",
                                    "filter": [_cond("item_code", "eq", "X001")], "limit": 1})
    assert result["total"] == 2
    assert len(result["records"]) == 1


def test_find_records_without_filter_returns_all_rows():
    result = _call("find_records", {"type": "item"})
    assert result["total"] == len(result["records"]) == 4


def test_get_record_returns_header_and_lines():
    record = _call("get_record", {"type": "purchase_order", "id": "PO-5030"})["record"]
    assert record["status"] == "未入荷"
    assert [l["item_code"] for l in record["lines"]] == ["X001", "Y001"]


def test_get_record_uses_the_id_field():
    record = _call("get_record", {"type": "stock_balance", "id": "SB-0002"})["record"]
    assert (record["item_code"], record["warehouse"]) == ("X001", "大阪倉庫")


def test_get_record_not_found_says_what_to_do():
    result = _call("get_record", {"type": "purchase_order", "id": "PO-9999"})
    assert result["status"] == "error"
    assert "find_records" in result["message"]


def test_aggregate_records_sums_per_group():
    result = _call("aggregate_records", {
        "type": "sales_order",
        "metric": {"op": "sum", "field": "amount"},
        "group_by": ["partner_name"],
        "filter": [_cond("order_date", "gte", "2026-09-01"), _cond("order_date", "lte", "2026-09-30")]})
    sums = {g["partner_name"]: g["sum_amount"] for g in result["groups"]}
    assert sums == {"A社": 605000, "C社": 210000, "D社": 90000}


def test_aggregate_records_count_needs_no_field():
    result = _call("aggregate_records", {"type": "sales_order", "metric": {"op": "count"}})
    assert result["groups"] == [{"count": 6}]


def test_aggregate_records_sum_without_field_is_an_error():
    result = _call("aggregate_records", {"type": "sales_order", "metric": {"op": "sum"}})
    assert result["status"] == "error"


def test_unknown_field_lists_the_known_fields():
    result = _call("find_records", {"type": "item", "filter": [_cond("code", "eq", "X001")]})
    assert result["status"] == "error"
    assert "item_code" in result["message"]


def test_call_tool_awaits_a_delay_within_configured_bounds():
    async def run():
        loop = asyncio.get_event_loop()
        start = loop.time()
        await mock_server.call_tool("find_records", {"type": "item"}, rng=random.Random(0))
        return loop.time() - start

    elapsed = asyncio.run(run())
    assert mock_server.MIN_LATENCY_S <= elapsed < mock_server.MAX_LATENCY_S + 0.05
