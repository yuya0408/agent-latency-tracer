"""Write one trace as masked JSON plus its waterfall HTML, for traces/ in the
repository (tracing/published.py says what is kept).

  python scripts/export_trace.py --db out/pitfall3.db <trace_id> traces/3/2b-q1
  python scripts/export_trace.py --cloudwatch <trace_id> traces/M2/5b-q2

writes traces/3/2b-q1.json and traces/3/2b-q1.html. --cloudwatch reads the
trace back from X-Ray (StartTraceRetrieval, then ListRetrievedTraces in OTel
format); it needs the "aws" extra and AWS credentials with
xray:StartTraceRetrieval and xray:ListRetrievedTraces.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracing.exporter import read_trace
from tracing.published import from_otel_document, mask_span
from tracing.viewer.waterfall import render_spans_html

# How far after the trace's start to look for its spans.
_WINDOW_S = 3600


def _from_cloudwatch(trace_id: str) -> list[dict]:
    import botocore.session  # needs the "aws" extra

    xray = botocore.session.get_session().create_client("xray")
    # An X-Ray trace ID's first 8 hex digits are its start time in epoch seconds.
    start = int(trace_id[:8], 16)
    token = xray.start_trace_retrieval(
        TraceIds=[f"1-{trace_id[:8]}-{trace_id[8:]}"],
        StartTime=start - 60,
        EndTime=start + _WINDOW_S,
    )["RetrievalToken"]

    spans: list[dict] = []
    kwargs = {"RetrievalToken": token, "TraceFormat": "OTEL"}
    while True:
        page = xray.list_retrieved_traces(**kwargs)
        status = page["RetrievalStatus"]
        if status in ("FAILED", "CANCELLED", "TIMEOUT"):
            raise RuntimeError(f"trace retrieval {status}")
        if status != "COMPLETE":
            time.sleep(3)
            continue
        spans += [from_otel_document(json.loads(s["Document"])) for t in page["Traces"] for s in t["Spans"]]
        if "NextToken" not in page:
            return spans
        kwargs["NextToken"] = page["NextToken"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", help="SQLite file written by the local run")
    source.add_argument("--cloudwatch", action="store_true", help="read the trace from CloudWatch")
    parser.add_argument("trace_id")
    parser.add_argument("out", help="output path without extension, e.g. traces/3/2b-q1")
    args = parser.parse_args()

    spans = _from_cloudwatch(args.trace_id) if args.cloudwatch else read_trace(args.db, args.trace_id)
    if not spans:
        sys.exit(f"no spans found for trace {args.trace_id}")
    spans = sorted((mask_span(s) for s in spans), key=lambda s: s["start_time_unix_nano"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(
        json.dumps({"trace_id": args.trace_id, "spans": spans}, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".html").write_text(render_spans_html(spans), encoding="utf-8")
    print(f"{out}.json, {out}.html ({len(spans)} spans)")


if __name__ == "__main__":
    main()
