"""Load the synthetic corpus into PostgreSQL + pgvector (RDS, through the
Session Manager tunnel; see infra/).

  python scripts/load_pgvector.py            # 100,000 x 1024
  python scripts/load_pgvector.py --n 1000   # a quick check

Connection settings come from the usual libpq environment variables
(PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, PGSSLMODE), so the same
script works against any PostgreSQL with pgvector.

The table is recreated each time, without a vector index: building HNSW is
part of the measurement (scripts/measure_pgvector.py).
"""
from __future__ import annotations
import argparse, sys, time
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from system.retrieval.corpus import generate_corpus

# 1024: the output size of common embedding models (Titan Text Embeddings V2's
# default, bge-m3, multilingual-e5-large). 100,000 chunks: a mid-sized set of
# internal documents (e.g. 5,000 documents x 20 chunks). docs/DESIGN.md §1.
DEFAULT_N = 100_000
DEFAULT_DIM = 1024
TABLE = "documents"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--dim", type=int, default=DEFAULT_DIM)
    args = parser.parse_args()

    vectors = generate_corpus(n=args.n, dim=args.dim)

    with psycopg.connect() as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(conn)
        conn.execute(f"DROP TABLE IF EXISTS {TABLE}")
        conn.execute(f"CREATE TABLE {TABLE} (id bigint PRIMARY KEY, embedding vector({args.dim}) NOT NULL)")

        start = time.perf_counter()
        with conn.cursor().copy(f"COPY {TABLE} (id, embedding) FROM STDIN WITH (FORMAT BINARY)") as copy:
            copy.set_types(["int8", "vector"])
            for i, v in enumerate(vectors):
                copy.write_row([i, v])
        conn.commit()
        print(f"copied {args.n} rows in {time.perf_counter() - start:.1f}s")

        # Planner statistics, so EXPLAIN in the measurement sees the real row count.
        conn.execute(f"ANALYZE {TABLE}")
        size = conn.execute(f"SELECT pg_size_pretty(pg_total_relation_size('{TABLE}'))").fetchone()[0]
        print(f"table size: {size}")


if __name__ == "__main__":
    main()
