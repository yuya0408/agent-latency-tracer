"""Call three tools on the local MCP server, one at a time and then concurrently,
and write a waterfall HTML for each run.

  terminal 1:  python -m system.mcp_server
  terminal 2:  python scripts/try_mcp.py

Both processes write spans to the same SQLite file (TRACE_DB, default
"traces.db"), so the server's spans land in the same trace as the agent's.
"""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.mcp_client import McpToolClient
from tracing.exporter import SQLiteSpanExporter
from tracing.instrumentation import (
    BATCH_EXPORT_INTERVAL_MS,
    build_tracer_provider,
    get_tracer,
    invoke_agent_span,
    set_global_tracer_provider,
)
from tracing.viewer.waterfall import render_trace_html

SERVER_URL = "http://127.0.0.1:8000/mcp"
OUT_DIR = Path("out")

# Three independent lookups: none needs another's result, so they can run concurrently.
CALLS = [
    ("search_sales_record", {"query": "先月の売上"}),
    ("search_inventory_record", {"query": "在庫が少ない品目"}),
    ("get_fiscal_calendar", {}),
]


async def run(client: McpToolClient, tracer, *, concurrent: bool) -> str:
    label = "concurrent" if concurrent else "sequential"
    with invoke_agent_span(tracer, agent_name=f"try_mcp {label}") as root:
        if concurrent:
            await asyncio.gather(*(client.call_tool(name, args) for name, args in CALLS))
        else:
            for name, args in CALLS:
                await client.call_tool(name, args)
        return f"{root.get_span_context().trace_id:032x}"


async def main() -> None:
    db_path = os.environ.get("TRACE_DB", "traces.db")
    provider = build_tracer_provider(SQLiteSpanExporter(db_path), service_name="agent", batch=True)
    set_global_tracer_provider(provider)
    tracer = get_tracer(provider)

    async with McpToolClient(SERVER_URL, tracer, agent_name="try_mcp") as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools listed")
        trace_ids = {
            "sequential": await run(client, tracer, concurrent=False),
            "concurrent": await run(client, tracer, concurrent=True),
        }

    provider.force_flush()  # our queued spans
    # The server's spans are written by its own batch worker, at most one
    # interval after they end.
    await asyncio.sleep(2 * BATCH_EXPORT_INTERVAL_MS / 1000)

    OUT_DIR.mkdir(exist_ok=True)
    for label, trace_id in trace_ids.items():
        path = OUT_DIR / f"try_mcp_{label}.html"
        path.write_text(render_trace_html(db_path, trace_id), encoding="utf-8")
        print(f"{label}: {path}")
    provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
