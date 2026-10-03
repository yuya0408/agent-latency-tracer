"""Tests for system/retrieval/: pitfall #5's observed target.

corpus.py/search.py are pure numpy/faiss logic (no dependency on
toolcall-serving-bench -- synthetic vectors need no semantic meaning, only
scale, cluster structure and an exact ground truth, see README "事例集" #5).
"""
from __future__ import annotations

import numpy as np
import pytest

from system.retrieval.corpus import generate_corpus, generate_queries
from system.retrieval.search import build_ann_index, build_flat_index, recall_at_k, search


def test_generate_corpus_is_reproducible_with_same_seed():
    a = generate_corpus(n=50, dim=8, seed=0)
    b = generate_corpus(n=50, dim=8, seed=0)
    assert np.array_equal(a, b)


def test_generate_corpus_shape_and_dtype():
    corpus = generate_corpus(n=50, dim=8, seed=0)
    assert corpus.shape == (50, 8)
    assert corpus.dtype == np.float32


def test_queries_land_near_the_corpus_generated_with_the_same_seed():
    # Same seed = same cluster centers, so a query's nearest corpus point is
    # an intra-cluster neighbor. A different seed means unrelated centers,
    # so the nearest point is much farther away.
    corpus = generate_corpus(n=2000, dim=32, seed=0)
    index = build_flat_index(corpus)
    same = generate_queries(20, dim=32, seed=0)
    other = generate_queries(20, dim=32, seed=1)
    same_dist, _ = index.search(same, 1)
    other_dist, _ = index.search(other, 1)
    assert same_dist.max() < other_dist.min()


def test_flat_search_returns_k_indices():
    corpus = generate_corpus(n=100, dim=16, seed=0)
    query = generate_queries(1, dim=16, seed=0)[0]
    result = search(build_flat_index(corpus), query, k=10)
    assert len(result) == 10
    assert all(0 <= i < 100 for i in result)
    assert len(set(result)) == 10  # no duplicate indices


def test_flat_search_finds_the_exact_closest_point():
    corpus = np.array(
        [[0, 0, 0], [10, 10, 10], [5, 5, 5], [-10, -10, -10], [1, 1, 1]],
        dtype="float32",
    )
    query = np.array([0.1, 0.1, 0.1], dtype="float32")
    assert search(build_flat_index(corpus), query, k=1) == [0]


def test_recall_at_k_identical_results_is_one():
    assert recall_at_k([1, 2, 3], [1, 2, 3]) == 1.0


def test_recall_at_k_disjoint_results_is_zero():
    assert recall_at_k([4, 5, 6], [1, 2, 3]) == 0.0


def test_recall_at_k_partial_overlap():
    assert recall_at_k([1, 2, 9], [1, 2, 3]) == pytest.approx(2 / 3)


def test_recall_at_k_empty_ground_truth_is_one():
    assert recall_at_k([], []) == 1.0


def test_ann_search_returns_k_indices():
    corpus = generate_corpus(n=500, dim=32, seed=0)
    query = generate_queries(1, dim=32, seed=0)[0]
    result = search(build_ann_index(corpus), query, k=10)
    assert len(result) == 10
    assert all(0 <= i < 500 for i in result)


def test_ann_search_has_high_mean_recall_against_brute_force():
    # Averaged over many queries, not checked per query: with faiss's
    # default efSearch (16), ~1% of queries end the greedy graph walk in the
    # wrong cluster and get recall 0 (measured 2026-09-28 at n=20,000/
    # dim=128: mean 0.95-0.98 over several seeds). A per-query bound would
    # be flaky; the mean is what #5 reports anyway.
    corpus = generate_corpus(n=20_000, dim=128, seed=0)
    queries = generate_queries(50, dim=128, seed=0)
    flat, ann = build_flat_index(corpus), build_ann_index(corpus)
    recalls = [recall_at_k(search(ann, q, 10), search(flat, q, 10)) for q in queries]
    assert np.mean(recalls) >= 0.9
