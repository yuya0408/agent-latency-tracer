"""Load the documents table into PostgreSQL + pgvector (RDS, through the
Session Manager tunnel; see infra/).

  python scripts/load_pgvector.py                      # the corpus (data/corpus/), with HNSW
  python scripts/load_pgvector.py --synthetic 100000   # pitfall #5's synthetic vectors, no index
  python scripts/load_pgvector.py --synthetic 1000     # a quick check

The corpus needs out/corpus_embeddings.npz (scripts/embed_corpus.py); its
rows carry the article's text, which retrieve_documents returns. The HNSW
index uses pgvector's defaults: it is the fixed state the runs start from.

--synthetic is what pitfall #5 measured (scripts/measure_pgvector.py): ids
and meaningless vectors only, and no index, because building HNSW is part of
that measurement.

Connection settings come from the usual libpq environment variables
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE), so the same
script works against any PostgreSQL with pgvector. The table is recreated
each time, which drops its grants: run scripts/create_mcp_server_user.sql after.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from system.retrieval.corpus import generate_corpus

CHUNKS = ROOT / "data" / "corpus" / "chunks.jsonl"
EMBEDDINGS = ROOT / "out" / "corpus_embeddings.npz"

# 1024: the output size of common embedding models (Titan Text Embeddings V2's
# default, bge-m3, multilingual-e5-large). 100,000 chunks: a mid-sized set of
# internal documents (e.g. 5,000 documents x 20 chunks). docs/DESIGN.md §1.
DEFAULT_N = 100_000
DEFAULT_DIM = 1024
TABLE = "documents"
INDEX = f"{TABLE}_embedding_hnsw"

CORPUS_COLUMNS = ("id", "doc_title", "article", "caption", "source", "text", "embedding")


def corpus_rows() -> list[tuple]:
    """The chunks joined with their vectors by id; fails if the two files
    were made from different versions of the corpus."""
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines()]
    emb = np.load(EMBEDDINGS)
    vectors = dict(zip(emb["ids"].tolist(), emb["vectors"]))
    if set(vectors) != {c["id"] for c in chunks}:
        sys.exit(f"{EMBEDDINGS.name} does not match {CHUNKS.name}; run scripts/embed_corpus.py --force")
    return [tuple(c[k] for k in CORPUS_COLUMNS[:-1]) + (vectors[c["id"]],) for c in chunks]


def load_corpus(conn: psycopg.Connection) -> int:
    rows = corpus_rows()
    dim = len(rows[0][-1])
    conn.execute(f"""CREATE TABLE {TABLE} (
        id text PRIMARY KEY, doc_title text NOT NULL, article text NOT NULL, caption text NOT NULL,
        source text NOT NULL, text text NOT NULL, embedding vector({dim}) NOT NULL)""")
    with conn.cursor().copy(f"COPY {TABLE} ({', '.join(CORPUS_COLUMNS)}) FROM STDIN WITH (FORMAT BINARY)") as copy:
        copy.set_types(["text"] * 6 + ["vector"])
        for row in rows:
            copy.write_row(row)
    conn.execute(f"CREATE INDEX {INDEX} ON {TABLE} USING hnsw (embedding vector_l2_ops)")
    return len(rows)


def load_synthetic(conn: psycopg.Connection, n: int, dim: int) -> int:
    conn.execute(f"CREATE TABLE {TABLE} (id bigint PRIMARY KEY, embedding vector({dim}) NOT NULL)")
    with conn.cursor().copy(f"COPY {TABLE} (id, embedding) FROM STDIN WITH (FORMAT BINARY)") as copy:
        copy.set_types(["int8", "vector"])
        for i, v in enumerate(generate_corpus(n=n, dim=dim)):
            copy.write_row([i, v])
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--synthetic", type=int, metavar="N", help=f"load N synthetic vectors instead (pitfall #5 used {DEFAULT_N})")
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM, help="with --synthetic")
    args = parser.parse_args()

    with psycopg.connect() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")

        start = time.perf_counter()
        n = load_synthetic(conn, args.synthetic, args.dim) if args.synthetic else load_corpus(conn)
        conn.commit()
        print(f"loaded {n} rows in {time.perf_counter() - start:.1f}s")

        # Planner statistics, so EXPLAIN in the measurement sees the real row count.
        conn.execute(f"ANALYZE {TABLE}")
        size = conn.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE}'))").fetchone()[0]
        print(f"table size: {size}")


if __name__ == "__main__":
    main()
