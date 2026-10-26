"""Measure vector search in PostgreSQL + pgvector without and with an HNSW
index (pitfall #5's before/after), on the table scripts/load_pgvector.py made.

  python scripts/measure_pgvector.py

Times are the server's own execution time from EXPLAIN ANALYZE, so the
Session Manager tunnel's latency is not in them. Each pass runs the query set
once to warm the cache and once to measure: the steady state of a running
service, not a cold start. Results also go to out/pgvector_measure.json.
"""
from __future__ import annotations
import argparse, json, statistics, sys, time
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.load_pgvector import DEFAULT_DIM, TABLE
from system.retrieval.corpus import generate_queries

INDEX = f"{TABLE}_embedding_hnsw"
TOP_K = 5  # the MCP server's default (system/mcp_server/server.py)
N_QUERIES = 100
OUT = Path("out/pgvector_measure.json")
SEARCH = f"SELECT id FROM {TABLE} ORDER BY embedding <-> %s LIMIT {TOP_K}"


def _scan_type(node: dict) -> str | None:
    """The plan node that reads the table: 'Seq Scan' or 'Index Scan'."""
    if node.get("Relation Name") == TABLE:
        return node["Node Type"]
    for child in node.get("Plans", []):
        if (found := _scan_type(child)) is not None:
            return found
    return None


def measure(conn: psycopg.Connection, queries) -> dict:
    for q in queries:  # warm-up pass, discarded
        conn.execute(SEARCH, (q,)).fetchall()
    times, ids, scans, read_blocks = [], [], set(), 0
    for q in queries:
        plan = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) {SEARCH}", (q,)).fetchone()[0][0]
        times.append(plan["Execution Time"])
        scans.add(_scan_type(plan["Plan"]))
        read_blocks += plan["Plan"].get("Shared Read Blocks", 0)
        ids.append([row[0] for row in conn.execute(SEARCH, (q,))])
    return {
        "scan": sorted(scans),
        "ms_p50": statistics.median(times),
        "ms_p95": statistics.quantiles(times, n=20)[18],
        "ms_mean": statistics.fmean(times),
        "shared_read_blocks": read_blocks,  # reads that missed PostgreSQL's cache
        "ids": ids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--queries", type=int, default=N_QUERIES)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    args = parser.parse_args()

    queries = generate_queries(args.queries, dim=args.dim)

    with psycopg.connect(autocommit=True) as conn:
        register_vector(conn)
        result = {
            "server": conn.execute("SELECT version()").fetchone()[0],
            "pgvector": conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0],
            "rows": conn.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0],
            "dim": args.dim,
            "queries": args.queries,
            "top_k": TOP_K,
        }

        conn.execute(f"DROP INDEX IF EXISTS {INDEX}")
        before = measure(conn, queries)

        # Only speeds up the build (pgvector's docs recommend raising it); searches don't use it.
        conn.execute("SET maintenance_work_mem = '1GB'")
        start = time.perf_counter()
        # pgvector's defaults (m=16, ef_construction=64): not tuned.
        conn.execute(f"CREATE INDEX {INDEX} ON {TABLE} USING hnsw (embedding vector_l2_ops)")
        result["hnsw_build_s"] = time.perf_counter() - start
        result["hnsw_size"] = conn.execute(f"SELECT pg_size_pretty(pg_relation_size('{INDEX}'))").fetchone()[0]
        result["hnsw_ef_search"] = conn.execute("SHOW hnsw.ef_search").fetchone()[0]
        after = measure(conn, queries)

    # The exact scan's top-k is the ground truth, so recall needs no labels.
    result["recall_at_k"] = statistics.fmean(
        len(set(a) & set(e)) / TOP_K for a, e in zip(after.pop("ids"), before.pop("ids"))
    )
    result["before"], result["after"] = before, after

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
