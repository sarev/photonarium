"""
Duplicate detection and group management for the Photonarium application.

This module provides the DuplicateManager class which handles all duplicate
detection across 6 group levels:

- Level 0: Identical (same SHA256 checksum)
- Level 1: Near-identical (perceptual hash within Hamming distance threshold)
- Level 2: Similar (high OpenCLIP embedding cosine similarity)
- Level 3: Related (lower embedding similarity threshold)
- Level 4: Directories (auto-generated from filesystem directory structure)
- Level 5: Custom (user-curated albums)

The module uses several optimisation techniques:
- Multi-index hashing (LSH) for level 1 to avoid O(n²) comparisons
- Chunked matrix multiplication for levels 2-3 to manage memory
- Union-find with path compression for efficient clustering
- Incremental updates for small batches of new/modified images
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any

import numpy as np

from config import Config, get_default_config
from dbutil import sql_placeholders
from safeconn import SafeConnection

logger = logging.getLogger(__name__)

# Semantic constants for group levels (avoids magic numbers throughout)
LEVEL_DIRECTORY = 4  # Auto-generated groups mirroring filesystem directories
LEVEL_CUSTOM = 5  # User-curated albums/groups


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def embedding_to_numpy(embedding_bytes: bytes) -> np.ndarray:
    """Convert stored embedding bytes back to numpy array.

    Args:
        embedding_bytes: Embedding stored as bytes (float32).

    Returns:
        Numpy array of the embedding vector.
    """
    return np.frombuffer(embedding_bytes, dtype=np.float32)


def _compute_unique_dir_names(dir_paths: list[str]) -> dict[str, str]:
    """Compute shortest-unique-suffix display names for a list of directories.

    When multiple directories share the same basename (e.g. .../Holiday/Beach
    and .../Birthday/Beach), parent path components are prepended until every
    name is unique.  Single-occurrence basenames stay as-is.

    Uses os.sep-aware splitting so it works on both Windows (backslash) and
    Unix (forward slash) paths.

    Args:
        dir_paths: List of absolute directory paths.

    Returns:
        Dict mapping each full path to its shortest unique display name.
    """
    if not dir_paths:
        return {}

    # Split each path into its components (reversed for suffix building)
    parts_map: dict[str, list[str]] = {}
    for p in dir_paths:
        # Normalise to platform separator, then split
        normalised = os.path.normpath(p)
        parts_map[p] = normalised.replace('\\', '/').rstrip('/').split('/')

    # Start with just the basename (last component)
    names: dict[str, str] = {}
    for p, parts in parts_map.items():
        names[p] = parts[-1] if parts else p

    # Iteratively resolve collisions by prepending parent components
    max_depth = max(len(parts) for parts in parts_map.values())
    for depth in range(2, max_depth + 1):
        # Find collisions (same display name for different paths)
        name_to_paths: dict[str, list[str]] = {}
        for p, name in names.items():
            name_to_paths.setdefault(name, []).append(p)

        collisions = {name: paths for name, paths in name_to_paths.items() if len(paths) > 1}
        if not collisions:
            break

        # Expand only the colliding entries
        for _name, paths in collisions.items():
            for p in paths:
                parts = parts_map[p]
                # Take up to `depth` trailing components
                suffix_parts = parts[-depth:] if depth <= len(parts) else parts
                names[p] = '/'.join(suffix_parts)

    return names


def rows_to_dicts(rows: list) -> list[dict[str, Any]]:
    """Convert sqlite3.Row objects to plain dictionaries."""
    return [dict(row) for row in rows]


# =============================================================================
# UNION-FIND DATA STRUCTURE
# =============================================================================


class UnionFind:
    """Union-Find (Disjoint Set Union) data structure with path compression and union-by-rank.

    This data structure efficiently manages groups/clusters and supports:
    - Near O(1) amortized time for union and find operations
    - Extraction of final groups

    Can operate in two modes:
    - Index mode: elements are integers 0..n-1 (for array-based algorithms)
    - ID mode: elements are arbitrary hashable IDs (for image_id based operations)

    Example usage (index mode):
        uf = UnionFind(n=100)
        uf.union(0, 1)
        uf.union(1, 2)
        groups = uf.extract_groups()  # {0: [0, 1, 2], ...}

    Example usage (ID mode):
        uf = UnionFind(ids=['img1', 'img2', 'img3'])
        uf.union_ids('img1', 'img2')
        groups = uf.extract_groups_by_id()  # {'img1': ['img1', 'img2'], ...}
    """

    def __init__(self, n: int = 0, ids: list[str] | None = None):
        """Initialise UnionFind.

        Args:
            n: Number of elements (for index mode). Elements are 0..n-1.
            ids: List of IDs (for ID mode). If provided, n is ignored.
        """
        if ids is not None:
            self._ids = list(ids)
            self._id_to_idx = {id_: idx for idx, id_ in enumerate(self._ids)}
            n = len(self._ids)
        else:
            self._ids = None
            self._id_to_idx = None

        self._n = n
        self._parent = list(range(n))
        self._rank = [0] * n

    def find(self, x: int) -> int:
        """Find the root of element x with path compression.

        Uses iterative two-pass path compression to avoid stack overflow
        on large datasets (recursive version could exceed Python's default
        1000-deep recursion limit with chain-structured groups).

        Args:
            x: Element index.

        Returns:
            Root index of the set containing x.
        """
        # Pass 1: walk to root
        root = x
        while self._parent[root] != root:
            root = self._parent[root]

        # Pass 2: compress path (point all nodes directly to root)
        while self._parent[x] != root:
            next_x = self._parent[x]
            self._parent[x] = root
            x = next_x

        return root

    def union(self, x: int, y: int) -> bool:
        """Union the sets containing elements x and y.

        Uses union-by-rank to keep trees balanced.

        Args:
            x: First element index.
            y: Second element index.

        Returns:
            True if the sets were merged, False if already in same set.
        """
        px, py = self.find(x), self.find(y)
        if px == py:
            return False

        # Union by rank: attach smaller tree under larger tree
        if self._rank[px] < self._rank[py]:
            px, py = py, px
        self._parent[py] = px
        if self._rank[px] == self._rank[py]:
            self._rank[px] += 1

        return True

    def union_ids(self, id1: str, id2: str) -> bool:
        """Union the sets containing two IDs (ID mode).

        Args:
            id1: First element ID.
            id2: Second element ID.

        Returns:
            True if the sets were merged, False if already in same set.
        """
        if self._id_to_idx is None:
            raise ValueError('UnionFind not initialised with IDs')
        return self.union(self._id_to_idx[id1], self._id_to_idx[id2])

    def extract_groups(self) -> dict[int, list[int]]:
        """Extract all groups as a dictionary (index mode).

        Returns:
            Dict mapping root index to list of member indices.
            Only includes groups with more than one member.
        """
        groups: dict[int, list[int]] = {}
        for i in range(self._n):
            root = self.find(i)
            if root not in groups:
                groups[root] = []
            groups[root].append(i)
        return groups

    def extract_groups_by_id(self) -> dict[str, list[str]]:
        """Extract all groups as a dictionary (ID mode).

        Returns:
            Dict mapping root ID to list of member IDs.
            Only includes groups with more than one member.
        """
        if self._ids is None:
            raise ValueError('UnionFind not initialised with IDs')

        groups: dict[str, list[str]] = {}
        for i in range(self._n):
            root = self.find(i)
            root_id = self._ids[root]
            if root_id not in groups:
                groups[root_id] = []
            groups[root_id].append(self._ids[i])
        return groups

    @property
    def size(self) -> int:
        """Return the number of elements."""
        return self._n


# =============================================================================
# DATABASE HELPER FUNCTIONS
# =============================================================================


def _get_metadata(conn: SafeConnection, key: str) -> str | None:
    """Get a metadata value by key."""
    cursor = conn.execute('SELECT value FROM metadata WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row['value'] if row else None


def _set_metadata(conn: SafeConnection, key: str, value: str) -> None:
    """Set a metadata value."""
    conn.execute('INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)', (key, value))
    conn.commit()


def _clear_duplicate_groups(conn: SafeConnection, level: int | None = None) -> None:
    """Clear duplicate groups from the database.

    Args:
        conn: Database connection.
        level: If specified, only clear groups at this level.
            If None, clear all groups.
    """
    if level is not None:
        conn.execute('DELETE FROM duplicate_groups WHERE level = ?', (level,))
    else:
        conn.execute('DELETE FROM duplicate_groups')
    conn.commit()


def _insert_duplicate_group(
    conn: SafeConnection,
    level: int,
    group_hash: str,
    image_ids: list[str],
) -> None:
    """Insert a duplicate group into the database.

    Args:
        conn: Database connection.
        level: Duplicate level (0-3).
        group_hash: Unique identifier for this group.
        image_ids: List of image IDs in the group.
    """
    now = datetime.now().isoformat()
    for image_id in image_ids:
        conn.execute(
            'INSERT INTO duplicate_groups (level, group_hash, image_id, updated_at) VALUES (?, ?, ?, ?)',
            (level, group_hash, image_id, now),
        )


def _get_dirty_image_ids(conn: SafeConnection, epoch: str) -> list[str]:
    """Get IDs of images that need duplicate checking.

    An image is "dirty" if it was added or modified after the last
    duplicate computation (i.e., images.updated_at > duplicate_epoch).

    Args:
        conn: Database connection.
        epoch: Last duplicate computation epoch (ISO timestamp).

    Returns:
        List of image IDs that need duplicate checking.
    """
    if not epoch:
        # Never computed - all images need checking
        cursor = conn.execute('SELECT id FROM images WHERE deleted = 0')
        return [row['id'] for row in cursor.fetchall()]

    # Images modified after last duplicate computation
    cursor = conn.execute(
        """
        SELECT id FROM images
        WHERE deleted = 0 AND updated_at > ?
    """,
        (epoch,),
    )
    return [row['id'] for row in cursor.fetchall()]


def _get_group_count(conn: SafeConnection, level: int) -> int:
    """Get the number of duplicate groups at a level."""
    cursor = conn.execute('SELECT COUNT(DISTINCT group_hash) as cnt FROM duplicate_groups WHERE level = ?', (level,))
    return cursor.fetchone()['cnt']


def _get_image_to_group_mapping(conn: SafeConnection, level: int) -> dict[str, str]:
    """Get mapping of image IDs to their group hashes."""
    cursor = conn.execute('SELECT image_id, group_hash FROM duplicate_groups WHERE level = ?', (level,))
    return {row['image_id']: row['group_hash'] for row in cursor.fetchall()}


def _add_image_to_group(
    conn: SafeConnection,
    level: int,
    group_hash: str,
    image_id: str,
) -> None:
    """Add a single image to an existing group."""
    now = datetime.now().isoformat()
    conn.execute(
        'INSERT INTO duplicate_groups (level, group_hash, image_id, updated_at) VALUES (?, ?, ?, ?)',
        (level, group_hash, image_id, now),
    )


def _merge_groups(
    conn: SafeConnection,
    level: int,
    group_hash_keep: str,
    group_hash_merge: str,
) -> None:
    """Merge two groups into one.

    All images in group_hash_merge are moved to group_hash_keep.
    """
    if group_hash_keep == group_hash_merge:
        return
    now = datetime.now().isoformat()
    conn.execute(
        """UPDATE duplicate_groups
           SET group_hash = ?, updated_at = ?
           WHERE level = ? AND group_hash = ?""",
        (group_hash_keep, now, level, group_hash_merge),
    )


# =============================================================================
# LEVEL 0: IDENTICAL DUPLICATES (CHECKSUM)
# =============================================================================


def _compute_duplicates_level0(conn: SafeConnection) -> int:
    """Compute level 0 duplicates (identical checksum).

    Groups images with the same SHA256 checksum.

    Returns:
        Number of duplicate groups found.
    """
    logger.info('Computing level 0 duplicates (identical checksum)')

    _clear_duplicate_groups(conn, level=0)

    cursor = conn.execute("""
        SELECT checksum, GROUP_CONCAT(id) as image_ids
        FROM images
        WHERE deleted = 0 AND checksum IS NOT NULL
        GROUP BY checksum
        HAVING COUNT(*) > 1
    """)

    group_count = 0
    for row in cursor.fetchall():
        checksum = row['checksum']
        image_ids = row['image_ids'].split(',')
        _insert_duplicate_group(conn, level=0, group_hash=checksum, image_ids=image_ids)
        group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 0 duplicate groups')
    return group_count


def _compute_duplicates_level0_incremental(
    conn: SafeConnection,
    dirty_ids: list[str],
) -> int:
    """Incrementally update level 0 duplicates for dirty images."""
    if not dirty_ids:
        return 0

    logger.info(f'Incremental level 0: checking {len(dirty_ids)} images')

    image_to_group = _get_image_to_group_mapping(conn, level=0)

    # Get checksums for dirty images
    placeholders = sql_placeholders(dirty_ids)
    cursor = conn.execute(f'SELECT id, checksum FROM images WHERE id IN ({placeholders}) AND deleted = 0', dirty_ids)
    dirty_checksums = {row['id']: row['checksum'] for row in cursor.fetchall()}

    new_groups = 0

    for dirty_id, checksum in dirty_checksums.items():
        if not checksum:
            continue

        # Find all images with this checksum (excluding the dirty one)
        cursor = conn.execute(
            'SELECT id FROM images WHERE checksum = ? AND deleted = 0 AND id != ?', (checksum, dirty_id)
        )
        matches = [row['id'] for row in cursor.fetchall()]

        if not matches:
            continue

        # Check if any match is already in a group
        existing_groups = set()
        for match_id in matches:
            if match_id in image_to_group:
                existing_groups.add(image_to_group[match_id])

        if existing_groups:
            # Add dirty image to existing group
            target_group = next(iter(existing_groups))
            if dirty_id not in image_to_group:
                _add_image_to_group(conn, level=0, group_hash=target_group, image_id=dirty_id)
                image_to_group[dirty_id] = target_group

            # Merge any other groups
            for other_group in existing_groups:
                if other_group != target_group:
                    _merge_groups(conn, level=0, group_hash_keep=target_group, group_hash_merge=other_group)
                    for img_id, grp in list(image_to_group.items()):
                        if grp == other_group:
                            image_to_group[img_id] = target_group
        else:
            # Create new group
            group_hash = f'chk_{checksum[:16]}'
            all_members = [dirty_id] + matches
            _insert_duplicate_group(conn, level=0, group_hash=group_hash, image_ids=all_members)
            for member in all_members:
                image_to_group[member] = group_hash
            new_groups += 1

    conn.commit()
    logger.info(f'Incremental level 0: created {new_groups} new groups')
    return new_groups


# =============================================================================
# LEVEL 1: NEAR-IDENTICAL DUPLICATES (PERCEPTUAL HASH)
# =============================================================================

# Threshold below which brute-force is faster than building LSH index
_LSH_MIN_IMAGES = 200

# Minimum bucket size to use vectorized comparison (smaller buckets use scalar)
_VECTORIZE_MIN_BUCKET = 8

# Lookup table for popcount of bytes 0-255 (computed once at module load)
_POPCOUNT_TABLE = np.array([bin(i).count('1') for i in range(256)], dtype=np.uint8)


def _popcount_vectorized(arr: np.ndarray) -> np.ndarray:
    """Compute popcount (number of 1 bits) for each element in array.

    Uses byte-wise lookup table for efficiency. Works with uint64 arrays.

    Args:
        arr: Array of unsigned integers.

    Returns:
        Array of same shape with popcount values.
    """
    # View as bytes and sum popcount of each byte
    arr_bytes = arr.view(np.uint8).reshape(arr.shape + (-1,))
    return _POPCOUNT_TABLE[arr_bytes].sum(axis=-1)


def _hamming_distance_fast(hash1: int, hash2: int) -> int:
    """Compute Hamming distance between two integer hashes.

    Uses int.bit_count() which is optimised in CPython 3.10+.
    Falls back to bin().count('1') for compatibility.
    """
    xor = hash1 ^ hash2
    # int.bit_count() is faster in Python 3.10+
    if hasattr(xor, 'bit_count'):
        return xor.bit_count()
    return bin(xor).count('1')


def _find_matches_in_bucket_vectorized(
    bucket_indices: list[int],
    hashes: np.ndarray,
    threshold: int,
) -> list[tuple[int, int]]:
    """Find all matching pairs within a bucket using vectorized operations.

    Computes all pairwise hamming distances in the bucket and returns
    pairs that are within threshold.

    Args:
        bucket_indices: List of global indices of images in this bucket.
        hashes: Array of all hash values (uint64), indexed by global index.
        threshold: Maximum hamming distance for a match.

    Returns:
        List of (idx1, idx2) tuples where idx1 < idx2 and distance <= threshold.
    """
    bucket_size = len(bucket_indices)
    bucket_idx_arr = np.array(bucket_indices, dtype=np.int32)
    bucket_hashes = hashes[bucket_idx_arr]

    # Compute XOR of all pairs using broadcasting: (n, 1) XOR (1, n) -> (n, n)
    xor_matrix = bucket_hashes[:, np.newaxis] ^ bucket_hashes[np.newaxis, :]

    # Compute hamming distances (popcount of XOR)
    distances = _popcount_vectorized(xor_matrix.ravel()).reshape(bucket_size, bucket_size)

    # Find matches in upper triangle (i < j) within threshold
    i_indices, j_indices = np.where(
        (distances <= threshold) & (np.triu(np.ones((bucket_size, bucket_size), dtype=bool), k=1))
    )

    # Convert bucket-local indices to global indices
    matches = [(int(bucket_idx_arr[i]), int(bucket_idx_arr[j])) for i, j in zip(i_indices, j_indices, strict=True)]

    return matches


def _compute_level1_brute_force(
    image_data: list[tuple[str, int]],
    threshold: int,
) -> tuple[UnionFind, int, int]:
    """Brute-force comparison for small datasets using vectorized operations.

    Computes all pairwise hamming distances using numpy broadcasting.
    For small n, this is faster than building an LSH index.

    Returns:
        Tuple of (UnionFind, comparisons, matches).
    """
    n = len(image_data)
    uf = UnionFind(n=n)

    # Extract hashes into numpy array
    hashes = np.array([h for _, h in image_data], dtype=np.uint64)

    # Compute all pairwise XOR using broadcasting
    xor_matrix = hashes[:, np.newaxis] ^ hashes[np.newaxis, :]

    # Compute hamming distances
    distances = _popcount_vectorized(xor_matrix.ravel()).reshape(n, n)

    # Find matches in upper triangle
    i_indices, j_indices = np.where((distances <= threshold) & (np.triu(np.ones((n, n), dtype=bool), k=1)))

    # Union all matches
    matches = len(i_indices)
    for i, j in zip(i_indices, j_indices, strict=True):
        uf.union(int(i), int(j))

    comparisons = n * (n - 1) // 2
    return uf, comparisons, matches


def _compute_level1_lsh(
    image_data: list[tuple[str, int]],
    threshold: int,
) -> tuple[UnionFind, int, int, dict[str, Any]]:
    """Multi-index hashing (LSH) for large datasets.

    Splits 64-bit perceptual hashes into bands. By pigeonhole principle,
    if two hashes differ by at most `threshold` bits and we use
    `threshold + 1` bands, at least one band must be identical.

    This guarantees no false negatives while dramatically reducing
    the number of comparisons needed.

    Band count formula:
    - num_bands = threshold + 1 (pigeonhole guarantee)
    - bits_per_band = 64 // num_bands

    For threshold=4: 5 bands of ~12 bits each
    For threshold=8: 9 bands of ~7 bits each

    Returns:
        Tuple of (UnionFind, comparisons, matches, metrics_dict)
    """
    n = len(image_data)

    # Band configuration based on pigeonhole principle
    num_bands = threshold + 1
    bits_per_band = 64 // num_bands
    # leftover_bits = 64 - (bits_per_band * num_bands)

    # Build inverted index: band_value -> list of image indices
    band_indices: list[dict[int, list[int]]] = [{} for _ in range(num_bands)]

    for idx, (_img_id, hash_int) in enumerate(image_data):
        for band in range(num_bands):
            shift = band * bits_per_band
            if band == num_bands - 1:
                # Last band gets any leftover bits
                band_value = hash_int >> shift
            else:
                mask = (1 << bits_per_band) - 1
                band_value = (hash_int >> shift) & mask

            if band_value not in band_indices[band]:
                band_indices[band][band_value] = []
            band_indices[band][band_value].append(idx)

    # Compute bucket statistics for metrics
    total_buckets = sum(len(band) for band in band_indices)
    non_singleton_buckets = sum(1 for band in band_indices for bucket in band.values() if len(bucket) > 1)
    max_bucket_size = max((len(bucket) for band in band_indices for bucket in band.values()), default=0)

    # Pre-extract hashes into numpy array for vectorized operations
    hashes = np.array([h for _, h in image_data], dtype=np.uint64)

    # Union-find for clustering
    uf = UnionFind(n=n)

    # Track compared pairs to avoid duplicates across bands
    compared: set[tuple[int, int]] = set()
    comparisons = 0
    matches = 0

    for band in range(num_bands):
        for bucket in band_indices[band].values():
            bucket_size = len(bucket)
            if bucket_size < 2:
                continue

            if bucket_size >= _VECTORIZE_MIN_BUCKET:
                # Vectorized comparison for larger buckets
                bucket_matches = _find_matches_in_bucket_vectorized(bucket, hashes, threshold)
                for idx1, idx2 in bucket_matches:
                    # Normalise pair ordering
                    if idx1 > idx2:
                        idx1, idx2 = idx2, idx1
                    pair = (idx1, idx2)
                    if pair not in compared:
                        compared.add(pair)
                        comparisons += 1
                        uf.union(idx1, idx2)
                        matches += 1
            else:
                # Scalar comparison for small buckets (less overhead)
                for i in range(bucket_size):
                    for j in range(i + 1, bucket_size):
                        idx1, idx2 = bucket[i], bucket[j]
                        if idx1 > idx2:
                            idx1, idx2 = idx2, idx1
                        pair = (idx1, idx2)
                        if pair in compared:
                            continue
                        compared.add(pair)

                        dist = _hamming_distance_fast(int(hashes[idx1]), int(hashes[idx2]))
                        comparisons += 1

                        if dist <= threshold:
                            uf.union(idx1, idx2)
                            matches += 1

    metrics = {
        'num_bands': num_bands,
        'bits_per_band': bits_per_band,
        'total_buckets': total_buckets,
        'non_singleton_buckets': non_singleton_buckets,
        'max_bucket_size': max_bucket_size,
        'candidate_pairs': len(compared),
    }

    return uf, comparisons, matches, metrics


def _compute_duplicates_level1(conn: SafeConnection, threshold: int = 4) -> int:
    """Compute level 1 duplicates (perceptual hash similarity).

    Groups images with perceptual hash Hamming distance <= threshold.

    Algorithm selection:
    - For small datasets (< 200 images): brute-force O(n²) comparison
    - For larger datasets: multi-index hashing (LSH) for ~90% reduction

    The LSH approach splits 64-bit hashes into bands. By pigeonhole
    principle, if two hashes differ by at most `threshold` bits and we
    use `threshold + 1` bands, at least one band must be identical.
    This guarantees no false negatives.

    Args:
        conn: Database connection.
        threshold: Maximum Hamming distance for near-identical matches.

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 1 duplicates (perceptual hash, threshold={threshold})')

    _clear_duplicate_groups(conn, level=1)

    cursor = conn.execute("""
        SELECT id, perceptual_hash
        FROM images
        WHERE deleted = 0 AND perceptual_hash IS NOT NULL
    """)
    images = cursor.fetchall()

    if len(images) < 2:
        logger.info('Not enough images for perceptual duplicate detection')
        return 0

    # Convert hashes to integers for fast comparison
    image_data: list[tuple[str, int]] = []
    invalid_hashes = 0
    for row in images:
        try:
            hash_int = int(row['perceptual_hash'], 16)
            image_data.append((row['id'], hash_int))
        except ValueError:
            invalid_hashes += 1
            continue

    if invalid_hashes > 0:
        logger.warning(f'Skipped {invalid_hashes} images with invalid perceptual hashes')

    if len(image_data) < 2:
        return 0

    n = len(image_data)
    brute_force_total = n * (n - 1) // 2

    # Choose algorithm based on dataset size
    if n < _LSH_MIN_IMAGES:
        logger.info(f'Processing {n} images with brute-force (small dataset)')
        uf, comparisons, matches = _compute_level1_brute_force(image_data, threshold)
        logger.info(f'  Completed: {comparisons:,} comparisons, {matches:,} matches')
    else:
        logger.info(f'Processing {n} images with multi-index hashing (LSH)')
        uf, comparisons, matches, metrics = _compute_level1_lsh(image_data, threshold)

        reduction = (1 - comparisons / brute_force_total) * 100 if brute_force_total > 0 else 0
        logger.info(
            f'  LSH config: {metrics["num_bands"]} bands × {metrics["bits_per_band"]} bits, '
            f'{metrics["candidate_pairs"]:,} candidate pairs'
        )
        logger.info(f'  Completed: {comparisons:,} comparisons ({reduction:.1f}% reduction), {matches:,} matches')
        if metrics['max_bucket_size'] > 100:
            logger.debug(
                f'  Bucket stats: {metrics["non_singleton_buckets"]} non-singleton buckets, '
                f'max size {metrics["max_bucket_size"]}'
            )

    # Build groups from union-find
    all_groups = uf.extract_groups()

    # Insert groups with more than one member
    group_count = 0
    for root, members in all_groups.items():
        if len(members) > 1:
            member_ids = [image_data[idx][0] for idx in members]
            group_hash = f'phash_{image_data[root][0]}'
            _insert_duplicate_group(conn, level=1, group_hash=group_hash, image_ids=member_ids)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 1 duplicate groups')
    return group_count


# =============================================================================
# LEVELS 2 & 3: EMBEDDING-BASED DUPLICATES
# =============================================================================


def _normalise_embeddings(embeddings: np.ndarray) -> np.ndarray:
    """Normalise embeddings to unit length for cosine similarity.

    After normalisation, dot product equals cosine similarity.

    Args:
        embeddings: Array of shape (n, dim).

    Returns:
        Normalised array of same shape.
    """
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    # Avoid division by zero for zero vectors
    norms = np.where(norms == 0, 1, norms)
    return embeddings / norms


def _compute_embedding_duplicates_chunked(
    image_ids: list[str],
    embeddings: np.ndarray,
    threshold: float,
    chunk_size: int = 1000,
) -> tuple[dict[int, list[str]], dict[str, Any]]:
    """Compute duplicate groups from embeddings using chunked processing.

    Uses chunked matrix multiplication to avoid O(n²) memory usage.
    Embeddings should already be normalised for cosine similarity.

    Algorithm:
    1. Process embeddings in chunks to limit memory to O(chunk_size * n)
    2. For each chunk, compute similarity matrix against all embeddings
    3. Use vectorized numpy operations to find pairs above threshold
    4. Union matching pairs in UnionFind structure
    5. Extract final groups

    Args:
        image_ids: List of image IDs corresponding to embeddings.
        embeddings: Normalised embedding matrix of shape (n, dim).
        threshold: Minimum cosine similarity for a match.
        chunk_size: Number of embeddings to process at once.

    Returns:
        Tuple of (groups_dict, metrics_dict) where groups_dict maps
        root index to list of image IDs.
    """
    n = len(image_ids)
    dim = embeddings.shape[1] if len(embeddings.shape) > 1 else 0

    # Union-find for clustering
    uf = UnionFind(n=n)

    # Metrics
    pairs_found = 0
    chunks_processed = 0
    total_comparisons = 0

    # Process in chunks to avoid O(n²) memory
    for chunk_start in range(0, n, chunk_size):
        chunk_end = min(chunk_start + chunk_size, n)
        chunk_embeddings = embeddings[chunk_start:chunk_end]
        chunk_len = chunk_end - chunk_start

        # Compute similarities between chunk and all embeddings
        # Shape: (chunk_len, n)
        similarities = chunk_embeddings @ embeddings.T

        # Vectorized: find all pairs above threshold in upper triangle
        # We only want pairs where i_global < j to avoid duplicates
        for i_local in range(chunk_len):
            i_global = chunk_start + i_local
            # Only check j > i_global (upper triangle)
            if i_global + 1 < n:
                # Get similarities for this row, only for j > i_global
                row_sims = similarities[i_local, i_global + 1 :]
                # Find indices where similarity >= threshold
                matches = np.where(row_sims >= threshold)[0]
                # Convert to global indices
                for match_offset in matches:
                    j = i_global + 1 + match_offset
                    uf.union(i_global, j)
                    pairs_found += 1
                total_comparisons += n - i_global - 1

        chunks_processed += 1

    # Build groups from union-find
    all_groups = uf.extract_groups()

    # Convert indices to image IDs
    groups: dict[int, list[str]] = {}
    for root, members in all_groups.items():
        groups[root] = [image_ids[idx] for idx in members]

    metrics = {
        'n_images': n,
        'embedding_dim': dim,
        'chunk_size': chunk_size,
        'chunks_processed': chunks_processed,
        'total_comparisons': total_comparisons,
        'pairs_found': pairs_found,
        'memory_per_chunk_mb': (chunk_size * n * 4) / (1024 * 1024),  # float32
    }

    return groups, metrics


def _load_embeddings_normalised(
    conn: SafeConnection,
) -> tuple[list[str], np.ndarray] | None:
    """Load all image embeddings and normalise them.

    Args:
        conn: Database connection.

    Returns:
        Tuple of (image_ids, normalised_embeddings) or None if < 2 images.
    """
    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if len(rows) < 2:
        return None

    image_ids = [row['id'] for row in rows]
    embeddings = np.array([embedding_to_numpy(row['embedding']) for row in rows])

    # Normalise once for all subsequent operations
    embeddings = _normalise_embeddings(embeddings)

    return image_ids, embeddings


def _compute_duplicates_level2(conn: SafeConnection, threshold: float = 0.95) -> int:
    """Compute level 2 duplicates (similar embeddings).

    Groups images with high cosine similarity (>= threshold).
    Level 2 uses a high threshold (default 0.95) for visually similar images
    like crops, colour adjustments, or shot sequences.

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity (default 0.95).

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 2 duplicates (embedding similarity >= {threshold})')

    _clear_duplicate_groups(conn, level=2)

    result = _load_embeddings_normalised(conn)
    if result is None:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids, embeddings = result
    n = len(image_ids)

    logger.info(f'Processing {n} images ({embeddings.shape[1]}-dim embeddings)')

    groups, metrics = _compute_embedding_duplicates_chunked(image_ids, embeddings, threshold, chunk_size=1000)

    logger.info(
        f'  Chunked processing: {metrics["chunks_processed"]} chunks, '
        f'~{metrics["memory_per_chunk_mb"]:.1f} MB peak per chunk'
    )
    logger.info(f'  Completed: {metrics["pairs_found"]:,} similar pairs found')

    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'emb2_{image_ids[root]}'
            _insert_duplicate_group(conn, level=2, group_hash=group_hash, image_ids=members)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 2 duplicate groups')
    return group_count


def _compute_duplicates_level3(conn: SafeConnection, threshold: float = 0.85) -> int:
    """Compute level 3 duplicates (related embeddings).

    Groups images with moderate cosine similarity (>= threshold).
    Level 3 uses a lower threshold (default 0.85) for thematically related
    images that may not be visually identical.

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity (default 0.85).

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 3 duplicates (embedding similarity >= {threshold})')

    _clear_duplicate_groups(conn, level=3)

    result = _load_embeddings_normalised(conn)
    if result is None:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids, embeddings = result
    n = len(image_ids)

    logger.info(f'Processing {n} images ({embeddings.shape[1]}-dim embeddings)')

    groups, metrics = _compute_embedding_duplicates_chunked(image_ids, embeddings, threshold, chunk_size=1000)

    logger.info(
        f'  Chunked processing: {metrics["chunks_processed"]} chunks, '
        f'~{metrics["memory_per_chunk_mb"]:.1f} MB peak per chunk'
    )
    logger.info(f'  Completed: {metrics["pairs_found"]:,} similar pairs found')

    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'emb3_{image_ids[root]}'
            _insert_duplicate_group(conn, level=3, group_hash=group_hash, image_ids=members)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 3 duplicate groups')
    return group_count


def _compute_duplicates_embedding_incremental(
    conn: SafeConnection,
    dirty_ids: list[str],
    level: int,
    threshold: float,
    chunk_size: int = 5000,
) -> int:
    """Incrementally update embedding-based duplicates for dirty images.

    For each dirty image, finds all images with similarity >= threshold
    and either adds to existing groups or creates new ones.

    Uses chunked database loading to avoid memory explosion with large databases.
    Memory usage is O(chunk_size + dirty_count) instead of O(total_images).

    Args:
        conn: Database connection.
        dirty_ids: List of image IDs that need checking.
        level: Duplicate level (2 or 3).
        threshold: Minimum cosine similarity for a match.
        chunk_size: Number of embeddings to load per chunk (default 5000).

    Returns:
        Number of new groups created.
    """
    if not dirty_ids:
        return 0

    logger.info(f'Incremental level {level}: checking {len(dirty_ids)} images')

    image_to_group = _get_image_to_group_mapping(conn, level=level)

    # Get total count for chunking
    cursor = conn.execute('SELECT COUNT(*) as cnt FROM images WHERE deleted = 0 AND embedding IS NOT NULL')
    total_count = cursor.fetchone()['cnt']

    if total_count < 2:
        return 0

    # Load dirty image embeddings first (these we need to keep in memory)
    dirty_embeddings: dict[str, np.ndarray] = {}

    # Fetch dirty embeddings - these are typically few so OK to load at once
    if dirty_ids:
        placeholders = sql_placeholders(dirty_ids)
        cursor = conn.execute(
            f'SELECT id, embedding FROM images WHERE id IN ({placeholders}) AND deleted = 0 AND embedding IS NOT NULL',
            dirty_ids,
        )
        for row in cursor:
            emb = embedding_to_numpy(row['embedding'])
            # Normalise
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb = emb / norm
            dirty_embeddings[row['id']] = emb

    if not dirty_embeddings:
        logger.info(f'Incremental level {level}: no valid dirty embeddings found')
        return 0

    # Stack dirty embeddings into matrix for vectorized comparison
    dirty_id_list = list(dirty_embeddings.keys())
    dirty_matrix = np.array([dirty_embeddings[id_] for id_ in dirty_id_list])

    # Collect all matches for each dirty image across all chunks
    # matches_by_dirty[dirty_id] = set of matching image IDs
    matches_by_dirty: dict[str, set[str]] = {did: set() for did in dirty_id_list}

    # Process database in chunks to find matches
    chunks_processed = 0
    offset = 0

    while offset < total_count:
        # Load a chunk of embeddings from database
        cursor = conn.execute(
            """SELECT id, embedding FROM images
               WHERE deleted = 0 AND embedding IS NOT NULL
               ORDER BY id
               LIMIT ? OFFSET ?""",
            (chunk_size, offset),
        )
        chunk_rows = cursor.fetchall()

        if not chunk_rows:
            break

        # Build chunk data
        chunk_ids = []
        chunk_embeddings = []
        for row in chunk_rows:
            chunk_ids.append(row['id'])
            emb = embedding_to_numpy(row['embedding'])
            norm = np.linalg.norm(emb)
            if norm > 0:
                emb = emb / norm
            chunk_embeddings.append(emb)

        chunk_matrix = np.array(chunk_embeddings)

        # Compute similarities: dirty_matrix @ chunk_matrix.T
        # Shape: (num_dirty, chunk_size)
        similarities = dirty_matrix @ chunk_matrix.T

        # Find matches above threshold
        for dirty_idx, dirty_id in enumerate(dirty_id_list):
            row_sims = similarities[dirty_idx]
            match_indices = np.where(row_sims >= threshold)[0]

            for match_idx in match_indices:
                match_id = chunk_ids[match_idx]
                # Exclude self-matches
                if match_id != dirty_id:
                    matches_by_dirty[dirty_id].add(match_id)

        # Free chunk memory
        del chunk_matrix, chunk_embeddings, chunk_rows
        chunks_processed += 1
        offset += chunk_size

    logger.info(f'Incremental level {level}: processed {chunks_processed} chunks')

    # Now process matches and update groups
    new_groups = 0

    for dirty_id in dirty_id_list:
        matches = list(matches_by_dirty[dirty_id])

        if not matches:
            continue

        existing_groups = set()
        for match_id in matches:
            if match_id in image_to_group:
                existing_groups.add(image_to_group[match_id])

        if existing_groups:
            target_group = next(iter(existing_groups))
            if dirty_id not in image_to_group:
                _add_image_to_group(conn, level=level, group_hash=target_group, image_id=dirty_id)
                image_to_group[dirty_id] = target_group

            for other_group in existing_groups:
                if other_group != target_group:
                    _merge_groups(conn, level=level, group_hash_keep=target_group, group_hash_merge=other_group)
                    for img_id, grp in list(image_to_group.items()):
                        if grp == other_group:
                            image_to_group[img_id] = target_group
        else:
            group_hash = f'emb{level}_{dirty_id}'
            all_members = [dirty_id] + matches
            _insert_duplicate_group(conn, level=level, group_hash=group_hash, image_ids=all_members)
            for member in all_members:
                image_to_group[member] = group_hash
            new_groups += 1

    conn.commit()
    logger.info(f'Incremental level {level}: created {new_groups} new groups')
    return new_groups


# =============================================================================
# GROUP RETRIEVAL FUNCTIONS
# =============================================================================


def _get_duplicate_epoch(conn: SafeConnection) -> str:
    """Get the current epoch timestamp for duplicate groups."""
    epoch = _get_metadata(conn, 'duplicate_epoch')
    return epoch if epoch else ''


def _set_duplicate_epoch(conn: SafeConnection, epoch: str) -> None:
    """Set the duplicate computation epoch."""
    _set_metadata(conn, 'duplicate_epoch', epoch)


# =============================================================================
# SIMILARITY SEARCH
# =============================================================================


def _get_images_by_similarity(
    conn: SafeConnection,
    reference_embedding: np.ndarray,
) -> list[dict[str, Any]]:
    """Get all images sorted by similarity to a reference embedding.

    Uses vectorized numpy operations for performance.
    """
    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)

    rows = cursor.fetchall()
    if not rows:
        return []

    ids = [row['id'] for row in rows]
    embeddings = [np.frombuffer(row['embedding'], dtype=np.float32) for row in rows]

    # Process in chunks to avoid a single large allocation on low-memory systems.
    # Each embedding is ~2KB (512 × float32), so 10k embeddings ≈ 20MB per chunk.
    chunk_size = 10000
    similarity_map: dict[str, float] = {}
    for start in range(0, len(embeddings), chunk_size):
        chunk_ids = ids[start : start + chunk_size]
        chunk_matrix = np.vstack(embeddings[start : start + chunk_size])
        chunk_sims = chunk_matrix @ reference_embedding
        for i, cid in enumerate(chunk_ids):
            similarity_map[cid] = float(chunk_sims[i])

    cursor = conn.execute("""
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)

    results = []
    for row in cursor.fetchall():
        image_dict = dict(row)
        image_dict['similarity'] = similarity_map.get(image_dict['id'], 0.0)
        results.append(image_dict)

    results.sort(key=lambda x: x['similarity'], reverse=True)
    return results


# =============================================================================
# DUPLICATE MANAGER CLASS
# =============================================================================


class DuplicateManager:
    """Manages duplicate detection and grouping for images.

    This class encapsulates all duplicate-related functionality:
    - Computing duplicates at all 4 auto-detected levels (0-3)
    - Directory groups (level 4) and custom groups (level 5)
    - Incremental updates for small batches
    - Status tracking per level
    - Group retrieval with in-memory caching

    Thread-safe: uses locks for status tracking, cache access, and database operations.

    Cache Structure:
        _group_cache[level][group_hash] = set of image_ids
        _image_to_group[level][image_id] = group_hash

    The cache is loaded lazily on first access and invalidated when:
    - Images are deleted or modified (call invalidate_image())
    - Duplicates are recomputed (automatic)
    """

    def __init__(self, db_path: str, config: Config | None = None):
        """Initialise the DuplicateManager.

        Args:
            db_path: Path to the SQLite database.
            config: Configuration object. Uses defaults if None.
        """
        self._db_path = db_path
        self._config = config or get_default_config()
        self._status_lock = threading.Lock()

        # If a duplicate epoch exists, groups were computed in a previous session
        # and are already stored in the DB — start as 'done' so the frontend
        # doesn't show "Waiting to compute…" for levels that are already complete.
        conn = self._get_db()
        try:
            initial = 'done' if _get_duplicate_epoch(conn) else 'pending'
        finally:
            conn.close()
        self._status: dict[int, str] = {0: initial, 1: initial, 2: initial, 3: initial}

        # In-memory group cache (lazy loaded)
        self._cache_lock = threading.Lock()
        self._group_cache: dict[int, dict[str, set[str]]] | None = None  # level -> group_hash -> image_ids
        self._image_to_group: dict[int, dict[str, str]] | None = None  # level -> image_id -> group_hash
        self._cache_loaded = False

    def _get_db(self) -> SafeConnection:
        """Get a private database connection wrapped in SafeConnection.

        Each call opens a fresh connection with its own RLock.  The
        SafeConnection wrapper provides retry on transient "database is
        locked" errors and auto-rollback on failure — matching the
        safety guarantees of the shared ``safe_conn`` used elsewhere.
        """
        raw = sqlite3.connect(self._db_path)
        raw.execute('PRAGMA busy_timeout=5000')
        raw.row_factory = sqlite3.Row
        return SafeConnection(raw, name='dup-manager')

    # =========================================================================
    # Group Cache
    # =========================================================================

    def _ensure_cache_loaded(self) -> None:
        """Load the group cache if not already loaded.

        Thread-safe: uses _cache_lock to prevent concurrent loading.
        """
        if self._cache_loaded:
            return

        with self._cache_lock:
            # Double-check after acquiring lock
            if self._cache_loaded:
                return

            logger.debug('Loading duplicate group cache from database')
            self._group_cache = {0: {}, 1: {}, 2: {}, 3: {}, LEVEL_DIRECTORY: {}, LEVEL_CUSTOM: {}}
            self._image_to_group = {0: {}, 1: {}, 2: {}, 3: {}, LEVEL_DIRECTORY: {}, LEVEL_CUSTOM: {}}

            conn = self._get_db()
            try:
                # Load auto-detected duplicate groups (levels 0-3)
                for level in range(4):
                    cursor = conn.execute(
                        """
                        SELECT dg.group_hash, dg.image_id
                        FROM duplicate_groups dg
                        JOIN images i ON i.id = dg.image_id
                        WHERE dg.level = ? AND i.deleted = 0
                    """,
                        (level,),
                    )

                    for row in cursor.fetchall():
                        group_hash = row['group_hash']
                        image_id = row['image_id']

                        if group_hash not in self._group_cache[level]:
                            self._group_cache[level][group_hash] = set()
                        self._group_cache[level][group_hash].add(image_id)
                        self._image_to_group[level][image_id] = group_hash

                # Load named groups (levels 4 and 5) — images can belong to multiple groups
                for named_level in (LEVEL_DIRECTORY, LEVEL_CUSTOM):
                    cursor = conn.execute(
                        """
                        SELECT dg.group_hash, dg.image_id
                        FROM duplicate_groups dg
                        JOIN images i ON i.id = dg.image_id
                        WHERE dg.level = ? AND i.deleted = 0
                    """,
                        (named_level,),
                    )
                    for row in cursor.fetchall():
                        group_hash = row['group_hash']
                        image_id = row['image_id']
                        if group_hash not in self._group_cache[named_level]:
                            self._group_cache[named_level][group_hash] = set()
                        self._group_cache[named_level][group_hash].add(image_id)
                        # Named levels allow overlap: _image_to_group is not used
                        # (an image can be in multiple directory/custom groups)

                # Also load empty custom groups (they persist when empty)
                # source_path IS NULL = custom groups (level 5)
                # source_path IS NOT NULL = directory groups (level 4)
                cursor = conn.execute(
                    """
                    SELECT cg.group_hash,
                           CASE WHEN cg.source_path IS NOT NULL THEN ? ELSE ? END AS level
                    FROM custom_groups cg
                    WHERE cg.group_hash NOT IN (
                        SELECT DISTINCT dg.group_hash
                        FROM duplicate_groups dg
                        WHERE dg.level IN (?, ?)
                    )
                """,
                    (LEVEL_DIRECTORY, LEVEL_CUSTOM, LEVEL_DIRECTORY, LEVEL_CUSTOM),
                )
                for row in cursor.fetchall():
                    self._group_cache[row['level']][row['group_hash']] = set()

                total_groups = sum(len(groups) for groups in self._group_cache.values())
                total_images = sum(len(imgs) for imgs in self._image_to_group.values())
                logger.debug(f'Loaded {total_groups} groups with {total_images} image mappings')

            finally:
                conn.close()

            self._cache_loaded = True

    def _invalidate_cache(self) -> None:
        """Invalidate the entire cache, forcing reload on next access.

        Called after duplicate computation completes.
        """
        with self._cache_lock:
            self._group_cache = None
            self._image_to_group = None
            self._cache_loaded = False
            logger.debug('Duplicate group cache invalidated')

    def invalidate_image(self, image_id: str, update_db: bool = True) -> None:
        """Remove an image from duplicate groups when it's deleted or modified.

        This removes the image from its group at all levels in both the cache
        and the database. If a group becomes a singleton (only one image),
        the group is dissolved since it's no longer a "duplicate" group.

        Args:
            image_id: ID of the image to remove.
            update_db: If True, also update the database. Set to False if
                the image is already being deleted from the database.
        """
        # Update database first
        if update_db:
            self._remove_image_from_db_groups(image_id)

        # Update cache if loaded
        if not self._cache_loaded:
            return

        with self._cache_lock:
            if not self._cache_loaded:
                return

            for level in range(4):
                if image_id in self._image_to_group[level]:
                    group_hash = self._image_to_group[level].pop(image_id)

                    if group_hash in self._group_cache[level]:
                        self._group_cache[level][group_hash].discard(image_id)

                        # Dissolve singleton groups (they're no longer duplicates)
                        if len(self._group_cache[level][group_hash]) <= 1:
                            # Remove remaining image from reverse index
                            for remaining_id in self._group_cache[level][group_hash]:
                                self._image_to_group[level].pop(remaining_id, None)
                            del self._group_cache[level][group_hash]

            # Named groups (levels 4-5): remove image but never dissolve empty groups
            for named_level in (LEVEL_DIRECTORY, LEVEL_CUSTOM):
                for _group_hash, members in list(self._group_cache.get(named_level, {}).items()):
                    members.discard(image_id)

    def _remove_image_from_db_groups(self, image_id: str) -> None:
        """Remove an image from all duplicate groups in the database.

        Also cleans up singleton groups that result from the removal.

        Args:
            image_id: ID of the image to remove.
        """
        conn = self._get_db()
        try:
            # Get all groups this image belongs to (before removing)
            cursor = conn.execute('SELECT level, group_hash FROM duplicate_groups WHERE image_id = ?', (image_id,))
            affected_groups = [(row['level'], row['group_hash']) for row in cursor.fetchall()]

            if not affected_groups:
                return

            # Remove the image from all groups
            conn.execute('DELETE FROM duplicate_groups WHERE image_id = ?', (image_id,))

            # Check each affected group for singleton status
            for level, group_hash in affected_groups:
                # Named groups (levels 4-5) persist even when empty — skip dissolution
                if level >= LEVEL_DIRECTORY:
                    continue

                cursor = conn.execute(
                    'SELECT COUNT(*) as cnt FROM duplicate_groups WHERE level = ? AND group_hash = ?',
                    (level, group_hash),
                )
                count = cursor.fetchone()['cnt']

                # If only 1 member left, dissolve the group (no longer a duplicate)
                if count <= 1:
                    conn.execute('DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ?', (level, group_hash))
                    logger.debug(f'Dissolved singleton group {group_hash} at level {level}')

            conn.commit()
            logger.debug(f'Removed image {image_id} from {len(affected_groups)} duplicate groups')

        finally:
            conn.close()

    def invalidate_images(self, image_ids: list[str]) -> tuple[int, set[int]]:
        """Remove multiple images from duplicate groups (batch operation).

        More efficient than calling invalidate_image() repeatedly for bulk
        deletions. Updates both the database and cache.

        Args:
            image_ids: List of image IDs to remove.

        Returns:
            Tuple of (affected_count, affected_levels) where affected_count
            is the number of images that were in at least one group, and
            affected_levels is the set of group levels that were modified.
        """
        if not image_ids:
            return 0, set()

        conn = self._get_db()
        affected_count = 0

        try:
            # Get all affected groups before removing
            placeholders = sql_placeholders(image_ids)
            cursor = conn.execute(
                f'SELECT DISTINCT level, group_hash FROM duplicate_groups WHERE image_id IN ({placeholders})', image_ids
            )
            affected_groups = [(row['level'], row['group_hash']) for row in cursor.fetchall()]

            if not affected_groups:
                return 0, set()

            # Count how many images were actually in groups
            cursor = conn.execute(
                f'SELECT COUNT(DISTINCT image_id) as cnt FROM duplicate_groups WHERE image_id IN ({placeholders})',
                image_ids,
            )
            affected_count = cursor.fetchone()['cnt']

            # Remove all images from groups in one query
            conn.execute(f'DELETE FROM duplicate_groups WHERE image_id IN ({placeholders})', image_ids)

            # Check each affected group for singleton status
            dissolved_count = 0
            for level, group_hash in affected_groups:
                # Named groups (levels 4-5) persist even when empty — skip dissolution
                if level >= LEVEL_DIRECTORY:
                    continue

                cursor = conn.execute(
                    'SELECT COUNT(*) as cnt FROM duplicate_groups WHERE level = ? AND group_hash = ?',
                    (level, group_hash),
                )
                count = cursor.fetchone()['cnt']

                if count <= 1:
                    conn.execute('DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ?', (level, group_hash))
                    dissolved_count += 1

            conn.commit()

            if dissolved_count > 0:
                logger.debug(f'Dissolved {dissolved_count} singleton groups')
            logger.info(f'Removed {affected_count} images from duplicate groups')

        finally:
            conn.close()

        # Collect distinct levels that were affected (for event notification)
        affected_levels = {level for level, _gh in affected_groups}

        # Update cache if loaded
        def remove_from_cache(img_id: str) -> None:
            """Remove a single image from cache, dissolving singleton groups."""
            for level in range(4):
                if img_id not in self._image_to_group[level]:
                    continue
                group_hash = self._image_to_group[level].pop(img_id)
                if group_hash not in self._group_cache[level]:
                    continue
                self._group_cache[level][group_hash].discard(img_id)
                # Dissolve singleton groups
                if len(self._group_cache[level][group_hash]) <= 1:
                    for remaining_id in self._group_cache[level][group_hash]:
                        self._image_to_group[level].pop(remaining_id, None)
                    del self._group_cache[level][group_hash]
            # Named groups (levels 4-5): remove image but never dissolve
            for named_level in (LEVEL_DIRECTORY, LEVEL_CUSTOM):
                for _group_hash, members in list(self._group_cache.get(named_level, {}).items()):
                    members.discard(img_id)

        if self._cache_loaded:
            with self._cache_lock:
                if self._cache_loaded:
                    for image_id in image_ids:
                        remove_from_cache(image_id)

        return affected_count, affected_levels

    # =========================================================================
    # Status
    # =========================================================================

    def get_status(self) -> dict[int, str]:
        """Get the computation status for each duplicate level.

        Returns:
            Dict mapping level (0-3) to status string:
            - 'pending': Not yet computed
            - 'computing': Currently being computed
            - 'done': Computation finished
        """
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, level: int, status: str) -> None:
        """Set status for a level."""
        with self._status_lock:
            self._status[level] = status

    def _set_all_status(self, status: str) -> None:
        """Set status for all levels."""
        with self._status_lock:
            for level in range(4):
                self._status[level] = status

    # =========================================================================
    # Group Retrieval
    # =========================================================================

    def get_group_images_ranked(
        self,
        level: int,
        group_hash: str | None = None,
        min_size: int = 2,
    ) -> list[dict[str, Any]]:
        """Get groups with quality-scoring fields for each image.

        Returns groups with the image metadata needed by
        :func:`trash.compute_quality_scores` to rank images by quality.
        Images are returned **unranked** — callers apply the scoring
        algorithm themselves.

        Args:
            level: Similarity level (0-5).
            group_hash: If specified, return just this one group.
                If None, return all groups at the level.
            min_size: Minimum number of non-deleted images for a group
                to be included.  Default 2 (standard for pruning).
                Pass 1 to include single-image groups (for preview).

        Returns:
            List of dicts, each with ``group_hash`` and ``images`` (list
            of dicts with ``id``, ``aesthetic_laion``, ``aesthetic_nima``,
            ``laplacian_var``, ``width``, ``height``, ``size``).
            Only groups with ``min_size``+ non-deleted images are included.
        """
        conn = self._get_db()
        try:
            if group_hash:
                hashes = [group_hash]
            else:
                cursor = conn.execute('SELECT DISTINCT group_hash FROM duplicate_groups WHERE level = ?', (level,))
                hashes = [row['group_hash'] for row in cursor.fetchall()]

            groups = []
            for gh in hashes:
                cursor = conn.execute(
                    """
                    SELECT i.id, i.aesthetic_laion, i.aesthetic_nima,
                           i.laplacian_var, i.width, i.height, i.size
                    FROM images i
                    JOIN duplicate_groups dg ON i.id = dg.image_id
                    WHERE dg.level = ? AND dg.group_hash = ? AND i.deleted = 0
                """,
                    (level, gh),
                )
                images = [dict(row) for row in cursor.fetchall()]

                if len(images) >= min_size:
                    groups.append(
                        {
                            'group_hash': gh,
                            'images': images,
                        }
                    )

            return groups
        finally:
            conn.close()

    def get_explicit_groups_ranked(
        self,
        explicit_groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Fetch quality-scoring fields for groups with explicit image IDs.

        Used by the preview endpoint for smart groups whose membership is
        resolved on the frontend (they have no entries in
        ``duplicate_groups``).  Returns the same format as
        :meth:`get_group_images_ranked` so callers can merge the results.

        Args:
            explicit_groups: List of dicts, each with ``group_hash`` (str)
                and ``image_ids`` (list of image ID strings).

        Returns:
            List of dicts with ``group_hash`` and ``images`` (list of
            dicts with quality-scoring columns).  Empty groups and groups
            where all images are deleted are excluded.
        """
        if not explicit_groups:
            return []

        conn = self._get_db()
        try:
            groups = []
            for eg in explicit_groups:
                image_ids = eg.get('image_ids', [])
                if not image_ids:
                    continue
                placeholders = sql_placeholders(image_ids)
                cursor = conn.execute(
                    f"""
                    SELECT id, aesthetic_laion, aesthetic_nima,
                           laplacian_var, width, height, size
                    FROM images
                    WHERE id IN ({placeholders}) AND deleted = 0
                    """,
                    image_ids,
                )
                images = [dict(row) for row in cursor.fetchall()]
                if images:
                    groups.append(
                        {
                            'group_hash': eg.get('group_hash', ''),
                            'images': images,
                        }
                    )
            return groups
        finally:
            conn.close()

    def get_groups_lightweight(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups with minimal data for efficient display.

        Uses the in-memory cache for image_ids to avoid per-group DB queries.
        Still queries DB for best_image selection (requires sorting by metadata).

        Level 4 (directory groups) and level 5 (custom groups) include the
        group name and allow empty groups.
        """
        self._ensure_cache_loaded()

        # Level 4: directory groups — sorted by source_path, includes source_path
        if level == LEVEL_DIRECTORY:
            return self._get_directory_groups_lightweight()

        # Level 5: custom groups — different query that includes name and allows empty groups
        if level == LEVEL_CUSTOM:
            return self._get_custom_groups_lightweight()

        conn = self._get_db()
        try:
            # Query for best image per group (still needs DB for sorting)
            cursor = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        dg.group_hash,
                        i.id,
                        i.basename,
                        ROW_NUMBER() OVER (
                            PARTITION BY dg.group_hash
                            ORDER BY
                                i.aesthetic_laion DESC,
                                i.laplacian_var DESC,
                                i.id ASC
                        ) as rank
                    FROM duplicate_groups dg
                    JOIN images i ON i.id = dg.image_id
                    WHERE dg.level = ? AND i.deleted = 0
                ),
                group_counts AS (
                    SELECT group_hash, COUNT(*) as cnt
                    FROM ranked
                    GROUP BY group_hash
                    HAVING cnt > 1
                )
                SELECT
                    r.group_hash,
                    gc.cnt as count,
                    r.id as best_id,
                    r.basename as best_basename
                FROM ranked r
                JOIN group_counts gc ON r.group_hash = gc.group_hash
                WHERE r.rank = 1
                ORDER BY gc.cnt DESC
            """,
                (level,),
            )

            groups = []
            with self._cache_lock:
                for row in cursor.fetchall():
                    group_hash = row['group_hash']

                    # Get image_ids from cache instead of DB query
                    image_ids = list(self._group_cache[level].get(group_hash, set()))

                    groups.append(
                        {
                            'group_hash': group_hash,
                            'count': row['count'],
                            'image_ids': image_ids,
                            'best_image': {
                                'id': row['best_id'],
                                'basename': row['best_basename'],
                            },
                        }
                    )

            return groups
        finally:
            conn.close()

    def _get_custom_groups_lightweight(self) -> list[dict[str, Any]]:
        """Get custom groups (level 5) with names, including empty groups.

        Custom groups differ from auto-detected levels:
        - They have a user-assigned name (from custom_groups table)
        - Empty groups are preserved (not dissolved)
        - Sorted alphabetically by name by default

        Returns:
            List of group dicts with group_hash, name, count, image_ids, best_image.
        """
        conn = self._get_db()
        try:
            # Get all custom groups (source_path IS NULL) with their names,
            # optional filter_json (non-NULL for smart groups), preview, and damage flag
            cursor = conn.execute("""
                SELECT cg.group_hash, cg.name, cg.filter_json, cg.preview_image_id, cg.damaged
                FROM custom_groups cg
                WHERE cg.source_path IS NULL
                ORDER BY cg.name COLLATE NOCASE ASC
            """)
            custom_group_rows = cursor.fetchall()

            if not custom_group_rows:
                return []

            # Get best image per non-empty regular group (from duplicate_groups membership)
            best_images = {}
            cursor = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        dg.group_hash,
                        i.id,
                        i.basename,
                        ROW_NUMBER() OVER (
                            PARTITION BY dg.group_hash
                            ORDER BY
                                i.aesthetic_laion DESC,
                                i.laplacian_var DESC,
                                i.id ASC
                        ) as rank
                    FROM duplicate_groups dg
                    JOIN images i ON i.id = dg.image_id
                    WHERE dg.level = ? AND i.deleted = 0
                )
                SELECT group_hash, id, basename
                FROM ranked
                WHERE rank = 1
            """,
                (LEVEL_CUSTOM,),
            )
            for row in cursor.fetchall():
                best_images[row['group_hash']] = {
                    'id': row['id'],
                    'basename': row['basename'],
                }

            # Resolve smart group preview images (stored as preview_image_id)
            preview_ids = [row['preview_image_id'] for row in custom_group_rows if row['preview_image_id']]
            preview_images: dict[str, dict] = {}
            if preview_ids:
                placeholders = sql_placeholders(preview_ids)
                cursor = conn.execute(
                    f'SELECT id, basename FROM images WHERE id IN ({placeholders}) AND deleted = 0',
                    preview_ids,
                )
                for row in cursor.fetchall():
                    preview_images[row['id']] = {
                        'id': row['id'],
                        'basename': row['basename'],
                    }

            groups = []
            with self._cache_lock:
                for row in custom_group_rows:
                    group_hash = row['group_hash']
                    image_ids = list(self._group_cache[LEVEL_CUSTOM].get(group_hash, set()))
                    filter_json = row['filter_json']

                    # Smart groups use preview_image_id; regular groups use ranked best
                    if filter_json is not None:
                        preview_id = row['preview_image_id']
                        best_image = preview_images.get(preview_id) if preview_id else None
                    else:
                        best_image = best_images.get(group_hash)

                    group_dict = {
                        'group_hash': group_hash,
                        'name': row['name'],
                        'count': len(image_ids),
                        'image_ids': image_ids,
                        'best_image': best_image,
                    }
                    # Include filter_json and damage flag only for smart groups (saves bandwidth)
                    if filter_json is not None:
                        group_dict['filter_json'] = filter_json
                        if row['damaged']:
                            group_dict['damaged'] = True
                    groups.append(group_dict)

            return groups
        finally:
            conn.close()

    def _get_directory_groups_lightweight(self) -> list[dict[str, Any]]:
        """Get directory groups (level 4) with names and source paths.

        Directory groups mirror filesystem directories. They are sorted by
        source_path for consistent alphabetical ordering by full path.

        Returns:
            List of group dicts with group_hash, name, source_path, count,
            image_ids, best_image.
        """
        conn = self._get_db()
        try:
            # Get all directory groups (source_path IS NOT NULL)
            cursor = conn.execute("""
                SELECT cg.group_hash, cg.name, cg.source_path
                FROM custom_groups cg
                WHERE cg.source_path IS NOT NULL
                ORDER BY cg.source_path COLLATE NOCASE ASC
            """)
            dir_group_rows = cursor.fetchall()

            if not dir_group_rows:
                return []

            # Get best image per non-empty group
            best_images = {}
            cursor = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        dg.group_hash,
                        i.id,
                        i.basename,
                        ROW_NUMBER() OVER (
                            PARTITION BY dg.group_hash
                            ORDER BY
                                i.aesthetic_laion DESC,
                                i.laplacian_var DESC,
                                i.id ASC
                        ) as rank
                    FROM duplicate_groups dg
                    JOIN images i ON i.id = dg.image_id
                    WHERE dg.level = ? AND i.deleted = 0
                )
                SELECT group_hash, id, basename
                FROM ranked
                WHERE rank = 1
            """,
                (LEVEL_DIRECTORY,),
            )
            for row in cursor.fetchall():
                best_images[row['group_hash']] = {
                    'id': row['id'],
                    'basename': row['basename'],
                }

            groups = []
            with self._cache_lock:
                for row in dir_group_rows:
                    group_hash = row['group_hash']
                    image_ids = list(self._group_cache[LEVEL_DIRECTORY].get(group_hash, set()))

                    groups.append(
                        {
                            'group_hash': group_hash,
                            'name': row['name'],
                            'source_path': row['source_path'],
                            'count': len(image_ids),
                            'image_ids': image_ids,
                            'best_image': best_images.get(group_hash),
                        }
                    )

            return groups
        finally:
            conn.close()

    # =========================================================================
    # Directory Group Sync (Level 4)
    # =========================================================================

    def sync_directory_groups(
        self,
        conn: SafeConnection,
    ) -> None:
        """Synchronise directory groups with current image data.

        Creates, updates, and removes directory groups to mirror the filesystem
        structure of non-deleted images. Each unique parent directory of at least
        one image becomes a group.

        Group names use shortest-unique-suffix disambiguation: if two directories
        share the same basename (e.g. /Photos/Holiday/Beach and /Photos/Birthday/Beach),
        parent path components are prepended until names are unique.

        Uses READ → COMPUTE → WRITE pattern to minimise lock hold time.
        The SafeConnection's context manager serialises DB access.

        Called at the end of processing (on_final_complete) and after folder removal.

        Args:
            conn: Shared SafeConnection (provides its own locking).
        """
        now = datetime.now().isoformat()

        # ── READ phase (lock): gather current state from DB ──
        with conn:
            cursor = conn.execute('SELECT id, path FROM images WHERE deleted = 0')
            rows = cursor.fetchall()

            cursor = conn.execute('SELECT group_hash, source_path FROM custom_groups WHERE source_path IS NOT NULL')
            existing = {row['source_path']: row['group_hash'] for row in cursor.fetchall()}

        # ── COMPUTE phase (no lock): prepare all SQL parameters ──

        # Map directory → set of image IDs
        dir_to_images: dict[str, set[str]] = {}
        for row in rows:
            parent = os.path.dirname(row['path'])
            if parent not in dir_to_images:
                dir_to_images[parent] = set()
            dir_to_images[parent].add(row['id'])

        all_dirs = list(dir_to_images.keys())
        display_names = _compute_unique_dir_names(all_dirs)
        needed_paths = set(all_dirs)

        # Prepare batched SQL parameters
        delete_membership_params: list[tuple] = []  # (level, group_hash)
        insert_membership_params: list[tuple] = []  # (level, group_hash, image_id, now)
        update_name_params: list[tuple] = []  # (name, now, group_hash)
        create_group_params: list[tuple] = []  # (group_hash, name, source_path, now, now)
        remove_dup_params: list[tuple] = []  # (level, group_hash)
        remove_group_hashes: list[tuple] = []  # (group_hash,)

        # Track newly created group hashes for cache rebuild
        new_groups: dict[str, str] = {}  # dir_path → group_hash

        created = 0
        updated = 0
        removed = 0

        for dir_path in all_dirs:
            image_ids = dir_to_images[dir_path]
            display_name = display_names[dir_path]

            if dir_path in existing:
                # Group exists — sync membership and name
                group_hash = existing[dir_path]
                delete_membership_params.append((LEVEL_DIRECTORY, group_hash))
                for image_id in image_ids:
                    insert_membership_params.append((LEVEL_DIRECTORY, group_hash, image_id, now))
                update_name_params.append((display_name, now, group_hash))
                updated += 1
            else:
                # New directory — create group
                group_hash = str(uuid.uuid4())
                new_groups[dir_path] = group_hash
                create_group_params.append((group_hash, display_name, dir_path, now, now))
                for image_id in image_ids:
                    insert_membership_params.append((LEVEL_DIRECTORY, group_hash, image_id, now))
                created += 1

        # Stale groups whose source_path no longer has images
        for source_path, group_hash in existing.items():
            if source_path not in needed_paths:
                remove_dup_params.append((LEVEL_DIRECTORY, group_hash))
                remove_group_hashes.append((group_hash,))
                removed += 1

        # ── WRITE phase (lock): execute all batched SQL in one transaction ──
        with conn:
            conn.executemany(
                'DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ?',
                delete_membership_params,
            )
            conn.executemany(
                'INSERT INTO custom_groups (group_hash, name, source_path, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?)',
                create_group_params,
            )
            conn.executemany(
                'INSERT INTO duplicate_groups (level, group_hash, image_id, updated_at) VALUES (?, ?, ?, ?)',
                insert_membership_params,
            )
            conn.executemany(
                'UPDATE custom_groups SET name = ?, updated_at = ? WHERE group_hash = ?',
                update_name_params,
            )
            conn.executemany(
                'DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ?',
                remove_dup_params,
            )
            conn.executemany(
                'DELETE FROM custom_groups WHERE group_hash = ?',
                remove_group_hashes,
            )
            conn.commit()

        # Rebuild level-4 cache from the data we just wrote (no DB query needed)
        with self._cache_lock:
            if self._cache_loaded and self._group_cache is not None:
                self._group_cache[LEVEL_DIRECTORY] = {}

                for dir_path in all_dirs:
                    image_ids = dir_to_images[dir_path]
                    group_hash = existing.get(dir_path) or new_groups.get(dir_path)
                    if group_hash:
                        self._group_cache[LEVEL_DIRECTORY][group_hash] = set(image_ids)

        if created or updated or removed:
            logger.info(
                f'Directory groups synced: {created} created, {updated} updated, {removed} removed '
                f'({len(all_dirs)} directories total)'
            )

    def get_epoch(self) -> str:
        """Get the current epoch timestamp for duplicate groups."""
        conn = self._get_db()
        try:
            return _get_duplicate_epoch(conn)
        finally:
            conn.close()

    # =========================================================================
    # Computation
    # =========================================================================

    def compute_all(
        self,
        conn: SafeConnection | None = None,
        force_full: bool = False,
    ) -> dict[int, int]:
        """Compute all duplicate groups, using incremental updates when possible.

        Uses incremental computation when the number of dirty images is below
        the configured thresholds (both absolute and percentage-based),
        otherwise does a full recomputation.

        Threshold logic:
        - Absolute threshold: max_incremental_duplicates (default: 500)
        - Percentage threshold: incremental_threshold_percent of total images (default: 20%)
        - Minimum threshold: 50 (always allow small batches incrementally)
        - Uses minimum of (absolute, percentage) but at least minimum threshold

        Args:
            conn: Optional database connection. If None, creates a new one.
            force_full: If True, skip incremental and do full recomputation.

        Returns:
            Dict mapping level to number of groups found.
        """
        should_close = conn is None
        if conn is None:
            conn = self._get_db()

        try:
            # Get dirty images (or all images if force_full)
            if force_full:
                # Treat all images as dirty for full recomputation
                cursor = conn.execute('SELECT id FROM images WHERE deleted = 0')
                dirty_ids = [row['id'] for row in cursor.fetchall()]
                logger.info(f'Force full: treating all {len(dirty_ids)} images as dirty')
            else:
                epoch = _get_duplicate_epoch(conn)
                dirty_ids = _get_dirty_image_ids(conn, epoch)

            if not dirty_ids:
                logger.info('No images to process for duplicates')
                self._set_all_status('done')
                return {0: 0, 1: 0, 2: 0, 3: 0}

            dirty_count = len(dirty_ids)

            # Get total image count for percentage calculation
            cursor = conn.execute('SELECT COUNT(*) as cnt FROM images WHERE deleted = 0')
            total_count = cursor.fetchone()['cnt']

            # Calculate effective threshold using both absolute and percentage
            absolute_threshold = self._config.max_incremental_duplicates
            percent_threshold = int(total_count * self._config.incremental_threshold_percent / 100)

            # Use the more conservative (lower) threshold, but maintain a minimum
            # to avoid full rebuilds for tiny batches
            min_threshold = 50
            effective_threshold = max(min_threshold, min(absolute_threshold, percent_threshold))

            # Determine whether to use incremental
            use_incremental = not force_full and dirty_count <= effective_threshold

            if use_incremental:
                logger.info(
                    f'Processing {dirty_count} dirty images incrementally '
                    f'(threshold: {effective_threshold}, {dirty_count}/{total_count} = '
                    f'{dirty_count * 100 / total_count:.1f}%)'
                )
            else:
                logger.info(
                    f'{dirty_count} dirty images ({dirty_count * 100 / total_count:.1f}%) '
                    f'exceeds threshold ({effective_threshold}), doing full recomputation'
                )

            results = {}
            for level in range(4):
                self._set_status(level, 'computing')

                try:
                    group_count = _get_group_count(conn, level)

                    # Level 1 always uses full computation (LSH is more efficient)
                    if level == 1:
                        logger.info(f'Level {level}: full computation (LSH)')
                        results[level] = _compute_duplicates_level1(conn, self._config.perceptual_hash_threshold)
                    elif group_count == 0:
                        # No existing groups - must do full computation
                        logger.info(f'Level {level}: no existing groups, full computation')
                        if level == 0:
                            results[level] = _compute_duplicates_level0(conn)
                        elif level == 2:
                            results[level] = _compute_duplicates_level2(conn, self._config.similarity_threshold_level2)
                        else:
                            results[level] = _compute_duplicates_level3(conn, self._config.similarity_threshold_level3)
                    elif use_incremental:
                        logger.info(f'Level {level}: {group_count} groups, incremental update')
                        if level == 0:
                            results[level] = _compute_duplicates_level0_incremental(conn, dirty_ids)
                        elif level == 2:
                            results[level] = _compute_duplicates_embedding_incremental(
                                conn, dirty_ids, level=2, threshold=self._config.similarity_threshold_level2
                            )
                        else:
                            results[level] = _compute_duplicates_embedding_incremental(
                                conn, dirty_ids, level=3, threshold=self._config.similarity_threshold_level3
                            )
                    else:
                        logger.info(f'Level {level}: over threshold, full recomputation')
                        if level == 0:
                            results[level] = _compute_duplicates_level0(conn)
                        elif level == 2:
                            results[level] = _compute_duplicates_level2(conn, self._config.similarity_threshold_level2)
                        else:
                            results[level] = _compute_duplicates_level3(conn, self._config.similarity_threshold_level3)

                except Exception as e:
                    logger.error(f'Error computing level {level} duplicates: {e}')
                    results[level] = 0

                self._set_status(level, 'done')

            # Update epoch
            _set_duplicate_epoch(conn, datetime.now().isoformat())

            # Invalidate cache so it reloads with fresh data
            self._invalidate_cache()

            total = sum(results.values())
            logger.info(f'Duplicate computation complete: {total} groups from {dirty_count} dirty images')
            return results

        finally:
            if should_close:
                conn.close()

    # =========================================================================
    # Similarity Search (for sorting)
    # =========================================================================

    def get_images_by_similarity(
        self,
        reference_embedding: np.ndarray,
        conn: SafeConnection | None = None,
    ) -> list[dict[str, Any]]:
        """Get all images sorted by similarity to a reference embedding."""
        should_close = conn is None
        if conn is None:
            conn = self._get_db()

        try:
            return _get_images_by_similarity(conn, reference_embedding)
        finally:
            if should_close:
                conn.close()

    # =========================================================================
    # Custom Groups (Level 5 — Albums)
    # =========================================================================

    def create_custom_group(
        self,
        group_hash: str,
        name: str,
        image_ids: list[str],
        filter_json: str | None = None,
        preview_image_id: str | None = None,
    ) -> None:
        """Create a custom group (album) or smart group with filter criteria.

        For regular groups, inserts into both custom_groups (metadata) and
        duplicate_groups (membership) tables.  For smart groups (filter_json
        is not None), only inserts into custom_groups -- membership is virtual,
        computed from the filter criteria each time the group is opened.

        Args:
            group_hash: Frontend-generated UUID for the group.
            name: Display name for the group.
            image_ids: Initial list of image IDs to include (may be empty).
                       Ignored when filter_json is provided.
            filter_json: JSON string of filter criteria for smart groups.
                         When None, creates a regular static custom group.
            preview_image_id: Representative image for smart group thumbnails.
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            conn.execute(
                'INSERT INTO custom_groups'
                ' (group_hash, name, filter_json, preview_image_id, created_at, updated_at)'
                ' VALUES (?, ?, ?, ?, ?, ?)',
                (group_hash, name, filter_json, preview_image_id, now, now),
            )
            # Smart groups have no static membership rows
            if filter_json is None:
                for image_id in image_ids:
                    conn.execute(
                        'INSERT OR IGNORE INTO duplicate_groups'
                        ' (level, group_hash, image_id, updated_at)'
                        ' VALUES (?, ?, ?, ?)',
                        (LEVEL_CUSTOM, group_hash, image_id, now),
                    )
            conn.commit()
            if filter_json:
                logger.info(f'Created smart group "{name}" ({group_hash})')
            else:
                logger.info(f'Created custom group "{name}" ({group_hash}) with {len(image_ids)} images')
        finally:
            conn.close()

        # Update cache (smart groups have an empty member set)
        self._ensure_cache_loaded()
        with self._cache_lock:
            self._group_cache[LEVEL_CUSTOM][group_hash] = set() if filter_json else set(image_ids)

    def rename_custom_group(self, group_hash: str, name: str) -> None:
        """Rename a custom group.

        Args:
            group_hash: The group identifier.
            name: New display name.
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            conn.execute(
                'UPDATE custom_groups SET name = ?, updated_at = ? WHERE group_hash = ?', (name, now, group_hash)
            )
            conn.commit()
            logger.info(f'Renamed custom group {group_hash} to "{name}"')
        finally:
            conn.close()

    def update_custom_group_filter(
        self, group_hash: str, filter_json: str, preview_image_id: str | None = None
    ) -> None:
        """Update the filter criteria (and optionally preview) of a smart group.

        Args:
            group_hash: The group identifier.
            filter_json: New JSON string of filter criteria.
            preview_image_id: New representative image ID (or None to clear).
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            conn.execute(
                'UPDATE custom_groups SET filter_json = ?, preview_image_id = ?,'
                ' damaged = 0, updated_at = ? WHERE group_hash = ?',
                (filter_json, preview_image_id, now, group_hash),
            )
            conn.commit()
            logger.info(f'Updated filter for smart group {group_hash}')
        finally:
            conn.close()

    def mark_smart_groups_damaged(self, removed_person_ids: list[str]) -> bool:
        """Mark smart groups as damaged if they reference deleted people.

        Scans all undamaged smart groups whose filter_json contains a
        people list, and flags any that reference a person ID in
        ``removed_person_ids``.  This is cheap: only undamaged smart
        groups are checked (typically few), and the person ID lookup is
        a set membership test.

        Args:
            removed_person_ids: Person UUIDs that were just deleted.

        Returns:
            True if any groups were newly marked damaged.
        """
        if not removed_person_ids:
            return False

        removed_set = set(removed_person_ids)
        conn = self._get_db()
        try:
            cursor = conn.execute(
                'SELECT group_hash, filter_json FROM custom_groups WHERE filter_json IS NOT NULL AND damaged = 0'
            )
            damaged_hashes = []
            for row in cursor.fetchall():
                try:
                    filt = json.loads(row['filter_json'])
                except (json.JSONDecodeError, TypeError):
                    continue
                people = filt.get('people')
                if not people:
                    continue
                # people is [{id, name}, ...] — check if any id was deleted
                if any(p.get('id') in removed_set for p in people):
                    damaged_hashes.append(row['group_hash'])

            if not damaged_hashes:
                return False

            placeholders = sql_placeholders(damaged_hashes)
            conn.execute(
                f'UPDATE custom_groups SET damaged = 1 WHERE group_hash IN ({placeholders})',
                damaged_hashes,
            )
            conn.commit()
            logger.info('Marked %d smart group(s) as damaged: %s', len(damaged_hashes), damaged_hashes)
            return True
        finally:
            conn.close()

    def update_smart_group_preview(self, group_hash: str, preview_image_id: str | None) -> None:
        """Update only the preview thumbnail of a smart group.

        Called by the frontend after evaluating the filter to pick a
        representative image, or to clear a stale preview after image deletion.

        Args:
            group_hash: The group identifier.
            preview_image_id: Image ID for the thumbnail, or None to clear.
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            conn.execute(
                'UPDATE custom_groups SET preview_image_id = ?, updated_at = ? WHERE group_hash = ?',
                (preview_image_id, now, group_hash),
            )
            conn.commit()
        finally:
            conn.close()

    def delete_custom_group(self, group_hash: str) -> None:
        """Delete a custom group and its image associations.

        Removes from both custom_groups and duplicate_groups tables.

        Args:
            group_hash: The group identifier.
        """
        conn = self._get_db()
        try:
            conn.execute('DELETE FROM custom_groups WHERE group_hash = ?', (group_hash,))
            conn.execute('DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ?', (LEVEL_CUSTOM, group_hash))
            conn.commit()
            logger.info(f'Deleted custom group {group_hash}')
        finally:
            conn.close()

        # Update cache
        if self._cache_loaded:
            with self._cache_lock:
                self._group_cache[LEVEL_CUSTOM].pop(group_hash, None)

    def add_images_to_custom_group(self, group_hash: str, image_ids: list[str]) -> None:
        """Add images to an existing custom group.

        Skips images already in the group (INSERT OR IGNORE).

        Args:
            group_hash: The group identifier.
            image_ids: Image IDs to add.
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            for image_id in image_ids:
                conn.execute(
                    'INSERT OR IGNORE INTO duplicate_groups'
                    ' (level, group_hash, image_id, updated_at)'
                    ' VALUES (?, ?, ?, ?)',
                    (LEVEL_CUSTOM, group_hash, image_id, now),
                )
            conn.execute(
                'UPDATE custom_groups SET updated_at = ? WHERE group_hash = ?',
                (now, group_hash),
            )
            conn.commit()
            logger.info(f'Added {len(image_ids)} images to custom group {group_hash}')
        finally:
            conn.close()

        # Update cache
        self._ensure_cache_loaded()
        with self._cache_lock:
            if group_hash not in self._group_cache[LEVEL_CUSTOM]:
                self._group_cache[LEVEL_CUSTOM][group_hash] = set()
            self._group_cache[LEVEL_CUSTOM][group_hash].update(image_ids)

    def remove_images_from_custom_group(self, group_hash: str, image_ids: list[str]) -> None:
        """Remove images from a custom group (group persists even if empty).

        Args:
            group_hash: The group identifier.
            image_ids: Image IDs to remove.
        """
        now = datetime.now().isoformat()
        conn = self._get_db()
        try:
            placeholders = sql_placeholders(image_ids)
            conn.execute(
                f'DELETE FROM duplicate_groups WHERE level = ? AND group_hash = ? AND image_id IN ({placeholders})',
                [LEVEL_CUSTOM, group_hash] + image_ids,
            )
            conn.execute('UPDATE custom_groups SET updated_at = ? WHERE group_hash = ?', (now, group_hash))
            conn.commit()
            logger.info(f'Removed {len(image_ids)} images from custom group {group_hash}')
        finally:
            conn.close()

        # Update cache — group persists even when empty
        if self._cache_loaded:
            with self._cache_lock:
                if group_hash in self._group_cache[LEVEL_CUSTOM]:
                    self._group_cache[LEVEL_CUSTOM][group_hash] -= set(image_ids)
