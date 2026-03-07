# Audit 16 — Schema Changes Need Proper SQLite Migrations

## Principle

> Schema changes need proper SQLite migrations

## Scope

- `app/imagedb.py` — migration system, `_SQL_MIGRATIONS`, `init_database()`, `_migrate_*()` methods
- `app/faces.py` — face table migrations (`_MIGRATIONS`, `_run_migrations()`)
- All `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE` statements
- Video-related schema changes (recent addition)
- Migration tracking table and helpers

## Findings

### Migration System Architecture

1. **Tracking table** (`imagedb.py:320-324`):
   ```sql
   CREATE TABLE IF NOT EXISTS migrations (
       id TEXT PRIMARY KEY, applied_at TEXT NOT NULL
   )
   ```

2. **Helper functions**:
   - `has_migration_run()` (`imagedb.py:474-485`) — checks if migration ID exists
   - `record_migration()` (`imagedb.py:488-496`) — records with `INSERT OR REPLACE` (idempotent)

### Core Migration Registry — `_SQL_MIGRATIONS` (18 entries)

`imagedb.py:249-284` — single source of truth for images/duplicates/video domain. Each entry is an `ALTER TABLE` or `DROP TABLE` statement with a comment mapping to its handler:

| # | Statement | Handler | Line |
|---|-----------|---------|------|
| 1 | `ALTER TABLE images ADD COLUMN description_embedding BLOB` | `_backfill_description_embeddings()` | 251 |
| 2 | `ALTER TABLE images ADD COLUMN mtime REAL` | Inline in `_process_image()` | 253 |
| 3 | `ALTER TABLE duplicate_groups ADD COLUMN updated_at TEXT` | `_migrate_duplicate_epoch_to_metadata()` | 255 |
| 4 | `ALTER TABLE images ADD COLUMN timestamp_confidence INTEGER NOT NULL DEFAULT 4` | `_migrate_add_timestamp_confidence()` | 257 |
| 5 | `ALTER TABLE images ADD COLUMN aesthetic_laion REAL` | `_backfill_aesthetic_laion()` | 259 |
| 6 | `ALTER TABLE images ADD COLUMN aesthetic_nima REAL` | `_queue_images_for_nima()` | 261 |
| 7 | `ALTER TABLE custom_groups ADD COLUMN source_path TEXT` | `_migrate_renumber_custom_groups_to_level5()` | 263 |
| 8 | `ALTER TABLE images ADD COLUMN exif_data TEXT` | `_migrate_add_exif_metadata()` | 265 |
| 9 | `ALTER TABLE custom_groups ADD COLUMN filter_json TEXT` | No backfill (NULL default) | 267 |
| 10 | `ALTER TABLE custom_groups ADD COLUMN preview_image_id TEXT` | No backfill (computed) | 269 |
| 11 | `ALTER TABLE custom_groups ADD COLUMN damaged INTEGER DEFAULT 0` | No backfill (0 default) | 271 |
| 12 | `ALTER TABLE images ADD COLUMN import_name TEXT` | No backfill (set by ImportWorker) | 273 |
| 13 | `ALTER TABLE images ADD COLUMN media_type TEXT NOT NULL DEFAULT 'image'` | No backfill (default) | 275 |
| 14 | `ALTER TABLE images ADD COLUMN duration REAL` | Set by VideoProcessingThread | 277 |
| 15 | `ALTER TABLE scenes ADD COLUMN embedding BLOB` | Set by VideoProcessingThread | 279 |
| 16 | `ALTER TABLE images ADD COLUMN preferred_scene_id TEXT` | Set by video processing | 281 |
| 17 | `DROP TABLE IF EXISTS scene_embeddings` | Cleanup of obsolete table | 283 |

### Face Migration Registry — `_MIGRATIONS` (5 entries)

`faces.py:136-151` — separate system using `PRAGMA table_info()` for column existence:

| # | Statement | Line |
|---|-----------|------|
| 1 | `ALTER TABLE faces ADD COLUMN unknown_group_id TEXT` | 138 |
| 2 | `ALTER TABLE people ADD COLUMN recognition_threshold REAL` | 140 |
| 3 | `ALTER TABLE faces ADD COLUMN semantic_embedding BLOB` | 142 |
| 4 | `ALTER TABLE faces ADD COLUMN manually_tagged INTEGER DEFAULT 0` | 145 |
| 5 | `ALTER TABLE faces ADD COLUMN updated_at TEXT` | 150 |

Runner: `_run_migrations()` at `faces.py:180-205` — uses `PRAGMA table_info()` + backfills `updated_at`.

### Startup Execution Sequence

`imagedb.py:5865-5873` — one-time migrations run before threads start:
1. `_migrate_recalculate_timestamps()` (6157-6202)
2. `_migrate_duplicate_epoch_to_metadata()` (6204-6233)
3. `_migrate_add_timestamp_confidence()` (6235-6280)
4. `_migrate_renumber_custom_groups_to_level5()` (6282-6305)
5. `_migrate_initial_directory_groups()` (6307-6333)
6. `_migrate_add_exif_metadata()` (6335-6360)
7. `_migrate_add_logs_table()` (6362-6377)

### Video Schema — Properly Migrated

All 4 video-related columns added through standard migration path (entries 13-17). The `scenes` table uses `CREATE TABLE IF NOT EXISTS` (`imagedb.py:327-342`).

### Idempotency

- All `CREATE TABLE` use `IF NOT EXISTS`
- All `ALTER TABLE` in `_SQL_MIGRATIONS` are wrapped in try/except for "duplicate column" (`imagedb.py:445-451`)
- `DROP TABLE IF EXISTS` for cleanup
- Migration tracking prevents re-execution

### Indexes

16 indexes in `imagedb.py:353-376`, 7 in `faces.py:123-133` — all `CREATE INDEX IF NOT EXISTS`.

### No Ad-Hoc Schema Modifications Found

No `ALTER TABLE`, `CREATE TABLE`, or `DROP TABLE` statements outside the migration registries.

## Status

**Compliant**

## Actions

None required.
