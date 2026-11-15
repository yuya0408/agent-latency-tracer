"""Tests for system/pitfalls.py: reading PITFALLS, and recording it on spans."""
from __future__ import annotations

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from system import pitfalls
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


def test_recorded_on_every_span(monkeypatch, available):
    monkeypatch.setenv(pitfalls.ENV_VAR, "3")
    exporter = InMemorySpanExporter()
    provider = build_tracer_provider(
        exporter, service_name="agent", resource_attributes=pitfalls.resource_attributes()
    )
    with invoke_agent_span(get_tracer(provider), agent_name="agent"):
        pass
    provider.shutdown()
    (stored,) = exporter.get_finished_spans()
    assert stored.resource.attributes[pitfalls.RESOURCE_ATTRIBUTE] == "3"
    assert stored.resource.attributes["service.name"] == "agent"  # not overridden by the extra attributes
