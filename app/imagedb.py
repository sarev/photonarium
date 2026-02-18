#!/usr/bin/env python3

"""
Photonarium image catalogue backend (single-file implementation).

Core responsibilities:

1) Maintain a local SQLite database of image files and their metadata.
2) Discover images by scanning user-registered folders on disk.
3) Compute derived properties (timestamps, checksums, perceptual hashes, etc.).
4) Compute and store semantic embeddings for images and text using OpenCLIP.
5) Provide query helpers for gallery browsing, semantic search, similarity, and
   duplicate grouping.
6) Generate and cache thumbnails on demand.
7) Run background processing in threads and optionally broadcast progress via
   Server-Sent Events (SSE).
8) Provide graceful shutdown helpers and an optional standalone test mode.

the main entry point is the `ImageDatabase` class. Everything else is either:

- Pure helper functions (database CRUD, scanning, embedding maths, thumbnails)
- Background worker threads
- Small utilities for streaming progress (SSE) and shutdown handling

-------------------------------------------------------------------------------
Concepts used in this file
-------------------------------------------------------------------------------

SQLite schema (high level)
    The database stores:
    - Registered folders (the roots to scan)
    - Images (one row per discovered image path, plus metadata)
    - Duplicate groups (pre-computed groupings at different similarity levels)
    - One-time migrations bookkeeping

    Embeddings are stored as raw float32 bytes (BLOB). When you read them back
    they are converted to NumPy arrays via `np.frombuffer(..., dtype=np.float32)`.

Images and metadata
    When ingesting an image, the module extracts or derives:
    - A "best" timestamp (preferring EXIF, then filename patterns, then filesystem)
    - A checksum (content hash, used for exact duplicates and thumbnail filenames)
    - A perceptual hash (used for near-duplicate detection)
    - Basic dimensions and file size
    - Optional user-editable fields such as description and rating

OpenCLIP embeddings (what and why)
    OpenCLIP is a library that provides CLIP-style models. CLIP models map both
    images and text into the same vector space. In practice this enables:
    - Semantic search: turn a text query into a vector, compare with stored image
      vectors using cosine similarity.
    - Visual similarity: compare image vectors to find visually related images.
    - Optional description embeddings: the same text encoder can embed user
      descriptions, which can be used to improve search recall.

    This module wraps OpenCLIP + PyTorch in `OpenCLIPModel` and uses it from the
    background embedding thread (and as a fallback for query-time encoding).

Duplicate detection levels
    Duplicate groups are computed after processing completes and stored in the
    database for fast retrieval. The module treats duplicates as tiers:
    - Level 0: exact matches (typically checksum-based)
    - Level 1: near-identical (perceptual hash distance threshold)
    - Level 2: similar (embedding cosine similarity threshold)
    - Level 3: related (a looser embedding similarity threshold)
    Thresholds are configurable in the YAML config file.

Thumbnails
    Thumbnails are generated on demand and cached on disk under a dedicated
    directory. The cache key is the image checksum so it remains stable even if
    the image is moved to a different path.

Events (optional)
    The module includes a small SSE implementation (`EventQueue` and helpers).
    The idea is simple: background work emits events like "image ingested" or
    "processing complete", and any number of subscribers can stream them.

-------------------------------------------------------------------------------
How the module is structured
-------------------------------------------------------------------------------

The module is laid out in sections separated by banners. Roughly:

1) Database schema and initialisation
    - SQL DDL strings and `init_database()` which enables WAL mode, creates
      tables/indexes, and applies lightweight migrations.

2) Folder management and scanning
    - Canonical path handling.
    - Folder registration helpers.
    - A scanner that walks registered folders and queues discovered image paths.

3) Image CRUD helpers
    - Thin helpers that read/write dictionaries to/from the `images` table.
    - Soft delete is supported (mark rows as deleted) with an option to delete
      from disk and/or hard-delete the row.

4) Metadata extraction
    - Image dimension, checksum, perceptual hash, and sharpness computation.
    - Delegates timestamp extraction to `timestamps.py`.

5) Ingestion thread
    - Consumes file paths, extracts metadata, writes rows, and queues image IDs
      for embedding.

6) Embedding thread (OpenCLIP)
    - Batches queued image IDs, computes embeddings, stores results in a single
      executemany+commit per batch.

7) Semantic search
    - `semantic_search()` compares a query embedding with stored embeddings.
    - `get_images_by_similarity()` compares one image to all others.
    - Cosine similarity via dot product (vectors are pre-normalised).

8) Thumbnail generation (stubs)
    - Database-dependent thumbnail helpers. Most thumbnail logic lives in
      `thumbnails.py`.

9) Event queue and SSE
    - `Event`, `EventQueue`, and `create_sse_generator()`.

10) `ImageDatabase` public API wrapper
    - Owns a single SQLite connection, queues, and thread control events.
    - Provides methods for external callers: folder management, image CRUD,
      thumbnail retrieval, semantic search, duplicate groups, stats, and SSE.

11) Graceful shutdown helpers
    - Signal handlers and a context manager to ensure threads stop and the DB is
      closed on exit.

Configuration is loaded from `config.py`, timestamps from `timestamps.py`,
thumbnails from `thumbnails.py`, and face detection/recognition from `faces.py`.

-------------------------------------------------------------------------------
Threading and safety notes
-------------------------------------------------------------------------------

- Three worker threads run by default: ingestion, embedding, and face detection.
- Work is coordinated through `queue.Queue` instances.
- The database connection is shared and protected by `threading.RLock`.
- Embedding and face detection threads yield the GIL periodically (10ms sleep
  between batches) to prevent blocking Flask request handling.
- "Up to date" means all queues are empty, not necessarily that the filesystem
  will never change. Rescans can be queued explicitly.

"""

# =============================================================================
# IMPORTS
# =============================================================================

from __future__ import annotations

# Set HuggingFace Hub to offline mode - models must be pre-downloaded.
# Use download_models.py to download required models before first run.
import os

os.environ['HF_HUB_OFFLINE'] = '1'

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageFile

# Tolerate truncated or mildly corrupt images rather than raising errors.
# Many real-world JPEGs are missing their EOI marker or have minor structural
# issues but are perfectly viewable in normal image viewers.
ImageFile.LOAD_TRUNCATED_IMAGES = True
import atexit
import hashlib
import json
import logging
import queue
import signal
import sqlite3
import threading
import time
import uuid
import warnings
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterator

import cv2
import imagehash
import numpy as np
import open_clip
import torch

# Local imports
from config import Config, get_default_config, load_config
from duplicates import DuplicateManager, embedding_to_numpy
from faces import (
    FaceDetector,
    compute_unknown_face_groups,
    create_face,
    delete_face_thumbnail,
    delete_people_without_faces,
    find_best_match,
    generate_face_thumbnail,
    generate_face_thumbnails_for_image,
    get_all_faces_for_thumbnail_regen,
    get_all_known_face_embeddings,
    get_face,
    get_face_thumbnail_path,
    get_faces_for_image,
    get_faces_without_semantic_embedding,
    get_group_computation_status,
    has_faces_detected,
    init_face_tables,
    mark_no_faces_detected,
    rotate_faces_for_image,
    update_face_semantic_embedding,
)
from metadata import (
    CONFIDENCE_UNKNOWN,
    derive_timestamp,
    derive_timestamp_with_confidence,
    extract_exif_data,
)
from rawimage import (
    RAW_EXTENSIONS,
    get_raw_dimensions,
    is_raw_format,
)
from rawimage import (
    open_image as raw_open_image,
)
from rawimage import (
    open_image_as_numpy as raw_open_image_as_numpy,
)
from thumbnails import (
    delete_thumbnails_for_checksum,
    generate_thumbnail,
    get_thumbnail_cache_path,
    rotate_image_file,
)
from trash import move_to_trash, validate_trash_dir

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# DATABASE SCHEMA AND INITIALISATION
# =============================================================================

# SQL schema for the folders table
_SQL_CREATE_FOLDERS = """
CREATE TABLE IF NOT EXISTS folders (
    path TEXT PRIMARY KEY
)
"""

# SQL schema for the images table
_SQL_CREATE_IMAGES = """
CREATE TABLE IF NOT EXISTS images (
    id                    TEXT PRIMARY KEY,
    path                  TEXT UNIQUE NOT NULL,
    basename              TEXT NOT NULL,
    size                  INTEGER NOT NULL,
    width                 INTEGER NOT NULL,
    height                INTEGER NOT NULL,
    timestamp             TEXT,
    timestamp_confidence  INTEGER NOT NULL DEFAULT 4,
    checksum              TEXT,
    perceptual_hash       TEXT,
    laplacian_var         REAL,
    lossless              INTEGER NOT NULL DEFAULT 0,
    mtime                 REAL,
    description           TEXT NOT NULL DEFAULT '',
    rating                TEXT NOT NULL DEFAULT '',
    aesthetic_laion        REAL,
    aesthetic_nima         REAL,
    exif_data             TEXT,
    embedding             BLOB,
    description_embedding BLOB,
    deleted               INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""

# Schema-only migrations for existing databases.
#
# IMPORTANT: These are LOW-LEVEL DDL statements (ALTER TABLE / CREATE TABLE)
# that modify the database schema idempotently.  They run silently on every
# startup — errors from "column already exists" are swallowed.
#
# Schema changes alone are NOT sufficient for a complete migration.  Each entry
# here MUST have a corresponding _migrate_*() or _backfill_*() method in the
# ImageDatabase class that:
#   1. Uses has_migration_run() / record_migration() to run exactly once
#   2. Logs what it's doing so the user can see the migration happened
#   3. Handles data backfill if the new column needs populating
#
# The mapping is documented below.  When adding new entries, follow the
# established pattern — don't just add an ALTER TABLE here and call it done.
_SQL_MIGRATIONS = [
    # → _backfill_description_embeddings() (query-based idempotency, re-checks each startup)
    'ALTER TABLE images ADD COLUMN description_embedding BLOB',
    # → backfilled inline in _process_image() during scan (per-image, no startup migration)
    'ALTER TABLE images ADD COLUMN mtime REAL',
    # → _migrate_duplicate_epoch_to_metadata()
    'ALTER TABLE duplicate_groups ADD COLUMN updated_at TEXT',
    # → _migrate_add_timestamp_confidence()
    'ALTER TABLE images ADD COLUMN timestamp_confidence INTEGER NOT NULL DEFAULT 4',
    # → _backfill_aesthetic_laion()
    'ALTER TABLE images ADD COLUMN aesthetic_laion REAL',
    # → _queue_images_for_nima() (queue-based, re-checks each startup)
    'ALTER TABLE images ADD COLUMN aesthetic_nima REAL',
    # → _migrate_renumber_custom_groups_to_level5(), _migrate_initial_directory_groups()
    'ALTER TABLE custom_groups ADD COLUMN source_path TEXT',
    # → _migrate_add_exif_metadata()
    'ALTER TABLE images ADD COLUMN exif_data TEXT',
    # → No backfill needed (NULL = regular custom group, non-NULL = smart group)
    'ALTER TABLE custom_groups ADD COLUMN filter_json TEXT',
    # → No backfill needed (smart group thumbnail, computed by frontend)
    'ALTER TABLE custom_groups ADD COLUMN preview_image_id TEXT',
    # → No backfill needed (0 = healthy, 1 = references deleted person)
    'ALTER TABLE custom_groups ADD COLUMN damaged INTEGER DEFAULT 0',
    # → No backfill needed (NULL for non-imported images, set by ImportWorker)
    'ALTER TABLE images ADD COLUMN import_name TEXT',
]

# SQL schema for the image_metadata table (indexed EXIF key-value pairs for search)
_SQL_CREATE_IMAGE_METADATA = """
CREATE TABLE IF NOT EXISTS image_metadata (
    image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (image_id, key)
)
"""

# SQL schema for the duplicate_groups table
_SQL_CREATE_DUPLICATE_GROUPS = """
CREATE TABLE IF NOT EXISTS duplicate_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       INTEGER NOT NULL,
    group_hash  TEXT NOT NULL,
    image_id    TEXT NOT NULL,
    updated_at  TEXT,
    FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
)
"""

# SQL schema for custom/directory groups metadata
_SQL_CREATE_CUSTOM_GROUPS = """
CREATE TABLE IF NOT EXISTS custom_groups (
    group_hash  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    source_path TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
)
"""

_SQL_CREATE_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS migrations (
    id          TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
)
"""

# SQL schema for storing app metadata (key-value pairs)
_SQL_CREATE_METADATA = """
CREATE TABLE IF NOT EXISTS metadata (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
)
"""

# Index definitions for performance
_SQL_CREATE_INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_images_path ON images(path)',
    'CREATE INDEX IF NOT EXISTS idx_images_checksum ON images(checksum)',
    'CREATE INDEX IF NOT EXISTS idx_images_perceptual_hash ON images(perceptual_hash)',
    'CREATE INDEX IF NOT EXISTS idx_images_deleted ON images(deleted)',
    'CREATE INDEX IF NOT EXISTS idx_images_timestamp ON images(timestamp)',
    # Composite index for efficient gallery listing (covers WHERE deleted=0 ORDER BY timestamp DESC)
    'CREATE INDEX IF NOT EXISTS idx_images_deleted_timestamp ON images(deleted, timestamp DESC)',
    'CREATE INDEX IF NOT EXISTS idx_dup_level_group ON duplicate_groups(level, group_hash)',
    'CREATE INDEX IF NOT EXISTS idx_dup_updated_at ON duplicate_groups(updated_at)',
    # Index for cascade deletes when an image is removed
    'CREATE INDEX IF NOT EXISTS idx_dup_image_id ON duplicate_groups(image_id)',
    # Index for custom group name lookups
    'CREATE INDEX IF NOT EXISTS idx_custom_groups_name ON custom_groups(name)',
    # Unique index for directory group source paths (partial: only non-NULL)
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_custom_groups_source_path'
    ' ON custom_groups(source_path) WHERE source_path IS NOT NULL',
    # Index for searching metadata by key+value (e.g. Camera = 'Nikon D850')
    'CREATE INDEX IF NOT EXISTS idx_image_metadata_key_value ON image_metadata(key, value COLLATE NOCASE)',
]


def init_database(db_path: Path | str) -> sqlite3.Connection:
    """Initialise the SQLite database with schema and WAL mode.

    Creates the database file if it doesn't exist, sets up WAL mode for
    concurrent access, creates all tables if they don't exist, and creates
    indexes for query performance.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Open database connection configured for use.

    Raises:
        sqlite3.Error: If database initialisation fails.
    """
    db_path = Path(db_path)
    logger.info(f'Initialising database: {db_path}')

    # Ensure parent directory exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Connect with settings for multi-threaded access
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        timeout=5.0,
    )

    # Enable WAL mode for better concurrent read/write performance
    conn.execute('PRAGMA journal_mode=WAL')

    # Enable foreign key constraints
    conn.execute('PRAGMA foreign_keys=ON')

    # Set busy timeout (milliseconds) for lock contention
    conn.execute('PRAGMA busy_timeout=5000')

    # Increase cache size to 100MB (default is 2MB) for better read performance
    # Negative value = kibibytes, so -102400 = 100 MB
    conn.execute('PRAGMA cache_size=-102400')

    # Use Row factory for dict-like access to rows
    conn.row_factory = sqlite3.Row

    # Create tables
    conn.execute(_SQL_CREATE_FOLDERS)
    conn.execute(_SQL_CREATE_IMAGES)
    conn.execute(_SQL_CREATE_DUPLICATE_GROUPS)
    conn.execute(_SQL_CREATE_CUSTOM_GROUPS)
    conn.execute(_SQL_CREATE_MIGRATIONS)
    conn.execute(_SQL_CREATE_METADATA)

    # Create image metadata table (EXIF key-value pairs for search)
    conn.execute(_SQL_CREATE_IMAGE_METADATA)

    # Create face recognition tables
    init_face_tables(conn)

    # Run migrations for existing databases (must run BEFORE indexes).
    # Log before executing so that if something goes wrong the user can
    # see which migration was being attempted.
    for migration_sql in _SQL_MIGRATIONS:
        trimmed = migration_sql.strip()
        try:
            logger.debug(f'    Checking migration: {trimmed}')
            conn.execute(migration_sql)
            logger.info(f'    Schema migration applied: {trimmed}')
        except sqlite3.OperationalError:
            # Column/table already exists, ignore
            pass

    # Create indexes (after migrations so new columns exist)
    for index_sql in _SQL_CREATE_INDEXES:
        try:
            conn.execute(index_sql)
        except sqlite3.OperationalError:
            # Index already exists, ignore
            pass

    conn.commit()

    logger.info('Database initialisation complete')
    return conn


def has_migration_run(conn: sqlite3.Connection, migration_id: str) -> bool:
    """Check if a one-time migration has already been applied.

    Args:
        conn: Database connection.
        migration_id: Unique identifier for the migration.

    Returns:
        True if migration has been applied, False otherwise.
    """
    cursor = conn.execute('SELECT 1 FROM migrations WHERE id = ?', (migration_id,))
    return cursor.fetchone() is not None


def record_migration(conn: sqlite3.Connection, migration_id: str) -> None:
    """Record that a one-time migration has been applied.

    Args:
        conn: Database connection.
        migration_id: Unique identifier for the migration.
    """
    conn.execute('INSERT OR REPLACE INTO migrations (id, applied_at) VALUES (?, datetime("now"))', (migration_id,))
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a sqlite3.Row to a dictionary.

    Args:
        row: SQLite row object, or None.

    Returns:
        Dictionary with column names as keys, or None if row is None.
    """
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of sqlite3.Row objects to dictionaries.

    Args:
        rows: List of SQLite row objects.

    Returns:
        List of dictionaries with column names as keys.
    """
    return [dict(row) for row in rows]


# =============================================================================
# FOLDER MANAGEMENT
# =============================================================================


def canonicalise_path(path: Path | str) -> Path:
    """Canonicalise a path to an absolute, resolved form.

    Resolves symlinks, normalises case (on case-insensitive filesystems),
    and converts to absolute path.

    Args:
        path: Path to canonicalise.

    Returns:
        Canonicalised Path object.
    """
    return Path(path).resolve()


def folder_path_upper_bound(folder_path: str) -> str:
    """Get the exclusive upper bound for a folder path range query.

    For efficient folder-based queries, use range comparisons instead of LIKE:
        WHERE path >= folder_path AND path < folder_path_upper_bound(folder_path)

    This allows SQLite to use the index on the path column, whereas
    LIKE with a prefix wildcard (path LIKE folder || '%') cannot use indexes.

    Args:
        folder_path: Folder path (should end with '/' for correct behavior).

    Returns:
        Upper bound string for exclusive comparison.

    Example:
        folder_path_upper_bound('/photos/2024')  # Returns '/photos/2024/~'
        # Query: WHERE path >= '/photos/2024' AND path < '/photos/2024/~'
    """
    # Append the path separator then '~' (ASCII 126, higher than all typical
    # filename characters).  The separator is critical: without it, a folder
    # like /photos would also match /photography because both are less than
    # /photos~ -- the separator ensures only children are matched.
    return folder_path + os.sep + '~'


def get_folders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all registered folders with their image counts.

    Args:
        conn: Database connection.

    Returns:
        List of folder dictionaries, each containing:
            - path: Folder path string
            - count: Number of non-deleted images from this folder
    """
    # Query folders with count of non-deleted images whose path starts with folder path
    # Use range query instead of LIKE for index efficiency (see folder_path_upper_bound)
    # The separator before '~' prevents /photos from matching /photography
    sep_tilde = os.sep + '~'
    cursor = conn.execute(
        """
        SELECT
            f.path,
            COUNT(i.id) as count
        FROM folders f
        LEFT JOIN images i ON i.path >= f.path AND i.path < f.path || ? AND i.deleted = 0
        GROUP BY f.path
        ORDER BY f.path
    """,
        (sep_tilde,),
    )
    rows = cursor.fetchall()

    return [{'path': row['path'], 'count': row['count']} for row in rows]


def add_folder(
    conn: sqlite3.Connection,
    path: Path | str,
) -> dict[str, Any] | None:
    """Register a new image source folder.

    Adds the folder to the database if not already registered. Does not
    scan the folder for images — the caller is responsible for triggering
    a filesystem scan and queuing images for ingestion separately.

    Args:
        conn: Database connection.
        path: Absolute path to the folder to register.

    Returns:
        Dictionary with folder info {'path': str, 'count': int},
        or None if folder already registered.

    Raises:
        ValueError: If path is not absolute, doesn't exist, or is not a directory.
    """
    path = canonicalise_path(path)

    # Validate path
    if not path.is_absolute():
        raise ValueError(f'Path must be absolute: {path}')
    if not path.exists():
        raise ValueError(f'Path does not exist: {path}')
    if not path.is_dir():
        raise ValueError(f'Path is not a directory: {path}')

    path_str = str(path)

    # Check if already registered
    cursor = conn.execute('SELECT path FROM folders WHERE path = ?', (path_str,))
    if cursor.fetchone() is not None:
        logger.info(f'Folder already registered: {path}')
        return None

    # Insert folder
    conn.execute('INSERT INTO folders (path) VALUES (?)', (path_str,))
    conn.commit()

    logger.info(f'Registered folder: {path}')

    return {
        'path': path_str,
        'count': 0,  # No images ingested yet
    }


def remove_folder(conn: sqlite3.Connection, path: Path | str) -> bool:
    """Remove a folder and mark its orphaned images as deleted.

    Removes the folder registration and marks any images that are no longer
    within any registered folder as deleted (soft delete).

    Args:
        conn: Database connection.
        path: Path of the folder to remove.

    Returns:
        True if folder was removed, False if folder was not registered.
    """
    path = canonicalise_path(path)
    path_str = str(path)

    # Check if folder exists in database
    cursor = conn.execute('SELECT path FROM folders WHERE path = ?', (path_str,))
    if cursor.fetchone() is None:
        logger.warning(f'Folder not registered: {path}')
        return False

    # Remove folder from database
    conn.execute('DELETE FROM folders WHERE path = ?', (path_str,))

    # Get all remaining registered folders
    cursor = conn.execute('SELECT path FROM folders')
    remaining_folders = [row['path'] for row in cursor.fetchall()]

    # Mark images as deleted if they're not within any remaining folder
    if remaining_folders:
        # Build query to find images not in any remaining folder
        # An image is orphaned if its path doesn't start with any remaining folder path
        # Use range queries instead of LIKE for index efficiency
        conditions = ' AND '.join(['NOT (path >= ? AND path < ?)'] * len(remaining_folders))
        # Build params: each folder needs (folder_path, folder_path + '~')
        params = [datetime.now().isoformat()]
        for folder in remaining_folders:
            params.extend([folder, folder_path_upper_bound(folder)])
        conn.execute(f'UPDATE images SET deleted = 1, updated_at = ? WHERE {conditions}', params)
    else:
        # No folders left, mark all images as deleted
        conn.execute('UPDATE images SET deleted = 1, updated_at = ? WHERE deleted = 0', (datetime.now().isoformat(),))

    conn.commit()

    logger.info(f'Removed folder: {path}')
    return True


def folder_contains_path(folder: Path | str, file_path: Path | str) -> bool:
    """Check if a file path is within a folder.

    Args:
        folder: Folder path.
        file_path: File path to check.

    Returns:
        True if file_path is within folder (or is folder itself).
    """
    folder = canonicalise_path(folder)
    file_path = canonicalise_path(file_path)

    try:
        file_path.relative_to(folder)
        return True
    except ValueError:
        return False


def find_images_in_folder(
    folder: Path | str,
    extensions: set[str],
    registered_folders: list[str] | None = None,
) -> Iterator[Path]:
    """Recursively find all image files in a folder.

    Args:
        folder: Folder to search.
        extensions: Set of lowercase file extensions to match (e.g., {'.jpg', '.png'}).
        registered_folders: Optional list of other registered folder paths.
            Subdirectories that are separately registered will be skipped
            to avoid redundant scanning.

    Yields:
        Path objects for each image file found.
    """
    folder = canonicalise_path(folder)
    folder_str = str(folder)

    if registered_folders is None:
        registered_folders = []

    # Filter out the current folder from the list to avoid self-skip
    other_folders = [f for f in registered_folders if f != folder_str]

    for root, dirs, files in os.walk(folder):
        root_path = Path(root)

        # Skip hidden directories (starting with '.') — these are system
        # or application dirs like .import-staging, .git, .thumbnails that
        # should never be indexed.  Also skip subdirectories that are
        # separately registered (optimisation).
        # Modify dirs in-place to prevent os.walk from descending.
        dirs_to_remove = []
        for d in dirs:
            if d.startswith('.'):
                dirs_to_remove.append(d)
                continue
            subdir = str(root_path / d)
            if subdir in other_folders:
                dirs_to_remove.append(d)
                logger.debug(f'Skipping separately registered subfolder: {subdir}')
        for d in dirs_to_remove:
            dirs.remove(d)

        # Yield image files
        for filename in files:
            ext = Path(filename).suffix.lower()
            if ext in extensions:
                yield root_path / filename


def verify_folders_exist(conn: sqlite3.Connection) -> list[str]:
    """Verify that all registered folders still exist on disk.

    Args:
        conn: Database connection.

    Returns:
        List of folder paths that no longer exist.
    """
    cursor = conn.execute('SELECT path FROM folders')
    missing = []

    for row in cursor.fetchall():
        path = Path(row['path'])
        if not path.exists() or not path.is_dir():
            missing.append(row['path'])
            logger.warning(f'Registered folder no longer exists: {path}')

    return missing


# =============================================================================
# IMAGE CRUD OPERATIONS
# =============================================================================


def get_all_images(
    conn: sqlite3.Connection,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Get all images from the database.

    Args:
        conn: Database connection.
        include_deleted: If True, include soft-deleted images. Defaults to False.

    Returns:
        List of image dictionaries with all metadata fields.
    """
    if include_deleted:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   timestamp_confidence, checksum, perceptual_hash, laplacian_var,
                   lossless, description, rating, deleted, created_at, updated_at,
                   mtime
            FROM images
            ORDER BY timestamp DESC, path ASC
        """)
    else:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   timestamp_confidence, checksum, perceptual_hash, laplacian_var,
                   lossless, description, rating, deleted, created_at, updated_at,
                   mtime
            FROM images
            WHERE deleted = 0
            ORDER BY timestamp DESC, path ASC
        """)

    return rows_to_dicts(cursor.fetchall())


def get_all_images_lightweight(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all images with minimal fields for gallery grid display.

    Returns only the fields needed for rendering the thumbnail grid and
    basic filtering/sorting. Use get_image() to fetch full details for
    a specific image.

    Args:
        conn: Database connection.

    Returns:
        List of image dictionaries with minimal fields:
        id, path, basename, size, width, height, timestamp, timestamp_confidence,
        rating, description, aesthetic_laion, aesthetic_nima, laplacian_var.
    """
    cursor = conn.execute("""
        SELECT id, path, basename, size, width, height, timestamp, timestamp_confidence,
               rating, description, aesthetic_laion, aesthetic_nima, laplacian_var
        FROM images
        WHERE deleted = 0
        ORDER BY timestamp DESC
    """)

    return rows_to_dicts(cursor.fetchall())


def get_images_for_thumbnail_generation(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Get images with fields needed for bulk thumbnail generation.

    Returns id, basename, path, and checksum for all non-deleted images
    that have a checksum. Used by the --generate-thumbnails CLI command.

    Args:
        conn: Database connection.

    Returns:
        List of image dictionaries with: id, basename, path, checksum.
    """
    cursor = conn.execute("""
        SELECT id, basename, path, checksum
        FROM images
        WHERE deleted = 0 AND checksum IS NOT NULL
        ORDER BY path ASC
    """)

    return rows_to_dicts(cursor.fetchall())


def get_images_delta(
    conn: sqlite3.Connection,
    since: str,
) -> dict[str, Any]:
    """Get image changes since a given timestamp for incremental updates.

    Returns images that have been added, updated, or deleted since the
    specified timestamp. The frontend can use this to efficiently update
    its cache without fetching all images.

    Args:
        conn: Database connection.
        since: ISO timestamp string. Only changes after this time are returned.

    Returns:
        Dictionary with:
        - epoch: Current max updated_at timestamp (for next delta request)
        - updated: List of added/modified images (lightweight fields including
          aesthetic_laion, aesthetic_nima, laplacian_var, + deleted flag)
        - deleted_ids: List of IDs for images that are now deleted
    """
    # Get current epoch (max updated_at)
    epoch_cursor = conn.execute('SELECT MAX(updated_at) as epoch FROM images')
    epoch_row = epoch_cursor.fetchone()
    current_epoch = epoch_row['epoch'] if epoch_row and epoch_row['epoch'] else since

    # Get all images changed since the given timestamp
    cursor = conn.execute(
        """
        SELECT id, path, basename, size, width, height, timestamp, timestamp_confidence,
               rating, description, aesthetic_laion, aesthetic_nima, laplacian_var,
               deleted, updated_at
        FROM images
        WHERE updated_at > ?
        ORDER BY updated_at ASC
    """,
        (since,),
    )

    updated = []
    deleted_ids = []

    for row in cursor.fetchall():
        img = dict(row)
        if img['deleted']:
            deleted_ids.append(img['id'])
        else:
            # Remove internal fields from response
            del img['deleted']
            del img['updated_at']
            updated.append(img)

    return {
        'epoch': current_epoch,
        'updated': updated,
        'deleted_ids': deleted_ids,
    }


def get_current_epoch(conn: sqlite3.Connection) -> str | None:
    """Get the current epoch (max updated_at timestamp).

    Args:
        conn: Database connection.

    Returns:
        ISO timestamp string of the most recent update, or None if no images.
    """
    cursor = conn.execute('SELECT MAX(updated_at) as epoch FROM images')
    row = cursor.fetchone()
    return row['epoch'] if row else None


def get_image(conn: sqlite3.Connection, image_id: str) -> dict[str, Any] | None:
    """Get a single image by ID.

    Does NOT include exif_data — use get_image_exif() for that (lazy-loaded
    separately to avoid slowing down the info panel and fullscreen viewer).

    Args:
        conn: Database connection.
        image_id: UUID of the image.

    Returns:
        Image dictionary with all metadata fields, or None if not found.
    """
    cursor = conn.execute(
        """
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating, deleted, created_at, updated_at,
               mtime
        FROM images
        WHERE id = ?
    """,
        (image_id,),
    )

    row = cursor.fetchone()
    if row is None:
        return None

    return row_to_dict(row)


def get_image_exif(conn: sqlite3.Connection, image_id: str) -> dict[str, str] | None:
    """Get parsed EXIF metadata for a single image.

    Returns the exif_data JSON blob parsed into a dict, or None if no EXIF
    data is available. Loaded lazily by the frontend when the metadata modal
    opens, to avoid including it in every image response.

    Args:
        conn: Database connection.
        image_id: UUID of the image.

    Returns:
        Dictionary of EXIF key-value pairs, or None.
    """
    cursor = conn.execute('SELECT exif_data FROM images WHERE id = ? AND deleted = 0', (image_id,))
    row = cursor.fetchone()
    if row is None:
        return None

    exif_raw = row[0]
    if exif_raw and isinstance(exif_raw, str):
        try:
            parsed = json.loads(exif_raw)
            # Return None for empty dicts (image had no EXIF)
            return parsed if parsed else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def get_image_thumbnail_info(
    conn: sqlite3.Connection,
    image_id: str,
) -> tuple[str, str] | None:
    """Get checksum and path for an image (for thumbnail lookup).

    Lightweight query that only fetches the fields needed for thumbnail
    generation/lookup.

    Args:
        conn: Database connection.
        image_id: UUID of the image.

    Returns:
        Tuple of (checksum, path) or None if not found or no checksum.
    """
    cursor = conn.execute('SELECT checksum, path FROM images WHERE id = ? AND deleted = 0', (image_id,))
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    return (row[0], row[1])


def get_image_by_path(conn: sqlite3.Connection, path: Path | str) -> dict[str, Any] | None:
    """Get a single image by file path.

    Args:
        conn: Database connection.
        path: File path of the image.

    Returns:
        Image dictionary with all metadata fields, or None if not found.
    """
    path_str = str(canonicalise_path(path))

    cursor = conn.execute(
        """
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating, embedding, description_embedding,
               deleted, created_at, updated_at, mtime, aesthetic_nima, exif_data
        FROM images
        WHERE path = ?
    """,
        (path_str,),
    )

    return row_to_dict(cursor.fetchone())


def create_image(
    conn: sqlite3.Connection,
    image_id: str,
    path: Path | str,
    size: int,
    width: int,
    height: int,
    timestamp: datetime | None = None,
    timestamp_confidence: int = CONFIDENCE_UNKNOWN,
    checksum: str | None = None,
    perceptual_hash: str | None = None,
    laplacian_var: float | None = None,
    lossless: bool = False,
    mtime: float | None = None,
    description: str = '',
    rating: str = '',
    exif_data: dict[str, str] | None = None,
    import_name: str | None = None,
) -> dict[str, Any]:
    """Create a new image record in the database.

    Args:
        conn: Database connection.
        image_id: UUID for the new image.
        path: Absolute file path to the image.
        size: File size in bytes.
        width: Image width in pixels.
        height: Image height in pixels.
        timestamp: Image timestamp (from EXIF, filesystem, or filename).
        timestamp_confidence: Confidence level (0=user, 1=EXIF, 2=filename, 3=FS, 4=unknown).
        checksum: SHA256 hex digest of file contents.
        perceptual_hash: Perceptual hash hex string.
        laplacian_var: Laplacian variance (focus score).
        lossless: Whether the image format is lossless.
        mtime: File modification time (Unix timestamp).
        description: User description (default empty).
        rating: User rating emoji string (default empty).
        exif_data: Normalised EXIF key-value pairs (stored as JSON blob and
            indexed in image_metadata table for search).
        import_name: Original filename at time of import (before collision
            renaming).  NULL for non-imported images.  Used by the preflight
            dedup endpoint so clients can match files by their original name
            even when the catalogue copy was renamed to avoid a collision.

    Returns:
        Dictionary with the created image record.

    Raises:
        sqlite3.IntegrityError: If image with same ID or path already exists.
    """
    path = canonicalise_path(path)
    path_str = str(path)
    basename = path.name
    now = datetime.now().isoformat()
    timestamp_str = timestamp.isoformat() if timestamp else None
    exif_json = json.dumps(exif_data) if exif_data else None

    conn.execute(
        """
        INSERT INTO images (
            id, path, basename, size, width, height, timestamp, timestamp_confidence,
            checksum, perceptual_hash, laplacian_var, lossless, mtime,
            description, rating, embedding, deleted, created_at, updated_at,
            exif_data, import_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, ?)
    """,
        (
            image_id,
            path_str,
            basename,
            size,
            width,
            height,
            timestamp_str,
            timestamp_confidence,
            checksum,
            perceptual_hash,
            laplacian_var,
            int(lossless),
            mtime,
            description,
            rating,
            now,
            now,
            exif_json,
            import_name,
        ),
    )

    # Populate indexed metadata table for search
    if exif_data:
        _upsert_image_metadata(conn, image_id, exif_data)

    conn.commit()

    logger.debug(f'Created image record: {image_id} ({path})')

    return {
        'id': image_id,
        'path': path_str,
        'basename': basename,
        'size': size,
        'width': width,
        'height': height,
        'timestamp': timestamp_str,
        'timestamp_confidence': timestamp_confidence,
        'checksum': checksum,
        'perceptual_hash': perceptual_hash,
        'laplacian_var': laplacian_var,
        'lossless': lossless,
        'mtime': mtime,
        'description': description,
        'rating': rating,
        'deleted': 0,
        'created_at': now,
        'updated_at': now,
    }


def update_image(
    conn: sqlite3.Connection,
    image_id: str,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Update an image record with new data.

    Only allows updating user-editable fields (description, rating) and
    computed metadata fields. Cannot change id, path, or timestamps directly.

    Args:
        conn: Database connection.
        image_id: UUID of the image to update.
        data: Dictionary of fields to update. Allowed fields:
            - description: User description text
            - rating: User rating emoji string
            - size, width, height: Dimensions (for re-ingestion)
            - timestamp: Image timestamp
            - timestamp_confidence: Confidence level (0=user, 1=EXIF, 2=filename, 3=FS, 4=unknown)
            - checksum: SHA256 hash
            - perceptual_hash: Perceptual hash
            - laplacian_var: Focus score
            - lossless: Lossless flag
            - embedding: OpenCLIP embedding bytes
            - deleted: Soft-delete flag

    Returns:
        Updated image dictionary, or None if image not found.
    """
    # Check image exists
    existing = get_image(conn, image_id)
    if existing is None:
        return None

    # Allowed fields for update
    allowed_fields = {
        'description',
        'rating',
        'size',
        'width',
        'height',
        'timestamp',
        'timestamp_confidence',
        'checksum',
        'perceptual_hash',
        'laplacian_var',
        'lossless',
        'embedding',
        'description_embedding',
        'deleted',
    }

    # Filter to only allowed fields that are present in data
    updates = {k: v for k, v in data.items() if k in allowed_fields}

    if not updates:
        return existing  # Nothing to update

    # Handle special conversions
    if 'timestamp' in updates and isinstance(updates['timestamp'], datetime):
        updates['timestamp'] = updates['timestamp'].isoformat()
    if 'lossless' in updates:
        updates['lossless'] = int(updates['lossless'])
    if 'deleted' in updates:
        updates['deleted'] = int(updates['deleted'])

    # Build UPDATE query
    updates['updated_at'] = datetime.now().isoformat()
    set_clause = ', '.join(f'{k} = ?' for k in updates)
    values = list(updates.values()) + [image_id]

    conn.execute(f'UPDATE images SET {set_clause} WHERE id = ?', values)
    conn.commit()

    logger.debug(f'Updated image: {image_id}')

    return get_image(conn, image_id)


def update_image_metadata(
    conn: sqlite3.Connection,
    image_id: str,
    size: int,
    width: int,
    height: int,
    timestamp: datetime | None,
    timestamp_confidence: int,
    checksum: str,
    perceptual_hash: str | None,
    laplacian_var: float | None,
    lossless: bool,
    mtime: float | None = None,
    exif_data: dict[str, str] | None = None,
) -> bool:
    """Update all computed metadata fields for an image.

    Used by the ingestion thread when re-processing a changed image.
    Clears the embedding field since it needs to be recomputed.

    Args:
        conn: Database connection.
        image_id: UUID of the image.
        size: File size in bytes.
        width: Image width in pixels.
        height: Image height in pixels.
        timestamp: Image timestamp.
        timestamp_confidence: Confidence level (0=user, 1=EXIF, 2=filename, 3=FS, 4=unknown).
        checksum: SHA256 hex digest.
        perceptual_hash: Perceptual hash hex string.
        laplacian_var: Laplacian variance.
        lossless: Whether format is lossless.
        mtime: File modification time (Unix timestamp).
        exif_data: Normalised EXIF key-value pairs (stored as JSON blob and
            indexed in image_metadata table for search).

    Returns:
        True if image was updated, False if not found.
    """
    timestamp_str = timestamp.isoformat() if timestamp else None
    now = datetime.now().isoformat()
    exif_json = json.dumps(exif_data) if exif_data else None

    cursor = conn.execute(
        """
        UPDATE images SET
            size = ?,
            width = ?,
            height = ?,
            timestamp = ?,
            timestamp_confidence = ?,
            checksum = ?,
            perceptual_hash = ?,
            laplacian_var = ?,
            lossless = ?,
            mtime = ?,
            exif_data = ?,
            embedding = NULL,
            updated_at = ?
        WHERE id = ?
    """,
        (
            size,
            width,
            height,
            timestamp_str,
            timestamp_confidence,
            checksum,
            perceptual_hash,
            laplacian_var,
            int(lossless),
            mtime,
            exif_json,
            now,
            image_id,
        ),
    )

    # Update indexed metadata table for search
    if exif_data is not None:
        _upsert_image_metadata(conn, image_id, exif_data)

    conn.commit()

    if cursor.rowcount > 0:
        logger.debug(f'Updated metadata for image: {image_id}')
        return True
    return False


def _upsert_image_metadata(
    conn: sqlite3.Connection,
    image_id: str,
    exif_data: dict[str, str],
) -> None:
    """Insert or replace indexed EXIF key-value pairs for an image.

    Replaces all existing rows for this image with the new data. This
    is simpler and safer than per-key upserts when metadata changes.

    Args:
        conn: Database connection.
        image_id: UUID of the image.
        exif_data: Dictionary of normalised EXIF key-value pairs.
    """
    # Delete old rows, then insert new ones (atomic within caller's commit)
    conn.execute('DELETE FROM image_metadata WHERE image_id = ?', (image_id,))
    if exif_data:
        conn.executemany(
            'INSERT INTO image_metadata (image_id, key, value) VALUES (?, ?, ?)',
            [(image_id, k, v) for k, v in exif_data.items()],
        )


def search_image_metadata(
    conn: sqlite3.Connection,
    criteria: dict[str, str],
) -> list[str]:
    """Search for images matching EXIF metadata criteria.

    Uses subsequence matching on the image_metadata table: each character
    in the query must appear in order within the value (case-insensitive).
    Multiple criteria are ANDed together.

    Args:
        conn: Database connection.
        criteria: Dictionary of {key: query_text} pairs. Each query uses
            subsequence matching against the stored values.

    Returns:
        List of image IDs matching ALL criteria.
    """
    if not criteria:
        return []

    result_sets: list[set[str]] = []

    for key, query in criteria.items():
        if not query:
            continue

        # Build SQL LIKE pattern for subsequence matching:
        # "nkn" → "%n%k%n%" (each char must appear in order)
        like_parts = ['%']
        for ch in query:
            # Escape SQL LIKE special chars
            if ch in ('%', '_', '\\'):
                like_parts.append(f'\\{ch}')
            else:
                like_parts.append(ch)
            like_parts.append('%')
        like_pattern = ''.join(like_parts)

        cursor = conn.execute(
            """SELECT image_id FROM image_metadata
               WHERE key = ? AND value LIKE ? ESCAPE '\\'""",
            (key, like_pattern),
        )
        ids = {row[0] for row in cursor.fetchall()}
        result_sets.append(ids)

    if not result_sets:
        return []

    # AND logic: intersect all result sets
    intersection = result_sets[0]
    for s in result_sets[1:]:
        intersection &= s

    return list(intersection)


def get_metadata_keys(conn: sqlite3.Connection) -> list[str]:
    """Get all distinct metadata keys present in the database.

    Used to populate the writable metadata filter modal with available
    keys the user can search by.

    Args:
        conn: Database connection.

    Returns:
        Sorted list of distinct key names (e.g. ['Aperture', 'Camera', ...]).
    """
    cursor = conn.execute('SELECT DISTINCT key FROM image_metadata ORDER BY key')
    return [row[0] for row in cursor.fetchall()]


def get_metadata_values(
    conn: sqlite3.Connection,
    key: str,
) -> list[str]:
    """Get all distinct values for a given metadata key.

    Used for autocomplete dropdowns in the metadata filter modal.

    Args:
        conn: Database connection.
        key: Metadata key name (e.g. 'Camera').

    Returns:
        Sorted list of distinct values for the key.
    """
    cursor = conn.execute('SELECT DISTINCT value FROM image_metadata WHERE key = ? ORDER BY value', (key,))
    return [row[0] for row in cursor.fetchall()]


def get_images_without_exif(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all non-deleted images that don't have EXIF data extracted.

    NULL exif_data means never attempted; '{}' means attempted but no EXIF found.

    Args:
        conn: Database connection.

    Returns:
        List of image dictionaries (id and basename) missing EXIF data.
    """
    cursor = conn.execute("""
        SELECT id, basename
        FROM images
        WHERE deleted = 0 AND exif_data IS NULL
        ORDER BY created_at ASC
    """)

    return rows_to_dicts(cursor.fetchall())


def delete_image(
    conn: sqlite3.Connection,
    image_id: str,
    from_disk: bool = False,
) -> bool:
    """Delete an image from the database.

    By default, performs a soft delete (sets deleted=1). If from_disk is True,
    also deletes the file from disk and performs a hard delete from the database.

    Args:
        conn: Database connection.
        image_id: UUID of the image to delete.
        from_disk: If True, also delete the file from disk and hard delete
            from database. Defaults to False (soft delete only).

    Returns:
        True if image was deleted, False if not found.
    """
    # Get image info first
    image = get_image(conn, image_id)
    if image is None:
        return False

    if from_disk:
        # Delete file from disk
        file_path = Path(image['path'])
        if file_path.exists():
            try:
                file_path.unlink()
                logger.info(f'Deleted file from disk: {file_path}')
            except OSError as e:
                logger.error(f'Failed to delete file {file_path}: {e}')
                # Continue with database deletion anyway

        # Hard delete from database
        conn.execute('DELETE FROM images WHERE id = ?', (image_id,))
        conn.commit()
        logger.info(f'Hard deleted image: {image_id}')
    else:
        # Soft delete
        now = datetime.now().isoformat()
        conn.execute('UPDATE images SET deleted = 1, updated_at = ? WHERE id = ?', (now, image_id))
        conn.commit()
        logger.info(f'Soft deleted image: {image_id}')

    return True


def get_images_without_embedding(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all non-deleted images that don't have an embedding.

    Used to queue images for the embedding thread on startup.

    Args:
        conn: Database connection.

    Returns:
        List of image dictionaries (id and path only) missing embeddings.
    """
    cursor = conn.execute("""
        SELECT id, path
        FROM images
        WHERE deleted = 0 AND embedding IS NULL
        ORDER BY created_at ASC
    """)

    return rows_to_dicts(cursor.fetchall())


# =============================================================================
# METADATA EXTRACTION
# =============================================================================

# Lossless image formats (by extension).
# RAW files are technically lossless sensor data, so they're included here.
LOSSLESS_EXTENSIONS = {'.png', '.bmp', '.tiff', '.tif', '.gif'} | RAW_EXTENSIONS


def compute_checksum(path: Path | str, algorithm: str = 'sha256') -> str:
    """Compute a cryptographic hash of a file's contents.

    Args:
        path: Path to the file.
        algorithm: Hash algorithm name (default 'sha256').

    Returns:
        Hex digest string of the file hash.

    Raises:
        FileNotFoundError: If file doesn't exist.
        OSError: If file cannot be read.
    """
    path = Path(path)
    hasher = hashlib.new(algorithm)

    with open(path, 'rb') as f:
        # Read in chunks to handle large files
        for chunk in iter(lambda: f.read(65536), b''):
            hasher.update(chunk)

    return hasher.hexdigest()


def compute_perceptual_hash(
    path: Path | str,
    max_dimension: int = 0,
) -> str | None:
    """Compute a perceptual hash of an image.

    Uses the pHash algorithm which is robust to minor changes like
    resizing, compression, and colour adjustments.

    Args:
        path: Path to the image file.
        max_dimension: Max dimension before downsampling (0 to disable).

    Returns:
        Hex string representation of the perceptual hash,
        or None if the image cannot be processed.
    """
    try:
        img = raw_open_image(path)

        # Downsample if oversized
        if max_dimension > 0:
            w, h = img.size
            max_dim = max(w, h)
            if max_dim > max_dimension:
                scale = max_dimension / max_dim
                new_w = int(w * scale)
                new_h = int(h * scale)
                logger.info(f'Downsampling oversized image for phash {path}: {w}x{h} -> {new_w}x{new_h}')
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        phash = imagehash.phash(img)
        return str(phash)
    except Exception as e:
        logger.warning(f'Failed to compute perceptual hash for {path}: {e}')
        return None


def compute_laplacian_variance(
    path: Path | str,
    max_dimension: int = 0,
) -> float | None:
    """Compute the Laplacian variance of an image as a focus/sharpness metric.

    Higher values indicate sharper images. This metric is useful for
    detecting blurry or out-of-focus images.

    Args:
        path: Path to the image file.
        max_dimension: Max dimension before downsampling (0 to disable).

    Returns:
        Variance of the Laplacian, or None if image cannot be processed.
    """
    try:
        img = raw_open_image_as_numpy(path)
        if img is None:
            logger.warning(f'Failed to read image for Laplacian: {path}')
            return None

        # Downsample if oversized
        if max_dimension > 0:
            h, w = img.shape[:2]
            max_dim = max(w, h)
            if max_dim > max_dimension:
                scale = max_dimension / max_dim
                new_w = int(w * scale)
                new_h = int(h * scale)
                logger.info(f'Downsampling oversized image for laplacian {path}: {w}x{h} -> {new_w}x{new_h}')
                img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        laplacian = cv2.Laplacian(gray, cv2.CV_64F)
        return float(laplacian.var())
    except Exception as e:
        logger.warning(f'Failed to compute Laplacian variance for {path}: {e}')
        return None


def is_lossless_format(path: Path | str) -> bool:
    """Determine if an image file is in a lossless format.

    Based on file extension. Note that some formats like TIFF can be
    either lossless or lossy, but we assume lossless for simplicity.

    Args:
        path: Path to the image file.

    Returns:
        True if the format is typically lossless, False otherwise.
    """
    ext = Path(path).suffix.lower()
    return ext in LOSSLESS_EXTENSIONS


def get_image_dimensions(path: Path | str) -> tuple[int, int] | None:
    """Get the dimensions of an image.

    Args:
        path: Path to the image file.

    Returns:
        Tuple of (width, height) in pixels, or None if image cannot be read.
    """
    try:
        # For RAW files, read dimensions from the header without full decode
        # (much faster than demosaicing a 40MP sensor image)
        if is_raw_format(path):
            dims = get_raw_dimensions(path)
            if dims is not None:
                return dims

        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception as e:
        logger.warning(f'Failed to get dimensions for {path}: {e}')
        return None


@dataclass
class ImageMetadata:
    """Container for extracted image metadata.

    Attributes:
        path: Canonicalised file path.
        size: File size in bytes.
        mtime: File modification time (Unix timestamp).
        width: Image width in pixels.
        height: Image height in pixels.
        timestamp: Derived timestamp (may be None).
        timestamp_confidence: Confidence level (0=user, 1=EXIF, 2=filename, 3=FS, 4=unknown).
        checksum: SHA256 hex digest.
        perceptual_hash: Perceptual hash hex string (may be None).
        laplacian_var: Focus/sharpness score (may be None).
        lossless: Whether the format is lossless.
        exif_data: Normalised EXIF key-value pairs (may be None).
    """

    path: Path
    size: int
    mtime: float
    width: int
    height: int
    timestamp: datetime | None
    timestamp_confidence: int
    checksum: str
    perceptual_hash: str | None
    laplacian_var: float | None
    lossless: bool
    exif_data: dict[str, str] | None = None


def extract_image_metadata(
    path: Path | str,
    max_dimension: int = 0,
) -> ImageMetadata | None:
    """Extract all metadata from an image file.

    Args:
        path: Path to the image file.
        max_dimension: Max dimension for phash/laplacian (0 to disable).

    Returns:
        ImageMetadata object with all extracted data,
        or None if the image cannot be processed.
    """
    path = canonicalise_path(path)

    if not path.exists():
        logger.warning(f'Image file not found: {path}')
        return None

    # Get file size and modification time
    try:
        stat_info = path.stat()
        size = stat_info.st_size
        mtime = stat_info.st_mtime
    except OSError as e:
        logger.warning(f'Failed to stat file {path}: {e}')
        return None

    # Get dimensions
    dimensions = get_image_dimensions(path)
    if dimensions is None:
        logger.warning(f'Failed to read image: {path}')
        return None
    width, height = dimensions

    # Compute checksum
    try:
        checksum = compute_checksum(path)
    except OSError as e:
        logger.warning(f'Failed to compute checksum for {path}: {e}')
        return None

    # Compute perceptual hash (may fail for some images)
    perceptual_hash = compute_perceptual_hash(path, max_dimension)

    # Compute Laplacian variance (may fail for some images)
    laplacian_var = compute_laplacian_variance(path, max_dimension)

    # Extract all EXIF metadata in one pass (avoids re-opening the file for timestamp)
    exif_data = extract_exif_data(path)

    # Derive timestamp with confidence level, reusing pre-read EXIF data
    timestamp, timestamp_confidence = derive_timestamp_with_confidence(path, exif_data=exif_data)

    # Check if lossless format
    lossless = is_lossless_format(path)

    return ImageMetadata(
        path=path,
        size=size,
        mtime=mtime,
        width=width,
        height=height,
        timestamp=timestamp,
        timestamp_confidence=timestamp_confidence,
        checksum=checksum,
        perceptual_hash=perceptual_hash,
        laplacian_var=laplacian_var,
        lossless=lossless,
        exif_data=exif_data,
    )


# =============================================================================
# INGESTION THREAD
# =============================================================================


class IngestionThread(threading.Thread):
    """Background thread for ingesting images into the database.

    Processes image paths from the ingestion queue using a thread pool for
    parallel metadata extraction. Images that need embeddings are queued
    to the embedding queue.

    Attributes:
        conn: Database connection.
        ingestion_queue: Queue of file paths to process.
        embedding_queue: Queue of image IDs needing embeddings.
        stop_event: Event to signal thread shutdown.
        pause_event: Event to temporarily pause processing.
        num_threads: Number of worker threads for parallel processing.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        ingestion_queue: queue.Queue[Path],
        embedding_queue: queue.Queue[str],
        stop_event: threading.Event,
        db_lock: threading.RLock,
        checksum_cache: dict[str, str],
        checksum_cache_lock: threading.Lock,
        generate_thumbnails: Callable[[Path, str], bool],
        pause_event: threading.Event | None = None,
        num_threads: int = 4,
        max_image_dimension: int = 0,
        nima_queue: queue.Queue[str] | None = None,
        import_names: dict[str, str] | None = None,
        import_names_lock: threading.Lock | None = None,
    ):
        """Initialise the ingestion thread.

        Args:
            conn: Database connection (must be created with check_same_thread=False).
            ingestion_queue: Queue of file paths to process.
            embedding_queue: Queue to add image IDs that need embeddings.
            stop_event: Event to signal thread should stop.
            db_lock: Shared lock for database access (from ImageDatabase).
            checksum_cache: Shared cache mapping image_id to checksum.
            checksum_cache_lock: Lock for checksum cache access.
            generate_thumbnails: Callback to generate thumbnails (path, checksum) -> bool.
            pause_event: Optional event to pause processing (for folder removal).
            num_threads: Number of worker threads for parallel metadata extraction.
            max_image_dimension: Max dimension for image processing (0 to disable).
            nima_queue: Optional queue for NIMA aesthetic scoring.
            import_names: Shared dict mapping catalogue dest path to original
                filename (populated by ImportWorker, consumed here).
            import_names_lock: Lock protecting the import_names dict.
        """
        super().__init__(name='IngestionThread', daemon=True)
        self.conn = conn
        self.ingestion_queue = ingestion_queue
        self.embedding_queue = embedding_queue
        self.stop_event = stop_event
        self.pause_event = pause_event or threading.Event()
        self.num_threads = max(1, min(16, num_threads))
        self.max_image_dimension = max_image_dimension
        self._processed_count = 0
        self._error_count = 0
        self._db_lock = db_lock  # Shared lock from ImageDatabase
        self._checksum_cache = checksum_cache  # Shared cache from ImageDatabase
        self._checksum_cache_lock = checksum_cache_lock  # Shared lock from ImageDatabase
        self._generate_thumbnails = generate_thumbnails  # Callback from ImageDatabase
        self._nima_queue = nima_queue  # Optional NIMA scoring queue
        self._import_names = import_names or {}  # Shared import name mapping
        self._import_names_lock = import_names_lock or threading.Lock()
        self._pending_count = 0  # Number of items being processed (not just in queue)
        self._pending_lock = threading.Lock()

    @property
    def processed_count(self) -> int:
        """Number of images successfully processed."""
        return self._processed_count

    @property
    def error_count(self) -> int:
        """Number of images that failed processing."""
        return self._error_count

    @property
    def is_idle(self) -> bool:
        """Check if thread is idle (queue empty AND no pending work).

        This is used by EmbeddingThread to determine when all ingestion
        is truly complete, not just when the queue appears empty.
        """
        with self._pending_lock:
            return self.ingestion_queue.empty() and self._pending_count == 0

    def run(self) -> None:
        """Main thread loop - process images using thread pool."""
        logger.info(f'Ingestion thread started with {self.num_threads} worker threads')

        # Progress logging state
        last_progress_time = time.time()
        progress_interval = 5.0  # Log every 5 seconds
        initial_queue_size = self.ingestion_queue.qsize()
        if initial_queue_size > 0:
            logger.info(f'Indexing {initial_queue_size} images...')

        with ThreadPoolExecutor(max_workers=self.num_threads) as executor:
            pending_futures: dict[Future, Path] = {}

            while not self.stop_event.is_set():
                # Check if paused
                if self.pause_event.is_set():
                    time.sleep(0.1)
                    continue

                # Submit new jobs while we have capacity
                while len(pending_futures) < self.num_threads * 2:
                    try:
                        path = self.ingestion_queue.get_nowait()
                        future = executor.submit(self._process_image, path)
                        pending_futures[future] = path
                        with self._pending_lock:
                            self._pending_count += 1
                    except queue.Empty:
                        break

                # Check for completed futures
                if pending_futures:
                    done_futures = [f for f in pending_futures if f.done()]
                    for future in done_futures:
                        path = pending_futures.pop(future)
                        try:
                            future.result()  # Raises exception if worker failed
                            self._processed_count += 1
                        except Exception as e:
                            logger.error(f'Error processing {path}: {e}')
                            self._error_count += 1
                        finally:
                            self.ingestion_queue.task_done()
                            with self._pending_lock:
                                self._pending_count -= 1

                # Periodic progress logging
                now = time.time()
                if now - last_progress_time >= progress_interval:
                    remaining = self.ingestion_queue.qsize() + len(pending_futures)
                    if remaining > 0:
                        logger.info(f'Indexing progress: {self._processed_count} done, {remaining} remaining')
                    last_progress_time = now

                # Small sleep if no work to prevent busy-waiting
                if not pending_futures:
                    time.sleep(0.1)
                else:
                    # Brief sleep to allow futures to complete
                    time.sleep(0.01)

            # Wait for remaining futures on shutdown
            for future in pending_futures:
                path = pending_futures[future]
                try:
                    future.result(timeout=1.0)
                    self._processed_count += 1
                except Exception as e:
                    logger.error(f'Error processing {path} during shutdown: {e}')
                    self._error_count += 1
                finally:
                    self.ingestion_queue.task_done()
                    with self._pending_lock:
                        self._pending_count -= 1

        logger.info('Ingestion thread stopped')

    def _process_image(self, path: Path) -> None:
        """Process a single image file.

        This method is called from worker threads. Database operations are
        protected by _db_lock to ensure thread safety.

        Uses size + mtime for fast change detection to avoid reading file
        contents (checksum) on every scan.

        Args:
            path: Path to the image file.
        """
        path = canonicalise_path(path)

        # Check if file still exists (no lock needed - file I/O)
        if not path.exists():
            logger.debug(f'Skipping non-existent file: {path}')
            return

        # Get file size and mtime (no lock needed - file I/O)
        try:
            stat_info = path.stat()
            current_size = stat_info.st_size
            current_mtime = stat_info.st_mtime
        except OSError:
            logger.warning(f'Cannot stat file: {path}')
            return

        # Check if already in database (lock needed - DB read)
        with self._db_lock:
            existing = get_image_by_path(self.conn, path)

        if existing is not None:
            # Image exists - check if it has changed using size + mtime
            # This is much faster than computing checksum for every file
            existing_mtime = existing.get('mtime')
            existing_checksum = existing.get('checksum')
            existing_size = existing.get('size', 0)

            # Check if mtime is missing (pre-migration image) - need to backfill
            if existing_mtime is None and existing['size'] == current_size:
                # Size matches but no mtime stored - just update mtime without full re-process
                # NOTE: Don't update updated_at here - mtime backfill is not a content change
                logger.debug(f'Backfilling mtime for: {path}')
                with self._db_lock:
                    self.conn.execute('UPDATE images SET mtime = ? WHERE id = ?', (current_mtime, existing['id']))
                    self.conn.commit()
                existing_mtime = current_mtime  # Continue with normal checks

            if existing['size'] == current_size and existing_mtime == current_mtime:
                # File unchanged (size and mtime match)
                needs_embedding = False

                # Check if we need to backfill missing checksum
                if existing_checksum is None and existing_size > 0:
                    # Missing checksum - need to regenerate metadata
                    logger.info(f'Backfilling missing checksum for: {path}')
                    metadata = extract_image_metadata(path, self.max_image_dimension)
                    if metadata is not None:
                        with self._db_lock:
                            update_image_metadata(
                                self.conn,
                                existing['id'],
                                size=metadata.size,
                                width=metadata.width,
                                height=metadata.height,
                                timestamp=metadata.timestamp,
                                timestamp_confidence=metadata.timestamp_confidence,
                                checksum=metadata.checksum,
                                perceptual_hash=metadata.perceptual_hash,
                                laplacian_var=metadata.laplacian_var,
                                lossless=metadata.lossless,
                                mtime=metadata.mtime,
                                exif_data=metadata.exif_data,
                            )
                        needs_embedding = True

                # Check if embedding is needed
                if existing['embedding'] is None:
                    needs_embedding = True

                # Check if description embedding is needed
                description = existing.get('description', '')
                description_embedding = existing.get('description_embedding')
                if description and description_embedding is None:
                    needs_embedding = True

                if needs_embedding:
                    self.embedding_queue.put(existing['id'])
                    logger.debug(f'Queued existing image for embedding: {path}')
                # Queue for NIMA scoring if missing (independent of embedding)
                if self._nima_queue is not None and existing.get('aesthetic_nima') is None:
                    self._nima_queue.put(existing['id'])
                # Backfill EXIF metadata if missing (lightweight I/O, done inline)
                if existing.get('exif_data') is None:
                    exif_data = extract_exif_data(path)
                    exif_json = json.dumps(exif_data) if exif_data else '{}'
                    with self._db_lock:
                        self.conn.execute(
                            'UPDATE images SET exif_data = ?, updated_at = ? WHERE id = ?',
                            (exif_json, datetime.now().isoformat(), existing['id']),
                        )
                        if exif_data:
                            _upsert_image_metadata(self.conn, existing['id'], exif_data)
                        self.conn.commit()
                    logger.debug(f'Backfilled EXIF data for: {path}')
                if not needs_embedding:
                    logger.debug(f'Skipping unchanged image: {path}')
                return

            # File has changed (size or mtime differ) - re-extract metadata
            logger.info(f'Re-ingesting changed image: {path}')
            metadata = extract_image_metadata(path, self.max_image_dimension)
            if metadata is None:
                logger.warning(f'Failed to extract metadata for changed image: {path}')
                return

            # Update existing record (lock needed - DB write)
            with self._db_lock:
                update_image_metadata(
                    self.conn,
                    existing['id'],
                    size=metadata.size,
                    width=metadata.width,
                    height=metadata.height,
                    timestamp=metadata.timestamp,
                    timestamp_confidence=metadata.timestamp_confidence,
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                    mtime=metadata.mtime,
                    exif_data=metadata.exif_data,
                )

            # Update checksum cache
            if metadata.checksum:
                with self._checksum_cache_lock:
                    self._checksum_cache[existing['id']] = metadata.checksum

            # Generate thumbnail for changed image
            if metadata.checksum:
                self._generate_thumbnails(path, metadata.checksum)

            # Queue for embedding (metadata cleared embedding) and NIMA
            self.embedding_queue.put(existing['id'])
            if self._nima_queue is not None:
                self._nima_queue.put(existing['id'])
            logger.debug(f'Queued changed image for embedding: {path}')

        else:
            # New image - extract metadata (no lock - file I/O)
            metadata = extract_image_metadata(path, self.max_image_dimension)
            if metadata is None:
                logger.warning(f'Failed to extract metadata for new image: {path}')
                return

            # DESIGN: Backend generates image IDs because images are discovered via folder
            # scanning, which frontend cannot pre-generate IDs for (see design-audit.md 1.11)
            image_id = str(uuid.uuid4())

            # Check if this file was placed by ImportWorker (has an original
            # import name that may differ from the on-disk basename due to
            # collision renaming).
            path_str_canon = str(canonicalise_path(path))
            with self._import_names_lock:
                import_name = self._import_names.pop(path_str_canon, None)

            # Insert new record (lock needed - DB write)
            with self._db_lock:
                create_image(
                    self.conn,
                    image_id=image_id,
                    path=metadata.path,
                    size=metadata.size,
                    width=metadata.width,
                    height=metadata.height,
                    timestamp=metadata.timestamp,
                    timestamp_confidence=metadata.timestamp_confidence,
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                    mtime=metadata.mtime,
                    exif_data=metadata.exif_data,
                    import_name=import_name,
                )

            # Add to checksum cache
            if metadata.checksum:
                with self._checksum_cache_lock:
                    self._checksum_cache[image_id] = metadata.checksum

            # Generate thumbnail for new image
            if metadata.checksum:
                self._generate_thumbnails(path, metadata.checksum)

            # Queue for embedding and NIMA scoring
            self.embedding_queue.put(image_id)
            if self._nima_queue is not None:
                self._nima_queue.put(image_id)
            logger.debug(f'Ingested new image: {path}')


# =============================================================================
# EMBEDDING THREAD (OpenCLIP)
# =============================================================================


class OpenCLIPModel:
    """Wrapper for OpenCLIP model with lazy loading.

    The model is loaded on first use to avoid startup delay if embeddings
    aren't needed. Supports both GPU and CPU inference.

    Attributes:
        model_name: OpenCLIP model architecture name.
        pretrained: Pretrained weights name.
        device: Torch device ('cuda' or 'cpu').
    """

    def __init__(
        self,
        model_name: str = 'ViT-B-32',
        pretrained: str = 'openai',
        max_dimension: int = 16384,
    ):
        """Initialise the model wrapper.

        Args:
            model_name: OpenCLIP model architecture (e.g., 'ViT-B-32').
            pretrained: Pretrained weights (e.g., 'openai', 'laion2b_s34b_b79k').
            max_dimension: Max image dimension before downsampling (0 to disable).
        """
        self.model_name = model_name
        self.pretrained = pretrained
        self.max_dimension = max_dimension
        # Priority: CUDA (NVIDIA GPU) > MPS (Apple Silicon) > CPU
        if torch.cuda.is_available():
            self.device = 'cuda'
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            self.device = 'mps'
        else:
            self.device = 'cpu'

        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._load_lock = threading.Lock()

    def _load_image_safe(self, path: Path | str) -> Image.Image | None:
        """Load an image, downsampling if it exceeds max_dimension.

        Args:
            path: Path to the image file.

        Returns:
            PIL Image in RGB mode, or None if loading failed.
        """
        try:
            # raw_open_image handles both standard and RAW formats,
            # and applies EXIF orientation correction
            img = raw_open_image(path)

            # Check if downsampling is needed
            if self.max_dimension > 0:
                w, h = img.size
                max_dim = max(w, h)
                if max_dim > self.max_dimension:
                    scale = self.max_dimension / max_dim
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    logger.info(f'Downsampling oversized image {path}: {w}x{h} -> {new_w}x{new_h}')
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            return img.convert('RGB')

        except Exception as e:
            logger.warning(f'Failed to load image {path}: {e}')
            return None

    def _load_model(self) -> None:
        """Load the model (called on first use).

        Thread-safe: uses double-checked locking so concurrent threads
        block while the model is loading rather than returning before
        all components (model, preprocess, tokenizer) are ready.
        """
        # Fast path: tokenizer is set last, so if it's ready everything is.
        if self._tokenizer is not None:
            return

        with self._load_lock:
            # Re-check under lock in case another thread loaded while we waited.
            if self._tokenizer is not None:
                return

            logger.info('=' * 60)
            logger.info(f'Loading OpenCLIP model: {self.model_name} ({self.pretrained})')
            logger.info(f'Device: {self.device}')
            logger.info('-' * 60)

            start_time = time.time()

            # Suppress QuickGELU mismatch warning from open_clip
            with warnings.catch_warnings():
                warnings.filterwarnings('ignore', message='QuickGELU mismatch')
                self._model, _, self._preprocess = open_clip.create_model_and_transforms(
                    self.model_name,
                    pretrained=self.pretrained,
                )
            self._model.eval().to(self.device)
            # Tokenizer MUST be set last — it's the sentinel for the fast-path check.
            self._tokenizer = open_clip.get_tokenizer(self.model_name)

            elapsed = time.time() - start_time
            logger.info('-' * 60)
            logger.info(f'OpenCLIP model loaded in {elapsed:.1f}s')
            logger.info('=' * 60)

    @property
    def model(self):
        """Get the model, loading if necessary."""
        self._load_model()
        return self._model

    @property
    def preprocess(self):
        """Get the preprocessing transform, loading if necessary."""
        self._load_model()
        return self._preprocess

    @property
    def tokenizer(self):
        """Get the tokenizer, loading if necessary."""
        self._load_model()
        return self._tokenizer

    def encode_image(self, path: Path | str) -> np.ndarray | None:
        """Encode a single image to an embedding vector.

        Args:
            path: Path to the image file.

        Returns:
            Normalised embedding as numpy array, or None if encoding fails.
        """
        try:
            # Load image (with downsampling if oversized)
            img = self._load_image_safe(path)
            if img is None:
                return None
            x = self.preprocess(img).unsqueeze(0).to(self.device)

            # Encode with inference mode
            with torch.inference_mode():
                if self.device == 'cuda':
                    with torch.amp.autocast('cuda'):
                        v = self.model.encode_image(x)
                else:
                    v = self.model.encode_image(x)

                # Normalise
                v = v / v.norm(dim=-1, keepdim=True)

            return v.cpu().numpy().flatten()

        except Exception as e:
            logger.warning(f'Failed to encode image {path}: {e}')
            return None

    def encode_images_batch(
        self,
        paths: list[Path | str],
    ) -> list[tuple[int, np.ndarray | None]]:
        """Encode a batch of images to embedding vectors.

        Args:
            paths: List of paths to image files.

        Returns:
            List of (index, embedding) tuples. Embedding is None if encoding failed.
        """
        results: list[tuple[int, np.ndarray | None]] = []
        tensors = []
        valid_indices = []

        # Load and preprocess all images
        for i, path in enumerate(paths):
            img = self._load_image_safe(path)
            if img is None:
                results.append((i, None))
                continue
            try:
                x = self.preprocess(img)
                tensors.append(x)
                valid_indices.append(i)
            except Exception as e:
                logger.warning(f'Failed to preprocess image {path}: {e}')
                results.append((i, None))

        if not tensors:
            return results

        # Stack into batch
        batch = torch.stack(tensors).to(self.device)

        # Encode batch
        try:
            with torch.inference_mode():
                if self.device == 'cuda':
                    with torch.amp.autocast('cuda'):
                        embeddings = self.model.encode_image(batch)
                else:
                    embeddings = self.model.encode_image(batch)

                # Normalise
                embeddings = embeddings / embeddings.norm(dim=-1, keepdim=True)
                embeddings = embeddings.cpu().numpy()

            # Map back to indices
            for batch_idx, original_idx in enumerate(valid_indices):
                results.append((original_idx, embeddings[batch_idx].flatten()))

        except Exception as e:
            logger.error(f'Failed to encode batch: {e}')
            # Mark all as failed
            for original_idx in valid_indices:
                results.append((original_idx, None))

        # Sort by original index
        results.sort(key=lambda x: x[0])
        return results

    def encode_text(self, query: str) -> np.ndarray:
        """Encode a text query to an embedding vector.

        Args:
            query: Text query string.

        Returns:
            Normalised embedding as numpy array.
        """
        tokens = self.tokenizer([query]).to(self.device)

        with torch.inference_mode():
            if self.device == 'cuda':
                with torch.amp.autocast('cuda'):
                    v = self.model.encode_text(tokens)
            else:
                v = self.model.encode_text(tokens)

            # Normalise
            v = v / v.norm(dim=-1, keepdim=True)

        return v.cpu().numpy().flatten()

    def encode_semantic_query(self, query: str, negative_weight: float = 0.5) -> np.ndarray:
        """Encode a semantic query with support for negative terms.

        Parses the query for negative terms (prefixed with '-' at start or after space)
        and computes a combined embedding: normalize(positive - weight * negative).

        Examples:
            "beach -face" -> positive: "beach", negative: "face"
            "-beach lake" -> positive: "lake", negative: "beach"
            "red train -steam-engine" -> positive: "red train", negative: "steam-engine"
            "double-blind" -> positive: "double-blind" (hyphen within word preserved)

        Args:
            query: Text query with optional negative terms.
            negative_weight: Weight for negative embedding subtraction (default 0.5).

        Returns:
            Normalised combined embedding as numpy array.
        """
        positive_parts, negative_parts = parse_semantic_query(query)

        if not positive_parts and not negative_parts:
            # Empty query - return zero vector
            return np.zeros(self.model.visual.output_dim, dtype=np.float32)

        # Encode positive parts
        if positive_parts:
            positive_text = ' '.join(positive_parts)
            positive_embedding = self.encode_text(positive_text)
        else:
            positive_embedding = None

        # Encode negative parts
        if negative_parts:
            negative_text = ' '.join(negative_parts)
            negative_embedding = self.encode_text(negative_text)
        else:
            negative_embedding = None

        # Combine embeddings
        if positive_embedding is not None and negative_embedding is not None:
            # Subtract weighted negative from positive
            combined = positive_embedding - negative_weight * negative_embedding
            # Re-normalize
            norm = np.linalg.norm(combined)
            if norm > 0:
                combined = combined / norm
            return combined
        elif positive_embedding is not None:
            return positive_embedding
        else:
            # Only negative terms - invert the embedding
            return -negative_embedding


def parse_semantic_query(query: str) -> tuple[list[str], list[str]]:
    """Parse a semantic query into positive and negative terms.

    A token is negative if it starts with '-' (e.g., "-face").
    Hyphens within words (like "double-blind") are preserved as positive
    because they don't start with '-'.

    Examples:
        "beach -face" -> (["beach"], ["face"])
        "-beach lake" -> (["lake"], ["beach"])
        "red train -steam-engine" -> (["red", "train"], ["steam-engine"])
        "double-blind" -> (["double-blind"], [])

    Args:
        query: Raw query string.

    Returns:
        Tuple of (positive_parts, negative_parts) where each is a list of terms.
    """
    if not query or not query.strip():
        return [], []

    positive_parts = []
    negative_parts = []

    # Split by whitespace and classify each token
    tokens = query.split()

    for token in tokens:
        if token.startswith('-') and len(token) > 1:
            # Token starts with '-' and has content after it -> negative
            word = token[1:]
            negative_parts.append(word)
        elif token == '-':  # noqa: S105
            # Bare '-' is ignored
            continue
        else:
            # Regular token -> positive
            positive_parts.append(token)

    return positive_parts, negative_parts


class EmbeddingThread(threading.Thread):
    """Background thread for computing image embeddings.

    Processes image IDs from the embedding queue in batches, computes
    OpenCLIP embeddings, and stores them in the database.

    Attributes:
        conn: Database connection.
        embedding_queue: Queue of image IDs to process.
        ingestion_thread: Reference to ingestion thread (to check if idle).
        stop_event: Event to signal thread shutdown.
        config: Configuration object.
        clip_model: OpenCLIP model wrapper.
        on_complete: Optional callback when all processing is done.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedding_queue: queue.Queue[str],
        ingestion_thread: IngestionThread,
        stop_event: threading.Event,
        db_lock: threading.RLock,
        config: Config | None = None,
        data_dir: Path | str = '.',
        on_complete: callable | None = None,
    ):
        """Initialise the embedding thread.

        Args:
            conn: Database connection (must be created with check_same_thread=False).
            embedding_queue: Queue of image IDs to process.
            ingestion_thread: Reference to ingestion thread (to check if idle).
            stop_event: Event to signal thread should stop.
            db_lock: Shared lock for database access (from ImageDatabase).
            config: Configuration object. Uses defaults if None.
            data_dir: Directory containing user data (for LAION aesthetic head).
            on_complete: Optional callback function called when both queues
                are empty. Used to trigger duplicate group computation.
        """
        super().__init__(name='EmbeddingThread', daemon=True)
        self.conn = conn
        self.embedding_queue = embedding_queue
        self.ingestion_thread = ingestion_thread
        self.stop_event = stop_event
        self._db_lock = db_lock  # Shared lock from ImageDatabase
        self.config = config or get_default_config()
        self._data_dir = Path(data_dir)
        self.on_complete = on_complete

        self._clip_model: OpenCLIPModel | None = None
        self._processed_count = 0
        self._error_count = 0
        self._completion_triggered = False

        # LAION aesthetic head weights (loaded lazily on first use).
        # Protected by _laion_lock because both the main thread (backfill)
        # and this thread (_process_batch) may call _load_laion_head().
        self._laion_lock = threading.Lock()
        self._laion_weight: np.ndarray | None = None
        self._laion_bias: float | None = None
        self._laion_loaded = False  # Track whether we've attempted loading

    @property
    def clip_model(self) -> OpenCLIPModel:
        """Get the OpenCLIP model (lazy loaded)."""
        if self._clip_model is None:
            self._clip_model = OpenCLIPModel(
                model_name=self.config.openclip_model,
                pretrained=self.config.openclip_pretrained,
                max_dimension=self.config.max_image_dimension,
            )
        return self._clip_model

    def _load_laion_head(self) -> None:
        """Load the LAION aesthetic predictor head weights.

        The aesthetic head is a nn.Linear(embed_dim, 1) checkpoint that scores
        image quality via dot product with the L2-normalised CLIP embedding.
        Loaded lazily on first use — either from the main thread (backfill at
        startup) or from this thread (_process_batch).

        Thread-safe: guarded by _laion_lock so concurrent callers from different
        threads don't double-load or see partially-initialised state.

        Sets self._laion_weight (1D numpy array) and self._laion_bias (float),
        or leaves them as None if the checkpoint is unavailable or incompatible.
        """
        # Fast path: already loaded (no lock needed for a boolean read under GIL,
        # but the lock ensures we see the final weight/bias values)
        if self._laion_loaded:
            return

        with self._laion_lock:
            # Double-check inside lock (another thread may have loaded while we waited)
            if self._laion_loaded:
                return
            self._laion_loaded = True

            head_path = self._data_dir / '.laion-aesthetic-head.pth'
            if not head_path.exists():
                logger.warning(
                    'LAION aesthetic head not found — aesthetic scoring disabled. '
                    'Run "python download_models.py" to download it.'
                )
                return

            try:
                import torch

                state_dict = torch.load(str(head_path), map_location='cpu', weights_only=True)

                weight = state_dict['weight'].numpy().flatten()  # shape: (embed_dim,)
                bias = float(state_dict['bias'].item())

                # Validate dimension matches the CLIP model's output
                embed_dim = self.clip_model.model.visual.output_dim
                if len(weight) != embed_dim:
                    logger.warning(
                        f'LAION head dimension mismatch: head has {len(weight)}, '
                        f'CLIP model has {embed_dim}. Aesthetic scoring disabled.'
                    )
                    return

                self._laion_weight = weight
                self._laion_bias = bias
                logger.info(f'LAION aesthetic head loaded ({len(weight)}D)')
            except Exception as e:
                logger.warning(f'Failed to load LAION aesthetic head: {e}')

    @property
    def processed_count(self) -> int:
        """Number of images successfully processed."""
        return self._processed_count

    @property
    def error_count(self) -> int:
        """Number of images that failed processing."""
        return self._error_count

    def run(self) -> None:
        """Main thread loop - process images in batches from the queue."""
        logger.info('Image embedding thread started')

        # Progress logging state
        last_progress_time = time.time()
        progress_interval = 5.0  # Log every 5 seconds

        while not self.stop_event.is_set():
            try:
                # Collect a batch of image IDs
                batch_ids: list[str] = []
                batch_paths: list[Path] = []

                # Try to fill batch up to configured size
                while len(batch_ids) < self.config.embedding_batch_size:
                    try:
                        # Short timeout to allow checking stop_event
                        image_id = self.embedding_queue.get(timeout=0.1)
                    except queue.Empty:
                        break

                    # Get image path from database
                    image = get_image(self.conn, image_id)
                    if image is None:
                        logger.warning(f'Image not found for image embedding: {image_id}')
                        self.embedding_queue.task_done()
                        continue

                    path = Path(image['path'])
                    if not path.exists():
                        logger.warning(f'Image file not found for image embedding: {path}')
                        self.embedding_queue.task_done()
                        continue

                    batch_ids.append(image_id)
                    batch_paths.append(path)

                # Process batch if we have any
                if batch_ids:
                    self._process_batch(batch_ids, batch_paths)
                    self._completion_triggered = False  # Reset completion flag
                    time.sleep(0.01)  # Yield GIL briefly for Flask request handling

                    # Periodic progress logging
                    now = time.time()
                    if now - last_progress_time >= progress_interval:
                        remaining = self.embedding_queue.qsize()
                        if remaining > 0 or self._processed_count > 0:
                            logger.info(
                                f'Image embedding progress: {self._processed_count} done, {remaining} remaining'
                            )
                        last_progress_time = now
                else:
                    # Queue is empty - check if we should trigger completion
                    self._check_completion()

            except Exception as e:
                logger.error(f'Unexpected error in image embedding thread: {e}')

        logger.info('Image embedding thread stopped')

    def _process_batch(self, image_ids: list[str], paths: list[Path]) -> None:
        """Process a batch of images.

        Computes CLIP embeddings and optionally LAION aesthetic scores
        (dot product of embedding with aesthetic head weights).

        Args:
            image_ids: List of image IDs.
            paths: List of corresponding file paths.
        """
        logger.debug(f'Processing embedding batch of {len(paths)} images')

        # Ensure LAION head is loaded (lazy, only attempts once)
        self._load_laion_head()

        # Encode batch
        results = self.clip_model.encode_images_batch(paths)

        # Collect successful embeddings for batch commit
        updates = []
        for (_idx, embedding), image_id in zip(results, image_ids, strict=True):
            try:
                if embedding is not None:
                    embedding_bytes = embedding.astype(np.float32).tobytes()
                    # Compute LAION aesthetic score (dot product on L2-normalised embedding)
                    aesthetic = None
                    if self._laion_weight is not None:
                        aesthetic = float(embedding @ self._laion_weight + self._laion_bias)
                    updates.append((embedding_bytes, aesthetic, datetime.now().isoformat(), image_id))
                    self._processed_count += 1
                else:
                    self._error_count += 1
            except Exception as e:
                logger.error(f'Failed to store image embedding for {image_id}: {e}')
                self._error_count += 1
            finally:
                self.embedding_queue.task_done()

        # Batch commit all updates at once (single fsync instead of per-row)
        if updates:
            with self._db_lock:
                self.conn.executemany(
                    'UPDATE images SET embedding = ?, aesthetic_laion = ?, updated_at = ? WHERE id = ?', updates
                )
                self.conn.commit()

    def _check_completion(self) -> None:
        """Check if all processing is complete and trigger completion callback.

        Completion requires:
        - IngestionThread is idle (queue empty AND no pending futures)
        - EmbeddingThread's queue is empty
        """
        if self._completion_triggered:
            return

        # Check if ingestion is truly idle (not just queue empty) and embedding queue empty
        if self.ingestion_thread.is_idle and self.embedding_queue.empty():
            self._completion_triggered = True
            logger.info('All processing complete - triggering completion callback')

            if self.on_complete:
                try:
                    self.on_complete()
                except Exception as e:
                    logger.error(f'Error in completion callback: {e}')


class FaceDetectionThread(threading.Thread):
    """Background thread for detecting faces in images.

    Processes image IDs from the face detection queue, detects faces using
    MTCNN, computes face embeddings, and stores them in the database.
    Also performs auto-recognition against known faces.

    Attributes:
        conn: Database connection.
        face_queue: Queue of image IDs to process.
        embedding_thread: Reference to embedding thread (to check if idle).
        stop_event: Event to signal thread shutdown.
        config: Configuration object.
        on_complete: Optional callback when all processing is done.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        face_queue: queue.Queue[str],
        embedding_thread: EmbeddingThread,
        ingestion_thread: IngestionThread,
        stop_event: threading.Event,
        db_lock: threading.RLock,
        config: Config | None = None,
        thumbnail_dir: Path | str = '.thumbnails',
        on_complete: callable | None = None,
    ):
        """Initialise the face detection thread.

        Args:
            conn: Database connection (must be created with check_same_thread=False).
            face_queue: Queue of image IDs to process.
            embedding_thread: Reference to embedding thread (to check if idle).
            ingestion_thread: Reference to ingestion thread (to check if idle).
            stop_event: Event to signal thread should stop.
            db_lock: Shared lock for database access (from ImageDatabase).
            config: Configuration object. Uses defaults if None.
            thumbnail_dir: Path to thumbnail cache directory.
            on_complete: Optional callback function called when processing complete.
        """
        super().__init__(name='FaceDetectionThread', daemon=True)
        self.conn = conn
        self.face_queue = face_queue
        self.embedding_thread = embedding_thread
        self.ingestion_thread = ingestion_thread
        self.stop_event = stop_event
        self._db_lock = db_lock
        self.config = config or get_default_config()
        self.thumbnail_dir = Path(thumbnail_dir)
        self.on_complete = on_complete

        self._face_detector: FaceDetector | None = None
        self._processed_count = 0
        self._faces_detected_count = 0
        self._error_count = 0
        self._completion_triggered = False

    @property
    def face_detector(self) -> FaceDetector:
        """Get the face detector (lazy loaded)."""
        if self._face_detector is None:
            self._face_detector = FaceDetector(
                min_confidence=self.config.face_detection_min_confidence,
                min_face_size=self.config.face_detection_min_size,
            )
        return self._face_detector

    @property
    def processed_count(self) -> int:
        """Number of images successfully processed."""
        return self._processed_count

    @property
    def faces_detected_count(self) -> int:
        """Total number of faces detected."""
        return self._faces_detected_count

    @property
    def error_count(self) -> int:
        """Number of images that failed processing."""
        return self._error_count

    # Batch size for face detection - now configured via config.face_detection_batch_size

    def run(self) -> None:
        """Main thread loop - process images from the queue in batches.

        Uses prefetching: while the GPU processes batch N, the CPU loads
        batch N+1's images in parallel for better throughput.
        """
        logger.info('Face detection thread started')

        # Don't do anything if face detection is disabled
        if not self.config.face_detection_enabled:
            logger.info('Face detection disabled in config - thread idle')
            while not self.stop_event.is_set():
                # Just check for completion
                time.sleep(1.0)
                self._check_completion()
            logger.info('Face detection thread stopped (was disabled)')
            return

        # Progress logging state
        last_progress_time = time.time()
        progress_interval = 5.0
        first_batch = True

        # Prefetch state
        prefetch_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='face-prefetch')
        prefetch_future: Future | None = None
        prefetch_batch_ids: list[str] = []

        def collect_batch() -> list[str]:
            """Collect up to batch_size image IDs from the queue."""
            batch = []
            try:
                # Get first image (blocking with timeout)
                image_id = self.face_queue.get(timeout=0.5)
                batch.append(image_id)

                # Try to fill the batch (non-blocking)
                while len(batch) < self.config.face_detection_batch_size:
                    try:
                        image_id = self.face_queue.get_nowait()
                        batch.append(image_id)
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            return batch

        def resolve_and_preload(batch_ids: list[str]) -> tuple[dict, dict, list]:
            """Resolve image IDs to paths and preload images (runs in prefetch thread)."""
            id_to_path: dict[str, Path] = {}
            path_to_id: dict[Path, str] = {}

            for image_id in batch_ids:
                # Check for shutdown during path resolution
                if self.stop_event.is_set():
                    break

                image = get_image(self.conn, image_id)
                if image is None:
                    logger.warning(f'Image not found for face detection: {image_id}')
                    continue

                path = Path(image['path'])
                if not path.exists():
                    logger.warning(f'Image file not found for face detection: {path}')
                    continue

                # Skip if already processed
                with self._db_lock:
                    if has_faces_detected(self.conn, image_id):
                        logger.debug(f'Skipping already-processed image: {path.name}')
                        self._processed_count += 1
                        continue

                id_to_path[image_id] = path
                path_to_id[path] = image_id

            # Preload images in parallel (CPU-bound) - skip if shutting down
            if id_to_path and not self.stop_event.is_set():
                paths = list(id_to_path.values())
                loaded_images = self.face_detector.preload_images_batch(paths, num_workers=4)
            else:
                loaded_images = []

            return id_to_path, path_to_id, loaded_images

        try:
            while not self.stop_event.is_set():
                batch_ids = []
                try:
                    # If we have a prefetch ready, use it; otherwise collect new batch
                    if prefetch_future is not None:
                        # Wait for prefetch to complete
                        id_to_path, path_to_id, loaded_images = prefetch_future.result()
                        batch_ids = prefetch_batch_ids
                        prefetch_future = None
                        prefetch_batch_ids = []
                    else:
                        # No prefetch - collect and prepare synchronously
                        batch_ids = collect_batch()
                        if not batch_ids:
                            self._check_completion()
                            continue

                        # Log before first batch (model loading happens here)
                        if first_batch:
                            logger.info('Loading face detection models (MTCNN + InceptionResnetV1)...')
                            first_batch = False

                        id_to_path, path_to_id, loaded_images = resolve_and_preload(batch_ids)

                    # Start prefetching next batch while we process current one
                    # (but only if we're not shutting down)
                    if not self.stop_event.is_set():
                        next_batch_ids = collect_batch()
                        if next_batch_ids:
                            prefetch_batch_ids = next_batch_ids
                            prefetch_future = prefetch_executor.submit(resolve_and_preload, next_batch_ids)

                    # Process the current batch (GPU work + DB writes)
                    if loaded_images:
                        self._process_preloaded_batch(id_to_path, path_to_id, loaded_images)
                    self._completion_triggered = False
                    time.sleep(0.01)  # Yield GIL briefly for Flask request handling

                    # Periodic progress logging
                    now = time.time()
                    if now - last_progress_time >= progress_interval:
                        remaining = self.face_queue.qsize()
                        if remaining > 0 or self._processed_count > 0:
                            logger.info(
                                f'Face detection progress: {self._processed_count} images, '
                                f'{self._faces_detected_count} faces, {remaining} remaining'
                            )
                        last_progress_time = now

                except Exception as e:
                    logger.error(f'Error in face detection thread: {e}')
                    # If the prefetch future failed, its batch_ids were never
                    # transferred to batch_ids.  Claim them now so the finally
                    # block calls task_done() for them and clear the failed
                    # future so we don't retry it on the next loop iteration.
                    if prefetch_future is not None:
                        batch_ids = prefetch_batch_ids
                        prefetch_future = None
                        prefetch_batch_ids = []
                    self._error_count += len(batch_ids)
                finally:
                    # Always mark items as done, even on error or shutdown
                    for _ in batch_ids:
                        self.face_queue.task_done()
        finally:
            # Clean up any pending prefetch
            if prefetch_future is not None:
                try:
                    # Wait for prefetch to complete (it checks stop_event internally)
                    prefetch_future.result(timeout=5.0)
                except Exception:
                    pass  # Ignore errors during shutdown
            if prefetch_batch_ids:
                for _ in prefetch_batch_ids:
                    self.face_queue.task_done()
            prefetch_executor.shutdown(wait=True)

        logger.info('Face detection thread stopped')

    def _process_batch(self, image_ids: list[str]) -> None:
        """Process a batch of images for face detection.

        Legacy method - loads images synchronously then processes.
        For better throughput, use the prefetching approach in run().

        Args:
            image_ids: List of image IDs to process.
        """
        # Build mapping of image_id -> path and filter out invalid images
        id_to_path: dict[str, Path] = {}
        path_to_id: dict[Path, str] = {}

        for image_id in image_ids:
            image = get_image(self.conn, image_id)
            if image is None:
                logger.warning(f'Image not found for face detection: {image_id}')
                continue

            path = Path(image['path'])
            if not path.exists():
                logger.warning(f'Image file not found for face detection: {path}')
                continue

            # Skip if already processed
            with self._db_lock:
                if has_faces_detected(self.conn, image_id):
                    logger.debug(f'Skipping already-processed image: {path.name}')
                    self._processed_count += 1
                    continue

            id_to_path[image_id] = path
            path_to_id[path] = image_id

        if not id_to_path:
            return

        # Run batched face detection (pass stop_event for graceful interruption)
        paths = list(id_to_path.values())
        loaded_images = self.face_detector.preload_images_batch(paths, num_workers=4)
        self._process_preloaded_batch(id_to_path, path_to_id, loaded_images)

    def _process_preloaded_batch(
        self,
        id_to_path: dict[str, Path],
        path_to_id: dict[Path, str],
        loaded_images: list,
    ) -> None:
        """Process pre-loaded images for face detection.

        This is the GPU phase - takes images that were already loaded and
        runs MTCNN detection + embedding generation.

        Args:
            id_to_path: Mapping of image_id to file path.
            path_to_id: Mapping of file path to image_id.
            loaded_images: List of (path, PIL.Image, scale) tuples from preload.
        """
        if not loaded_images:
            return

        # Run GPU face detection on pre-loaded images
        results = self.face_detector.detect_faces_from_preloaded(loaded_images, stop_event=self.stop_event)

        # Get known face embeddings, per-person thresholds, and ignored
        # person IDs for auto-recognition.  Ignored people (name == '-')
        # are only matched as a fallback after all named people are tried.
        with self._db_lock:
            known_embeddings = get_all_known_face_embeddings(self.conn)
            cursor = self.conn.execute('SELECT id, name, recognition_threshold FROM people')
            per_person_thresholds: dict[str, float | None] = {}
            ignored_person_ids: set[str] = set()
            for row in cursor.fetchall():
                per_person_thresholds[row['id']] = row['recognition_threshold']
                if row['name'] == '-':
                    ignored_person_ids.add(row['id'])

        # Track auto-match statistics for batch summary
        batch_faces_total = 0
        batch_matched = 0
        batch_best_unmatched = -1.0  # Highest similarity that didn't meet threshold

        # Process results for each image
        for path, detected_faces in results.items():
            if path not in path_to_id:
                logger.error(f'Path {path} not found in path_to_id mapping - skipping')
                continue
            image_id = path_to_id[path]

            if not detected_faces:
                # Mark image as processed with no faces found
                with self._db_lock:
                    mark_no_faces_detected(self.conn, image_id)
                self._processed_count += 1
                continue

            for face in detected_faces:
                batch_faces_total += 1

                # Try to auto-match against known faces (respects per-person thresholds)
                person_id = None
                match = find_best_match(
                    face.embedding,
                    known_embeddings,
                    threshold=self.config.face_recognition_threshold,
                    person_thresholds=per_person_thresholds,
                    ignored_person_ids=ignored_person_ids,
                )
                if match:
                    _, person_id, similarity = match
                    batch_matched += 1
                    logger.debug(
                        f'Auto-matched face in {path.name} to person {person_id} (similarity: {similarity:.3f})'
                    )
                elif known_embeddings:
                    # Track best unmatched similarity for diagnostics
                    emb = face.embedding
                    emb_norm = np.linalg.norm(emb)
                    if emb_norm > 0:
                        if not np.isclose(emb_norm, 1.0, atol=0.01):
                            emb = emb / emb_norm
                        for _, _, known_emb in known_embeddings:
                            sim = float(np.dot(emb, known_emb))
                            if sim > batch_best_unmatched:
                                batch_best_unmatched = sim

                # Generate face thumbnail first (needed for semantic embedding)
                face_id = str(uuid.uuid4())
                thumb_path = get_face_thumbnail_path(face_id, self.thumbnail_dir)
                generate_face_thumbnail(
                    path,
                    thumb_path,
                    box_x=face.box_x,
                    box_y=face.box_y,
                    box_w=face.box_w,
                    box_h=face.box_h,
                    size=200,
                    quality=self.config.thumbnail_quality,
                )

                # Generate semantic embedding from face thumbnail using CLIP
                semantic_embedding = None
                if thumb_path.exists():
                    semantic_embedding = self.embedding_thread.clip_model.encode_image(thumb_path)

                # Create face record with semantic embedding
                with self._db_lock:
                    create_face(
                        self.conn,
                        image_id=image_id,
                        box_x=face.box_x,
                        box_y=face.box_y,
                        box_w=face.box_w,
                        box_h=face.box_h,
                        embedding=face.embedding,
                        confidence=face.confidence,
                        person_id=person_id,
                        face_id=face_id,
                        semantic_embedding=semantic_embedding,
                    )

                self._faces_detected_count += 1

            self._processed_count += 1
            logger.debug(f'Detected {len(detected_faces)} faces in {path.name}')

        # Log batch auto-match summary at INFO level for diagnostics
        if batch_faces_total > 0:
            threshold = self.config.face_recognition_threshold
            parts = [
                f'{batch_faces_total} faces detected',
                f'{len(known_embeddings)} known references',
                f'{batch_matched} auto-matched',
            ]
            if batch_faces_total > batch_matched and batch_best_unmatched > -1.0:
                parts.append(f'best unmatched similarity: {batch_best_unmatched:.3f}/{threshold:.2f}')
            logger.info(f'Face auto-match: {", ".join(parts)}')

    def _check_completion(self) -> None:
        """Check if all processing is complete and trigger completion callback.

        Completion requires:
        - IngestionThread is idle
        - EmbeddingThread's queue is empty
        - FaceDetectionThread's queue is empty
        """
        if self._completion_triggered:
            return

        # Check all threads are idle
        embedding_idle = self.embedding_thread.embedding_queue.empty()
        ingestion_idle = self.ingestion_thread.is_idle
        face_queue_empty = self.face_queue.empty()

        if ingestion_idle and embedding_idle and face_queue_empty:
            self._completion_triggered = True
            logger.info('Face detection complete - triggering completion callback')

            if self.on_complete:
                try:
                    self.on_complete()
                except Exception as e:
                    logger.error(f'Error in face detection completion callback: {e}')


class NimaThread(threading.Thread):
    """Background thread for NIMA aesthetic scoring.

    Processes image IDs from the NIMA queue, loads 400px thumbnails from
    disk, scores them with the MobileNetV2-AVA NIMA model, and stores results.

    Runs concurrently with (not chained into) the embedding and face
    detection pipelines.  GPU memory for MobileNetV2 (~9MB) plus CLIP ViT-B-32
    (~350MB) plus MTCNN+InceptionResnet (~100MB) totals ~460MB, fitting
    comfortably on 2GB+ GPUs.  Disable via ``nima_enabled: false`` on
    machines with limited VRAM.

    The model is loaded lazily on first batch (thread-safe double-checked
    locking).  If the checkpoint is missing the thread logs a warning and
    sits idle.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        nima_queue: queue.Queue[str],
        ingestion_thread: IngestionThread,
        stop_event: threading.Event,
        db_lock: threading.RLock,
        config: Config,
        data_dir: Path | str = '.',
        thumbnail_dir: Path | str = '.thumbnails',
        event_queue: EventQueue | None = None,
    ):
        """Initialise the NIMA scoring thread.

        Args:
            conn: Database connection (check_same_thread=False).
            nima_queue: Queue of image IDs to score.
            ingestion_thread: Ingestion thread (for idle checks).
            stop_event: Event to signal thread should stop.
            db_lock: Shared lock for database access.
            config: Application configuration.
            data_dir: Directory containing the NIMA checkpoint.
            thumbnail_dir: Directory containing cached thumbnails.
            event_queue: Optional event queue for completion notifications.
        """
        super().__init__(name='NimaThread', daemon=True)
        self.conn = conn
        self.nima_queue = nima_queue
        self.ingestion_thread = ingestion_thread
        self.stop_event = stop_event
        self._db_lock = db_lock
        self.config = config
        self._data_dir = Path(data_dir)
        self._thumbnail_dir = Path(thumbnail_dir)
        self._event_queue = event_queue

        self._model = None
        self._device: str | None = None
        self._model_lock = threading.Lock()
        self._model_loaded = False  # Track whether we've attempted loading

        self._processed_count = 0
        self._skipped_count = 0  # Images skipped (e.g. missing thumbnails)
        self._error_count = 0
        self._completion_triggered = False

    @property
    def processed_count(self) -> int:
        """Number of images successfully scored."""
        return self._processed_count

    def _load_model(self) -> bool:
        """Lazily load the NIMA model (thread-safe, only attempts once).

        Returns:
            True if model is available, False otherwise.
        """
        if self._model_loaded:
            return self._model is not None

        with self._model_lock:
            if self._model_loaded:
                return self._model is not None
            self._model_loaded = True

            checkpoint_path = self._data_dir / '.nima-mobilenetv2-ava.pth'
            if not checkpoint_path.exists():
                logger.warning(
                    'NIMA checkpoint not found — aesthetic scoring disabled. '
                    'Run "python download_models.py" to download it.'
                )
                return False

            try:
                from nima import load_nima_model

                # Priority: CUDA (NVIDIA GPU) > MPS (Apple Silicon) > CPU
                if torch.cuda.is_available():
                    device = 'cuda'
                elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                    device = 'mps'
                else:
                    device = 'cpu'
                self._model = load_nima_model(str(checkpoint_path), device=device)
                self._device = device
                logger.info(f'NIMA model loaded on {device}')
                return True
            except Exception as e:
                logger.warning(f'Failed to load NIMA model: {e}')
                return False

    def run(self) -> None:
        """Main thread loop — score images from the queue."""
        logger.info('NIMA scoring thread started')

        # Don't do anything if NIMA scoring is disabled
        if not self.config.nima_enabled:
            logger.info('NIMA scoring disabled in config — thread idle')
            while not self.stop_event.is_set():
                time.sleep(1.0)
                self._check_completion()
            logger.info('NIMA scoring thread stopped (was disabled)')
            return

        batch_size = self.config.nima_batch_size
        last_progress_time = time.time()
        progress_interval = 5.0

        while not self.stop_event.is_set():
            # Collect a batch of image IDs
            batch_ids: list[str] = []
            try:
                # Block briefly for the first item
                image_id = self.nima_queue.get(timeout=0.1)
                batch_ids.append(image_id)
                # Drain up to batch_size - 1 more without blocking
                while len(batch_ids) < batch_size:
                    try:
                        image_id = self.nima_queue.get_nowait()
                        batch_ids.append(image_id)
                    except queue.Empty:
                        break
            except queue.Empty:
                # No work available — check completion
                self._check_completion()
                continue

            # Process the batch
            try:
                self._process_batch(batch_ids)
            except Exception as e:
                logger.error(f'Error processing NIMA batch: {e}')
                self._error_count += len(batch_ids)
            finally:
                for _ in batch_ids:
                    self.nima_queue.task_done()

            # Progress logging
            now = time.time()
            if now - last_progress_time >= progress_interval:
                remaining = self.nima_queue.qsize()
                logger.info(f'NIMA scoring: {self._processed_count} done, {remaining} remaining')
                last_progress_time = now

            # Yield GIL briefly to avoid blocking Flask request handling
            time.sleep(0.01)

        logger.info(f'NIMA scoring thread stopped (scored {self._processed_count}, errors {self._error_count})')

    def _process_batch(self, image_ids: list[str]) -> None:
        """Score a batch of images using their 400px thumbnails.

        Resolves image IDs to checksums, loads thumbnails from disk,
        runs NIMA inference, and writes scores to the database.

        Args:
            image_ids: List of image IDs to score.
        """
        # Ensure model is loaded
        if not self._load_model():
            return

        # Look up checksums for these image IDs
        with self._db_lock:
            placeholders = ','.join('?' * len(image_ids))
            cursor = self.conn.execute(f'SELECT id, checksum FROM images WHERE id IN ({placeholders})', image_ids)
            rows = {row['id']: row['checksum'] for row in cursor.fetchall()}

        # Load 400px thumbnails from disk
        valid_ids = []
        pil_images = []
        for image_id in image_ids:
            checksum = rows.get(image_id)
            if not checksum:
                logger.debug(f'No checksum for image {image_id}, skipping NIMA')
                continue

            thumb_path = get_thumbnail_cache_path(checksum, 400, thumbnail_dir=self._thumbnail_dir)
            if not Path(thumb_path).exists():
                logger.debug(f'No 400px thumbnail for {image_id}, skipping NIMA')
                continue

            try:
                img = Image.open(thumb_path).convert('RGB')
                valid_ids.append(image_id)
                pil_images.append(img)
            except Exception as e:
                logger.warning(f'Failed to load thumbnail for NIMA: {image_id}: {e}')

        skipped = len(image_ids) - len(valid_ids)
        if skipped > 0:
            self._skipped_count += skipped

        if not pil_images:
            return

        # Score the batch
        from nima import score_images_batch

        scores = score_images_batch(self._model, pil_images, device=self._device)

        # Batch commit to database
        now = datetime.now().isoformat()
        updates = [(score, now, vid) for score, vid in zip(scores, valid_ids, strict=True)]

        with self._db_lock:
            self.conn.executemany('UPDATE images SET aesthetic_nima = ?, updated_at = ? WHERE id = ?', updates)
            self.conn.commit()

        self._processed_count += len(updates)

    def _check_completion(self) -> None:
        """Check if all scoring is complete and emit completion event.

        Completion requires:
        - IngestionThread is idle (no more images coming in)
        - NIMA queue is empty
        - At least one image has been dequeued (avoids spurious events on
          startup when the queue hasn't been populated yet)
        """
        if self._completion_triggered:
            return

        total_dequeued = self._processed_count + self._skipped_count + self._error_count
        if total_dequeued > 0 and self.ingestion_thread.is_idle and self.nima_queue.empty():
            self._completion_triggered = True
            parts = []
            if self._processed_count:
                parts.append(f'scored {self._processed_count}')
            if self._skipped_count:
                parts.append(f'skipped {self._skipped_count} (no thumbnail)')
            if self._error_count:
                parts.append(f'{self._error_count} errors')
            logger.info(f'NIMA scoring complete — {", ".join(parts)}')

            if self._event_queue:
                self._event_queue.emit(
                    EVENT_NIMA_COMPLETE,
                    {
                        'scored_count': self._processed_count,
                    },
                )


def get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value by key.

    Args:
        conn: Database connection.
        key: Metadata key.

    Returns:
        The value, or None if not found.
    """
    cursor = conn.execute('SELECT value FROM metadata WHERE key = ?', (key,))
    row = cursor.fetchone()
    if row:
        return row['value']
    return None


def set_metadata(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a metadata value.

    Args:
        conn: Database connection.
        key: Metadata key.
        value: Value to store.
    """
    conn.execute('INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)', (key, value))
    conn.commit()


# =============================================================================
# SEMANTIC SEARCH
# =============================================================================


def semantic_search(
    conn: sqlite3.Connection,
    query_embedding: np.ndarray,
    threshold: float = 0.2,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Search for images similar to a query embedding.

    Compares the query embedding against image embeddings using cosine
    similarity. Description embeddings provide a small additive boost but
    cannot dominate the score, since text-to-text similarity in CLIP is
    inherently 2-3x higher than text-to-image similarity.

    Uses vectorized numpy operations for performance - computing similarity
    scores for 50k+ images in milliseconds rather than minutes.

    Args:
        conn: Database connection.
        query_embedding: Normalised query embedding vector.
        threshold: Minimum similarity score (0.0 to 1.0). Defaults to 0.2.
        limit: Maximum number of results to return. Defaults to 100.

    Returns:
        List of image dictionaries with added 'score' field, sorted by
        descending similarity score.
    """
    # Additive boost weight for description embedding score.  Text-to-text
    # cosine similarity in CLIP is ~2-3× higher than text-to-image for the same
    # semantic relevance, so using max(img, desc) lets even unrelated descriptions
    # dominate.  Instead, treat description as a small additive bonus on top of
    # the image score.
    DESC_BOOST = 0.1

    # Step 1: Get just IDs and embeddings (minimal data transfer)
    cursor = conn.execute("""
        SELECT id, embedding, description_embedding
        FROM images
        WHERE deleted = 0 AND (embedding IS NOT NULL OR description_embedding IS NOT NULL)
    """)

    rows = cursor.fetchall()
    if not rows:
        return []

    # Step 2: Build numpy arrays for vectorized computation
    embedding_dim = len(query_embedding)
    ids = []
    img_embeddings = []
    desc_embeddings = []
    has_img_embedding = []
    has_desc_embedding = []

    for row in rows:
        ids.append(row['id'])
        if row['embedding']:
            img_embeddings.append(np.frombuffer(row['embedding'], dtype=np.float32))
            has_img_embedding.append(True)
        else:
            img_embeddings.append(np.zeros(embedding_dim, dtype=np.float32))
            has_img_embedding.append(False)

        if row['description_embedding']:
            desc_embeddings.append(np.frombuffer(row['description_embedding'], dtype=np.float32))
            has_desc_embedding.append(True)
        else:
            desc_embeddings.append(np.zeros(embedding_dim, dtype=np.float32))
            has_desc_embedding.append(False)

    # Stack into matrices for vectorized dot product
    img_matrix = np.vstack(img_embeddings)  # Shape: (n, embedding_dim)
    desc_matrix = np.vstack(desc_embeddings)  # Shape: (n, embedding_dim)
    has_img = np.array(has_img_embedding)
    has_desc = np.array(has_desc_embedding)

    # Step 3: Vectorized similarity computation (single matrix multiply)
    img_scores = img_matrix @ query_embedding  # Shape: (n,)
    desc_scores = desc_matrix @ query_embedding  # Shape: (n,)

    # Zero out scores for missing embeddings
    img_scores = np.where(has_img, img_scores, 0.0)
    desc_scores = np.where(has_desc, desc_scores, 0.0)

    # Description is an additive boost on top of image score, not an
    # independent competing signal.  A relevant description nudges an image
    # up in the rankings; an irrelevant one barely registers.
    scores = img_scores + desc_scores * DESC_BOOST

    # Step 4: Filter by threshold and get top results
    above_threshold = scores >= threshold
    if not np.any(above_threshold):
        return []

    # Get indices of results above threshold, sorted by score descending
    valid_indices = np.where(above_threshold)[0]
    valid_scores = scores[valid_indices]
    sorted_order = np.argsort(-valid_scores)  # Descending
    top_indices = valid_indices[sorted_order[:limit]]
    # top_scores = valid_scores[sorted_order[:limit]]

    # Step 5: Fetch full metadata only for top results
    top_ids = [ids[i] for i in top_indices]
    if not top_ids:
        return []

    # Build a mapping of id -> score
    score_map = {ids[i]: float(scores[i]) for i in top_indices}

    # Fetch full image data for the top results
    placeholders = ','.join('?' * len(top_ids))
    cursor = conn.execute(
        f"""
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating
        FROM images
        WHERE id IN ({placeholders})
    """,
        top_ids,
    )

    results = []
    for row in cursor.fetchall():
        image_dict = dict(row)
        image_dict['score'] = score_map[image_dict['id']]
        results.append(image_dict)

    # Sort by score descending (order may have changed from the IN query)
    results.sort(key=lambda x: x['score'], reverse=True)

    return results


def get_images_by_similarity(
    conn: sqlite3.Connection,
    reference_embedding: np.ndarray,
) -> list[dict[str, Any]]:
    """Get all images sorted by similarity to a reference embedding.

    Uses vectorized numpy operations for performance - computing similarity
    scores for 50k+ images in milliseconds rather than minutes.

    Args:
        conn: Database connection.
        reference_embedding: Normalised reference embedding vector.

    Returns:
        List of image dictionaries with added 'similarity' field,
        sorted by descending similarity.
    """
    # Step 1: Get just IDs and embeddings (minimal data transfer)
    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)

    rows = cursor.fetchall()
    if not rows:
        return []

    # Step 2: Build numpy arrays for vectorized computation
    ids = [row['id'] for row in rows]
    embeddings = [np.frombuffer(row['embedding'], dtype=np.float32) for row in rows]

    # Stack into matrix for vectorized dot product
    embedding_matrix = np.vstack(embeddings)  # Shape: (n, embedding_dim)

    # Step 3: Vectorized similarity computation (single matrix multiply)
    similarities = embedding_matrix @ reference_embedding  # Shape: (n,)

    # Step 4: Build similarity map
    similarity_map = {ids[i]: float(similarities[i]) for i in range(len(ids))}

    # Step 5: Fetch full image data
    # Note: For very large datasets, we could add a limit here, but for now
    # we return all images sorted by similarity as the original function did.
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

    # Sort by similarity descending
    results.sort(key=lambda x: x['similarity'], reverse=True)

    return results


# =============================================================================
# THUMBNAIL GENERATION
# =============================================================================
# Note: Most thumbnail functions are in thumbnails.py
# Only database-dependent functions remain here.


# =============================================================================
# EVENT QUEUE AND SSE
# =============================================================================

# Event types
EVENT_FOLDER_ADDED = 'folder_added'
EVENT_FOLDER_REMOVED = 'folder_removed'
EVENT_PROCESSING_COMPLETE = 'processing_complete'
EVENT_IMAGES_MODIFIED = 'images_modified'
EVENT_NIMA_COMPLETE = 'nima_complete'

# Multi-client mutation events — emitted by Flask routes so that other
# browser tabs/clients can pick up user-initiated changes via polling.
EVENT_FACES_CHANGED = 'faces_changed'
EVENT_PEOPLE_CHANGED = 'people_changed'
EVENT_IMAGES_CHANGED = 'images_changed'
EVENT_GROUPS_CHANGED = 'groups_changed'
EVENT_IMPORT_COMPLETE = 'import_complete'


@dataclass
class Event:
    """Event data container for frontend polling.

    Attributes:
        event_type: Type of event (e.g., 'folder_added', 'processing_complete').
        data: Event payload as dictionary.
        timestamp: Unix timestamp (seconds) from time.time().
    """

    event_type: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)


class EventQueue:
    """Thread-safe event queue for multi-client frontend polling.

    Accumulates events in a ring buffer. Clients poll with a cursor
    (timestamp from previous response) to receive only new events.
    Events are NOT drained on read — multiple clients can each
    maintain their own cursor and independently catch up.

    Attributes:
        _events: List of buffered events (oldest first).
        _lock: Threading lock for thread safety.
    """

    # Maximum events to buffer (prevents unbounded growth).
    # When exceeded, oldest events are dropped. Clients whose cursor
    # falls behind the oldest event receive a 'stale' flag and must
    # do a full reload instead of incremental catch-up.
    MAX_EVENTS = 200

    def __init__(self):
        """Initialise the event queue."""
        self._events: list[Event] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Create and queue an event.

        Args:
            event_type: Type of event.
            data: Event payload (default empty dict).
        """
        event = Event(event_type=event_type, data=data or {})
        with self._lock:
            self._events.append(event)
            # Trim oldest events if queue is too large
            if len(self._events) > self.MAX_EVENTS:
                self._events = self._events[-self.MAX_EVENTS :]
        logger.debug(f'Event queued: {event_type} (buffered: {len(self._events)})')

    def get_pending_count(self) -> int:
        """Get number of buffered events.

        Returns:
            Number of events currently in the buffer.
        """
        with self._lock:
            return len(self._events)

    def get_since(self, since: float = 0) -> dict[str, Any]:
        """Get events newer than the given timestamp.

        Uses a 100ms safety margin to avoid missing near-simultaneous
        events due to floating-point timing. Events stay in the buffer
        (not cleared on read) so multiple clients can each poll
        independently.

        Args:
            since: Unix timestamp cursor from previous poll response.
                   Pass 0 for the initial poll (returns all buffered events).

        Returns:
            Dict with:
                events: List of Event objects newer than ``since``.
                server_time: Current server time (use as next cursor).
                stale: True when client's cursor has fallen behind the
                    buffer — the client missed events and must do a
                    full state reload instead of incremental catch-up.
        """
        now = time.time()
        with self._lock:
            # Stale detection: client had a cursor (since > 0), the buffer
            # is full, and the cursor is older than our oldest event.
            stale = False
            if since > 0 and len(self._events) >= self.MAX_EVENTS:  # noqa: SIM102
                if self._events and since < self._events[0].timestamp:
                    stale = True

            if stale:
                return {'events': [], 'server_time': now, 'stale': True}

            # 100ms safety margin to catch near-simultaneous events
            cutoff = since - 0.1 if since > 0 else 0
            events = [e for e in self._events if e.timestamp >= cutoff]

        if events:
            logger.debug(f'Returning {len(events)} events since {since:.3f}')
        return {'events': events, 'server_time': now, 'stale': False}


# Convenience functions for emitting specific events


def emit_folder_added(event_queue: EventQueue, folder_path: str) -> None:
    """Emit a folder_added event.

    Args:
        event_queue: EventQueue instance.
        folder_path: Path of the added folder.
    """
    event_queue.emit(EVENT_FOLDER_ADDED, {'folder': folder_path})


def emit_folder_removed(event_queue: EventQueue, folder_path: str) -> None:
    """Emit a folder_removed event.

    Args:
        event_queue: EventQueue instance.
        folder_path: Path of the removed folder.
    """
    event_queue.emit(EVENT_FOLDER_REMOVED, {'folder': folder_path})


def emit_processing_complete(event_queue: EventQueue) -> None:
    """Emit a processing_complete event.

    Args:
        event_queue: EventQueue instance.
    """
    event_queue.emit(EVENT_PROCESSING_COMPLETE, {})


# =============================================================================
# TRASH WORKER THREAD
# =============================================================================


class TrashWorker(threading.Thread):
    """Background thread for asynchronous file-move trash operations.

    Reads ``(image_id, file_path)`` tuples from a queue and moves files
    into the trash directory using a ``ThreadPoolExecutor`` for I/O
    parallelism.  The DB soft-delete and cache invalidation have already
    happened by the time items reach this worker — only the slow
    filesystem I/O is deferred.

    Progress is tracked via ``_trash_progress`` on the owning
    ``ImageDatabase`` so ``/api/status`` can report live numbers.
    On graceful shutdown, any remaining queue items are persisted to
    ``<trash_dir>/.pending_trash.json`` for recovery on next startup.

    Follows the same daemon-thread pattern as :class:`NimaThread`.
    """

    def __init__(
        self,
        trash_queue: queue.Queue[tuple[str, str]],
        stop_event: threading.Event,
        trash_dir: Path,
        max_workers: int,
        progress: dict | None,
        progress_lock: threading.Lock,
    ):
        """Initialise the trash worker thread.

        Args:
            trash_queue: Queue of ``(image_id, file_path)`` tuples to move.
            stop_event: Event to signal thread should stop.
            trash_dir: Destination trash directory.
            max_workers: Number of threads in the file-move pool.
            progress: Shared ``_trash_progress`` dict reference (may be None).
            progress_lock: Lock protecting ``_trash_progress`` mutations.
        """
        super().__init__(name='TrashWorker', daemon=True)
        self._queue = trash_queue
        self._stop_event = stop_event
        self._trash_dir = trash_dir
        self._max_workers = max_workers
        # The progress dict is *replaced* (not mutated) by the owner, so
        # we store a reference to the owner object and read its attribute.
        self._progress = progress
        self._progress_lock = progress_lock

    def run(self) -> None:
        """Main loop -- drain queue items and move files in parallel.

        Creates a single :class:`ThreadPoolExecutor` that lives for the
        entire thread lifetime.  This avoids per-batch ``shutdown(wait=True)``
        calls which can hang on Windows (same pattern as :class:`ImportWorker`).
        """
        logger.info('Trash worker thread started')

        executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix='trash-move',
        )

        try:
            while not self._stop_event.is_set():
                # Block briefly for the first item
                batch: list[tuple[str, str]] = []
                try:
                    item = self._queue.get(timeout=0.2)
                    batch.append(item)
                    # Drain up to 200 more without blocking (batched I/O)
                    while len(batch) < 200:
                        try:
                            batch.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    continue

                # Move files in parallel
                self._process_batch(batch, executor)

                # Yield GIL briefly
                time.sleep(0.01)

            # On shutdown, drain remaining items and persist for recovery
            remaining = self._drain_remaining()
            if remaining:
                self._persist_pending(remaining)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        logger.info('Trash worker thread stopped')

    def _process_batch(
        self,
        batch: list[tuple[str, str]],
        executor: ThreadPoolExecutor,
    ) -> None:
        """Move a batch of files to trash using a thread pool.

        Args:
            batch: List of ``(image_id, file_path)`` tuples.
            executor: Long-lived thread pool for parallel file moves.
        """
        futures = [executor.submit(self._move_one, item) for item in batch]
        # Wait for all moves in this batch to complete
        for future in futures:
            if self._stop_event.is_set():
                for f in futures:
                    f.cancel()
                break
            try:
                future.result()
            except Exception as e:
                logger.error(f'TrashWorker: executor error: {e}')

    def _move_one(self, item: tuple[str, str]) -> None:
        """Move a single file to the trash directory.

        Args:
            item: ``(image_id, file_path)`` tuple.
        """
        _image_id, file_path = item
        try:
            move_to_trash(Path(file_path), self._trash_dir)
        except Exception as e:
            logger.error(f'TrashWorker: failed to move {file_path}: {e}')
        finally:
            with self._progress_lock:
                if self._progress is not None:
                    self._progress['done'] += 1

    def _drain_remaining(self) -> list[tuple[str, str]]:
        """Drain any items left in the queue after stop_event is set.

        Returns:
            List of un-processed ``(image_id, file_path)`` tuples.
        """
        remaining: list[tuple[str, str]] = []
        while True:
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return remaining

    def _persist_pending(self, items: list[tuple[str, str]]) -> None:
        """Write un-processed items to disk for crash recovery.

        Args:
            items: List of ``(image_id, file_path)`` tuples to persist.
        """
        pending_path = self._trash_dir / '.pending_trash.json'
        try:
            pending_path.write_text(
                json.dumps([{'id': iid, 'path': fp} for iid, fp in items]),
                encoding='utf-8',
            )
            logger.info(f'Persisted {len(items)} pending trash items to {pending_path}')
        except Exception as e:
            logger.error(f'Failed to persist pending trash items: {e}')


# =============================================================================
# IMPORT WORKER
# =============================================================================


class ImportWorker(threading.Thread):
    """Background thread for importing (copying) images into the catalogue directory.

    Reads source file paths from a queue and copies them into a date-based
    directory structure inside the catalogue dir (``YYYY/YYYY-MM-DD/filename``).
    Duplicate files (matching SHA-256 checksum) are skipped.

    Uses a ``ThreadPoolExecutor`` for I/O parallelism.  Progress is tracked
    via ``_import_progress`` on the owning ``ImageDatabase`` so ``/api/status``
    can report live numbers.

    On graceful shutdown, remaining queue items are persisted to
    ``<catalogue_dir>/.pending_import.json`` for recovery on next startup.

    Follows the same daemon-thread pattern as :class:`TrashWorker`.
    """

    def __init__(
        self,
        import_queue: queue.Queue[str],
        stop_event: threading.Event,
        catalogue_dir: Path,
        max_workers: int,
        progress: dict | None,
        progress_lock: threading.Lock,
        checksum_cache: dict[str, str],
        checksum_cache_lock: threading.Lock,
        image_extensions: set[str],
        import_names: dict[str, str],
        import_names_lock: threading.Lock,
        on_complete: Callable[[dict], None] | None = None,
    ):
        """Initialise the import worker thread.

        Args:
            import_queue: Queue of source file paths to import.
            stop_event: Event to signal thread should stop.
            catalogue_dir: Destination catalogue directory.
            max_workers: Number of threads in the file-copy pool.
            progress: Shared ``_import_progress`` dict reference (may be None).
            progress_lock: Lock protecting ``_import_progress`` mutations.
            checksum_cache: Shared image_id -> checksum cache for dedup.
            checksum_cache_lock: Lock protecting checksum cache reads.
            image_extensions: Set of supported image file extensions.
            import_names: Shared dict mapping catalogue dest path to original
                filename, consumed by ``_process_image()`` during ingestion.
            import_names_lock: Lock protecting the import_names dict.
            on_complete: Callback invoked when all queued files have been
                processed (``done >= total``).  Receives the final progress
                snapshot dict.  Called from the ImportWorker thread.
        """
        super().__init__(name='ImportWorker', daemon=True)
        self._queue = import_queue
        self._stop_event = stop_event
        self._catalogue_dir = catalogue_dir
        self._max_workers = max_workers
        self._progress = progress
        self._progress_lock = progress_lock
        self._checksum_cache = checksum_cache
        self._checksum_cache_lock = checksum_cache_lock
        self._image_extensions = image_extensions
        self._on_complete = on_complete
        self._import_names = import_names
        self._import_names_lock = import_names_lock

    def run(self) -> None:
        """Main loop - drain queue items and copy files in parallel.

        Creates a single :class:`ThreadPoolExecutor` that lives for the
        entire thread lifetime (matching the :class:`IngestionThread`
        pattern).  Creating a fresh executor per batch caused hangs on
        Windows where ``shutdown(wait=True)`` would occasionally block
        indefinitely even after all futures had completed.
        """
        logger.info('Import worker thread started')

        # A long-lived executor avoids per-batch shutdown overhead and
        # the Windows hang observed with per-batch executor lifecycle.
        executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix='import-copy',
        )

        try:
            while not self._stop_event.is_set():
                # Block briefly for the first item
                batch: list[str] = []
                try:
                    item = self._queue.get(timeout=0.2)
                    batch.append(item)
                    # Drain up to 200 more without blocking (batched I/O)
                    while len(batch) < 200:
                        try:
                            batch.append(self._queue.get_nowait())
                        except queue.Empty:
                            break
                except queue.Empty:
                    continue

                # Copy files in parallel
                logger.info(f'ImportWorker: processing batch of {len(batch)} file(s)')
                self._process_batch(batch, executor)

                # Check if all queued files have been processed.
                # This is the definitive completion check — it runs in the
                # ImportWorker thread immediately after the last file is
                # processed, rather than depending on external status polling.
                self._check_completion()

                # Yield GIL briefly
                time.sleep(0.01)

            # On shutdown, drain remaining items and persist for recovery
            remaining = self._drain_remaining()
            if remaining:
                self._persist_pending(remaining)

        except Exception:
            logger.exception('ImportWorker: thread crashed')
        finally:
            # Shut down the executor without blocking.  Worker threads
            # exit on their own via the sentinel chain and Python's atexit
            # handler joins them if needed.
            executor.shutdown(wait=False, cancel_futures=True)

        logger.info('Import worker thread stopped')

    def _process_batch(self, batch: list[str], executor: ThreadPoolExecutor) -> None:
        """Copy a batch of source files into the catalogue directory.

        Args:
            batch: List of source file paths.
            executor: Long-lived thread pool for parallel file copies.
        """
        # Snapshot known checksums once per batch to avoid O(M*N) set
        # creation inside the per-file loop.  The set may go slightly
        # stale within a batch, but that's fine - worst case a file is
        # re-imported and the ingestion pipeline deduplicates on checksum.
        with self._checksum_cache_lock:
            known_checksums = set(self._checksum_cache.values())

        futures = [executor.submit(self._copy_one, path, known_checksums) for path in batch]
        for future in futures:
            # Check stop event between futures so large batches can be
            # interrupted without waiting for all copies to finish.
            if self._stop_event.is_set():
                for f in futures:
                    f.cancel()
                break
            try:
                future.result()
            except Exception as e:
                logger.error(f'ImportWorker: executor error: {e}')

    def _is_staging_file(self, path: Path) -> bool:
        """Check if a file is inside the upload staging directory.

        Staging files are temporary copies created by the upload route
        (``/api/import/upload``) and should be deleted after the
        ImportWorker has processed them (copied or skipped).  Files
        from desktop imports (local paths) must NOT be deleted.

        Args:
            path: Path to check.

        Returns:
            True if the file is inside ``.import-staging/`` under the
            catalogue directory.
        """
        staging_dir = self._catalogue_dir / '.import-staging'
        try:
            path.resolve().relative_to(staging_dir.resolve())
            return True
        except ValueError:
            return False

    def _cleanup_staging_file(self, src: Path) -> None:
        """Delete a staging file and its empty parent batch directory.

        Only deletes files under ``.import-staging/``.  Silently ignores
        errors (the file may already be gone or locked).

        Args:
            src: Path to the staging file.
        """
        if not self._is_staging_file(src):
            return
        try:
            src.unlink(missing_ok=True)
            # Remove the per-upload batch directory if now empty
            batch_dir = src.parent
            staging_dir = self._catalogue_dir / '.import-staging'
            if batch_dir != staging_dir:
                try:
                    batch_dir.rmdir()  # Only succeeds if empty
                except OSError:
                    pass
        except Exception as e:
            logger.debug(f'ImportWorker: staging cleanup for {src.name}: {e}')

    def _copy_one(self, source_path: str, known_checksums: set[str]) -> None:
        """Copy a single source image into the catalogue directory.

        Validates the source file, checks for duplicates via SHA-256
        checksum, derives a date-based subdirectory from EXIF or mtime,
        handles filename collisions, and records the original name for
        the ingestion pipeline's ``import_name`` column.

        If the source is a staging file (from the upload route), it is
        deleted after processing regardless of outcome.

        Args:
            source_path: Absolute path to the source image file.
            known_checksums: Snapshot of checksums already in the database.
        """
        import shutil

        try:
            src = Path(source_path)
            if not src.is_file():
                logger.warning(f'ImportWorker: source not found: {source_path}')
                with self._progress_lock:
                    if self._progress is not None:
                        self._progress['skipped'] += 1
                        self._progress['done'] += 1
                return

            # Validate it's a supported image extension
            if src.suffix.lower() not in self._image_extensions:
                logger.debug(f'ImportWorker: skipping unsupported extension: {src.suffix}')
                with self._progress_lock:
                    if self._progress is not None:
                        self._progress['skipped'] += 1
                        self._progress['done'] += 1
                self._cleanup_staging_file(src)
                return

            # Compute SHA-256 checksum of source file
            sha256 = hashlib.sha256()
            with open(src, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    sha256.update(chunk)
            src_checksum = sha256.hexdigest()

            # Check if this checksum already exists in the database
            if src_checksum in known_checksums:
                logger.info(f'ImportWorker: duplicate checksum, skipping: {src.name}')
                with self._progress_lock:
                    if self._progress is not None:
                        self._progress['skipped'] += 1
                        self._progress['done'] += 1
                self._cleanup_staging_file(src)
                return

            # Derive date for subdirectory from file metadata or mtime
            file_date = self._get_file_date(src)
            year_dir = file_date.strftime('%Y')
            date_dir = file_date.strftime('%Y-%m-%d')
            dest_dir = self._catalogue_dir / year_dir / date_dir
            dest_dir.mkdir(parents=True, exist_ok=True)

            # Build destination path, handling name collisions
            dest = dest_dir / src.name
            if dest.exists():
                # Check if the existing file has the same checksum
                existing_sha256 = hashlib.sha256()
                with open(dest, 'rb') as f:
                    for chunk in iter(lambda: f.read(65536), b''):
                        existing_sha256.update(chunk)
                if existing_sha256.hexdigest() == src_checksum:
                    logger.info(f'ImportWorker: identical file at dest, skipping: {src.name}')
                    with self._progress_lock:
                        if self._progress is not None:
                            self._progress['skipped'] += 1
                            self._progress['done'] += 1
                    self._cleanup_staging_file(src)
                    return
                # Different file with same name - append counter
                stem = src.stem
                suffix = src.suffix
                counter = 1
                while dest.exists():
                    dest = dest_dir / f'{stem}_{counter}{suffix}'
                    counter += 1

            # Copy file preserving timestamps
            shutil.copy2(str(src), str(dest))
            logger.info(f'ImportWorker: copied {src.name} -> {dest}')

            # Clean up the staging copy now that we've successfully copied
            self._cleanup_staging_file(src)

            # Record the original filename so the ingestion pipeline can
            # set import_name on the image record.  This allows preflight
            # dedup to match by original name even if the catalogue copy
            # was renamed to avoid a collision (e.g. IMG_1234_1.jpg).
            canon_dest = str(canonicalise_path(dest))
            with self._import_names_lock:
                self._import_names[canon_dest] = src.name

            with self._progress_lock:
                if self._progress is not None:
                    self._progress['done'] += 1

        except Exception as e:
            logger.error(f'ImportWorker: failed to import {source_path}: {e}')
            # Still try to clean up staging even on failure
            try:
                self._cleanup_staging_file(Path(source_path))
            except Exception:
                pass
            with self._progress_lock:
                if self._progress is not None:
                    self._progress['done'] += 1

    def _check_completion(self) -> None:
        """Check if all queued import files have been processed.

        Called after each batch.  When ``done >= total`` in the progress
        dict, fires the ``on_complete`` callback with a snapshot of the
        final progress and clears the progress reference so the callback
        fires exactly once per import operation.
        """
        with self._progress_lock:
            if self._progress is None:
                return
            done = self._progress.get('done', 0)
            total = self._progress.get('total', 0)
            if total <= 0 or done < total:
                return
            # Snapshot and clear so the callback fires exactly once
            final_progress = dict(self._progress)
            self._progress = None

        # Fire callback outside the lock
        if self._on_complete:
            try:
                self._on_complete(final_progress)
            except Exception:
                logger.exception('ImportWorker: on_complete callback failed')

    @staticmethod
    def _get_file_date(path: Path) -> datetime:
        """Get the best date for organizing the file into date-based directories.

        Tries EXIF DateTimeOriginal first, then falls back to the file's
        modification time. This determines which ``YYYY/YYYY-MM-DD/`` subdirectory
        the file lands in.

        Args:
            path: Path to the image file.

        Returns:
            datetime for the file's date.
        """
        try:
            from metadata import derive_timestamp

            ts = derive_timestamp(path)
            if ts is not None:
                return ts
        except Exception:
            pass
        # Fallback to file modification time
        return datetime.fromtimestamp(path.stat().st_mtime)

    def _drain_remaining(self) -> list[str]:
        """Drain any items left in the queue after stop_event is set.

        Returns:
            List of un-processed source file paths.
        """
        remaining: list[str] = []
        while True:
            try:
                remaining.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return remaining

    def _persist_pending(self, items: list[str]) -> None:
        """Write un-processed items to disk for crash recovery.

        Args:
            items: List of source file paths to persist.
        """
        pending_path = self._catalogue_dir / '.pending_import.json'
        try:
            pending_path.write_text(
                json.dumps(items),
                encoding='utf-8',
            )
            logger.info(f'Persisted {len(items)} pending import items to {pending_path}')
        except Exception as e:
            logger.error(f'Failed to persist pending import items: {e}')


# =============================================================================
# IMAGE DATABASE CLASS (STARTUP SEQUENCE)
# =============================================================================


class ImageDatabase:
    """Main image database class that coordinates all components.

    This class manages the database connection, background threads, and
    provides the public API for the Flask application.

    Startup sequence:
        0. Load or create configuration YAML file
        1. Open/create SQLite database with WAL mode
        2. Create tables if they don't exist
        3. Verify registered folders still exist on disk
        4. Rescan all registered directories
        5. Queue images with missing embeddings
        6. Start ingestion thread
        7. Start embedding thread

    Attributes:
        db_path: Path to the SQLite database file.
        thumbnail_dir: Path to thumbnail cache directory.
        config: Configuration object.
        conn: Database connection.
        event_queue: Event queue for SSE.
    """

    def __init__(
        self,
        db_path: Path | str = 'photonarium.db',
        thumbnail_dir: Path | str = '.thumbnails',
        config_path: Path | str | None = None,
        config: Config | None = None,
        auto_start: bool = True,
        preload_model: bool = True,
        run_scan: bool = False,
        run_face_detection: bool = False,
        run_face_grouping: bool = False,
    ):
        """Initialise the image database.

        Args:
            db_path: Path to the SQLite database file.
            thumbnail_dir: Path to thumbnail cache directory.
            config_path: Path to configuration file. If None, uses default.
                Ignored when ``config`` is provided.
            config: Pre-loaded Config object. When provided, the config is
                used directly instead of loading from ``config_path``. This
                avoids a redundant load when the caller has already resolved
                the config (e.g. app.py's startup sequence).
            auto_start: If True, start background threads automatically.
            preload_model: If True, load the OpenCLIP model during startup
                instead of lazily on first use. This provides better console
                feedback during first-time setup.
            run_scan: If True, scan folders and queue embeddings on startup.
                If False (default), just start the server without processing.
            run_face_detection: If True, run face detection after embeddings.
                Requires run_scan=True to have any effect.
            run_face_grouping: If True, compute face/duplicate groups after
                face detection. Requires run_face_detection=True.

            Use the GUI "Rescan" button to trigger all processing phases.
        """
        self._preload_model = preload_model
        self._run_scan = run_scan
        self._run_face_detection = run_face_detection
        self._run_face_grouping = run_face_grouping
        self.db_path = Path(db_path)
        self.thumbnail_dir = Path(thumbnail_dir)

        logger.info('=' * 60)
        logger.info('PHOTONARIUM - Image Catalogue Backend')
        logger.info('=' * 60)

        # Step 0: Load configuration (use pre-loaded Config if provided)
        logger.info('[1/5] Loading configuration...')
        self.config = config if config is not None else load_config(config_path)

        # Step 1-2: Initialise database
        logger.info('[2/5] Initialising database...')
        self.conn = init_database(self.db_path)
        logger.info(f'        Database: {self.db_path.absolute()}')

        # Create event queue
        self.event_queue = EventQueue()

        # Create thread control events
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        # Create locks for thread safety
        self._db_lock = threading.RLock()  # Reentrant lock for nested calls
        self._image_locks: dict[str, threading.Lock] = {}  # Per-image locks for rotation
        self._image_locks_lock = threading.Lock()  # Lock for the locks dict
        self._active_rotations = 0  # Count of in-flight rotation operations
        self._rotations_lock = threading.Lock()  # Lock for the counter
        self._rotations_done = threading.Condition(self._rotations_lock)  # Signal when rotations complete

        # Trash directory state — resolved in startup() after folders are verified
        self._trash_progress: dict | None = None  # {total, done, started_at} during trash ops
        self._trash_progress_lock = threading.Lock()  # Protects _trash_progress mutations

        # Import directory state — resolved in startup() after folders are verified
        self._import_progress: dict | None = None  # {total, done, skipped, started_at}
        self._import_progress_lock = threading.Lock()  # Protects _import_progress mutations
        # Maps catalogue destination path → original import filename, so the
        # ingestion pipeline can set import_name when the file is first indexed.
        # Populated by ImportWorker, consumed by _process_image().
        self._import_names: dict[str, str] = {}
        self._import_names_lock = threading.Lock()

        # Create queues
        self._ingestion_queue: queue.Queue[Path] = queue.Queue()
        self._embedding_queue: queue.Queue[str] = queue.Queue()
        self._face_queue: queue.Queue[str] = queue.Queue()
        self._nima_queue: queue.Queue[str] = queue.Queue()
        self._trash_queue: queue.Queue[tuple[str, str]] = queue.Queue()  # (image_id, file_path)
        self._import_queue: queue.Queue[str] = queue.Queue()  # source file paths

        # Thread references (created when started)
        self._ingestion_thread: IngestionThread | None = None
        self._embedding_thread: EmbeddingThread | None = None
        self._face_thread: FaceDetectionThread | None = None
        self._nima_thread: NimaThread | None = None
        self._trash_thread: TrashWorker | None = None
        self._import_thread: ImportWorker | None = None

        # Phase 4 status tracking (post-processing after queues empty)
        self._phase4_status_lock = threading.Lock()
        self._face_embedding_status: dict[str, Any] = {'status': 'idle'}
        self._face_reassess_status: dict[str, Any] | None = None

        # Track if we've been closed
        self._closed = False

        # Duplicate manager handles duplicate detection across all 4 levels
        self._duplicate_manager = DuplicateManager(str(self.db_path), self.config)

        # RAM cache for image_id -> checksum lookups (avoids DB query per thumbnail)
        self._checksum_cache: dict[str, str] = {}
        self._checksum_cache_lock = threading.Lock()
        self._load_checksum_cache()

        # Ensure thumbnail directory exists
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)

        if auto_start:
            self.startup()

    def startup(self) -> None:
        """Run the startup sequence.

        Steps 3-7 of the startup sequence. Call this if auto_start=False
        was passed to __init__.

        Uses self._run_scan, self._run_face_detection, and self._run_face_grouping
        to determine which processing phases to run.
        """
        # Step 3: Verify registered folders exist
        logger.info('[3/5] Verifying registered folders...')
        missing_folders = verify_folders_exist(self.conn)
        for folder in missing_folders:
            logger.warning(f'        Folder missing: {folder}')

        # Initialise trash directory (after folders are verified)
        self._init_trash_dir()

        # Initialise import/catalogue directory
        self._init_import_dir()

        # Step 4: Optionally scan folders and queue processing
        if self._run_scan:
            logger.info('[4/5] Scanning registered folders...')
            self._rescan_all_folders()

            # Queue images with missing embeddings
            missing_embeddings = get_images_without_embedding(self.conn)
            for image in missing_embeddings:
                self._embedding_queue.put(image['id'])
            if missing_embeddings:
                logger.info(f'        {len(missing_embeddings)} images queued for image embedding')
        else:
            logger.info('[4/5] Skipping scan (use --scan or GUI Rescan button to process)')

        # Step 5: Start background threads (idle until work is queued)
        logger.info('[5/5] Starting background threads...')

        # Run one-time migrations BEFORE starting threads
        # (duplicate epoch migration must complete before completion callback fires)
        self._migrate_recalculate_timestamps()
        self._migrate_duplicate_epoch_to_metadata()
        self._migrate_add_timestamp_confidence()
        self._migrate_renumber_custom_groups_to_level5()
        self._migrate_initial_directory_groups()
        self._migrate_add_exif_metadata()

        # Steps 6-7: Start background threads
        self.start_threads()

        # Optionally pre-load OpenCLIP model to show download progress during startup
        if self._preload_model and self._embedding_thread is not None:
            logger.info('Pre-loading OpenCLIP model...')
            # Access the clip_model property to trigger loading
            _ = self._embedding_thread.clip_model

        # Backfill description embeddings for images with descriptions but no embedding
        self._backfill_description_embeddings()

        # Backfill LAION aesthetic scores for images with embeddings but no score
        self._backfill_aesthetic_laion()

        # NIMA model invalidation — wipe stale scores if model identity changed
        self._invalidate_nima_model()

        # Queue existing images for NIMA scoring (backfill)
        self._queue_images_for_nima()

        logger.info('-' * 60)
        logger.info('Database initialisation complete')
        logger.info('-' * 60)

    def _backfill_description_embeddings(self) -> None:
        """Compute description embeddings for images that have descriptions but no embedding.

        This runs during startup to handle images that had descriptions added
        before the description_embedding feature was implemented.
        """
        # Find images with descriptions but no description embedding
        cursor = self.conn.execute("""
            SELECT id, description
            FROM images
            WHERE deleted = 0
              AND description IS NOT NULL
              AND description != ''
              AND description_embedding IS NULL
        """)
        rows = cursor.fetchall()

        if not rows:
            return

        logger.info(f'Backfilling {len(rows)} description embeddings...')

        clip_model = self._get_clip_model()
        count = 0

        for row in rows:
            image_id = row['id']
            description = row['description']

            try:
                embedding = clip_model.encode_text(description)
                embedding_bytes = embedding.astype(np.float32).tobytes()

                with self._db_lock:
                    self.conn.execute(
                        'UPDATE images SET description_embedding = ? WHERE id = ?', (embedding_bytes, image_id)
                    )
                count += 1
            except Exception as e:
                logger.warning(f'Failed to compute description embedding for {image_id}: {e}')

        with self._db_lock:
            self.conn.commit()
        logger.info(f'        Backfilled {count} description embeddings')

    def _backfill_aesthetic_laion(self) -> None:
        """Compute LAION aesthetic scores for images with embeddings but no score.

        This is a cheap operation — just dot products on existing embedding blobs,
        no image I/O required. Uses the has_migration_run/record_migration pattern
        to run only once per database.

        Respects _stop_event for graceful shutdown. Acquires _db_lock for writes
        since the embedding thread may be running concurrently.

        Requires the LAION head to be loaded in the embedding thread; skips
        gracefully if the head is unavailable.
        """
        migration_id = 'backfill_aesthetic_laion'
        if has_migration_run(self.conn, migration_id):
            return

        # Get LAION head weights from the embedding thread
        if self._embedding_thread is None:
            return

        # Trigger lazy loading of the LAION head (thread-safe)
        self._embedding_thread._load_laion_head()
        laion_weight = self._embedding_thread._laion_weight
        laion_bias = self._embedding_thread._laion_bias

        if laion_weight is None:
            # LAION head not available — record migration anyway to avoid
            # re-checking every startup (user can re-download and re-run)
            logger.info('Skipping LAION aesthetic backfill — head not available')
            record_migration(self.conn, migration_id)
            return

        cursor = self.conn.execute("""
            SELECT id, embedding
            FROM images
            WHERE embedding IS NOT NULL AND aesthetic_laion IS NULL AND deleted = 0
        """)
        rows = cursor.fetchall()

        if not rows:
            record_migration(self.conn, migration_id)
            return

        logger.info(f'Backfilling LAION aesthetic scores for {len(rows)} images...')

        updates = []
        for row in rows:
            # Check for shutdown between rows
            if self._stop_event.is_set():
                logger.info('LAION aesthetic backfill interrupted by shutdown')
                break

            try:
                embedding = np.frombuffer(row['embedding'], dtype=np.float32)
                score = float(embedding @ laion_weight + laion_bias)
                updates.append((score, datetime.now().isoformat(), row['id']))
            except Exception as e:
                logger.warning(f'Failed to compute aesthetic score for {row["id"]}: {e}')

        # Commit whatever we computed (even on early exit from shutdown).
        # Acquire _db_lock since the embedding thread may be writing concurrently.
        if updates:
            with self._db_lock:
                self.conn.executemany('UPDATE images SET aesthetic_laion = ?, updated_at = ? WHERE id = ?', updates)
                self.conn.commit()

        # Only record migration as complete if we weren't interrupted
        if not self._stop_event.is_set():
            record_migration(self.conn, migration_id)
            logger.info(f'        Backfilled {len(updates)} LAION aesthetic scores')
        else:
            logger.info(f'        Partially backfilled {len(updates)} LAION aesthetic scores (interrupted)')

    def backfill_face_semantic_embeddings(self) -> int:
        """Generate semantic embeddings for faces that don't have them.

        Loads each face thumbnail and encodes it with OpenCLIP for text search.
        Respects _stop_event for graceful shutdown.

        Returns:
            Number of faces updated.
        """
        face_ids = get_faces_without_semantic_embedding(self.conn)

        if not face_ids:
            return 0

        total = len(face_ids)
        logger.info(f'Backfilling CLIP embeddings for {total} faces (for text search)...')

        # Set status to computing
        with self._phase4_status_lock:
            self._face_embedding_status = {
                'status': 'computing',
                'current': 0,
                'total': total,
            }

        clip_model = self._get_clip_model()
        count = 0

        try:
            for i, face_id in enumerate(face_ids):
                # Check for shutdown
                if self._stop_event.is_set():
                    logger.info('Face CLIP embedding backfill interrupted')
                    break

                thumb_path = get_face_thumbnail_path(face_id, self.thumbnail_dir)

                if not thumb_path.exists():
                    logger.debug(f'Face thumbnail not found for {face_id}, skipping')
                    continue

                try:
                    embedding = clip_model.encode_image(thumb_path)
                    if embedding is not None:
                        update_face_semantic_embedding(self.conn, face_id, embedding)
                        count += 1
                except Exception as e:
                    logger.warning(f'Failed to compute CLIP embedding for face {face_id}: {e}')

                # Update progress status
                with self._phase4_status_lock:
                    self._face_embedding_status['current'] = i + 1

                # Progress logging every 100 faces
                if (i + 1) % 100 == 0:
                    logger.info(f'  Progress: {i + 1}/{total} faces, {count} CLIP embeddings generated')

            logger.info(f'Backfilled {count} face CLIP embeddings')
        finally:
            # Set status to idle when done
            with self._phase4_status_lock:
                self._face_embedding_status = {'status': 'idle'}

        return count

    def regenerate_face_thumbnails(self) -> int:
        """Regenerate all face thumbnails with improved non-distorted rendering.

        For non-square face crops, creates a square thumbnail with a blurred,
        darkened background and the undistorted face centered on top.

        Groups faces by image and processes in parallel using a thread pool.
        Respects _stop_event for graceful shutdown.

        Returns:
            Number of thumbnails regenerated.
        """
        faces = get_all_faces_for_thumbnail_regen(self.conn)

        if not faces:
            return 0

        # Group faces by image for efficient processing
        faces_by_image: dict[str, list[dict]] = defaultdict(list)
        for face in faces:
            faces_by_image[face['image_path']].append(face)

        total_faces = len(faces)
        total_images = len(faces_by_image)
        logger.info(f'Regenerating {total_faces} face thumbnails from {total_images} images...')

        # Worker function for thread pool
        def process_image(image_path: str, image_faces: list[dict]) -> int:
            if self._stop_event.is_set():
                return 0
            path = Path(image_path)
            if not path.exists():
                return 0
            return generate_face_thumbnails_for_image(
                path,
                image_faces,
                self.thumbnail_dir,
                size=200,
                quality=self.config.thumbnail_quality,
            )

        count = 0
        images_processed = 0
        num_workers = self.config.indexing_threads or 4

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(process_image, image_path, image_faces): image_path
                for image_path, image_faces in faces_by_image.items()
            }

            for future in as_completed(futures):
                # Check for shutdown
                if self._stop_event.is_set():
                    logger.info('Face thumbnail regeneration interrupted')
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                try:
                    count += future.result()
                except Exception as e:
                    logger.warning(f'Error processing image: {e}')
                images_processed += 1

                # Progress logging every 100 images
                if images_processed % 100 == 0:
                    logger.info(f'  Progress: {images_processed}/{total_images} images, {count} thumbnails generated')

        logger.info(f'Regenerated {count} face thumbnails from {images_processed} images')
        return count

    def _migrate_recalculate_timestamps(self) -> None:
        """One-time migration to recalculate timestamps using improved logic.

        This migration re-derives timestamps for all existing images using the
        updated timestamp derivation order (filename before filesystem) and
        support for partial dates (year-only defaults to January 1st).

        Only runs once; tracked via the migrations table.
        """
        migration_id = 'recalculate_timestamps_v1'

        if has_migration_run(self.conn, migration_id):
            return

        # Get all non-deleted images
        cursor = self.conn.execute("""
            SELECT id, path
            FROM images
            WHERE deleted = 0
        """)
        rows = cursor.fetchall()

        if not rows:
            record_migration(self.conn, migration_id)
            return

        logger.info(f'Recalculating timestamps for {len(rows)} images (one-time migration)...')

        updated = 0
        for row in rows:
            image_id = row['id']
            path = row['path']

            try:
                new_timestamp = derive_timestamp(path)
                if new_timestamp:
                    self.conn.execute(
                        'UPDATE images SET timestamp = ? WHERE id = ?', (new_timestamp.isoformat(), image_id)
                    )
                    updated += 1
            except Exception as e:
                logger.warning(f'Failed to recalculate timestamp for {path}: {e}')

        self.conn.commit()
        record_migration(self.conn, migration_id)
        logger.info(f'        Updated {updated} image timestamps')

    def _migrate_duplicate_epoch_to_metadata(self) -> None:
        """One-time migration to move duplicate epoch from duplicate_groups to metadata table.

        Previously, the epoch was derived from MAX(duplicate_groups.updated_at), which
        fails when there are no duplicate groups (returns NULL). This migration transfers
        the epoch to the metadata table where it's tracked independently.
        """
        migration_id = 'duplicate_epoch_to_metadata_v1'

        if has_migration_run(self.conn, migration_id):
            return

        # Check if we already have an epoch in metadata (shouldn't happen, but be safe)
        existing_epoch = get_metadata(self.conn, 'duplicate_epoch')
        if existing_epoch:
            record_migration(self.conn, migration_id)
            return

        # Get the old epoch from duplicate_groups (if any groups exist)
        cursor = self.conn.execute('SELECT MAX(updated_at) as epoch FROM duplicate_groups')
        row = cursor.fetchone()
        old_epoch = row['epoch'] if row and row['epoch'] else None

        if old_epoch:
            logger.info(f'Migrating duplicate epoch to metadata table: {old_epoch}')
            set_metadata(self.conn, 'duplicate_epoch', old_epoch)
        else:
            logger.debug('No existing duplicate epoch to migrate')

        record_migration(self.conn, migration_id)

    def _migrate_add_timestamp_confidence(self) -> None:
        """One-time migration to add timestamp_confidence to all images.

        Re-derives timestamps for all images to determine the confidence level
        (0=user, 1=EXIF, 2=filename, 3=filesystem, 4=unknown).

        Only runs once; tracked via the migrations table.
        """
        migration_id = 'add_timestamp_confidence_v1'

        if has_migration_run(self.conn, migration_id):
            return

        # Get all non-deleted images
        cursor = self.conn.execute("""
            SELECT id, path
            FROM images
            WHERE deleted = 0
        """)
        rows = cursor.fetchall()

        if not rows:
            record_migration(self.conn, migration_id)
            return

        logger.info(f'Adding timestamp_confidence for {len(rows)} images (one-time migration)...')

        updated = 0
        for row in rows:
            image_id = row['id']
            path = row['path']

            try:
                new_timestamp, confidence = derive_timestamp_with_confidence(path)
                timestamp_str = new_timestamp.isoformat() if new_timestamp else None
                self.conn.execute(
                    'UPDATE images SET timestamp = ?, timestamp_confidence = ? WHERE id = ?',
                    (timestamp_str, confidence, image_id),
                )
                updated += 1
            except Exception as e:
                logger.warning(f'Failed to derive timestamp confidence for {path}: {e}')

        self.conn.commit()
        record_migration(self.conn, migration_id)
        logger.info(f'        Updated {updated} images with timestamp_confidence')

    def _migrate_renumber_custom_groups_to_level5(self) -> None:
        """One-time migration to move custom groups from level 4 to level 5.

        This makes room for directory groups at level 4. All existing custom
        group membership rows in duplicate_groups are renumbered.

        Only runs once; tracked via the migrations table.
        """
        migration_id = 'renumber_custom_groups_to_level5'

        if has_migration_run(self.conn, migration_id):
            return

        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM duplicate_groups WHERE level = 4')
        count = cursor.fetchone()['cnt']

        if count > 0:
            self.conn.execute('UPDATE duplicate_groups SET level = 5 WHERE level = 4')
            self.conn.commit()
            logger.info(f'Migrated {count} custom group membership rows from level 4 to level 5')
        else:
            logger.debug('No custom group rows to migrate (level 4 → 5)')

        record_migration(self.conn, migration_id)

    def _migrate_initial_directory_groups(self) -> None:
        """One-time migration to create directory groups for existing images.

        For databases that already have indexed images but were created before
        the directory groups feature existed, this runs sync_directory_groups()
        once at startup so that the Directories level is immediately populated.

        Subsequent syncs happen automatically at the end of each processing cycle.

        Only runs once; tracked via the migrations table.
        """
        migration_id = 'initial_directory_groups'

        if has_migration_run(self.conn, migration_id):
            return

        # Check if there are any images to group
        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM images WHERE deleted = 0')
        count = cursor.fetchone()['cnt']

        if count > 0:
            logger.info(f'Creating initial directory groups for {count} images (one-time migration)...')
            self._duplicate_manager.sync_directory_groups(self.conn, self._db_lock)
        else:
            logger.debug('No images to create directory groups for')

        record_migration(self.conn, migration_id)

    def _migrate_add_exif_metadata(self) -> None:
        """One-time migration to note the EXIF metadata schema addition.

        The exif_data column and image_metadata table are created by init_db().
        This migration logs the change and notes that a rescan will backfill
        EXIF data for existing images automatically.

        Only runs once; tracked via the migrations table.
        """
        migration_id = 'add_exif_metadata_v1'

        if has_migration_run(self.conn, migration_id):
            return

        # Count images that need EXIF extraction
        cursor = self.conn.execute('SELECT COUNT(*) as cnt FROM images WHERE deleted = 0 AND exif_data IS NULL')
        count = cursor.fetchone()['cnt']

        if count > 0:
            logger.info(
                f'Added EXIF metadata support — {count} images will be backfilled on next rescan (one-time migration)'
            )
        else:
            logger.info('Added EXIF metadata support (one-time migration)')

        record_migration(self.conn, migration_id)

    def _load_checksum_cache(self) -> None:
        """Load all image_id -> checksum mappings into RAM.

        Called during startup to populate the cache. This eliminates
        DB queries for thumbnail lookups.
        """
        cursor = self.conn.execute('SELECT id, checksum FROM images WHERE checksum IS NOT NULL AND deleted = 0')
        cache = {row[0]: row[1] for row in cursor}
        with self._checksum_cache_lock:
            self._checksum_cache = cache
        logger.info(f'        Loaded {len(cache)} checksums into cache')

        # Warn about images missing checksums — these will 404 on
        # thumbnail/histogram requests until re-scanned
        missing = self.conn.execute('SELECT COUNT(*) FROM images WHERE checksum IS NULL AND deleted = 0').fetchone()[0]
        if missing:
            logger.warning(f'        {missing} image(s) have NULL checksums — run with --scan to repair')

    def _rescan_all_folders(self) -> None:
        """Rescan all registered folders for new/changed/deleted files."""
        folders = get_folders(self.conn)
        folder_paths = [f['path'] for f in folders]

        if not folder_paths:
            logger.info('        No folders registered yet')
            return

        logger.info(f'        {len(folder_paths)} folder(s) to scan')

        # Get all currently known image paths
        all_images = get_all_images(self.conn, include_deleted=False)
        known_paths = {img['path'] for img in all_images}
        found_paths: set[str] = set()

        # Scan each folder
        for folder_path in folder_paths:
            folder = Path(folder_path)
            if not folder.exists():
                continue

            logger.info(f'        Scanning: {folder}')

            # Find all images in folder
            for image_path in find_images_in_folder(
                folder,
                self.config.image_extensions,
                registered_folders=folder_paths,
            ):
                path_str = str(image_path)
                found_paths.add(path_str)

                # Queue for ingestion (thread will skip unchanged)
                self._ingestion_queue.put(image_path)

        # Mark missing files as deleted
        missing_paths = known_paths - found_paths
        if missing_paths:
            logger.info(f'Marking {len(missing_paths)} missing images as deleted')
            now = datetime.now().isoformat()
            with self._db_lock:
                for path in missing_paths:
                    self.conn.execute(
                        'UPDATE images SET deleted = 1, updated_at = ? WHERE path = ? AND deleted = 0', (now, path)
                    )
                self.conn.commit()

        logger.info(f'        Found {len(found_paths)} images')

    def _queue_images_for_face_detection(self) -> None:
        """Queue all images that need face detection.

        Finds images that don't have any face records (including suppressed)
        and queues them for face detection processing.
        """
        if not self.config.face_detection_enabled:
            return

        # Find images without face detection
        cursor = self.conn.execute("""
            SELECT i.id
            FROM images i
            WHERE i.deleted = 0
              AND i.embedding IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM faces f WHERE f.image_id = i.id
              )
        """)
        rows = cursor.fetchall()

        if rows:
            logger.info(f'Queueing {len(rows)} images for face detection')
            for row in rows:
                self._face_queue.put(row['id'])

    def _queue_images_for_nima(self) -> None:
        """Queue all images that need NIMA aesthetic scoring.

        Finds images with a checksum (so thumbnails exist) but no NIMA score.
        Called during startup for backfill of existing images.
        """
        if not self.config.nima_enabled:
            return

        cursor = self.conn.execute("""
            SELECT id FROM images
            WHERE aesthetic_nima IS NULL AND deleted = 0 AND checksum IS NOT NULL
        """)
        rows = cursor.fetchall()

        if rows:
            logger.info(f'Queueing {len(rows)} images for NIMA scoring')
            for row in rows:
                self._nima_queue.put(row['id'])

    def _invalidate_nima_model(self) -> None:
        """Check if the NIMA model identity has changed and wipe stale scores.

        Stores the current NIMA model name in the metadata table.  If the
        stored value differs from the config, all aesthetic_nima scores are
        cleared and images re-queued for scoring.
        """
        current_model = 'mobilenetv2-ava'  # Only one model currently; future-proofing
        stored_model = get_metadata(self.conn, 'nima_model')

        if stored_model == current_model:
            return  # No change

        if stored_model is not None:
            # Model has changed — wipe all NIMA scores
            logger.info(
                f'NIMA model changed ({stored_model} → {current_model}), clearing existing scores for re-computation'
            )
            with self._db_lock:
                self.conn.execute('UPDATE images SET aesthetic_nima = NULL')
                self.conn.commit()

        set_metadata(self.conn, 'nima_model', current_model)

    def start_threads(self) -> None:
        """Start the background processing threads."""
        if self._ingestion_thread is not None and self._ingestion_thread.is_alive():
            logger.warning('Threads already running')
            return

        self._stop_event.clear()

        # Final completion callback (after all processing including faces)
        def on_final_complete():
            # Sync directory groups — lightweight DB operation that mirrors
            # filesystem folders as browse-able groups. Runs regardless of
            # whether face grouping was requested.
            self._duplicate_manager.sync_directory_groups(self.conn, self._db_lock)

            if not self._run_face_grouping:
                logger.info('Skipping grouping phase (use --group-faces or GUI Rescan)')
                emit_processing_complete(self.event_queue)
                return

            # --- Face reassessment FIRST (user-visible, latency-sensitive) ---
            # Match unknown faces against known people before the slower
            # duplicate/grouping calculations so that newly imported faces
            # get identified as quickly as possible.
            if self.config.face_detection_enabled:
                # Clean up people with no faces
                with self._db_lock:
                    delete_people_without_faces(self.conn)
                # Match unknown faces against known people (locked faces)
                self._reassess_faces_with_status()

            # --- Duplicate and face grouping (slower, less urgent) ---
            self._compute_duplicates_with_status()
            if self.config.face_detection_enabled:
                with self._db_lock:
                    compute_unknown_face_groups(self.conn, threshold=self.config.face_recognition_threshold)
                # Backfill semantic embeddings for faces that don't have them
                # (e.g., faces added before this feature existed)
                self.backfill_face_semantic_embeddings()
            emit_processing_complete(self.event_queue)

        # Callback when embedding completes - queue images for face detection
        def on_embedding_complete():
            # Notify frontend that new images are indexed and ready for display.
            # This fires well before processing_complete, which waits for face
            # detection, reassessment, and duplicate grouping (can take minutes).
            self.event_queue.emit('images_indexed', {})

            if not self._run_face_detection:
                logger.info('Skipping face detection (use --detect-faces or GUI Rescan)')
                return
            if self.config.face_detection_enabled:
                # Queue all images that don't have face detection run yet
                self._queue_images_for_face_detection()

        # Start ingestion thread with configured number of worker threads
        self._ingestion_thread = IngestionThread(
            conn=self.conn,
            ingestion_queue=self._ingestion_queue,
            embedding_queue=self._embedding_queue,
            stop_event=self._stop_event,
            db_lock=self._db_lock,
            checksum_cache=self._checksum_cache,
            checksum_cache_lock=self._checksum_cache_lock,
            generate_thumbnails=self._generate_thumbnails,
            pause_event=self._pause_event,
            num_threads=self.config.indexing_threads,
            max_image_dimension=self.config.max_image_dimension,
            nima_queue=self._nima_queue,
            import_names=self._import_names,
            import_names_lock=self._import_names_lock,
        )
        self._ingestion_thread.start()

        # Start embedding thread
        self._embedding_thread = EmbeddingThread(
            conn=self.conn,
            embedding_queue=self._embedding_queue,
            ingestion_thread=self._ingestion_thread,
            stop_event=self._stop_event,
            db_lock=self._db_lock,
            config=self.config,
            data_dir=self.db_path.parent,
            on_complete=on_embedding_complete,
        )
        self._embedding_thread.start()

        # Start face detection thread
        self._face_thread = FaceDetectionThread(
            conn=self.conn,
            face_queue=self._face_queue,
            embedding_thread=self._embedding_thread,
            ingestion_thread=self._ingestion_thread,
            stop_event=self._stop_event,
            db_lock=self._db_lock,
            config=self.config,
            thumbnail_dir=self.thumbnail_dir,
            on_complete=on_final_complete,
        )
        self._face_thread.start()

        # Start NIMA scoring thread (runs concurrently, not chained)
        self._nima_thread = NimaThread(
            conn=self.conn,
            nima_queue=self._nima_queue,
            ingestion_thread=self._ingestion_thread,
            stop_event=self._stop_event,
            db_lock=self._db_lock,
            config=self.config,
            data_dir=self.db_path.parent,
            thumbnail_dir=self.thumbnail_dir,
            event_queue=self.event_queue,
        )
        self._nima_thread.start()

        # Start trash worker thread (moves files asynchronously)
        if getattr(self, '_trash_enabled', False):
            self._trash_thread = TrashWorker(
                trash_queue=self._trash_queue,
                stop_event=self._stop_event,
                trash_dir=self.trash_dir,
                max_workers=self.config.trash_threads,
                progress=self._trash_progress,
                progress_lock=self._trash_progress_lock,
            )
            self._trash_thread.start()

        # Start import worker thread (copies files into catalogue)
        if getattr(self, '_import_enabled', False):
            self._import_thread = ImportWorker(
                import_queue=self._import_queue,
                stop_event=self._stop_event,
                catalogue_dir=self.catalogue_dir,
                max_workers=self.config.import_threads,
                progress=self._import_progress,
                progress_lock=self._import_progress_lock,
                checksum_cache=self._checksum_cache,
                checksum_cache_lock=self._checksum_cache_lock,
                image_extensions=self.config.image_extensions,
                import_names=self._import_names,
                import_names_lock=self._import_names_lock,
                on_complete=self._on_import_complete,
            )
            self._import_thread.start()

        logger.info('Background threads started')

    def stop_threads(self, timeout: float = 5.0) -> None:
        """Stop the background processing threads.

        Args:
            timeout: Maximum time to wait for threads to stop.
        """
        logger.info('Stopping background threads')
        self._stop_event.set()

        if self._ingestion_thread is not None:
            self._ingestion_thread.join(timeout=timeout)
            if self._ingestion_thread.is_alive():
                logger.warning('Ingestion thread did not stop in time')

        if self._embedding_thread is not None:
            self._embedding_thread.join(timeout=timeout)
            if self._embedding_thread.is_alive():
                logger.warning('Image embedding thread did not stop in time')

        if self._face_thread is not None:
            self._face_thread.join(timeout=timeout)
            if self._face_thread.is_alive():
                logger.warning('Face detection thread did not stop in time')

        if self._nima_thread is not None:
            self._nima_thread.join(timeout=timeout)
            if self._nima_thread.is_alive():
                logger.warning('NIMA scoring thread did not stop in time')

        if self._trash_thread is not None:
            self._trash_thread.join(timeout=timeout)
            if self._trash_thread.is_alive():
                logger.warning('Trash worker thread did not stop in time')

        if self._import_thread is not None:
            self._import_thread.join(timeout=timeout)
            if self._import_thread.is_alive():
                logger.warning('Import worker thread did not stop in time')

        logger.info('Background threads stopped')

    def close(self) -> None:
        """Close the database and stop all threads.

        Safe to call multiple times. After closing, the database
        cannot be used. Waits for any in-flight rotation operations
        to complete before closing.
        """
        if self._closed:
            return

        self._closed = True
        self.stop_threads()

        # Wait for any in-flight rotation operations to complete
        with self._rotations_lock:
            while self._active_rotations > 0:
                logger.info(f'Waiting for {self._active_rotations} rotation(s) to complete...')
                self._rotations_done.wait(timeout=5.0)

        with self._db_lock:
            if self.conn:
                self.conn.close()
                self.conn = None
                logger.info('Database connection closed')

    def __enter__(self) -> ImageDatabase:
        """Context manager entry - returns self."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - closes the database."""
        self.close()

    def __del__(self) -> None:
        """Destructor - ensure cleanup on garbage collection."""
        try:
            self.close()
        except Exception:
            pass  # Ignore errors during cleanup

    @property
    def is_closed(self) -> bool:
        """Check if the database has been closed."""
        return self._closed

    # =========================================================================
    # Public API - Folders
    # =========================================================================

    def get_folders(self) -> list[dict[str, Any]]:
        """Get all registered folders with image counts."""
        return get_folders(self.conn)

    def add_folder(self, path: str) -> dict[str, Any] | None:
        """Register a new folder and queue its images for processing.

        Returns:
            Folder info dict, or None if already registered.
        """
        with self._db_lock:
            result = add_folder(self.conn, path)
        if result is not None:
            # Enable full processing chain — adding a folder via the GUI
            # should trigger face detection and grouping just like Rescan
            self._run_face_detection = True
            self._run_face_grouping = True
            emit_folder_added(self.event_queue, result['path'])
            # Re-validate trash dir (new folder may conflict)
            self._validate_trash_dir()
            # Scan the folder for images in a background thread so the
            # HTTP response returns immediately.  Walking a NAS path over
            # SMB can take minutes for large collections; blocking the
            # request would leave the user staring at a frozen UI.
            scan_thread = threading.Thread(
                target=self._scan_and_queue_folder,
                args=(result['path'],),
                daemon=True,
                name='folder-scan',
            )
            scan_thread.start()
        return result

    def _scan_and_queue_folder(self, folder_path: str) -> None:
        """Scan a single folder for images and queue them for ingestion.

        Runs in a background thread after a folder is registered so that
        the API response is not blocked by potentially slow filesystem
        traversal (e.g. network shares over SMB).
        """
        try:
            count = 0
            for image_path in find_images_in_folder(
                folder_path,
                self.config.image_extensions,
            ):
                if self._stop_event.is_set():
                    logger.info(f'Folder scan interrupted by shutdown: {folder_path}')
                    return
                self._ingestion_queue.put(image_path)
                count += 1
            if count:
                logger.info(f'Queued {count} image(s) from {folder_path}')
            else:
                logger.info(f'No images found in {folder_path}')
        except Exception:
            logger.exception(f'Error scanning folder: {folder_path}')

    def remove_folder(self, path: str) -> bool:
        """Remove a folder and mark orphaned images as deleted."""
        # Pause ingestion while modifying
        self._pause_event.set()
        try:
            # Clear ingestion queue of paths from this folder
            self._clear_folder_from_queue(path)

            # Get image IDs that will be orphaned (for duplicate cleanup)
            orphaned_ids = self._get_orphaned_image_ids(path)

            with self._db_lock:
                result = remove_folder(self.conn, path)

            if result:
                # Clean up duplicate groups for orphaned images
                if orphaned_ids:
                    _count, affected_levels = self._duplicate_manager.invalidate_images(orphaned_ids)
                    for level in affected_levels:
                        self.event_queue.emit(
                            EVENT_GROUPS_CHANGED,
                            {'level': level, 'invalidate': True},
                        )
                # Re-sync directory groups (removes groups for the deleted folder)
                self._duplicate_manager.sync_directory_groups(self.conn, self._db_lock)
                emit_folder_removed(self.event_queue, path)
                # Re-validate trash dir (removed folder may resolve a conflict)
                self._validate_trash_dir()

            return result
        finally:
            self._pause_event.clear()

    def _get_orphaned_image_ids(self, folder_path: str) -> list[str]:
        """Get IDs of images that will be orphaned when a folder is removed.

        An image is orphaned if it's within the folder being removed and not
        within any other registered folder.
        """
        folder = canonicalise_path(folder_path)
        folder_str = str(folder)

        # Get all remaining folders (excluding the one being removed)
        cursor = self.conn.execute('SELECT path FROM folders WHERE path != ?', (folder_str,))
        remaining_folders = [row['path'] for row in cursor.fetchall()]

        # Find images in this folder that won't be covered by remaining folders
        # Use range queries instead of LIKE for index efficiency
        folder_upper = folder_path_upper_bound(folder_str)

        if remaining_folders:
            # Images that start with this folder but don't start with any remaining folder
            not_conditions = ' AND '.join(['NOT (path >= ? AND path < ?)'] * len(remaining_folders))
            # Build params: folder range, then each remaining folder's range
            params = [folder_str, folder_upper]
            for remaining in remaining_folders:
                params.extend([remaining, folder_path_upper_bound(remaining)])
            cursor = self.conn.execute(
                f'SELECT id FROM images WHERE path >= ? AND path < ? AND deleted = 0 AND {not_conditions}', params
            )
        else:
            # No other folders, all images in this folder will be orphaned
            cursor = self.conn.execute(
                'SELECT id FROM images WHERE path >= ? AND path < ? AND deleted = 0', (folder_str, folder_upper)
            )

        return [row['id'] for row in cursor.fetchall()]

    def _clear_folder_from_queue(self, folder_path: str) -> None:
        """Remove paths from ingestion queue that are within a folder."""
        folder = Path(folder_path)
        remaining: list[Path] = []

        # Drain queue
        while True:
            try:
                path = self._ingestion_queue.get_nowait()
                if not folder_contains_path(folder, path):
                    remaining.append(path)
                self._ingestion_queue.task_done()
            except queue.Empty:
                break

        # Re-add remaining paths
        for path in remaining:
            self._ingestion_queue.put(path)

    # =========================================================================
    # Public API - Images
    # =========================================================================

    def get_all_images(self, include_deleted: bool = False) -> list[dict[str, Any]]:
        """Get all images."""
        return get_all_images(self.conn, include_deleted)

    def get_all_images_lightweight(self) -> list[dict[str, Any]]:
        """Get all images with minimal fields for gallery grid."""
        return get_all_images_lightweight(self.conn)

    def get_images_for_thumbnail_generation(self) -> list[dict[str, Any]]:
        """Get images with fields needed for bulk thumbnail generation."""
        return get_images_for_thumbnail_generation(self.conn)

    def get_images_delta(self, since: str) -> dict[str, Any]:
        """Get image changes since a given timestamp."""
        return get_images_delta(self.conn, since)

    def get_current_epoch(self) -> str | None:
        """Get the current epoch (max updated_at timestamp)."""
        return get_current_epoch(self.conn)

    def get_image(self, image_id: str) -> dict[str, Any] | None:
        """Get a single image by ID."""
        return get_image(self.conn, image_id)

    def get_image_exif(self, image_id: str) -> dict[str, str] | None:
        """Get parsed EXIF metadata for a single image (lazy-loaded)."""
        return get_image_exif(self.conn, image_id)

    def search_image_metadata(self, criteria: dict[str, str]) -> list[str]:
        """Search for images matching EXIF metadata criteria."""
        return search_image_metadata(self.conn, criteria)

    def get_metadata_keys(self) -> list[str]:
        """Get all distinct metadata keys in the database."""
        return get_metadata_keys(self.conn)

    def get_metadata_values(self, key: str) -> list[str]:
        """Get all distinct values for a given metadata key."""
        return get_metadata_values(self.conn, key)

    def get_images_without_exif(self) -> list[dict[str, Any]]:
        """Get all non-deleted images missing EXIF data."""
        return get_images_without_exif(self.conn)

    def extract_exif_for_image(self, image_id: str) -> bool:
        """Extract and store EXIF data for a single image.

        Thread-safe: acquires _db_lock for the database writes.
        File I/O (EXIF reading) happens outside the lock.
        """
        # Read path under lock (quick DB read)
        with self._db_lock:
            cursor = self.conn.execute('SELECT path FROM images WHERE id = ? AND deleted = 0', (image_id,))
            row = cursor.fetchone()
        if not row:
            return False

        path = Path(row[0])
        if not path.exists():
            return False

        # Extract EXIF outside lock (file I/O)
        exif_data = extract_exif_data(path)
        exif_json = json.dumps(exif_data) if exif_data else '{}'

        # Write results under lock
        with self._db_lock:
            self.conn.execute('UPDATE images SET exif_data = ? WHERE id = ?', (exif_json, image_id))
            if exif_data:
                _upsert_image_metadata(self.conn, image_id, exif_data)
            self.conn.commit()

        return bool(exif_data)

    def get_checksum(self, image_id: str) -> str | None:
        """Get checksum for an image from RAM cache.

        This is the fast path for thumbnail lookups - no DB query needed.
        Thread-safe.
        """
        with self._checksum_cache_lock:
            return self._checksum_cache.get(image_id)

    def get_image_thumbnail_info(self, image_id: str) -> tuple[str, str] | None:
        """Get checksum and path for an image (for thumbnail lookup).

        Uses RAM cache for checksum, only queries DB for path if needed.
        Thread-safe.
        """
        with self._checksum_cache_lock:
            checksum = self._checksum_cache.get(image_id)
        if checksum is None:
            return None
        # Still need path from DB (could cache this too, but it's less critical)
        cursor = self.conn.execute('SELECT path FROM images WHERE id = ? AND deleted = 0', (image_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        return (checksum, row[0])

    def update_image(self, image_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update image metadata (description, rating).

        If description is updated to a non-empty value, computes and stores
        the description embedding for semantic search.
        """
        # Check if description is being updated
        if 'description' in data:
            description = data['description'].strip() if data['description'] else ''
            if description:
                # Compute embedding for the new description (outside lock - CPU intensive)
                try:
                    embedding = self._get_clip_model().encode_text(description)
                    data['description_embedding'] = embedding.astype(np.float32).tobytes()
                except Exception as e:
                    logger.warning(f'Failed to compute description embedding: {e}')
            else:
                # Clear description embedding if description is empty
                data['description_embedding'] = None

        with self._db_lock:
            return update_image(self.conn, image_id, data)

    def delete_image(self, image_id: str, from_disk: bool = False) -> bool:
        """Delete an image (soft delete or from disk)."""
        with self._db_lock:
            result = delete_image(self.conn, image_id, from_disk)
        # Remove from checksum cache
        with self._checksum_cache_lock:
            self._checksum_cache.pop(image_id, None)
        # Remove from duplicate group cache
        self._duplicate_manager.invalidate_image(image_id)
        return result

    # =========================================================================
    # TRASH DIRECTORY
    # =========================================================================

    def _init_trash_dir(self) -> None:
        """Initialise the trash directory from config.

        Resolves the trash path, creates it if needed, and validates it
        against indexed folders.  Sets ``self.trash_dir`` and
        ``self._trash_enabled``.  Called from :meth:`startup` after
        folders are verified.
        """
        # Resolve trash path: custom from config, or default <data-dir>/trash
        if self.config.trash_dir:
            self.trash_dir = Path(self.config.trash_dir)
        else:
            self.trash_dir = self.db_path.parent / 'trash'

        # Create directory
        try:
            self.trash_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f'Failed to create trash directory {self.trash_dir}: {e}')
            self._trash_enabled = False
            self._trashed_count = 0
            return

        # Count existing trashed files for the status display
        self._trashed_count = sum(1 for f in self.trash_dir.iterdir() if f.is_file())

        # Validate against indexed folders
        self._validate_trash_dir()

        # Crash recovery: re-enqueue items persisted from a previous shutdown
        self._recover_pending_trash()

    def _recover_pending_trash(self) -> None:
        """Re-enqueue trash items saved from a previous interrupted shutdown.

        Reads ``.pending_trash.json`` from the trash directory.  If found,
        queues the items for the TrashWorker and deletes the file.
        """
        pending_path = self.trash_dir / '.pending_trash.json'
        if not pending_path.exists():
            return

        try:
            data = json.loads(pending_path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                logger.warning(f'Invalid pending trash file format, ignoring: {pending_path}')
                pending_path.unlink(missing_ok=True)
                return

            count = 0
            for item in data:
                image_id = item.get('id', '')
                file_path = item.get('path', '')
                if image_id and file_path:
                    self._trash_queue.put((image_id, file_path))
                    count += 1

            pending_path.unlink(missing_ok=True)
            if count:
                logger.info(f'Re-enqueued {count} pending trash items from previous shutdown')
        except Exception as e:
            logger.error(f'Failed to load pending trash items: {e}')

    def _validate_trash_dir(self) -> None:
        """Validate trash dir does not overlap indexed folders.

        Safe to call any time — updates ``self._trash_enabled``.
        """
        try:
            folders = [f['path'] for f in get_folders(self.conn)]
            validate_trash_dir(self.trash_dir, folders)
            self._trash_enabled = True
            logger.info(f'        Trash directory: {self.trash_dir}')
        except ValueError as e:
            logger.warning(f'Trash disabled: {e}')
            self._trash_enabled = False

    def is_trash_enabled(self) -> bool:
        """Check whether the trash directory is configured and valid.

        Returns:
            True if images can be moved to trash, False if trash is
            disabled due to misconfiguration or overlap with indexed folders.
        """
        return getattr(self, '_trash_enabled', False)

    def get_trash_progress(self) -> dict | None:
        """Get progress of the current trash operation, if any.

        Returns:
            Dict with ``total``, ``done``, and ``started_at`` keys while
            a trash operation is running, or None when idle.
        """
        with self._trash_progress_lock:
            return dict(self._trash_progress) if self._trash_progress else None

    # ------------------------------------------------------------------
    # Import / Catalogue directory
    # ------------------------------------------------------------------

    def _init_import_dir(self) -> None:
        """Initialise the catalogue/import directory from config.

        If ``catalogue_dir`` is set, creates it if needed and auto-registers
        it as a watched folder. Also recovers any pending imports from a
        previous interrupted shutdown.
        """
        self._import_enabled = False

        # Resolve catalogue path: custom from config, or default <data-dir>/catalogue
        if self.config.catalogue_dir:
            self.catalogue_dir = Path(self.config.catalogue_dir)
        else:
            self.catalogue_dir = self.db_path.parent / 'catalogue'

        # Create directory if it does not exist
        already_exists = self.catalogue_dir.is_dir()
        try:
            self.catalogue_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f'Import disabled: failed to create catalogue directory {self.catalogue_dir}: {e}')
            return

        self._import_enabled = True
        if already_exists:
            logger.info(f'        Catalogue directory: {self.catalogue_dir}')
        else:
            logger.info(f'        Catalogue directory (created): {self.catalogue_dir}')

        # Auto-register catalogue dir as a watched folder if not already
        existing = {f['path'] for f in get_folders(self.conn)}
        canon = str(canonicalise_path(self.catalogue_dir))
        if canon not in existing:
            logger.info('        Auto-registered catalogue as watched folder')
            with self._db_lock:
                add_folder(self.conn, canon)

        # Crash recovery: re-enqueue items from previous shutdown
        self._recover_pending_import()

    def _recover_pending_import(self) -> None:
        """Re-enqueue import items saved from a previous interrupted shutdown.

        Reads ``.pending_import.json`` from the catalogue directory.  If found,
        queues the items for the ImportWorker and deletes the file.
        """
        pending_path = self.catalogue_dir / '.pending_import.json'
        if not pending_path.exists():
            return

        try:
            data = json.loads(pending_path.read_text(encoding='utf-8'))
            if not isinstance(data, list):
                logger.warning(f'Invalid pending import file format, ignoring: {pending_path}')
                pending_path.unlink(missing_ok=True)
                return

            count = 0
            for item in data:
                if isinstance(item, str) and item:
                    self._import_queue.put(item)
                    count += 1

            pending_path.unlink(missing_ok=True)
            if count:
                self._import_progress = {
                    'total': count,
                    'done': 0,
                    'skipped': 0,
                    'started_at': time.time(),
                }
                logger.info(f'Re-enqueued {count} pending import items from previous shutdown')
        except Exception as e:
            logger.error(f'Failed to load pending import items: {e}')

    def is_import_enabled(self) -> bool:
        """Check whether the import/catalogue directory is configured.

        Returns:
            True if images can be imported into the catalogue, False otherwise.
        """
        return getattr(self, '_import_enabled', False)

    def get_import_progress(self) -> dict | None:
        """Get progress of the current import operation, if any.

        Returns:
            Dict with ``total``, ``done``, ``skipped``, and ``started_at``
            keys while an import is running, or None when idle.
        """
        with self._import_progress_lock:
            return dict(self._import_progress) if self._import_progress else None

    def enqueue_import(self, paths: list[str]) -> int:
        """Enqueue files/directories for import into the catalogue directory.

        Walks directories recursively, filtering by supported image extensions.
        Sets up progress tracking and queues each file for the ImportWorker.

        Args:
            paths: List of file or directory paths to import.

        Returns:
            Number of files queued for import.

        Raises:
            ValueError: If import is disabled (catalogue_dir not configured).
        """
        if not self._import_enabled:
            raise ValueError('Import is disabled. Set catalogue_dir in config to enable.')

        # Collect all image files from the provided paths
        files: list[str] = []
        for p in paths:
            path = Path(p)
            if path.is_file():
                if path.suffix.lower() in self.config.image_extensions:
                    files.append(str(path))
            elif path.is_dir():
                for child in find_images_in_folder(str(path), self.config.image_extensions):
                    files.append(str(child))

        if not files:
            return 0

        # Set up progress tracking
        with self._import_progress_lock:
            if self._import_progress is not None:
                # Append to existing import operation
                self._import_progress['total'] += len(files)
            else:
                self._import_progress = {
                    'total': len(files),
                    'done': 0,
                    'skipped': 0,
                    'started_at': time.time(),
                }
            # Update the worker's progress reference
            if self._import_thread is not None:
                self._import_thread._progress = self._import_progress

        # Queue each file for the ImportWorker
        for f in files:
            self._import_queue.put(f)

        logger.info(f'Queued {len(files)} file(s) for import into catalogue')
        return len(files)

    def _on_import_complete(self, progress: dict) -> None:
        """Called after all import items in a batch have been processed.

        Invoked by the :class:`ImportWorker`'s ``_check_completion()``
        callback when ``done >= total``.  Clears the shared progress state,
        emits an ``import_complete`` event, and triggers a rescan so the
        newly copied files get ingested by the existing pipeline.

        Args:
            progress: Final progress snapshot (``total``, ``done``, ``skipped``).
                Passed by the caller since the worker's ``_progress`` reference
                has already been cleared to fire exactly once.
        """
        # Clear ImageDatabase's _import_progress so get_processing_status()
        # stops reporting stale progress.  The ImportWorker already cleared
        # its own _progress reference in _check_completion().
        with self._import_progress_lock:
            self._import_progress = None

        imported = progress.get('done', 0) - progress.get('skipped', 0)
        skipped = progress.get('skipped', 0)

        logger.info(f'Import batch complete: {imported} imported, {skipped} skipped')

        # Emit event for frontend (include catalogue_dir so clients can
        # optimistically bump the per-folder count before indexing completes)
        self.event_queue.emit(
            EVENT_IMPORT_COMPLETE,
            {
                'imported': imported,
                'skipped': skipped,
                'catalogue_dir': str(self.catalogue_dir),
            },
        )

        # Trigger a rescan so the newly imported files get picked up by
        # the existing ingestion pipeline. Enable full processing chain.
        self._run_face_detection = True
        self._run_face_grouping = True
        scan_thread = threading.Thread(
            target=self._scan_and_queue_folder,
            args=(str(self.catalogue_dir),),
            daemon=True,
            name='import-rescan',
        )
        scan_thread.start()

    @staticmethod
    def _find_closest_face(
        old_preferred_id: str,
        removed_embeddings: dict[str, bytes],
        remaining_faces: list[sqlite3.Row],
    ) -> str:
        """Find the remaining face most similar to the old preferred face.

        When a person's preferred face is trashed, this picks the best
        replacement by cosine similarity to the old embedding. Falls back
        to the first remaining face if the old embedding is unavailable.

        Args:
            old_preferred_id: Face ID of the removed preferred face.
            removed_embeddings: Map of removed face_id -> raw embedding bytes.
            remaining_faces: Rows with 'id' and 'embedding' columns.

        Returns:
            Face ID of the best replacement.
        """
        old_emb_bytes = removed_embeddings.get(old_preferred_id)
        if not old_emb_bytes or len(remaining_faces) == 1:
            return remaining_faces[0]['id']

        old_emb = np.frombuffer(old_emb_bytes, dtype=np.float32)
        best_id = remaining_faces[0]['id']
        best_sim = -1.0
        for row in remaining_faces:
            if not row['embedding']:
                continue
            emb = np.frombuffer(row['embedding'], dtype=np.float32)
            sim = float(np.dot(old_emb, emb))
            if sim > best_sim:
                best_sim = sim
                best_id = row['id']
        return best_id

    def enqueue_trash(self, image_ids: list[str]) -> dict[str, Any]:
        """Enqueue images for background trashing.

        Immediately: looks up paths, soft-deletes in DB, invalidates caches,
        and emits ``EVENT_IMAGES_CHANGED``.  File moves happen asynchronously
        via the :class:`TrashWorker` thread.

        Args:
            image_ids: List of image UUIDs to trash.

        Returns:
            Dict with:
                - enqueued: List of image IDs accepted for trashing
                - errors: Dict mapping image_id → error message

        Raises:
            ValueError: If trash is disabled.
        """
        if self._closed:
            logger.warning('enqueue_trash: Database is closed')
            return {'enqueued': [], 'errors': {id: 'Database closed' for id in image_ids}}

        if not self._trash_enabled:
            raise ValueError('Trash directory is disabled. Check that it does not overlap an indexed folder.')

        # Look up file paths for all images (single batch query)
        paths: dict[str, str] = {}
        with self._db_lock:
            placeholders = ','.join('?' for _ in image_ids)
            cursor = self.conn.execute(f'SELECT id, path FROM images WHERE id IN ({placeholders})', image_ids)
            for row in cursor.fetchall():
                paths[row['id']] = row['path']

        enqueued: list[str] = []
        errors: dict[str, str] = {}

        for image_id in image_ids:
            if image_id in paths:
                enqueued.append(image_id)
            else:
                errors[image_id] = 'Image not found'

        if not enqueued:
            return {'enqueued': [], 'errors': errors}

        # Soft-delete in DB immediately (images vanish from queries at once)
        # Also clean up associated face records and person references.
        now = datetime.now().isoformat()
        removed_face_ids = []
        affected_person_ids = set()
        with self._db_lock:
            placeholders = ','.join('?' for _ in enqueued)

            # Collect faces belonging to these images BEFORE soft-delete,
            # so we can clean up person references and face thumbnails.
            # Also grab embeddings for preferred faces that are being removed,
            # so we can pick the most visually similar replacement.
            cursor = self.conn.execute(
                f'SELECT id, person_id, embedding FROM faces WHERE image_id IN ({placeholders})',
                enqueued,
            )
            removed_face_embeddings = {}  # face_id -> embedding bytes
            for row in cursor.fetchall():
                removed_face_ids.append(row['id'])
                if row['person_id']:
                    affected_person_ids.add(row['person_id'])
                if row['embedding']:
                    removed_face_embeddings[row['id']] = row['embedding']

            # Soft-delete the images
            self.conn.execute(
                f'UPDATE images SET deleted = 1, updated_at = ? WHERE id IN ({placeholders})',
                [now] + enqueued,
            )

            # Hard-delete orphaned face records (CASCADE won't fire on UPDATE)
            if removed_face_ids:
                face_placeholders = ','.join('?' for _ in removed_face_ids)
                self.conn.execute(
                    f'DELETE FROM faces WHERE id IN ({face_placeholders})',
                    removed_face_ids,
                )

            # Fix preferred_face_id for affected people and remove empty people
            removed_set = set(removed_face_ids)
            for person_id in affected_person_ids:
                remaining = self.conn.execute(
                    'SELECT id, embedding FROM faces WHERE person_id = ? AND suppressed = 0',
                    (person_id,),
                ).fetchall()
                if not remaining:
                    # Person has no more faces - delete them
                    self.conn.execute('DELETE FROM people WHERE id = ?', (person_id,))
                else:
                    # Check if preferred face was among the removed
                    preferred = self.conn.execute(
                        'SELECT preferred_face_id FROM people WHERE id = ?',
                        (person_id,),
                    ).fetchone()
                    old_preferred_id = preferred['preferred_face_id'] if preferred else None
                    if old_preferred_id and old_preferred_id in removed_set:
                        new_preferred_id = self._find_closest_face(
                            old_preferred_id,
                            removed_face_embeddings,
                            remaining,
                        )
                        self.conn.execute(
                            "UPDATE people SET preferred_face_id = ?, updated_at = datetime('now') WHERE id = ?",
                            (new_preferred_id, person_id),
                        )

            # Collect person event data while we still hold the lock
            deleted_person_ids = []
            updated_people = []
            for pid in affected_person_ids:
                row = self.conn.execute('SELECT * FROM people WHERE id = ?', (pid,)).fetchone()
                if row:
                    updated_people.append(dict(row))
                else:
                    deleted_person_ids.append(pid)

            self.conn.commit()

        # Delete orphaned face thumbnail files (outside DB lock)
        for face_id in removed_face_ids:
            delete_face_thumbnail(face_id, self.thumbnail_dir)

        # Invalidate the known-embedding cache since faces were removed
        if affected_person_ids:
            from faces import invalidate_embedding_cache

            invalidate_embedding_cache()

        # Update trashed count and invalidate caches
        self._trashed_count += len(enqueued)
        with self._checksum_cache_lock:
            for image_id in enqueued:
                self._checksum_cache.pop(image_id, None)
        _affected_count, affected_levels = self._duplicate_manager.invalidate_images(enqueued)

        # Set up or accumulate progress tracking
        with self._trash_progress_lock:
            if self._trash_progress is None:
                self._trash_progress = {
                    'total': len(enqueued),
                    'done': 0,
                    'started_at': now,
                }
            else:
                self._trash_progress['total'] += len(enqueued)

            # Keep the TrashWorker's reference current
            if self._trash_thread is not None:
                self._trash_thread._progress = self._trash_progress

        # Enqueue file moves for background processing
        for image_id in enqueued:
            self._trash_queue.put((image_id, paths[image_id]))

        # Emit event for other clients (and the current client's image cache)
        self.event_queue.emit(EVENT_IMAGES_CHANGED, {'removed_ids': enqueued})

        # Notify other clients that faces/people changed due to trashed images
        if removed_face_ids:
            self.event_queue.emit(
                EVENT_FACES_CHANGED,
                {'removed': removed_face_ids},
            )
        if deleted_person_ids or updated_people:
            event_data = {}
            if deleted_person_ids:
                event_data['removed'] = deleted_person_ids
            if updated_people:
                event_data['upserted'] = updated_people
            self.event_queue.emit(EVENT_PEOPLE_CHANGED, event_data)

        # Notify other clients that groups were affected at each level where
        # images were removed (counts may have changed, groups may be dissolved)
        for level in affected_levels:
            self.event_queue.emit(
                EVENT_GROUPS_CHANGED,
                {'level': level, 'invalidate': True},
            )

        logger.info(f'Enqueued {len(enqueued)}/{len(image_ids)} images for trashing ({len(errors)} errors)')
        return {'enqueued': enqueued, 'errors': errors}

    def rotate_images(
        self,
        image_ids: list[str],
        degrees: float,
    ) -> dict[str, Any]:
        """Rotate multiple image files in parallel.

        Uses a thread pool for parallel processing, controlled by the
        indexing_threads config option.

        Args:
            image_ids: List of image UUIDs to rotate.
            degrees: Rotation angle in degrees (clockwise positive).
                     Common values: 90 (right), 180, 270 (left).

        Returns:
            Dict with:
                - results: Dict mapping image_id to success boolean
                - rotated: List of successfully rotated image IDs
        """
        # Check if database is closing
        if self._closed:
            logger.warning('rotate_images: Database is closed')
            return {'results': {id: False for id in image_ids}, 'rotated': []}

        # Track this rotation operation for graceful shutdown
        with self._rotations_lock:
            self._active_rotations += 1

        try:
            results = {}
            rotated = []
            old_checksums = []  # For thumbnail RAM cache invalidation

            # Use thread pool for parallel rotation
            max_workers = self.config.indexing_threads

            def rotate_one(image_id: str) -> tuple[str, bool, str | None]:
                success, old_checksum = self._rotate_single_image(image_id, degrees)
                return (image_id, success, old_checksum)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(rotate_one, img_id) for img_id in image_ids]
                for future in futures:
                    try:
                        image_id, success, old_checksum = future.result()
                        results[image_id] = success
                        if success:
                            rotated.append(image_id)
                            if old_checksum:
                                old_checksums.append(old_checksum)
                    except Exception as e:
                        logger.error(f'rotate_images: Thread error: {e}')

            # Emit event for frontend to refresh affected images
            if rotated:
                self.event_queue.emit(EVENT_IMAGES_MODIFIED, {'image_ids': rotated})

            return {'results': results, 'rotated': rotated, 'old_checksums': old_checksums}

        finally:
            # Signal that this rotation operation is complete
            with self._rotations_lock:
                self._active_rotations -= 1
                self._rotations_done.notify_all()

    def _get_image_lock(self, image_id: str) -> threading.Lock:
        """Get or create a lock for a specific image.

        Used to prevent concurrent operations on the same image file.

        Args:
            image_id: UUID of the image.

        Returns:
            Lock for the specified image.
        """
        with self._image_locks_lock:
            if image_id not in self._image_locks:
                self._image_locks[image_id] = threading.Lock()
            return self._image_locks[image_id]

    def _rotate_single_image(self, image_id: str, degrees: float) -> tuple[bool, str | None]:
        """Rotate a single image file and update its metadata.

        Performs lossless rotation for JPEG files when possible.
        Updates the database with new checksum, size, and mtime.
        Deletes old cached thumbnails.

        Uses a per-image lock to prevent concurrent rotations of the same image.

        Args:
            image_id: UUID of the image to rotate.
            degrees: Rotation angle in degrees (clockwise positive).

        Returns:
            Tuple of (success, old_checksum). old_checksum is returned on success
            so the caller can invalidate thumbnail RAM cache.
        """
        # Get per-image lock to prevent concurrent rotations of the same image
        image_lock = self._get_image_lock(image_id)

        with image_lock:
            # Get current image info (inside lock to ensure consistent read)
            image = get_image(self.conn, image_id)
            if image is None:
                logger.warning(f'rotate_image: Image not found: {image_id}')
                return (False, None)

            path = Path(image['path'])
            if not path.exists():
                logger.error(f'rotate_image: File not found: {path}')
                return (False, None)

            old_checksum = image.get('checksum')

            # Rotate the image file
            if not rotate_image_file(path, degrees):
                logger.error(f'rotate_image: Rotation failed for: {path}')
                return (False, None)

            # Delete old thumbnails (based on old checksum)
            if old_checksum:
                deleted_count = delete_thumbnails_for_checksum(old_checksum, self.thumbnail_dir)
                logger.debug(f'Deleted {deleted_count} old thumbnails for checksum {old_checksum[:8]}...')

            # Compute new metadata
            try:
                new_checksum = compute_checksum(path)
                stat = path.stat()
                new_size = stat.st_size
                new_mtime = stat.st_mtime

                # Get new dimensions
                with Image.open(path) as img:
                    new_width, new_height = img.size
            except Exception as e:
                logger.error(f'rotate_image: Failed to compute new metadata: {e}')
                return (False, None)

            # Update database (with db lock for thread safety)
            try:
                with self._db_lock:
                    self.conn.execute(
                        """UPDATE images SET
                            checksum = ?,
                            size = ?,
                            width = ?,
                            height = ?,
                            mtime = ?,
                            updated_at = ?
                        WHERE id = ?""",
                        (
                            new_checksum,
                            new_size,
                            new_width,
                            new_height,
                            new_mtime,
                            datetime.now().isoformat(),
                            image_id,
                        ),
                    )
                    self.conn.commit()

                # Update checksum cache with new checksum
                with self._checksum_cache_lock:
                    self._checksum_cache[image_id] = new_checksum
            except Exception as e:
                logger.error(f'rotate_image: Failed to update database: {e}')
                return (False, None)

            # Rotate face bounding boxes and regenerate face thumbnails
            try:
                with self._db_lock:
                    # Get faces before rotating their coordinates
                    faces = get_faces_for_image(self.conn, image_id, include_suppressed=True)
                    logger.debug(f'rotate_image: Found {len(faces)} faces for {path.name}')

                    # Rotate the bounding box coordinates in the database
                    rotated_count = rotate_faces_for_image(self.conn, image_id, degrees)
                    logger.debug(f'rotate_image: Rotated {rotated_count} face bounding boxes')

                    if rotated_count > 0:
                        # Delete old face thumbnails and regenerate them
                        for face in faces:
                            face_id = face['id']
                            old_bbox = (face['box_x'], face['box_y'], face['box_w'], face['box_h'])

                            # Delete old thumbnail
                            thumb_path = get_face_thumbnail_path(face_id, self.thumbnail_dir)
                            thumb_existed = thumb_path.exists()
                            deleted = delete_face_thumbnail(face_id, self.thumbnail_dir)
                            logger.debug(
                                f'rotate_image: Face {face_id[:8]}... old_bbox={old_bbox}, '
                                f'thumb_existed={thumb_existed}, deleted={deleted}'
                            )

                            # Get updated face coordinates (after rotation)
                            updated_face = get_face(self.conn, face_id)
                            if updated_face:
                                new_bbox = (
                                    updated_face['box_x'],
                                    updated_face['box_y'],
                                    updated_face['box_w'],
                                    updated_face['box_h'],
                                )
                                logger.debug(f'rotate_image: Face {face_id[:8]}... new_bbox={new_bbox}')

                                # Regenerate face thumbnail with new coordinates
                                try:
                                    success = generate_face_thumbnail(
                                        path,
                                        thumb_path,
                                        box_x=updated_face['box_x'],
                                        box_y=updated_face['box_y'],
                                        box_w=updated_face['box_w'],
                                        box_h=updated_face['box_h'],
                                        size=200,
                                        quality=self.config.thumbnail_quality,
                                    )
                                    thumb_exists_now = thumb_path.exists()
                                    logger.debug(
                                        f'rotate_image: Face {face_id[:8]}... '
                                        f'regen_success={success}, '
                                        f'thumb_exists_now={thumb_exists_now}, '
                                        f'thumb_path={thumb_path}'
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f'rotate_image: Failed to regenerate face thumbnail for {face_id}: {e}'
                                    )
            except Exception as e:
                logger.warning(f'rotate_image: Failed to rotate faces: {e}')
                # Non-fatal - image was still rotated successfully

            logger.info(f'Rotated image {degrees}°: {path.name}')
            return (True, old_checksum)

    def search_images(
        self,
        query: str,
        threshold: float = 0.2,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search for images using semantic similarity.

        Encodes the query text using OpenCLIP and finds images with similar
        embeddings (both image content and descriptions). Supports negative
        terms prefixed with '-' (e.g., "beach -face" finds beaches without faces).

        Args:
            query: Text query to search for.
            threshold: Minimum similarity score (0.0 to 1.0). Defaults to 0.2.
            limit: Maximum number of results. Defaults to 100.

        Returns:
            List of matching images with 'score' field, sorted by similarity.
        """
        # Encode the query text (with support for negative terms)
        query_embedding = self._get_clip_model().encode_semantic_query(query)

        # Perform semantic search
        return semantic_search(self.conn, query_embedding, threshold, limit)

    def get_semantic_scores_for_images(
        self,
        query: str,
        image_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Get semantic similarity scores for specific images.

        Used for sorting duplicate groups by similarity to a query.
        Supports negative terms prefixed with '-'.

        Args:
            query: Text query to compare against.
            image_ids: List of image IDs to score.

        Returns:
            List of {image_id, score} dicts sorted by descending score.
        """
        if not image_ids:
            return []

        # Encode the query text (with support for negative terms)
        # encode_semantic_query returns a normalized embedding
        query_embedding = self._get_clip_model().encode_semantic_query(query)

        # Get embeddings for the specified images
        placeholders = ','.join('?' * len(image_ids))
        cursor = self.conn.execute(
            f'SELECT id, embedding FROM images WHERE id IN ({placeholders}) AND embedding IS NOT NULL', image_ids
        )

        results = []
        for row in cursor.fetchall():
            embedding = embedding_to_numpy(row['embedding'])
            embedding = embedding / (np.linalg.norm(embedding) or 1)
            score = float(np.dot(query_embedding, embedding))
            results.append({'image_id': row['id'], 'score': score})

        # Sort by score descending
        results.sort(key=lambda x: x['score'], reverse=True)
        return results

    def get_similar_images(self, reference_image_id: str) -> list[dict[str, Any]] | None:
        """Get all images sorted by visual similarity to a reference image.

        Args:
            reference_image_id: ID of the reference image.

        Returns:
            List of images with 'similarity' field, sorted by descending
            similarity. Returns None if reference image not found or has
            no embedding.
        """
        # Get the reference image's embedding
        cursor = self.conn.execute('SELECT id, embedding, deleted FROM images WHERE id = ?', (reference_image_id,))
        row = cursor.fetchone()

        # Debug logging
        if row is None:
            logger.warning(f'get_similar_images: No row found for id={reference_image_id}')
            return None

        logger.info(
            f'get_similar_images: id={reference_image_id}, '
            f'deleted={row["deleted"]}, has_embedding={row["embedding"] is not None}'
        )

        if row['deleted']:
            logger.warning('get_similar_images: Image is deleted')
            return None

        if row['embedding'] is None:
            logger.warning('get_similar_images: Embedding is None')
            return None

        reference_embedding = np.frombuffer(row['embedding'], dtype=np.float32)

        # Get all images sorted by similarity
        return get_images_by_similarity(self.conn, reference_embedding)

    def _get_clip_model(self) -> OpenCLIPModel:
        """Get or create the OpenCLIP model for search operations."""
        if self._embedding_thread is not None:
            return self._embedding_thread.clip_model
        # Fallback: create a new model if embedding thread not running
        if not hasattr(self, '_clip_model_fallback'):
            self._clip_model_fallback = OpenCLIPModel(
                model_name=self.config.openclip_model,
                pretrained=self.config.openclip_pretrained,
                max_dimension=self.config.max_image_dimension,
            )
        return self._clip_model_fallback

    # =========================================================================
    # Public API - Thumbnails
    # =========================================================================

    def _generate_thumbnails(self, source_path: Path, checksum: str) -> bool:
        """Generate and cache thumbnails for an image at standard sizes.

        Called during image ingestion to pre-generate thumbnails at both
        200px and 400px sizes. The frontend uses CSS to resize whichever
        is closest to the current display size.

        Skips generation for sizes that already exist in cache.

        Args:
            source_path: Path to the source image file.
            checksum: SHA256 checksum of the image (used as cache key).

        Returns:
            True if all thumbnails were generated or already exist.
        """
        success = True
        for size in (200, 400):
            cache_path = get_thumbnail_cache_path(checksum, size=size, thumbnail_dir=self.thumbnail_dir)
            if cache_path.exists():
                continue
            if not generate_thumbnail(
                source_path,
                cache_path,
                size=size,
                quality=self.config.thumbnail_quality,
                max_source_dimension=self.config.max_image_dimension,
            ):
                success = False
        return success

    # =========================================================================
    # Public API - Duplicates
    # =========================================================================

    def get_duplicate_groups_lightweight(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups with minimal data for efficient display."""
        return self._duplicate_manager.get_groups_lightweight(level)

    def get_duplicate_epoch(self) -> str:
        """Get the current epoch timestamp for duplicate groups."""
        return self._duplicate_manager.get_epoch()

    # =========================================================================
    # Public API - Custom Groups (Albums)
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

        Serialised with _db_lock to prevent concurrent group mutations
        from corrupting the DuplicateManager's in-memory cache.

        Args:
            group_hash: Frontend-generated UUID for the group.
            name: Display name for the group.
            image_ids: Initial list of image IDs to include (ignored for smart groups).
            filter_json: JSON string of filter criteria for smart groups.
            preview_image_id: Representative image for smart group thumbnails.
        """
        with self._db_lock:
            self._duplicate_manager.create_custom_group(group_hash, name, image_ids, filter_json, preview_image_id)

    def update_custom_group_filter(
        self, group_hash: str, filter_json: str, preview_image_id: str | None = None
    ) -> None:
        """Update the filter criteria (and optionally preview) of a smart group.

        Args:
            group_hash: The group identifier.
            filter_json: New JSON string of filter criteria.
            preview_image_id: New representative image ID (or None to clear).
        """
        with self._db_lock:
            self._duplicate_manager.update_custom_group_filter(group_hash, filter_json, preview_image_id)

    def update_smart_group_preview(self, group_hash: str, preview_image_id: str | None) -> None:
        """Update only the preview thumbnail of a smart group.

        Args:
            group_hash: The group identifier.
            preview_image_id: Image ID for the thumbnail, or None to clear.
        """
        with self._db_lock:
            self._duplicate_manager.update_smart_group_preview(group_hash, preview_image_id)

    def rename_custom_group(self, group_hash: str, name: str) -> None:
        """Rename a custom group.

        Args:
            group_hash: The group identifier.
            name: New display name.
        """
        with self._db_lock:
            self._duplicate_manager.rename_custom_group(group_hash, name)

    def delete_custom_group(self, group_hash: str) -> None:
        """Delete a custom group and its image associations.

        Args:
            group_hash: The group identifier.
        """
        with self._db_lock:
            self._duplicate_manager.delete_custom_group(group_hash)

    def add_images_to_custom_group(self, group_hash: str, image_ids: list[str]) -> None:
        """Add images to an existing custom group.

        Args:
            group_hash: The group identifier.
            image_ids: Image IDs to add.
        """
        with self._db_lock:
            self._duplicate_manager.add_images_to_custom_group(group_hash, image_ids)

    def remove_images_from_custom_group(self, group_hash: str, image_ids: list[str]) -> None:
        """Remove images from a custom group (group persists even if empty).

        Args:
            group_hash: The group identifier.
            image_ids: Image IDs to remove.
        """
        with self._db_lock:
            self._duplicate_manager.remove_images_from_custom_group(group_hash, image_ids)

    # =========================================================================
    # Public API - Stats and Status
    # =========================================================================

    def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        cursor = self.conn.execute('SELECT COUNT(*) as count FROM images WHERE deleted = 0')
        total_images = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM folders')
        total_folders = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM people')
        total_people = cursor.fetchone()['count']

        cursor = self.conn.execute(
            """SELECT COUNT(*) as count FROM faces f
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0 AND i.deleted = 0"""
        )
        total_faces = cursor.fetchone()['count']

        return {
            'totalImages': total_images,
            'totalFolders': total_folders,
            'totalPeople': total_people,
            'totalFaces': total_faces,
            'totalTrashed': self._trashed_count,
        }

    def get_processing_status(self) -> dict[str, Any]:
        """Get current processing status.

        Returns:
            Dict with status, queue counts, and Phase 4 processing statuses.
        """
        indexing_count = self._ingestion_queue.qsize()
        embedding_count = self._embedding_queue.qsize()
        face_count = self._face_queue.qsize()
        nima_count = self._nima_queue.qsize() if self._nima_queue else 0
        trash_count = self._trash_queue.qsize()
        import_count = self._import_queue.qsize()

        # Auto-clear trash progress when queue has drained
        if trash_count == 0:
            with self._trash_progress_lock:
                if self._trash_progress is not None:
                    self._trash_progress = None
                    if self._trash_thread is not None:
                        self._trash_thread._progress = None

        # Import progress is NOT auto-cleared here — the ImportWorker's
        # _check_completion() callback fires _on_import_complete() which
        # clears _import_progress when all files are done.  This avoids
        # double-fire (polling + worker both detecting completion).
        # Snapshot the progress under lock for the response.
        with self._import_progress_lock:
            import_progress = dict(self._import_progress) if self._import_progress else None

        # Get Phase 4 statuses
        duplicate_status = self._duplicate_manager.get_status()
        face_grouping_status = get_group_computation_status()
        with self._phase4_status_lock:
            face_embedding_status = self._face_embedding_status.copy()
            face_reassess_status = self._face_reassess_status.copy() if self._face_reassess_status else None

        # Check if any Phase 4 process is active
        duplicates_computing = any(s == 'computing' for s in duplicate_status.values())
        face_grouping_computing = face_grouping_status.get('status') == 'computing'
        face_embedding_computing = face_embedding_status.get('status') == 'computing'
        face_reassess_computing = face_reassess_status is not None and face_reassess_status.get('status') == 'computing'

        # Determine overall status (NIMA + trash + import queues also contribute to 'updating').
        # Import progress is checked in addition to import_count because
        # the ImportWorker dequeues items before copying — qsize() can be 0
        # while files are still being processed.
        queues_empty = (
            indexing_count == 0
            and embedding_count == 0
            and face_count == 0
            and nima_count == 0
            and trash_count == 0
            and import_count == 0
            and import_progress is None
        )
        phase4_idle = not (
            duplicates_computing or face_grouping_computing or face_embedding_computing or face_reassess_computing
        )
        status = 'up_to_date' if (queues_empty and phase4_idle) else 'updating'

        # Get counts for live updates during processing
        cursor = self.conn.execute('SELECT COUNT(*) as count FROM images WHERE deleted = 0')
        total_images = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM people')
        total_people = cursor.fetchone()['count']

        cursor = self.conn.execute(
            """SELECT COUNT(*) as count FROM faces f
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0 AND i.deleted = 0"""
        )
        total_faces = cursor.fetchone()['count']

        # Build response - only include Phase 4 statuses if they're active
        result = {
            'status': status,
            'indexing_queue': indexing_count,
            'embedding_queue': embedding_count,
            'face_queue': face_count,
            'nima_queue': nima_count,
            'total_images': total_images,
            'total_people': total_people,
            'total_faces': total_faces,
            'trash_queue': trash_count,
            'trashed_count': self._trashed_count,
            'import_queue': import_count,
            'face_detection_enabled': self.config.face_detection_enabled,
            'nima_enabled': self.config.nima_enabled,
        }

        # Include import progress if active (total, done, skipped)
        if import_progress is not None:
            result['import_progress'] = import_progress

        # Include duplicate status if computing
        if duplicates_computing:
            # Find which level is currently computing
            computing_level = next((level for level, s in duplicate_status.items() if s == 'computing'), None)
            result['duplicates'] = {
                'status': 'computing',
                'level': computing_level,
            }

        # Include face grouping status if computing
        if face_grouping_computing:
            result['face_grouping'] = {'status': 'computing'}

        # Include face embedding status if computing
        if face_embedding_computing:
            result['face_embeddings'] = face_embedding_status

        # Include face reassessment status if computing
        if face_reassess_computing:
            result['face_reassess'] = face_reassess_status

        return result

    def get_duplicate_status(self) -> dict[int, str]:
        """Get the computation status for each duplicate level.

        Returns:
            Dict mapping level (0-3) to status string:
            - 'pending': Not yet computed
            - 'computing': Currently being computed
            - 'done': Computation finished
        """
        return self._duplicate_manager.get_status()

    def _compute_duplicates_with_status(self) -> None:
        """Compute all duplicate groups while tracking status per level.

        Delegates to DuplicateManager.compute_all() which handles:
        - Incremental vs full computation based on dirty image count
        - Status tracking per level
        - Epoch management
        """
        self._duplicate_manager.compute_all(self.conn)

    def _reassess_faces_with_status(self) -> None:
        """Match unknown faces against known people (locked faces).

        This is the final phase of face processing - after face detection
        and grouping, we try to match newly detected unknown faces against
        the locked (manually tagged) faces of known people.

        Uses synchronous reassessment to ensure completion before
        emit_processing_complete is called.
        """
        from faces import get_cached_known_embeddings, reassess_unknown_faces

        # Check if there are any known people with locked faces to match against
        with self._db_lock:
            known_embeddings = get_cached_known_embeddings(self.conn)

        if not known_embeddings:
            logger.info('Face reassessment: no known faces to match against')
            return

        logger.info('Face reassessment: matching unknown faces against known people')

        # Set status for progress display
        with self._phase4_status_lock:
            self._face_reassess_status = {'status': 'computing'}

        try:
            with self._db_lock:
                matches = reassess_unknown_faces(self.conn, threshold=self.config.face_recognition_threshold)
            if matches:
                logger.info(f'Face reassessment: matched {len(matches)} faces to known people')
                # Build per-face update list for the frontend (same format as
                # async reassessment in faces.py).  Look up person names so the
                # frontend can update its cache without a round-trip.
                person_names: dict[str, str] = {}
                unique_pids = {pid for _, pid, _ in matches}
                with self._db_lock:
                    for pid in unique_pids:
                        row = self.conn.execute(
                            'SELECT name FROM people WHERE id = ?',
                            (pid,),
                        ).fetchone()
                        if row:
                            person_names[pid] = row['name']
                updated_faces = [
                    {
                        'face_id': face_id,
                        'person_id': pid,
                        'person_name': person_names.get(pid, ''),
                    }
                    for face_id, pid, _ in matches
                ]
                self.event_queue.emit(
                    'faces_reassessed',
                    {
                        'matched_count': len(matches),
                        'updated_faces': updated_faces,
                    },
                )
            else:
                logger.info('Face reassessment: no new matches found')
        finally:
            with self._phase4_status_lock:
                self._face_reassess_status = None

    def queue_rescan_all(self) -> None:
        """Queue all registered folders for rescanning and full processing.

        This triggers the full processing chain:
        1. Rescan folders for new/changed/deleted files
        2. Queue images with missing embeddings
        3. (Automatic) Face detection after embeddings complete
        4. (Automatic) Duplicate grouping and face grouping after face detection

        Called from GUI "Rescan" button - enables all processing phases.
        """
        logger.info('Queueing full rescan of all folders')

        # Enable all processing phases (GUI rescan runs everything)
        self._run_face_detection = True
        self._run_face_grouping = True

        self._rescan_all_folders()

        # Queue images that need embedding (new images or images without embeddings)
        missing_embeddings = get_images_without_embedding(self.conn)
        for image in missing_embeddings:
            self._embedding_queue.put(image['id'])
        if missing_embeddings:
            logger.info(f'{len(missing_embeddings)} images queued for image embedding')

        # Reset completion flags so callbacks fire again
        if self._embedding_thread:
            self._embedding_thread._completion_triggered = False
        if self._face_thread:
            self._face_thread._completion_triggered = False

    # =========================================================================
    # Public API - Events (SSE)
    # =========================================================================

    def get_pending_events(self, since: float = 0) -> dict[str, Any]:
        """Get events newer than the given timestamp.

        Multi-client safe: events are not drained on read. Each client
        passes its cursor (``since``) and receives only new events.

        Args:
            since: Unix timestamp cursor from previous poll response.
                   Pass 0 for the initial poll.

        Returns:
            Dict with 'events' (list of dicts), 'server_time' (float),
            and 'stale' (bool).
        """
        result = self.event_queue.get_since(since)
        result['events'] = [{'type': e.event_type, 'data': e.data} for e in result['events']]
        return result

    def get_pending_event_count(self) -> int:
        """Get number of buffered events.

        Returns:
            Number of events currently in the buffer.
        """
        return self.event_queue.get_pending_count()

    # -------------------------------------------------------------------------
    # Face Recognition Methods
    # -------------------------------------------------------------------------

    def get_faces_for_image(
        self,
        image_id: str,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        """Get all faces detected in an image.

        Args:
            image_id: Image's UUID.
            include_suppressed: If True, include suppressed (false positive) faces.

        Returns:
            List of face dicts with person_name if identified.
        """
        with self._db_lock:
            return get_faces_for_image(self.conn, image_id, include_suppressed)


# =============================================================================
# GRACEFUL SHUTDOWN AND SIGNAL HANDLING
# =============================================================================

# Global reference for signal handlers
_active_database: ImageDatabase | None = None


def _signal_handler(signum: int, frame) -> None:
    """Handle shutdown signals (SIGINT, SIGTERM).

    Args:
        signum: Signal number.
        frame: Current stack frame (unused).
    """
    signal_name = signal.Signals(signum).name
    logger.info(f'Received {signal_name}, initiating graceful shutdown')

    if _active_database is not None:
        _active_database.close()

    # Re-raise to allow default handling
    raise SystemExit(0)


def _atexit_handler() -> None:
    """Handle cleanup on normal exit."""
    if _active_database is not None and not _active_database.is_closed:
        logger.info('Cleaning up on exit')
        _active_database.close()


def register_signal_handlers(db: ImageDatabase) -> None:
    """Register signal handlers for graceful shutdown.

    Registers handlers for SIGINT (Ctrl+C) and SIGTERM that will
    cleanly shut down the database before exiting.

    Args:
        db: ImageDatabase instance to shut down on signal.
    """
    global _active_database
    _active_database = db

    # Register signal handlers
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Register atexit handler for normal exit
    atexit.register(_atexit_handler)

    logger.debug('Signal handlers registered for graceful shutdown')
