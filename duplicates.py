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

def _compute_duplicates_level1(conn: sqlite3.Connection, threshold: int = 4) -> int:
    """Compute level 1 duplicates (perceptual hash similarity).

    Groups images with perceptual hash Hamming distance <= threshold.

    Uses multi-index hashing (locality-sensitive hashing) to avoid O(n²)
    comparisons. By splitting each hash into bands, we only compare images
    that share at least one band.

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
    image_data = []
    for row in images:
        try:
            hash_int = int(row['perceptual_hash'], 16)
            image_data.append((row['id'], hash_int))
        except ValueError:
            continue

    if len(image_data) < 2:
        return 0

    n = len(image_data)
    logger.info(f'Processing {n} images with multi-index hashing')

    # Multi-index hashing: split 64-bit hash into bands
    num_bands = threshold + 1
    bits_per_band = 64 // num_bands

    # Build inverted index: band_value -> list of image indices
    band_indices: list[dict[int, list[int]]] = [{} for _ in range(num_bands)]

    for idx, (img_id, hash_int) in enumerate(image_data):
        for band in range(num_bands):
            shift = band * bits_per_band
            if band == num_bands - 1:
                band_value = hash_int >> shift
            else:
                mask = (1 << bits_per_band) - 1
                band_value = (hash_int >> shift) & mask

            if band_value not in band_indices[band]:
                band_indices[band][band_value] = []
            band_indices[band][band_value].append(idx)

    # Union-find for clustering
    parent = list(range(n))

    def find(x: int) -> int:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Compare only candidate pairs that share at least one band
    compared = set()
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
                    dist = bin(hash1 ^ hash2).count('1')
                    comparisons += 1

                    if dist <= threshold:
                        union(idx1, idx2)
                        matches += 1

    brute_force = n * (n - 1) // 2
    reduction = (1 - comparisons / brute_force) * 100 if brute_force > 0 else 0
    logger.info(f'  Completed: {comparisons:,} comparisons ({reduction:.1f}% reduction from brute force)')

    # Build groups from union-find
    groups: dict[int, list[str]] = {}
    for idx, (img_id, _) in enumerate(image_data):
        root = find(idx)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)

    # Insert groups with more than one member
    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'phash_{image_data[root][0]}'
            _insert_duplicate_group(conn, level=1, group_hash=group_hash, image_ids=members)
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

def _compute_embedding_duplicates_chunked(
    image_ids: list[str],
    embeddings: np.ndarray,
    threshold: float,
    chunk_size: int = 1000,
) -> dict[int, list[str]]:
    """Compute duplicate groups from embeddings using chunked processing.

    Uses chunked matrix multiplication to avoid O(n²) memory usage.
    Only stores pairs above threshold, then builds clusters with union-find.

    Returns:
        Dictionary mapping group root index to list of image IDs.
    """
    n = len(image_ids)

    # Union-find with path compression and union by rank
    parent = list(range(n))
    rank = [0] * n

    def find(x: int) -> int:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px == py:
            return
        if rank[px] < rank[py]:
            px, py = py, px
        parent[py] = px
        if rank[px] == rank[py]:
            rank[px] += 1

    # Process in chunks to avoid O(n²) memory
    pairs_found = 0
    total_chunks = (n + chunk_size - 1) // chunk_size

    for chunk_start in range(0, n, chunk_size):
        chunk_end = min(chunk_start + chunk_size, n)
        chunk_embeddings = embeddings[chunk_start:chunk_end]

        # Compute similarities between chunk and all embeddings
        similarities = chunk_embeddings @ embeddings.T

        # Find pairs above threshold (only upper triangle)
        for i_local in range(chunk_end - chunk_start):
            i_global = chunk_start + i_local
            start_j = max(i_global + 1, 0)
            for j in range(start_j, n):
                if similarities[i_local, j] >= threshold:
                    union(i_global, j)
                    pairs_found += 1

    logger.info(f'  Completed: {pairs_found:,} similar pairs found')

    # Build groups from union-find
    groups: dict[int, list[str]] = {}
    for i, img_id in enumerate(image_ids):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)

    return groups


def _compute_duplicates_level2(conn: sqlite3.Connection, threshold: float = 0.95) -> int:
    """Compute level 2 duplicates (similar embeddings)."""
    logger.info(f'Computing level 2 duplicates (embedding similarity >= {threshold})')

    _clear_duplicate_groups(conn, level=2)

    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if len(rows) < 2:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids = [row['id'] for row in rows]
    embeddings = np.array([embedding_to_numpy(row['embedding']) for row in rows])

    logger.info(f'Processing {len(image_ids)} images with chunked similarity')

    groups = _compute_embedding_duplicates_chunked(
        image_ids, embeddings, threshold, chunk_size=1000
    )

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
    """Compute level 3 duplicates (related embeddings)."""
    logger.info(f'Computing level 3 duplicates (embedding similarity >= {threshold})')

    _clear_duplicate_groups(conn, level=3)

    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if len(rows) < 2:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    image_ids = [row['id'] for row in rows]
    embeddings = np.array([embedding_to_numpy(row['embedding']) for row in rows])

    logger.info(f'Processing {len(image_ids)} images with chunked similarity')

    groups = _compute_embedding_duplicates_chunked(
        image_ids, embeddings, threshold, chunk_size=1000
    )

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
    """Incrementally update embedding-based duplicates for dirty images."""
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
    embedding_matrix = np.array([all_embeddings[id] for id in id_list])

    # Normalize for cosine similarity
    norms = np.linalg.norm(embedding_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    embedding_matrix = embedding_matrix / norms

    new_groups = 0

    for dirty_id in dirty_ids:
        if dirty_id not in all_embeddings:
            continue

        dirty_emb = all_embeddings[dirty_id]
        dirty_emb_norm = dirty_emb / (np.linalg.norm(dirty_emb) or 1)

        similarities = embedding_matrix @ dirty_emb_norm

        matches = []
        for i, (other_id, sim) in enumerate(zip(id_list, similarities)):
            if other_id != dirty_id and sim >= threshold:
                matches.append(other_id)

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
    - Group retrieval

    Thread-safe: uses locks for status tracking and database operations.
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

    def _get_db(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
        """Get duplicate groups with minimal data for efficient display."""
        conn = self._get_db()
        try:
            return _get_duplicate_groups_lightweight(conn, level)
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

    def compute_all(self, conn: sqlite3.Connection | None = None) -> dict[int, int]:
        """Compute all duplicate groups, using incremental updates when possible.

        Uses incremental computation when the number of dirty images is below
        the configured threshold, otherwise does a full recomputation.

        Args:
            conn: Optional database connection. If None, creates a new one.

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
            max_incremental = self._config.max_incremental_duplicates
            use_incremental = dirty_count <= max_incremental

            if use_incremental:
                logger.info(
                    f'Processing {dirty_count} dirty images incrementally '
                    f'(threshold: {max_incremental})'
                )
            else:
                logger.info(
                    f'{dirty_count} dirty images exceeds threshold ({max_incremental}), '
                    'doing full recomputation'
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
