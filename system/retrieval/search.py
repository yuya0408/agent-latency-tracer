"""Brute-force (exact) vs ANN (HNSW) retrieval -- pitfall #5's before/after.

Both variants come from the same library (faiss): IndexFlatL2 is an exact
linear scan, IndexHNSWFlat is the approximate HNSW graph search. Using one
library for both keeps the distance metric identical between them, which
matters because brute-force's exact top-k doubles as recall@k's ground
truth -- no extra labeling is needed to measure the accuracy #5's
speedup costs (README "事例集" #5).
"""
from __future__ import annotations
import faiss
import numpy as np

VARIANTS: tuple[str, ...] = ("before", "after")

HNSW_M = 32  # faiss's default graph degree; not tuned, just made explicit
# faiss's default search breadth; not tuned, just made explicit. At this
# value ~1% of queries end in the wrong cluster with recall 0 (64 removed
# them) -- kept on purpose and reported, see docs/DESIGN.md 7.5.
HNSW_EF_SEARCH = 16


def build_flat_index(corpus: np.ndarray) -> faiss.IndexFlatL2:
    index = faiss.IndexFlatL2(corpus.shape[1])
    index.add(corpus)
    return index


def build_ann_index(corpus: np.ndarray) -> faiss.IndexHNSWFlat:
    index = faiss.IndexHNSWFlat(corpus.shape[1], HNSW_M)
    index.hnsw.efSearch = HNSW_EF_SEARCH
    index.add(corpus)
    return index


def search(index: faiss.Index, query: np.ndarray, k: int) -> list[int]:
    """One query per call, on purpose: batching several queries into one
    index.search() is ~3.3x faster and would erase the slowness #5
    exists to reproduce. Identical for both variants -- only the index
    passed in differs."""
    _, indices = index.search(query.reshape(1, -1), k)
    return indices[0].tolist()


def recall_at_k(ann_result: list[int], exact_result: list[int]) -> float:
    """Fraction of the exact top-k that the ANN result also found.

    exact_result must come from search() on a build_flat_index() index over
    the same corpus and query -- it is the ground truth, not an independent
    measurement.
    """
    if not exact_result:
        return 1.0
    return len(set(ann_result) & set(exact_result)) / len(exact_result)
