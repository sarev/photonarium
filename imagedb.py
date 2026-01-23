#!/usr/bin/env python3

"""Image database module for the Imaginary application.

This module provides all database operations, image processing, and background
task management for the Imaginary image catalogue application. It is the core
backend logic that the Flask API (app.py) delegates to.

For additional context and design decisions, see: snippets/notes.txt

===============================================================================
ARCHITECTURE OVERVIEW
===============================================================================

The module centres on the ImageDatabase class, which manages:

    1. SQLite database (WAL mode for concurrent access)
    2. Ingestion thread (metadata extraction, hashing)
    3. Embedding thread (OpenCLIP batch processing)
    4. Event queue (for SSE notifications to frontend)

Architecture diagram::

    ┌─────────────────────────────────────────────────────────────┐
    │                        Flask App                            │
    │  • API endpoints                                            │
    │  • SSE event stream ◄──────────────────────┐                │
    └─────────────────┬──────────────────────────│────────────────┘
                      │                          │
    ┌─────────────────▼──────────────────────────│────────────────┐
    │                      ImageDatabase         │                │
    │  • SQLite (WAL mode)                       │                │
    │  • Folder/image CRUD                       │                │
    │  • Duplicate group queries          event_queue             │
    │  • Thumbnail generation                    │                │
    └─────────────────┬───────────────────┬──────│────────────────┘
                      │                   │      │
    ┌─────────────────▼─────────┐ ┌───────▼──────┴────────────────┐
    │   Ingestion Thread        │ │   Embedding Thread            │
    │  • Metadata extraction    │ │  • OpenCLIP batch processing  │
    │  • SHA256 checksum        │ │  • GPU-accelerated            │
    │  • Perceptual hash        │ │  • Processes batches of 16-32 │
    │  • Laplacian variance     │ │  • Triggers duplicate compute │
    │  • EXIF parsing           │ │    when batch completes       │
    └───────────────────────────┘ └────────────────────────────────┘

===============================================================================
DATABASE SCHEMA
===============================================================================

Table: folders
--------------
Registered image source directories.

    path            TEXT PRIMARY KEY    -- Canonicalised absolute path

Table: images
-------------
All image metadata. Soft-delete via 'deleted' flag.

    id              TEXT PRIMARY KEY    -- UUID
    path            TEXT UNIQUE         -- Canonicalised absolute path
    basename        TEXT                -- Filename only
    size            INTEGER             -- File size in bytes
    width           INTEGER             -- Image width in pixels
    height          INTEGER             -- Image height in pixels
    timestamp       TEXT                -- ISO format datetime (nullable)
    checksum        TEXT                -- SHA256 hex digest
    perceptual_hash TEXT                -- imagehash phash hex string
    laplacian_var   REAL                -- Focus/sharpness score
    lossless        INTEGER             -- Boolean: 1=lossless, 0=lossy
    description     TEXT                -- User-editable, default ''
    rating          TEXT                -- User-editable emoji string, default ''
    embedding       BLOB                -- OpenCLIP vector as numpy bytes (nullable)
    deleted         INTEGER             -- Boolean: 1=soft-deleted, 0=active
    created_at      TEXT                -- When added to database
    updated_at      TEXT                -- Last metadata update

    Indexes:
        - idx_images_path ON images(path)
        - idx_images_checksum ON images(checksum)
        - idx_images_perceptual_hash ON images(perceptual_hash)
        - idx_images_deleted ON images(deleted)
        - idx_images_timestamp ON images(timestamp)

Table: duplicate_groups
-----------------------
Pre-computed duplicate relationships. Rebuilt after ingestion.

    id              INTEGER PRIMARY KEY
    level           INTEGER             -- 0=identical, 1=perceptual, 2=similar, 3=related
    group_hash      TEXT                -- Identifier linking images in same group
    image_id        TEXT                -- FK to images.id

    Index:
        - idx_dup_level_group ON duplicate_groups(level, group_hash)

===============================================================================
STARTUP SEQUENCE
===============================================================================

When ImageDatabase is instantiated:

    0. Load or create configuration YAML file
    1. Open/create SQLite database with WAL mode
    2. Create tables if they don't exist
    3. Verify registered folders still exist on disk
    4. Rescan all registered directories:
       - Recurse each folder for image files
       - Queue new/changed files for ingestion
       - Mark missing files as deleted
    5. Query images with missing embeddings → queue for embedding thread
    6. Start ingestion thread
    7. Start embedding thread

===============================================================================
DIRECTORY MANAGEMENT
===============================================================================

Adding a directory (see notes.txt for full context):

    1. Canonicalise the path
    2. Verify it exists and is a directory
    3. Add to folders table (ignore if already present)
    4. Recurse directory for image files:
       - Skip subdirectories already registered (optimisation)
       - For each image file found, add to ingestion queue
         (ingestion thread checks if already in DB)
    5. Emit 'scan_started' event

Removing a directory:

    1. Remove from folders table
    2. Pause ingestion thread (acquire lock)
    3. Filter ingestion queue:
       - Remove any queued paths not within remaining folders
    4. Resume ingestion thread (release lock)
    5. Mark orphaned images as deleted:
       - Query all images
       - If image path not within any remaining folder, set deleted=1
    6. Emit 'folder_removed' event

Subdirectory overlap handling:

    - Allowed without warning
    - Example: User can add both /a and /a/b/c
    - When queuing for ingestion, files already in DB are skipped
    - When removing /a, images in /a/b/c remain if that folder is registered

===============================================================================
IMAGE INGESTION THREAD
===============================================================================

Runs continuously, processing images from ingestion_queue.

For each image path:

    1. Check if path already in database:
       - If yes and file unchanged (size + mtime match): skip
       - If yes and file changed: update metadata
       - If no: insert new record

    2. Extract metadata:
       - File size (os.path.getsize)
       - Dimensions (PIL Image.open, read size)
       - Checksum (SHA256 of file contents)
       - Perceptual hash (imagehash.phash)
       - Laplacian variance (cv2.Laplacian + variance)
       - Lossless flag (based on file extension/format)
       - Timestamp (see timestamp derivation below)

    3. Insert/update database record (embedding=NULL initially)

    4. Add image ID to embedding queue

    5. Emit 'image_ingested' event (for progress tracking)

When ingestion queue becomes empty:

    - Do NOT emit completion yet
    - Wait for embedding thread to finish current batch
    - Then compute duplicate groups
    - Then emit 'ingestion_complete' event

===============================================================================
TIMESTAMP DERIVATION
===============================================================================

Priority order (use first non-None value):

    1. EXIF "DateTimeOriginal" tag
    2. EXIF "DateTime" tag
    3. Filesystem creation time (Windows) / birth time (Unix if available)
    4. Filesystem modification time
    5. Parsed from filename/path

Filename parsing algorithm:

    Search the canonicalised full path for digit groups. A valid date is:

    - 8 contiguous digits: YYYYMMDD
    - 6 contiguous digits: YYMMDD (assume 19xx if YY>50, else 20xx)
    - 3 separate groups (max 1 char separator): YYYY-MM-DD or YY-MM-DD

    A valid time (optional) is:

    - 6 contiguous digits: HHMMSS
    - 4 contiguous digits: HHMM (seconds=00)
    - 2-3 separate groups (max 1 char separator): HH:MM or HH:MM:SS

    If both date and time patterns match, earlier position = date, later = time.
    If no time found, default to 00:00:00.
    If no date found, return None.

    Validation:
    - Year: 1900-2099
    - Month: 01-12
    - Day: 01-31 (basic validation, not calendar-aware)
    - Hour: 00-23
    - Minute: 00-59
    - Second: 00-59

===============================================================================
EMBEDDING THREAD (OpenCLIP)
===============================================================================

Runs continuously, processing images from embedding_queue in batches.

Batch processing:

    1. Accumulate up to BATCH_SIZE (16-32) image IDs from queue
    2. Load images from disk (skip any that fail to load)
    3. Preprocess batch for OpenCLIP
    4. Run inference (GPU if available, else CPU)
    5. Store embeddings in database (numpy.tobytes() → BLOB)
    6. If ingestion queue is empty AND embedding queue is empty:
       - Compute duplicate groups for all levels
       - Emit 'processing_complete' event

OpenCLIP model:

    - Model: ViT-B-32 (good balance of speed/quality)
    - Pretrained: openai or laion2b_s34b_b79k
    - Load once at startup, reuse for all batches

===============================================================================
DUPLICATE GROUP COMPUTATION
===============================================================================

Called after both queues are empty. Computes groups at 4 levels:

Level 0 - Identical:
    - GROUP BY checksum WHERE deleted=0
    - Groups with COUNT(*) > 1 are duplicates

Level 1 - Perceptual (near-identical):
    - Compare perceptual hashes
    - Hamming distance <= 4 considered duplicate
    - Use efficient algorithm (e.g., VP-tree or brute force for small sets)

Level 2 - Similar:
    - Compare OpenCLIP embeddings
    - Cosine similarity >= 0.95
    - Cluster using agglomerative clustering or brute force

Level 3 - Related:
    - Compare OpenCLIP embeddings
    - Cosine similarity >= 0.85
    - Same clustering approach as level 2

Storage:

    - Clear duplicate_groups table
    - Insert new groupings
    - Each image can appear in multiple groups at different levels

Note: Deleted images (deleted=1) are excluded from all duplicate groups.

===============================================================================
THUMBNAIL GENERATION
===============================================================================

Thumbnails are generated on-demand and cached.

Cache location: .thumbnails/<size>/<first2chars>/<checksum>.jpg

Generation:
    1. Check cache for existing thumbnail
    2. If not cached:
       - Load original image
       - Resize maintaining aspect ratio (longest edge = size)
       - Convert to RGB (handle RGBA, palette modes)
       - Save as JPEG quality 85
    3. Return path to thumbnail

Sizes supported: 50-800px (clamped in API)

===============================================================================
EVENT SYSTEM (SSE)
===============================================================================

Events emitted to event_queue for frontend consumption:

    scan_started        {folder: str}
    scan_progress       {processed: int, total: int, current_file: str}
    image_ingested      {image_id: str, path: str}
    folder_removed      {folder: str}
    processing_complete {}
    error               {message: str}

Frontend connects to /api/events SSE endpoint to receive these.

===============================================================================
THREAD SAFETY
===============================================================================

SQLite configuration:
    - WAL mode for concurrent reads
    - check_same_thread=False
    - busy_timeout=5000ms

Locks:
    - ingestion_lock: Acquired when modifying ingestion queue or pausing thread
    - db_lock: Acquired for write operations (SQLite handles most concurrency)

Queue implementation:
    - Use queue.Queue (thread-safe) for ingestion_queue and embedding_queue
    - Use queue.Queue for event_queue

===============================================================================
PUBLIC API
===============================================================================

Class: ImageDatabase

    __init__(db_path: str, thumbnail_dir: str = '.thumbnails')
    close() -> None

    # Folder management
    get_folders() -> List[Dict]
    add_folder(path: str) -> Dict
    remove_folder(path: str) -> bool

    # Image queries
    get_all_images(include_deleted: bool = False) -> List[Dict]
    get_image(image_id: str) -> Optional[Dict]
    update_image(image_id: str, data: Dict) -> Optional[Dict]
    delete_image(image_id: str, from_disk: bool = False) -> bool

    # Thumbnails
    get_thumbnail_path(image_id: str, size: int = 200) -> Optional[str]

    # Duplicates
    get_duplicate_groups(level: int) -> List[Dict]

    # Stats
    get_stats() -> Dict

    # Events
    get_event_queue() -> queue.Queue

    # Background tasks (internal, but exposed for testing)
    start_background_threads() -> None
    stop_background_threads() -> None
    queue_folder_scan(path: str) -> None

===============================================================================
DEPENDENCIES
===============================================================================

Standard library:
    - sqlite3
    - threading
    - queue
    - hashlib
    - os
    - pathlib
    - uuid
    - datetime
    - re

Third-party:
    - PIL / Pillow (image loading, EXIF, thumbnails)
    - imagehash (perceptual hashing)
    - numpy (embedding storage)
    - open_clip (OpenCLIP model)
    - torch (OpenCLIP backend)
    - cv2 / opencv-python (Laplacian variance)

Notes:

    - prefer pathlib Path objects over OS path/filename strings where possible.
    - use `typing` and declare types on all globals, parameters, and attributes
    - use from __future__ import annotations  so we don't need "quotes" around types
    - ensure all functions, classes, and methods have decent PEP format docstrings

===============================================================================
CONFIGURATION CONSTANTS
===============================================================================

Configuration is persisted in `.config.yml` which includes comments describing
what each configuration item does, along with the (range of) valid values:

    IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}
    BATCH_SIZE = 16  # OpenCLIP batch size
    PERCEPTUAL_HASH_THRESHOLD = 4  # Hamming distance for level 1
    SIMILARITY_THRESHOLD_L2 = 0.95  # Cosine similarity for level 2
    SIMILARITY_THRESHOLD_L3 = 0.85  # Cosine similarity for level 3
    THUMBNAIL_QUALITY = 85  # JPEG quality for thumbnails

===============================================================================
IMPLEMENTATION STATUS
===============================================================================

[ ] Configuration file handling (including creating with default values)
[ ] Database schema and initialisation
[ ] Folder management (add, remove, list)
[ ] Image CRUD operations
[ ] Timestamp derivation (EXIF, filesystem, filename parsing)
[ ] Ingestion thread
[ ] Perceptual hash computation
[ ] Laplacian variance computation
[ ] Checksum computation
[ ] Embedding thread with OpenCLIP
[ ] Duplicate group computation (all 4 levels)
[ ] Thumbnail generation and caching
[ ] Event queue and SSE support
[ ] Startup sequence (rescan folders, queue missing embeddings)
[ ] Thread safety and graceful shutdown

===============================================================================
"""

# Implementation follows below
# TODO: Implement ImageDatabase class and supporting functions
