"""OTel span taxonomy for the retrieval -> tool-call -> generation pipeline.

Attribute names and span shapes follow the OpenTelemetry GenAI semantic
conventions as published at
https://github.com/open-telemetry/semantic-conventions-genai
(docs/gen-ai/gen-ai-spans.md, docs/gen-ai/gen-ai-agent-spans.md; fetched
2026-09-13, cross-linked against semantic-conventions v1.44.0 for the stable
parts such as error.type/server.*). These conventions are still under
`development` stability for the gen_ai.* namespace itself, so attribute names
may shift upstream -- re-check the spec before relying on this module for
anything beyond this project.

Design choice ("規約は借りる、パイプラインは自分で書く"): this module only
builds spans and sets attributes via the OTel SDK/API. Collection, storage,
and visualization are custom (see exporter.py, viewer/).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Sequence

from opentelemetry import trace
from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# --- gen_ai.* attribute keys (see module docstring for source) -------------

OP_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
AGENT_NAME = "gen_ai.agent.name"
CONVERSATION_ID = "gen_ai.conversation.id"

REQUEST_MODEL = "gen_ai.request.model"
REQUEST_TEMPERATURE = "gen_ai.request.temperature"
RESPONSE_MODEL = "gen_ai.response.model"
RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

DATA_SOURCE_ID = "gen_ai.data_source.id"
RETRIEVAL_TOP_K = "gen_ai.retrieval.top_k"
RETRIEVAL_QUERY_TEXT = "gen_ai.retrieval.query.text"  # opt-in, may be sensitive
RETRIEVAL_DOCUMENTS = "gen_ai.retrieval.documents"  # opt-in, may be sensitive

TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_TYPE = "gen_ai.tool.type"
TOOL_DESCRIPTION = "gen_ai.tool.description"
TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"  # opt-in, may be sensitive
TOOL_CALL_RESULT = "gen_ai.tool.call.result"  # opt-in, may be sensitive

ERROR_TYPE = "error.type"

# --- mcp.* attribute keys (OTel MCP semantic conventions) -------------------

MCP_METHOD_NAME = "mcp.method.name"
MCP_PROTOCOL_VERSION = "mcp.protocol.version"
MCP_SESSION_ID = "mcp.session.id"

# --- db.* / server.* attribute keys (OTel database semantic conventions) ----

DB_SYSTEM_NAME = "db.system.name"
DB_OPERATION_NAME = "db.operation.name"
DB_COLLECTION_NAME = "db.collection.name"
DB_QUERY_TEXT = "db.query.text"  # parameterized, so no values in it
SERVER_ADDRESS = "server.address"
SERVER_PORT = "server.port"

_INSTRUMENTATION_SCOPE = "agent-latency-tracer"


# How often the batch worker writes queued spans. Shorter than the SDK default
# (5s) so a local run's spans are on disk soon after it finishes.
BATCH_EXPORT_INTERVAL_MS = 500


def build_tracer_provider(
    *exporters: SpanExporter,
    service_name: str = "agent-latency-tracer",
    batch: bool = False,
    resource_attributes: Mapping[str, Any] | None = None,
) -> TracerProvider:
    """Wire one or more exporters to a new provider, each behind its own processor.

    resource_attributes go on every span alongside service.name (e.g. which
    pitfalls the process runs with, system/pitfalls.py).

    One processor per exporter, so a slow destination (CloudWatch: signing and
    an HTTPS round trip per batch) doesn't hold up the others; with batch=True
    each has its own worker thread and queue.

    batch=False (SimpleSpanProcessor): each span is exported synchronously the
    moment it ends, on the calling thread. Deterministic, so tests use it --
    but the write blocks the event loop, and a SQLite commit after an idle
    gap took ~67ms on this PC, which delayed every tool response by that much
    (docs/DESIGN.md §7). With CloudWatch it is worse: every span waits for its
    own signed HTTPS request.

    batch=True (BatchSpanProcessor): ending a span only queues it; a worker
    thread exports queued spans every BATCH_EXPORT_INTERVAL_MS. Use this
    in anything whose latency is being observed. Spans still queued when the
    process exits are lost unless provider.shutdown() (or force_flush()) runs.
    """
    if not exporters:
        raise ValueError("at least one exporter is required")
    # X-Ray-format trace IDs (the first 8 hex digits are the start time). The
    # CloudWatch console reads the time from the ID to open a trace that isn't
    # in the index; with the SDK's random IDs it says the trace has expired
    # (docs/DESIGN.md §5). Still valid W3C IDs, so SQLite and the viewer don't care.
    provider = TracerProvider(
        resource=Resource.create({**(resource_attributes or {}), "service.name": service_name}),
        id_generator=AwsXRayIdGenerator(),
    )
    for exporter in exporters:
        if batch:
            provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=BATCH_EXPORT_INTERVAL_MS))
        else:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def set_global_tracer_provider(provider: TracerProvider) -> None:
    """Also register `provider` as the process-wide default.

    The MCP SDK creates its own spans (client "MCP send ..." and server-side
    middleware) through the global provider, not one passed in. Without this
    they are silently dropped, and no traceparent reaches the server either.
    OTel allows this only once per process; later calls are ignored with a
    warning.
    """
    trace.set_tracer_provider(provider)


def get_tracer(provider: TracerProvider) -> trace.Tracer:
    return provider.get_tracer(_INSTRUMENTATION_SCOPE)


def mark_error(span: Span, error_type: str, description: str | None = None) -> None:
    span.set_attribute(ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR, description))


@contextmanager
def invoke_agent_span(
    tracer: trace.Tracer,
    *,
    agent_name: str,
    conversation_id: str | None = None,
) -> Iterator[Span]:
    """Root span for one end-to-end pipeline run (retrieval -> tool calls -> generation).

    Kind INTERNAL: the agent loop runs in-process, it isn't a call to a
    remote agent service (that would be the CLIENT-kind invoke_agent variant).
    """
    attributes: dict[str, Any] = {
        OP_NAME: "invoke_agent",
        AGENT_NAME: agent_name,
    }
    if conversation_id is not None:
        attributes[CONVERSATION_ID] = conversation_id
    with tracer.start_as_current_span(
        f"invoke_agent {agent_name}", kind=SpanKind.INTERNAL, attributes=attributes
    ) as span:
        yield span


@contextmanager
def retrieval_span(
    tracer: trace.Tracer,
    *,
    data_source_id: str,
    top_k: int | None = None,
    query_text: str | None = None,
    capture_content: bool = False,
) -> Iterator[Span]:
    """Span for a search/retrieval step against a vector index or corpus."""
    attributes: dict[str, Any] = {
        OP_NAME: "retrieval",
        DATA_SOURCE_ID: data_source_id,
    }
    if top_k is not None:
        attributes[RETRIEVAL_TOP_K] = top_k
    if capture_content and query_text is not None:
        attributes[RETRIEVAL_QUERY_TEXT] = query_text
    with tracer.start_as_current_span(
        f"retrieval {data_source_id}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


def set_retrieval_results(
    span: Span, documents: Sequence[Mapping[str, Any]], *, capture_content: bool = False
) -> None:
    """Attach retrieved document ids/scores. Opt-in: content may be sensitive."""
    if not capture_content:
        return
    span.set_attribute(RETRIEVAL_DOCUMENTS, json.dumps(list(documents)))


@contextmanager
def db_query_span(
    tracer: trace.Tracer,
    *,
    system: str,
    operation: str,
    collection: str,
    query_text: str | None = None,
    server_address: str | None = None,
    server_port: int | None = None,
) -> Iterator[Span]:
    """Span for one database query, named `{operation} {collection}` as the
    database conventions suggest (e.g. "SELECT documents")."""
    attributes: dict[str, Any] = {
        DB_SYSTEM_NAME: system,
        DB_OPERATION_NAME: operation,
        DB_COLLECTION_NAME: collection,
    }
    if query_text is not None:
        attributes[DB_QUERY_TEXT] = query_text
    if server_address is not None:
        attributes[SERVER_ADDRESS] = server_address
    if server_port is not None:
        attributes[SERVER_PORT] = server_port
    with tracer.start_as_current_span(
        f"{operation} {collection}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


@contextmanager
def execute_tool_span(
    tracer: trace.Tracer,
    *,
    tool_name: str,
    tool_call_id: str | None = None,
    tool_type: str = "function",
    tool_description: str | None = None,
    agent_name: str | None = None,
) -> Iterator[Span]:
    """Span for one tool invocation. Open one per call -- callers that fan out
    multiple tool calls concurrently should open each span from within its own
    coroutine/task so the SDK's context propagation gives each the same parent
    (the invoke_agent span) and non-overlapping sibling relationships fall out
    naturally from real wall-clock timing.
    """
    attributes: dict[str, Any] = {
        OP_NAME: "execute_tool",
        TOOL_NAME: tool_name,
        TOOL_TYPE: tool_type,
    }
    if tool_call_id is not None:
        attributes[TOOL_CALL_ID] = tool_call_id
    if tool_description is not None:
        attributes[TOOL_DESCRIPTION] = tool_description
    if agent_name is not None:
        attributes[AGENT_NAME] = agent_name
    with tracer.start_as_current_span(
        f"execute_tool {tool_name}", kind=SpanKind.INTERNAL, attributes=attributes
    ) as span:
        yield span


def set_tool_call_io(
    span: Span,
    *,
    arguments: Mapping[str, Any] | None = None,
    result: Any | None = None,
    capture_content: bool = False,
) -> None:
    """Attach tool call arguments/result. Opt-in: content may be sensitive."""
    if not capture_content:
        return
    if arguments is not None:
        span.set_attribute(TOOL_CALL_ARGUMENTS, json.dumps(arguments))
    if result is not None:
        span.set_attribute(TOOL_CALL_RESULT, json.dumps(result))


@contextmanager
def chat_span(
    tracer: trace.Tracer,
    *,
    request_model: str,
    provider_name: str,
    temperature: float | None = None,
) -> Iterator[Span]:
    """Span for one generation call. Usage/response attributes are only known
    after the call returns -- set them with set_chat_usage before the `with`
    block exits.
    """
    attributes: dict[str, Any] = {
        OP_NAME: "chat",
        REQUEST_MODEL: request_model,
        PROVIDER_NAME: provider_name,
    }
    if temperature is not None:
        attributes[REQUEST_TEMPERATURE] = temperature
    with tracer.start_as_current_span(
        f"chat {request_model}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        yield span


def set_chat_usage(
    span: Span,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    response_model: str | None = None,
    finish_reasons: Sequence[str] | None = None,
) -> None:
    if input_tokens is not None:
        span.set_attribute(USAGE_INPUT_TOKENS, input_tokens)
    if output_tokens is not None:
        span.set_attribute(USAGE_OUTPUT_TOKENS, output_tokens)
    if response_model is not None:
        span.set_attribute(RESPONSE_MODEL, response_model)
    if finish_reasons is not None:
        span.set_attribute(RESPONSE_FINISH_REASONS, list(finish_reasons))
