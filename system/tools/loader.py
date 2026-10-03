"""Loads the tool catalog (40 generic business-system tools) used by the agent.

catalog.json is a byte-identical snapshot of toolcall-serving-bench's
schema/tools.json (source commit c6bbd51, copied 2026-09-22). It is not a
live dependency: this project intentionally does not import from
toolcall-serving-bench at runtime. If the
source catalog changes, this snapshot must be re-copied by hand.
"""
from __future__ import annotations
import json, pathlib
from typing import Any

_CATALOG_PATH = pathlib.Path(__file__).resolve().parent / "catalog.json"


def load_catalog(path: str | pathlib.Path = _CATALOG_PATH) -> dict[str, dict[str, Any]]:
    doc = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    return {t["name"]: t for t in doc["tools"]}
