"""Synthetic embedding corpus for pitfall #5's observed target.

Vectors carry no semantic meaning, but MUST have cluster structure --
plain iid standard-normal vectors in 128 dimensions concentrate pairwise
distances (curse of dimensionality: every point is nearly equidistant
from every other), which makes recall@k against such a corpus not
representative of recall@k against real (clustered) embeddings. Points
are drawn near one of n_clusters centers instead, so "nearest neighbor"
is a meaningful, recoverable relationship (case #5 in docs/runbook.md).

Corpus and queries must share the same cluster centers (a query should
land near an actual cluster of corpus points, not an unrelated random
point) -- generate_corpus and generate_queries regenerate the same
centers deterministically from the same `seed`, then diverge into
separate random streams for their own points.
"""
from __future__ import annotations
import numpy as np

DEFAULT_N = 500_000
DEFAULT_DIM = 128
DEFAULT_N_CLUSTERS = 200
DEFAULT_CLUSTER_STD = 1.0
DEFAULT_CENTER_SCALE = 10.0

# Seed offsets keep centers, corpus points, and query points on separate
# deterministic streams while letting corpus/queries share centers by
# passing the same base seed.
_CENTER_SEED_OFFSET = 0
_CORPUS_SEED_OFFSET = 1_000_000
_QUERY_SEED_OFFSET = 2_000_000


def _generate_cluster_centers(n_clusters: int, dim: int, seed: int, center_scale: float) -> np.ndarray:
    rng = np.random.default_rng(seed + _CENTER_SEED_OFFSET)
    return (rng.standard_normal((n_clusters, dim)) * center_scale).astype("float32")


def generate_corpus(
    n: int = DEFAULT_N,
    dim: int = DEFAULT_DIM,
    seed: int = 0,
    n_clusters: int = DEFAULT_N_CLUSTERS,
    cluster_std: float = DEFAULT_CLUSTER_STD,
    center_scale: float = DEFAULT_CENTER_SCALE,
) -> np.ndarray:
    centers = _generate_cluster_centers(n_clusters, dim, seed, center_scale)
    rng = np.random.default_rng(seed + _CORPUS_SEED_OFFSET)
    assignments = rng.integers(0, n_clusters, size=n)
    noise = rng.standard_normal((n, dim)).astype("float32") * cluster_std
    return centers[assignments] + noise


def generate_queries(
    n_queries: int,
    dim: int = DEFAULT_DIM,
    seed: int = 0,
    n_clusters: int = DEFAULT_N_CLUSTERS,
    cluster_std: float = DEFAULT_CLUSTER_STD,
    center_scale: float = DEFAULT_CENTER_SCALE,
) -> np.ndarray:
    """seed must match the seed passed to generate_corpus() for the
    queries to land near that corpus's actual clusters."""
    centers = _generate_cluster_centers(n_clusters, dim, seed, center_scale)
    rng = np.random.default_rng(seed + _QUERY_SEED_OFFSET)
    cluster_ids = rng.integers(0, n_clusters, size=n_queries)
    noise = rng.standard_normal((n_queries, dim)).astype("float32") * cluster_std
    return centers[cluster_ids] + noise
