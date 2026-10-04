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

from system.mcp_server.server import DEFAULT_CORPUS_N, build_server
from tracing.exporter import SQLiteSpanExporter
from tracing.instrumentation import build_tracer_provider

HOST = "127.0.0.1"
PORT = 8000


def main() -> None:
    otel = os.environ.get("MCP_SERVER_OTEL", "1") != "0"
    if otel:
        # The SDK's middleware uses the global tracer provider, so register ours there.
        exporter = SQLiteSpanExporter(os.environ.get("TRACE_DB", "traces.db"))
        trace.set_tracer_provider(build_tracer_provider(exporter, service_name="mcp-server"))
    server = build_server(otel=otel, corpus_n=int(os.environ.get("CORPUS_N", DEFAULT_CORPUS_N)))
    uvicorn.run(server.streamable_http_app(host=HOST), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
