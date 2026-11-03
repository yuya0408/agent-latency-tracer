"""Ask the agent the provisional questions (docs/DESIGN.md §6-3) on the local
LLM and MCP server, and write a waterfall HTML for each run.

  terminal 1:  python -m system.mcp_server
  terminal 2:  python scripts/run_agent.py          # all questions
               python scripts/run_agent.py 2 3      # only questions 2 and 3
               python scripts/run_agent.py --cloudwatch 1   # also send the agent's spans to CloudWatch

llama.cpp must be serving on 127.0.0.1:8080 (docs/DESIGN.md §8). Both
processes write spans to the same SQLite file (TRACE_DB, default
"traces.db"), so the server's spans land in the same trace as the agent's.
--cloudwatch needs the "aws" extra and AWS credentials, and Transaction
Search on in the account (infra/account/). Only the agent's spans go there.
PITFALLS switches cases on in the agent (system/pitfalls.py); the server
reads its own.
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system import pitfalls
from system.agent import Agent
from system.llm.client import LlmClient
from system.mcp_client import McpToolClient
from tracing.exporter import SQLiteSpanExporter
from tracing.instrumentation import (
    BATCH_EXPORT_INTERVAL_MS,
    build_tracer_provider,
    get_tracer,
    set_global_tracer_provider,
)
from tracing.viewer.waterfall import render_trace_html

# MCP_SERVER_URL: e.g. the AgentCore URL from `terraform output mcp_server_url`
SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
OUT_DIR = Path("out")

# For checking that the loop works end to end, not a fixed evaluation set;
# each case's own questions are decided in §6-6.
QUESTIONS = [
    "A社の未出荷の受注は?",
    "A社の未出荷の受注と、品目X001の在庫は?",
    "2026年9月の、取引先ごとの受注金額の合計は?",
    "B社からの仕入請求で、支払期日が10月中のものは?",
    "品目X001の入荷待ちの発注は?",
    "出張旅費の精算のルールは?",
]


def _tool_calls(messages: list[dict]) -> list[str]:
    """`name(arguments)` for every tool call in the conversation, in order."""
    return [
        f"{c['function']['name']}({c['function']['arguments']})"
        for m in messages
        for c in m.get("tool_calls", [])
    ]


async def main(numbers: list[int], cloudwatch: bool) -> None:
    db_path = os.environ.get("TRACE_DB", "traces.db")
    exporters = [SQLiteSpanExporter(db_path)]
    if cloudwatch:
        # Imported here so the script runs without the "aws" extra.
        from tracing.aws_exporter import cloudwatch_span_exporter
        exporters.append(cloudwatch_span_exporter())
    provider = build_tracer_provider(
        *exporters, service_name="agent", batch=True, resource_attributes=pitfalls.resource_attributes()
    )
    set_global_tracer_provider(provider)
    tracer = get_tracer(provider)

    trace_ids: dict[int, str] = {}
    async with LlmClient(tracer) as llm, McpToolClient(SERVER_URL, tracer, agent_name="agent") as mcp:
        agent = Agent(llm, mcp, tracer)
        for n in numbers:
            question = QUESTIONS[n - 1]
            start = time.perf_counter()
            result = await agent.run(question)
            elapsed = time.perf_counter() - start
            trace_ids[n] = result.trace_id
            print(f"--- Q{n}: {question}")
            print(f"{elapsed:.1f}s, {result.turns} LLM calls, stopped: {result.stop_reason}")
            for call in _tool_calls(result.messages):
                print(f"  {call}")
            print(json.dumps(result.answer, ensure_ascii=False))

    provider.force_flush()  # our queued spans
    # The server's spans are written by its own batch worker, at most one
    # interval after they end.
    await asyncio.sleep(2 * BATCH_EXPORT_INTERVAL_MS / 1000)

    OUT_DIR.mkdir(exist_ok=True)
    for n, trace_id in trace_ids.items():
        path = OUT_DIR / f"run_agent_q{n}.html"
        path.write_text(render_trace_html(db_path, trace_id), encoding="utf-8")
        print(f"Q{n}: {path}  (trace {trace_id})")
    provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the provisional questions and write a waterfall for each.")
    parser.add_argument("numbers", nargs="*", type=int, help="question numbers (default: all)")
    parser.add_argument("--cloudwatch", action="store_true", help="also send the agent's spans to CloudWatch")
    args = parser.parse_args()
    asyncio.run(main(args.numbers or list(range(1, len(QUESTIONS) + 1)), args.cloudwatch))
