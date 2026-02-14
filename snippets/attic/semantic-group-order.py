"""
Semantic “flow” ordering for near-duplicate groups using OpenCLIP-style embeddings.

What this does
--------------
Given a mapping of {group_key: embedding_vector}, this module produces a 1D ordering of
group keys such that neighbouring groups in the list are usually semantically similar.
The goal is a gallery that “flows” in a way that mostly makes intuitive sense, rather
than appearing arbitrary.

Why this approach
-----------------
A naive attempt to “connect all points with the shortest path” resembles the travelling
salesman / Hamiltonian path problem and is computationally intractable at tens of
thousands of groups.

Instead, we approximate the desired behaviour with a scalable pipeline:

  1) Represent each group by a single vector (typically the centroid of member image
     embeddings). This reduces the problem from per-image to per-group scale.

  2) Reduce dimensionality (PCA to target_dim, e.g. 128) and optionally store as float16.
     For the “make it feel coherent” goal, 128 dims at float16 is usually sufficient,
     and it drastically reduces storage and index cost.

  3) Build an approximate k-nearest-neighbour graph (HNSW) in cosine space.
     This provides a sparse set of candidate “similarity edges” without O(n^2) work.

  4) Compute a minimum spanning tree (MST) over that sparse graph.
     The MST connects all groups while favouring short (high-similarity) edges, giving a
     globally connected structure with good locality at low cost.

  5) Traverse the MST (DFS with simple heuristics) to derive a 1D ordering.
     The traversal order tends to keep semantically related groups adjacent and produces
     a smooth browsing experience. It also avoids the expensive global optimisation that
     a true shortest Hamiltonian path would require.

This is intentionally “good enough” for human browsing: it prioritises scalability,
stability, and locality over optimality.

Complexity (big-O)
------------------
Let:
  n = number of groups
  d = original embedding dimension (e.g. 512)
  r = reduced dimension (e.g. 128)
  k = kNN degree per node (e.g. 24)

PCA reduction (IncrementalPCA):
  - Fit:    O(n * d * r) time (approx; depends on implementation/batching)
  - Transform: O(n * d * r) time
  - Extra working memory: O(batch * d) plus model state (~O(d * r))

HNSW build + query (approximate):
  - Build:  ~O(n * M * log n) time (empirical; depends on ef_construction, data)
  - Query:  ~O(n * ef_search) time (empirical; depends on ef_search, data)
  - Space:  O(n * M) graph links plus per-vector storage inside the index

MST (Prim) over sparse kNN edges:
  - Time:   O((n * k) log n) worst-case due to heap operations
  - Space:  O(n * k) for kNN arrays; MST itself is O(n)

DFS traversal:
  - Time:   O(n)
  - Space:  O(n)

Practical estimates for 100,000 groups (order-of-magnitude)
----------------------------------------------------------
Assumptions:
  - Original dim d = 512, reduced dim r = 128
  - k = 24 neighbours
  - float16 storage for reduced vectors (float32 used transiently for indexing)
  - HNSW parameters around M=16, ef_construction~200, ef_search~64

Vector storage (reduced centroids):
  - float16: n * r * 2 bytes = 100,000 * 128 * 2 = ~25.6 MB
  - float32: n * r * 4 bytes = ~51.2 MB (if kept in RAM)

kNN result arrays:
  - labels int32: n * k * 4  = 100,000 * 24 * 4 = ~9.6 MB
  - dists float32: n * k * 4 = 100,000 * 24 * 4 = ~9.6 MB
  - total kNN arrays: ~19.2 MB

MST adjacency (as Python lists) can be expensive due to object overhead.
If represented as a compact int32 edge list, it is ~2*(n-1)*4 bytes ≈ ~0.8 MB.
If represented as Python list-of-lists, overhead can dominate (tens of MB). Prefer
compact arrays/memmaps in production.

HNSW index memory:
  - Highly implementation- and parameter-dependent. As a rough rule of thumb, expect
    “a few times” the vector storage, often on the order of 100–300+ MB for 100k vectors
    at 128 dims with M around 16. Measure in your environment.

Total RAM envelope (very rough):
  - Best case (compact structures, float16 stored, float32 transient): ~200–500 MB
  - More typical Python-heavy adjacency/object overhead: can exceed that; keep data in
    ndarrays/memmaps to stay predictable.

Time:
  - PCA + HNSW build + full kNN query at 100k is typically seconds to low minutes
    depending on CPU, parameters, and library build (native SIMD helps). The MST and
    traversal are comparatively cheap once kNN is available.

Notes
-----
- For stability across runs and incremental updates, fit PCA once on a representative
  sample and reuse it for new groups.
- If memory is tight, use memmap for reduced vectors and kNN arrays, and avoid Python
  object graphs for adjacency.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Hashable, Mapping, Sequence

import numpy as np


@dataclass(frozen=True)
class OrderConfig:
    # Dimensionality/precision
    target_dim: int = 128
    store_dtype: np.dtype = np.float16

    # PCA (for very large n, IncrementalPCA avoids holding extra copies)
    pca_batch: int = 4096
    pca_seed: int = 0

    # ANN (HNSW)
    k: int = 24
    seed: int = 0
    ef_construction: int = 200
    M: int = 16
    ef_search: int = 64
    knn_batch: int = 2048


def _as_matrix(groups: Mapping[Hashable, Sequence[float]]) -> tuple[list[Hashable], np.ndarray]:
    keys = list(groups.keys())
    if not keys:
        return [], np.empty((0, 0), dtype=np.float32)

    vecs = []
    dim = None
    for k in keys:
        v = np.asarray(groups[k], dtype=np.float32).reshape(-1)
        if dim is None:
            dim = int(v.shape[0])
            if dim == 0:
                raise ValueError('Embeddings must be non-empty vectors.')
        elif v.shape[0] != dim:
            raise ValueError(f'All embeddings must share the same dimension (got {v.shape[0]} vs {dim}).')
        vecs.append(v)

    X = np.vstack(vecs).astype(np.float32, copy=False)
    return keys, X


def _l2_normalise_rows(X: np.ndarray) -> np.ndarray:
    if X.size == 0:
        return X
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-12).astype(np.float32)
    return (X / norms).astype(np.float32, copy=False)


def _reduce_pca_incremental(X: np.ndarray, target_dim: int, batch: int, seed: int) -> np.ndarray:
    """
    Uses IncrementalPCA to reduce to target_dim. Output is float32.
    """
    if target_dim >= X.shape[1]:
        return X

    try:
        from sklearn.decomposition import IncrementalPCA  # type: ignore
    except Exception as e:
        raise RuntimeError('scikit-learn is required for PCA reduction. Install with: pip install scikit-learn') from e

    ipca = IncrementalPCA(n_components=target_dim, batch_size=batch)

    # Fit in batches to reduce peak memory overhead.
    for i0 in range(0, X.shape[0], batch):
        ipca.partial_fit(X[i0 : i0 + batch])

    # Transform in batches too.
    Y = np.empty((X.shape[0], target_dim), dtype=np.float32)
    for i0 in range(0, X.shape[0], batch):
        Y[i0 : i0 + batch] = ipca.transform(X[i0 : i0 + batch]).astype(np.float32, copy=False)

    return Y


def _build_knn_hnsw(X32: np.ndarray, k: int, cfg: OrderConfig) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (labels, distances) arrays of shape (n, k) where distances are cosine distances.
    Requires: pip install hnswlib
    """
    try:
        import hnswlib  # type: ignore
    except Exception as e:
        raise RuntimeError('hnswlib is required at this scale. Install it with: pip install hnswlib') from e

    n, dim = X32.shape
    k = max(1, min(k, n - 1))

    index = hnswlib.Index(space='cosine', dim=dim)
    index.init_index(
        max_elements=n,
        ef_construction=cfg.ef_construction,
        M=cfg.M,
        random_seed=cfg.seed,
    )
    index.add_items(X32, np.arange(n, dtype=np.int32))
    index.set_ef(max(cfg.ef_search, k + 1))

    labels = np.empty((n, k + 1), dtype=np.int32)
    dists = np.empty((n, k + 1), dtype=np.float32)

    for i0 in range(0, n, cfg.knn_batch):
        i1 = min(n, i0 + cfg.knn_batch)
        lab, dist = index.knn_query(X32[i0:i1], k=k + 1)
        labels[i0:i1] = lab
        dists[i0:i1] = dist

    # Drop self neighbour wherever it appears.
    out_labels = np.empty((n, k), dtype=np.int32)
    out_dists = np.empty((n, k), dtype=np.float32)
    for i in range(n):
        li = labels[i]
        di = dists[i]
        mask = li != i
        li2 = li[mask]
        di2 = di[mask]
        if li2.shape[0] < k:
            pad = k - li2.shape[0]
            li2 = np.concatenate([li2, np.repeat(li2[-1], pad)])
            di2 = np.concatenate([di2, np.repeat(di2[-1], pad)])
        out_labels[i] = li2[:k]
        out_dists[i] = di2[:k]

    return out_labels, out_dists


def _mst_from_knn(knn_labels: np.ndarray, knn_dists: np.ndarray) -> list[list[int]]:
    """
    Build an MST using Prim’s algorithm over the implicit symmetrised kNN graph.
    We avoid Python tuple-heavy adjacency lists for the full graph.
    Returns MST adjacency list (neighbour indices only).
    """
    n, k = knn_labels.shape
    in_tree = np.zeros(n, dtype=bool)
    tree: list[list[int]] = [[] for _ in range(n)]

    # For Prim: best known edge to each node not yet in tree.
    best_w = np.full(n, np.inf, dtype=np.float32)
    best_parent = np.full(n, -1, dtype=np.int32)
    heap: list[tuple[float, int]] = []

    def relax_from(u: int) -> None:
        # Consider directed edges u -> v, but treat as usable undirected candidates.
        for t in range(k):
            v = int(knn_labels[u, t])
            w = float(knn_dists[u, t])
            if in_tree[v]:
                continue
            if w < best_w[v]:
                best_w[v] = w
                best_parent[v] = u
                heapq.heappush(heap, (w, v))

    # Handle disconnected cases by restarting.
    for start in range(n):
        if in_tree[start]:
            continue
        in_tree[start] = True
        relax_from(start)

        while heap:
            w, v = heapq.heappop(heap)
            if in_tree[v]:
                continue
            # Ignore stale heap entries.
            if w != float(best_w[v]):
                continue
            p = int(best_parent[v])
            if p >= 0:
                tree[p].append(v)
                tree[v].append(p)
            in_tree[v] = True
            relax_from(v)

    return tree


def _dfs_order(tree: list[list[int]]) -> list[int]:
    n = len(tree)
    if n == 0:
        return []

    visited = np.zeros(n, dtype=bool)
    order: list[int] = []

    # Start on a fringe for a more “walk-in” feel.
    start = min(range(n), key=lambda i: (len(tree[i]), i))

    stack: list[tuple[int, int]] = [(start, -1)]
    while stack:
        u, parent = stack.pop()
        if visited[u]:
            continue
        visited[u] = True
        order.append(u)

        # Visit smaller-degree neighbours first tends to stay on fringes and reduce jumps.
        neigh = [v for v in tree[u] if v != parent and not visited[v]]
        neigh.sort(key=lambda v: (len(tree[v]), v), reverse=True)
        for v in neigh:
            stack.append((v, u))

    # Append any disconnected components
    for i in range(n):
        if not visited[i]:
            stack = [(i, -1)]
            while stack:
                u, parent = stack.pop()
                if visited[u]:
                    continue
                visited[u] = True
                order.append(u)
                neigh = [v for v in tree[u] if v != parent and not visited[v]]
                neigh.sort(key=lambda v: (len(tree[v]), v), reverse=True)
                for v in neigh:
                    stack.append((v, u))

    return order


def order_groups_semantic_flow(
    groups: Mapping[Hashable, Sequence[float]],
    *,
    config: OrderConfig | None = None,
) -> list[Hashable]:
    """
    Input:  {group_key: embedding_vector} (one vector per group, typically centroid)
    Output: [group_key, ...] in a semantically smoother order.

    Scales to 50k+ groups with ANN (hnswlib) and avoids O(n^2).
    """
    cfg = config or OrderConfig()
    keys, X = _as_matrix(groups)
    n = len(keys)
    if n <= 1:
        return keys

    # Normalise then reduce. (Normalise again after PCA since PCA changes lengths.)
    X = _l2_normalise_rows(X)
    X = _reduce_pca_incremental(X, cfg.target_dim, cfg.pca_batch, cfg.pca_seed)
    X = _l2_normalise_rows(X)

    # Store reduced vectors in float16 if desired (useful if you persist them).
    X_store = X.astype(cfg.store_dtype, copy=False)

    # Most ANN libraries prefer float32 for indexing and querying.
    X32 = X_store.astype(np.float32, copy=False)

    k = max(2, min(cfg.k, n - 1))
    knn_labels, knn_dists = _build_knn_hnsw(X32, k, cfg)

    tree = _mst_from_knn(knn_labels, knn_dists)
    idx_order = _dfs_order(tree)

    return [keys[i] for i in idx_order]


# --- Example usage (remove in your integration) ---
if __name__ == '__main__':
    rng = np.random.default_rng(0)
    groups = {f'g{i}': rng.normal(size=(512,)).astype(np.float32) for i in range(5000)}
    order = order_groups_semantic_flow(groups)
    print(order[:10])
