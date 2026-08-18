"""Offline re-ranking: k-reciprocal encoding, camera-aware Jaccard, query expansion.

Operates on an (N, D) matrix of L2-normalized track embeddings after a run.
k-reciprocal follows Zhong et al. CVPR'17 (k1=20, k2=6, lambda=0.3).
CA-Jaccard restricts k-reciprocal neighbor sets using camera labels so that
cross-camera neighbors are not crowded out by same-camera near-duplicates.
"""
from __future__ import annotations

import numpy as np


def _pairwise_cosine_dist(feats: np.ndarray) -> np.ndarray:
    sim = feats @ feats.T
    return np.clip(1.0 - sim, 0.0, 2.0)


def k_reciprocal_rerank(
    feats: np.ndarray,
    k1: int = 20,
    k2: int = 6,
    lambda_value: float = 0.3,
) -> np.ndarray:
    """Standard k-reciprocal re-ranking over all-vs-all track embeddings.

    Returns the re-ranked (N, N) distance matrix.
    """
    n = feats.shape[0]
    if n < 3:
        return _pairwise_cosine_dist(feats)
    k1 = min(k1, n - 1)
    k2 = min(k2, n - 1)

    original_dist = _pairwise_cosine_dist(feats)
    initial_rank = np.argsort(original_dist, axis=1)

    V = np.zeros_like(original_dist, dtype=np.float32)
    for i in range(n):
        forward = initial_rank[i, : k1 + 1]
        backward_ok = [
            j for j in forward if i in initial_rank[j, : k1 + 1]
        ]
        k_recip = set(backward_ok)
        # neighbor expansion
        for j in list(k_recip):
            cand = initial_rank[j, : int(k1 / 2) + 1]
            cand_recip = {c for c in cand if j in initial_rank[c, : int(k1 / 2) + 1]}
            if len(cand_recip & k_recip) > 2 / 3 * len(cand_recip):
                k_recip |= cand_recip
        idx = np.array(sorted(k_recip), dtype=int)
        weights = np.exp(-original_dist[i, idx])
        V[i, idx] = weights / weights.sum()

    if k2 > 1:
        V = np.stack([V[initial_rank[i, :k2]].mean(axis=0) for i in range(n)])

    # Jaccard distance from sparse V rows
    jaccard = np.zeros_like(original_dist, dtype=np.float32)
    for i in range(n):
        minim = np.minimum(V[i], V).sum(axis=1)
        maxim = np.maximum(V[i], V).sum(axis=1)
        jaccard[i] = 1.0 - minim / np.maximum(maxim, 1e-12)

    return jaccard * (1 - lambda_value) + original_dist * lambda_value


def ca_jaccard_rerank(
    feats: np.ndarray,
    cameras: list[str],
    k1: int = 20,
    k2: int = 6,
    lambda_value: float = 0.3,
    per_camera_k: int = 10,
) -> np.ndarray:
    """Camera-aware variant: k-reciprocal neighbor lists are built per camera
    (take top per_camera_k from EACH camera, then union) so same-camera
    near-duplicates cannot crowd out legitimate cross-camera neighbors.
    """
    n = feats.shape[0]
    if n < 3:
        return _pairwise_cosine_dist(feats)
    cams = np.asarray(cameras)
    original_dist = _pairwise_cosine_dist(feats)

    # camera-balanced initial rank: for each row, interleave best per camera
    initial_rank = np.zeros((n, n), dtype=int)
    for i in range(n):
        order = np.argsort(original_dist[i])
        picked, rest = [], []
        per_cam_count: dict[str, int] = {}
        for j in order:
            c = cams[j]
            if per_cam_count.get(c, 0) < per_camera_k:
                picked.append(j)
                per_cam_count[c] = per_cam_count.get(c, 0) + 1
            else:
                rest.append(j)
        initial_rank[i] = np.array(picked + rest, dtype=int)

    # standard k-reciprocal machinery on the camera-balanced ranks
    k1 = min(k1, n - 1)
    k2 = min(k2, n - 1)
    V = np.zeros_like(original_dist, dtype=np.float32)
    for i in range(n):
        forward = initial_rank[i, : k1 + 1]
        k_recip = {j for j in forward if i in initial_rank[j, : k1 + 1]}
        idx = np.array(sorted(k_recip), dtype=int)
        if idx.size == 0:
            idx = forward
        weights = np.exp(-original_dist[i, idx])
        V[i, idx] = weights / weights.sum()
    if k2 > 1:
        V = np.stack([V[initial_rank[i, :k2]].mean(axis=0) for i in range(n)])

    jaccard = np.zeros_like(original_dist, dtype=np.float32)
    for i in range(n):
        minim = np.minimum(V[i], V).sum(axis=1)
        maxim = np.maximum(V[i], V).sum(axis=1)
        jaccard[i] = 1.0 - minim / np.maximum(maxim, 1e-12)

    return jaccard * (1 - lambda_value) + original_dist * lambda_value


def average_query_expansion(feats: np.ndarray, top_k: int = 3) -> np.ndarray:
    """AQE: replace each embedding with the mean of itself and its top_k
    nearest neighbors, re-normalized. Returns expanded (N, D) features."""
    n = feats.shape[0]
    if n <= top_k:
        return feats
    dist = _pairwise_cosine_dist(feats)
    np.fill_diagonal(dist, np.inf)
    nn = np.argsort(dist, axis=1)[:, :top_k]
    expanded = np.stack([
        np.vstack([feats[i:i + 1], feats[nn[i]]]).mean(axis=0) for i in range(n)
    ])
    norms = np.linalg.norm(expanded, axis=1, keepdims=True)
    return expanded / np.maximum(norms, 1e-12)


def merge_ids_by_rerank(
    track_gids: list[int],
    feats: np.ndarray,
    cameras: list[str],
    method: str = "k_reciprocal",
    merge_threshold: float = 0.25,
) -> dict[int, int]:
    """Post-run ID consolidation: re-rank all track embeddings, then merge
    global IDs whose re-ranked distance falls under merge_threshold.

    Returns {old_gid -> canonical_gid} mapping (union-find over merges).
    """
    if method == "aqe":
        dist = _pairwise_cosine_dist(average_query_expansion(feats))
    elif method == "ca_jaccard":
        dist = ca_jaccard_rerank(feats, cameras)
    else:
        dist = k_reciprocal_rerank(feats)

    parent = {g: g for g in set(track_gids)}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n = len(track_gids)
    for i in range(n):
        for j in range(i + 1, n):
            if track_gids[i] == track_gids[j]:
                continue
            if dist[i, j] < merge_threshold:
                ri, rj = find(track_gids[i]), find(track_gids[j])
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    return {g: find(g) for g in parent}
