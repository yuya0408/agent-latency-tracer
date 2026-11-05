"""Ask the agent on AgentCore Runtime the provisional questions (scripts/run_agent.py),
as its user would: one InvokeAgentRuntime call per question.

  AGENT_RUNTIME_ARN=$(terraform -chdir=infra output -raw agent_runtime_arn) \
    python scripts/invoke_agent.py 2 6

With --concurrent, the questions are sent at the same time (pitfall #12,
queueing in the inference server), e.g. `--concurrent 2 3 2 3 2 3`.

Each call sits in a `caller` span of its own, whose traceparent goes with the
request, so the platform's spans and the agent's join that trace. The spans go
to CloudWatch (needs the "aws" extra, AWS credentials and Transaction Search on,
infra/account/). Each question gets a new runtime session, so a new agent
process: the first answer includes its start-up.
"""
from __future__ import annotations
import argparse, json, os, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import botocore.session
from opentelemetry import propagate, trace

from scripts.run_agent import QUESTIONS
from tracing.aws_exporter import cloudwatch_span_exporter
from tracing.instrumentation import build_tracer_provider, get_tracer


def main(numbers: list[int], concurrent: bool) -> None:
    arn = os.environ["AGENT_RUNTIME_ARN"]
    region = arn.split(":")[3]
    provider = build_tracer_provider(cloudwatch_span_exporter(region), service_name="caller", batch=True)
    tracer = get_tracer(provider)
    client = botocore.session.get_session().create_client("bedrock-agentcore", region_name=region)

    def ask(n: int) -> str:
        question = QUESTIONS[n - 1]
        with tracer.start_as_current_span("InvokeAgentRuntime", kind=trace.SpanKind.CLIENT) as span:
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            start = time.perf_counter()
            response = client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                runtimeSessionId=str(uuid.uuid4()),  # at least 33 characters
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
            f"--- Q{n}: {question}",
            f"{elapsed:.1f}s, {body.get('turns')} LLM calls, stopped: {body.get('stop_reason')}",
            json.dumps(body.get("answer"), ensure_ascii=False),
            f"trace {trace_id}" + ("" if body.get("trace_id") == trace_id else f" (agent: {body.get('trace_id')})"),
        ])

    if concurrent:
        # One thread per question, all sent together; each still gets its own
        # trace. Reports are printed in the order given.
        with ThreadPoolExecutor(max_workers=len(numbers)) as pool:
            for report in pool.map(ask, numbers):
                print(report)
    else:
        for n in numbers:
            print(ask(n))

    provider.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ask the agent on AgentCore Runtime.")
    parser.add_argument("numbers", nargs="+", type=int, help="question numbers (scripts/run_agent.py)")
    parser.add_argument("--concurrent", action="store_true", help="send all the questions at the same time")
    args = parser.parse_args()
    main(args.numbers, args.concurrent)
