"""Ask the agent on AgentCore Runtime the provisional questions (scripts/run_agent.py),
as its user would: one InvokeAgentRuntime call per question.

  AGENT_RUNTIME_ARN=$(terraform -chdir=infra output -raw agent_runtime_arn) \
    python scripts/invoke_agent.py 2 6

With --concurrent, the questions are sent at the same time (pitfall #12,
queueing in the inference server), e.g. `--concurrent 1 2 3 4 5 6`.

Each call sits in a `caller` span of its own, whose traceparent goes with the
request, so the platform's spans and the agent's join that trace. The answer
is streamed (server-sent events, system/agent_server.py), as a chat shows it;
the time until its first piece arrives goes on that span as
caller.time_to_first_text_ms and in the report as "shown after". The spans go
to CloudWatch (needs the "aws" extra, AWS credentials and Transaction Search on,
infra/account/).

A runtime session is one agent process. By default the questions share one
session, warmed up first with a question that is not measured (its span has
caller.warmup=true), so the agent's start-up stays out of them. The agent keeps
the conversation for its session (system/agent.py), so each question follows
the warm-up and the questions before it. With --concurrent, each question has a session of
its own, all warmed up before the questions go. --cold gives every question a
new session without warming up, for looking at the start-up itself.

For how sessions end and break (docs/DESIGN.md §6, 7):
  --session ID      ask in this session without warming up; the same ID later
                    continues the conversation (e.g. after 15 minutes unused)
  --question TEXT   ask this text instead of a numbered question (repeatable)
  --cut-after S     close the stream S seconds after sending, as a user who
                    leaves, and stop the session (StopRuntimeSession); the
                    report says where it was cut
"""
from __future__ import annotations
import argparse, json, os, sys, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import botocore.session
from botocore.config import Config
from opentelemetry import propagate, trace

from scripts.run_agent import QUESTIONS
from tracing.aws_exporter import cloudwatch_span_exporter
from tracing.instrumentation import build_tracer_provider, get_tracer


# Not part of any convention: when the user would see the answer start.
TIME_TO_FIRST_TEXT_MS = "caller.time_to_first_text_ms"
# Not part of any convention: the caller closed the stream on purpose after this long.
CUT_AFTER_S = "caller.cut_after_s"

# Sent first on each session and left out of the measurements: it starts the
# agent's process, its MCP connection and tool list, and puts the tool
# definitions in vLLM's prefix cache, as on a server that is already running.
WARMUP_QUESTION = "こんにちは"


def main(
    numbers: list[int],
    concurrent: bool,
    cold: bool,
    *,
    texts: list[str] = (),
    session: str | None = None,
    cut_after_s: float | None = None,
) -> None:
    arn = os.environ["AGENT_RUNTIME_ARN"]
    region = arn.split(":")[3]
    provider = build_tracer_provider(cloudwatch_span_exporter(region), service_name="caller", batch=True)
    tracer = get_tracer(provider)
    # botocore's defaults (60 s read timeout, then a retry) sent a question
    # longer than 60 s a second time, and the runtime ran both (2026-11-05).
    # One attempt, and long enough for the agent's longest run.
    config = Config(read_timeout=900, retries={"total_max_attempts": 1})
    client = botocore.session.get_session().create_client("bedrock-agentcore", region_name=region, config=config)
    # Stopping a session keeps botocore's retries: the runtime answers 409
    # (RetryableConflictException) while it is still setting a session up or
    # tearing it down, and asks callers to retry.
    stopper = botocore.session.get_session().create_client("bedrock-agentcore", region_name=region)

    def new_session() -> str:
        return str(uuid.uuid4())  # at least 33 characters

    def ask(label: str, question: str, session_id: str, warmup: bool = False) -> str:
        with tracer.start_as_current_span("InvokeAgentRuntime", kind=trace.SpanKind.CLIENT) as span:
            span.set_attribute("caller.warmup", warmup)
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            start = time.perf_counter()
            trace_id = f"{span.get_span_context().trace_id:032x}"
            try:
                response = client.invoke_agent_runtime(
                    agentRuntimeArn=arn,
                    runtimeSessionId=session_id,
                    contentType="application/json",
                    accept="text/event-stream",
                    payload=json.dumps({"question": question}, ensure_ascii=False).encode(),
                    traceParent=carrier["traceparent"],
                )
                body: dict = {}
                first_text_s = None
                # A timer cuts the stream at the given time, even while nothing
                # arrives (tools running between pings). Closing the body from
                # another thread doesn't wake a read that is waiting; urllib3's
                # shutdown() (2.3+) does, by shutting the socket down.
                # The runtime doesn't pass the closed stream on to the agent,
                # which went on to the end of its answer (2026-11-09), so the
                # session is stopped too, as a chat's web server would do when
                # its user leaves.
                cut = None
                was_cut = threading.Event()
                if cut_after_s is not None:
                    asking = trace.set_span_in_context(span)

                    def close() -> None:
                        was_cut.set()
                        response["response"]._raw_stream.shutdown()
                        response["response"].close()
                        with tracer.start_as_current_span(
                            "StopRuntimeSession", context=asking, kind=trace.SpanKind.CLIENT
                        ):
                            stopper.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session_id)

                    cut = threading.Timer(max(0.0, cut_after_s - (time.perf_counter() - start)), close)
                    cut.start()
                try:
                    # One byte at a time, so a piece is seen when it arrives rather
                    # than when enough bytes for a bigger read have come in.
                    for line in response["response"].iter_lines(chunk_size=1):
                        if not line.startswith(b"data: "):
                            continue
                        event = json.loads(line[len(b"data: "):])
                        if "text" in event:
                            if first_text_s is None:
                                first_text_s = time.perf_counter() - start
                                span.set_attribute(TIME_TO_FIRST_TEXT_MS, first_text_s * 1000)
                        else:
                            body = event
                except Exception:
                    if not was_cut.is_set():
                        raise  # not our cut
                finally:
                    if cut is not None:
                        cut.cancel()
                        cut.join()  # the session is stopped before the next question
                if was_cut.is_set() and not body:
                    elapsed = time.perf_counter() - start
                    span.set_attribute(CUT_AFTER_S, cut_after_s)
                    shown = "no text" if first_text_s is None else f"shown after {first_text_s:.1f}s"
                    return "\n".join([
                        f"--- {label}: {question}",
                        f"{elapsed:.1f}s ({shown}), cut by the caller after {cut_after_s:g}s",
                        f"trace {trace_id}",
                    ])
                if "error" in body:
                    raise RuntimeError(f"the agent failed: {body['error']}")
                if "answer" not in body:
                    # Cut off on the way: the HTTP status said 200 at the start.
                    raise RuntimeError("the stream ended without the summary event")
            except Exception as e:
                # A failed question (e.g. the prompt outgrew vLLM's context)
                # is a result too: report it and go on to the next one. The
                # agent keeps the conversation as it was before the question.
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, type(e).__name__)
                elapsed = time.perf_counter() - start
                return "\n".join([
                    f"--- {label}: {question}",
                    f"{elapsed:.1f}s, failed: {type(e).__name__}: {e}",
                    f"trace {trace_id}",
                ])
            elapsed = time.perf_counter() - start
        # The agent reports the trace it ran in, which should be this one.
        shown = "no text" if first_text_s is None else f"shown after {first_text_s:.1f}s"
        return "\n".join([
            f"--- {label}: {question}",
            f"{elapsed:.1f}s ({shown}), {body.get('turns')} LLM calls, stopped: {body.get('stop_reason')}",
            json.dumps(body.get("answer"), ensure_ascii=False),
            f"trace {trace_id}" + ("" if body.get("trace_id") == trace_id else f" (agent: {body.get('trace_id')})"),
        ])

    def warm(session_id: str) -> str:
        return ask("warm-up", WARMUP_QUESTION, session_id, warmup=True)

    started = datetime.now(timezone.utc)
    jobs = [(f"Q{n}", QUESTIONS[n - 1]) for n in numbers] + [(f"T{i}", t) for i, t in enumerate(texts, 1)]
    if session is not None:
        # The caller's own session: no warm-up, so a new one starts cold and
        # a known one continues where it was.
        print(f"session {session}")
        for job in jobs:
            print(ask(*job, session))
    elif concurrent:
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
    parser.add_argument("numbers", nargs="*", type=int, help="question numbers (scripts/run_agent.py)")
    parser.add_argument("--concurrent", action="store_true", help="send all the questions at the same time")
    parser.add_argument("--cold", action="store_true", help="a new session for every question, without warming up")
    parser.add_argument("--question", action="append", default=[], help="ask this text (repeatable)")
    parser.add_argument("--session", help="ask in this session, without warming up (at least 33 characters)")
    parser.add_argument("--cut-after", type=float, help="close the stream this many seconds after sending")
    args = parser.parse_args()
    if not args.numbers and not args.question:
        parser.error("give question numbers or --question")
    if args.session is not None and (args.concurrent or args.cold):
        parser.error("--session asks in one session; it can't go with --concurrent or --cold")
    if args.session is not None and len(args.session) < 33:
        parser.error("--session must be at least 33 characters (AgentCore Runtime)")
    main(
        args.numbers, args.concurrent, args.cold,
        texts=args.question, session=args.session, cut_after_s=args.cut_after,
    )
