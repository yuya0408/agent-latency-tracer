"""Self-built MCP server, written against the SDK's low-level API.

Exposes the 4 business tools from the catalog (bodies are the mock in
system/tools/mock_server.py) plus one document retrieval tool. All 5 are
offered to the LLM, which decides when to search, the same way it decides
which business tool to call.

Server-side OTel is the SDK's built-in OpenTelemetryMiddleware, which the
low-level Server installs by default. `otel=False` removes it, standing in
for an MCP server that was built elsewhere without instrumentation.

Retrieval runs on faiss in-process (local development) or on PostgreSQL +
pgvector (`retrieval="pgvector"`; RDS on AWS).
"""
from __future__ import annotations
import json, os, zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from mcp import types
from mcp.server import Server, ServerRequestContext
from opentelemetry import trace

from system.retrieval.corpus import generate_corpus, generate_queries
from system.retrieval.search import build_flat_index, search
from system.tools.loader import load_catalog
from system.tools.mock_server import call_tool as call_mock_tool
from system.tools.per_type import per_type_tools, to_baseline
from tracing.instrumentation import db_query_span

SERVER_NAME = "agent-latency-tracer-tools"
RETRIEVAL_TOOL = "retrieve_documents"

# Small enough to build at startup in well under a second. Pitfall #5's
# full-size corpus (500k) is the case file's job, not the server default.
DEFAULT_CORPUS_N = 10_000
N_QUERY_VECTORS = 1_000
DEFAULT_TOP_K = 5

# What scripts/load_pgvector.py creates (same seed, so the query vectors
# below land near the loaded documents).
PGVECTOR_TABLE = "documents"
PGVECTOR_DIM = 1024
_PGVECTOR_SEARCH = f"SELECT id FROM {PGVECTOR_TABLE} ORDER BY embedding <-> %s LIMIT %s"

# A proxy until a tracer provider is registered, so it can be made at import.
_tracer = trace.get_tracer("agent-latency-tracer")


def _business_tools(per_type: bool = False) -> list[types.Tool]:
    """per_type=True is pitfall M1's switch (system/tools/per_type.py)."""
    defs = per_type_tools() if per_type else load_catalog().values()
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            input_schema=t["inputSchema"],
        )
        for t in defs
    ]


_RETRIEVAL_TOOL_DEF = types.Tool(
    name=RETRIEVAL_TOOL,
    # Says "documents, not business records" up front: find_records also
    # searches, and a small model may mix the two up.
    description="社内文書(規程・手順書・マニュアル)から、質問に近い文書のIDを返す。売上・在庫などの業務データの検索には使わない。",
    input_schema={
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
)


class _Retriever:
    """The vectors are synthetic and carry no meaning, so the query text
    cannot be embedded. Its hash only picks one of the pre-generated query
    vectors: the same text always hits the same vector, nothing more."""

    def __init__(self, corpus_n: int) -> None:
        self._index = build_flat_index(generate_corpus(n=corpus_n))
        self._queries = generate_queries(N_QUERY_VECTORS)

    async def retrieve(self, ctx: ServerRequestContext, query: str, top_k: int) -> list[int]:
        vector = self._queries[zlib.crc32(query.encode("utf-8")) % N_QUERY_VECTORS]
        # Synchronous faiss call inside the event loop: well under 1ms at
        # the default corpus size, so it doesn't block anything noticeable.
        return search(self._index, np.ascontiguousarray(vector), top_k)


def _rds_iam_token() -> dict[str, str]:
    """A fresh IAM auth token as the password. Valid 15 minutes, and signed
    locally (no request to AWS), so making one per new connection is cheap."""
    import botocore.session  # needs the "aws" extra

    host, port, user = os.environ["PGHOST"], int(os.environ.get("PGPORT", "5432")), os.environ["PGUSER"]
    rds = botocore.session.get_session().create_client("rds")
    return {"password": rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)}


class _PgvectorRetriever:
    """Same idea as _Retriever, with the search in PostgreSQL. Connection
    settings come from the libpq environment variables (PGHOST, PGUSER, ...);
    with RDS_IAM_AUTH=1 the password is an IAM token made per connection.

    The pool is made once per process in the server's lifespan, opened on the
    first search, and reached through ctx.lifespan_context. Async, so a search
    doesn't hold up the event loop, and pooled, so concurrent tool calls don't
    queue on one connection."""

    def __init__(self) -> None:
        self._queries = generate_queries(N_QUERY_VECTORS, dim=PGVECTOR_DIM)

    @asynccontextmanager
    async def lifespan(self, server: Server) -> AsyncIterator[dict[str, Any]]:
        # Imported here: only the `rds` extra installs these.
        from pgvector.psycopg import register_vector_async
        from psycopg_pool import AsyncConnectionPool

        # The pool calls kwargs for every new connection, so tokens don't expire on it.
        kwargs = _rds_iam_token if os.environ.get("RDS_IAM_AUTH") == "1" else None
        # open=False: no connections until the first search. AgentCore starts
        # the process before it assigns a session, and connections made then
        # were reset at their first use after the assignment (2026-11-06).
        # check: test each connection before handing it out, for one that
        # dies while it sits unused in the pool.
        pool = AsyncConnectionPool(
            "", kwargs=kwargs, configure=register_vector_async, check=AsyncConnectionPool.check_connection,
            open=False,
        )
        try:
            yield {"pool": pool}
        finally:
            await pool.close()

    async def retrieve(self, ctx: ServerRequestContext, query: str, top_k: int) -> list[int]:
        vector = self._queries[zlib.crc32(query.encode("utf-8")) % N_QUERY_VECTORS]
        pool = ctx.lifespan_context["pool"]
        await pool.open()  # does nothing once the pool is open
        async with pool.connection() as conn:
            info = conn.info
            with db_query_span(
                _tracer, system="postgresql", operation="SELECT", collection=PGVECTOR_TABLE,
                query_text=_PGVECTOR_SEARCH, server_address=info.host, server_port=info.port,
            ):
                cur = await conn.execute(_PGVECTOR_SEARCH, (vector, top_k))
                return [row[0] for row in await cur.fetchall()]


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=is_error,
    )


def build_server(
    *,
    otel: bool = True,
    corpus_n: int = DEFAULT_CORPUS_N,
    retrieval: str = "faiss",
    pitfalls: frozenset[str] = frozenset(),
) -> Server:
    """`retrieval` is "faiss" (in-process, `corpus_n` documents) or "pgvector"
    (whatever scripts/load_pgvector.py loaded; `corpus_n` is not used).
    `pitfalls` are the cases switched on in this server (system/pitfalls.py)."""
    blocking = "2" in pitfalls
    contentless = "3" in pitfalls
    per_type = "M1" in pitfalls
    tools = _business_tools(per_type)
    business_names = {t.name for t in tools}
    if retrieval == "faiss":
        retriever: _Retriever | _PgvectorRetriever = _Retriever(corpus_n)
    elif retrieval == "pgvector":
        retriever = _PgvectorRetriever()
    else:
        raise ValueError(f"unknown retrieval backend: {retrieval}")

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[*tools, _RETRIEVAL_TOOL_DEF])

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name == RETRIEVAL_TOOL:
            ids = await retriever.retrieve(ctx, arguments["query"], arguments.get("top_k", DEFAULT_TOP_K))
            return _text_result({"document_ids": ids})
        if params.name in business_names:
            name = params.name
            if per_type:
                name, arguments = to_baseline(name, arguments)
            result = await call_mock_tool(name, arguments, contentless=contentless, blocking=blocking)
            return _text_result(result, is_error=result["status"] == "error")
        return _text_result({"error": f"unknown tool: {params.name}"}, is_error=True)

    extra: dict[str, Any] = {}
    if isinstance(retriever, _PgvectorRetriever):
        extra["lifespan"] = retriever.lifespan
    server = Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool, **extra)
    if not otel:
        server.middleware = []
    return server
