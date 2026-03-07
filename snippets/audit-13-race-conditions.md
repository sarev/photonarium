# Audit 13 — Avoid Race Conditions

## Principle

> Avoid race conditions

## Scope

- All `Lock`, `RLock`, `Condition`, `Event`, `Semaphore` usage in `app/`
- Shared mutable state across threads
- TOCTOU (time-of-check-time-of-use) prevention
- Optimistic locking (`updated_at`) in `app/faces.py`
- Database connection thread safety
- Double-checked locking patterns
- `app/imagedb.py`, `app/faces.py`, `app/duplicates.py`, `app/thumbnails.py`, `app/app.py`

## Findings

### Lock Inventory

**ImageDatabase (`imagedb.py`):**

| Lock | Line | Purpose |
|------|------|---------|
| `_db_lock` (RLock) | 5763 | All DB operations (reentrant for nested calls) |
| `_image_locks_lock` (Lock) | 5765 | Protects `_image_locks` dict |
| `_rotation_lock` (Lock) | 5767 | Protects `_active_rotations` counter |
| `_rotation_done` (Condition) | 5768 | Signals rotation completion |
| `_trash_progress_lock` | 5772 | Protects `_trash_progress` dict |
| `_import_progress_lock` | 5776 | Protects `_import_progress` dict |
| `_import_names_lock` | 5781 | Protects `_import_names` dict |
| `_phase4_lock` | 5806 | Protects face embedding/reassess status |
| `_checksum_cache_lock` | 5818 | Protects `_checksum_cache` dict |

**app.py globals:**

| Global | Lock | Lines |
|--------|------|-------|
| `_images_cache` | `_images_cache_lock` | 255-256 |
| `_caption_generator` | `_caption_generator_lock` | 282, 294-305 |
| `_thumbnail_cache` | `_thumbnail_cache_lock` | 395, 403-410 |
| `_face_thumb_regenerating` | `_face_thumb_regen_lock` | 251-252 |

**Other modules:**

| Component | Lock | Lines |
|-----------|------|-------|
| faces._reassess_lock | `faces.py:2571` | Reassessment thread |
| faces._grouping_lock | `faces.py:2576` | Grouping thread |
| duplicates._cache_lock | `duplicates.py:1348` | Group cache |
| duplicates._status_lock | `duplicates.py:1335` | Computation status |
| thumbnails._lock | `thumbnails.py:593` | LRU cache |

### Double-Checked Locking (3 instances, correct)

1. **CaptionGenerator** (`app.py:285-305`): Check → lock → re-check → create
2. **LAION aesthetic head** (`imagedb.py:2982-3036`): Check → lock → re-check → load
3. **Duplicate group cache** (`duplicates.py:1368-1384`): Check → lock → re-check → load

### TOCTOU Prevention

`app.py:2922-2924` demonstrates the pattern with explicit comment:
```python
# All reads and writes under one lock to prevent TOCTOU races
# (e.g. person deleted between existence check and update)
with db._db_lock:
    person = get_person(db.conn, person_id)
    if person is None:
        return error_response('Person not found', 404)
    # ... update operations follow
```

### Optimistic Locking in Face Reassessment

Three-phase pattern (`faces.py`):
1. **READ** (with lock, `2889-2915`): Capture face data + `updated_at` timestamps
2. **COMPUTE** (no lock, `3006-3016`): Similarity matching — safe since no shared state mutation
3. **WRITE** (with lock, `3024-3085`): Conditional `UPDATE ... WHERE updated_at = ?` — if `rowcount == 0`, face was modified by user, skip it

All face mutations set `updated_at`:
- `update_face_person()`: `faces.py:1845,1849`
- `toggle_face_manual_tag()`: `faces.py:1880`
- `suppress_face()`: `faces.py:1904`
- `set_semantic_embedding()`: `faces.py:1437`
- Background reassessment: `faces.py:3038,3046,3067,3074`

### Per-Image Locks (Rotation Safety)

`imagedb.py:7728-7742` — per-image lock creation under `_image_locks_lock` prevents concurrent rotation of the same file.

### Database Connection Safety

- All connections: `check_same_thread=False` (`imagedb.py:402-406`)
- WAL mode enabled (`imagedb.py:409`) for concurrent reads during writes
- `busy_timeout=5000ms` everywhere: main (`imagedb.py:415`), logdb (`logdb.py:80`), duplicates (`duplicates.py:1360`)

### Recent Fix: VideoProcessingThread Locking

Commit `52b0dd9` fixed per-row lock/unlock loops that caused "database is locked" errors. Solution: batch updates into single transaction.

### Potential Concerns

1. **No explicit deadlock prevention**: Relies on consistent lock ordering (db_lock always first). No documentation of lock ordering hierarchy.
2. **Global state in app.py**: Multiple lazy-initialised globals require discipline — currently handled correctly with double-checked locking.

## Status

**Compliant**

## Actions

- **P3**: Document lock ordering hierarchy in a code comment (e.g., "Always acquire `_db_lock` before any other lock") to aid future maintainability
