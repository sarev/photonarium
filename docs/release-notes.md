# Release Notes

## v1.2.8-beta

### Single-Writer Database Architecture

The database access layer has been redesigned to eliminate `database is locked` errors permanently. Previously, four independent SQLite connections competed for the WAL write lock — the shared connection (Flask + pipeline), up to 16 Stage 1 worker connections, DuplicateManager transient connections, and the log handler's dedicated connection. `SQLITE_BUSY_SNAPSHOT` could bypass SQLite's busy handler entirely, causing instant failures that retry logic couldn't recover from.

`SafeConnection` (`app/safeconn.py`) now uses a **single-writer queue architecture**: one dedicated writer thread owns the only writable connection, and all writes from every thread are submitted via a `queue.Queue`. Reads go to a separate read-only connection under WAL mode, allowing concurrent readers without blocking the writer. This eliminates cross-connection write contention by construction — no more retries, no more backoff, no more `SQLITE_BUSY`.

Key changes:

- **Automatic read/write routing**: `execute()` inspects the SQL prefix and routes SELECTs to the read connection, writes to the writer queue. Callers don't need to change.
- **`_WriteCursor` proxy**: write operations return a lightweight cursor proxy instead of a raw `sqlite3.Cursor`, preventing cross-thread cursor finalisation from corrupting internal C-level state.
- **Context manager**: `with safe_conn:` routes all enclosed operations to the writer thread atomically via a per-scope sub-queue. Re-entrant for nested blocks.
- **`write_fn` / `write_fn_async`**: submit arbitrary callables to the writer thread. The async variant is fire-and-forget (used by the log handler).
- **Pipeline Stage 1**: workers no longer create per-thread connections. Parallel I/O (stat, hash, EXIF extraction) continues across 16 threads; only the DB writes serialise through the queue.
- **DuplicateManager**: no longer creates transient per-call connections. Uses the shared `SafeConnection` throughout.
- **Removed**: `_db_lock` (RLock), retry logic, backoff timers, per-worker connection tracking, `busy_timeout` reliance.

### Bug Fixes

- **Face assign contention**: the "ignore all unknown faces" action could fail with `database is locked` when the background face reassessment was writing simultaneously. Fixed by batching face updates with `executemany` + single commit instead of per-face UPDATE + commit loops. Also fixed the same pattern in the unassign, suppress, and lock/unlock batch endpoints.
- **Raw connection bypasses**: `_clone_transcode_record` and `_load_checksum_cache` used the raw `sqlite3.Connection` directly, bypassing `SafeConnection`'s safety guarantees. Now routed through `safe_conn`.
- **Type annotations**: `init_face_tables()` and `_run_migrations()` in `faces.py` annotated their parameter as `SafeConnection` but received a raw connection during startup. Fixed to `SafeConnection | sqlite3.Connection`.

## v1.2.7-beta

### Quality Scoring Overhaul

The "Sort by quality" feature has been substantially reworked. Previously, NIMA scores were computed from 400px thumbnails — the model saw a 224px centre crop of a heavily downscaled, sharpened, JPEG-recompressed image. This crushed the model's dynamic range to 3.0–6.3 (on a 1–10 scale), making the quality sort near-useless.

NIMA now scores from the original image. A threaded prefetch pipeline (4 workers) overlaps disk I/O with GPU inference so the throughput impact is modest. A one-time migration clears existing scores for automatic recomputation.

The LAION aesthetic head is now validated against the pretrained weights. The `sa_0_4` heads were trained on `openai` embeddings — using them with other pretrained variants (e.g. `laion2b_s34b_b88k`) produced near-random scores because the embedding geometry differs even when the dimension matches. The pipeline now detects incompatible weights and disables LAION scoring with a clear log message. This affected the `high_laptop` and `high_desktop` config presets.

The frontend quality formula has switched from absolute normalisation (`score / 10`) to percentile ranking for the aesthetic component, matching what was already done for sharpness, resolution, and BPP. This gives the aesthetic weight its full [0, 1] range regardless of the model's output distribution. The backend mirror in `trash.py` has been updated to match.

### Event-Driven Pipeline

The pipeline orchestrator no longer polls. Previously it woke every 2 seconds and ran 8+ DB queries (Stages 2–5) even when idle, contending with Flask threads for the shared database lock and causing multi-second waits during normal browsing.

The pipeline now blocks on a `threading.Event` and only runs when explicitly triggered — by rescan, folder add, import, scan timer, or cascading work from a previous stage. When idle: zero DB queries, zero lock contention, zero CPU usage. Shutdown and rescan triggers wake the pipeline immediately.

Broad `with safe_conn:` scopes around single-SELECT work-check queries have been removed (8 sites). Each `safe_conn.execute()` call already auto-locks per call — the broad scope was holding the RLock for the entire query duration unnecessarily.

### Duplicate Detection Performance

Stage 6 (grouping) has been reworked for large libraries:

- **Diff-based directory group sync**: Previously deleted and reinserted all level-4 memberships every cycle (~45,000 writes for a 44k library). Now computes a diff and only writes changes — typically ~1 INSERT for one new image. Eliminates the "database is locked" errors caused by the long write phase.
- **Incremental level 1**: Perceptual hash duplicates now support incremental updates (previously always did a full clear + rebuild via LSH). For 1 dirty image, avoids rebuilding ~1,881 groups.
- **Combined embedding scan**: Levels 2 and 3 incremental now scan all embeddings once instead of twice, halving the time for small updates.
- **Cursor-based pagination**: Replaced `LIMIT/OFFSET` with `WHERE id > ?` for chunked embedding queries. SQLite's OFFSET is O(offset) — the 9th chunk at offset 40,000 was scanning 40k rows to skip them.
- **Batched INSERTs**: `_insert_duplicate_group` now uses `executemany` instead of per-row `execute` calls, reducing lock acquisitions from N to 1 per group.
- **Shared connection**: Duplicate computation now uses the shared `safe_conn` instead of creating throwaway private connections that competed for the WAL write lock.
- **Status polling fix**: Removed the broad `with safe_conn:` from `get_processing_status()` COUNT queries — they don't need cross-query atomicity.

### Threaded Image Prefetch (Stages 3, 4, 5)

All three GPU pipeline stages now use double-buffered prefetching: a `ThreadPoolExecutor` (4 workers) loads and preprocesses the next batch's images while the GPU processes the current batch. Previously, image loading was sequential — the GPU sat idle during disk I/O.

- **Stage 3** (embeddings): Workers run `raw_open_image` + `clip.preprocess` in parallel. New `encode_tensors_batch` method on `OpenCLIPModel` accepts pre-processed tensors.
- **Stage 4** (NIMA): Workers load originals via `raw_open_image`; `score_images_batch` handles the transform.
- **Stage 5** (faces): Batch-level prefetch overlaps `preload_images_batch` for the next batch with the current batch's GPU detection and DB writes.

### GPU Resilience and Automatic CPU Fallback

Previously, a GPU error (context corruption, driver reset, or resource exhaustion) permanently disabled GPU features until the app was restarted. The codebase only caught OOM errors — any other `RuntimeError` from the GPU crashed through unhandled.

New centralised `GpuHealth` state machine (`app/gputil.py`) tracks GPU availability across all models (OpenCLIP, NIMA, MTCNN/ResNet, BLIP, Whisper). On a non-OOM GPU failure:

1. First failure: model is unloaded, CUDA cache cleared, one retry on the same device
2. Second failure: automatic fallback to CPU (slower but functional)
3. CPU failure: feature disabled permanently

A **modal dialog** warns the user on both the GPU→CPU transition and the disabled transition. The dialog also appears on page load if the backend is already in a degraded state (e.g. if the user reloads the browser after a GPU failure). The `/api/status` endpoint includes a `gpu_health` field so the frontend can check on connect.

OOM errors are handled separately — they're transient (the GPU works, it's just full) and use the existing single-item fallback with no user notification.

Error detection covers all supported GPU backends: CUDA (NVIDIA), MPS (Apple Silicon), and XPU/IPEX (Intel). The `is_gpu_error()` helper in `gputil.py` matches by `torch.OutOfMemoryError` class where available, and by keyword in the error message as a fallback (covering `'cuda'`, `'mps'`, `'xpu'`, `'ipex'`, `'native api failed'`, `'dpcpp'`).

The pipeline skips remaining GPU stages after a context error rather than letting each stage independently discover the same broken GPU. Stages that have fallen back to CPU still run.

Also reverted the idle GPU model unloading from v1.2.6-beta — the `unload()` call between pipeline cycles raced with Flask search threads, causing `AttributeError` on the `model` property. Models now stay loaded for the app lifetime.

### Bug Fixes

- **Sort order stuck on similarity**: Clearing a semantic search filter left the sort order on "content" (by similarity) instead of restoring the previous sort. The duplicate-group filter already had save/restore logic — this extends it to semantic filters.
- **NIMA video death loop**: The NIMA scoring query was missing `AND media_type = 'image'`, causing videos to be included. Since `raw_open_image` can't open `.mp4` files, they failed silently every cycle, creating an infinite loop of model load → fail → retry.
- **NIMA corrupt image death loop**: Images that fail to load (corrupt, zero-byte) now get a sentinel score of 0.0 so they aren't retried every pipeline cycle.
- **NIMA/LAION DB lock crash**: A "database is locked" error during the batch commit killed the entire pipeline stage. Now caught per-batch with a warning, allowing the stage to continue.
- **LAION query ordering**: The LAION backfill checked head compatibility after fetching all embedding BLOBs (~86MB for 44k images). Moved the check before the query.
- **Stage 4 silent completion**: Stages 4a/4b only logged "complete" when `count > 0`. When all images failed to load, the stage completed with no log output at all.
- **LAION video scoring**: The LAION backfill query was also missing the `media_type = 'image'` filter.
- **Unhandled GPU errors in `encode_text()`**: Text encoding (used by search) had no error handling at all — a GPU context error crashed through as a 500. Now catches GPU errors, unloads the model for reload on the next request, and returns a proper error.
- **LAION head incompatible with non-openai pretrained**: The LAION aesthetic head was trained on `openai` embeddings but was silently applied to other pretrained variants (e.g. `laion2b_s34b_b88k`), producing near-random scores. Now detects the mismatch and disables LAION scoring with a clear log message. Affected the `high_laptop` and `high_desktop` config presets.
- **Face semantic embedding unprotected**: `clip.encode_image()` for face semantic embeddings in Stage 5 had no error handling — GPU errors crashed the face processing stage.
- **Caption model None check**: `CaptionGenerator.generate()` accessed `self.processor` and `self.model` without checking for None after a failed load.
- **Server restart broken on Windows**: The `/api/restart` endpoint used `os.execv` which on Windows spawns a child process without killing the parent, leaving two instances running against the same database and GPU. Now uses `subprocess.Popen` + `os._exit` on Windows to cleanly replace the process.

## v1.2.6-beta

### OpenCLIP Model Change Detection

Changing the OpenCLIP model or pretrained weights in the config no longer requires manual database surgery. On startup, the app compares the configured model against the identity stored in the `metadata` table. If they differ, all OpenCLIP-derived data is automatically invalidated and recomputed by the pipeline:

- Image embeddings and LAION aesthetic scores
- Description embeddings (image captions)
- Video scene embeddings and transcription embeddings
- Face semantic embeddings (CLIP encodings of face thumbnails)
- Video preferred scene selection

The invalidation runs before background threads start (no lock contention). Text embedding backfills (descriptions, transcriptions) run in the pipeline background thread with progress visible on the Database screen, rather than blocking startup. Both respect graceful shutdown and commit in batches of 100.

The same pattern already existed for NIMA model changes — this extends it to cover the much larger OpenCLIP dependency graph.

### Model Download Prompt on Settings Change

Model-affecting config fields (`openclip_model`, `openclip_pretrained`, `caption_model`, `stt_model`) are now tagged with a `[M]` marker in the config schema. When the user changes any of these in the Settings editor, a confirmation dialog offers to open the setup wizard's Download tab so the new models can be fetched before restarting.

This prevents the app from restarting into a broken state where models are missing. The wizard opens directly on the HF Token step (skipping Hardware/Language/Review) and only runs the download — no config values are overwritten. On successful download, the Finish button becomes "Restart Server".

The restart logic (`App.restartServer()`) has been extracted from the Database screen into `core.js` so both the Database restart button and the wizard can share it.

### Face Detection Scaling Fix

Stage 5 (face detection) feeds 400px thumbnails to MTCNN, but the user's configured `face_detection_min_size` is specified in original-image pixels. Previously, MTCNN's internal pre-filter applied the pixel threshold at thumbnail resolution, silently rejecting faces that would have passed at original resolution.

The pipeline now:
- Scales `min_face_size` proportionally for MTCNN based on the largest original image in the batch (floor of 10px)
- Tracks the thumbnail-to-original scale factor per image so the post-filter evaluates face sizes in original-image pixel space

This may increase face detections (including false positives) for users with large source images. The default `face_detection_batch_size` has been lowered from 32 to 24.

### Video Search Heatmap Fix

The per-scene heatmap on the Videos screen is now normalised on visual similarity scores only. Previously, transcript similarity was folded into the normalised score, which penalised scenes without speech and produced washed-out heatmaps. Transcript similarity still contributes to the `combined_score` used for ranking videos in search results, but no longer distorts the heatmap colours.

`TRANSCRIPT_BOOST` reduced from 0.15 to 0.05 to prevent transcript matches from dominating visual relevance.

### Video Pipeline Fixes

- **Stage naming**: Video scene processing now uses the stage name `video_scenes` instead of sharing `thumbnails` with image thumbnail generation. The Database screen no longer shows a spurious "Video" pipeline status during image-only processing.
- **STT race condition**: `_rerun_requested` is now cleared before finalisation stages (grouping, STT). Previously, a rerun requested during stages 2–5 could cause finalisation to skip immediately, leaving transcription unprocessed.

### Video Timeline Fix

Clearing the search filter on the Videos screen no longer loses the selected video. `clearSearch()` now preserves the selection and re-fetches scenes without heatmap scores.

### GPU Batch Size Benchmarking Tool

New standalone tool (`tools/benchmark_batch_sizes.py`) that finds optimal batch sizes for the three CUDA pipeline stages: OpenCLIP embeddings, NIMA scoring, and face detection.

- Loads the user's real config to pick the correct models
- Binary-searches for the maximum viable batch size (no OOM)
- Sweeps candidates with warmup + timed trials to find peak throughput
- Reports recommendations with current vs optimal values
- Timestamps each stage and the overall run

```bash
python tools/benchmark_batch_sizes.py                     # All stages, default config
python tools/benchmark_batch_sizes.py --stage embeddings   # Single stage
python tools/benchmark_batch_sizes.py --max 128            # Search higher
python tools/benchmark_batch_sizes.py --images ~/photos    # Custom test images
```

### Batch Size Config Changes

- Maximum batch size raised from 64 to 256 for all three settings (`embedding_batch_size`, `nima_batch_size`, `face_detection_batch_size`). Benchmark data shows mid-range GPUs (RTX 4060, 8 GB) handle 96+ without OOM.
- Hardware wizard presets updated with per-stage values informed by benchmark data:
  - High-end laptop: embeddings 64, NIMA 16, faces 32
  - High-end desktop: embeddings 128, NIMA 32, faces 64
- Field comments now reference `tools/benchmark_batch_sizes.py`.

### OpenCLIP Model Load Error Handling

A generic `Exception` catch has been added to the OpenCLIP model loader alongside the existing OOM handler. Previously, a download failure (e.g. missing model files with `HF_HUB_OFFLINE=1`) would throw an uncaught exception on every image instead of setting `_load_failed` and logging once.

### Config Schema Reorganisation

The 55 configuration fields have been reorganised from 18 organically-grown sections into 12 logically-grouped sections: Storage & Server, Performance, Models, Image & Video Processing, Face Detection & Recognition, Captioning, Video & Speech-to-Text, Duplicate Detection, Quality Scoring, Features, Thumbnail Loading, and Logging. All field descriptions have been rewritten to be friendlier for non-technical users.

### Text Embedding Backfill Lock Contention Fix

The description and transcription embedding backfills (which run after a model change) were holding the shared database connection for the entire duration of `encode_text()` calls — up to an hour for large libraries. This starved other writers (log handler, API requests) and caused "database is locked" failures. Both backfills now use a two-phase batch pattern: encode a batch of 100 texts without the DB lock, then write results in a brief burst.

### Other Fixes

- **LAION download 429 errors**: Switched from `github.com/…/blob/…?raw=true` URLs (which get rate-limited) to `raw.githubusercontent.com` for the LAION aesthetic predictor weight files.
- **Pretrained tag typo**: Settings help text corrected from `laion2b_s34b_b79k` to `laion2b_s34b_b88k`.
- **Test scripts**: `test-start.sh` now symlinks NIMA/LAION model files from the real data directory into the test instance, and supports a `--tutorial` flag for tutorial-matching face detection config.
- **Lint cleanup**: All pre-existing ESLint and ruff warnings suppressed or fixed for zero-noise lint output.

## v1.2.5-beta

### SafeConnection Database Abstraction

All SQLite database access now routes through a new `SafeConnection` wrapper (`app/safeconn.py`). This is a comprehensive fix for the "database is locked" connection-poisoning problem that could make face tagging and other write operations fail permanently until restart.

`SafeConnection` wraps every `execute()`, `executemany()`, and `commit()` call with:

- **Automatic RLock serialisation** — thread-safe by default, no manual lock management needed at call sites.
- **Retry on transient lock errors** — retries up to 3 times with linear back-off before giving up.
- **Rollback on failure** — if the final retry fails, the pending transaction is automatically rolled back so the connection stays usable for subsequent operations.
- **Context manager** — `with safe_conn:` provides broader atomic scope for read-modify-write patterns.
- **Diagnostic logging** — lock wait times, retries, and rollbacks are logged at DEBUG/WARNING level with named connections for diagnosing contention.

Every `sqlite3.connect()` call in the codebase is now wrapped in `SafeConnection`, including the shared connection (`ImageDatabase.safe_conn`), per-thread pipeline worker connections, `DuplicateManager` connections, and log handler connections. This gives a single point of control for all database contention.

### Per-Folder Rescan

The "Rescan this folder" button on the Database screen now correctly rescans only the selected folder rather than triggering a full rescan of all registered folders. Multiple per-folder rescans can be queued and are processed together. A full rescan (via the Rescan button or `scan_interval_minutes`) still walks all folders.

### GIL Contention Improvements

Several threaded loops that held the Python GIL for extended periods have been improved to allow other threads (particularly Flask request handlers) to run:

- **Pipeline ingestion** — replaced busy-spin polling of futures with `concurrent.futures.wait()`, which blocks and releases the GIL until a worker completes.
- **Face matching** — `find_best_match()` vectorised from a per-face `np.dot()` loop into a single `known_matrix @ embedding` matrix multiply.
- **Face reassessment** — periodic GIL yield every 200 candidates in the matching loop.
- **Union-find clustering** — periodic GIL yield every 500 rows in post-matrix-multiply union loops (both face grouping and duplicate detection).

### Pipeline Stage Logging

All seven pipeline stages now log clear INFO-level messages at both start and completion, making it straightforward to confirm each stage ran and how long it took. Model loading (MTCNN, InceptionResnetV1, Whisper, NIMA) now includes timing in log messages.

### Bug Fixes

- **Stage 1 deadlock causing 100% CPU** — the ingestion loop had no exit condition when all work was done; it spun indefinitely after processing all files. Fixed by breaking out of the loop when all futures are complete.
- **Thumbnail placeholders persisting in Gallery** — two issues: (1) Stage 2a thumbnail generation crashed on the first pipeline cycle with "signal only works in main thread" because importing `get_thumbnail_cache` from `app.py` triggered a second module initialisation (app.py runs as `__main__`). Fixed by storing the cache reference on `ImageDatabase`. (2) The RAM thumbnail cache served stale placeholder bytes after real thumbnails were generated on disk. Fixed by evicting checksums from the cache after Stage 2a/2b overwrites placeholders.
- **Video search results missing media_type** — videos were not returned in search results due to a missing `media_type` field in the search response.

### Server Startup Responsiveness

The images cache and thumbnail RAM cache are now pre-populated synchronously before the pipeline starts, completing in ~1-2 seconds without GIL contention from worker threads. This ensures the `/api/images` endpoint responds immediately when the frontend loads, rather than being starved of CPU time by Stage 1 worker threads.

### Debug Logging Cleanup

PIL/Pillow plugin import messages are now suppressed when running with `--debug`, eliminating hundreds of noisy log lines that obscured useful output.

---

## v1.2.4-beta

### Wildcard Date Filter

The date filter on the Search screen has been redesigned. Instead of two date-picker inputs, each date is now entered as separate **Year**, **Month**, and **Day** fields. Any field can be left as "Any" to act as a wildcard, making it easy to search for recurring date patterns:

- **Every May 14th** — leave Year blank, set Month to May, Day to 14
- **All of March, any year** — leave Year and Day blank, set Month to Mar
- **April 2002 through October 2024** — fill in both dates with the range toggle on

A compact **range toggle** (⇄) switches between single-date and range mode. When enabled, a second row of fields appears, prepopulated with the "from" values. Range mode supports **wrap-around month ranges** — for example, "Oct to Feb" matches October through February regardless of year, useful for finding winter or summer photos across your entire library.

Each date row includes a **calendar picker** button for quick entry via the browser's native date picker. Existing Smart Groups saved with the old date format are automatically converted when opened.

The Search screen layout has also been tightened to reduce vertical space, with helpful tooltips added to all filter fields.

### Scroll Date Hint on Videos Screen

The floating date hint that appears while scrolling the Gallery thumbnail grid is now also shown on the Videos screen. As you scroll through your video library, a small overlay near the cursor shows the date of the videos at that position.

### HEVC Codec Support

HEVC/H.265 videos are now recognised as browser-compatible (Chrome 107+, Firefox 130+, Safari), removing false-positive transcoding warning badges. Video thumbnails on the Videos screen now show resolution and codec information in their tooltip (e.g. 1920×1080 · HEVC · AAC).

### Bug Fixes

- **Subtitle editor not resetting when switching videos** — clicking a different video while the transcription editor was active left stale text in the editor and could cause the timeline to get stuck on "Loading scenes...".
- **Clear filter button not resetting the Search screen** — pressing the toolbar's clear filter button while on the Search screen cleared the active filter but left the form fields populated with the old values.

---

## v1.2.3-beta

### Inline Subtitle Editor

A new **edit** toggle in the Videos toolbar opens a subtitle editor panel above the scene timeline. Click any scene to load its transcription, edit the text, and press **Enter** to save or **Escape** to cancel. The editor stays active for editing multiple scenes in sequence. You can correct auto-generated transcriptions or add subtitles to scenes that have none. Edits update the semantic search embedding so edited text is immediately searchable.

### Codec-Aware Video Transcoding

Videos encoded with browser-incompatible codecs (e.g. EAC-3/Dolby Digital Plus audio in MKV containers) now show a **warning badge** on their thumbnails. Clicking the badge opens a transcoding dialog that converts the video to MP4/H.264/AAC using ffmpeg. The transcoded copy inherits the original's scenes, transcriptions, thumbnails, and embeddings — no pipeline re-processing required. Progress is reported on the Database screen.

Video and audio codec metadata is extracted during ingestion and stored in the database. A one-time backfill migration runs on upgrade to populate codec data for existing videos.

### Import Timestamp Pinning

Files imported into the catalogue previously had their filesystem timestamps replaced with the copy date, making date-based sorting unreliable after import. The importer now derives the authoritative timestamp from the original file (using EXIF, filename parsing, and filesystem metadata while they're still trustworthy) and stores it with manual confidence, preventing the self-healing logic from overwriting correct dates on subsequent scans.

### Bug Fixes

- **Perpetual re-indexing of unreadable files** — files that fail metadata extraction (zero-byte, corrupt, unsupported) were silently skipped with no DB record, causing the pipeline to retry them on every startup and triggering a full Stage 6 run each time. Stub records are now created so they're recognised as existing on subsequent runs, but self-heal if the file is replaced on disk.
- **Silent videos re-processed every run** — videos with no audio or no detected speech had scene transcriptions left as NULL, causing Stage 7 to re-select them every restart. Silent scenes are now marked with empty transcription strings, with a one-time migration to backfill existing silent scenes.
- **Unnecessary Stage 6 on restart** — grouping and duplicate computation ran on every restart even when nothing had changed, significant for large libraries where this takes minutes. Now skipped when no new or changed files are detected.
- **Import completion callback** — fixed a call to a non-existent method that could cause errors after import.
- **Codec backfill migration logging** — the one-time codec backfill job now logs "this may take a few minutes" before starting and commits in batches of 50 (instead of one giant transaction), releasing the DB connection between batches so the log handler and other operations can proceed.
- **Thread-safety in read-only DB methods** — several read-only database methods were called from Waitress request threads without the connection lock, causing intermittent crashes under concurrent load.
- **Inline Python imports** — moved ~30 inline stdlib imports to top-level across 8 backend files for consistency and readability.

---

## v1.2.2-beta.19

### First-Run Setup Assistant

New users are greeted with a guided setup flow that configures Photonarium for their hardware and language before the first scan. The assistant appears automatically when the library is empty and walks through five steps:

1. **Hardware profile** — choose from four presets (Low-end / NAS, Moderate PC, High-end Laptop, High-end Desktop) that tune thread counts, batch sizes, thumbnail cache, STT model size, CLIP model, and captioning model to match your system. A "Manual" option skips tuning for users who prefer to configure settings themselves.
2. **Language & search models** — choose English (fastest), Multilingual (200+ languages), or Multilingual High Quality. This selects the appropriate CLIP model for semantic search. A note advises that the small Whisper model or above is recommended for non-English audio transcription.
3. **Review** — a summary table showing exactly which models will be downloaded and their approximate sizes.
4. **HuggingFace token** (optional) — enter a token for faster, more reliable downloads and access to gated models. The token is used for the download session only and is not saved.
5. **Download** — launches the model downloader with real-time progress output. Models already cached locally are skipped. You can abort and retry, or skip the download entirely and run `download_models.py` later.

The assistant can be re-launched at any time from the Settings dialog. Hardware presets and language choices update the configuration file, so all settings remain editable afterwards.

### Video Transcription Subtitles

Scene transcriptions now appear as **subtitles** during video playback:

- **Full-screen viewer** — WebVTT captions are loaded automatically for videos with transcriptions. Standard browser subtitle controls apply.
- **Scene preview popup** — hovering (or long-pressing on mobile) a scene in the Videos timeline shows a popup with the scene's keyframe thumbnail and transcription text.

Speech-to-text is now **enabled by default** with automatic language detection, so new installations transcribe video audio without any configuration.

### Per-Video Language Selection

Each video can have its language set individually for transcription. Right-click a video in the Videos grid to choose a language, then retranscribe with the correct Whisper language hint. Useful for multilingual libraries where auto-detection doesn't always get it right.

### Timeline Collapse Animation

When no single video is selected (multiple selected or none), the scene timeline smoothly collapses to the bottom of the Videos screen with a CSS transition, rather than disappearing abruptly.

### Database Reliability

Several fixes to SQLite lock contention and transaction handling that improve stability during long processing runs:

- **Stage 6 lock contention** — `sync_directory_groups()`, `reassess_unknown_faces()`, and `compute_unknown_face_groups()` previously held the database lock across heavy computation (matrix multiplication, O(n²) similarity). All three are restructured into **READ (lock) → COMPUTE (no lock) → WRITE (lock)** phases, so the lock is only held briefly for actual database I/O.
- **Stage 7 STT lock contention** — log handler `busy_timeout` increased and batch flush hardened with rollback on failure.
- **Cascade transaction failures** — added missing `conn.rollback()` calls to seven exception handlers across `pipeline.py`, `imagedb.py`, and `logdb.py`. Without rollback, a failed `commit()` left the connection in a broken transaction state, causing all subsequent operations on that connection to fail for the rest of the pipeline run.

### Filename Date Parsing Fixes

- **Dot-delimited times parsed as dates** — filenames like `2025-07-13 14.02.30.mp4` had their time portion (`14.02.30`) misinterpreted as a DMY date (16 August 1933), which then outscored the real date. The parser now recognises space-preceded triplets in the HH.MM.SS range as times, not dates.
- **Windows path separators** — the scoring parser now normalises backslash path separators before splitting into components, so filenames produce consistent results regardless of operating system.
- **Video timestamps not self-healing** — videos with wrong timestamps from earlier parser bugs were not being corrected on rescan because the parser was still producing the same wrong result. Fixed by the two changes above.
- **Resolution numbers parsed as years** — four-digit numbers like 1080 or 2160 in filenames (e.g. `video_1080p.mp4`) were being interpreted as years. Fixed with a pattern exclusion for common resolution suffixes.

### Bug Fixes

- **Stale video subtitles** — switching between videos in the full-screen viewer could leave subtitle text from the previous clip visible. Caption tracks are now cleared before loading a new video source.
- **Timeline showing during multi-select** — the Videos timeline continued showing scenes from a previously selected video when multiple videos were selected. The timeline now collapses when more than one video is selected.
- **Videos toolbar button colours** — hover and active states for the Videos toolbar button now use the correct accent colour.

### Other

- **Test helper scripts** — `tools/test-start.sh` and `tools/test-stop.sh` for quickly spinning up and tearing down isolated test instances with example images and videos.

---

## v1.2.1-beta.18

### Sequential Pipeline Orchestrator

The five concurrent processing threads (ingestion, embeddings, face detection, NIMA scoring, video processing) have been replaced with a single **PipelineOrchestrator** thread that runs seven stages sequentially in a loop:

1. **Ingestion** — walk registered folders, create/update DB records
2. **Thumbnails** — generate image thumbnails (2a) and detect video scenes + generate scene thumbnails (2b)
3. **Embeddings** — compute OpenCLIP vectors for images and video scenes
4. **Scoring** — NIMA + LAION aesthetic scores
5. **Faces** — MTCNN detection + InceptionResnetV1 embeddings
6. **Grouping** — directory groups, face reassessment, duplicate computation
7. **Transcription** — speech-to-text for video audio (when enabled)

**Why this matters:**

- **No GPU contention** — only one model is loaded at a time; each stage explicitly unloads its model before the next begins.
- **No database lock contention** — stages run sequentially, so the shared lock is only briefly held for reads/writes, never contested between stages.
- **Self-healing** — each stage queries the DB for incomplete rows (e.g. `embedding IS NULL`, `thumbnails_pending = 1`). If the process is killed mid-pipeline, restarting picks up exactly where it left off.
- **Simpler control flow** — no callback chains or inter-thread signalling between stages.

Nine CLI flags that manually triggered individual stages (`--detect-faces`, `--group-faces`, `--generate-thumbnails`, `--rebuild-duplicates`, etc.) have been retired — `--scan` now triggers the full pipeline automatically.

### Placeholder Thumbnails

All media now gets **placeholder thumbnails** (the Photonarium logo on a dark background) written to the cache immediately during ingestion. This means images and videos appear in the Gallery straight away rather than as blank spaces while waiting for real thumbnail generation or video scene detection.

A new `thumbnails_pending` database flag tracks which items still need real thumbnails, replacing the previous approach of checking for files on disk. Stage 2a overwrites placeholders with real thumbnails for images; Stage 2b generates scene thumbnails for videos (which live at a separate path).

### Database Screen Improvements

- **Per-folder rescan** — each folder row now shows a refresh button that rescans only that folder, complementing the existing global "Rescan local folders" button.
- **Adaptive status polling** — the Database screen now polls at 1-second intervals while processing and 5 seconds when idle, reducing CPU usage. The backend caches count queries with a 5-second TTL so most idle polls skip SQL entirely.

### Video Processing Improvements

- **Single-pass keyframe processing** — scene keyframe extraction and thumbnail generation are now merged into a single pass: each frame is decoded once, thumbnailed at both sizes, then released. This halves decode work and drops peak memory from N full-res frames to one.
- **Video rotation caching** — ffprobe rotation is now queried once per video instead of once per frame.
- **Progress reporting** — the Database screen shows real-time video processing progress with human-readable step detail (e.g. "Detecting scenes (1/4)", "Generating thumbnails (2/4)") instead of just a queue count.
- **Ingestion retry** — files that fail with transient "database is locked" errors during ingestion are re-queued up to 5 times, so a single rescan can self-heal without requiring repeated manual rescans.

### Cross-Screen Video Selection

Selecting a video in the Gallery and switching to the Videos screen (or vice versa) now preserves the selection — the same video is selected, its timeline is loaded, and the grid scrolls to it. Fullscreen launched from Videos now navigates only through videos visible on that screen (respecting any active filter), and closing fullscreen returns to the last-viewed video rather than the one originally opened.

### Bug Fixes

- **Database locking during video ingestion:** Ingestion workers shared a single SQLite connection with Flask threads, causing "database is locked" errors during video ingestion. Each worker now gets its own thread-local connection with WAL mode and 10-second busy timeout.
- **Video processing database locking:** Scene inserts, embedding updates, and transcription updates are now batched into single `executemany` + `commit` calls inside one lock acquisition, replacing per-row lock/unlock loops.
- **Partially-ingested videos not reprocessed:** The startup recovery query now catches videos where any scene has a null embedding, indicating the pipeline never completed.
- **Video scene detection showing no progress:** Stage 2b was not calling `_set_stage()`, so the frontend showed only "Updating" with no detail.
- **Video status showing 'undefined':** The progress dict used `basename` but the frontend expected `label`, and step names were internal identifiers instead of human-readable text.
- **Pipeline stage logging suppressed:** The pipeline module logger was not registered at INFO level, so all stage messages were silently dropped. Added time-throttled progress logging to long-running stages.

### Code Quality

- **British English consistency:** All comments, docstrings, log messages, and identifiers now use British English spellings (initialise, normalise, centre, colour, etc.) throughout the Python and JavaScript codebase. External API names (PIL `optimize`, CSS `color`, etc.) are preserved.
- **18-principle audit:** Systematic audit of all source against the project's key principles, with fixes for all findings — including vectorising an O(n²) face grouping loop, extracting a shared `sharpen_thumbnail()` utility, batching face unassign/suppress endpoints, adding OOM guards to face grouping and NIMA scoring, and replacing technical UI labels with user-friendly text.

## v1.2.0-beta.17

### Video Support

Photonarium now manages videos alongside images as a first-class feature. A new **Videos** screen provides a dedicated space for browsing, searching, and managing video content.

**Videos screen:**

- **Video grid** - a thumbnail grid of all videos (or search results), using the same VirtualGrid, selection model, and keyboard shortcuts as the Gallery. Each card shows the preferred scene thumbnail, a duration badge, and a match score badge when searching. Thumbnail size is adjustable independently from the Gallery.
- **Scene timeline** - selecting a video reveals a horizontal strip of scene keyframe thumbnails below the grid. Scenes are proportionally sized by duration, giving an intuitive sense of the video's structure at a glance. Each scene shows a timecode and a preferred star.
- **Drag-to-scroll and minimap** - long timelines scroll horizontally with click-and-drag. A minimap bar appears below, showing the full duration with time ticks and a draggable viewport indicator. Click anywhere on the minimap to jump; gradient indicators at the edges signal hidden content. The minimap hides automatically when everything fits on screen.
- **Video search with heatmap** - a three-way search mode toggle (Images / Videos / All) on the Search screen lets you search specifically for video content. "Videos" mode performs scene-level semantic search and navigates to the Videos screen with a per-scene heatmap overlay (blue → yellow → red) showing match strength across the timeline. The minimap also displays a smoothed heatmap gradient in search mode.
- **Preferred scenes** - each video has a preferred scene that represents it across the app (Gallery thumbnail, Videos grid, search ranking in "All" mode). Click the star on any scene to change it. The first scene is the default.
- **Sorting** - videos can be sorted by date, rating, or content similarity. Sort by similarity works like the Gallery: select a video, click the similarity button, and all videos re-order by visual similarity to the selected one. When searching, videos sort by match score.
- **Double-click scenes** to open the video in the full-screen viewer, seeked to the scene's start time, with autoplay.

**Scene detection:**

- Uses ffmpeg's `select` filter for cut detection, with automatic subdivision of long scenes at configurable intervals (`video_max_scene_duration`, default 8 seconds). Short, natural scene lengths (3–8 seconds) produce more useful timelines than the previous 30-second default.
- If ffmpeg isn't available or finds no cuts, the video is uniformly subdivided.

**Video processing pipeline:**

- The Database screen shows real-time video processing progress with step detail (e.g. "Detecting scenes (1/6)", "Computing embeddings (4/6)") instead of just a queue count.
- Videos are processed through scene detection, keyframe extraction, OpenCLIP embedding, and (where audio is present) speech transcription.

**Transcriptions:**

- Automatic speech-to-text for video audio. Transcription text is displayed below the scene timeline, labelled by timecode.
- Transcriptions are semantically searchable - searching for something someone said in a video finds matching scenes.

**Videos in the Gallery and full-screen viewer:**

- Videos appear alongside images in the Gallery, with their preferred scene as the thumbnail and a duration badge.
- In the full-screen viewer, face tagging and rotate buttons are visually greyed out for videos. Videos autoplay in slideshow mode, advancing after playback ends or the hold time elapses (capped at 30 seconds).

### Videos Screen Theming and Polish

- **Orange accent** - video card labels and minimap tick labels use the Videos screen accent colour (orange), consistent with each screen having its own accent.
- **Contrast improvements** - selected video card labels darken for readability; minimap tick labels and timeline stars use page-background text outlines so they blend naturally in both light and dark themes.

### Documentation Overhaul

- **Sub-documents** - the monolithic README has been split into focused guides under `docs/`: [Gallery](gallery.md), [Full-screen viewer](fullscreen.md), [Search](search.md), [Videos](videos.md), [Groups](groups.md), [Faces](faces.md), [Database](database.md), [Installation](installation.md). The README is now a concise overview with links.
- **Feature coverage** - updated documentation to cover light/dark themes, similarity sort (Gallery and Videos), image histogram, auto-captioning, video transcriptions, Docker/NAS deployment, and the 100k+ image scalability of the thumbnail grid.
- **Competitive comparison** - updated the background document with video management and scene-level search rows in the comparison table.

### Bug Fixes

- **Scene detection on Windows:** The previous `lavfi`/`movie` approach silently failed on Windows paths (backslash escaping), causing all videos to fall back to uniform segments. Replaced with `ffmpeg -i` which handles paths natively.
- **Preferred scene not updating in search mode:** Setting a preferred scene while searching updated `AppState.images` but not the separate `_searchResults` object, so the star didn't visually update. Both are now kept in sync.
- **Timeline drag-to-scroll:** `overflow-x: hidden` prevented programmatic scrolling; switched to `overflow-x: auto` with hidden scrollbar. Also prevented native drag on scene thumbnails which could swallow click events.
- **Search input selector:** Fixed `#filter-description` → `#filter-text` so video search queries are correctly read from the search input.

## v1.1.6-beta.16

### Smarter Filename Date Parsing

Photonarium now uses a scoring-based date parser that handles a much wider range of filename and folder naming conventions. The parser evaluates multiple possible interpretations of each path and picks the best one based on confidence scoring.

**New patterns recognised:**

- **Human-style month names:** "February", "Feb", "febr", and similar abbreviations
- **Seasons:** "Summer 2006", "Winter 2023" (mapped to the season's start month)
- **Holidays:** "Christmas 2019", "Xmas", "Halloween", "NYE" (mapped to the holiday date)
- **Position words:** "early May", "mid June", "late December" (mapped to day 1/15/28)
- **Apostrophe years:** "Feb'03" → February 2003
- **Year-only folders:** "Photos/2019/" fills in January 1st as a best guess
- **WhatsApp timestamps:** "WhatsApp Image 2026-01-06 at 12.33.29.jpeg" now correctly parses the separated time (HH.MM.SS)

**Ambiguous date handling (date_order config):**

A new `date_order` setting (DMY/MDY/YMD) controls how ambiguous numeric dates like "07-03-2024" are interpreted. This is a preference, not absolute — if the preferred interpretation produces an invalid date (e.g. month 13), a valid alternative is used automatically. The setting appears as a dropdown in the Settings dialog.

**Filename date overrides:**

A new `filename_date_overrides` config setting lets you map filename patterns (like `WhatsApp Image *`) to specific strftime formats, overriding the automatic parser for known naming conventions.

**Camera sequence number fix:**

Digit runs directly preceded by letters (e.g. "DSC0042", "IMG0001") are no longer misinterpreted as timestamps.

### Timestamp Recomputation on Rescan

When you rescan local folders, Photonarium now automatically recomputes timestamps for all images that weren't manually dated by the user. This means images that previously fell back to filesystem dates or "unknown" will benefit from the improved parser on their next rescan — no re-import needed.

### Bug Fixes

- **DB concurrency errors during rescan completion:** The post-processing completion callback (duplicate computation, face grouping) ran on the shared database connection without proper locking while the NIMA scoring thread was still active, causing "another row available" and "database is locked" errors on large libraries. Fixed by giving duplicate computation its own private connection and adding lock protection to face semantic embedding backfill.
- **Premature completion callbacks during rescan:** Three intertwined bugs caused background threads to fire completion callbacks before work was finished: (1) all queues being momentarily empty at startup triggered spurious completion, (2) the `_on_complete_finished` gate flag was never reset between rescans, and (3) errors in completion callbacks left uncommitted transactions that blocked subsequent database access. Fixed with a `has_started_processing` guard, proper flag resets (including in `queue_rescan_all()`), and `conn.rollback()` in error handlers.
- **Debug logging not visible in frontend log viewer:** The DatabaseLogHandler was attached after `get_db()` returned, so all startup messages (thread starts, scanning, model loading) were already emitted and missed. Split database initialisation to attach the handler between table creation and startup. Also added `busy_timeout` to the handler's SQLite connection to prevent silent INSERT failures during heavy write contention.
- **YAML serialisation of list config values:** List-type config fields (like `filename_date_overrides`) were serialised using Python repr instead of valid YAML. Now renders as proper YAML block lists with correct quoting.
- **list[str] config values not saved from UI:** The settings save endpoint didn't handle `list[str]` fields, causing them to fall through to the default handler. Added explicit coercion.

### Developer / Debugging

- **`--debug` CLI flag:** Enables DEBUG-level logging for all application modules. Debug output appears on both the console and the in-app log viewer (select "Debug" from the level filter dropdown).

### Docker

- Improved Docker build caching and pip download resilience.
- Added container update notification guidance to README.

## v1.1.5-beta.15

### Bug Fixes

- **Content similarity sort not re-triggerable:** Clicking "Sort by content similarity" with a different image selected did nothing because `setSortBy('content')` was a no-op when already on content sort. Added a `{ force }` option to `setSortBy()` so the button re-evaluates the reference image and re-fetches similarity scores.
- **Content similarity button enabled without valid selection:** The button was clickable with no selection or multiple images selected, producing an error toast. It is now disabled unless exactly one image is selected, matching the "Clear filter" button's disabled style.
- **Face detection completing before its queue was populated:** `FaceDetectionThread` raced with `EmbeddingThread`'s completion callback — the face thread would check its (empty) queue and fire its completion callback before `on_embedding_complete()` had populated the face queue. This caused face detection to run after duplicate computation instead of before it. Fixed by adding a flag so the face thread waits until the embedding callback has finished before checking queue emptiness.
- **"Database is locked" errors during image indexing:** Six database access sites in background threads were not protected by `_db_lock`, causing SQLite contention during sustained ingestion writes. Wrapped all six sites (embedding batch collection, face prefetch thread pool, face legacy path, face queue population, NIMA queue, and NIMA model invalidation).

### Code Quality

- **Deduplicated helpers:** Extracted `parse_exif_datetime()` into shared `app/exifutil.py` (was duplicated in `metadata.py` and `rawimage.py`). Added `sql_placeholders()` helper in `app/dbutil.py`, replacing 16 inline `','.join('?' * len(...))` patterns.
- **Named constants:** Replaced magic numbers with named constants in `thumbnails.py`, `app.py`, `faces.py`, and `events.js`.
- **Silent catch logging:** Added `logger.debug()`/`console.warn()` to previously silent `except`/`catch` blocks across backend and frontend modules.
- **Documentation:** Updated `imagedb.py` module docstring to reflect current codebase (event polling, 6 group levels, 4 threads, faces/people schema).

## v1.1.4-beta.14

### Management Screen

The Database screen (now titled "Management") has been expanded with tools for administering headless and Docker deployments without needing terminal access.

**Server Log Viewer:** A **View Logs** button opens a dialog showing recent server log output.

- **Database-backed storage:** Log entries are written to a `logs` table in the SQLite database via a dedicated logging handler, independent of the main database connection.
- **Colour-coded levels:** ERROR (red), WARNING (amber), INFO (green), and DEBUG (muted) entries are visually distinct in both light and dark themes.
- **Level filtering:** A dropdown filters log entries by severity level.
- **Configurable retention:** The `log_retention_lines` setting (100-100,000, default 1,000) controls how many lines are kept. Set to 0 to disable database logging entirely. Old entries are trimmed automatically.
- **Fail-silent design:** The log handler never crashes the application — all writes are wrapped in exception handlers, and the handler is only attached after the database is fully initialised.

**Server Restart:** A **Restart** button lets you restart the backend process directly from the UI — useful for applying settings changes on headless or Docker deployments.

- Uses `os.execv()` to replace the process in-place, preserving the PID and all original CLI arguments. This works in bare-metal and Docker (PID 1 preserved) deployments without external orchestration.
- The frontend shows a "Restarting..." overlay, polls `/api/health` until the server is back, and auto-reloads the page. Times out after 30 seconds with an error message.

**Toolbar refresh:** The Management screen toolbar icon has been changed to a cog and repositioned as the left-most button for quicker access.

### Screen-Accent Thumbnail Labels

Thumbnail text labels now use each screen's signature toolbar colour for at-a-glance screen identification:

- **Gallery:** Image filenames shown in blue (matching the Gallery toolbar button)
- **Groups:** Custom group and directory names shown in purple (matching the Groups toolbar button)
- **Faces:** Person names in the People list shown in green (matching the Faces toolbar button)

### Bug Fixes

- **Noisy console logging for event polling:** The `[API] GET /events?since=...` message appeared in the browser console every 2 seconds because the polling filter compared the full URL (with query params) against a bare path. Switched to `startsWith()` matching so polling endpoints are correctly suppressed.

### Documentation

- **Docker quick start:** Added `--detect-faces` to the Docker quick start examples so face detection runs on first launch.

## v1.1.3-beta.13

### Graceful OOM / Low-Memory Handling

Memory-intensive code paths now fail gracefully instead of silently crashing the processing thread when RAM is insufficient. This is particularly relevant for deployments on memory-constrained systems such as Proxmox LXC containers or small NAS devices.

**Model loading protection (4 sites):**

All four ML model load sites (OpenCLIP, BLIP/BLIP-2, MTCNN, InceptionResnetV1) are now wrapped in OOM-aware try/except blocks. On failure, the app logs a clear error message identifying the model, sets a "load failed" flag to prevent retry loops on every request, and disables the dependent feature (e.g. semantic search, captioning, face detection) rather than crashing.

**Batch inference OOM retry (3 sites):**

The three `torch.stack().to(device)` batch inference sites (image embeddings, NIMA scoring, face embeddings) now catch OOM errors and automatically fall back to single-item processing. This maximises progress — if a batch of 16 images fails, each is retried individually, and only truly impossible items are skipped.

**Chunked similarity search:**

The `np.vstack` call in `_get_images_by_similarity()` previously loaded ALL image embeddings into a single matrix (~200MB for 100k images). This is now processed in chunks of 10,000 embeddings (~20MB each), reducing peak memory allocation.

### Documentation

- **CLAUDE.md gitignored:** `CLAUDE.md` is deliberately excluded from version control (listed in `.gitignore`). It contains project context for Claude Code and is maintained locally by each developer. It is not part of the distributed source.

## v1.1.2-beta.12

### Docker Build Improvements

**Shared base image for DockerHub layer sharing:**

Docker builds have been split into a base image (`Dockerfile.base`) and per-variant images (`Dockerfile`). The base image contains ML models, system dependencies, and torch-independent Python packages (~2.5GB). Variant images extend the base with only PyTorch and application code. This means variants share a single base layer on DockerHub instead of each uploading ~4-10GB independently. Requirements have been split into `requirements-base.txt` and `requirements-ml.txt` accordingly.

**Layer optimization for faster rebuilds:**

Within each image, ML models are placed in the first layer, independent of pip installs and application code:

- **Before:** Any code change triggered a full rebuild including model re-processing (~10+ minutes).
- **After:** Code changes only rebuild the final ~3MB layer (~seconds). The model layer stays cached.

| Layer | Contents | Size | Rebuilds when... |
|-------|----------|------|------------------|
| 1 | ML models (HuggingFace + PyTorch) | ~2.6GB | Model defaults change |
| 2 | PyTorch | 1-4GB | CUDA variant changes |
| 3 | pip requirements | ~500MB | requirements.txt changes |
| 4 | Application code | ~3MB | Any code change |

**New build workflow:**

```bash
make models     # Downloads ~2.5GB models (once, or if config.py changes)
make base-x64   # Builds shared base image
make build       # Builds CPU variant (extends base)
```

The Makefile uses marker files for proper dependency tracking. Build targets fail with a helpful error if models haven't been pre-downloaded.

**FaceNet models now pre-downloaded:**

The FaceNet face detection (MTCNN) and recognition (InceptionResnetV1) models are now included in the Docker image. Previously, these ~107MB models were downloaded on first container start, requiring internet access. Now all models are baked in and the container works fully offline from the first run.

**Version display in Docker containers:**

A `VERSION` file (tag, commit hash, build date) is written during Docker builds so the app can display version information in headless mode where git is not available. Development installs continue to use git directly.

**Eliminated model duplication on first run:**

Previously, the entrypoint copied ~2.6GB of HuggingFace and FaceNet models from the image to the `/config` volume on first start, doubling disk usage and requiring extra storage. Models now stay in the image and are accessed directly via `HF_HOME` and `TORCH_HOME` environment variables. Only the small LAION/NIMA weights (~11MB) are copied to `/config`.

**Fixed numpy crash on startup:**

Docker's union filesystem was causing a `cannot load module more than once per process` error when numpy was installed in both the base and variant image layers. Fixed by moving all numpy-dependent packages (pillow, opencv, imagehash, rawpy) from the base image to the variant image, so numpy is only installed once — after PyTorch.

**Makefile push/rebuild improvements:**

- Per-variant push targets (`make push-latest`, `make push-cu118`, etc.) for pushing individual images without triggering builds of other variants.
- Base image is now an order-only prerequisite for variant builds, so rebuilding the base no longer cascades to all variants.

### Bug Fixes

- **Status endpoint crash on fresh databases:** The `/api/status` and `/api/stats` endpoints crashed with `TypeError: 'NoneType' object is not subscriptable` on a brand-new database because the `people` and `faces` tables (created by FaceDB) might not exist yet when the frontend first polls. Now returns zero counts gracefully.

### Website

- Docker added as a primary call-to-action button in the hero section.
- Docker icon added to the top navigation alongside GitHub.
- New "Docker & NAS Ready" feature card; feature grid consolidated to a 3x3 layout.
- Getting Started section split into Docker and Manual Installation paths.
- DockerHub link added to footer.

### Documentation

- **NAS photo syncing guide:** README now explains how to sync photos to a NAS using vendor tools (Synology Cloud Sync, QNAP Qsync, etc.) before indexing with Photonarium.
- **BACKGROUND.md updated:** Added Docker/NAS deployment information and negative semantic search as a differentiator.
- **Building from Source:** README now documents the `make download-models` prerequisite step.
- **Proxmox LXC guide:** README now includes setup instructions for running Docker inside a Proxmox LXC container, including bind-mount configuration and disk space requirements.
- **Memory tuning guidance:** Expanded memory considerations section with specific settings to reduce for constrained systems.

## v1.1.1-beta.11

### Docker Support

Photonarium can now run as a Docker container, making it easy to deploy on NAS devices (Synology, QNAP, Unraid) or any system with Docker installed.

**Pre-built images** are available on DockerHub at `7thsw/photonarium`:

| Tag | Size | Best For |
|-----|------|----------|
| `latest` / `cpu` | ~4.5 GB | Most NAS devices, systems without a dedicated GPU |
| `cu118` | ~8 GB | NVIDIA GTX 10-series, RTX 20-series (CUDA 11.8) |
| `cu126` | ~10 GB | NVIDIA RTX 30-series, 40-series (CUDA 12.6) |
| `cu128` | ~10 GB | NVIDIA RTX 50-series / Blackwell (CUDA 12.8) |
| `intel` | ~5 GB | Intel integrated graphics (Celeron/Atom NAS CPUs) |
| `arm64` | ~4 GB | ARM64 systems (Raspberry Pi 4/5, Apple Silicon) |

The CPU and CUDA images are x86_64 only. Use the `arm64` tag for ARM-based systems.

**Key features:**

- **All models pre-downloaded:** ML models are baked into the image, so you can start using Photonarium immediately with no internet required after the initial pull.
- **NAS-friendly permissions:** PUID/PGID environment variables let the container run as your NAS user, avoiding filesystem permission issues.
- **Hardware acceleration:** Docker Compose overlay files for NVIDIA GPUs (via Container Toolkit) and Intel iGPUs (via `/dev/dri`).
- **Headless mode:** Desktop-only features (folder picker dialogs, reveal in explorer) are hidden by default in Docker since they don't work without a display.
- **Scheduled rescans:** The `scan_interval_minutes` setting enables automatic background rescans, useful for NAS setups where photos sync continuously via cloud services.
- **OCI version labels:** Images include standard metadata (version, git commit, build date, variant) so you always know which release you're running.

**Quick start:**

```bash
docker run -d \
  --name photonarium \
  -p 5000:5000 \
  -v ~/photonarium/config:/config \
  -v ~/photonarium/catalogue:/catalogue \
  -v /path/to/your/photos:/photos:ro \
  -e PUID=$(id -u) \
  -e PGID=$(id -g) \
  7thsw/photonarium:latest \
  --add-folder /photos --scan
```

The `--add-folder` flag registers the photo directory (only needed on first run). The `--scan` flag triggers indexing. The `--add-folder` flag is needed because Docker runs in headless mode, which hides the "Add Folder" button (native folder picker dialogs don't work without a display). The folder list and Rescan button remain available in the web UI.

See the [Docker Installation](#docker-installation) section in README.md for complete setup instructions, including Docker Compose examples, GPU acceleration, performance tips, and updating.

### Quality Scoring: Absolute Aesthetics

The Quality sort now uses absolute aesthetic scores instead of percentile ranks:

- **Before:** The aesthetic component (60% weight) was percentile-ranked within the current image set, so a score of 0.8 meant "better than 80% of these images".
- **After:** The aesthetic component uses the raw NIMA/LAION model output (divided by 10 to normalise to 0-1), so a score of 0.6 reflects a genuine 6/10 from the models.

This change means quality scores are now comparable across different sets of images. Previously, viewing a group of all excellent images would spread them from 0% to 100%; now they'll cluster near their true aesthetic value.

Sharpness, resolution, and compression quality remain percentile-ranked since they have no natural absolute scale.

### Documentation

- **Comprehensive Docker guide:** README.md now includes a full Docker Installation section covering quick start, image variants, Docker Compose, hardware acceleration (NVIDIA and Intel), configuration, performance tips (SSD for database, network storage for photos), auto-scanning, and updating.
- **Licence documentation:** Added LICENSES.md documenting all third-party dependencies and their licences (Apache 2.0, MIT, BSD, PSF).

## v1.1.0-beta.10

### Toolbar Colour Overhaul

The toolbar has been visually refreshed with meaningful colour cues throughout:

- **Always-visible navigation:** Screen buttons (Gallery, Database, Groups, Search, Faces) are now always shown. The current screen's button displays in its signature colour (blue, pink, purple, teal, green) rather than being hidden entirely.
- **Active toggle colours:** Sort buttons highlight yellow when active, face filter buttons highlight green, and the search button highlights teal - making it easy to see which toggles are on at a glance.
- **Hover colours:** Most toolbar buttons now show a themed background tint on hover - blue for slideshow/shuffle/fullscreen, red for trash/delete, purple for group new/rename, teal for refine/clear-filter, indigo for selection controls, and orange for rotation.
- **Group filter repositioned:** The group name filter input has been moved next to the Refine button, starts narrower, and smoothly expands on focus.

### Clearer Database Screen Labels

The Database screen now distinguishes between its two modes of adding images more clearly:

- **Import Images** - "copies photos into your catalogue, organised by date"
- **Local Indexed Folders** - "photos stay where they are, nothing is moved or copied"

Button labels updated to match: "Add Local Folder" and "Rescan Local Folders". Short explanatory hints appear next to each section heading.

### Other Improvements

- **Version in toolbar:** The Database screen now shows the Photonarium version (git tag or short commit hash with date) next to the logo.
- **Friendlier status labels:** The processing queue label "Embedding" has been renamed to "Classifying" - less technical for end users.
- **Tutorial updated:** Screenshot script updated for all beta 9 changes (slideshow/shuffle/trash buttons, Refine groups, import labels, Quick Match percentages). Mouse cursor is now parked in a neutral location before each screenshot to prevent misleading hover highlights on toolbar buttons.

### Bug Fixes

- **Fullscreen hover invisible on black:** Hover tint on fullscreen viewer controls (buttons, navigation arrows) was darkening from black to darker black - invisible when viewing a portrait image on a landscape display with black bars. Now uses a white tint that visibly lightens buttons regardless of backdrop.
- **Search button falsely highlighted:** The Search screen navigation button showed a teal "active" highlight whenever a filter was active, even when viewing a different screen. This made it look like two screens were selected simultaneously. The Clear Filter button already indicates an active filter, so the redundant Search button highlight has been removed.
- **Preflight dedup docs corrected:** Documentation incorrectly stated that client-side import dedup uses SHA-256 checksums. It actually uses file basename and size pairs for the preflight check (the backend's ImportWorker still uses SHA-256 as a safety net).

## v1.0.9-beta.9

### Import into Catalogue

A new Import feature lets you copy images from external sources (SD cards, phone uploads, downloads) into a Photonarium-managed catalogue directory, organised by date. Set `catalogue_dir` in settings to enable it.

- **Date-based organisation:** Imported files are stored as `catalogue_dir/YYYY/YYYY-MM-DD/filename.jpg` using the EXIF timestamp (or file modification time as a fallback).
- **Desktop import:** Drag and drop files or folders onto the import drop zone on the Database screen, or use the Pick Folder / Pick Photos buttons. When dropping a folder, a choice dialog lets you reference it in place (Add Folder) or copy its contents into the catalogue (Import).
- **Mobile import:** Pick Photos opens the system photo picker (iOS Camera Roll, Android Files). On Android, a Pick Folder button is also available via the `webkitdirectory` API.
- **Preflight dedup:** Before uploading from a browser, file names and sizes are sent to the backend for a fast duplicate check against existing images. Only new files are transferred, saving bandwidth when re-importing folders that partially overlap with the existing library. The backend's SHA-256 dedup in the ImportWorker catches any edge cases that slip through.
- **Backend dedup:** The ImportWorker also checks checksums server-side (for the desktop path and as a safety net for uploads), skipping files already in the library.
- **ImportWorker:** Modeled on TrashWorker - daemon thread, queue-fed with `ThreadPoolExecutor` for parallel file copying (`import_threads` setting, 1-16, default 4). Progress is shown on the Database screen. Unfinished items are persisted to `.pending_import.json` on shutdown and recovered on restart.
- **Auto-registration:** The catalogue directory is automatically registered as a watched folder. It is shown with a catalogue badge and cannot be removed from the folder list.
- **No new dependencies:** Uses stdlib `shutil.copy2` and existing `hashlib`.

### Refine Groups

The old Prune feature (levels 0-3 only, trash-only) has been replaced by a general-purpose Refine Groups tool available at all 6 group levels (including directories and custom groups).

- **Quality filtering:** Choose how many images to keep (or trash) per group - best only, top N, or top N%. Uses the same composite quality scoring as the Gallery Quality sort.
- **View in Gallery:** The primary action opens the selected subset (best or worst images) as a filtered Gallery view without changing anything. Works for all group levels including directories and custom/smart groups.
- **Trash:** An optional secondary action (levels 0-4) moves the non-kept images to the trash directory. Custom groups and smart groups are view-only since their membership is user-curated or dynamically evaluated.
- **Smart group support:** For smart groups, the refine dialog resolves filter criteria asynchronously (including live semantic search if needed) and caches the resulting image IDs to avoid double evaluation.

### Gallery Slideshow Buttons

The Gallery toolbar now has slideshow (play) and shuffle buttons, in addition to the existing fullscreen toolbar buttons. The buttons are selection-aware:

- **No selection:** Plays all images in the current sort order.
- **Single selection:** Starts the slideshow from that image.
- **Multiple selection:** Plays only the selected images.

### Apple MPS Device Support

PyTorch device selection now detects Apple Silicon GPU acceleration (MPS) in addition to NVIDIA CUDA. All four ML model sites (OpenCLIP embeddings, NIMA aesthetic scoring, face detection, and image captioning) use the priority order CUDA > MPS > CPU. The BLIP/BLIP-2 captioning models also use float16 precision on MPS (previously only on CUDA), improving performance on Apple Silicon.

### Repository Restructure

The project source tree has been reorganized into two top-level directories for cleaner separation of application code and development tooling:

- **`app/`**: Python backend (app.py, imagedb.py, faces.py, etc.) and the frontend (`app/static/`).
- **`tools/`**: Development and CI/CD tooling (ruff.toml, eslint.config.mjs, jsconfig.json, package.json, compat-test/, mktutorial/).

The application is now launched with `python app/app.py` instead of `python app.py`. Data files (database, thumbnails, models, trash) continue to live in the OS data directory, not the repository root. The installer scripts have been updated accordingly.

### Tutorial Automation

The tutorial generator (`tools/mktutorial/tutorial.py`) now supports a `--setup` mode that automates the initial data preparation - replacing manual steps that previously had to be done before tutorial generation:

- **Automated setup:** `--setup` creates the tutorial config, downloads ML models, starts the server against an empty database, captures the Getting Started screenshots via Playwright, adds the example image folder, and waits for all image processing (indexing, embeddings, faces, duplicates, NIMA scoring) to complete.
- **Composite screenshots:** The OS folder picker dialog (which cannot be automated as a native widget) is composited from a manually-provided overlay onto an automatically-captured background screenshot using Pillow.
- **Deterministic face identification:** Section 6 (Faces) now uses stable selectors based on image filenames and face bounding-box positions rather than DOM order, making the face identification steps reproducible across different processing orders.

### Bug Fixes

- **People disappearing from Known People section:** When the originating client received its own mutation events back via event polling, `handleFacesChanged` called `people.invalidate()` which triggered a full cache reload - wiping the optimistic face counts that had just been set. Additionally, `autoUpsert` in the people handler overwrote the optimistic `face_count` with a stale backend snapshot (captured at person creation time, before faces were assigned). Fixed by replacing cache invalidation with incremental reconciliation and stripping the derived `face_count` field from backend event payloads.
- **Orphaned faces after trashing images:** Trashing images only soft-deleted them (`deleted = 1`) without cleaning up associated face records, because the CASCADE DELETE foreign key only fires on hard `DELETE` statements. Orphaned faces continued to appear on the Faces screen, in people's face lists, in auto-recognition, and in semantic search. Fixed by hard-deleting face records during trash, adding `deleted = 0` filters to all face queries, deleting orphaned face thumbnails, and emitting face/people change events for multi-client sync. When the trashed image contained a person's preferred face, the replacement is now chosen by embedding similarity to the old preferred rather than arbitrarily.
- **Quick Match popup jumping to top-left corner:** When a face reassessment event triggered a grid refresh while the Quick Match popup's async match fetch was in flight, the anchor element was detached from the DOM. The subsequent repositioning call read `getBoundingClientRect()` on the detached element (returning zeros), jumping the card to the top-left corner.
- **Stale person thumbnails in Quick Match results:** The Quick Match popup used a raw thumbnail URL without cache-busting, so after changing a person's preferred face, the popup continued to show the old face until the browser cache expired.
- **Face auto-matching never worked:** Three bugs prevented automatic face recognition from assigning detected faces to known people. First, detection-time matching used only the global threshold, ignoring per-person thresholds (so a person with a relaxed 70% threshold was still held to the global 92%). Second, both matching paths used a single-best-match approach - if the closest known face belonged to a person whose threshold was not met, no fallback was tried, even when other people would have matched. Third, faces belonging to the ignored person ('-') could "steal" the best match from named people. Fixed by grouping matches by person, trying each in descending similarity order against their per-person threshold, and partitioning named people before ignored so named matches are always preferred.
- **Gallery not refreshing after import:** The Gallery only updated at `processing_complete`, which fires after face detection and duplicate grouping - often 1-2 minutes after images were actually ready to view. Added an `images_indexed` event that fires as soon as embeddings complete, so imported or newly scanned images appear in the Gallery within seconds.

### Improvements

- **Quick Match similarity scores:** The Quick Match popup now shows match confidence as a percentage next to each person name (e.g. "Alice (83%)"), making it easier to judge match quality.
- **Faster face reassessment after scanning:** Face reassessment (auto-matching unknown faces to known people) now runs immediately after face detection, before the slower duplicate grouping phase. Previously it ran last, adding an unnecessary delay before newly scanned faces were identified.
- **Gallery trash button:** A toolbar button for moving selected images to trash, making deletion accessible on mobile devices where the Delete key is unavailable. The button is disabled when no images are selected.

## v1.0.8-beta.8

### Slideshow Mode

The full-screen viewer now supports slideshows with smooth cross-fade transitions between images.

- **Two playback modes:** Linear (in the current sort order) or shuffled (Fisher-Yates random).
- **Toolbar and keyboard:** Play/shuffle buttons in the full-screen toolbar, or press Space to start. Space pauses/resumes, Escape exits, arrow keys skip manually.
- **Configurable timing:** The hold duration defaults to 5 seconds and can be changed via the `slideshow_interval` setting in `photonarium.yml` (1-60 seconds).
- **Groups integration:** Hover over any group stack on the Groups screen to reveal play and shuffle badges that jump straight into a slideshow scoped to that group.
- **Preloading:** The next image is preloaded during the hold period so it is browser-cached before the cross-fade begins, eliminating flashes of black or stale images. In shuffle mode, the actual shuffle-next target is preloaded (not just the index-adjacent image).

### Smart Groups

Smart Groups are saved searches with dynamic membership. Instead of manually adding images to a group, you define filter criteria (text, date range, rating, people, metadata) and Photonarium evaluates them each time you open the group - so new photos that match your criteria appear automatically.

- **Create from Search:** Set up filters on the Search screen and click "Save as Smart Group". Enter a name and the group appears alongside your regular custom groups.
- **Dynamic evaluation:** Opening a Smart Group runs a fresh filter evaluation. If the filter includes a text search, this runs a live semantic search each time.
- **Edit in place:** An edit badge appears on hover. Click it to return to the Search screen with the saved criteria pre-loaded, modify them, and click "Update Smart Group".
- **Preview thumbnails:** Smart Group stacks show a representative thumbnail that updates automatically. Trashing the preview image triggers a fresh evaluation to pick a new one.
- **Group picker exclusion:** The Gallery's "Add to Group" dialog only shows regular groups, since adding static images to a dynamic group does not make sense.
- **Schema:** Adds `filter_json` and `preview_image_id` columns to `custom_groups` via migration. Existing custom groups are unaffected (both columns are NULL for them).

### Fullscreen Sync with External Deletions

The full-screen viewer now subscribes to image changes from AppState, so images trashed by other clients (or other browser tabs) are pruned from the navigation list in real time. If the currently-displayed image is trashed externally, fullscreen closes immediately. Slideshow state (position, shuffle order) is rebuilt after pruning.

### Search UX Improvements

- **People filter moved up:** The People section now sits directly below Description on the Search screen, making it harder to overlook.
- **Name-only warning:** A warning triangle appears inside the description input when it contains only recognised people names with no other descriptive text. This catches cases where the semantic search would receive an empty query after name extraction.

### On This Day Improvements

- **Full context in Gallery:** "View in Gallery" now shows all images matching the month and day across all years, not just the cherry-picked highlights from the album. This gives wider context around the photos you just saw.

### Damaged Smart Group Detection

Smart Groups that reference people in their filter criteria are now tracked for staleness. When a person is deleted (from any path - direct deletion, merge, dissolve, face cleanup, etc.), any Smart Group whose filter references that person is flagged as "damaged". A warning icon and amber label appear on the group's stack in the Groups screen, with a tooltip explaining the issue. Opening a damaged group still works (it just skips the missing person), and editing the filter clears the damage automatically since stale person references are pruned on load.

### Bug Fixes

- **Stale people in Search filter:** Deleting a person on one client left stale references in the Search screen's people chips, auto-added tracking set, and active filter on other clients. The people subscriber now prunes deleted people automatically.
- **Gallery not showing new images after rescan:** The Gallery skipped its delta sync when no explicit refresh was requested, so newly scanned images only appeared after a full page reload.
- **Face auto-recognition broken:** Async face reassessment crashed on every run with a Row attribute error, so automatic spread of face identities to other photos never worked. Additionally, sync reassessment during scanning emitted incomplete event payloads, so faces detected during a rescan were never auto-identified against known people.
- **Rotation left temp files in photo directories:** Image rotation used the photo's own directory for temporary files. Cloud sync services (Dropbox, OneDrive) would pick these up before the rename completed, creating orphaned 0-byte files. Temp files are now written to the system temp directory instead.
- **OpenCLIP thread-safety race:** Concurrent requests could hit a window where the CLIP model was loaded but the tokenizer was not yet initialised, causing smart group preview evaluation and search to crash. Fixed with double-checked locking.

## v1.0.7-beta.7

### "On This Day..." Photo Album

When you return to Photonarium after a long absence (8+ hours), it checks whether any photos in your library were taken on today's date across multiple years. If so, a nostalgic album overlay fades in - scattered photos on textured paper with a wire ring binder and coffee ring stain. You can dismiss it or click "View in Gallery" to see just those images as a filtered set. The album shows at most once per calendar day and can be disabled via the `on_this_day_enabled` config option.

### Offline-Safe Icons

Material Symbols icons now degrade gracefully when the Google font has not loaded (e.g. on a fresh install with no internet). A new `App.icon()` helper renders Unicode fallback glyphs that are upgraded to the real font icons once the stylesheet loads. This replaces 20+ sites that previously showed blank squares when offline.

### Consolidated Reveal Endpoint

The two separate "reveal in file manager" endpoints have been merged into a single `POST /api/reveal` that accepts a target parameter (`image`, `config`, or `trash`). The Database screen now shows a clickable "Trashed" stat that opens the trash directory directly.

### Bug Fixes

- **Selection lost when clearing a group filter:** Clearing a filter (e.g. leaving a duplicate group) was wiping the gallery selection. The selection is now preserved and the gallery scrolls back to the selected image.
- **Improved settings tooltips:** The vague "may require reconfiguration" warning on dangerous settings fields has been replaced with specific guidance - `data_dir` warns about losing the database and thumbnails, `server_port` notes that bookmarks and open tabs will need updating.

## v1.0.6-beta.6

### Async Trash System

Image trashing has been reworked from synchronous file moves to an asynchronous background worker with parallel I/O:

- **TrashWorker:** Images are soft-deleted immediately (removed from the UI) and file moves run in a background thread using a configurable thread pool (`trash_threads` setting, 1-32, default 8).
- **Live progress:** The Database screen shows a progress row while trash moves are in flight.
- **Crash recovery:** Unfinished queue items are persisted to `.pending_trash.json` on shutdown and recovered on next startup, so no files are lost.
- **Multi-client notifications:** Trashing now emits `groups_changed` events for every affected duplicate level, so other browser tabs see dissolved groups and updated counts immediately.

### Prune Dialog: Keep/Trash Toggle

The prune dialog (Groups screen) now supports both "keep the best" and "trash the worst" semantics:

- **Clickable legend:** The "Keep per group" heading is now a toggle button. Click it to switch to "Trash per group" mode and back.
- **Inverted labels:** In trash mode, "Best image only" becomes "Worst image only", "Top N" becomes "Bottom N", etc.
- **Always keeps one:** Trash mode never removes every image from a group - at least one is always kept.
- **API:** The backend accepts `trash_count`/`trash_percent` as alternatives to `keep_count`/`keep_percent`, with mutual exclusion validation.

### Smart People Detection in Search

Typing a known person's name in the search description field now automatically adds them as a People filter chip:

- **Greedy matching:** Multi-word names like "Mary Jane" are preferred over shorter overlapping matches like "Mary" + "Jane".
- **Non-destructive:** Detected names are stripped from the CLIP search query at apply time without altering the text you typed, so the full description is preserved when you return to the Search screen.
- **Manual override:** Auto-detected chips coexist with manually-picked people from the People Picker. Removing an auto-detected chip and re-typing the name won't re-add it.

### Internationalisation Tools

- **String extraction/injection:** New `extract_strings.py` and `apply_strings.py` scripts in `demo-seed/` support round-tripping translatable tutorial title/caption strings for localisation.
- **Translated tutorials:** Tutorial scripts for Spanish, French, and Japanese are now included.

### Bug Fixes

- **Trashing didn't update groups on other clients:** Previously, trashing images only emitted `images_changed` events. Other browser tabs would see the images disappear but group counts wouldn't update and dissolved groups would linger until a manual refresh. Now `groups_changed` is emitted for every affected duplicate level.
- **Folder removal didn't update groups:** Removing a folder invalidated duplicate groups internally but never emitted group change events, so other clients' Groups screens would show stale data.
- **Trash progress race condition:** The trash progress dict was read and written without a lock, which could cause inconsistent progress display under concurrent trash operations.
- **Landscape info panel:** On mobile in landscape orientation, the info panel is now restored to the right side of the gallery (instead of stacking below) where vertical space is scarce and width is ample.
- **Duplicate status on restart:** The duplicate detection status no longer shows stale "Waiting to compute..." when a prior epoch already exists.
- **Tutorial reliability:** Slider interactions in the tutorial now use real click events instead of programmatic dispatch, and unnecessary screen navigation detours have been removed.

## v1.0.5-beta.5

### Unicode Path Support

Photonarium now correctly handles image folders with non-ASCII names - Japanese, Chinese, Korean, Cyrillic, Arabic, accented European characters, emoji, etc. Previously, images inside folders like `Google Photos (Japanese)` would fail to index because OpenCV's C++ file I/O layer garbles Unicode paths on Windows.

- **OpenCV fix:** Image loading for Laplacian sharpness calculation now reads files through Python's Unicode-aware I/O layer instead of passing paths directly to OpenCV's C++ `imread()`, which silently corrupts non-ASCII characters.
- **Console encoding fix:** Python on Windows defaults console output to the system code page (often cp1252 on Western systems). Any log message containing a non-ASCII path would either print garbled text or crash with `UnicodeEncodeError`. The application now forces UTF-8 on stdout/stderr at startup.

### Code Quality

A linting, formatting, and static analysis toolchain has been added to catch bugs earlier and maintain consistent style:

- **Python:** [ruff](https://docs.astral.sh/ruff/) for linting (pyflakes, pycodestyle, bugbear, bandit security rules, and more) and formatting. [vulture](https://github.com/jendrikseipp/vulture) for cross-file dead code detection (on-demand).
- **JavaScript:** [ESLint 9](https://eslint.org/) with [@stylistic](https://eslint.style/) for linting and formatting. [TypeScript checkJs](https://www.typescriptlang.org/tsconfig/#checkJs) for IDE type inference on plain JS.
- **Git pre-commit hook:** Blocks commits with lint or formatting errors in staged files.

The initial pass caught three real bugs and removed ~2,700 lines of confirmed dead code:

- **Broken Delete key on Faces screen:** The keyboard handler referenced a function that had been renamed, so pressing Delete on unknown faces did nothing.
- **Duplicate method in Search:** Two `_fuzzyMatch` methods in the same object literal - the first was silently overwritten by the second.
- **Caption closure bug:** A loop variable captured by reference in image captioning could theoretically produce wrong substitutions.

### Bug Fixes

- **Tutorial screenshots:** The tutorial viewport has been widened to prevent the info panel from auto-collapsing, which was causing several tutorial steps to fail.

## v1.0.4-beta.4

### Installation Flexibility

The installer is now more accommodating of real-world setups - different Python versions, different NVIDIA drivers, and machines with no GPU at all.

- **Python 3.10+:** The minimum Python version has been lowered from 3.11 to 3.10. Ubuntu 22.04 LTS ships Python 3.10, and Python 3.13 (the latest release) is also fully supported. All combinations have been tested with a compatibility matrix covering Python 3.10, 3.11, and 3.13.
- **CUDA auto-detection:** The installer now runs `nvidia-smi` to detect your GPU's CUDA version and installs the matching PyTorch build automatically - CUDA 11.x gets `cu118`, CUDA 12.x+ gets `cu124`, and machines with no NVIDIA GPU get the CPU-only build. Previously it always tried `cu124` and fell back to CPU if that failed, missing users with CUDA 11.x entirely.
- **macOS MPS:** Documentation now recognises Apple MPS acceleration alongside NVIDIA CUDA. macOS installs use the default PyPI torch build, which includes MPS support on Apple Silicon.
- **Cleaner install output:** The `facenet-pytorch` package (which declares overly strict version bounds on torch, numpy, and Pillow) is now installed last with its pip warnings suppressed. Previously it produced four alarming-looking ERROR lines mid-install that were harmless but confusing.
- **Transformers unpinned:** The `transformers==4.44.*` version pin has been removed. It was originally added as a precaution against BLIP-2 API changes, but testing confirmed that current transformers (5.x) works fine with both BLIP and BLIP-2. More importantly, the pin actively broke Python 3.13 installs because the old `tokenizers` version it pulled in has no Python 3.13 wheel.

### Tutorial

- **Touch swipe navigation:** The interactive tutorial now supports swipe gestures - swipe left to advance, right to go back, up to return to the menu. Swiping right on the first slide returns to the menu.
- **Mobile-friendly help text:** On touch devices, the tutorial shows swipe instructions instead of keyboard shortcuts.
- **Improved readability:** The tutorial menu now has better contrast, a brand gradient on the heading, and a darker overlay background.
- **Updated screenshots:** Tutorial screenshots have been refreshed to reflect the current UI.
- **Mobile showcase:** The website now includes a section showing Photonarium on mobile devices.

## v1.0.3-beta.3

### Multi-Client Support

Multiple browser tabs or devices on the same network can now use Photonarium at the same time. Changes made on one client - naming a face, rating an image, creating a group, trashing a photo - are automatically pushed to every other open client within a couple of seconds.

- **Cursor-based event polling:** Each client tracks its own position in the event stream, so events are never lost when multiple clients are polling.
- **Mutation broadcasting:** User-initiated changes (face assignments, people edits, image ratings, group modifications) emit events that other clients pick up and apply incrementally - no full reload needed.
- **Stale client recovery:** If a client falls too far behind (e.g. a laptop lid was closed), it detects the gap and silently reloads all data to catch up.
- **Offline detection:** If the backend becomes unreachable, mutations are blocked with a warning message until the connection is restored, preventing changes from being silently lost.
- **Concurrency fix:** Custom group operations are now properly serialized, preventing data corruption when two clients modify groups at the same time.

### Mobile & Responsive

- **Hamburger menu:** On narrow screens (<=768px), the toolbar collapses into a compact bar showing the logo, screen title, theme toggle, and a hamburger button. Tapping the hamburger reveals the full toolbar controls as a vertical dropdown; tapping outside or navigating to a different screen closes it. The menu auto-closes when resizing back to desktop width.
- **Collapsible info panel:** A toggle button at the edge of the gallery info panel lets you collapse it to reclaim horizontal space. The panel auto-collapses when it would take more than 20% of the viewport width (e.g. on narrow windows or tablets). Once you explicitly toggle the panel, auto-collapse stops overriding your choice. The preference persists across sessions.
- **Dynamic viewport height:** The app and mobile info panel now use `dvh` units (with `vh` fallback) so they correctly resize when the mobile browser address bar appears or disappears - fixing the "info panel stuck at half height after rotation" bug.
- **Wider mobile scrollbars:** Scrollbar touch targets are wider (16px) on mobile for easier dragging. Firefox scrollbar styling is now also supported via the standard `scrollbar-width`/`scrollbar-color` properties.

### NAS / Network Folder Performance

Adding a folder on a NAS or network share (SMB) no longer freezes the UI. The folder is registered immediately and the filesystem scan runs in the background, so you can keep using Photonarium while a large network folder is being indexed.

### Bug Fixes

- **Blank Faces screen:** Fixed a bug where navigating away from the Faces screen while it was still loading would leave it permanently blank on return, with no error shown.
- **Windows installer:** The installer now correctly tells Command Prompt users to run `activate.bat` instead of the bash-only `activate` script.

## v1.0.2-beta.2

### LAN Access

Photonarium is now accessible from other devices on your local network. The server binds to all network interfaces (`0.0.0.0`) by default, so you can browse your photo library from a phone, tablet, or another computer on the same network.

To restrict access to the machine Photonarium is running on, set `server_host: 127.0.0.1` in your config file. Photonarium is designed for trusted home networks and should not be exposed to the public internet.

### Configuration Relocated to OS-Standard Location

The configuration file has moved from `.photonarium.yml` inside the data directory to the OS-standard location:

- **Windows:** `%LOCALAPPDATA%\Photonarium\photonarium.yml`
- **macOS:** `~/Library/Application Support/Photonarium/photonarium.yml`
- **Linux:** `~/.config/photonarium/photonarium.yml`

The config file now stores a `data_dir` setting, so after installation `python app.py` just works - no need to pass `--data-dir` every time.

**Existing users:** If Photonarium finds a `.photonarium.yml` in the current directory but no config at the new location, it will automatically migrate your settings and inject the correct `data_dir`. The old file is left in place but ignored.

### In-App Settings Editor

The **Edit Settings** button on the Database screen now opens an in-app settings editor. The editor works from any device on your network - no need for local file access.

- **Schema-driven:** The backend describes all fields, types, numeric constraints, and help text in a single API response. The frontend renders a generic form with zero hardcoded knowledge of individual settings.
- **Danger fields:** Settings that could break connectivity (`data_dir`, `server_host`, `server_port`) are highlighted with a red border and warning icon.
- **Validation:** Client-side range checking plus full backend validation on save, with clear error messages.
- **Restart required:** Saved changes are written to disk but don't take effect until Photonarium is restarted. The dialog shows the on-disk values, so re-opening after a save reflects what was saved.
- **Direct editing:** A link in the dialog header lets you reveal the YAML file in your file manager if you prefer editing it by hand.

### Installation Improvements

- The installer now creates the config file at the OS-standard location with `data_dir` pre-configured, so the final startup command is simply `python app.py`.
- New `--init-config <data-dir>` flag for scripted/automated installs.
- New `--config` / `-c` flag to use a config file at a custom location.

### Bug Fixes

- **Search filter button:** The Clear Filter button on the search screen now works correctly (was broken by a duplicate HTML element ID).
- **Touch panning:** One-finger panning now works when zoomed in full-screen view, matching the existing mouse drag behaviour.
- **Filter scroll position:** Applying a new filter or opening a group now scrolls the gallery to the top instead of staying at a stale position.
- **Histogram errors:** Requesting a histogram for an image with no checksum now returns a clean 404 instead of a 500 error.
- **Fullscreen performance:** The gallery info panel (including on-demand histogram generation) is deferred while full-screen view is open, reducing unnecessary work.
- **Group navigation:** A loading overlay is now shown when opening a group from the Groups screen or navigating between groups.
- **Error messages:** API errors from the backend now show clearer messages when the response is not valid JSON.
- **Accessibility:** Full-screen images now have alt text set to the filename.
- **Config alignment:** The default similarity threshold for level 2 duplicates now matches between the config template and the internal default (0.93).
