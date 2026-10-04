"""Run the MCP server locally: `python -m system.mcp_server` -> http://127.0.0.1:8000/mcp

Environment variables:
  MCP_SERVER_OTEL  "0" turns server-side spans off (default "1")
  TRACE_DB         SQLite file the server's spans go to (default "traces.db")
  CORPUS_N         retrieval corpus size (default system.mcp_server.server.DEFAULT_CORPUS_N)
"""
from __future__ import annotations
import os

import uvicorn
from opentelemetry import trace
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware as HttpSpanMiddleware

from system.mcp_server.server import DEFAULT_CORPUS_N, build_server
from tracing.exporter import SQLiteSpanExporter
from tracing.instrumentation import build_tracer_provider

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    otel = os.environ.get("MCP_SERVER_OTEL", "1") != "0"
    provider = None
    if otel:
        # The SDK's middleware uses the global tracer provider, so register ours there.
        exporter = SQLiteSpanExporter(os.environ.get("TRACE_DB", "traces.db"))
        provider = build_tracer_provider(exporter, service_name="mcp-server", batch=True)
        trace.set_tracer_provider(provider)
    server = build_server(otel=otel, corpus_n=int(os.environ.get("CORPUS_N", DEFAULT_CORPUS_N)))
    app = server.streamable_http_app(host=HOST)
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
