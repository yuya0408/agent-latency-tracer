"""MCP client used by the agent: the SDK's Client, with tool calls wrapped in spans.

The SDK already emits a CLIENT span per request ("MCP send tools/call ...")
and injects traceparent into params._meta, as long as a global tracer
provider is set (tracing.instrumentation.set_global_tracer_provider). This
wrapper only adds the agent-side execute_tool span around each call, so
the SDK's span and the server's span nest under it.

Over HTTP it also writes traceparent into each request's HTTP headers, so
HTTP-level spans on the server side (ASGI middleware) join the same trace.
The SDK only puts it in params._meta, which HTTP-level code never reads.

A server on AgentCore Runtime (a bedrock-agentcore.<region>.amazonaws.com
URL) takes only SigV4-signed requests; those are signed with the local AWS
credentials (needs the "aws" extra).

One instance = one connection, reused for every call inside `async with`.
"""
from __future__ import annotations
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp import Client, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.shared._httpx_utils import create_mcp_http_client
from opentelemetry import propagate, trace

from tracing.instrumentation import (
    MCP_METHOD_NAME,
    MCP_PROTOCOL_VERSION,
    execute_tool_span,
    mark_error,
)


async def _inject_traceparent(request: httpx2.Request) -> None:
    propagate.inject(request.headers)


_AGENTCORE_HOST_PREFIX = "bedrock-agentcore."


class _SigV4Auth(httpx2.Auth):
    """Signs each request for AgentCore Runtime (signing name bedrock-agentcore).

    Only the host and the body are signed. Headers added after signing (the
    traceparent hook runs later) are sent unsigned, which SigV4 allows."""

    requires_request_body = True

    def __init__(self, region: str) -> None:
        import botocore.session  # needs the "aws" extra

        self._region = region
        # botocore refreshes temporary credentials on access, so hold the object.
        self._credentials = botocore.session.get_session().get_credentials()
        if self._credentials is None:
            raise RuntimeError("no AWS credentials found (aws configure, or AWS_PROFILE)")

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content)
        SigV4Auth(self._credentials, "bedrock-agentcore", self._region).add_auth(signed)
        request.headers.update(dict(signed.headers))
        yield request


class _SessionPin:
    """Sends back the Mcp-Session-Id that AgentCore Runtime returns, so the
    requests stay on one microVM; without it each one may land on a new one
    and wait for its cold start (5-8s each when this was first measured).

    The 2026-07-28 protocol has no sessions, so the SDK doesn't send one back;
    AgentCore's routing relies on this header anyway, and its docs ask clients
    to return it."""

    def __init__(self) -> None:
        self._session_id: str | None = None

    async def on_request(self, request: httpx2.Request) -> None:
        if self._session_id is not None and "mcp-session-id" not in request.headers:
            request.headers["mcp-session-id"] = self._session_id

    async def on_response(self, response: httpx2.Response) -> None:
        self._session_id = response.headers.get("mcp-session-id", self._session_id)


class McpToolClient:
    def __init__(self, server: str | Server, tracer: trace.Tracer, *, agent_name: str | None = None) -> None:
        """`server` is a URL (e.g. http://127.0.0.1:8000/mcp), or a Server object
        to connect in-process without HTTP (for tests)."""
        self._http: httpx2.AsyncClient | None = None
        if isinstance(server, str):
            # Same defaults (timeouts) the SDK would use, plus our request hook.
            self._http = create_mcp_http_client()
            self._http.event_hooks = {"request": [_inject_traceparent]}
            host = urlsplit(server).hostname or ""
            if host.startswith(_AGENTCORE_HOST_PREFIX):
                # bedrock-agentcore.<region>.amazonaws.com
                self._http.auth = _SigV4Auth(host.split(".")[1])
                pin = _SessionPin()
                self._http.event_hooks = {
                    "request": [_inject_traceparent, pin.on_request],
                    "response": [pin.on_response],
                }
            self._client = Client(streamable_http_client(server, http_client=self._http))
        else:
            self._client = Client(server)
        self._tracer = tracer
        self._agent_name = agent_name

    async def __aenter__(self) -> McpToolClient:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._client.__aexit__(*exc_info)
        if self._http is not None:  # passed in by us, so the SDK leaves closing it to us
            await self._http.aclose()

    async def list_tools(self) -> list[types.Tool]:
        return (await self._client.list_tools()).tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, tool_call_id: str | None = None
    ) -> types.CallToolResult:
        with execute_tool_span(
            self._tracer, tool_name=name, tool_call_id=tool_call_id, agent_name=self._agent_name
        ) as span:
            span.set_attribute(MCP_METHOD_NAME, "tools/call")
            span.set_attribute(MCP_PROTOCOL_VERSION, self._client.protocol_version)
            result = await self._client.call_tool(name, arguments)
            if result.is_error:
                mark_error(span, "tool_error")
            return result
