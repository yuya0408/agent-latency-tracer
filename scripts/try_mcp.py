"""Call three tools on the local MCP server, one at a time and then concurrently,
and print where to open each run's trace in Jaeger.

  terminal 1:  python -m system.mcp_server
  terminal 2:  python scripts/try_mcp.py

Both processes send spans to Jaeger on localhost (README), so the server's
spans land in the same trace as the agent's.
"""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.mcp_client import McpToolClient
from tracing.exporter import exporters_from_env
from tracing.instrumentation import BATCH_EXPORT_INTERVAL_MS, build_tracer_provider, get_tracer, invoke_agent_span, set_global_tracer_provider

# MCP_SERVER_URL: e.g. the AgentCore URL from `terraform output mcp_server_url`
SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
# JAEGER_UI: where the printed links point
JAEGER_UI = os.environ.get("JAEGER_UI", "http://localhost:16686")

# Three independent lookups: none needs another's result, so they can run concurrently.
CALLS = [
    ("find_records", {"type": "sales_order", "filter": [{"field": "status", "op": "eq", "value": "未出荷"}]}),
    ("find_records", {"type": "stock_balance", "filter": [{"field": "quantity", "op": "lt", "value": 10}]}),
    ("retrieve_documents", {"query": "返品の手順"}),
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
    provider = build_tracer_provider(*exporters_from_env(), service_name="agent", batch=True)
    set_global_tracer_provider(provider)
    tracer = get_tracer(provider)

    async with McpToolClient(SERVER_URL, tracer, agent_name="try_mcp") as client:
        tools = await client.list_tools()
        print(f"{len(tools)} tools listed")
        trace_ids = {
            "sequential": await run(client, tracer, concurrent=False),
            "concurrent": await run(client, tracer, concurrent=True),
        }

    provider.shutdown()  # send spans still queued
    # The server's spans are sent by its own batch worker, at most one
    # interval after they end.
    await asyncio.sleep(2 * BATCH_EXPORT_INTERVAL_MS / 1000)
    for label, trace_id in trace_ids.items():
        print(f"{label}: {JAEGER_UI}/trace/{trace_id}")


if __name__ == "__main__":
    asyncio.run(main())
