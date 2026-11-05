"""Ask the agent on AgentCore Runtime the provisional questions (scripts/run_agent.py),
as its user would: one InvokeAgentRuntime call per question.

  AGENT_RUNTIME_ARN=$(terraform -chdir=infra output -raw agent_runtime_arn) \
    python scripts/invoke_agent.py 2 6

With --concurrent, the questions are sent at the same time (pitfall #12,
queueing in the inference server), e.g. `--concurrent 1 2 3 4 5 6`.

Each call sits in a `caller` span of its own, whose traceparent goes with the
request, so the platform's spans and the agent's join that trace. The spans go
to CloudWatch (needs the "aws" extra, AWS credentials and Transaction Search on,
infra/account/).

A runtime session is one agent process. By default the questions share one
session, warmed up first with a question that is not measured (its span has
caller.warmup=true), so the agent's start-up stays out of them; each question
is still a new conversation. With --concurrent, each question has a session of
its own, all warmed up before the questions go. --cold gives every question a
new session without warming up, for looking at the start-up itself.
"""
from __future__ import annotations
import argparse, json, os, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import botocore.session
from opentelemetry import propagate, trace

from scripts.run_agent import QUESTIONS
from tracing.aws_exporter import cloudwatch_span_exporter
from tracing.instrumentation import build_tracer_provider, get_tracer


# Sent first on each session and left out of the measurements: it starts the
# agent's process, its MCP connection and tool list, and puts the tool
# definitions in vLLM's prefix cache, as on a server that is already running.
WARMUP_QUESTION = "こんにちは"


def main(numbers: list[int], concurrent: bool, cold: bool) -> None:
    arn = os.environ["AGENT_RUNTIME_ARN"]
    region = arn.split(":")[3]
    provider = build_tracer_provider(cloudwatch_span_exporter(region), service_name="caller", batch=True)
    tracer = get_tracer(provider)
    client = botocore.session.get_session().create_client("bedrock-agentcore", region_name=region)

    def new_session() -> str:
        return str(uuid.uuid4())  # at least 33 characters

    def ask(label: str, question: str, session_id: str, warmup: bool = False) -> str:
        with tracer.start_as_current_span("InvokeAgentRuntime", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute("caller.warmup", warmup)
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            start = time.perf_counter()
            response = client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                runtimeSessionId=session_id,
                contentType="application/json",
                accept="application/json",
                payload=json.dumps({"question": question}, ensure_ascii=False).encode(),
                traceParent=carrier["traceparent"],
            )
            body = json.loads(response["response"].read())
            elapsed = time.perf_counter() - start
            trace_id = f"{span.get_span_context().trace_id:032x}"
        # The agent reports the trace it ran in, which should be this one.
        return "\n".join([
            f"--- {label}: {question}",
            f"{elapsed:.1f}s, {body.get('turns')} LLM calls, stopped: {body.get('stop_reason')}",
            json.dumps(body.get("answer"), ensure_ascii=False),
            f"trace {trace_id}" + ("" if body.get("trace_id") == trace_id else f" (agent: {body.get('trace_id')})"),
        ])

    def warm(session_id: str) -> str:
        return ask("warm-up", WARMUP_QUESTION, session_id, warmup=True)

    started = datetime.now(timezone.utc)
    jobs = [(f"Q{n}", QUESTIONS[n - 1]) for n in numbers]
    if concurrent:
        # One session and one thread per question. All sessions are warmed
        # first, so the questions reach the agents together instead of being
        # spread out by their start-ups. Reports are printed in the order given.
        sessions = [new_session() for _ in jobs]
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            if not cold:
                for report in pool.map(warm, sessions):
                    print(report)
            for report in pool.map(lambda job, sid: ask(*job, sid), jobs, sessions):
                print(report)
    elif cold:
        # A new session, so a new agent process, for every question.
        for job in jobs:
            print(ask(*job, new_session()))
    else:
        session = new_session()
        print(warm(session))
        for job in jobs:
            print(ask(*job, session))
    finished = datetime.now(timezone.utc)
    # For finding the traces again (StartTraceRetrieval, scripts/export_trace.py).
    print(f"time range (UTC): {started:%Y-%m-%dT%H:%M:%SZ} .. {finished:%Y-%m-%dT%H:%M:%SZ}")

    provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ask the agent on AgentCore Runtime.")
    parser.add_argument("numbers", nargs="+", type=int, help="question numbers (scripts/run_agent.py)")
    parser.add_argument("--concurrent", action="store_true", help="send all the questions at the same time")
    parser.add_argument("--cold", action="store_true", help="a new session for every question, without warming up")
    args = parser.parse_args()
    main(args.numbers, args.concurrent, args.cold)
