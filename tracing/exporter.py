"""Where the spans go, chosen with TRACE_TO.

Both destinations take OTLP, so nothing here formats spans itself:
  otlp        the stock OTLP/HTTP exporter. Locally this is Jaeger (README),
              at OTEL_EXPORTER_OTLP_ENDPOINT (default http://localhost:4318).
  cloudwatch  the same exporter, signing each request with SigV4
              (tracing/aws_exporter.py; needs the "aws" extra and AWS credentials).
"""

from __future__ import annotations

import os

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import SpanExporter


def exporters_from_env() -> list[SpanExporter]:
    """The destinations in TRACE_TO: "otlp" (default), "cloudwatch", or both, comma-separated."""
    exporters: list[SpanExporter] = []
    for name in os.environ.get("TRACE_TO", "otlp").split(","):
        if name == "otlp":
            exporters.append(OTLPSpanExporter())
        elif name == "cloudwatch":
            from tracing.aws_exporter import cloudwatch_span_exporter  # needs the "aws" extra

            exporters.append(cloudwatch_span_exporter())
        else:
            raise ValueError(f"unknown TRACE_TO destination: {name}")
    return exporters
