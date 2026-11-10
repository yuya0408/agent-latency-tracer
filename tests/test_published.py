"""Tests for tracing/published.py (the masked traces kept under traces/)."""
from __future__ import annotations

from tracing.published import from_otel_document, mask_span

_ACCOUNT = "123456789012"
_ARN = f"arn:aws:bedrock-agentcore:ap-northeast-1:{_ACCOUNT}:runtime/agent-abc"


def _otel_document(**overrides):
    doc = {
        "traceId": "6ae92b853aa0d8271b263058f7a9ec59",
        "spanId": "1111111111111111",
        "parentSpanId": "2222222222222222",
        "name": "AgentCore.Runtime.Invoke",
        "kind": "SERVER",
        "startTimeUnixNano": 1_000,
        "endTimeUnixNano": 3_000,
        "status": {"code": "ERROR", "message": "failed"},
        "attributes": {
            "aws.account.id": _ACCOUNT,
            "aws.resource.arn": _ARN,
            "net.peer.ip": "10.0.1.23",
            "http.url": "https://bedrock-agentcore.ap-northeast-1.amazonaws.com/runtimes/x",
            "aws.local.service": "agent",
            "session.id": "s-1",
            "latency_ms": 5_200,
            "gen_ai.operation.name": "chat",
        },
        "resource": {"attributes": {"service.name": "agent", "cloud.resource_id": _ARN}},
    }
    return {**doc, **overrides}


def test_otel_document_becomes_the_read_trace_shape():
    span = from_otel_document(_otel_document())
    assert span["trace_id"] == "6ae92b853aa0d8271b263058f7a9ec59"
    assert span["parent_span_id"] == "2222222222222222"
    assert (span["start_time_unix_nano"], span["end_time_unix_nano"]) == (1_000, 3_000)
    assert (span["status_code"], span["status_description"]) == ("ERROR", "failed")
    assert span["resource"]["service.name"] == "agent"


def test_root_span_has_no_parent():
    doc = _otel_document()
    del doc["parentSpanId"]
    assert from_otel_document(doc)["parent_span_id"] is None


def test_mask_keeps_only_listed_keys():
    span = mask_span(from_otel_document(_otel_document()))
    assert span["attributes"] == {"session.id": "s-1", "latency_ms": 5_200, "gen_ai.operation.name": "chat"}
    assert span["resource"] == {"service.name": "agent"}


def test_mask_replaces_account_ids_and_arns_left_in_kept_values():
    doc = _otel_document(
        attributes={"error.type": f"denied for {_ARN}", "gen_ai.agent.name": f"agent-{_ACCOUNT}"},
        status={"code": "ERROR", "message": f"account {_ACCOUNT}"},
    )
    span = mask_span(from_otel_document(doc))
    assert span["attributes"] == {"error.type": "denied for <arn>", "gen_ai.agent.name": "agent-<acct>"}
    assert span["status_description"] == "account <acct>"


def test_mask_leaves_numbers_alone():
    doc = _otel_document(attributes={"vllm.generation_time_ms": 8941.123456789012})
    assert mask_span(from_otel_document(doc))["attributes"] == {"vllm.generation_time_ms": 8941.123456789012}


def test_mask_keeps_the_retry_count_on_the_embeddings_span():
    doc = _otel_document(attributes={"botocore.retry_attempts": 0})
    assert mask_span(from_otel_document(doc))["attributes"] == {"botocore.retry_attempts": 0}


def test_mask_keeps_what_the_caller_says_about_its_call():
    attributes = {"caller.time_to_first_text_ms": 6400.5, "caller.cut_after_s": 4.0, "caller.warmup": False}
    doc = _otel_document(attributes=attributes)
    assert mask_span(from_otel_document(doc))["attributes"] == attributes
