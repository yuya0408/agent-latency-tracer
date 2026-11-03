"""Tests for system/pitfalls.py: reading PITFALLS, and recording it on spans."""
from __future__ import annotations

import pytest

from system import pitfalls
from tracing.exporter import SQLiteSpanExporter, read_trace
from tracing.instrumentation import build_tracer_provider, get_tracer, invoke_agent_span


@pytest.fixture
def available(monkeypatch):
    monkeypatch.setattr(pitfalls, "AVAILABLE", frozenset({"3", "M2"}))


def test_unset_or_empty_is_the_baseline(monkeypatch, available):
    monkeypatch.delenv(pitfalls.ENV_VAR, raising=False)
    assert pitfalls.enabled() == frozenset()
    monkeypatch.setenv(pitfalls.ENV_VAR, " , ")
    assert pitfalls.resource_attributes() == {pitfalls.RESOURCE_ATTRIBUTE: "none"}


def test_comma_separated_and_sorted(monkeypatch, available):
    monkeypatch.setenv(pitfalls.ENV_VAR, "M2, 3")
    assert pitfalls.enabled() == frozenset({"3", "M2"})
    assert pitfalls.resource_attributes() == {pitfalls.RESOURCE_ATTRIBUTE: "3,M2"}


def test_a_case_not_built_yet_fails(monkeypatch, available):
    monkeypatch.setenv(pitfalls.ENV_VAR, "3,12")
    with pytest.raises(ValueError, match="12"):
        pitfalls.enabled()


def test_recorded_on_every_span(monkeypatch, available, tmp_path):
    monkeypatch.setenv(pitfalls.ENV_VAR, "3")
    db_path = tmp_path / "traces.db"
    provider = build_tracer_provider(
        SQLiteSpanExporter(db_path), service_name="agent", resource_attributes=pitfalls.resource_attributes()
    )
    with invoke_agent_span(get_tracer(provider), agent_name="agent") as span:
        trace_id = f"{span.get_span_context().trace_id:032x}"
    provider.shutdown()
    (stored,) = read_trace(db_path, trace_id)
    assert stored["resource"][pitfalls.RESOURCE_ATTRIBUTE] == "3"
    assert stored["resource"]["service.name"] == "agent"  # not overridden by the extra attributes
