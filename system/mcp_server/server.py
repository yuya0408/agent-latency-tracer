"""Self-built MCP server, written against the SDK's low-level API.

Exposes the 40 business tools from the catalog (bodies are the mock in
system/tools/mock_server.py) plus one document retrieval tool. All 41 are
offered to the LLM, which decides when to search, the same way it decides
which business tool to call.

Server-side OTel is the SDK's built-in OpenTelemetryMiddleware, which the
low-level Server installs by default. `otel=False` removes it, standing in
for an MCP server that was built elsewhere without instrumentation.
"""
from __future__ import annotations
import json, zlib
from typing import Any

import numpy as np
from mcp import types
from mcp.server import Server, ServerRequestContext

from system.retrieval.corpus import generate_corpus, generate_queries
from system.retrieval.search import build_flat_index, search
from system.tools.loader import load_catalog
from system.tools.mock_server import call_tool as call_mock_tool

SERVER_NAME = "agent-latency-tracer-tools"
RETRIEVAL_TOOL = "retrieve_documents"

# Small enough to build at startup in well under a second. Pitfall #5's
# full-size corpus (500k) is the case file's job, not the server default.
DEFAULT_CORPUS_N = 10_000
N_QUERY_VECTORS = 1_000
DEFAULT_TOP_K = 5

# toolcall-serving-bench's scoring annotation; meaningless to an LLM and
# would only inflate the prompt.
_BENCH_ONLY_KEYS = ("x-scoring",)


def _strip_bench_keys(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip_bench_keys(v) for k, v in schema.items() if k not in _BENCH_ONLY_KEYS}
    if isinstance(schema, list):
        return [_strip_bench_keys(v) for v in schema]
    return schema


def _business_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            input_schema=_strip_bench_keys(t["parameters"]),
        )
        for t in load_catalog().values()
    ]


_RETRIEVAL_TOOL_DEF = types.Tool(
    name=RETRIEVAL_TOOL,
    # Says "documents, not business records" up front: the catalog already has
    # many search_* tools over business data, and a small model may mix them up.
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

    def retrieve(self, query: str, top_k: int) -> list[int]:
        vector = self._queries[zlib.crc32(query.encode("utf-8")) % N_QUERY_VECTORS]
        return search(self._index, np.ascontiguousarray(vector), top_k)


def _text_result(payload: Any, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
        is_error=is_error,
    )


def build_server(*, otel: bool = True, corpus_n: int = DEFAULT_CORPUS_N) -> Server:
    tools = _business_tools()
    business_names = {t.name for t in tools}
    retriever = _Retriever(corpus_n)

    async def on_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[*tools, _RETRIEVAL_TOOL_DEF])

    async def on_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        arguments = params.arguments or {}
        if params.name == RETRIEVAL_TOOL:
            # Synchronous faiss call inside the event loop: well under 1ms at
            # the default corpus size, so it doesn't block anything noticeable.
            ids = retriever.retrieve(arguments["query"], arguments.get("top_k", DEFAULT_TOP_K))
            return _text_result({"document_ids": ids})
        if params.name in business_names:
            return _text_result(await call_mock_tool(params.name, arguments))
        return _text_result({"error": f"unknown tool: {params.name}"}, is_error=True)

    server = Server(SERVER_NAME, on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    if not otel:
        server.middleware = []
    return server
