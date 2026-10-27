"""Send spans to CloudWatch (Transaction Search) over OTLP/HTTP.

The X-Ray OTLP endpoint (https://xray.<region>.amazonaws.com/v1/traces) takes
only SigV4-signed requests over HTTP, not gRPC. This is the same approach as
ADOT's OTLPAwsSpanExporter: the stock OTLP/HTTP exporter, handed a requests
session that signs each request with the local AWS credentials. ADOT itself
is not used: it pins the whole OTel SDK to one version and expects to build
the tracer provider itself, while this project builds its own
(instrumentation.build_tracer_provider).

Transaction Search must be on in the account (infra/account/), or the
endpoint rejects the spans.
"""

from __future__ import annotations

import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

_SERVICE = "xray"  # the signing name of the traces endpoint


class _SigV4Session(requests.Session):
    """Signs every request (the exporter only ever POSTs one OTLP batch)."""

    def __init__(self, region: str, credentials) -> None:
        super().__init__()
        self._region = region
        # botocore refreshes temporary credentials on access, so holding the
        # object (not its values) is enough.
        self._credentials = credentials

    def request(self, method, url, *args, data=None, headers=None, **kwargs):
        signed = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/x-protobuf"})
        SigV4Auth(self._credentials, _SERVICE, self._region).add_auth(signed)
        # A new dict: the exporter may pass the same headers dict on every call.
        headers = {**(headers or {}), **dict(signed.headers)}
        return super().request(method, url, *args, data=data, headers=headers, **kwargs)


def cloudwatch_span_exporter(region: str | None = None) -> OTLPSpanExporter:
    """An exporter for the X-Ray OTLP endpoint, signed with the default AWS
    credentials (the same ones the AWS CLI uses). region defaults to the
    AWS CLI's configured region."""
    session = botocore.session.get_session()
    region = region or session.get_config_variable("region")
    if not region:
        raise RuntimeError("no AWS region: pass one, or set AWS_REGION")
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials found (aws configure, or AWS_PROFILE)")
    return OTLPSpanExporter(
        endpoint=f"https://xray.{region}.amazonaws.com/v1/traces",
        session=_SigV4Session(region, credentials),
    )
