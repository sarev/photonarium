"""
Duplicate detection and group management for the Imaginary application.

This module provides the DuplicateManager class which handles all duplicate
detection across 4 similarity levels:

- Level 0: Identical (same SHA256 checksum)
- Level 1: Near-identical (perceptual hash within Hamming distance threshold)
- Level 2: Similar (high OpenCLIP embedding cosine similarity)
- Level 3: Related (lower embedding similarity threshold)

The module uses several optimization techniques:
- Multi-index hashing (LSH) for level 1 to avoid O(n²) comparisons
- Chunked matrix multiplication for levels 2-3 to manage memory
- Union-find with path compression for efficient clustering
- Incremental updates for small batches of new/modified images
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any

import numpy as np

from config import Config, get_default_config

logger = logging.getLogger(__name__)


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


def compute_cosine_similarity(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
    """Compute cosine similarity between two embeddings.

    Both embeddings should already be normalised.

    Args:
        embedding1: First embedding vector.
        embedding2: Second embedding vector.

    Returns:
        Cosine similarity in range [-1, 1].
    """
    return float(np.dot(embedding1, embedding2))


def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two perceptual hash hex strings.

    Args:
        hash1: First hash as hex string.
        hash2: Second hash as hex string.

    Returns:
        Number of differing bits between the hashes.
    """
    int1 = int(hash1, 16)
    int2 = int(hash2, 16)
    xor = int1 ^ int2
    return bin(xor).count('1')


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
    - Initialization from existing groups
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
        """Initialize UnionFind.

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

        Args:
            x: Element index.

        Returns:
            Root index of the set containing x.
        """
        if self._parent[x] != x:
            self._parent[x] = self.find(self._parent[x])  # Path compression
        return self._parent[x]

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

    def find_id(self, id_: str) -> str:
        """Find the root ID for an element (ID mode).

        Args:
            id_: Element ID.

        Returns:
            Root ID of the set containing this element.
        """
        if self._id_to_idx is None:
            raise ValueError("UnionFind not initialized with IDs")
        idx = self._id_to_idx[id_]
        root_idx = self.find(idx)
        return self._ids[root_idx]

    def union_ids(self, id1: str, id2: str) -> bool:
        """Union the sets containing two IDs (ID mode).

        Args:
            id1: First element ID.
            id2: Second element ID.

        Returns:
            True if the sets were merged, False if already in same set.
        """
        if self._id_to_idx is None:
            raise ValueError("UnionFind not initialized with IDs")
        return self.union(self._id_to_idx[id1], self._id_to_idx[id2])

    def connected(self, x: int, y: int) -> bool:
        """Check if two elements are in the same set.

        Args:
            x: First element index.
            y: Second element index.

        Returns:
            True if in the same set.
        """
        return self.find(x) == self.find(y)

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
            raise ValueError("UnionFind not initialized with IDs")

        groups: dict[str, list[str]] = {}
        for i in range(self._n):
            root = self.find(i)
            root_id = self._ids[root]
            if root_id not in groups:
                groups[root_id] = []
            groups[root_id].append(self._ids[i])
        return groups

    def extract_groups_filtered(self, min_size: int = 2) -> dict[int, list[int]]:
        """Extract groups with at least min_size members (index mode).

        Args:
            min_size: Minimum group size to include.

        Returns:
            Dict mapping root index to list of member indices.
        """
        all_groups = self.extract_groups()
        return {root: members for root, members in all_groups.items()
                if len(members) >= min_size}

    def load_existing_groups(self, groups: list[set[str]]) -> None:
        """Initialize from existing groups (ID mode).

        Unions all members of each group together.

        Args:
            groups: List of sets, each containing IDs in the same group.
        """
        if self._id_to_idx is None:
            raise ValueError("UnionFind not initialized with IDs")

        for group in groups:
            group_list = list(group)
            if len(group_list) < 2:
                continue
            first = group_list[0]
            for other in group_list[1:]:
                if first in self._id_to_idx and other in self._id_to_idx:
                    self.union_ids(first, other)

    @property
    def size(self) -> int:
        """Return the number of elements."""
        return self._n


# =============================================================================
# DATABASE HELPER FUNCTIONS
# =============================================================================

def _get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value by key."""
    cursor = conn.execute('SELECT value FROM metadata WHERE key = ?', (key,))
    row = cursor.fetchone()
    return row['value'] if row else None


def _set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata value."""
    conn.execute(
        'INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)',
        (key, value)
    )
    conn.commit()


def _clear_duplicate_groups(conn: sqlite3.Connection, level: int | None = None) -> None:
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
    conn: sqlite3.Connection,
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
            (level, group_hash, image_id, now)
        )


def _get_dirty_image_ids(conn: sqlite3.Connection, epoch: str) -> list[str]:
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
    cursor = conn.execute("""
        SELECT id FROM images
        WHERE deleted = 0 AND updated_at > ?
    """, (epoch,))
    return [row['id'] for row in cursor.fetchall()]


def _get_group_count(conn: sqlite3.Connection, level: int) -> int:
    """Get the number of duplicate groups at a level."""
    cursor = conn.execute(
        'SELECT COUNT(DISTINCT group_hash) as cnt FROM duplicate_groups WHERE level = ?',
        (level,)
    )
    return cursor.fetchone()['cnt']


def _get_image_to_group_mapping(conn: sqlite3.Connection, level: int) -> dict[str, str]:
    """Get mapping of image IDs to their group hashes."""
    cursor = conn.execute(
        'SELECT image_id, group_hash FROM duplicate_groups WHERE level = ?',
        (level,)
    )
    return {row['image_id']: row['group_hash'] for row in cursor.fetchall()}


def _add_image_to_group(
    conn: sqlite3.Connection,
    level: int,
    group_hash: str,
    image_id: str,
) -> None:
    """Add a single image to an existing group."""
    now = datetime.now().isoformat()
    conn.execute(
        'INSERT INTO duplicate_groups (level, group_hash, image_id, updated_at) VALUES (?, ?, ?, ?)',
        (level, group_hash, image_id, now)
    )


def _merge_groups(
    conn: sqlite3.Connection,
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
        '''UPDATE duplicate_groups
           SET group_hash = ?, updated_at = ?
           WHERE level = ? AND group_hash = ?''',
        (group_hash_keep, now, level, group_hash_merge)
    )


# =============================================================================
# LEVEL 0: IDENTICAL DUPLICATES (CHECKSUM)
# =============================================================================

def _compute_duplicates_level0(conn: sqlite3.Connection) -> int:
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
    conn: sqlite3.Connection,
    dirty_ids: list[str],
) -> int:
    """Incrementally update level 0 duplicates for dirty images."""
    if not dirty_ids:
        return 0

    logger.info(f'Incremental level 0: checking {len(dirty_ids)} images')

    image_to_group = _get_image_to_group_mapping(conn, level=0)

    # Get checksums for dirty images
    placeholders = ','.join('?' * len(dirty_ids))
    cursor = conn.execute(
        f'SELECT id, checksum FROM images WHERE id IN ({placeholders}) AND deleted = 0',
        dirty_ids
    )
    dirty_checksums = {row['id']: row['checksum'] for row in cursor.fetchall()}

    new_groups = 0

    for dirty_id, checksum in dirty_checksums.items():
        if not checksum:
            continue

        # Find all images with this checksum (excluding the dirty one)
        cursor = conn.execute(
            'SELECT id FROM images WHERE checksum = ? AND deleted = 0 AND id != ?',
            (checksum, dirty_id)
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


def _hamming_distance_fast(hash1: int, hash2: int) -> int:
    """Compute Hamming distance between two integer hashes.

    Uses int.bit_count() which is optimized in CPython 3.10+.
    Falls back to bin().count('1') for compatibility.
    """
    xor = hash1 ^ hash2
    # int.bit_count() is faster in Python 3.10+
    if hasattr(xor, 'bit_count'):
        return xor.bit_count()
    return bin(xor).count('1')


def _compute_level1_brute_force(
    image_data: list[tuple[str, int]],
    threshold: int,
) -> tuple[UnionFind, int, int]:
    """Brute-force O(n²) comparison for small datasets.

    For small n, the overhead of building LSH index exceeds the cost of
    direct comparison. Returns UnionFind with clusters and match stats.
    """
    n = len(image_data)
    uf = UnionFind(n=n)
    comparisons = 0
    matches = 0

    for i in range(n):
        hash1 = image_data[i][1]
        for j in range(i + 1, n):
            hash2 = image_data[j][1]
            dist = _hamming_distance_fast(hash1, hash2)
            comparisons += 1
            if dist <= threshold:
                uf.union(i, j)
                matches += 1

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
    leftover_bits = 64 - (bits_per_band * num_bands)

    # Build inverted index: band_value -> list of image indices
    band_indices: list[dict[int, list[int]]] = [{} for _ in range(num_bands)]

    for idx, (img_id, hash_int) in enumerate(image_data):
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
    non_singleton_buckets = sum(
        1 for band in band_indices
        for bucket in band.values()
        if len(bucket) > 1
    )
    max_bucket_size = max(
        (len(bucket) for band in band_indices for bucket in band.values()),
        default=0
    )

    # Union-find for clustering
    uf = UnionFind(n=n)

    # Compare only candidate pairs that share at least one band
    compared: set[tuple[int, int]] = set()
    comparisons = 0
    matches = 0

    for band in range(num_bands):
        for bucket in band_indices[band].values():
            if len(bucket) < 2:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    idx1, idx2 = bucket[i], bucket[j]
                    if idx1 > idx2:
                        idx1, idx2 = idx2, idx1
                    pair = (idx1, idx2)
                    if pair in compared:
                        continue
                    compared.add(pair)

                    hash1 = image_data[idx1][1]
                    hash2 = image_data[idx2][1]
                    dist = _hamming_distance_fast(hash1, hash2)
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


def _compute_duplicates_level1(conn: sqlite3.Connection, threshold: int = 4) -> int:
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
        logger.info(
            f'  Completed: {comparisons:,} comparisons ({reduction:.1f}% reduction), '
            f'{matches:,} matches'
        )
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


def _compute_duplicates_level1_incremental(
    conn: sqlite3.Connection,
    dirty_ids: list[str],
    threshold: int = 5,
) -> int:
    """Incrementally update level 1 duplicates for dirty images."""
    if not dirty_ids:
        return 0

    logger.info(f'Incremental level 1: checking {len(dirty_ids)} images')

    image_to_group = _get_image_to_group_mapping(conn, level=1)

    # Get all images with perceptual hashes for comparison
    cursor = conn.execute(
        'SELECT id, perceptual_hash FROM images WHERE deleted = 0 AND perceptual_hash IS NOT NULL'
    )
    all_images = {row['id']: row['perceptual_hash'] for row in cursor.fetchall()}

    dirty_hashes = {img_id: all_images.get(img_id) for img_id in dirty_ids if img_id in all_images}

    new_groups = 0

    for dirty_id, dirty_hash in dirty_hashes.items():
        if dirty_hash is None:
            continue

        dirty_hash_int = int(dirty_hash, 16) if isinstance(dirty_hash, str) else dirty_hash

        # Find matches within Hamming distance threshold
        matches = []
        for other_id, other_hash in all_images.items():
            if other_id == dirty_id:
                continue
            other_hash_int = int(other_hash, 16) if isinstance(other_hash, str) else other_hash
            distance = bin(dirty_hash_int ^ other_hash_int).count('1')
            if distance <= threshold:
                matches.append(other_id)

        if not matches:
            continue

        # Check if any match is already in a group
        existing_groups = set()
        for match_id in matches:
            if match_id in image_to_group:
                existing_groups.add(image_to_group[match_id])

        if existing_groups:
            target_group = next(iter(existing_groups))
            if dirty_id not in image_to_group:
                _add_image_to_group(conn, level=1, group_hash=target_group, image_id=dirty_id)
                image_to_group[dirty_id] = target_group

            for other_group in existing_groups:
                if other_group != target_group:
                    _merge_groups(conn, level=1, group_hash_keep=target_group, group_hash_merge=other_group)
                    for img_id, grp in list(image_to_group.items()):
                        if grp == other_group:
                            image_to_group[img_id] = target_group
        else:
            group_hash = f'phash_{dirty_id}'
            all_members = [dirty_id] + matches
            _insert_duplicate_group(conn, level=1, group_hash=group_hash, image_ids=all_members)
            for member in all_members:
                image_to_group[member] = group_hash
            new_groups += 1

    conn.commit()
    logger.info(f'Incremental level 1: created {new_groups} new groups')
    return new_groups


# =============================================================================
# LEVELS 2 & 3: EMBEDDING-BASED DUPLICATES
# =============================================================================

def _normalize_embeddings(embeddings: np.ndarray) -> np.ndarray:
    """Normalize embeddings to unit length for cosine similarity.

    After normalization, dot product equals cosine similarity.

    Args:
        embeddings: Array of shape (n, dim).

    Returns:
        Normalized array of same shape.
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
    Embeddings should already be normalized for cosine similarity.

    Algorithm:
    1. Process embeddings in chunks to limit memory to O(chunk_size * n)
    2. For each chunk, compute similarity matrix against all embeddings
    3. Use vectorized numpy operations to find pairs above threshold
    4. Union matching pairs in UnionFind structure
    5. Extract final groups

    Args:
        image_ids: List of image IDs corresponding to embeddings.
        embeddings: Normalized embedding matrix of shape (n, dim).
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
                row_sims = similarities[i_local, i_global + 1:]
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


def _load_embeddings_normalized(
    conn: sqlite3.Connection,
) -> tuple[list[str], np.ndarray] | None:
    """Load all image embeddings and normalize them.

    Args:
        conn: Database connection.

    Returns:
        Tuple of (image_ids, normalized_embeddings) or None if < 2 images.
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

    # Normalize once for all subsequent operations
    embeddings = _normalize_embeddings(embeddings)

    return image_ids, embeddings


def _compute_duplicates_level2(conn: sqlite3.Connection, threshold: float = 0.95) -> int:
    """Compute level 2 duplicates (similar embeddings).

    Groups images with high cosine similarity (>= threshold).
    Level 2 uses a high threshold (default 0.95) for visually similar images
    like crops, color adjustments, or shot sequences.

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity (default 0.95).

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 2 duplicates (embedding similarity >= {threshold})')

    _clear_duplicate_groups(conn, level=2)

    result = _load_embeddings_normalized(conn)
    if result is None:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids, embeddings = result
    n = len(image_ids)

    logger.info(f'Processing {n} images ({embeddings.shape[1]}-dim embeddings)')

    groups, metrics = _compute_embedding_duplicates_chunked(
        image_ids, embeddings, threshold, chunk_size=1000
    )

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


def _compute_duplicates_level3(conn: sqlite3.Connection, threshold: float = 0.85) -> int:
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

    result = _load_embeddings_normalized(conn)
    if result is None:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids, embeddings = result
    n = len(image_ids)

    logger.info(f'Processing {n} images ({embeddings.shape[1]}-dim embeddings)')

    groups, metrics = _compute_embedding_duplicates_chunked(
        image_ids, embeddings, threshold, chunk_size=1000
    )

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
    conn: sqlite3.Connection,
    dirty_ids: list[str],
    level: int,
    threshold: float,
) -> int:
    """Incrementally update embedding-based duplicates for dirty images.

    For each dirty image, finds all images with similarity >= threshold
    and either adds to existing groups or creates new ones.

    Args:
        conn: Database connection.
        dirty_ids: List of image IDs that need checking.
        level: Duplicate level (2 or 3).
        threshold: Minimum cosine similarity for a match.

    Returns:
        Number of new groups created.
    """
    if not dirty_ids:
        return 0

    logger.info(f'Incremental level {level}: checking {len(dirty_ids)} images')

    image_to_group = _get_image_to_group_mapping(conn, level=level)

    cursor = conn.execute(
        'SELECT id, embedding FROM images WHERE deleted = 0 AND embedding IS NOT NULL'
    )
    rows = cursor.fetchall()

    if len(rows) < 2:
        return 0

    # Build lookup structures
    all_embeddings = {row['id']: embedding_to_numpy(row['embedding']) for row in rows}

    # Stack all embeddings for vectorized comparison
    id_list = list(all_embeddings.keys())
    embedding_matrix = np.array([all_embeddings[id_] for id_ in id_list])

    # Normalize once for all comparisons
    embedding_matrix = _normalize_embeddings(embedding_matrix)

    # Build id -> index mapping for fast lookup
    id_to_idx = {id_: idx for idx, id_ in enumerate(id_list)}

    new_groups = 0

    for dirty_id in dirty_ids:
        if dirty_id not in all_embeddings:
            continue

        # Get normalized embedding for dirty image
        dirty_idx = id_to_idx[dirty_id]
        dirty_emb_norm = embedding_matrix[dirty_idx]

        # Vectorized similarity computation
        similarities = embedding_matrix @ dirty_emb_norm

        # Find matches above threshold (excluding self)
        match_mask = (similarities >= threshold)
        match_mask[dirty_idx] = False  # Exclude self
        match_indices = np.where(match_mask)[0]
        matches = [id_list[idx] for idx in match_indices]

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

def _get_duplicate_groups(conn: sqlite3.Connection, level: int) -> list[dict[str, Any]]:
    """Get duplicate groups at a specific level with full image data."""
    cursor = conn.execute("""
        SELECT DISTINCT group_hash
        FROM duplicate_groups
        WHERE level = ?
    """, (level,))
    group_hashes = [row['group_hash'] for row in cursor.fetchall()]

    groups = []
    for group_hash in group_hashes:
        cursor = conn.execute("""
            SELECT i.id, i.path, i.basename, i.size, i.width, i.height,
                   i.timestamp, i.timestamp_confidence, i.checksum,
                   i.perceptual_hash, i.laplacian_var, i.lossless,
                   i.description, i.rating
            FROM images i
            JOIN duplicate_groups dg ON i.id = dg.image_id
            WHERE dg.level = ? AND dg.group_hash = ? AND i.deleted = 0
            ORDER BY i.size DESC, i.path ASC
        """, (level, group_hash))

        images = rows_to_dicts(cursor.fetchall())

        if len(images) > 1:
            groups.append({
                'group_hash': group_hash,
                'images': images,
            })

    return groups


def _get_duplicate_groups_lightweight(conn: sqlite3.Connection, level: int) -> list[dict[str, Any]]:
    """Get duplicate groups with minimal data for efficient grid display.

    The "best" image is selected by: highest resolution, then lossless format,
    then largest file size, then best focus (Laplacian variance).
    """
    cursor = conn.execute("""
        WITH ranked AS (
            SELECT
                dg.group_hash,
                i.id,
                i.basename,
                i.width,
                i.height,
                i.size,
                i.laplacian_var,
                i.lossless,
                ROW_NUMBER() OVER (
                    PARTITION BY dg.group_hash
                    ORDER BY
                        (i.width * i.height) DESC,
                        i.lossless DESC,
                        i.size DESC,
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
    """, (level,))

    groups = []
    for row in cursor.fetchall():
        id_cursor = conn.execute("""
            SELECT i.id
            FROM images i
            JOIN duplicate_groups dg ON i.id = dg.image_id
            WHERE dg.level = ? AND dg.group_hash = ? AND i.deleted = 0
        """, (level, row['group_hash']))
        image_ids = [r['id'] for r in id_cursor.fetchall()]

        groups.append({
            'group_hash': row['group_hash'],
            'count': row['count'],
            'image_ids': image_ids,
            'best_image': {
                'id': row['best_id'],
                'basename': row['best_basename'],
            },
        })

    return groups


def _get_duplicate_epoch(conn: sqlite3.Connection) -> str:
    """Get the current epoch timestamp for duplicate groups."""
    epoch = _get_metadata(conn, 'duplicate_epoch')
    return epoch if epoch else ''


def _set_duplicate_epoch(conn: sqlite3.Connection, epoch: str) -> None:
    """Set the duplicate computation epoch."""
    _set_metadata(conn, 'duplicate_epoch', epoch)


# =============================================================================
# SIMILARITY SEARCH
# =============================================================================

def _get_images_by_similarity(
    conn: sqlite3.Connection,
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

    embedding_matrix = np.vstack(embeddings)
    similarities = embedding_matrix @ reference_embedding

    similarity_map = {ids[i]: float(similarities[i]) for i in range(len(ids))}

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
    - Computing duplicates at all 4 levels
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
        """Initialize the DuplicateManager.

        Args:
            db_path: Path to the SQLite database.
            config: Configuration object. Uses defaults if None.
        """
        self._db_path = db_path
        self._config = config or get_default_config()
        self._status_lock = threading.Lock()
        self._status: dict[int, str] = {0: 'pending', 1: 'pending', 2: 'pending', 3: 'pending'}

        # In-memory group cache (lazy loaded)
        self._cache_lock = threading.Lock()
        self._group_cache: dict[int, dict[str, set[str]]] | None = None  # level -> group_hash -> image_ids
        self._image_to_group: dict[int, dict[str, str]] | None = None    # level -> image_id -> group_hash
        self._cache_loaded = False

    def _get_db(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
            self._group_cache = {0: {}, 1: {}, 2: {}, 3: {}}
            self._image_to_group = {0: {}, 1: {}, 2: {}, 3: {}}

            conn = self._get_db()
            try:
                for level in range(4):
                    cursor = conn.execute("""
                        SELECT dg.group_hash, dg.image_id
                        FROM duplicate_groups dg
                        JOIN images i ON i.id = dg.image_id
                        WHERE dg.level = ? AND i.deleted = 0
                    """, (level,))

                    for row in cursor.fetchall():
                        group_hash = row['group_hash']
                        image_id = row['image_id']

                        if group_hash not in self._group_cache[level]:
                            self._group_cache[level][group_hash] = set()
                        self._group_cache[level][group_hash].add(image_id)
                        self._image_to_group[level][image_id] = group_hash

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

    def invalidate_image(self, image_id: str) -> None:
        """Remove an image from the cache when it's deleted or modified.

        This removes the image from its group at all levels. If the group
        becomes a singleton (only one image), the group is dissolved.

        Args:
            image_id: ID of the image to remove from cache.
        """
        if not self._cache_loaded:
            return  # Nothing to invalidate

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

    def get_group_for_image(self, level: int, image_id: str) -> str | None:
        """Get the group hash for an image at a specific level.

        Args:
            level: Duplicate level (0-3).
            image_id: ID of the image.

        Returns:
            Group hash if the image is in a group, None otherwise.
        """
        self._ensure_cache_loaded()
        with self._cache_lock:
            return self._image_to_group[level].get(image_id)

    def get_images_in_group(self, level: int, group_hash: str) -> set[str]:
        """Get all image IDs in a group.

        Args:
            level: Duplicate level (0-3).
            group_hash: The group identifier.

        Returns:
            Set of image IDs in the group (empty set if group not found).
        """
        self._ensure_cache_loaded()
        with self._cache_lock:
            return self._group_cache[level].get(group_hash, set()).copy()

    def get_group_count(self, level: int) -> int:
        """Get the number of groups at a level from cache.

        Args:
            level: Duplicate level (0-3).

        Returns:
            Number of duplicate groups at this level.
        """
        self._ensure_cache_loaded()
        with self._cache_lock:
            return len(self._group_cache[level])

    def get_all_group_hashes(self, level: int) -> list[str]:
        """Get all group hashes at a level.

        Args:
            level: Duplicate level (0-3).

        Returns:
            List of group hashes.
        """
        self._ensure_cache_loaded()
        with self._cache_lock:
            return list(self._group_cache[level].keys())

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

    def get_groups(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups at a specific level with full image data."""
        conn = self._get_db()
        try:
            return _get_duplicate_groups(conn, level)
        finally:
            conn.close()

    def get_groups_lightweight(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups with minimal data for efficient display.

        Uses the in-memory cache for image_ids to avoid per-group DB queries.
        Still queries DB for best_image selection (requires sorting by metadata).
        """
        self._ensure_cache_loaded()

        conn = self._get_db()
        try:
            # Query for best image per group (still needs DB for sorting)
            cursor = conn.execute("""
                WITH ranked AS (
                    SELECT
                        dg.group_hash,
                        i.id,
                        i.basename,
                        i.width,
                        i.height,
                        i.size,
                        i.laplacian_var,
                        i.lossless,
                        ROW_NUMBER() OVER (
                            PARTITION BY dg.group_hash
                            ORDER BY
                                (i.width * i.height) DESC,
                                i.lossless DESC,
                                i.size DESC,
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
            """, (level,))

            groups = []
            with self._cache_lock:
                for row in cursor.fetchall():
                    group_hash = row['group_hash']

                    # Get image_ids from cache instead of DB query
                    image_ids = list(self._group_cache[level].get(group_hash, set()))

                    groups.append({
                        'group_hash': group_hash,
                        'count': row['count'],
                        'image_ids': image_ids,
                        'best_image': {
                            'id': row['best_id'],
                            'basename': row['best_basename'],
                        },
                    })

            return groups
        finally:
            conn.close()

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
        conn: sqlite3.Connection | None = None,
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
            # Get dirty images
            epoch = _get_duplicate_epoch(conn)
            dirty_ids = _get_dirty_image_ids(conn, epoch)

            if not dirty_ids:
                existing_epoch = _get_duplicate_epoch(conn)
                if existing_epoch:
                    logger.info(
                        'Skipping duplicate computation: no dirty images and '
                        f'duplicates already computed (epoch: {existing_epoch})'
                    )
                    self._set_all_status('done')
                    return {0: 0, 1: 0, 2: 0, 3: 0}
                else:
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

            if force_full:
                logger.info(
                    f'Force full recomputation requested ({dirty_count} dirty images)'
                )
            elif use_incremental:
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
                        results[level] = _compute_duplicates_level1(
                            conn, self._config.perceptual_hash_threshold
                        )
                    elif group_count == 0:
                        # No existing groups - must do full computation
                        logger.info(f'Level {level}: no existing groups, full computation')
                        if level == 0:
                            results[level] = _compute_duplicates_level0(conn)
                        elif level == 2:
                            results[level] = _compute_duplicates_level2(
                                conn, self._config.similarity_threshold_level2
                            )
                        else:
                            results[level] = _compute_duplicates_level3(
                                conn, self._config.similarity_threshold_level3
                            )
                    elif use_incremental:
                        logger.info(f'Level {level}: {group_count} groups, incremental update')
                        if level == 0:
                            results[level] = _compute_duplicates_level0_incremental(
                                conn, dirty_ids
                            )
                        elif level == 2:
                            results[level] = _compute_duplicates_embedding_incremental(
                                conn, dirty_ids, level=2,
                                threshold=self._config.similarity_threshold_level2
                            )
                        else:
                            results[level] = _compute_duplicates_embedding_incremental(
                                conn, dirty_ids, level=3,
                                threshold=self._config.similarity_threshold_level3
                            )
                    else:
                        logger.info(f'Level {level}: over threshold, full recomputation')
                        if level == 0:
                            results[level] = _compute_duplicates_level0(conn)
                        elif level == 2:
                            results[level] = _compute_duplicates_level2(
                                conn, self._config.similarity_threshold_level2
                            )
                        else:
                            results[level] = _compute_duplicates_level3(
                                conn, self._config.similarity_threshold_level3
                            )

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
        conn: sqlite3.Connection | None = None,
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
