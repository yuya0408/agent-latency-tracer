"""Self-built MCP server, written against the SDK's low-level API.

Exposes the 4 business tools from the catalog (bodies are the mock in
system/tools/mock_server.py) plus one document retrieval tool. All 5 are
offered to the LLM, which decides when to search, the same way it decides
which business tool to call.

Server-side OTel is the SDK's built-in OpenTelemetryMiddleware, which the
low-level Server installs by default. `otel=False` removes it, standing in
for an MCP server that was built elsewhere without instrumentation.

Retrieval returns the text of the articles in data/corpus/. It runs on faiss
in-process with synthetic vectors (local development) or on PostgreSQL +
pgvector with the query embedded by Titan Text Embeddings V2
(`retrieval="pgvector"`; RDS and Bedrock on AWS).
"""
from __future__ import annotations
import asyncio, json, os, zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from mcp import types
from mcp.server import Server, ServerRequestContext
from opentelemetry import trace

from system.retrieval.corpus import generate_corpus, generate_queries
from system.retrieval.search import build_flat_index, search
from system.tools.loader import load_catalog
from system.tools.mock_server import call_tool as call_mock_tool
from system.tools.per_type import per_type_tools, to_baseline
from tracing.instrumentation import DB_RETURNED_ROWS, USAGE_INPUT_TOKENS, db_query_span, embeddings_span

SERVER_NAME = "agent-latency-tracer-tools"
RETRIEVAL_TOOL = "retrieve_documents"

CORPUS_PATH = Path(__file__).resolve().parents[2] / "data" / "corpus" / "chunks.jsonl"
N_QUERY_VECTORS = 1_000
# Without a reranker, defaults run from 2 (LlamaIndex) to 20 (OpenAI's file
# search); 10 sits between them. The cap keeps one call from filling the
# context (16,384 tokens). docs/DESIGN.md §6, 7.
DEFAULT_TOP_K = 10
MAX_TOP_K = 20

# What scripts/load_pgvector.py creates.
PGVECTOR_TABLE = "documents"
PGVECTOR_DIM = 1024
_PGVECTOR_SEARCH = (
    f"SELECT id, source, text, embedding <-> %(q)s AS distance FROM {PGVECTOR_TABLE}"
    " ORDER BY embedding <-> %(q)s LIMIT %(k)s"
)
EMBEDDING_PROVIDER = "aws.bedrock"
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"

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
    description="社内文書(規程・手順書・マニュアル)から、質問に近い文書の本文を返す。売上・在庫などの業務データの検索には使わない。",
    input_schema={
        "type": "object",
        "required": ["query"],
        "additionalProperties": False,
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K},
        },
    },
)


def _load_corpus() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()]


class _Retriever:
    """Local development, without AWS: the real articles, picked with
    synthetic vectors. The query text cannot be embedded here, so its hash
    picks one of the pre-generated query vectors and faiss returns the
    articles nearest to it: the same text always gets the same articles,
    which need not be related to it (docs/DESIGN.md §6, 7)."""

    def __init__(self) -> None:
        self._chunks = _load_corpus()
        self._index = build_flat_index(generate_corpus(n=len(self._chunks)))
        self._queries = generate_queries(N_QUERY_VECTORS)

    async def retrieve(self, ctx: ServerRequestContext, query: str, top_k: int) -> list[dict[str, Any]]:
        vector = self._queries[zlib.crc32(query.encode("utf-8")) % N_QUERY_VECTORS]
        # Synchronous faiss call inside the event loop: well under 1ms for a
        # few hundred articles, so it doesn't block anything noticeable.
        hits = search(self._index, np.ascontiguousarray(vector), top_k)
        return [{"id": c["id"], "source": c["source"], "text": c["text"]} for c in (self._chunks[i] for i in hits)]


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class _TitanEmbedder:
    """The query's vector from Titan Text Embeddings V2 on Bedrock (through
    the VPC endpoint on AWS), with the region from AWS_REGION.

    The client is made at start-up: making one reads botocore's service model,
    which takes a while and would block, while it opens no connection until the
    first request. That request comes after AgentCore assigns the session, so
    no socket made before the assignment is used (the DB pool waits for the
    first search for the same reason). For that reason there is no warm-up call.

    invoke_model is synchronous and runs in a thread: called directly it would
    hold up the event loop, which is pitfall #2's shape, in the baseline."""

    def __init__(self) -> None:
        import botocore.session  # needs the "aws" extra

        self._client = botocore.session.get_session().create_client("bedrock-runtime")

    def _invoke(self, text: str) -> dict[str, Any]:
        body = json.dumps({"inputText": text, "dimensions": PGVECTOR_DIM, "normalize": True})
        resp = self._client.invoke_model(
            modelId=EMBEDDING_MODEL, body=body, contentType="application/json", accept="application/json"
        )
        payload = json.loads(resp["body"].read())
        payload["retry_attempts"] = resp["ResponseMetadata"]["RetryAttempts"]
        return payload

    async def embed(self, text: str) -> list[float]:
        with embeddings_span(
            _tracer, provider_name=EMBEDDING_PROVIDER, request_model=EMBEDDING_MODEL, dimension_count=PGVECTOR_DIM
        ) as span:
            payload = await asyncio.to_thread(self._invoke, text)
            span.set_attribute(USAGE_INPUT_TOKENS, payload["inputTextTokenCount"])
            # botocore retries throttling and dropped connections on its own;
            # a slow first call with retries here is a retry, not the model.
            span.set_attribute("botocore.retry_attempts", payload["retry_attempts"])
            return payload["embedding"]


def _rds_iam_token() -> dict[str, str]:
    """A fresh IAM auth token as the password. Valid 15 minutes, and signed
    locally (no request to AWS), so making one per new connection is cheap."""
    import botocore.session  # needs the "aws" extra

    host, port, user = os.environ["PGHOST"], int(os.environ.get("PGPORT", "5432")), os.environ["PGUSER"]
    rds = botocore.session.get_session().create_client("rds")
    return {"password": rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)}


class _PgvectorRetriever:
    """The query embedded by `embedder`, then the nearest articles from
    PostgreSQL. Connection settings come from the libpq environment variables
    (PGHOST, PGUSER, ...); with RDS_IAM_AUTH=1 the password is an IAM token
    made per connection.

    The pool is made once per process in the server's lifespan, opened on the
    first search, and reached through ctx.lifespan_context. Async, so a search
    doesn't hold up the event loop, and pooled, so concurrent tool calls don't
    queue on one connection."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

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

    async def retrieve(self, ctx: ServerRequestContext, query: str, top_k: int) -> list[dict[str, Any]]:
        vector = np.asarray(await self._embedder.embed(query), dtype=np.float32)
        pool = ctx.lifespan_context["pool"]
        await pool.open()  # does nothing once the pool is open
        async with pool.connection() as conn:
            info = conn.info
            with db_query_span(
                _tracer, system="postgresql", operation="SELECT", collection=PGVECTOR_TABLE,
                query_text=_PGVECTOR_SEARCH, server_address=info.host, server_port=info.port,
            ) as span:
                cur = await conn.execute(_PGVECTOR_SEARCH, {"q": vector, "k": top_k})
                rows = await cur.fetchall()
                # Which articles came back and how close, not their text: the
                # text is in data/corpus/, and would make every trace heavy.
                # The vectors are unit length, so 1 - d^2/2 is the cosine similarity.
                docs = [{"id": i, "source": s, "score": round(1 - d * d / 2, 3), "text": t} for i, s, t, d in rows]
                span.set_attribute(DB_RETURNED_ROWS, len(docs))
                span.set_attribute("retrieval.document_ids", [d["id"] for d in docs])
                span.set_attribute("retrieval.scores", [d["score"] for d in docs])
                span.set_attribute("retrieval.text_chars", sum(len(d["text"]) for d in docs))
                return docs


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=is_error,
    )


def build_server(
    *,
    otel: bool = True,
    retrieval: str = "faiss",
    embedder: Embedder | None = None,
    pitfalls: frozenset[str] = frozenset(),
) -> Server:
    """`retrieval` is "faiss" (in-process, synthetic vectors) or "pgvector"
    (whatever scripts/load_pgvector.py loaded, searched with the query embedded
    by `embedder`; Titan V2 if not given). `pitfalls` are the cases switched on
    in this server (system/pitfalls.py)."""
    blocking = "2" in pitfalls
    contentless = "3" in pitfalls
    per_type = "M1" in pitfalls
    tools = _business_tools(per_type)
    business_names = {t.name for t in tools}
    if retrieval == "faiss":
        retriever: _Retriever | _PgvectorRetriever = _Retriever()
    elif retrieval == "pgvector":
        retriever = _PgvectorRetriever(embedder or _TitanEmbedder())
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
            docs = await retriever.retrieve(ctx, arguments["query"], arguments.get("top_k", DEFAULT_TOP_K))
            return _text_result({"documents": docs})
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
