"""Loads the business tool definitions and the data schema behind them.

catalog.json holds the tool definitions in MCP's Tool shape. Every tool takes
a `type` argument (which kind of business data), and its allowed values are
filled in here from data_schema.json, so the list of data types is written in
one place only. Why the catalog has this shape: docs/DESIGN.md §6 2b.
"""
from __future__ import annotations
import json, pathlib
from typing import Any

_HERE = pathlib.Path(__file__).resolve().parent
_CATALOG_PATH = _HERE / "catalog.json"
_DATA_SCHEMA_PATH = _HERE / "data_schema.json"


def load_data_schema(path: str | pathlib.Path = _DATA_SCHEMA_PATH) -> dict[str, dict[str, Any]]:
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return doc["types"]


def load_catalog(
    path: str | pathlib.Path = _CATALOG_PATH,
    data_schema_path: str | pathlib.Path = _DATA_SCHEMA_PATH,
) -> dict[str, dict[str, Any]]:
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    type_names = list(load_data_schema(data_schema_path))
    catalog = {}
    for tool in doc["tools"]:
        tool["inputSchema"]["properties"]["type"]["enum"] = type_names
        catalog[tool["name"]] = tool
    return catalog
