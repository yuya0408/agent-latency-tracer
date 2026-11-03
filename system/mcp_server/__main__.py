"""Run the MCP server locally: `python -m system.mcp_server` -> http://127.0.0.1:8000/mcp

Environment variables:
  MCP_SERVER_OTEL  "0" turns server-side spans off (default "1")
  TRACE_TO         where the spans go: "sqlite" (default), "cloudwatch", or both
                   ("sqlite,cloudwatch"). cloudwatch needs the "aws" extra and AWS credentials
  TRACE_DB         SQLite file the server's spans go to (default "traces.db")
  CORPUS_N         retrieval corpus size (default system.mcp_server.server.DEFAULT_CORPUS_N)
  MCP_SERVER_HOST  address to listen on (default "127.0.0.1"; the container sets "0.0.0.0")
  RETRIEVAL_BACKEND  "faiss" (default) or "pgvector"; pgvector reads PGHOST, PGUSER, ... like libpq
  RDS_IAM_AUTH     "1": log in to RDS with an IAM token instead of PGPASSWORD (needs the "aws" extra)
  PITFALLS         cases switched on in the server (system/pitfalls.py)
"""
from __future__ import annotations
import os

import uvicorn
from opentelemetry import trace
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware as HttpSpanMiddleware

from system import pitfalls
from system.mcp_server.server import DEFAULT_CORPUS_N, build_server
from tracing.exporter import exporters_from_env
from tracing.instrumentation import build_tracer_provider

HOST = os.environ.get("MCP_SERVER_HOST", "127.0.0.1")
PORT = 8000  # AgentCore Runtime expects MCP servers on 8000, path /mcp


def main() -> None:
    otel = os.environ.get("MCP_SERVER_OTEL", "1") != "0"
    provider = None
    if otel:
        # The SDK's middleware uses the global tracer provider, so register ours there.
        provider = build_tracer_provider(
            *exporters_from_env(),
            service_name="mcp-server",
            batch=True,
            resource_attributes=pitfalls.resource_attributes(),
        )
        trace.set_tracer_provider(provider)
    server = build_server(
        otel=otel,
        corpus_n=int(os.environ.get("CORPUS_N", DEFAULT_CORPUS_N)),
        retrieval=os.environ.get("RETRIEVAL_BACKEND", "faiss"),
        pitfalls=pitfalls.enabled(),
    )
    # Stateless, as AgentCore Runtime recommends: no per-session state kept in
    # the server, so any request can go to any instance.
    app = server.streamable_http_app(host=HOST, stateless_http=True)
    if otel:
        # One span per HTTP request, from receiving it to finishing the response.
        # The per-message receive/send child spans are left out: each span costs
        # a synchronous SQLite write (docs/DESIGN.md §7).
        app = HttpSpanMiddleware(app, exclude_spans=["receive", "send"])
    uvicorn.run(app, host=HOST, port=PORT)
    if provider is not None:
        provider.shutdown()  # write out spans still queued in the batch processor


if __name__ == "__main__":
    main()
