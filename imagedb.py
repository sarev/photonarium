#!/usr/bin/env python3

"""
Imaginary image catalogue backend (single-file implementation).

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
from PIL import Image, ImageFile, ImageOps

# Tolerate truncated or mildly corrupt images rather than raising errors.
# Many real-world JPEGs are missing their EOI marker or have minor structural
# issues but are perfectly viewable in normal image viewers.
ImageFile.LOAD_TRUNCATED_IMAGES = True
from typing import Any, Callable, Iterator

import atexit
import cv2
import hashlib
import imagehash
import json
import logging
import numpy as np
import open_clip
import queue
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
import signal
import sqlite3
import threading
import time
import torch
import uuid
import warnings

# Local imports
from config import Config, load_config, get_default_config
from duplicates import DuplicateManager, embedding_to_numpy
from thumbnails import (
    DEFAULT_THUMBNAIL_DIR,
    get_thumbnail_cache_path,
    generate_thumbnail,
    rotate_image_file,
    delete_thumbnails_for_checksum,
)
from faces import (
    init_face_tables,
    FaceDetector,
    create_face,
    get_face,
    get_faces_for_image,
    has_faces_detected,
    mark_no_faces_detected,
    rotate_faces_for_image,
    get_all_known_face_embeddings,
    find_best_match,
    generate_face_thumbnail,
    generate_face_thumbnails_for_image,
    get_face_thumbnail_path,
    delete_face_thumbnail,
    delete_people_without_faces,
    compute_unknown_face_groups,
    get_group_computation_status,
    update_face_semantic_embedding,
    get_faces_without_semantic_embedding,
    get_all_faces_for_thumbnail_regen,
)
from timestamps import (
    derive_timestamp,
    derive_timestamp_with_confidence,
    CONFIDENCE_UNKNOWN,
)
from rawimage import (
    RAW_EXTENSIONS,
    is_raw_format,
    open_image as raw_open_image,
    open_image_as_numpy as raw_open_image_as_numpy,
    get_raw_dimensions,
)

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
    embedding             BLOB,
    description_embedding BLOB,
    deleted               INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
)
"""

# Migrations to run on existing databases
_SQL_MIGRATIONS = [
    # Add description_embedding column if it doesn't exist
    "ALTER TABLE images ADD COLUMN description_embedding BLOB",
    # Add mtime column for fast change detection (avoids checksum on every scan)
    "ALTER TABLE images ADD COLUMN mtime REAL",
    # Add updated_at column for duplicate epoch tracking
    "ALTER TABLE duplicate_groups ADD COLUMN updated_at TEXT",
    # Add timestamp_confidence column (0=user, 1=EXIF, 2=filename, 3=filesystem, 4=unknown)
    "ALTER TABLE images ADD COLUMN timestamp_confidence INTEGER NOT NULL DEFAULT 4",
]

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

# SQL schema for tracking one-time migrations
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
    "CREATE INDEX IF NOT EXISTS idx_images_path ON images(path)",
    "CREATE INDEX IF NOT EXISTS idx_images_checksum ON images(checksum)",
    "CREATE INDEX IF NOT EXISTS idx_images_perceptual_hash ON images(perceptual_hash)",
    "CREATE INDEX IF NOT EXISTS idx_images_deleted ON images(deleted)",
    "CREATE INDEX IF NOT EXISTS idx_images_timestamp ON images(timestamp)",
    # Composite index for efficient gallery listing (covers WHERE deleted=0 ORDER BY timestamp DESC)
    "CREATE INDEX IF NOT EXISTS idx_images_deleted_timestamp ON images(deleted, timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_dup_level_group ON duplicate_groups(level, group_hash)",
    "CREATE INDEX IF NOT EXISTS idx_dup_updated_at ON duplicate_groups(updated_at)",
    # Index for cascade deletes when an image is removed
    "CREATE INDEX IF NOT EXISTS idx_dup_image_id ON duplicate_groups(image_id)",
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
    conn.execute(_SQL_CREATE_MIGRATIONS)
    conn.execute(_SQL_CREATE_METADATA)

    # Create face recognition tables
    init_face_tables(conn)

    # Run migrations for existing databases (must run BEFORE indexes)
    for migration_sql in _SQL_MIGRATIONS:
        try:
            conn.execute(migration_sql)
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
    cursor = conn.execute(
        'SELECT 1 FROM migrations WHERE id = ?',
        (migration_id,)
    )
    return cursor.fetchone() is not None


def record_migration(conn: sqlite3.Connection, migration_id: str) -> None:
    """Record that a one-time migration has been applied.

    Args:
        conn: Database connection.
        migration_id: Unique identifier for the migration.
    """
    conn.execute(
        'INSERT OR REPLACE INTO migrations (id, applied_at) VALUES (?, datetime("now"))',
        (migration_id,)
    )
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
        folder_path_upper_bound('/photos/2024/')  # Returns '/photos/2024/~'
        # Query: WHERE path >= '/photos/2024/' AND path < '/photos/2024/~'
    """
    # Append '~' (ASCII 126) which is higher than all typical filename characters
    # This ensures we match all paths starting with folder_path but nothing else
    return folder_path + '~'


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
    cursor = conn.execute("""
        SELECT
            f.path,
            COUNT(i.id) as count
        FROM folders f
        LEFT JOIN images i ON i.path >= f.path AND i.path < f.path || '~' AND i.deleted = 0
        GROUP BY f.path
        ORDER BY f.path
    """)
    rows = cursor.fetchall()

    return [{'path': row['path'], 'count': row['count']} for row in rows]


def add_folder(
    conn: sqlite3.Connection,
    path: Path | str,
    config: Config | None = None,
) -> dict[str, Any] | None:
    """Register a new image source folder.

    Adds the folder to the database if not already registered. Does not
    scan the folder for images - that should be triggered separately via
    the ingestion queue.

    Args:
        conn: Database connection.
        path: Absolute path to the folder to register.
        config: Configuration object (used for image extensions when scanning).
            If None, uses default configuration.

    Returns:
        Dictionary with folder info {'path': str, 'count': int, 'new_images': list},
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

    # Find image files in the folder (for queuing to ingestion)
    if config is None:
        config = get_default_config()

    new_images = list(find_images_in_folder(path, config.image_extensions))

    return {
        'path': path_str,
        'count': 0,  # No images ingested yet
        'new_images': new_images,  # Paths to queue for ingestion
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
        conditions = ' AND '.join(
            ['NOT (path >= ? AND path < ?)'] * len(remaining_folders)
        )
        # Build params: each folder needs (folder_path, folder_path + '~')
        params = [datetime.now().isoformat()]
        for folder in remaining_folders:
            params.extend([folder, folder_path_upper_bound(folder)])
        conn.execute(
            f'UPDATE images SET deleted = 1, updated_at = ? WHERE {conditions}',
            params
        )
    else:
        # No folders left, mark all images as deleted
        conn.execute(
            'UPDATE images SET deleted = 1, updated_at = ? WHERE deleted = 0',
            (datetime.now().isoformat(),)
        )

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

        # Skip subdirectories that are separately registered (optimisation)
        # Modify dirs in-place to prevent os.walk from descending
        dirs_to_remove = []
        for d in dirs:
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
        id, basename, size, width, height, timestamp, timestamp_confidence, rating, description.
    """
    cursor = conn.execute("""
        SELECT id, basename, size, width, height, timestamp, timestamp_confidence, rating, description
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
        - updated: List of added/modified images (lightweight fields + deleted flag)
        - deleted_ids: List of IDs for images that are now deleted
    """
    # Get current epoch (max updated_at)
    epoch_cursor = conn.execute("SELECT MAX(updated_at) as epoch FROM images")
    epoch_row = epoch_cursor.fetchone()
    current_epoch = epoch_row['epoch'] if epoch_row and epoch_row['epoch'] else since

    # Get all images changed since the given timestamp
    cursor = conn.execute("""
        SELECT id, basename, width, height, timestamp, timestamp_confidence, rating, description, deleted, updated_at
        FROM images
        WHERE updated_at > ?
        ORDER BY updated_at ASC
    """, (since,))

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
    cursor = conn.execute("SELECT MAX(updated_at) as epoch FROM images")
    row = cursor.fetchone()
    return row['epoch'] if row else None


def get_image(conn: sqlite3.Connection, image_id: str) -> dict[str, Any] | None:
    """Get a single image by ID.

    Args:
        conn: Database connection.
        image_id: UUID of the image.

    Returns:
        Image dictionary with all metadata fields, or None if not found.
    """
    cursor = conn.execute("""
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating, deleted, created_at, updated_at,
               mtime
        FROM images
        WHERE id = ?
    """, (image_id,))

    return row_to_dict(cursor.fetchone())


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
    cursor = conn.execute(
        'SELECT checksum, path FROM images WHERE id = ? AND deleted = 0',
        (image_id,)
    )
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

    cursor = conn.execute("""
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating, embedding, deleted, created_at,
               updated_at, mtime
        FROM images
        WHERE path = ?
    """, (path_str,))

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

    conn.execute("""
        INSERT INTO images (
            id, path, basename, size, width, height, timestamp, timestamp_confidence,
            checksum, perceptual_hash, laplacian_var, lossless, mtime,
            description, rating, embedding, deleted, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
    """, (
        image_id, path_str, basename, size, width, height, timestamp_str, timestamp_confidence,
        checksum, perceptual_hash, laplacian_var, int(lossless), mtime,
        description, rating, now, now
    ))
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
        'description', 'rating', 'size', 'width', 'height', 'timestamp',
        'timestamp_confidence', 'checksum', 'perceptual_hash', 'laplacian_var',
        'lossless', 'embedding', 'description_embedding', 'deleted'
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
    set_clause = ', '.join(f'{k} = ?' for k in updates.keys())
    values = list(updates.values()) + [image_id]

    conn.execute(
        f'UPDATE images SET {set_clause} WHERE id = ?',
        values
    )
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

    Returns:
        True if image was updated, False if not found.
    """
    timestamp_str = timestamp.isoformat() if timestamp else None
    now = datetime.now().isoformat()

    cursor = conn.execute("""
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
            embedding = NULL,
            updated_at = ?
        WHERE id = ?
    """, (
        size, width, height, timestamp_str, timestamp_confidence, checksum,
        perceptual_hash, laplacian_var, int(lossless), mtime, now, image_id
    ))
    conn.commit()

    if cursor.rowcount > 0:
        logger.debug(f'Updated metadata for image: {image_id}')
        return True
    return False


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
        conn.execute(
            'UPDATE images SET deleted = 1, updated_at = ? WHERE id = ?',
            (now, image_id)
        )
        conn.commit()
        logger.info(f'Soft deleted image: {image_id}')

    return True


def restore_image(conn: sqlite3.Connection, image_id: str) -> bool:
    """Restore a soft-deleted image.

    Args:
        conn: Database connection.
        image_id: UUID of the image to restore.

    Returns:
        True if image was restored, False if not found or not deleted.
    """
    now = datetime.now().isoformat()

    cursor = conn.execute(
        'UPDATE images SET deleted = 0, updated_at = ? WHERE id = ? AND deleted = 1',
        (now, image_id)
    )
    conn.commit()

    if cursor.rowcount > 0:
        logger.info(f'Restored image: {image_id}')
        return True
    return False


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


def get_images_in_folder(
    conn: sqlite3.Connection,
    folder: Path | str,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """Get all images within a folder.

    Args:
        conn: Database connection.
        folder: Folder path.
        include_deleted: If True, include soft-deleted images.

    Returns:
        List of image dictionaries within the folder.
    """
    folder_str = str(canonicalise_path(folder))

    # Use range query instead of LIKE for index efficiency
    folder_upper = folder_path_upper_bound(folder_str)

    if include_deleted:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   timestamp_confidence, checksum, perceptual_hash, laplacian_var,
                   lossless, description, rating, deleted, created_at, updated_at
            FROM images
            WHERE path >= ? AND path < ?
            ORDER BY path ASC
        """, (folder_str, folder_upper))
    else:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   timestamp_confidence, checksum, perceptual_hash, laplacian_var,
                   lossless, description, rating, deleted, created_at, updated_at
            FROM images
            WHERE path >= ? AND path < ? AND deleted = 0
            ORDER BY path ASC
        """, (folder_str, folder_upper))

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
                logger.info(
                    f'Downsampling oversized image for phash {path}: '
                    f'{w}x{h} -> {new_w}x{new_h}'
                )
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
                logger.info(
                    f'Downsampling oversized image for laplacian {path}: '
                    f'{w}x{h} -> {new_w}x{new_h}'
                )
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

    # Derive timestamp with confidence level
    timestamp, timestamp_confidence = derive_timestamp_with_confidence(path)

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
                        logger.info(
                            f'Indexing progress: {self._processed_count} done, '
                            f'{remaining} remaining'
                        )
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
                    self.conn.execute(
                        'UPDATE images SET mtime = ? WHERE id = ?',
                        (current_mtime, existing['id'])
                    )
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
                else:
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
                )

            # Update checksum cache
            if metadata.checksum:
                with self._checksum_cache_lock:
                    self._checksum_cache[existing['id']] = metadata.checksum

            # Generate thumbnail for changed image
            if metadata.checksum:
                self._generate_thumbnails(path, metadata.checksum)

            # Queue for embedding (metadata cleared embedding)
            self.embedding_queue.put(existing['id'])
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
                )

            # Add to checksum cache
            if metadata.checksum:
                with self._checksum_cache_lock:
                    self._checksum_cache[image_id] = metadata.checksum

            # Generate thumbnail for new image
            if metadata.checksum:
                self._generate_thumbnails(path, metadata.checksum)

            # Queue for embedding
            self.embedding_queue.put(image_id)
            logger.debug(f'Ingested new image: {path}')


def queue_images_for_ingestion(
    ingestion_queue: queue.Queue[Path],
    paths: list[Path] | Iterator[Path],
) -> int:
    """Add multiple image paths to the ingestion queue.

    Args:
        ingestion_queue: The ingestion queue.
        paths: Iterable of paths to add.

    Returns:
        Number of paths added to the queue.
    """
    count = 0
    for path in paths:
        ingestion_queue.put(path)
        count += 1
    return count


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
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

        self._model = None
        self._preprocess = None
        self._tokenizer = None

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
                    logger.info(
                        f'Downsampling oversized image {path}: '
                        f'{w}x{h} -> {new_w}x{new_h}'
                    )
                    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            return img.convert('RGB')

        except Exception as e:
            logger.warning(f'Failed to load image {path}: {e}')
            return None

    def _load_model(self) -> None:
        """Load the model (called on first use)."""
        if self._model is not None:
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
        elif token == '-':
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
        self.on_complete = on_complete

        self._clip_model: OpenCLIPModel | None = None
        self._processed_count = 0
        self._error_count = 0
        self._completion_triggered = False

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
                                f'Image embedding progress: {self._processed_count} done, '
                                f'{remaining} remaining'
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

        Args:
            image_ids: List of image IDs.
            paths: List of corresponding file paths.
        """
        logger.debug(f'Processing embedding batch of {len(paths)} images')

        # Encode batch
        results = self.clip_model.encode_images_batch(paths)

        # Collect successful embeddings for batch commit
        updates = []
        for (idx, embedding), image_id in zip(results, image_ids):
            try:
                if embedding is not None:
                    embedding_bytes = embedding.astype(np.float32).tobytes()
                    updates.append((embedding_bytes, datetime.now().isoformat(), image_id))
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
                    'UPDATE images SET embedding = ?, updated_at = ? WHERE id = ?',
                    updates
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
        results = self.face_detector.detect_faces_from_preloaded(
            loaded_images, stop_event=self.stop_event
        )

        # Get known face embeddings for auto-recognition
        with self._db_lock:
            known_embeddings = get_all_known_face_embeddings(self.conn)

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
                # Try to auto-match against known faces
                person_id = None
                match = find_best_match(
                    face.embedding,
                    known_embeddings,
                    threshold=self.config.face_recognition_threshold,
                )
                if match:
                    _, person_id, similarity = match
                    logger.debug(
                        f'Auto-matched face in {path.name} to person {person_id} '
                        f'(similarity: {similarity:.3f})'
                    )

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
                    semantic_embedding = self.embedding_thread.clip_model.encode_image(
                        thumb_path
                    )

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
    conn.execute(
        'INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)',
        (key, value)
    )
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
    cursor = conn.execute(f"""
        SELECT id, path, basename, size, width, height, timestamp,
               timestamp_confidence, checksum, perceptual_hash, laplacian_var,
               lossless, description, rating
        FROM images
        WHERE id IN ({placeholders})
    """, top_ids)

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

def get_or_create_thumbnail(
    conn: sqlite3.Connection,
    image_id: str,
    size: int = 200,
    thumbnail_dir: Path | str = DEFAULT_THUMBNAIL_DIR,
    quality: int = 85,
    max_source_dimension: int = 0,
) -> Path | None:
    """Get a thumbnail for an image, generating if necessary.

    Only two canonical sizes are supported: 200px and 400px. Any other
    size is snapped to the nearest canonical size. Thumbnails are
    pre-generated during indexing, so this should rarely need to
    generate on-demand.

    Args:
        conn: Database connection.
        image_id: UUID of the image.
        size: Requested size (snapped to 200 or 400).
        thumbnail_dir: Root thumbnail cache directory.
        quality: JPEG quality for generated thumbnails.
        max_source_dimension: Max dimension for draft mode (0 to disable).

    Returns:
        Path to the thumbnail file, or None if image not found or
        thumbnail cannot be generated.
    """
    # Snap to canonical size (200 or 400)
    size = 400 if size > 300 else 200

    # Get image info
    image = get_image(conn, image_id)
    if image is None:
        logger.warning(f'Image not found for thumbnail: {image_id}')
        return None

    checksum = image.get('checksum')
    if not checksum:
        logger.warning(f'Image has no checksum for thumbnail: {image_id}')
        return None

    # Get cache path
    cache_path = get_thumbnail_cache_path(checksum, size, thumbnail_dir)

    # Check if cached thumbnail exists
    if cache_path.exists():
        return cache_path

    # Generate thumbnail
    source_path = Path(image['path'])
    if not source_path.exists():
        logger.warning(f'Source image not found for thumbnail: {source_path}')
        return None

    if generate_thumbnail(source_path, cache_path, size, quality, max_source_dimension):
        return cache_path

    return None


def cleanup_orphaned_thumbnails(
    conn: sqlite3.Connection,
    thumbnail_dir: Path | str = DEFAULT_THUMBNAIL_DIR,
) -> int:
    """Remove thumbnails for images no longer in the database.

    Args:
        conn: Database connection.
        thumbnail_dir: Root thumbnail cache directory.

    Returns:
        Number of orphaned thumbnails deleted.
    """
    thumbnail_dir = Path(thumbnail_dir)

    if not thumbnail_dir.exists():
        return 0

    # Get all valid checksums
    cursor = conn.execute('SELECT DISTINCT checksum FROM images WHERE checksum IS NOT NULL')
    valid_checksums = {row['checksum'] for row in cursor.fetchall()}

    count = 0
    for thumb_file in thumbnail_dir.rglob('*.jpg'):
        # Extract checksum from filename
        checksum = thumb_file.stem
        if checksum not in valid_checksums:
            try:
                thumb_file.unlink()
                count += 1
            except OSError:
                pass

    logger.info(f'Removed {count} orphaned thumbnails')
    return count


# =============================================================================
# EVENT QUEUE AND SSE
# =============================================================================

# Event types
EVENT_FOLDER_ADDED = 'folder_added'
EVENT_FOLDER_REMOVED = 'folder_removed'
EVENT_IMAGE_INGESTED = 'image_ingested'
EVENT_PROCESSING_COMPLETE = 'processing_complete'
EVENT_ERROR = 'error'
EVENT_FACES_REASSESSED = 'faces_reassessed'
EVENT_IMAGES_MODIFIED = 'images_modified'


@dataclass
class Event:
    """Server-Sent Event data container.

    Attributes:
        event_type: Type of event (e.g., 'folder_added', 'processing_complete').
        data: Event payload as dictionary.
        timestamp: When the event occurred.
    """
    event_type: str
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)

    def to_sse(self) -> str:
        """Format event as Server-Sent Event string.

        Returns:
            SSE-formatted string ready to send to client.
        """
        # SSE format: "event: <type>\ndata: <json>\n\n"
        json_data = json.dumps({
            'type': self.event_type,
            'data': self.data,
            'timestamp': self.timestamp.isoformat(),
        })
        return f'event: {self.event_type}\ndata: {json_data}\n\n'


class EventQueue:
    """Thread-safe event queue for frontend polling.

    Accumulates events in a single queue. Frontend polls periodically
    to fetch pending events. This replaces SSE which blocked threads.

    Attributes:
        _events: List of pending events.
        _lock: Threading lock for thread safety.
    """

    # Maximum events to accumulate (prevents unbounded growth if frontend stops polling)
    MAX_EVENTS = 100

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
                self._events = self._events[-self.MAX_EVENTS:]
        logger.debug(f'Event queued: {event_type} (pending: {len(self._events)})')

    def get_pending_count(self) -> int:
        """Get number of pending events.

        Returns:
            Number of events waiting to be fetched.
        """
        with self._lock:
            return len(self._events)

    def get_pending(self) -> list[Event]:
        """Fetch and clear all pending events.

        Returns:
            List of pending events (oldest first). Queue is cleared.
        """
        with self._lock:
            events = self._events
            self._events = []
        if events:
            logger.debug(f'Fetched {len(events)} pending events')
        return events


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


def emit_image_ingested(
    event_queue: EventQueue,
    image_id: str,
    path: str,
) -> None:
    """Emit an image_ingested event.

    Args:
        event_queue: EventQueue instance.
        image_id: UUID of the ingested image.
        path: Path of the ingested image.
    """
    event_queue.emit(EVENT_IMAGE_INGESTED, {'id': image_id, 'path': path})


def emit_processing_complete(event_queue: EventQueue) -> None:
    """Emit a processing_complete event.

    Args:
        event_queue: EventQueue instance.
    """
    event_queue.emit(EVENT_PROCESSING_COMPLETE, {})


def emit_error(event_queue: EventQueue, message: str) -> None:
    """Emit an error event.

    Args:
        event_queue: EventQueue instance.
        message: Error message.
    """
    event_queue.emit(EVENT_ERROR, {'message': message})


def emit_faces_reassessed(
    event_queue: EventQueue,
    matched_count: int,
    person_id: str | None = None,
) -> None:
    """Emit a faces_reassessed event when async face reassessment completes.

    Args:
        event_queue: EventQueue instance.
        matched_count: Number of faces that were auto-matched.
        person_id: If reassessment was for a specific person, their ID.
    """
    event_queue.emit(EVENT_FACES_REASSESSED, {
        'matched_count': matched_count,
        'person_id': person_id,
    })


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
        db_path: Path | str = 'imaginary.db',
        thumbnail_dir: Path | str = '.thumbnails',
        config_path: Path | str | None = None,
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
        logger.info('IMAGINARY - Image Catalogue Backend')
        logger.info('=' * 60)

        # Step 0: Load configuration
        logger.info('[1/5] Loading configuration...')
        self.config = load_config(config_path)

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

        # Create queues
        self._ingestion_queue: queue.Queue[Path] = queue.Queue()
        self._embedding_queue: queue.Queue[str] = queue.Queue()
        self._face_queue: queue.Queue[str] = queue.Queue()

        # Thread references (created when started)
        self._ingestion_thread: IngestionThread | None = None
        self._embedding_thread: EmbeddingThread | None = None
        self._face_thread: FaceDetectionThread | None = None

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

        # Steps 6-7: Start background threads
        self.start_threads()

        # Optionally pre-load OpenCLIP model to show download progress during startup
        if self._preload_model and self._embedding_thread is not None:
            logger.info('Pre-loading OpenCLIP model...')
            # Access the clip_model property to trigger loading
            _ = self._embedding_thread.clip_model

        # Backfill description embeddings for images with descriptions but no embedding
        self._backfill_description_embeddings()

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

                self.conn.execute(
                    'UPDATE images SET description_embedding = ? WHERE id = ?',
                    (embedding_bytes, image_id)
                )
                count += 1
            except Exception as e:
                logger.warning(f'Failed to compute description embedding for {image_id}: {e}')

        self.conn.commit()
        logger.info(f'        Backfilled {count} description embeddings')

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
                        'UPDATE images SET timestamp = ? WHERE id = ?',
                        (new_timestamp.isoformat(), image_id)
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
                    (timestamp_str, confidence, image_id)
                )
                updated += 1
            except Exception as e:
                logger.warning(f'Failed to derive timestamp confidence for {path}: {e}')

        self.conn.commit()
        record_migration(self.conn, migration_id)
        logger.info(f'        Updated {updated} images with timestamp_confidence')

    def _load_checksum_cache(self) -> None:
        """Load all image_id -> checksum mappings into RAM.

        Called during startup to populate the cache. This eliminates
        DB queries for thumbnail lookups.
        """
        cursor = self.conn.execute(
            'SELECT id, checksum FROM images WHERE checksum IS NOT NULL AND deleted = 0'
        )
        cache = {row[0]: row[1] for row in cursor}
        with self._checksum_cache_lock:
            self._checksum_cache = cache
        logger.info(f'        Loaded {len(cache)} checksums into cache')

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
                        'UPDATE images SET deleted = 1, updated_at = ? WHERE path = ? AND deleted = 0',
                        (now, path)
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
        cursor = self.conn.execute('''
            SELECT i.id
            FROM images i
            WHERE i.deleted = 0
              AND i.embedding IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM faces f WHERE f.image_id = i.id
              )
        ''')
        rows = cursor.fetchall()

        if rows:
            logger.info(f'Queueing {len(rows)} images for face detection')
            for row in rows:
                self._face_queue.put(row['id'])

    def start_threads(self) -> None:
        """Start the background processing threads."""
        if self._ingestion_thread is not None and self._ingestion_thread.is_alive():
            logger.warning('Threads already running')
            return

        self._stop_event.clear()

        # Final completion callback (after all processing including faces)
        def on_final_complete():
            if not self._run_face_grouping:
                logger.info('Skipping grouping phase (use --group-faces or GUI Rescan)')
                return
            # Clean up people with no faces
            with self._db_lock:
                delete_people_without_faces(self.conn)
            self._compute_duplicates_with_status()
            # Compute unknown face groups (similar to duplicate grouping)
            if self.config.face_detection_enabled:
                with self._db_lock:
                    compute_unknown_face_groups(
                        self.conn,
                        threshold=self.config.face_recognition_threshold
                    )
                # Backfill semantic embeddings for faces that don't have them
                # (e.g., faces added before this feature existed)
                self.backfill_face_semantic_embeddings()
                # Match unknown faces against known people (locked faces)
                self._reassess_faces_with_status()
            emit_processing_complete(self.event_queue)

        # Callback when embedding completes - queue images for face detection
        def on_embedding_complete():
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

    def __enter__(self) -> 'ImageDatabase':
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

    def _check_open(self) -> None:
        """Raise an error if the database is closed.

        Raises:
            RuntimeError: If the database has been closed.
        """
        if self._closed:
            raise RuntimeError('Database has been closed')

    # =========================================================================
    # Thread-Safe Database Operations
    # =========================================================================

    def _execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write operation with locking.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the SQL statement.

        Returns:
            Cursor from the execution.
        """
        self._check_open()
        with self._db_lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
            return cursor

    def _execute_read(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a read operation.

        Note: SQLite WAL mode allows concurrent reads without locking.

        Args:
            sql: SQL statement to execute.
            params: Parameters for the SQL statement.

        Returns:
            Cursor from the execution.
        """
        self._check_open()
        return self.conn.execute(sql, params)

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
            result = add_folder(self.conn, path, self.config)
        if result is not None:
            # Queue images for ingestion
            for image_path in result['new_images']:
                self._ingestion_queue.put(image_path)
            emit_folder_added(self.event_queue, result['path'])
            # Remove the paths list from return value
            del result['new_images']
        return result

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
                    self._duplicate_manager.invalidate_images(orphaned_ids)
                emit_folder_removed(self.event_queue, path)

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
        cursor = self.conn.execute(
            'SELECT path FROM folders WHERE path != ?',
            (folder_str,)
        )
        remaining_folders = [row['path'] for row in cursor.fetchall()]

        # Find images in this folder that won't be covered by remaining folders
        # Use range queries instead of LIKE for index efficiency
        folder_upper = folder_path_upper_bound(folder_str)

        if remaining_folders:
            # Images that start with this folder but don't start with any remaining folder
            not_conditions = ' AND '.join(
                ['NOT (path >= ? AND path < ?)'] * len(remaining_folders)
            )
            # Build params: folder range, then each remaining folder's range
            params = [folder_str, folder_upper]
            for remaining in remaining_folders:
                params.extend([remaining, folder_path_upper_bound(remaining)])
            cursor = self.conn.execute(
                f'SELECT id FROM images WHERE path >= ? AND path < ? AND deleted = 0 AND {not_conditions}',
                params
            )
        else:
            # No other folders, all images in this folder will be orphaned
            cursor = self.conn.execute(
                'SELECT id FROM images WHERE path >= ? AND path < ? AND deleted = 0',
                (folder_str, folder_upper)
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
        cursor = self.conn.execute(
            'SELECT path FROM images WHERE id = ? AND deleted = 0',
            (image_id,)
        )
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
                        '''UPDATE images SET
                            checksum = ?,
                            size = ?,
                            width = ?,
                            height = ?,
                            mtime = ?,
                            updated_at = ?
                        WHERE id = ?''',
                        (new_checksum, new_size, new_width, new_height, new_mtime,
                         datetime.now().isoformat(), image_id)
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
                            logger.debug(f'rotate_image: Face {face_id[:8]}... old_bbox={old_bbox}, thumb_existed={thumb_existed}, deleted={deleted}')

                            # Get updated face coordinates (after rotation)
                            updated_face = get_face(self.conn, face_id)
                            if updated_face:
                                new_bbox = (updated_face['box_x'], updated_face['box_y'],
                                            updated_face['box_w'], updated_face['box_h'])
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
                                    logger.debug(f'rotate_image: Face {face_id[:8]}... regen_success={success}, thumb_exists_now={thumb_exists_now}, thumb_path={thumb_path}')
                                except Exception as e:
                                    logger.warning(f'rotate_image: Failed to regenerate face thumbnail for {face_id}: {e}')
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
            f'SELECT id, embedding FROM images WHERE id IN ({placeholders}) AND embedding IS NOT NULL',
            image_ids
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
        cursor = self.conn.execute(
            'SELECT id, embedding, deleted FROM images WHERE id = ?',
            (reference_image_id,)
        )
        row = cursor.fetchone()

        # Debug logging
        if row is None:
            logger.warning(f'get_similar_images: No row found for id={reference_image_id}')
            return None

        logger.info(f'get_similar_images: id={reference_image_id}, deleted={row["deleted"]}, has_embedding={row["embedding"] is not None}')

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
            cache_path = get_thumbnail_cache_path(
                checksum, size=size, thumbnail_dir=self.thumbnail_dir
            )
            if cache_path.exists():
                continue
            if not generate_thumbnail(
                source_path, cache_path,
                size=size, quality=self.config.thumbnail_quality,
                max_source_dimension=self.config.max_image_dimension
            ):
                success = False
        return success

    def get_thumbnail_path(self, image_id: str, size: int = 200) -> Path | None:
        """Get or create a thumbnail for an image."""
        return get_or_create_thumbnail(
            self.conn,
            image_id,
            size,
            self.thumbnail_dir,
            self.config.thumbnail_quality,
            self.config.max_image_dimension,
        )

    # =========================================================================
    # Public API - Duplicates
    # =========================================================================

    def get_duplicate_groups(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups at a specific level."""
        return self._duplicate_manager.get_groups(level)

    def get_duplicate_groups_lightweight(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups with minimal data for efficient display."""
        return self._duplicate_manager.get_groups_lightweight(level)

    def get_duplicate_epoch(self) -> str:
        """Get the current epoch timestamp for duplicate groups."""
        return self._duplicate_manager.get_epoch()

    # =========================================================================
    # Public API - Stats and Status
    # =========================================================================

    def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        cursor = self.conn.execute(
            'SELECT COUNT(*) as count FROM images WHERE deleted = 0'
        )
        total_images = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM folders')
        total_folders = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM people')
        total_people = cursor.fetchone()['count']

        cursor = self.conn.execute(
            'SELECT COUNT(*) as count FROM faces WHERE suppressed = 0'
        )
        total_faces = cursor.fetchone()['count']

        return {
            'totalImages': total_images,
            'totalFolders': total_folders,
            'totalPeople': total_people,
            'totalFaces': total_faces,
        }

    def get_processing_status(self) -> dict[str, Any]:
        """Get current processing status.

        Returns:
            Dict with status, queue counts, and Phase 4 processing statuses.
        """
        indexing_count = self._ingestion_queue.qsize()
        embedding_count = self._embedding_queue.qsize()
        face_count = self._face_queue.qsize()

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

        # Determine overall status
        queues_empty = (indexing_count == 0 and embedding_count == 0 and face_count == 0)
        phase4_idle = not (duplicates_computing or face_grouping_computing or face_embedding_computing or face_reassess_computing)
        status = 'up_to_date' if (queues_empty and phase4_idle) else 'updating'

        # Get counts for live updates during processing
        cursor = self.conn.execute(
            'SELECT COUNT(*) as count FROM images WHERE deleted = 0'
        )
        total_images = cursor.fetchone()['count']

        cursor = self.conn.execute('SELECT COUNT(*) as count FROM people')
        total_people = cursor.fetchone()['count']

        cursor = self.conn.execute(
            'SELECT COUNT(*) as count FROM faces WHERE suppressed = 0'
        )
        total_faces = cursor.fetchone()['count']

        # Build response - only include Phase 4 statuses if they're active
        result = {
            'status': status,
            'indexing_queue': indexing_count,
            'embedding_queue': embedding_count,
            'face_queue': face_count,
            'total_images': total_images,
            'total_people': total_people,
            'total_faces': total_faces,
            'face_detection_enabled': self.config.face_detection_enabled,
        }

        # Include duplicate status if computing
        if duplicates_computing:
            # Find which level is currently computing
            computing_level = next(
                (level for level, s in duplicate_status.items() if s == 'computing'),
                None
            )
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
        from faces import reassess_unknown_faces, get_cached_known_embeddings

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
                matches = reassess_unknown_faces(
                    self.conn,
                    threshold=self.config.face_recognition_threshold
                )
            if matches:
                logger.info(f'Face reassessment: matched {len(matches)} faces to known people')
                # Emit event so frontend can refresh
                self.event_queue.emit('faces_reassessed', {'matched': len(matches)})
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

    def get_pending_events(self) -> list[dict[str, Any]]:
        """Get all pending events and clear the queue.

        Returns:
            List of event dicts with 'type' and 'data' keys.
        """
        events = self.event_queue.get_pending()
        return [{'type': e.event_type, 'data': e.data} for e in events]

    def get_pending_event_count(self) -> int:
        """Get number of pending events without fetching them.

        Returns:
            Number of events in queue.
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

    def queue_image_for_face_detection(self, image_id: str) -> None:
        """Queue an image for face detection.

        Args:
            image_id: Image's UUID.
        """
        if self.config.face_detection_enabled:
            self._face_queue.put(image_id)


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


def unregister_signal_handlers() -> None:
    """Unregister signal handlers and atexit handler."""
    global _active_database
    _active_database = None

    # Restore default signal handlers
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # Note: atexit handlers cannot be unregistered individually,
    # but our handler checks if database is already closed

    logger.debug('Signal handlers unregistered')


class GracefulShutdown:
    """Context manager for graceful shutdown handling.

    Automatically registers signal handlers on entry and ensures
    clean shutdown on exit or signal.

    Example::

        with GracefulShutdown(ImageDatabase()) as db:
            # Use db...
            pass  # Automatically closes on exit or Ctrl+C
    """

    def __init__(self, db: ImageDatabase):
        """Initialise with an ImageDatabase instance.

        Args:
            db: ImageDatabase instance to manage.
        """
        self.db = db

    def __enter__(self) -> ImageDatabase:
        """Register signal handlers and return the database."""
        register_signal_handlers(self.db)
        return self.db

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Unregister handlers and close the database."""
        unregister_signal_handlers()
        self.db.close()


def wait_for_completion(
    db: ImageDatabase,
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    """Wait for all processing to complete.

    Blocks until both ingestion and embedding queues are empty.

    Args:
        db: ImageDatabase instance.
        poll_interval: Seconds between status checks.
        timeout: Maximum time to wait, or None for no limit.

    Returns:
        True if processing completed, False if timeout reached.
    """
    start_time = time.time()

    while True:
        status = db.get_processing_status()
        if status['status'] == 'up_to_date':
            return True

        if timeout is not None:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return False

        time.sleep(poll_interval)
