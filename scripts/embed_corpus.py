"""Embed data/corpus/chunks.jsonl with Titan Text Embeddings V2, once, from
this PC (docs/DESIGN.md §6, 7).

  python scripts/embed_corpus.py           # skips if out/corpus_embeddings.npz exists
  python scripts/embed_corpus.py --force

Calls Bedrock over the public endpoint with the usual AWS credentials; the
VPC endpoint is only for the MCP server's per-question calls. The vectors go
to out/ (not committed) and are loaded into PostgreSQL by load_pgvector.py.
One text per request: the API takes no batch.
"""
from __future__ import annotations
import argparse, json, time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CHUNKS = ROOT / "data" / "corpus" / "chunks.jsonl"
OUT = ROOT / "out" / "corpus_embeddings.npz"

REGION = "ap-northeast-1"
MODEL_ID = "amazon.titan-embed-text-v2:0"
# The model's default size, and what the documents table is built for.
DIM = 1024


def embed(client, text: str) -> tuple[list[float], int]:
    """(vector, input token count). normalize=True (the API's default, made
    explicit) gives unit vectors, so L2 and cosine rank the same."""
    body = json.dumps({"inputText": text, "dimensions": DIM, "normalize": True})
    resp = client.invoke_model(modelId=MODEL_ID, body=body, contentType="application/json", accept="application/json")
    payload = json.loads(resp["body"].read())
    return payload["embedding"], payload["inputTextTokenCount"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="embed again even if the output exists")
    args = parser.parse_args()
    if OUT.exists() and not args.force:
        print(f"{OUT.relative_to(ROOT)} exists; pass --force to embed again")
        return

    import botocore.session  # needs the "aws" extra
    from botocore.config import Config

    client = botocore.session.get_session().create_client("bedrock-runtime", region_name=REGION, config=Config(retries={"mode": "standard"}))
    chunks = [json.loads(line) for line in CHUNKS.read_text(encoding="utf-8").splitlines()]
    vectors, tokens = [], []
    start = time.perf_counter()
    for c in chunks:
        v, n = embed(client, c["text"])
        vectors.append(v)
        tokens.append(n)
    elapsed = time.perf_counter() - start

    OUT.parent.mkdir(parents=True, exist_ok=True)
    np.savez(OUT, ids=np.array([c["id"] for c in chunks]), vectors=np.array(vectors, dtype="float32"),
             tokens=np.array(tokens), model=np.array(MODEL_ID))
    chars = sum(len(c["text"]) for c in chunks)
    print(f"embedded {len(chunks)} chunks in {elapsed:.0f}s: {sum(tokens)} tokens for {chars} characters "
          f"({sum(tokens) / chars:.2f} tokens per character), longest {max(tokens)} tokens")
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
