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

1) Configuration
    - DEFAULT_CONFIG_PATH is a YAML file written to disk on first run.
    - `Config` defines supported keys and validates ranges.
    - `load_config()` creates the file if missing, then loads and validates.

2) Timestamp extraction utilities
    - EXIF parsing and robust filename/path parsing helpers.
    - `derive_timestamp()` applies a priority order so timestamps are stable even
      when files have been copied between machines.

3) Database schema and initialisation
    - SQL DDL strings and `init_database()` which enables WAL mode, creates
      tables/indexes, and applies lightweight migrations.

4) Folder management and scanning
    - Canonical path handling.
    - Folder registration helpers.
    - A scanner that walks registered folders and queues discovered image paths.

5) Image CRUD helpers
    - Thin helpers that read/write dictionaries to/from the `images` table.
    - Soft delete is supported (mark rows as deleted) with an option to delete
      from disk and/or hard-delete the row.

6) Embedding and search helpers
    - `semantic_search()` compares a query embedding with stored embeddings.
    - `get_images_by_similarity()` compares one image to all others.
    - These functions assume vectors are already normalised, so cosine similarity
      reduces to a dot product.

7) Thumbnail helpers
    - Cache path calculation, thumbnail generation, cache cleanup.

8) SSE events
    - `Event`, `EventQueue`, and `create_sse_generator()`.

9) Background threads
    - Ingestion thread: consumes file paths, extracts metadata, writes rows, and
      queues image IDs for embedding when needed.
    - Embedding thread: batches queued image IDs, computes embeddings using
      OpenCLIP, stores results, and can trigger duplicate group computation once
      all queues are drained.

10) `ImageDatabase` public API wrapper
    - Owns a single SQLite connection (created with thread usage in mind), the
      queues, and thread control events.
    - Provides methods intended for external callers:
        * folder management (add/remove/list)
        * image listing and updates
        * thumbnail retrieval
        * semantic search and similarity
        * duplicate group retrieval
        * stats and processing status
        * SSE stream generator

11) Graceful shutdown helpers
    - Signal handlers and a context manager to ensure threads stop and the DB is
      closed on exit.

12) Standalone test mode
    - When executed directly, the module can run a basic automated test suite
      that creates temporary images, exercises ingestion, and checks key paths.

-------------------------------------------------------------------------------
Threading and safety notes
-------------------------------------------------------------------------------

- Two worker threads are used by default (ingestion, embedding).
- Work is coordinated through `queue.Queue` instances.
- The database connection is shared, so the module uses locking and creates the
  connection in a way that supports thread usage.
- "Up to date" means both queues are empty, not necessarily that the filesystem
  will never change. Rescans can be queued explicitly.

"""

# =============================================================================
# IMPORTS
# =============================================================================

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from PIL import Image, ImageOps
from PIL.ExifTags import TAGS
from typing import Any, Iterator

import atexit
import cv2
import hashlib
import imagehash
import json
import logging
import numpy as np
import open_clip
import os
import queue
import random
from concurrent.futures import ThreadPoolExecutor, Future
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import torch
import traceback
import uuid
import yaml

# Local imports
from config import Config, load_config, get_default_config, DEFAULT_CONFIG_PATH
from thumbnails import (
    DEFAULT_THUMBNAIL_DIR,
    get_thumbnail_cache_path,
    generate_thumbnail,
    rotate_image_file,
    delete_thumbnails_for_checksum,
    clear_thumbnail_cache,
)

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# TIMESTAMP EXTRACTION
# =============================================================================

# Regex patterns for parsing dates and times from filenames
# 8 digits: YYYYMMDD
_PATTERN_DATE_8DIGITS = re.compile(r'(\d{8})')
# 6 digits: YYMMDD
_PATTERN_DATE_6DIGITS = re.compile(r'(\d{6})')
# 3 groups with separator: YYYY-MM-DD or YY-MM-DD (separator is single non-digit)
_PATTERN_DATE_SEPARATED = re.compile(r'(\d{2,4})\D(\d{2})\D(\d{2})')
# Partial date patterns (for incomplete dates - default missing parts to Jan 1)
# Year-month: YYYY-MM or YYYYMM (4 digits for month to avoid matching YYMMDD)
_PATTERN_DATE_YEAR_MONTH_SEP = re.compile(r'((?:19|20)\d{2})\D(\d{2})(?!\d)')
_PATTERN_DATE_YEAR_MONTH = re.compile(r'((?:19|20)\d{2})(\d{2})(?!\d)')
# Year only: standalone 4-digit year (1900-2099) with word boundaries
_PATTERN_DATE_YEAR_ONLY = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')

# 6 digits for time: HHMMSS
_PATTERN_TIME_6DIGITS = re.compile(r'(\d{6})')
# 4 digits for time: HHMM
_PATTERN_TIME_4DIGITS = re.compile(r'(\d{4})')
# 2-3 groups with separator: HH:MM or HH:MM:SS
_PATTERN_TIME_SEPARATED = re.compile(r'(\d{2})\D(\d{2})(?:\D(\d{2}))?')


def _validate_date(year: int, month: int, day: int) -> bool:
    """Validate date components are within reasonable ranges.

    Args:
        year: Year value (should be 1900-2099).
        month: Month value (should be 1-12).
        day: Day value (should be 1-31).

    Returns:
        True if all components are valid, False otherwise.
    """
    return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31


def _validate_time(hour: int, minute: int, second: int) -> bool:
    """Validate time components are within reasonable ranges.

    Args:
        hour: Hour value (should be 0-23).
        minute: Minute value (should be 0-59).
        second: Second value (should be 0-59).

    Returns:
        True if all components are valid, False otherwise.
    """
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


def _parse_exif_datetime(exif_value: str) -> datetime | None:
    """Parse an EXIF datetime string into a datetime object.

    EXIF datetime format is typically "YYYY:MM:DD HH:MM:SS".

    Args:
        exif_value: EXIF datetime string.

    Returns:
        datetime object if parsing succeeds, None otherwise.
    """
    if not exif_value or not isinstance(exif_value, str):
        return None

    # EXIF format: "2024:01:15 14:30:00"
    try:
        return datetime.strptime(exif_value.strip(), '%Y:%m:%d %H:%M:%S')
    except ValueError:
        pass

    # Some cameras use different formats, try alternatives
    alternative_formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S',
        '%Y:%m:%d %H:%M',
        '%Y-%m-%d %H:%M',
    ]
    for fmt in alternative_formats:
        try:
            return datetime.strptime(exif_value.strip(), fmt)
        except ValueError:
            continue

    return None


def extract_exif_timestamp(path: Path | str) -> datetime | None:
    """Extract timestamp from image EXIF data.

    Tries DateTimeOriginal first (when photo was taken), then DateTime
    (when file was last modified by software).

    Args:
        path: Path to the image file.

    Returns:
        datetime object if EXIF timestamp found, None otherwise.
    """
    path = Path(path)

    try:
        with Image.open(path) as img:
            exif_data = img._getexif()
            if exif_data is None:
                return None

            # Build tag name to value mapping
            exif_dict: dict[str, Any] = {}
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                exif_dict[tag_name] = value

            # Try DateTimeOriginal first (when photo was actually taken)
            if 'DateTimeOriginal' in exif_dict:
                result = _parse_exif_datetime(exif_dict['DateTimeOriginal'])
                if result:
                    return result

            # Fall back to DateTime (when file was modified)
            if 'DateTime' in exif_dict:
                result = _parse_exif_datetime(exif_dict['DateTime'])
                if result:
                    return result

    except (OSError, AttributeError, KeyError) as e:
        logger.debug(f'Failed to extract EXIF from {path}: {e}')

    return None


def extract_filesystem_timestamp(path: Path | str) -> datetime | None:
    """Extract timestamp from filesystem metadata.

    Prefers creation time (Windows) or birth time (Unix if available),
    falls back to modification time.

    Args:
        path: Path to the file.

    Returns:
        datetime object from filesystem metadata, or None if file doesn't exist.
    """
    path = Path(path)

    if not path.exists():
        return None

    try:
        stat_result = path.stat()

        # Try creation time first (Windows st_ctime, or st_birthtime on some Unix)
        # On Windows, st_ctime is creation time
        # On Unix, st_ctime is metadata change time, not creation time
        if os.name == 'nt':
            # Windows: st_ctime is creation time
            creation_time = stat_result.st_ctime
        else:
            # Unix: try st_birthtime if available (macOS, some BSDs)
            creation_time = getattr(stat_result, 'st_birthtime', None)

        if creation_time:
            return datetime.fromtimestamp(creation_time)

        # Fall back to modification time
        return datetime.fromtimestamp(stat_result.st_mtime)

    except OSError as e:
        logger.debug(f'Failed to get filesystem timestamp for {path}: {e}')
        return None


def _parse_date_from_string(text: str) -> tuple[int, int, int, int] | None:
    """Parse a date from a string, returning (year, month, day, position).

    Tries multiple patterns in order of specificity. For partial dates where
    month or day cannot be determined, defaults to January 1st for missing parts.

    Pattern priority:
    1. YYYYMMDD (8 digits)
    2. YYYY-MM-DD or YY-MM-DD (separated)
    3. YYMMDD (6 digits)
    4. YYYY-MM (year-month, day defaults to 1)
    5. YYYY (year only, month and day default to January 1st)

    Args:
        text: String to search for date patterns.

    Returns:
        Tuple of (year, month, day, end_position) if found, None otherwise.
        The end_position indicates where the date pattern ends in the string.
    """
    # Try 8-digit pattern first: YYYYMMDD
    for match in _PATTERN_DATE_8DIGITS.finditer(text):
        digits = match.group(1)
        year = int(digits[0:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try separated pattern: YYYY-MM-DD or YY-MM-DD
    for match in _PATTERN_DATE_SEPARATED.finditer(text):
        year_str, month_str, day_str = match.groups()
        year = int(year_str)
        month = int(month_str)
        day = int(day_str)

        # Handle 2-digit year
        if year < 100:
            year = 1900 + year if year > 50 else 2000 + year

        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try 6-digit pattern: YYMMDD (must avoid matching time patterns)
    # Only use this if no 8-digit pattern found
    for match in _PATTERN_DATE_6DIGITS.finditer(text):
        digits = match.group(1)
        year = int(digits[0:2])
        month = int(digits[2:4])
        day = int(digits[4:6])

        # Handle 2-digit year
        year = 1900 + year if year > 50 else 2000 + year

        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try partial date patterns - default missing parts to January 1st
    # Year-month with separator: YYYY-MM
    for match in _PATTERN_DATE_YEAR_MONTH_SEP.finditer(text):
        year = int(match.group(1))
        month = int(match.group(2))
        if 1900 <= year <= 2099 and 1 <= month <= 12:
            return (year, month, 1, match.end())

    # Year-month without separator: YYYYMM (e.g., 202401)
    for match in _PATTERN_DATE_YEAR_MONTH.finditer(text):
        year = int(match.group(1))
        month = int(match.group(2))
        if 1900 <= year <= 2099 and 1 <= month <= 12:
            return (year, month, 1, match.end())

    # Year only: standalone 4-digit year (e.g., folder "2014" or "Photos 2014")
    for match in _PATTERN_DATE_YEAR_ONLY.finditer(text):
        year = int(match.group(1))
        if 1900 <= year <= 2099:
            return (year, 1, 1, match.end())

    return None


def _parse_time_from_string(text: str, start_pos: int = 0) -> tuple[int, int, int] | None:
    """Parse a time from a string, searching from a given position.

    Args:
        text: String to search for time patterns.
        start_pos: Position in string to start searching from.

    Returns:
        Tuple of (hour, minute, second) if found, None otherwise.
    """
    search_text = text[start_pos:]

    # Try 6-digit pattern: HHMMSS
    for match in _PATTERN_TIME_6DIGITS.finditer(search_text):
        digits = match.group(1)
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = int(digits[4:6])
        if _validate_time(hour, minute, second):
            return (hour, minute, second)

    # Try separated pattern: HH:MM:SS or HH:MM
    for match in _PATTERN_TIME_SEPARATED.finditer(search_text):
        hour_str, minute_str, second_str = match.groups()
        hour = int(hour_str)
        minute = int(minute_str)
        second = int(second_str) if second_str else 0
        if _validate_time(hour, minute, second):
            return (hour, minute, second)

    # Try 4-digit pattern: HHMM (less reliable, could be other numbers)
    for match in _PATTERN_TIME_4DIGITS.finditer(search_text):
        digits = match.group(1)
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        if _validate_time(hour, minute, 0):
            return (hour, minute, 0)

    return None


def parse_timestamp_from_path(path: Path | str) -> datetime | None:
    """Parse timestamp from filename or path components.

    Searches the full path string for date patterns, optionally followed
    by time patterns.

    Args:
        path: File path to parse.

    Returns:
        datetime object if a valid date pattern is found, None otherwise.
    """
    path = Path(path)
    # Use the full path string for searching (includes directory names)
    path_str = str(path)

    # Parse date
    date_result = _parse_date_from_string(path_str)
    if date_result is None:
        return None

    year, month, day, date_end_pos = date_result

    # Try to parse time after the date
    time_result = _parse_time_from_string(path_str, date_end_pos)
    if time_result:
        hour, minute, second = time_result
    else:
        hour, minute, second = 0, 0, 0

    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError as e:
        # Invalid date (e.g., Feb 30)
        logger.debug(f'Invalid date from path {path}: {e}')
        return None


def derive_timestamp(path: Path | str) -> datetime | None:
    """Derive the best timestamp for an image using multiple sources.

    Tries sources in priority order:
    1. EXIF DateTimeOriginal tag
    2. EXIF DateTime tag
    3. Parsed from filename/path (more reliable than filesystem dates)
    4. Filesystem creation time
    5. Filesystem modification time

    Args:
        path: Path to the image file.

    Returns:
        datetime object from the highest-priority available source,
        or None if no timestamp could be determined.
    """
    path = Path(path)

    # Try EXIF first (handles both DateTimeOriginal and DateTime internally)
    timestamp = extract_exif_timestamp(path)
    if timestamp:
        logger.debug(f'Timestamp from EXIF: {timestamp} for {path}')
        return timestamp

    # Try parsing from filename/path (before filesystem, as files get copied around)
    timestamp = parse_timestamp_from_path(path)
    if timestamp:
        logger.debug(f'Timestamp from filename: {timestamp} for {path}')
        return timestamp

    # Try filesystem timestamp as last resort
    timestamp = extract_filesystem_timestamp(path)
    if timestamp:
        logger.debug(f'Timestamp from filesystem: {timestamp} for {path}')
        return timestamp

    logger.debug(f'No timestamp found for {path}')
    return None


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
    checksum              TEXT,
    perceptual_hash       TEXT,
    laplacian_var         REAL,
    lossless              INTEGER NOT NULL DEFAULT 0,
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
]

# SQL schema for the duplicate_groups table
_SQL_CREATE_DUPLICATE_GROUPS = """
CREATE TABLE IF NOT EXISTS duplicate_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       INTEGER NOT NULL,
    group_hash  TEXT NOT NULL,
    image_id    TEXT NOT NULL,
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

# Index definitions for performance
_SQL_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_images_path ON images(path)",
    "CREATE INDEX IF NOT EXISTS idx_images_checksum ON images(checksum)",
    "CREATE INDEX IF NOT EXISTS idx_images_perceptual_hash ON images(perceptual_hash)",
    "CREATE INDEX IF NOT EXISTS idx_images_deleted ON images(deleted)",
    "CREATE INDEX IF NOT EXISTS idx_images_timestamp ON images(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_dup_level_group ON duplicate_groups(level, group_hash)",
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

    # Use Row factory for dict-like access to rows
    conn.row_factory = sqlite3.Row

    # Create tables
    conn.execute(_SQL_CREATE_FOLDERS)
    conn.execute(_SQL_CREATE_IMAGES)
    conn.execute(_SQL_CREATE_DUPLICATE_GROUPS)
    conn.execute(_SQL_CREATE_MIGRATIONS)

    # Create indexes
    for index_sql in _SQL_CREATE_INDEXES:
        conn.execute(index_sql)

    # Run migrations for existing databases
    for migration_sql in _SQL_MIGRATIONS:
        try:
            conn.execute(migration_sql)
        except sqlite3.OperationalError:
            # Column/table already exists, ignore
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
    cursor = conn.execute("""
        SELECT
            f.path,
            COUNT(i.id) as count
        FROM folders f
        LEFT JOIN images i ON i.path LIKE f.path || '%' AND i.deleted = 0
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
        placeholders = ' AND '.join(['path NOT LIKE ? || \'%\''] * len(remaining_folders))
        conn.execute(
            f'UPDATE images SET deleted = 1, updated_at = ? WHERE {placeholders}',
            [datetime.now().isoformat()] + remaining_folders
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
                   checksum, perceptual_hash, laplacian_var, lossless,
                   description, rating, deleted, created_at, updated_at
            FROM images
            ORDER BY timestamp DESC, path ASC
        """)
    else:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   checksum, perceptual_hash, laplacian_var, lossless,
                   description, rating, deleted, created_at, updated_at
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
        id, basename, width, height, timestamp, rating, description.
    """
    cursor = conn.execute("""
        SELECT id, basename, width, height, timestamp, rating, description
        FROM images
        WHERE deleted = 0
        ORDER BY timestamp DESC, path ASC
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
        SELECT id, basename, width, height, timestamp, rating, description, deleted, updated_at
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
               checksum, perceptual_hash, laplacian_var, lossless,
               description, rating, deleted, created_at, updated_at
        FROM images
        WHERE id = ?
    """, (image_id,))

    return row_to_dict(cursor.fetchone())


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
               checksum, perceptual_hash, laplacian_var, lossless,
               description, rating, embedding, deleted, created_at, updated_at
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
            id, path, basename, size, width, height, timestamp,
            checksum, perceptual_hash, laplacian_var, lossless, mtime,
            description, rating, embedding, deleted, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
    """, (
        image_id, path_str, basename, size, width, height, timestamp_str,
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
        'checksum', 'perceptual_hash', 'laplacian_var', 'lossless',
        'embedding', 'description_embedding', 'deleted'
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
            checksum = ?,
            perceptual_hash = ?,
            laplacian_var = ?,
            lossless = ?,
            mtime = ?,
            embedding = NULL,
            updated_at = ?
        WHERE id = ?
    """, (
        size, width, height, timestamp_str, checksum,
        perceptual_hash, laplacian_var, int(lossless), mtime, now, image_id
    ))
    conn.commit()

    if cursor.rowcount > 0:
        logger.debug(f'Updated metadata for image: {image_id}')
        return True
    return False


def update_image_embedding(
    conn: sqlite3.Connection,
    image_id: str,
    embedding: bytes,
) -> bool:
    """Update the embedding for an image.

    Args:
        conn: Database connection.
        image_id: UUID of the image.
        embedding: OpenCLIP embedding as bytes (numpy.tobytes()).

    Returns:
        True if image was updated, False if not found.
    """
    now = datetime.now().isoformat()

    cursor = conn.execute(
        'UPDATE images SET embedding = ?, updated_at = ? WHERE id = ?',
        (embedding, now, image_id)
    )
    conn.commit()

    if cursor.rowcount > 0:
        logger.debug(f'Updated embedding for image: {image_id}')
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

    if include_deleted:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   checksum, perceptual_hash, laplacian_var, lossless,
                   description, rating, deleted, created_at, updated_at
            FROM images
            WHERE path LIKE ? || '%'
            ORDER BY path ASC
        """, (folder_str,))
    else:
        cursor = conn.execute("""
            SELECT id, path, basename, size, width, height, timestamp,
                   checksum, perceptual_hash, laplacian_var, lossless,
                   description, rating, deleted, created_at, updated_at
            FROM images
            WHERE path LIKE ? || '%' AND deleted = 0
            ORDER BY path ASC
        """, (folder_str,))

    return rows_to_dicts(cursor.fetchall())


# =============================================================================
# METADATA EXTRACTION
# =============================================================================

# Lossless image formats (by extension)
LOSSLESS_EXTENSIONS = {'.png', '.bmp', '.tiff', '.tif', '.gif'}


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


def compute_perceptual_hash(path: Path | str) -> str | None:
    """Compute a perceptual hash of an image.

    Uses the pHash algorithm which is robust to minor changes like
    resizing, compression, and colour adjustments.

    Args:
        path: Path to the image file.

    Returns:
        Hex string representation of the perceptual hash,
        or None if the image cannot be processed.
    """
    try:
        with Image.open(path) as img:
            phash = imagehash.phash(img)
            return str(phash)
    except Exception as e:
        logger.warning(f'Failed to compute perceptual hash for {path}: {e}')
        return None


def compute_laplacian_variance(path: Path | str) -> float | None:
    """Compute the Laplacian variance of an image as a focus/sharpness metric.

    Higher values indicate sharper images. This metric is useful for
    detecting blurry or out-of-focus images.

    Args:
        path: Path to the image file.

    Returns:
        Variance of the Laplacian, or None if image cannot be processed.
    """
    try:
        img = cv2.imread(str(path))
        if img is None:
            logger.warning(f'OpenCV failed to read image: {path}')
            return None

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
    checksum: str
    perceptual_hash: str | None
    laplacian_var: float | None
    lossless: bool


def extract_image_metadata(path: Path | str) -> ImageMetadata | None:
    """Extract all metadata from an image file.

    Args:
        path: Path to the image file.

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
    perceptual_hash = compute_perceptual_hash(path)

    # Compute Laplacian variance (may fail for some images)
    laplacian_var = compute_laplacian_variance(path)

    # Derive timestamp
    timestamp = derive_timestamp(path)

    # Check if lossless format
    lossless = is_lossless_format(path)

    return ImageMetadata(
        path=path,
        size=size,
        mtime=mtime,
        width=width,
        height=height,
        timestamp=timestamp,
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
        pause_event: threading.Event | None = None,
        num_threads: int = 4,
    ):
        """Initialise the ingestion thread.

        Args:
            conn: Database connection (must be created with check_same_thread=False).
            ingestion_queue: Queue of file paths to process.
            embedding_queue: Queue to add image IDs that need embeddings.
            stop_event: Event to signal thread should stop.
            pause_event: Optional event to pause processing (for folder removal).
            num_threads: Number of worker threads for parallel metadata extraction.
        """
        super().__init__(name='IngestionThread', daemon=True)
        self.conn = conn
        self.ingestion_queue = ingestion_queue
        self.embedding_queue = embedding_queue
        self.stop_event = stop_event
        self.pause_event = pause_event or threading.Event()
        self.num_threads = max(1, min(16, num_threads))
        self._processed_count = 0
        self._error_count = 0
        self._db_lock = threading.Lock()

    @property
    def processed_count(self) -> int:
        """Number of images successfully processed."""
        return self._processed_count

    @property
    def error_count(self) -> int:
        """Number of images that failed processing."""
        return self._error_count

    def run(self) -> None:
        """Main thread loop - process images using thread pool."""
        logger.info(f'Ingestion thread started with {self.num_threads} worker threads')

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
                logger.debug(f'Backfilling mtime for: {path}')
                with self._db_lock:
                    self.conn.execute(
                        'UPDATE images SET mtime = ?, updated_at = ? WHERE id = ?',
                        (current_mtime, datetime.now().isoformat(), existing['id'])
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
                    metadata = extract_image_metadata(path)
                    if metadata is not None:
                        with self._db_lock:
                            update_image_metadata(
                                self.conn,
                                existing['id'],
                                size=metadata.size,
                                width=metadata.width,
                                height=metadata.height,
                                timestamp=metadata.timestamp,
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
            metadata = extract_image_metadata(path)
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
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                    mtime=metadata.mtime,
                )

            # Queue for embedding (metadata cleared embedding)
            self.embedding_queue.put(existing['id'])
            logger.debug(f'Queued changed image for embedding: {path}')

        else:
            # New image - extract metadata (no lock - file I/O)
            metadata = extract_image_metadata(path)
            if metadata is None:
                logger.warning(f'Failed to extract metadata for new image: {path}')
                return

            # Generate new UUID
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
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                    mtime=metadata.mtime,
                )

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

    def __init__(self, model_name: str = 'ViT-B-32', pretrained: str = 'openai'):
        """Initialise the model wrapper.

        Args:
            model_name: OpenCLIP model architecture (e.g., 'ViT-B-32').
            pretrained: Pretrained weights (e.g., 'openai', 'laion2b_s34b_b79k').
        """
        self.model_name = model_name
        self.pretrained = pretrained
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'

        self._model = None
        self._preprocess = None
        self._tokenizer = None

    def _load_model(self) -> None:
        """Load the model (called on first use)."""
        if self._model is not None:
            return

        logger.info('=' * 60)
        logger.info(f'Loading OpenCLIP model: {self.model_name} ({self.pretrained})')
        logger.info(f'Device: {self.device}')
        logger.info('-' * 60)
        logger.info('If this is the first run, the model weights will be downloaded.')
        logger.info('This may take several minutes depending on your connection...')
        logger.info('-' * 60)

        start_time = time.time()

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
            # Load and preprocess image
            img = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
            x = self.preprocess(img).unsqueeze(0).to(self.device)

            # Encode with inference mode
            with torch.inference_mode():
                if self.device == 'cuda':
                    with torch.cuda.amp.autocast():
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
            try:
                img = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
                x = self.preprocess(img)
                tensors.append(x)
                valid_indices.append(i)
            except Exception as e:
                logger.warning(f'Failed to load image for embedding {path}: {e}')
                results.append((i, None))

        if not tensors:
            return results

        # Stack into batch
        batch = torch.stack(tensors).to(self.device)

        # Encode batch
        try:
            with torch.inference_mode():
                if self.device == 'cuda':
                    with torch.cuda.amp.autocast():
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
                with torch.cuda.amp.autocast():
                    v = self.model.encode_text(tokens)
            else:
                v = self.model.encode_text(tokens)

            # Normalise
            v = v / v.norm(dim=-1, keepdim=True)

        return v.cpu().numpy().flatten()


class EmbeddingThread(threading.Thread):
    """Background thread for computing image embeddings.

    Processes image IDs from the embedding queue in batches, computes
    OpenCLIP embeddings, and stores them in the database.

    Attributes:
        conn: Database connection.
        embedding_queue: Queue of image IDs to process.
        ingestion_queue: Reference to ingestion queue (to check if empty).
        stop_event: Event to signal thread shutdown.
        config: Configuration object.
        clip_model: OpenCLIP model wrapper.
        on_complete: Optional callback when all processing is done.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        embedding_queue: queue.Queue[str],
        ingestion_queue: queue.Queue[Path],
        stop_event: threading.Event,
        config: Config | None = None,
        on_complete: callable | None = None,
    ):
        """Initialise the embedding thread.

        Args:
            conn: Database connection (must be created with check_same_thread=False).
            embedding_queue: Queue of image IDs to process.
            ingestion_queue: Reference to ingestion queue (to check if empty).
            stop_event: Event to signal thread should stop.
            config: Configuration object. Uses defaults if None.
            on_complete: Optional callback function called when both queues
                are empty. Used to trigger duplicate group computation.
        """
        super().__init__(name='EmbeddingThread', daemon=True)
        self.conn = conn
        self.embedding_queue = embedding_queue
        self.ingestion_queue = ingestion_queue
        self.stop_event = stop_event
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
        logger.info('Embedding thread started')

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
                        logger.warning(f'Image not found for embedding: {image_id}')
                        self.embedding_queue.task_done()
                        continue

                    path = Path(image['path'])
                    if not path.exists():
                        logger.warning(f'Image file not found for embedding: {path}')
                        self.embedding_queue.task_done()
                        continue

                    batch_ids.append(image_id)
                    batch_paths.append(path)

                # Process batch if we have any
                if batch_ids:
                    self._process_batch(batch_ids, batch_paths)
                    self._completion_triggered = False  # Reset completion flag
                else:
                    # Queue is empty - check if we should trigger completion
                    self._check_completion()

            except Exception as e:
                logger.error(f'Unexpected error in embedding thread: {e}')

        logger.info('Embedding thread stopped')

    def _process_batch(self, image_ids: list[str], paths: list[Path]) -> None:
        """Process a batch of images.

        Args:
            image_ids: List of image IDs.
            paths: List of corresponding file paths.
        """
        logger.debug(f'Processing embedding batch of {len(paths)} images')

        # Encode batch
        results = self.clip_model.encode_images_batch(paths)

        # Store results
        for (idx, embedding), image_id in zip(results, image_ids):
            try:
                if embedding is not None:
                    # Convert to bytes for storage
                    embedding_bytes = embedding.astype(np.float32).tobytes()
                    update_image_embedding(self.conn, image_id, embedding_bytes)
                    self._processed_count += 1
                else:
                    self._error_count += 1
            except Exception as e:
                logger.error(f'Failed to store embedding for {image_id}: {e}')
                self._error_count += 1
            finally:
                self.embedding_queue.task_done()

    def _check_completion(self) -> None:
        """Check if both queues are empty and trigger completion callback."""
        if self._completion_triggered:
            return

        # Check if both queues are empty
        if self.ingestion_queue.empty() and self.embedding_queue.empty():
            self._completion_triggered = True
            logger.info('Both queues empty - processing complete')

            if self.on_complete:
                try:
                    self.on_complete()
                except Exception as e:
                    logger.error(f'Error in completion callback: {e}')


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


# =============================================================================
# DUPLICATE GROUP COMPUTATION
# =============================================================================

def hamming_distance(hash1: str, hash2: str) -> int:
    """Compute Hamming distance between two perceptual hash hex strings.

    Args:
        hash1: First hash as hex string.
        hash2: Second hash as hex string.

    Returns:
        Number of differing bits between the hashes.
    """
    # Convert hex strings to integers
    int1 = int(hash1, 16)
    int2 = int(hash2, 16)

    # XOR and count bits
    xor = int1 ^ int2
    return bin(xor).count('1')


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
    for image_id in image_ids:
        conn.execute(
            'INSERT INTO duplicate_groups (level, group_hash, image_id) VALUES (?, ?, ?)',
            (level, group_hash, image_id)
        )


def compute_duplicates_level0(conn: sqlite3.Connection) -> int:
    """Compute level 0 duplicates (identical checksum).

    Groups images with the same SHA256 checksum.

    Args:
        conn: Database connection.

    Returns:
        Number of duplicate groups found.
    """
    logger.info('Computing level 0 duplicates (identical checksum)')

    # Clear existing level 0 groups
    _clear_duplicate_groups(conn, level=0)

    # Find checksums with multiple images
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


def compute_duplicates_level1(conn: sqlite3.Connection, threshold: int = 4) -> int:
    """Compute level 1 duplicates (perceptual hash similarity).

    Groups images with perceptual hash Hamming distance <= threshold.

    Args:
        conn: Database connection.
        threshold: Maximum Hamming distance to consider as duplicate.

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 1 duplicates (perceptual hash, threshold={threshold})')

    # Clear existing level 1 groups
    _clear_duplicate_groups(conn, level=1)

    # Get all images with perceptual hashes
    cursor = conn.execute("""
        SELECT id, perceptual_hash
        FROM images
        WHERE deleted = 0 AND perceptual_hash IS NOT NULL
    """)
    images = cursor.fetchall()

    if len(images) < 2:
        logger.info('Not enough images for perceptual duplicate detection')
        return 0

    # Build list of (id, hash) tuples
    image_data = [(row['id'], row['perceptual_hash']) for row in images]

    # Use union-find to group similar images
    parent = {img_id: img_id for img_id, _ in image_data}

    def find(x: str) -> str:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: str, y: str) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Compare all pairs (O(n²) but necessary for clustering)
    for i in range(len(image_data)):
        for j in range(i + 1, len(image_data)):
            id1, hash1 = image_data[i]
            id2, hash2 = image_data[j]

            if hamming_distance(hash1, hash2) <= threshold:
                union(id1, id2)

    # Build groups from union-find
    groups: dict[str, list[str]] = {}
    for img_id, _ in image_data:
        root = find(img_id)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)

    # Insert groups with more than one member
    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'phash_{root}'
            _insert_duplicate_group(conn, level=1, group_hash=group_hash, image_ids=members)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 1 duplicate groups')
    return group_count


def compute_duplicates_level2(conn: sqlite3.Connection, threshold: float = 0.95) -> int:
    """Compute level 2 duplicates (similar embeddings).

    Groups images with cosine similarity >= threshold.

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity to consider as similar.

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 2 duplicates (embedding similarity >= {threshold})')

    # Clear existing level 2 groups
    _clear_duplicate_groups(conn, level=2)

    # Get all images with embeddings
    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if len(rows) < 2:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    # Convert to numpy arrays
    image_ids = [row['id'] for row in rows]
    embeddings = np.array([embedding_to_numpy(row['embedding']) for row in rows])

    # Compute pairwise cosine similarities (dot product of normalised vectors)
    # similarities[i,j] = similarity between image i and j
    similarities = embeddings @ embeddings.T

    # Use union-find to cluster
    parent = {i: i for i in range(len(image_ids))}

    def find(x: int) -> int:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Find pairs above threshold
    for i in range(len(image_ids)):
        for j in range(i + 1, len(image_ids)):
            if similarities[i, j] >= threshold:
                union(i, j)

    # Build groups
    groups: dict[int, list[str]] = {}
    for i, img_id in enumerate(image_ids):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)

    # Insert groups with more than one member
    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'emb2_{image_ids[root]}'
            _insert_duplicate_group(conn, level=2, group_hash=group_hash, image_ids=members)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 2 duplicate groups')
    return group_count


def compute_duplicates_level3(conn: sqlite3.Connection, threshold: float = 0.85) -> int:
    """Compute level 3 duplicates (related embeddings).

    Groups images with cosine similarity >= threshold (lower than level 2).

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity to consider as related.

    Returns:
        Number of duplicate groups found.
    """
    logger.info(f'Computing level 3 duplicates (embedding similarity >= {threshold})')

    # Clear existing level 3 groups
    _clear_duplicate_groups(conn, level=3)

    # Get all images with embeddings
    cursor = conn.execute("""
        SELECT id, embedding
        FROM images
        WHERE deleted = 0 AND embedding IS NOT NULL
    """)
    rows = cursor.fetchall()

    if len(rows) < 2:
        logger.info('Not enough images with embeddings for similarity detection')
        return 0

    # Convert to numpy arrays
    image_ids = [row['id'] for row in rows]
    embeddings = np.array([embedding_to_numpy(row['embedding']) for row in rows])

    # Compute pairwise cosine similarities
    similarities = embeddings @ embeddings.T

    # Use union-find to cluster
    parent = {i: i for i in range(len(image_ids))}

    def find(x: int) -> int:
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x: int, y: int) -> None:
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    # Find pairs above threshold
    for i in range(len(image_ids)):
        for j in range(i + 1, len(image_ids)):
            if similarities[i, j] >= threshold:
                union(i, j)

    # Build groups
    groups: dict[int, list[str]] = {}
    for i, img_id in enumerate(image_ids):
        root = find(i)
        if root not in groups:
            groups[root] = []
        groups[root].append(img_id)

    # Insert groups with more than one member
    group_count = 0
    for root, members in groups.items():
        if len(members) > 1:
            group_hash = f'emb3_{image_ids[root]}'
            _insert_duplicate_group(conn, level=3, group_hash=group_hash, image_ids=members)
            group_count += 1

    conn.commit()
    logger.info(f'Found {group_count} level 3 duplicate groups')
    return group_count


def compute_all_duplicate_groups(conn: sqlite3.Connection, config: Config | None = None) -> dict[int, int]:
    """Compute duplicate groups at all levels.

    Args:
        conn: Database connection.
        config: Configuration object for thresholds. Uses defaults if None.

    Returns:
        Dictionary mapping level to number of groups found.
    """
    if config is None:
        config = get_default_config()

    logger.info('Computing all duplicate groups')

    results = {
        0: compute_duplicates_level0(conn),
        1: compute_duplicates_level1(conn, threshold=config.perceptual_hash_threshold),
        2: compute_duplicates_level2(conn, threshold=config.similarity_threshold_level2),
        3: compute_duplicates_level3(conn, threshold=config.similarity_threshold_level3),
    }

    total = sum(results.values())
    logger.info(f'Duplicate computation complete: {total} total groups')
    return results


def get_duplicate_groups(conn: sqlite3.Connection, level: int) -> list[dict[str, Any]]:
    """Get duplicate groups at a specific level.

    Args:
        conn: Database connection.
        level: Duplicate level (0-3).

    Returns:
        List of group dictionaries, each containing:
            - group_hash: Unique group identifier
            - images: List of image dictionaries in the group
    """
    # Get all groups at this level
    cursor = conn.execute("""
        SELECT DISTINCT group_hash
        FROM duplicate_groups
        WHERE level = ?
    """, (level,))
    group_hashes = [row['group_hash'] for row in cursor.fetchall()]

    groups = []
    for group_hash in group_hashes:
        # Get images in this group
        cursor = conn.execute("""
            SELECT i.id, i.path, i.basename, i.size, i.width, i.height,
                   i.timestamp, i.checksum, i.perceptual_hash, i.laplacian_var,
                   i.lossless, i.description, i.rating
            FROM images i
            JOIN duplicate_groups dg ON i.id = dg.image_id
            WHERE dg.level = ? AND dg.group_hash = ? AND i.deleted = 0
            ORDER BY i.size DESC, i.path ASC
        """, (level, group_hash))

        images = rows_to_dicts(cursor.fetchall())

        if len(images) > 1:  # Only include groups with multiple images
            groups.append({
                'group_hash': group_hash,
                'images': images,
            })

    return groups


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
    similarity. Description embeddings provide a weighted boost but don't
    dominate the score, since text-to-text similarity in CLIP tends to be
    higher than text-to-image similarity.

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
    # Weight for description embedding score (lower to avoid text-to-text bias)
    DESC_WEIGHT = 0.5

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
    n = len(rows)
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
    desc_scores = desc_matrix @ query_embedding * DESC_WEIGHT  # Shape: (n,)

    # Zero out scores for missing embeddings
    img_scores = np.where(has_img, img_scores, 0.0)
    desc_scores = np.where(has_desc, desc_scores, 0.0)

    # Take max of the two scores
    scores = np.maximum(img_scores, desc_scores)

    # Step 4: Filter by threshold and get top results
    above_threshold = scores >= threshold
    if not np.any(above_threshold):
        return []

    # Get indices of results above threshold, sorted by score descending
    valid_indices = np.where(above_threshold)[0]
    valid_scores = scores[valid_indices]
    sorted_order = np.argsort(-valid_scores)  # Descending
    top_indices = valid_indices[sorted_order[:limit]]
    top_scores = valid_scores[sorted_order[:limit]]

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
               checksum, perceptual_hash, laplacian_var, lossless,
               description, rating
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
               checksum, perceptual_hash, laplacian_var, lossless,
               description, rating
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
) -> Path | None:
    """Get a thumbnail for an image, generating if necessary.

    Args:
        conn: Database connection.
        image_id: UUID of the image.
        size: Thumbnail size in pixels (clamped to 50-800).
        thumbnail_dir: Root thumbnail cache directory.
        quality: JPEG quality for generated thumbnails.

    Returns:
        Path to the thumbnail file, or None if image not found or
        thumbnail cannot be generated.
    """
    # Clamp size to valid range
    size = max(50, min(800, size))

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

    if generate_thumbnail(source_path, cache_path, size, quality):
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
    """Thread-safe event queue for SSE broadcasting.

    Supports multiple subscribers (SSE connections). Each subscriber
    gets their own queue of events.

    Attributes:
        subscribers: Set of subscriber queues.
    """

    def __init__(self):
        """Initialise the event queue."""
        self._subscribers: list[queue.Queue[Event]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[Event]:
        """Create a new subscriber queue.

        Returns:
            Queue that will receive all future events.
        """
        subscriber_queue: queue.Queue[Event] = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber_queue)
        logger.debug(f'New SSE subscriber (total: {len(self._subscribers)})')
        return subscriber_queue

    def unsubscribe(self, subscriber_queue: queue.Queue[Event]) -> None:
        """Remove a subscriber queue.

        Args:
            subscriber_queue: Queue to remove.
        """
        with self._lock:
            if subscriber_queue in self._subscribers:
                self._subscribers.remove(subscriber_queue)
        logger.debug(f'SSE subscriber removed (total: {len(self._subscribers)})')

    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers.

        Args:
            event: Event to broadcast.
        """
        with self._lock:
            for subscriber_queue in self._subscribers:
                try:
                    subscriber_queue.put_nowait(event)
                except queue.Full:
                    logger.warning('Subscriber queue full, event dropped')

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        """Create and publish an event.

        Args:
            event_type: Type of event.
            data: Event payload (default empty dict).
        """
        event = Event(event_type=event_type, data=data or {})
        self.publish(event)
        logger.debug(f'Event emitted: {event_type}')

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        with self._lock:
            return len(self._subscribers)


def create_sse_generator(
    event_queue: EventQueue,
    timeout: float = 30.0,
) -> Iterator[str]:
    """Create a generator for SSE streaming.

    This generator yields SSE-formatted strings and should be used
    as the response body for an SSE endpoint.

    Args:
        event_queue: EventQueue instance to subscribe to.
        timeout: Timeout in seconds for waiting on events. A keepalive
            comment is sent if no events arrive within this time.

    Yields:
        SSE-formatted strings (events or keepalive comments).
    """
    subscriber_queue = event_queue.subscribe()

    try:
        # Send initial connection message
        yield ': connected\n\n'

        while True:
            try:
                event = subscriber_queue.get(timeout=timeout)
                yield event.to_sse()
            except queue.Empty:
                # Send keepalive comment to prevent connection timeout
                yield ': keepalive\n\n'
    except GeneratorExit:
        # Client disconnected
        pass
    finally:
        event_queue.unsubscribe(subscriber_queue)


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
        """
        self._preload_model = preload_model
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

        # Thread references (created when started)
        self._ingestion_thread: IngestionThread | None = None
        self._embedding_thread: EmbeddingThread | None = None

        # Track if we've been closed
        self._closed = False

        # Ensure thumbnail directory exists
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)

        if auto_start:
            self.startup()

    def startup(self) -> None:
        """Run the startup sequence.

        Steps 3-7 of the startup sequence. Call this if auto_start=False
        was passed to __init__.
        """
        # Step 3: Verify registered folders exist
        logger.info('[3/5] Verifying registered folders...')
        missing_folders = verify_folders_exist(self.conn)
        for folder in missing_folders:
            logger.warning(f'        Folder missing: {folder}')

        # Step 4: Rescan all registered directories
        logger.info('[4/5] Scanning registered folders...')
        self._rescan_all_folders()

        # Step 5: Queue images with missing embeddings
        logger.info('[5/5] Starting background threads...')
        missing_embeddings = get_images_without_embedding(self.conn)
        for image in missing_embeddings:
            self._embedding_queue.put(image['id'])
        if missing_embeddings:
            logger.info(f'        {len(missing_embeddings)} images queued for embedding')

        # Steps 6-7: Start background threads
        self.start_threads()

        # Optionally pre-load OpenCLIP model to show download progress during startup
        if self._preload_model and self._embedding_thread is not None:
            logger.info('Pre-loading OpenCLIP model...')
            # Access the clip_model property to trigger loading
            _ = self._embedding_thread.clip_model

        # Run one-time migrations
        self._migrate_recalculate_timestamps()

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
            for path in missing_paths:
                self.conn.execute(
                    'UPDATE images SET deleted = 1, updated_at = ? WHERE path = ? AND deleted = 0',
                    (now, path)
                )
            self.conn.commit()

        logger.info(f'        Found {len(found_paths)} images')

    def start_threads(self) -> None:
        """Start the background processing threads."""
        if self._ingestion_thread is not None and self._ingestion_thread.is_alive():
            logger.warning('Threads already running')
            return

        self._stop_event.clear()

        # Create completion callback
        def on_complete():
            compute_all_duplicate_groups(self.conn, self.config)
            emit_processing_complete(self.event_queue)

        # Start ingestion thread with configured number of worker threads
        self._ingestion_thread = IngestionThread(
            conn=self.conn,
            ingestion_queue=self._ingestion_queue,
            embedding_queue=self._embedding_queue,
            stop_event=self._stop_event,
            pause_event=self._pause_event,
            num_threads=self.config.indexing_threads,
        )
        self._ingestion_thread.start()

        # Start embedding thread
        self._embedding_thread = EmbeddingThread(
            conn=self.conn,
            embedding_queue=self._embedding_queue,
            ingestion_queue=self._ingestion_queue,
            stop_event=self._stop_event,
            config=self.config,
            on_complete=on_complete,
        )
        self._embedding_thread.start()

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
                logger.warning('Embedding thread did not stop in time')

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
            result = remove_folder(self.conn, path)
            if result:
                emit_folder_removed(self.event_queue, path)
            return result
        finally:
            self._pause_event.clear()

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

    def get_images_delta(self, since: str) -> dict[str, Any]:
        """Get image changes since a given timestamp."""
        return get_images_delta(self.conn, since)

    def get_current_epoch(self) -> str | None:
        """Get the current epoch (max updated_at timestamp)."""
        return get_current_epoch(self.conn)

    def get_image(self, image_id: str) -> dict[str, Any] | None:
        """Get a single image by ID."""
        return get_image(self.conn, image_id)

    def update_image(self, image_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update image metadata (description, rating).

        If description is updated to a non-empty value, computes and stores
        the description embedding for semantic search.
        """
        # Check if description is being updated
        if 'description' in data:
            description = data['description'].strip() if data['description'] else ''
            if description:
                # Compute embedding for the new description
                try:
                    embedding = self._get_clip_model().encode_text(description)
                    data['description_embedding'] = embedding.astype(np.float32).tobytes()
                except Exception as e:
                    logger.warning(f'Failed to compute description embedding: {e}')
            else:
                # Clear description embedding if description is empty
                data['description_embedding'] = None

        return update_image(self.conn, image_id, data)

    def delete_image(self, image_id: str, from_disk: bool = False) -> bool:
        """Delete an image (soft delete or from disk)."""
        return delete_image(self.conn, image_id, from_disk)

    def rotate_images(
        self,
        image_ids: list[str],
        direction: str,
    ) -> dict[str, Any]:
        """Rotate multiple image files in parallel.

        Uses a thread pool for parallel processing, controlled by the
        indexing_threads config option.

        Args:
            image_ids: List of image UUIDs to rotate.
            direction: 'cw' for clockwise, 'ccw' for counter-clockwise.

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

            # Use thread pool for parallel rotation
            max_workers = self.config.indexing_threads

            def rotate_one(image_id: str) -> tuple[str, bool]:
                success = self._rotate_single_image(image_id, direction)
                return (image_id, success)

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(rotate_one, img_id) for img_id in image_ids]
                for future in futures:
                    try:
                        image_id, success = future.result()
                        results[image_id] = success
                        if success:
                            rotated.append(image_id)
                    except Exception as e:
                        logger.error(f'rotate_images: Thread error: {e}')

            return {'results': results, 'rotated': rotated}

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

    def _rotate_single_image(self, image_id: str, direction: str) -> bool:
        """Rotate a single image file and update its metadata.

        Performs lossless rotation for JPEG files when possible.
        Updates the database with new checksum, size, and mtime.
        Deletes old cached thumbnails.

        Uses a per-image lock to prevent concurrent rotations of the same image.

        Args:
            image_id: UUID of the image to rotate.
            direction: 'cw' for clockwise, 'ccw' for counter-clockwise.

        Returns:
            True if rotation succeeded, False otherwise.
        """
        # Get per-image lock to prevent concurrent rotations of the same image
        image_lock = self._get_image_lock(image_id)

        with image_lock:
            # Get current image info (inside lock to ensure consistent read)
            image = get_image(self.conn, image_id)
            if image is None:
                logger.warning(f'rotate_image: Image not found: {image_id}')
                return False

            path = Path(image['path'])
            if not path.exists():
                logger.error(f'rotate_image: File not found: {path}')
                return False

            old_checksum = image.get('checksum')

            # Rotate the image file
            if not rotate_image_file(path, direction):
                logger.error(f'rotate_image: Rotation failed for: {path}')
                return False

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
                return False

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
            except Exception as e:
                logger.error(f'rotate_image: Failed to update database: {e}')
                return False

            logger.info(f'Rotated image {direction}: {path.name}')
            return True

    def search_images(
        self,
        query: str,
        threshold: float = 0.2,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Search for images using semantic similarity.

        Encodes the query text using OpenCLIP and finds images with similar
        embeddings (both image content and descriptions).

        Args:
            query: Text query to search for.
            threshold: Minimum similarity score (0.0 to 1.0). Defaults to 0.2.
            limit: Maximum number of results. Defaults to 100.

        Returns:
            List of matching images with 'score' field, sorted by similarity.
        """
        # Encode the query text
        query_embedding = self._get_clip_model().encode_text(query)

        # Perform semantic search
        return semantic_search(self.conn, query_embedding, threshold, limit)

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
            logger.warning(f'get_similar_images: Image is deleted')
            return None

        if row['embedding'] is None:
            logger.warning(f'get_similar_images: Embedding is None')
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
            )
        return self._clip_model_fallback

    # =========================================================================
    # Public API - Thumbnails
    # =========================================================================

    def get_thumbnail_path(self, image_id: str, size: int = 200) -> Path | None:
        """Get or create a thumbnail for an image."""
        return get_or_create_thumbnail(
            self.conn,
            image_id,
            size,
            self.thumbnail_dir,
            self.config.thumbnail_quality,
        )

    # =========================================================================
    # Public API - Duplicates
    # =========================================================================

    def get_duplicate_groups(self, level: int) -> list[dict[str, Any]]:
        """Get duplicate groups at a specific level."""
        return get_duplicate_groups(self.conn, level)

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

        return {
            'totalImages': total_images,
            'totalFolders': total_folders,
        }

    def get_processing_status(self) -> dict[str, Any]:
        """Get current processing status.

        Returns:
            Dict with status, indexing_queue, embedding_queue counts, and total_images.
        """
        indexing_count = self._ingestion_queue.qsize()
        embedding_count = self._embedding_queue.qsize()

        status = 'up_to_date' if (indexing_count == 0 and embedding_count == 0) else 'updating'

        # Get total image count for live updates during indexing
        cursor = self.conn.execute(
            'SELECT COUNT(*) as count FROM images WHERE deleted = 0'
        )
        total_images = cursor.fetchone()['count']

        return {
            'status': status,
            'indexing_queue': indexing_count,
            'embedding_queue': embedding_count,
            'total_images': total_images,
        }

    def queue_rescan_all(self) -> None:
        """Queue all registered folders for rescanning."""
        logger.info('Queueing rescan of all folders')
        self._rescan_all_folders()

    # =========================================================================
    # Public API - Events (SSE)
    # =========================================================================

    def get_event_stream(self, timeout: float = 30.0) -> Iterator[str]:
        """Get an SSE event stream generator.

        Args:
            timeout: Keepalive timeout in seconds.

        Returns:
            Generator yielding SSE-formatted strings.
        """
        return create_sse_generator(self.event_queue, timeout)


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


# =============================================================================
# STANDALONE TEST MODE
# =============================================================================

def _run_tests() -> None:
    """Run automated tests of all main functionality.

    Creates temporary test data, exercises all major features, and
    reports results. Cleans up after itself.
    """

    # Configure logging for test output
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    print('\n' + '=' * 70)
    print('IMAGINARY DATABASE - AUTOMATED TEST SUITE')
    print('=' * 70 + '\n')

    # Create temporary directory for test
    test_dir = Path(tempfile.mkdtemp(prefix='imaginary_test_'))
    db_path = test_dir / 'test.db'
    thumbnail_dir = test_dir / 'thumbnails'
    config_path = test_dir / 'config.yml'
    image_dir = test_dir / 'images'
    image_dir.mkdir()

    print(f'Test directory: {test_dir}\n')

    passed = 0
    failed = 0

    def test(name: str, condition: bool, detail: str = '') -> bool:
        nonlocal passed, failed
        if condition:
            print(f'  [PASS] {name}')
            passed += 1
            return True
        else:
            print(f'  [FAIL] {name}')
            if detail:
                print(f'         {detail}')
            failed += 1
            return False

    try:
        # =====================================================================
        # Test 1: Create test images
        # =====================================================================
        print('1. Creating test images...')

        test_images = []

        # Create some test images with different characteristics
        colors = [
            ('red', (255, 0, 0)),
            ('green', (0, 255, 0)),
            ('blue', (0, 0, 255)),
            ('yellow', (255, 255, 0)),
            ('purple', (128, 0, 128)),
        ]

        for name, color in colors:
            # Create a simple colored image
            img = Image.new('RGB', (200, 150), color)

            # Add some variation (random noise) to make them slightly different
            pixels = img.load()
            for _ in range(100):
                x, y = random.randint(0, 199), random.randint(0, 149)
                r, g, b = pixels[x, y]
                pixels[x, y] = (
                    min(255, r + random.randint(-10, 10)),
                    min(255, g + random.randint(-10, 10)),
                    min(255, b + random.randint(-10, 10)),
                )

            path = image_dir / f'{name}.jpg'
            img.save(path, 'JPEG', quality=90)
            test_images.append(path)

        # Create a duplicate (exact copy)
        shutil.copy(test_images[0], image_dir / 'red_copy.jpg')
        test_images.append(image_dir / 'red_copy.jpg')

        # Create a near-duplicate (slightly modified)
        img = Image.open(test_images[1])
        img = img.resize((180, 135))  # Slightly different size
        path = image_dir / 'green_similar.jpg'
        img.save(path, 'JPEG', quality=85)
        test_images.append(path)

        # Create a PNG (lossless) image
        img = Image.new('RGBA', (100, 100), (0, 128, 255, 200))
        path = image_dir / 'blue_alpha.png'
        img.save(path, 'PNG')
        test_images.append(path)

        test(f'Created {len(test_images)} test images', len(test_images) == 8)

        # =====================================================================
        # Test 2: Configuration
        # =====================================================================
        print('\n2. Testing configuration...')

        config = load_config(config_path)
        test('Config created with defaults', config_path.exists())
        test('Config has correct batch size', config.embedding_batch_size == 16)
        test('Config has image extensions', '.jpg' in config.image_extensions)

        # =====================================================================
        # Test 3: Database initialization
        # =====================================================================
        print('\n3. Testing database initialization...')

        # Create database without auto-start (we'll test components separately)
        db = ImageDatabase(
            db_path=db_path,
            thumbnail_dir=thumbnail_dir,
            config_path=config_path,
            auto_start=False,
        )

        test('Database file created', db_path.exists())
        test('Database connection open', db.conn is not None)
        test('Thumbnail directory created', thumbnail_dir.exists())

        # =====================================================================
        # Test 4: Folder management
        # =====================================================================
        print('\n4. Testing folder management...')

        # Add folder
        result = db.add_folder(str(image_dir))
        test('Add folder returns result', result is not None)
        test('Add folder has correct path', result and result['path'] == str(image_dir))

        # List folders
        folders = db.get_folders()
        test('Get folders returns list', len(folders) == 1)
        test('Folder path correct', folders[0]['path'] == str(image_dir))

        # Add same folder again (should return None)
        result2 = db.add_folder(str(image_dir))
        test('Adding duplicate folder returns None', result2 is None)

        # =====================================================================
        # Test 5: Image ingestion (manual, without threads)
        # =====================================================================
        print('\n5. Testing image ingestion...')

        # Manually ingest images
        for img_path in test_images:
            metadata = extract_image_metadata(img_path)
            if metadata:
                image_id = str(uuid.uuid4())
                create_image(
                    db.conn,
                    image_id=image_id,
                    path=metadata.path,
                    size=metadata.size,
                    width=metadata.width,
                    height=metadata.height,
                    timestamp=metadata.timestamp,
                    checksum=metadata.checksum,
                    perceptual_hash=metadata.perceptual_hash,
                    laplacian_var=metadata.laplacian_var,
                    lossless=metadata.lossless,
                )

        images = db.get_all_images()
        test(f'Ingested {len(images)} images', len(images) == len(test_images))

        # Check image properties
        if images:
            img = images[0]
            test('Image has ID', 'id' in img and img['id'])
            test('Image has path', 'path' in img and img['path'])
            test('Image has dimensions', img.get('width', 0) > 0 and img.get('height', 0) > 0)
            test('Image has checksum', 'checksum' in img and img['checksum'])

        # =====================================================================
        # Test 6: Image queries
        # =====================================================================
        print('\n6. Testing image queries...')

        if images:
            # Get single image
            img = db.get_image(images[0]['id'])
            test('Get image by ID', img is not None)

            # Get by path
            img_by_path = get_image_by_path(db.conn, images[0]['path'])
            test('Get image by path', img_by_path is not None)

            # Update image
            updated = db.update_image(images[0]['id'], {
                'description': 'Test description',
                'rating': '⭐⭐⭐',
            })
            test('Update image returns result', updated is not None)
            test('Description updated', updated and updated.get('description') == 'Test description')
            test('Rating updated', updated and updated.get('rating') == '⭐⭐⭐')

        # =====================================================================
        # Test 7: Thumbnail generation
        # =====================================================================
        print('\n7. Testing thumbnail generation...')

        if images:
            thumb_path = db.get_thumbnail_path(images[0]['id'], size=100)
            test('Thumbnail generated', thumb_path is not None and thumb_path.exists())

            if thumb_path and thumb_path.exists():
                thumb_img = Image.open(thumb_path)
                test('Thumbnail has correct max dimension',
                     max(thumb_img.size) <= 100)

        # =====================================================================
        # Test 8: Duplicate detection
        # =====================================================================
        print('\n8. Testing duplicate detection...')

        # Compute duplicates
        results = compute_all_duplicate_groups(db.conn, db.config)
        test('Duplicate computation completed', results is not None)
        test('Level 0 (identical) found duplicates', results.get(0, 0) >= 1,
             f'Found {results.get(0, 0)} groups')

        # Get duplicate groups
        level0_groups = db.get_duplicate_groups(0)
        test('Get duplicate groups returns list', isinstance(level0_groups, list))

        if level0_groups:
            group = level0_groups[0]
            test('Group has images', 'images' in group and len(group['images']) >= 2)

        # =====================================================================
        # Test 9: Stats and status
        # =====================================================================
        print('\n9. Testing stats and status...')

        stats = db.get_stats()
        test('Stats has totalImages', 'totalImages' in stats)
        test('Stats has totalFolders', 'totalFolders' in stats)
        test('Total images correct', stats['totalImages'] == len(test_images))

        status = db.get_processing_status()
        test('Status has status field', 'status' in status)
        test('Status has queue counts', 'indexing_queue' in status and 'embedding_queue' in status)

        # =====================================================================
        # Test 10: Event queue
        # =====================================================================
        print('\n10. Testing event queue...')

        # Create event queue and emit events
        eq = EventQueue()
        subscriber = eq.subscribe()
        test('Subscriber created', subscriber is not None)

        eq.emit('test_event', {'message': 'hello'})
        try:
            event = subscriber.get(timeout=1.0)
            test('Event received', event is not None)
            test('Event type correct', event.event_type == 'test_event')
            test('Event data correct', event.data.get('message') == 'hello')
        except queue.Empty:
            test('Event received', False, 'Queue was empty')

        eq.unsubscribe(subscriber)
        test('Unsubscribe works', eq.subscriber_count == 0)

        # =====================================================================
        # Test 11: Soft delete and restore
        # =====================================================================
        print('\n11. Testing soft delete and restore...')

        if images:
            image_id = images[-1]['id']

            # Soft delete
            deleted = db.delete_image(image_id, from_disk=False)
            test('Soft delete returns True', deleted)

            # Image should not appear in normal query
            visible_images = db.get_all_images(include_deleted=False)
            test('Deleted image hidden', len(visible_images) == len(images) - 1)

            # But should appear with include_deleted
            all_images = db.get_all_images(include_deleted=True)
            test('Deleted image in full list', len(all_images) == len(images))

            # Restore
            restored = restore_image(db.conn, image_id)
            test('Restore returns True', restored)

            visible_after = db.get_all_images(include_deleted=False)
            test('Restored image visible', len(visible_after) == len(images))

        # =====================================================================
        # Test 12: Folder removal
        # =====================================================================
        print('\n12. Testing folder removal...')

        removed = db.remove_folder(str(image_dir))
        test('Remove folder returns True', removed)

        folders_after = db.get_folders()
        test('No folders remaining', len(folders_after) == 0)

        # Images should be marked as deleted
        visible_after_remove = db.get_all_images(include_deleted=False)
        test('Images marked deleted after folder removal', len(visible_after_remove) == 0)

        # =====================================================================
        # Test 13: Context manager
        # =====================================================================
        print('\n13. Testing context manager...')

        db.close()
        test('Database closed', db.is_closed)

        # Re-open with context manager
        with ImageDatabase(
            db_path=db_path,
            thumbnail_dir=thumbnail_dir,
            config_path=config_path,
            auto_start=False,
        ) as db2:
            test('Context manager enters', not db2.is_closed)
            stats2 = db2.get_stats()
            test('Database accessible in context', stats2 is not None)

        test('Context manager exits and closes', db2.is_closed)

        # =====================================================================
        # Summary
        # =====================================================================
        print('\n' + '=' * 70)
        print(f'TEST RESULTS: {passed} passed, {failed} failed')
        print('=' * 70)

        if failed == 0:
            print('\nAll tests passed!')
        else:
            print(f'\n{failed} test(s) failed.')

    except Exception as e:
        print(f'\n[ERROR] Test suite crashed: {e}')
        traceback.print_exc()
        failed += 1

    finally:
        # Cleanup
        print(f'\nCleaning up test directory: {test_dir}')
        try:
            shutil.rmtree(test_dir)
            print('Cleanup complete.')
        except Exception as e:
            print(f'Warning: Could not fully clean up: {e}')

    # Exit with appropriate code
    raise SystemExit(0 if failed == 0 else 1)


if __name__ == '__main__':
    _run_tests()
