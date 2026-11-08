"""The pgvector retriever opens its connection pool on the first tools/list,
not when the server starts (system/mcp_server/server.py, _PgvectorRetriever).

Needs a PostgreSQL with the pgvector extension available, reached through the
libpq environment variables (PGHOST, PGUSER, ...), and the `rds` extra. The
tests make their own database and drop it afterwards; without PGHOST they are
skipped. For example, with the image the runbook uses:
  docker run -d -p 5433:5432 -e POSTGRES_PASSWORD=local pgvector/pgvector:0.8.1-pg18
  PGHOST=localhost PGPORT=5433 PGUSER=postgres PGPASSWORD=local pytest tests/test_pgvector_pool.py
"""
from __future__ import annotations

import asyncio
import json
import os

import numpy as np
import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")
pytest.importorskip("pgvector")
if not os.environ.get("PGHOST"):
    pytest.skip("PGHOST is not set", allow_module_level=True)

from opentelemetry import trace  # noqa: E402

from system.mcp_client import McpToolClient  # noqa: E402
from system.mcp_server.server import PGVECTOR_DIM, PGVECTOR_TABLE, RETRIEVAL_TOOL, build_server  # noqa: E402

TEST_DB = "agent_latency_tracer_pool_test"
N_DOCS = 20


@pytest.fixture
def test_db(monkeypatch):
    """A database with a small documents table; PGDATABASE points at it."""
    with psycopg.connect("", dbname="postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    with psycopg.connect("", dbname=TEST_DB) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(
            f"CREATE TABLE {PGVECTOR_TABLE} (id text PRIMARY KEY, source text, text text, embedding vector({PGVECTOR_DIM}))"
        )
        rng = np.random.default_rng(0)
        for i in range(N_DOCS):
            vec = "[" + ",".join(f"{x:.4f}" for x in rng.standard_normal(PGVECTOR_DIM)) + "]"
            conn.execute(
                f"INSERT INTO {PGVECTOR_TABLE} VALUES (%s, %s, %s, %s::vector)", (f"doc-{i}", "test", f"本文{i}", vec)
            )
    monkeypatch.setenv("PGDATABASE", TEST_DB)
    monkeypatch.delenv("RDS_IAM_AUTH", raising=False)
    yield
    with psycopg.connect("", dbname="postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


class FixedEmbedder:
    """Stands in for Titan: the same vector for any text, no AWS."""

    async def embed(self, text: str) -> list[float]:
        return [1.0] + [0.0] * (PGVECTOR_DIM - 1)


def _connections_to_test_db() -> int:
    with psycopg.connect("", dbname="postgres") as admin:
        return admin.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (TEST_DB,)).fetchone()[0]


def test_pool_connects_on_the_tool_list_not_at_start(test_db):
    async def run():
        server = build_server(otel=False, retrieval="pgvector", embedder=FixedEmbedder())
        async with McpToolClient(server, trace.get_tracer("test")) as client:
            # The server is up (lifespan entered, initialized) but has not
            # touched the database yet. The wait leaves time for a pool opened
            # at start-up, which connects in the background, to show up.
            await asyncio.sleep(1.0)
            at_start = _connections_to_test_db()
            # The tool list opens the pool; the connections come in the
            # background, ready before the first search.
            await client.list_tools()
            await asyncio.sleep(1.0)
            after_list = _connections_to_test_db()
            result = await client.call_tool(RETRIEVAL_TOOL, {"query": "返品の手順", "top_k": 3})
        return at_start, after_list, result

    at_start, after_list, result = asyncio.run(run())
    assert at_start == 0
    assert after_list >= 1
    assert not result.is_error
    docs = json.loads(result.content[0].text)["documents"]
    assert [d["text"] for d in docs] == [f"本文{d['id'].removeprefix('doc-')}" for d in docs]
    assert len(docs) == 3
