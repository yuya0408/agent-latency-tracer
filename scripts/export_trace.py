"""Read traces back from CloudWatch and write them as one masked OTLP/JSON
file, for traces/ in the repository (tracing/published.py says what is kept).

  python scripts/export_trace.py traces/base/gpu4-q2 <trace_id> [<trace_id> ...]

writes traces/base/gpu4-q2.json. It uses X-Ray's StartTraceRetrieval, then
ListRetrievedTraces in OTel format, so it needs the "aws" extra and AWS
credentials with xray:StartTraceRetrieval and xray:ListRetrievedTraces.
To look at the file, send it to Jaeger (README).
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracing.published import from_otel_document, mask_span, to_otlp

# How far after the trace's start to look for its spans.
_WINDOW_S = 3600


def _from_cloudwatch(trace_id: str, start: int | None) -> list[dict]:
    import botocore.session  # needs the "aws" extra
    from botocore.config import Config

    xray = botocore.session.get_session().create_client("xray", config=Config(retries={"mode": "standard"}))
    # An X-Ray trace ID's first 8 hex digits are its start time in epoch seconds.
    # Traces sent before the IDs took that format (2026-10-31) need --start.
    if start is None:
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
    parser.add_argument("out", help="output path without extension, e.g. traces/3/gpu1-q1")
    parser.add_argument("trace_ids", nargs="+", help="32 hex digits each")
    parser.add_argument("--start", type=int, help="epoch seconds before the traces began, for IDs not in X-Ray format")
    args = parser.parse_args()

    spans = []
    for trace_id in args.trace_ids:
        found = _from_cloudwatch(trace_id, args.start)
        if not found:
            sys.exit(f"no spans found for trace {trace_id}")
        spans += found
    out = Path(args.out).with_suffix(".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(to_otlp([mask_span(s) for s in spans]), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out} ({len(spans)} spans)")


if __name__ == "__main__":
    main()
