"""Sequential Pipeline Orchestrator for Photonarium.

Replaces the five concurrent processing threads (IngestionThread,
EmbeddingThread, FaceDetectionThread, NimaThread, VideoProcessingThread)
with a single daemon thread that runs seven stages sequentially.
The orchestrator is event-driven: it blocks on a ``threading.Event``
when idle and wakes immediately when ``request_rerun()`` is called
(from ``--scan``, GUI Rescan, folder add/remove, imports, or timed
rescans).  When idle: zero DB queries, zero lock contention.

Benefits
--------
- **No GPU contention** — only one model is loaded at a time.
  Short-lived models (NIMA, MTCNN) are explicitly deleted +
  ``empty_cache()`` after use.  The shared OpenCLIP model stays loaded
  for the app lifetime (used by search as well as the pipeline).
- **No DB lock contention** — stages run sequentially, so the shared
  lock inside ``safe_conn`` is only briefly held for reads/writes, never
  contested between stages.
- **Self-healing** — each stage discovers its own work by querying the
  DB for incomplete rows (e.g. ``embedding IS NULL``,
  ``thumbnails_pending = 1``).  If the process is killed mid-pipeline,
  restarting picks up exactly where it left off with no manual
  intervention.
- **Simpler control flow** — no callback chains, no inter-thread
  signalling between stages.

Stages
------
1. **Ingestion** — Walk registered folders, create/update DB records.

   *Threading*: `ThreadPoolExecutor` with `config.indexing_threads`
   workers (capped at 16).  Workers do I/O (stat, read, hash, EXIF) in
   parallel and submit DB writes through the shared ``SafeConnection``,
   which serialises them on a dedicated writer thread.

   *No GPU, no batching* — work is I/O-bound (stat, read, hash, EXIF).

2. **Thumbnails** — Replace placeholder thumbnails with real 200 px and
   400 px thumbnails for images (2a, flag-driven via
   ``thumbnails_pending``), and detect scenes + generate scene
   thumbnails for videos (2b).

   *Threading (2a)*: `ThreadPoolExecutor` with
   `config.indexing_threads` workers (capped at 8).  Each thumbnail is
   independently generated from the original image using Pillow/rawpy.

   *Sequential (2b)*: Video scene detection (PyAV + content-aware scene
   splits) runs one video at a time.  For each video: detect scene
   boundaries, insert scene records, extract keyframes, and generate
   200 px + 400 px scene thumbnails.  Scene thumbnails live at a
   separate path (``scenes/<size>/<scene_id[:2]>/``) from the
   checksum-based image thumbnails — the placeholder written during
   ingestion becomes unused once a preferred scene is set.  Clears
   ``thumbnails_pending`` after processing.  Config vars:
   `video_scene_detection_threshold`, `video_max_scene_duration`.

   *No GPU* — Pillow resize + sharpen + JPEG encode.

3. **Embeddings** — Compute OpenCLIP vectors for images (3a) and video
   scenes (3b).

   *GPU model*: OpenCLIP (`config.openclip_model` /
   `config.openclip_pretrained`).  Loaded lazily via
   `_get_clip_model()` and kept in memory until the orchestrator
   stops (shared with Stages 5 and 7).

   *Batching (3a)*: Images are processed in batches of
   `config.embedding_batch_size` (default 16).  A double-buffered
   prefetch pipeline (4 worker threads) loads and preprocesses the next
   batch while the GPU encodes the current batch.  LAION aesthetic
   scores (dot product with a ~2 KB linear head) are computed in the
   same loop if the head weights are compatible with the pretrained
   variant (``openai`` only).

   *Sequential (3b)*: Video scenes are embedded one scene at a time
   from 400 px scene thumbnails.  All scene embeddings for a single
   video are committed atomically with the image-level representative
   embedding to prevent partial state on crash.

   *OOM protection (3b)*: Individual scene encodes are wrapped in a
   `MemoryError`/`RuntimeError` catch that calls
   `torch.cuda.empty_cache()` and skips the scene.

4. **Scoring** — NIMA aesthetic scores (4a) and LAION backfill (4b).

   *GPU model (4a)*: NIMA MobileNetV2 checkpoint
   (`<data_dir>/.nima-mobilenetv2-ava.pth`).  Loaded, used, then
   explicitly deleted + `empty_cache()` to free VRAM for Stage 5.

   *Batching (4a)*: `config.nima_batch_size` (default 16) images per
   GPU call.  Input is original images loaded via ``raw_open_image``,
   with a double-buffered prefetch pipeline (4 worker threads).
   Images that fail to load get a sentinel score of 0.0 to prevent
   retrying every cycle.

   *OOM protection (4a)*: If a batch OOMs, falls back to single-image
   scoring.  If a single image also OOMs, it is skipped and the cache
   is cleared.  Model load itself is also wrapped.

   *CPU-only (4b)*: LAION backfill is a numpy dot product on existing
   embedding blobs — no GPU needed.  DB writes are chunked in groups of
   1 000 to avoid holding the lock too long.

5. **Faces** — Detect faces with MTCNN, compute InceptionResnetV1
   embeddings, auto-match to known people, generate face thumbnails,
   and compute semantic (OpenCLIP) embeddings of face crops.

   *GPU models*: MTCNN + InceptionResnetV1 (from facenet-pytorch).
   Loaded per-stage, explicitly deleted + `empty_cache()` afterwards.
   Config: `face_detection_min_confidence`, `face_detection_min_size`.
   Gated by `config.face_detection_enabled`.

   *Batching*: `config.face_detection_batch_size` (default 24) images
   per iteration.  Within each batch, MTCNN's `preload_images_batch()`
   uses 4 I/O workers to load 400 px thumbnails.  Detection results are
   committed per-image (all faces for one image in a single transaction)
   to guarantee atomicity on crash.

   *Batch-level prefetch* — the next batch's images are loaded (via
   ``preload_images_batch``) in a background thread while the current
   batch is being detected and written to DB.

6. **Grouping** — Post-processing housekeeping (always runs after data
   stages when work was done or on startup for self-healing).

   *Sub-stages*: (a) sync directory groups, (b) face reassessment
   (optimistic-locking three-phase: READ→COMPUTE→conditional WRITE),
   (c) duplicate computation (LSH + embedding similarity; see
   `duplicates.py`), (d) unknown face clustering, (e) backfill face
   semantic embeddings.

   *No GPU, no threadpool* — CPU-bound similarity math + DB writes.

7. **STT** — Transcribe videos with `faster-whisper`.

   *GPU model*: Whisper (via `stt.py`).  Loaded lazily; config:
   `stt_enabled`, `stt_model`, `stt_language`.

   *Sequential*: One video at a time.  Audio is extracted to a temp WAV
   file, transcribed as a whole, then segments are assigned to scenes by
   temporal overlap.  Text embeddings are computed via the shared
   OpenCLIP model.

   *Interruptible*: Checks `_rerun_requested` between videos so a
   GUI rescan doesn't wait for slow transcription to finish.

Finalisation
------------
Stages 1–5 are "data" stages.  Stages 6–7 ("finalisation") only run
when a data stage actually did work.  This avoids repeated no-op
grouping on every restart or poll cycle — important for large libraries
where duplicate computation can take minutes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import numpy as np
import torch
from PIL import Image

from faces import (
    FaceDetector,
    compute_unknown_face_groups,
    delete_people_without_faces,
    get_all_known_face_embeddings,
    reconcile_detected_faces,
)
from gputil import STATE_DISABLED, is_gpu_error, is_oom_error
from metadata import derive_timestamp_with_confidence, extract_exif_data
from rawimage import open_image as raw_open_image
from safeconn import SafeConnection
from thumbnails import generate_thumbnail, get_thumbnail_cache_path
from video import (
    _get_video_rotation,
    detect_scenes,
    extract_audio_segment,
    extract_frame,
    generate_scene_thumbnails,
    get_video_metadata,
    is_video_supported,
)

if TYPE_CHECKING:
    from imagedb import ImageDatabase, OpenCLIPModel

logger = logging.getLogger(__name__)


class PipelineOrchestrator(threading.Thread):
    """Sequential pipeline — runs 7 stages one at a time.

    Self-healing: each stage queries DB for items with null values,
    so interrupted processing resumes on restart.
    """

    # Pre-generated placeholder thumbnails (logo on dark 16:9 background).
    # Keyed by size (200/400), values are absolute paths to static JPEGs.
    _PLACEHOLDER_PATHS: ClassVar[dict[int, Path]] = {
        200: Path(__file__).parent / 'static' / 'placeholder_200.jpg',
        400: Path(__file__).parent / 'static' / 'placeholder_400.jpg',
    }

    def _write_placeholder_thumbnails(self, checksum: str) -> None:
        """Copy pre-generated placeholder thumbnails to the cache.

        Called during ingestion so all media appears in the Gallery
        immediately (with the Photonarium logo) rather than as blank
        spaces while waiting for real thumbnail generation.
        """
        for size_px, src_path in self._PLACEHOLDER_PATHS.items():
            thumb_path = get_thumbnail_cache_path(
                checksum,
                size_px,
                thumbnail_dir=self._db.thumbnail_dir,
            )
            if not thumb_path.exists():
                thumb_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src_path, thumb_path)

    def __init__(
        self,
        db: ImageDatabase,
        stop_event: threading.Event,
        pause_event: threading.Event,
    ):
        """Initialise the pipeline orchestrator.

        Args:
            db: ImageDatabase instance (provides conn, config, locks, etc.).
            stop_event: Event to signal thread shutdown.
            pause_event: Event to temporarily pause ingestion.
        """
        super().__init__(name='PipelineOrchestrator', daemon=True)
        self._db = db
        self._stop_event = stop_event
        self._pause_event = pause_event

        # Stage progress (read by get_processing_status)
        self._current_stage: str | None = None
        self._stage_total = 0
        self._stage_done = 0
        self._stage_lock = threading.Lock()

        # Re-entrancy: request_rerun() sets this to restart from Stage 1
        self._rerun_requested = False

        # Wake event: set by request_rerun(), request_rescan_folder(), and
        # stop_threads() to interrupt the idle sleep immediately.  Without
        # this, the pipeline polls every 2s running DB queries for nothing.
        self._wake_event = threading.Event()

        # Lazy-loaded models (one at a time — no concurrent GPU contention).
        # OpenCLIP is shared with ImageDatabase via _get_clip_model().
        self._stt_backend = None
        self._stt_loaded = False

        # Per-video progress tracking (for status endpoint)
        self._current_video: dict[str, Any] | None = None

        # Finalisation flag: reserved for future use.  Stages 6-7 now
        # only run when a data stage actually did work (``had_work``).
        self._finalisation_requested = False

        # Ingestion throttle: after a full file walk that found nothing
        # new, skip Stage 1 on subsequent idle polls until a rescan is
        # explicitly requested.  The first cycle always runs Stage 1.
        self._ingestion_needed = True

        # Thread-local storage for per-worker DB connections (Stage 1).
        # When set, Stage 1 only walks these folders instead of all
        # registered folders.  Populated by request_rescan_folder(),
        # cleared at the start of each ingestion run.
        self._rescan_folders: set[str] = set()
        self._rescan_folders_lock = threading.Lock()

    # -----------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------

    def request_rerun(self) -> None:
        """Called from rescan methods to trigger a full pipeline cycle."""
        self._rerun_requested = True
        self._finalisation_requested = True
        self._ingestion_needed = True
        # Full rescan — clear any per-folder filter
        with self._rescan_folders_lock:
            self._rescan_folders.clear()
        self._wake_event.set()

    def request_rescan_folder(self, folder_path: str) -> None:
        """Queue a single folder for rescan on the next pipeline cycle.

        Unlike ``request_rerun()``, this only walks the specified folder
        in Stage 1, skipping unchanged files in other folders.  Multiple
        calls accumulate — all queued folders are scanned together.

        Args:
            folder_path: Absolute path of the folder to rescan.
        """
        with self._rescan_folders_lock:
            self._rescan_folders.add(folder_path)
        self._ingestion_needed = True
        self._rerun_requested = True
        self._finalisation_requested = True
        self._wake_event.set()

    def get_stage_progress(self) -> dict[str, Any]:
        """Get current stage progress for status reporting.

        Returns:
            Dict with 'stage', 'total', 'done' keys (or empty if idle).
        """
        with self._stage_lock:
            if self._current_stage is None:
                return {}
            return {
                'stage': self._current_stage,
                'total': self._stage_total,
                'done': self._stage_done,
            }

    @property
    def current_video(self) -> dict[str, Any] | None:
        """Current video processing progress (for status endpoint)."""
        return self._current_video

    # -----------------------------------------------------------------
    # Main thread loop
    # -----------------------------------------------------------------

    def run(self) -> None:
        """Main thread loop — run pipeline cycles until stopped."""
        logger.info('Pipeline orchestrator started')

        while not self._stop_event.is_set():
            self._rerun_requested = False
            had_work = self._run_pipeline()
            if not had_work and not self._rerun_requested:
                self._set_stage(None, 0, 0)
                self._flush_logs()
                # Block until woken by request_rerun() or stop_threads().
                # No timeout — all triggers set _wake_event, so the pipeline
                # does zero work and holds zero locks while idle.
                self._wake_event.wait()
                self._wake_event.clear()
            # If _rerun_requested was set during pipeline, loop immediately

        logger.info('Pipeline orchestrator stopped')

    def _run_pipeline(self) -> bool:
        """Run all stages. Returns True if any stage did work.

        Stage 1 (ingestion) walks all registered folders looking for
        new/changed files.  This is expensive for large libraries, so
        it only runs when explicitly requested (startup, rescan, folder
        change) or when a previous cycle found work (cascading changes).

        Stages 2-5 query the DB for unprocessed images.  They only run
        when Stage 1 found work, when explicitly triggered (rescan,
        folder add, import), or when a previous cycle's stage found
        cascading work.  When idle, nothing runs — zero DB queries.

        Stage 6 (grouping) and Stage 7 (STT) only run when a preceding
        stage did work, avoiding repeated no-op grouping every poll
        cycle.
        """
        t0 = time.perf_counter()
        had_work = False

        # Stage 1 is a full file-system walk — only run when needed
        if self._ingestion_needed and not self._stop_event.is_set():
            try:
                ingestion_did_work = self._stage_ingestion()
                if ingestion_did_work:
                    had_work = True
                    logger.debug('Pipeline stage "ingestion" reported work')
                else:
                    # No changes found — skip Stage 1 on subsequent idle
                    # polls until a rescan is requested.
                    self._ingestion_needed = False
            except Exception:
                logger.exception('Error in pipeline stage "ingestion"')
                try:
                    self._db.safe_conn.rollback()
                except Exception:
                    pass
            self._flush_logs()

        # Stages 2-5 check for unprocessed images.  Skip entirely when
        # idle — the wake event ensures we run when there's actual work.
        if not (had_work or self._rerun_requested):
            return had_work

        # Stages that use the GPU — skipped if GPU is permanently disabled.
        _GPU_STAGES = {'embeddings', 'scoring', 'faces'}

        for stage_name, stage_fn in [
            ('thumbnails', self._stage_thumbnails),
            ('embeddings', self._stage_embeddings),
            ('scoring', self._stage_scoring),
            ('faces', self._stage_faces),
        ]:
            if self._stop_event.is_set():
                break
            if stage_name in _GPU_STAGES and self._db.gpu_health.state == STATE_DISABLED:
                continue
            try:
                stage_did_work = stage_fn()
                if stage_did_work:
                    had_work = True
                    logger.debug(f'Pipeline stage "{stage_name}" reported work')
            except Exception:
                logger.exception(f'Error in pipeline stage "{stage_name}"')
                try:
                    self._db.safe_conn.rollback()
                except Exception:
                    pass
        self._flush_logs()

        # Run grouping and STT when data stages did work.  Previously
        # this also ran on every ``request_rerun()`` (startup, rescan
        # button) regardless of whether any stage found work, which
        # meant Stage 6 re-ran duplicate computation on every restart
        # even when nothing had changed — very expensive on large
        # libraries.  Now we only finalise when a data stage actually
        # modified something.
        run_finalisation = had_work
        self._finalisation_requested = False

        if run_finalisation and not self._stop_event.is_set():
            # Clear _rerun_requested so finalisation stages (grouping, STT)
            # are not skipped by their early-exit checks.  If a rerun was
            # requested during stages 2-5, the outer loop in run() will
            # start a new cycle after finalisation completes.
            self._rerun_requested = False
            for stage_name, stage_fn in [
                ('grouping', self._stage_grouping),
                ('transcription', self._stage_stt),
            ]:
                if self._stop_event.is_set():
                    break
                try:
                    stage_fn()
                except Exception:
                    logger.exception(f'Error in pipeline stage "{stage_name}"')
                    try:
                        self._db.safe_conn.rollback()
                    except Exception:
                        pass
            self._flush_logs()

        if had_work:
            elapsed = time.perf_counter() - t0
            logger.info(f'Pipeline cycle complete ({elapsed:.1f}s)')

        return had_work

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _flush_logs(self) -> None:
        """Flush buffered log records to the database.

        Called at pipeline stage boundaries to ensure log records are
        persisted before the next stage begins its own DB-intensive work.
        """
        handler = self._db._log_handler
        if handler is not None:
            handler.flush()

    def _set_stage(self, stage: str | None, total: int, done: int) -> None:
        """Update stage progress atomically."""
        with self._stage_lock:
            self._current_stage = stage
            self._stage_total = total
            self._stage_done = done

    def _update_done(self, done: int) -> None:
        """Update done count for current stage."""
        with self._stage_lock:
            self._stage_done = done

    def _stopped(self) -> bool:
        """Check if we should stop processing."""
        return self._stop_event.is_set()

    def _get_clip_model(self) -> OpenCLIPModel:
        """Get the shared OpenCLIP model from ImageDatabase.

        A single instance is shared between the pipeline and search
        queries — no need for two copies on the GPU.
        """
        return self._db._get_clip_model()

    def _get_worker_conn(self) -> SafeConnection:
        """Return the shared SafeConnection for worker thread DB access.

        All writes are serialised by SafeConnection's writer thread,
        so per-worker connections are no longer needed.  Workers still
        do I/O (stat, hash, EXIF) in parallel — only the DB operations
        go through the shared connection.
        """
        return self._db.safe_conn

    def _get_stt_backend(self):
        """Lazy-load the STT backend."""
        if not self._stt_loaded:
            self._stt_loaded = True
            from stt import get_stt_backend

            self._stt_backend = get_stt_backend(self._db.config)
            if self._stt_backend is not None:
                self._stt_backend.set_gpu_health(self._db.gpu_health)
        return self._stt_backend

    # =================================================================
    # Stage 1: Ingestion
    # =================================================================

    def _stage_ingestion(self) -> bool:
        """Walk all registered folders, create/update DB records.

        Uses ThreadPoolExecutor with per-worker SQLite connections for
        parallel metadata extraction.

        Returns:
            True if any files were processed.
        """
        from imagedb import (
            _upsert_image_metadata,
            canonicalise_path,
            create_image,
            extract_image_metadata,
            find_images_in_folder,
            get_folders,
            get_image_by_path,
            update_image_metadata,
        )

        folders = get_folders(self._db.safe_conn)
        if not folders:
            return False

        all_folder_paths = [f['path'] for f in folders]

        # Check if this is a targeted single-folder rescan
        with self._rescan_folders_lock:
            targeted = set(self._rescan_folders)
            self._rescan_folders.clear()

        if targeted:
            # Only walk the requested folders (must still be registered)
            folder_paths = [p for p in all_folder_paths if p in targeted]
            if not folder_paths:
                return False
            logger.info(f'Stage 1: Targeted rescan of {len(folder_paths)} folder(s)')
        else:
            folder_paths = all_folder_paths

        # Collect all file paths from folders to scan
        all_paths: list[Path] = []
        for folder_path in folder_paths:
            folder = Path(folder_path)
            if not folder.exists():
                continue
            for image_path in find_images_in_folder(
                folder,
                self._db.config.image_extensions | self._db.config.video_extensions,
                registered_folders=folder_paths,
            ):
                all_paths.append(image_path)

        if not all_paths:
            # Still need to check for deleted files
            self._mark_deleted_files(folder_paths, set())
            return False

        self._set_stage('ingestion', len(all_paths), 0)
        logger.info(f'Stage 1: Indexing {len(all_paths)} files...')

        found_paths: set[str] = set()
        processed_count = 0
        changed_count = 0
        error_count = 0
        num_threads = max(1, min(16, self._db.config.indexing_threads))

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            pending_futures: dict[Future, Path] = {}
            path_iter = iter(all_paths)
            paths_exhausted = False

            while not self._stopped():
                # Check if paused
                if self._pause_event.is_set():
                    time.sleep(0.1)
                    continue

                # Submit new jobs while we have capacity
                while len(pending_futures) < num_threads * 2 and not paths_exhausted:
                    try:
                        path = next(path_iter)
                    except StopIteration:
                        paths_exhausted = True
                        break
                    found_paths.add(str(path))
                    future = executor.submit(
                        self._process_file,
                        path,
                        extract_image_metadata,
                        create_image,
                        update_image_metadata,
                        get_image_by_path,
                        canonicalise_path,
                        _upsert_image_metadata,
                    )
                    pending_futures[future] = path

                # Wait for at least one future to complete (releases GIL,
                # avoids busy-spin polling).  Short timeout so we can
                # still check _stopped() and _pause_event periodically.
                if pending_futures:
                    done, _ = wait(pending_futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        path = pending_futures.pop(future)
                        try:
                            was_changed = future.result()
                            processed_count += 1
                            if was_changed:
                                changed_count += 1
                        except Exception as e:
                            logger.error(f'Error processing {path}: {e}')
                            error_count += 1

                        total_done = processed_count + error_count
                        self._update_done(total_done)
                        if total_done % 500 == 0 and total_done < len(all_paths):
                            logger.info(f'  Ingestion: {total_done}/{len(all_paths)} files')
                elif paths_exhausted:
                    # All paths submitted and all futures completed — done
                    break

        # Mark missing files as deleted
        self._mark_deleted_files(folder_paths, found_paths)

        logger.info(
            f'Stage 1 complete: {processed_count} checked, {changed_count} new/changed, {error_count} errors'
        )
        if changed_count > 0 or error_count > 0:
            # Notify frontend that images are indexed
            self._db.event_queue.emit('images_indexed', {})

        return changed_count > 0

    def _process_file(
        self,
        path: Path,
        extract_image_metadata,
        create_image,
        update_image_metadata,
        get_image_by_path,
        canonicalise_path,
        _upsert_image_metadata,
    ) -> bool:
        """Process a single file (image or video) for ingestion.

        Runs in a worker thread with its own SafeConnection. The heavy
        I/O work (stat, read, hash, EXIF) runs without any lock; only
        the brief DB calls acquire the per-connection RLock.

        Does NOT generate thumbnails (moved to Stage 2) or queue items
        for further processing (stages query DB directly).

        Returns:
            True if the file was new or changed (actual work done),
            False if unchanged (only backfill checks).
        """
        path = canonicalise_path(path)
        conn = self._get_worker_conn()

        if not path.exists():
            return False

        try:
            stat_info = path.stat()
            current_size = stat_info.st_size
            current_mtime = stat_info.st_mtime
        except OSError:
            logger.warning(f'Cannot stat file: {path}')
            return False

        existing = get_image_by_path(conn, path)

        if existing is not None:
            return self._process_existing_file(
                conn,
                path,
                existing,
                current_size,
                current_mtime,
                extract_image_metadata,
                update_image_metadata,
                _upsert_image_metadata,
            )
        else:
            return self._process_new_file(
                conn,
                path,
                current_size,
                current_mtime,
                extract_image_metadata,
                create_image,
                canonicalise_path,
            )

    def _process_existing_file(
        self,
        conn: SafeConnection,
        path: Path,
        existing: dict,
        current_size: int,
        current_mtime: float,
        extract_image_metadata,
        update_image_metadata,
        _upsert_image_metadata,
    ) -> bool:
        """Handle re-ingestion of an existing file (unchanged or changed).

        Returns:
            True if the file was changed and re-ingested, False if unchanged.
        """
        existing_mtime = existing.get('mtime')
        existing_checksum = existing.get('checksum')
        existing_size = existing.get('size', 0)
        is_video = existing.get('media_type') == 'video'

        # Stub record from a previous failed ingestion (unreadable file).
        # Skip if unchanged; if the file has been replaced on disk (different
        # size or mtime), fall through to the normal re-extraction path.
        is_stub = (existing.get('width') or 0) == 0 and (existing.get('height') or 0) == 0
        if (
            is_stub
            and current_size == existing_size
            and existing_mtime is not None
            and abs(current_mtime - existing_mtime) < 1.0
        ):
            return False

        # Backfill mtime if missing
        if existing_mtime is None and existing['size'] == current_size:
            conn.execute('UPDATE images SET mtime = ? WHERE id = ?', (current_mtime, existing['id']))
            conn.commit()
            existing_mtime = current_mtime

        # Determine whether the file content has actually changed.
        # Size difference → definitely changed.  Mtime-only difference →
        # compute checksum to distinguish real modifications from benign
        # mtime drift (Dropbox sync, filesystem precision, backup tools).
        # Use a tolerance for mtime comparison: floating-point round-trip
        # through SQLite can introduce sub-second drift, causing false
        # "changed" detections and unnecessary DB writes on every reindex.
        mtime_matches = (
            existing_mtime is not None
            and abs(existing_mtime - current_mtime) < 0.01
        )
        file_unchanged = existing['size'] == current_size and mtime_matches

        if not file_unchanged and existing['size'] == current_size and existing_checksum:
            # Same size, different mtime — check if content actually changed
            current_checksum = self._compute_checksum(path)
            if current_checksum == existing_checksum:
                # Content identical — mtime drifted but file wasn't modified.
                # Update stored mtime so we don't re-checksum every cycle,
                # then fall through to the unchanged-file path.
                logger.debug(f'Mtime changed but checksum matches, updating stored mtime: {path}')
                conn.execute(
                    'UPDATE images SET mtime = ? WHERE id = ?',
                    (current_mtime, existing['id']),
                )
                conn.commit()
                file_unchanged = True

        if file_unchanged:
            # File unchanged — backfill missing data
            if not is_video:
                # Backfill missing checksum
                if existing_checksum is None and existing_size > 0:
                    logger.info(f'Backfilling missing checksum for: {path}')
                    metadata = extract_image_metadata(
                        path,
                        self._db.config.max_image_dimension,
                        self._db.config.filename_date_overrides,
                        self._db.config.date_order,
                    )
                    if metadata is not None:
                        update_image_metadata(
                            conn,
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

                # Backfill EXIF
                if existing.get('exif_data') is None:
                    exif_data = extract_exif_data(path)
                    exif_json = json.dumps(exif_data) if exif_data else '{}'
                    conn.execute(
                        'UPDATE images SET exif_data = ?, updated_at = ? WHERE id = ?',
                        (exif_json, datetime.now().isoformat(), existing['id']),
                    )
                    if exif_data:
                        _upsert_image_metadata(conn, existing['id'], exif_data)
                    conn.commit()

            # Recompute non-user-assigned timestamps
            ts_conf = existing.get('timestamp_confidence')
            if ts_conf is not None and ts_conf != 0:
                exif_data = None
                raw_exif = existing.get('exif_data')
                if raw_exif:
                    parsed = json.loads(raw_exif)
                    if isinstance(parsed, dict) and parsed:
                        exif_data = parsed
                new_ts, new_conf = derive_timestamp_with_confidence(
                    path,
                    exif_data=exif_data,
                    filename_date_overrides=self._db.config.filename_date_overrides,
                    date_order=self._db.config.date_order,
                )
                new_ts_str = new_ts.isoformat() if new_ts else None
                old_ts = existing.get('timestamp')
                if new_ts_str != old_ts or new_conf != ts_conf:
                    conn.execute(
                        'UPDATE images SET timestamp = ?, timestamp_confidence = ?, updated_at = ? WHERE id = ?',
                        (new_ts_str, new_conf, datetime.now().isoformat(), existing['id']),
                    )
                    conn.commit()

            # Backfill placeholder thumbnails for items ingested before
            # the placeholder feature was added.  Sets the pending flag
            # so Stage 2a/2b will generate real thumbnails.
            checksum = existing_checksum or existing.get('checksum')
            if checksum:
                needs_placeholder = False
                for size_px in (200, 400):
                    thumb_path = get_thumbnail_cache_path(
                        checksum,
                        size_px,
                        thumbnail_dir=self._db.thumbnail_dir,
                    )
                    if not thumb_path.exists():
                        needs_placeholder = True
                        break
                if needs_placeholder:
                    self._write_placeholder_thumbnails(checksum)
                    conn.execute(
                        'UPDATE images SET thumbnails_pending = 1, updated_at = ? WHERE id = ?',
                        (datetime.now().isoformat(), existing['id']),
                    )
                    conn.commit()

            return False

        # File genuinely changed (different size, or different checksum)
        if is_video:
            self._reingest_changed_video(conn, path, existing, current_size, current_mtime)
        else:
            self._reingest_changed_image(
                conn,
                path,
                existing,
                extract_image_metadata,
                update_image_metadata,
            )
        return True

    def _reingest_changed_video(
        self,
        conn: SafeConnection,
        path: Path,
        existing: dict,
        current_size: int,
        current_mtime: float,
    ) -> None:
        """Re-ingest a changed video file."""
        logger.info(f'Re-ingesting changed video: {path}')
        if not is_video_supported():
            logger.warning(f'Skipping video (PyAV not installed): {path}')
            return
        vmeta = get_video_metadata(path)
        if vmeta is None:
            logger.warning(f'Failed to extract video metadata for changed file: {path}')
            return

        checksum = self._compute_checksum(path)

        # Recompute timestamp from the (possibly renamed) filename / exif
        ts_conf = existing.get('timestamp_confidence')
        if ts_conf is not None and ts_conf != 0:
            new_ts, new_conf = derive_timestamp_with_confidence(
                path,
                exif_data=None,  # Videos have no EXIF
                filename_date_overrides=self._db.config.filename_date_overrides,
                date_order=self._db.config.date_order,
            )
            new_ts_str = new_ts.isoformat() if new_ts else None
        else:
            # User-assigned timestamp — preserve it
            new_ts_str = existing.get('timestamp')
            new_conf = ts_conf

        now_ts = datetime.now().isoformat()
        conn.execute(
            """UPDATE images SET size = ?, width = ?, height = ?, duration = ?,
               timestamp = ?, timestamp_confidence = ?,
               mtime = ?, checksum = ?, embedding = NULL,
               aesthetic_nima = NULL, aesthetic_laion = NULL,
               thumbnails_pending = 1, updated_at = ?,
               codec_video = ?, codec_audio = ?, codec_container = ?
               WHERE id = ?""",
            (
                current_size,
                vmeta.width,
                vmeta.height,
                vmeta.duration,
                new_ts_str,
                new_conf,
                current_mtime,
                checksum,
                now_ts,
                vmeta.codec,
                vmeta.codec_audio,
                vmeta.codec_container,
                existing['id'],
            ),
        )
        # Delete old scenes and faces
        conn.execute('DELETE FROM scenes WHERE image_id = ?', (existing['id'],))
        conn.execute('DELETE FROM faces WHERE image_id = ?', (existing['id'],))
        conn.commit()

        if checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[existing['id']] = checksum
            # Refresh placeholder so the video is visible immediately
            self._write_placeholder_thumbnails(checksum)

    def _reingest_changed_image(
        self,
        conn: SafeConnection,
        path: Path,
        existing: dict,
        extract_image_metadata,
        update_image_metadata,
    ) -> None:
        """Re-ingest a changed image file."""
        logger.info(f'Re-ingesting changed image: {path}')
        metadata = extract_image_metadata(
            path,
            self._db.config.max_image_dimension,
            self._db.config.filename_date_overrides,
            self._db.config.date_order,
        )
        if metadata is None:
            logger.warning(f'Failed to extract metadata for changed image: {path}')
            return

        # Preserve manual (0) confidence — imported files have junk FS
        # timestamps, so re-deriving would overwrite the correct date.
        img_ts = metadata.timestamp
        img_ts_conf = metadata.timestamp_confidence
        existing_conf = existing.get('timestamp_confidence')
        if existing_conf is not None and existing_conf == 0:
            img_ts = datetime.fromisoformat(existing['timestamp']) if existing.get('timestamp') else metadata.timestamp
            img_ts_conf = 0

        update_image_metadata(
            conn,
            existing['id'],
            size=metadata.size,
            width=metadata.width,
            height=metadata.height,
            timestamp=img_ts,
            timestamp_confidence=img_ts_conf,
            checksum=metadata.checksum,
            perceptual_hash=metadata.perceptual_hash,
            laplacian_var=metadata.laplacian_var,
            lossless=metadata.lossless,
            mtime=metadata.mtime,
            exif_data=metadata.exif_data,
        )

        # Clear embedding and scores so Stages 3-5 re-process; mark
        # thumbnails as pending so Stage 2a regenerates them.
        conn.execute(
            """UPDATE images SET embedding = NULL, aesthetic_nima = NULL,
               aesthetic_laion = NULL, thumbnails_pending = 1 WHERE id = ?""",
            (existing['id'],),
        )
        # Delete faces so Stage 5 re-detects
        conn.execute('DELETE FROM faces WHERE image_id = ?', (existing['id'],))
        conn.commit()

        if metadata.checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[existing['id']] = metadata.checksum
            # Copy placeholder thumbnails so the image is visible
            # immediately while Stage 2a generates real ones.
            self._write_placeholder_thumbnails(metadata.checksum)

    def _process_new_file(
        self,
        conn: SafeConnection,
        path: Path,
        current_size: int,
        current_mtime: float,
        extract_image_metadata,
        create_image,
        canonicalise_path,
    ) -> bool:
        """Ingest a completely new file (image or video).

        Returns:
            True if the file was successfully ingested, False if it was
            skipped (e.g. corrupt file, missing metadata).
        """
        ext = path.suffix.lower()
        is_video = ext in self._db.config.video_extensions

        if is_video:
            return self._ingest_new_video(conn, path, current_size, current_mtime, create_image, canonicalise_path)
        else:
            return self._ingest_new_image(
                conn, path, current_size, current_mtime, extract_image_metadata, create_image, canonicalise_path
            )

    def _ingest_new_video(
        self,
        conn: SafeConnection,
        path: Path,
        current_size: int,
        current_mtime: float,
        create_image,
        canonicalise_path,
    ) -> bool:
        """Ingest a new video file.

        Returns:
            True if successfully ingested, False if skipped.
        """
        if not is_video_supported():
            logger.warning(f'Skipping video (PyAV not installed): {path}')
            return False

        vmeta = get_video_metadata(path)
        if vmeta is None:
            # Create a stub record so we don't retry this file every startup.
            # width=0, height=0 acts as a sentinel for unreadable files.
            logger.warning(f'Failed to read video, creating stub record: {path}')
            image_id = str(uuid.uuid4())
            checksum = self._compute_checksum(path)
            ts, ts_conf = derive_timestamp_with_confidence(
                path,
                exif_data=None,
                filename_date_overrides=self._db.config.filename_date_overrides,
                date_order=self._db.config.date_order,
            )
            create_image(
                conn,
                image_id=image_id,
                path=path,
                size=current_size,
                width=0,
                height=0,
                timestamp=ts,
                timestamp_confidence=ts_conf,
                checksum=checksum,
                mtime=current_mtime,
                media_type='video',
            )
            conn.commit()
            return True

        image_id = str(uuid.uuid4())
        checksum = self._compute_checksum(path)

        path_str_canon = str(canonicalise_path(path))
        with self._db._import_names_lock:
            import_info = self._db._import_names.pop(path_str_canon, None)

        if import_info:
            # Use the timestamp derived by ImportWorker from the original
            # file (before copy), and pin to manual (0) so self-healing
            # never overwrites it with the catalogue copy's FS metadata.
            import_name, ts = import_info
            ts_conf = 0
        else:
            import_name = None
            ts, ts_conf = derive_timestamp_with_confidence(
                path,
                exif_data=None,
                filename_date_overrides=self._db.config.filename_date_overrides,
                date_order=self._db.config.date_order,
            )

        create_image(
            conn,
            image_id=image_id,
            path=path,
            size=current_size,
            width=vmeta.width,
            height=vmeta.height,
            timestamp=ts,
            timestamp_confidence=ts_conf,
            checksum=checksum,
            mtime=current_mtime,
            import_name=import_name,
            media_type='video',
            duration=vmeta.duration,
            thumbnails_pending=True,
            codec_video=vmeta.codec,
            codec_audio=vmeta.codec_audio,
            codec_container=vmeta.codec_container,
        )

        if checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[image_id] = checksum
            # Copy placeholder thumbnails so videos are visible in the
            # Gallery immediately, before Stage 2b runs.
            self._write_placeholder_thumbnails(checksum)

        logger.debug(f'Ingested new video: {path}')
        return True

    def _ingest_new_image(
        self,
        conn: SafeConnection,
        path: Path,
        current_size: int,
        current_mtime: float,
        extract_image_metadata,
        create_image,
        canonicalise_path,
    ) -> bool:
        """Ingest a new image file.

        Returns:
            True if successfully ingested, False if skipped.
        """
        metadata = extract_image_metadata(
            path,
            self._db.config.max_image_dimension,
            self._db.config.filename_date_overrides,
            self._db.config.date_order,
        )
        if metadata is None:
            # Create a stub record so we don't retry this file every startup.
            # width=0, height=0 acts as a sentinel for unreadable files.
            logger.warning(f'Failed to read image, creating stub record: {path}')
            image_id = str(uuid.uuid4())
            ts, ts_conf = derive_timestamp_with_confidence(
                path,
                exif_data=None,
                filename_date_overrides=self._db.config.filename_date_overrides,
                date_order=self._db.config.date_order,
            )
            create_image(
                conn,
                image_id=image_id,
                path=path,
                size=current_size,
                width=0,
                height=0,
                timestamp=ts,
                timestamp_confidence=ts_conf,
                mtime=current_mtime,
            )
            conn.commit()
            return True

        image_id = str(uuid.uuid4())

        path_str_canon = str(canonicalise_path(path))
        with self._db._import_names_lock:
            import_info = self._db._import_names.pop(path_str_canon, None)

        if import_info:
            # Use the timestamp derived by ImportWorker from the original
            # file (before copy), and pin to manual (0) so self-healing
            # never overwrites it with the catalogue copy's FS metadata.
            import_name, img_ts = import_info
            img_ts_conf = 0
        else:
            import_name = None
            img_ts = metadata.timestamp
            img_ts_conf = metadata.timestamp_confidence

        create_image(
            conn,
            image_id=image_id,
            path=metadata.path,
            size=metadata.size,
            width=metadata.width,
            height=metadata.height,
            timestamp=img_ts,
            timestamp_confidence=img_ts_conf,
            checksum=metadata.checksum,
            perceptual_hash=metadata.perceptual_hash,
            laplacian_var=metadata.laplacian_var,
            lossless=metadata.lossless,
            mtime=metadata.mtime,
            exif_data=metadata.exif_data,
            import_name=import_name,
            thumbnails_pending=True,
        )

        if metadata.checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[image_id] = metadata.checksum
            # Copy placeholder thumbnails so images are visible in the
            # Gallery immediately, before Stage 2a runs.
            self._write_placeholder_thumbnails(metadata.checksum)

        logger.debug(f'Ingested new image: {path}')
        return True

    def _mark_deleted_files(self, folder_paths: list[str], found_paths: set[str]) -> None:
        """Mark files in DB but not on disk as deleted."""
        from imagedb import get_all_images

        all_images = get_all_images(self._db.safe_conn, include_deleted=False)
        known_paths = {img['path'] for img in all_images}

        # Only consider paths under registered folders
        registered_prefixes = [fp.rstrip('/\\') + '/' for fp in folder_paths]
        known_under_folders = {p for p in known_paths if any(p.startswith(prefix) for prefix in registered_prefixes)}

        missing_paths = known_under_folders - found_paths
        if missing_paths:
            logger.info(f'Marking {len(missing_paths)} missing images as deleted')
            now = datetime.now().isoformat()
            with self._db.safe_conn:
                for path in missing_paths:
                    self._db.safe_conn.execute(
                        'UPDATE images SET deleted = 1, updated_at = ? WHERE path = ? AND deleted = 0',
                        (now, path),
                    )
                self._db.safe_conn.commit()

    @staticmethod
    def _compute_checksum(path: Path) -> str | None:
        """Compute SHA-256 checksum for a file."""
        try:
            sha256 = hashlib.sha256()
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    sha256.update(chunk)
            return sha256.hexdigest()
        except OSError:
            return None

    # =================================================================
    # Stage 2: Thumbnails
    # =================================================================

    def _stage_thumbnails(self) -> bool:
        """Generate missing thumbnails for images and video scenes.

        Returns:
            True if any thumbnails were generated.
        """
        did_work = False

        # 2a: Image thumbnails
        img_count = self._generate_image_thumbnails()
        if img_count > 0:
            did_work = True

        # 2b: Video scene detection + scene thumbnails
        if not self._stopped():
            vid_count = self._process_video_scenes()
            if vid_count > 0:
                did_work = True

        return did_work

    def _generate_image_thumbnails(self) -> int:
        """Generate real thumbnails for images with ``thumbnails_pending = 1``.

        Returns:
            Number of thumbnails generated.
        """
        cursor = self._db.safe_conn.execute("""
            SELECT id, path, checksum FROM images
            WHERE deleted = 0 AND checksum IS NOT NULL
              AND media_type = 'image' AND thumbnails_pending = 1
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        need_thumbnails: list[tuple[str, str, str]] = [  # (id, path, checksum)
            (row['id'], row['path'], row['checksum']) for row in rows
        ]

        self._set_stage('thumbnails', len(need_thumbnails), 0)
        logger.info(f'Stage 2a: Generating thumbnails for {len(need_thumbnails)} images...')

        count = 0
        num_threads = max(1, min(8, self._db.config.indexing_threads))

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures: dict[Future, tuple[str, str]] = {}
            for image_id, path_str, checksum in need_thumbnails:
                if self._stopped():
                    break
                future = executor.submit(self._gen_thumb, Path(path_str), checksum)
                futures[future] = (image_id, checksum)

            for future in futures:
                if self._stopped():
                    break
                image_id, checksum = futures[future]
                try:
                    if future.result():
                        count += 1
                        # Evict stale placeholder bytes from the RAM cache
                        # so the next request reads the real thumbnail from
                        # disk instead of serving the cached placeholder.
                        if self._db.thumbnail_ram_cache is not None:
                            self._db.thumbnail_ram_cache.remove(checksum)
                except Exception as e:
                    logger.warning(f'Thumbnail generation failed: {e}')
                # Clear the pending flag regardless of success — if it
                # failed, the placeholder stays but we don't keep retrying.
                with self._db.safe_conn:
                    self._db.safe_conn.execute(
                        'UPDATE images SET thumbnails_pending = 0 WHERE id = ?',
                        (image_id,),
                    )
                    self._db.safe_conn.commit()
                self._update_done(count)

        if count > 0:
            logger.info(f'Stage 2a complete: generated thumbnails for {count} images')
        return count

    def _gen_thumb(self, source_path: Path, checksum: str) -> bool:
        """Generate 200px and 400px thumbnails for a single image."""
        success = True
        for size in (200, 400):
            cache_path = get_thumbnail_cache_path(checksum, size, thumbnail_dir=self._db.thumbnail_dir)
            if not generate_thumbnail(
                source_path,
                cache_path,
                size=size,
                quality=self._db.config.thumbnail_quality,
                max_source_dimension=self._db.config.max_image_dimension,
            ):
                success = False
        return success

    def _process_video_scenes(self) -> int:
        """Process videos that need scene detection and scene thumbnails.

        Returns:
            Number of videos processed.
        """
        if not is_video_supported():
            return 0

        cursor = self._db.safe_conn.execute("""
            SELECT i.id, i.path
            FROM images i
            WHERE i.deleted = 0
              AND i.media_type = 'video'
              AND NOT EXISTS (SELECT 1 FROM scenes s WHERE s.image_id = i.id)
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        self._set_stage('video_scenes', len(rows), 0)
        logger.info(f'Stage 2b: Processing scenes for {len(rows)} videos...')
        count = 0

        for row in rows:
            if self._stopped():
                break

            image_id = row['id']
            path = Path(row['path'])

            if not path.exists():
                continue

            basename = path.name
            logger.info(f'Processing video {count + 1}/{len(rows)}: {basename}')
            self._current_video = {
                'label': basename,
                'step': 'Detecting scenes',
                'step_index': 1,
                'total_steps': 2,
                'done': count,
                'total': len(rows),
            }

            # Scene detection
            scenes = detect_scenes(
                path,
                threshold=self._db.config.video_scene_detection_threshold,
                max_scene_duration=self._db.config.video_max_scene_duration,
            )
            if not scenes:
                logger.warning(f'No scenes detected for {path}')
                continue

            # Insert scene records
            now = datetime.now().isoformat()
            scene_ids = []
            insert_params: list[tuple] = []
            for idx, (start, end) in enumerate(scenes):
                scene_id = str(uuid.uuid4())
                scene_ids.append(scene_id)
                keyframe_time = (start + end) / 2
                insert_params.append((scene_id, image_id, idx, start, end, keyframe_time, now, now))

            with self._db.safe_conn:
                self._db.safe_conn.executemany(
                    """INSERT OR REPLACE INTO scenes
                        (id, image_id, scene_index, start_time, end_time,
                         keyframe_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    insert_params,
                )
                self._db.safe_conn.commit()

            # Extract keyframes and generate thumbnails in a single pass.
            # Each frame is decoded once, used for both thumbnail sizes,
            # then released — peak memory is one full-res frame at a time
            # instead of holding all N frames in a list.
            self._current_video = {
                'label': basename,
                'step': 'Generating thumbnails',
                'step_index': 2,
                'total_steps': 2,
                'done': count,
                'total': len(rows),
            }
            thumbnail_dir = self._db.thumbnail_dir
            rotation = _get_video_rotation(path)
            for scene_idx, (start, end) in enumerate(scenes):
                if self._stopped():
                    break
                midpoint = (start + end) / 2
                frame = extract_frame(path, midpoint, rotation=rotation)
                if frame is not None:
                    generate_scene_thumbnails(
                        path,
                        scene_ids[scene_idx],
                        midpoint,
                        thumbnail_dir,
                        quality=self._db.config.thumbnail_quality,
                        frame=frame,
                    )

            # Clear the pending flag and set preferred scene to the
            # first scene so the video has a visible thumbnail in the
            # Gallery/Videos screens immediately (Stage 3b will later
            # copy the scene's embedding to the image row).
            with self._db.safe_conn:
                self._db.safe_conn.execute(
                    """UPDATE images SET thumbnails_pending = 0,
                       preferred_scene_id = COALESCE(preferred_scene_id, ?),
                       updated_at = ?
                       WHERE id = ?""",
                    (scene_ids[0], datetime.now().isoformat(), image_id),
                )
                self._db.safe_conn.commit()

            # Evict stale placeholder from the RAM thumbnail cache so
            # the next request reads the real scene-based thumbnail.
            if self._db.thumbnail_ram_cache is not None:
                checksum = self._db.get_checksum(image_id)
                if checksum:
                    self._db.thumbnail_ram_cache.remove(checksum)

            count += 1
            self._update_done(count)
            logger.info(f'Video {count}/{len(rows)} done: {basename} ({len(scenes)} scenes)')

        self._current_video = None
        if count > 0:
            logger.info(f'Stage 2b complete: processed {count} videos')
        return count

    # =================================================================
    # Stage 3: Embeddings
    # =================================================================

    def _stage_embeddings(self) -> bool:
        """Compute OpenCLIP embeddings for images and video scenes.

        Images: encode from originals.
        Videos: encode from 400px scene thumbnails.

        Returns:
            True if any embeddings were computed.
        """
        did_work = False

        # 3a: Image embeddings
        img_count = self._embed_images()
        if img_count > 0:
            did_work = True

        # 3b: Video scene embeddings + set preferred scene
        if not self._stopped():
            vid_count = self._embed_video_scenes()
            if vid_count > 0:
                did_work = True

        # 3c: Backfill description embeddings (cheap text encoding for
        # descriptions that predate the feature, or were wiped by a
        # model change).  Re-uses the 'embeddings' stage so the
        # frontend shows progress in the Embedding row.
        if not self._stopped():
            pending = self._db.safe_conn.execute("""
                SELECT COUNT(*) FROM images
                WHERE deleted = 0
                  AND description IS NOT NULL AND description != ''
                  AND description_embedding IS NULL
            """).fetchone()[0]
            if pending > 0:
                self._set_stage('embeddings', pending, 0)
                count = self._db._backfill_description_embeddings(
                    progress_fn=self._update_done,
                )
                if count > 0:
                    did_work = True

        return did_work

    def _embed_images(self) -> int:
        """Compute OpenCLIP embeddings for images missing them.

        Also computes LAION aesthetic scores (dot product with stored embedding).

        Returns:
            Number of images embedded.
        """
        cursor = self._db.safe_conn.execute("""
            SELECT id, path FROM images
            WHERE embedding IS NULL AND deleted = 0 AND media_type = 'image'
            AND width > 0
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        self._set_stage('embeddings', len(rows), 0)
        logger.info(f'Stage 3a: Computing embeddings for {len(rows)} images...')

        clip = self._get_clip_model()
        batch_size = self._db.config.embedding_batch_size

        # Load LAION head for aesthetic scoring
        laion_weight, laion_bias = self._load_laion_head(clip)

        count = 0
        last_log = time.perf_counter()

        def _load_and_preprocess(row) -> tuple[str, torch.Tensor | None]:
            """Load and preprocess an image for CLIP (runs in worker thread)."""
            try:
                img = clip._load_image_safe(Path(row['path']))
                if img is None:
                    return (row['id'], None)
                return (row['id'], clip.preprocess(img))
            except Exception as e:
                logger.debug(f'Failed to preprocess image {row["path"]}: {e}')
                return (row['id'], None)

        # Double-buffered prefetch: load/preprocess next batch on worker
        # threads while the GPU encodes the current batch.
        num_workers = min(4, batch_size)
        prefetch_executor = ThreadPoolExecutor(max_workers=num_workers)
        pending_futures: list[Future] | None = None

        try:
            for batch_start in range(0, len(rows), batch_size):
                if self._stopped() or self._db.gpu_health.state == STATE_DISABLED:
                    break

                batch = rows[batch_start : batch_start + batch_size]

                # Kick off prefetch for this batch (or collect already-running prefetch)
                if pending_futures is None:
                    pending_futures = [prefetch_executor.submit(_load_and_preprocess, row) for row in batch]

                # Collect prefetched tensors
                valid_ids = []
                tensors = []
                for future in pending_futures:
                    image_id, tensor = future.result()
                    if tensor is not None:
                        valid_ids.append(image_id)
                        tensors.append(tensor)

                # Start prefetching the NEXT batch while we encode this one
                next_start = batch_start + batch_size
                if next_start < len(rows) and not self._stopped():
                    next_batch = rows[next_start : next_start + batch_size]
                    pending_futures = [prefetch_executor.submit(_load_and_preprocess, row) for row in next_batch]
                else:
                    pending_futures = None

                if not tensors:
                    done = batch_start + len(batch)
                    self._update_done(done)
                    continue

                # Encode on GPU
                embeddings = clip.encode_tensors_batch(tensors)

                updates = []
                for embedding, image_id in zip(embeddings, valid_ids, strict=True):
                    if embedding is not None:
                        embedding_bytes = embedding.astype(np.float32).tobytes()
                        aesthetic = None
                        if laion_weight is not None:
                            aesthetic = float(embedding @ laion_weight + laion_bias)
                        updates.append((embedding_bytes, aesthetic, datetime.now().isoformat(), image_id))
                        count += 1

                if updates:
                    with self._db.safe_conn:
                        self._db.safe_conn.executemany(
                            'UPDATE images SET embedding = ?, aesthetic_laion = ?, updated_at = ? WHERE id = ?',
                            updates,
                        )
                        self._db.safe_conn.commit()

                done = batch_start + len(batch)
                self._update_done(done)
                elapsed = time.perf_counter()
                if done < len(rows) and elapsed - last_log >= 10.0:
                    logger.info(f'  Embeddings: {done}/{len(rows)}')
                    last_log = elapsed
                time.sleep(0.01)  # Yield GIL
        finally:
            prefetch_executor.shutdown(wait=False, cancel_futures=True)

        if count > 0:
            logger.info(f'Stage 3a complete: embedded {count} images')
        return count

    def _embed_video_scenes(self) -> int:
        """Compute embeddings for video scenes and set preferred scene.

        Returns:
            Number of videos with scenes embedded.
        """
        # Find videos needing scene embedding work:
        # - scenes with NULL embeddings (not yet processed), OR
        # - image-level embedding/preferred_scene_id still NULL
        #   AND at least one scene has a real embedding (length > 0,
        #   i.e. not an empty-blob marker for missing thumbnails).
        cursor = self._db.safe_conn.execute("""
            SELECT DISTINCT i.id, i.path
            FROM images i
            JOIN scenes s ON s.image_id = i.id
            WHERE i.deleted = 0
              AND i.media_type = 'video'
              AND (
                  s.embedding IS NULL
                  OR (
                      (i.embedding IS NULL OR i.preferred_scene_id IS NULL)
                      AND EXISTS (
                          SELECT 1 FROM scenes s2
                          WHERE s2.image_id = i.id
                            AND s2.embedding IS NOT NULL
                            AND length(s2.embedding) > 0
                      )
                  )
              )
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        logger.info(f'Stage 3b: Computing scene embeddings for {len(rows)} videos...')

        clip = self._get_clip_model()
        count = 0

        for row in rows:
            if self._stopped():
                break

            image_id = row['id']

            with self._db.safe_conn:
                scene_rows = self._db.safe_conn.execute(
                    'SELECT id, scene_index FROM scenes WHERE image_id = ? AND embedding IS NULL ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

            if not scene_rows:
                # All scene embeddings are done, but we may still need to
                # set preferred_scene_id or image embedding (the query
                # matched on preferred_scene_id IS NULL or i.embedding IS NULL).
                if self._fix_video_preferred_scene(image_id):
                    count += 1
                continue

            embedding_updates: list[tuple] = []
            scene_embeddings: dict[int, np.ndarray] = {}
            # Scenes whose thumbnails are missing — they can never be
            # embedded, so we mark them with an empty blob to prevent
            # the query from re-matching them every cycle.
            missing_thumb_ids: list[str] = []

            for scene_row in scene_rows:
                if self._stopped():
                    break

                scene_id = scene_row['id']
                scene_idx = scene_row['scene_index']

                # Load 400px scene thumbnail
                prefix = scene_id[:2]
                thumb_path = self._db.thumbnail_dir / 'scenes' / '400' / prefix / f'{scene_id}.jpg'
                if not thumb_path.exists():
                    missing_thumb_ids.append(scene_id)
                    continue

                try:
                    pil_img = Image.open(thumb_path).convert('RGB')
                    embedding = clip.encode_pil_image(pil_img)
                    if embedding is not None:
                        scene_embeddings[scene_idx] = embedding
                        emb_blob = embedding.astype(np.float32).tobytes()
                        now_ts = datetime.now().isoformat()
                        embedding_updates.append((emb_blob, now_ts, scene_id))
                except (MemoryError, RuntimeError) as e:
                    if not is_gpu_error(e):
                        raise
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    if is_oom_error(e):
                        logger.warning(f'OOM embedding video scene: {e}')
                    else:
                        # Context/driver error — stop processing this video
                        logger.warning(f'GPU error embedding video scene: {e} — aborting video')
                        self._db.gpu_health.report_failure('embeddings')
                        break
                except Exception as e:
                    logger.error(f'Error embedding scene {scene_id}: {e}')

            # Write scene embeddings + image embedding in one transaction.
            # This prevents a kill between the two commits from leaving a
            # video with embedded scenes but no image-level embedding
            # (which would be invisible to the re-query on restart).
            did_work = False
            with self._db.safe_conn:
                if embedding_updates:
                    self._db.safe_conn.executemany(
                        'UPDATE scenes SET embedding = ?, updated_at = ? WHERE id = ?',
                        embedding_updates,
                    )
                    did_work = True

                # Mark scenes with missing thumbnails so they stop
                # matching the s.embedding IS NULL query.  An empty
                # blob distinguishes them from genuinely unprocessed
                # scenes (NULL) while being obviously not a real
                # embedding (length 0 vs 2048 bytes).
                if missing_thumb_ids:
                    now_ts = datetime.now().isoformat()
                    self._db.safe_conn.executemany(
                        'UPDATE scenes SET embedding = ?, updated_at = ? WHERE id = ?',
                        [(b'', now_ts, sid) for sid in missing_thumb_ids],
                    )
                    logger.debug(
                        f'Marked {len(missing_thumb_ids)} scenes with missing thumbnails '
                        f'as un-embeddable for video {row["path"]}'
                    )

                # Set preferred scene and image embedding — only if changed
                all_scenes = self._db.safe_conn.execute(
                    'SELECT id, scene_index FROM scenes WHERE image_id = ? ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

                if all_scenes:
                    # Pick the first scene with a real embedding as the
                    # preferred scene.  Scene 0 is ideal, but if its
                    # thumbnail was missing (empty-blob marker) we fall
                    # back to the next available scene.
                    preferred_scene_id = None
                    preferred_emb = None
                    for sc in all_scenes:
                        emb = scene_embeddings.get(sc['scene_index'])
                        if emb is None:
                            emb_row = self._db.safe_conn.execute(
                                'SELECT embedding FROM scenes WHERE id = ?', (sc['id'],)
                            ).fetchone()
                            if emb_row and emb_row['embedding']:
                                emb = np.frombuffer(emb_row['embedding'], dtype=np.float32)
                        if emb is not None:
                            preferred_scene_id = sc['id']
                            preferred_emb = emb
                            break

                    if preferred_emb is not None:
                        # Check current state to avoid no-op updates that
                        # bump updated_at and create perpetually dirty rows
                        current = self._db.safe_conn.execute(
                            'SELECT embedding, preferred_scene_id FROM images WHERE id = ?',
                            (image_id,),
                        ).fetchone()
                        needs_update = (
                            current['embedding'] is None or current['preferred_scene_id'] != preferred_scene_id
                        )

                        if needs_update:
                            rep_blob = preferred_emb.astype(np.float32).tobytes()
                            now_ts = datetime.now().isoformat()
                            self._db.safe_conn.execute(
                                'UPDATE images SET embedding = ?, preferred_scene_id = ?, updated_at = ? WHERE id = ?',
                                (rep_blob, preferred_scene_id, now_ts, image_id),
                            )
                            did_work = True

                self._db.safe_conn.commit()

            if did_work:
                count += 1

        if count > 0:
            logger.info(f'Stage 3b complete: embedded scenes for {count} videos')
        return count

    def _fix_video_preferred_scene(self, image_id: str) -> bool:
        """Set preferred_scene_id and image embedding for a video whose
        scene embeddings are already computed but whose image-level fields
        are still NULL (self-healing for interrupted processing).

        Returns:
            True if the image row was actually updated, False if already correct.
        """
        with self._db.safe_conn:
            all_scenes = self._db.safe_conn.execute(
                'SELECT id, scene_index FROM scenes WHERE image_id = ? ORDER BY scene_index',
                (image_id,),
            ).fetchall()

            if not all_scenes:
                return False

            # Check current state — skip update if already correct
            current = self._db.safe_conn.execute(
                'SELECT embedding, preferred_scene_id FROM images WHERE id = ?',
                (image_id,),
            ).fetchone()

            # Pick the first scene with a real embedding (non-NULL,
            # non-empty — empty blobs mark un-embeddable scenes).
            preferred_scene_id = None
            preferred_emb = None
            for sc in all_scenes:
                emb_row = self._db.safe_conn.execute(
                    'SELECT embedding FROM scenes WHERE id = ?', (sc['id'],)
                ).fetchone()
                if emb_row and emb_row['embedding']:
                    preferred_scene_id = sc['id']
                    preferred_emb = np.frombuffer(emb_row['embedding'], dtype=np.float32)
                    break

            if preferred_emb is not None:
                # Already has correct embedding and preferred_scene_id — no-op
                if current['embedding'] is not None and current['preferred_scene_id'] == preferred_scene_id:
                    return False

                rep_blob = preferred_emb.astype(np.float32).tobytes()
                now_ts = datetime.now().isoformat()
                self._db.safe_conn.execute(
                    'UPDATE images SET embedding = ?, preferred_scene_id = ?, updated_at = ? WHERE id = ?',
                    (rep_blob, preferred_scene_id, now_ts, image_id),
                )
            else:
                # No scene has a usable embedding — at least set
                # preferred_scene_id so this video isn't re-queried
                # every cycle.
                preferred_scene_id = all_scenes[0]['id']
                if current['preferred_scene_id'] == preferred_scene_id:
                    return False

                now_ts = datetime.now().isoformat()
                self._db.safe_conn.execute(
                    'UPDATE images SET preferred_scene_id = ?, updated_at = ? WHERE id = ?',
                    (preferred_scene_id, now_ts, image_id),
                )

            self._db.safe_conn.commit()
            return True

    # LAION aesthetic predictor heads (sa_0_4_*) were trained on embeddings
    # from specific pretrained weights.  Using them with a different pretrained
    # variant produces garbage scores because the embedding geometry differs
    # even when the dimension matches.
    _LAION_HEAD_COMPATIBLE_PRETRAINED: ClassVar[dict[str, set[str]]] = {
        'ViT-B-16': {'openai'},
        'ViT-B-32': {'openai'},
        'ViT-L-14': {'openai'},
    }

    def _load_laion_head(self, clip: OpenCLIPModel) -> tuple[np.ndarray | None, float | None]:
        """Load the LAION aesthetic predictor head weights.

        Returns (None, None) if the head is missing, incompatible with the
        current model architecture, or incompatible with the current
        pretrained weights (the heads were trained on ``openai`` embeddings).

        Returns:
            Tuple of (weight, bias) or (None, None) if unavailable.
        """
        # Check pretrained compatibility before loading the file
        compatible = self._LAION_HEAD_COMPATIBLE_PRETRAINED.get(clip.model_name, set())
        if clip.pretrained not in compatible:
            if not getattr(self, '_laion_warned', False):
                logger.info(
                    f'LAION aesthetic head not compatible with '
                    f'{clip.model_name}/{clip.pretrained} '
                    f'(trained for: {", ".join(sorted(compatible)) or "no known variants"}) '
                    f'— LAION scoring disabled, using NIMA only'
                )
                self._laion_warned = True
            return None, None

        head_path = self._db.db_path.parent / '.laion-aesthetic-head.pth'
        if not head_path.exists():
            if not getattr(self, '_laion_warned', False):
                logger.warning(
                    'LAION aesthetic head not found — aesthetic scoring disabled. '
                    'Run "python download_models.py" to download it.'
                )
                self._laion_warned = True
            return None, None

        try:
            state_dict = torch.load(str(head_path), map_location='cpu', weights_only=True)
            weight = state_dict['weight'].numpy().flatten()
            bias = float(state_dict['bias'].item())

            embed_dim = clip.model.visual.output_dim
            if len(weight) != embed_dim:
                logger.warning(
                    f'LAION head dimension mismatch: head has {len(weight)}, '
                    f'CLIP model has {embed_dim}. Aesthetic scoring disabled.'
                )
                return None, None

            logger.info(f'LAION aesthetic head loaded ({len(weight)}D)')
            return weight, bias
        except Exception as e:
            logger.warning(f'Failed to load LAION aesthetic head: {e}')
            return None, None

    # =================================================================
    # Stage 4: Scoring (NIMA + LAION backfill)
    # =================================================================

    def _stage_scoring(self) -> bool:
        """Score images with NIMA and backfill LAION scores.

        Returns:
            True if any scoring was done.
        """
        did_work = False

        # 4a: NIMA scoring
        if self._db.config.nima_enabled and not self._stopped():
            nima_count = self._score_nima()
            if nima_count > 0:
                did_work = True

        # 4b: LAION backfill (images with embeddings but no LAION score)
        if not self._stopped():
            laion_count = self._backfill_laion()
            if laion_count > 0:
                did_work = True

        return did_work

    def _score_nima(self) -> int:
        """Score images missing NIMA aesthetic scores.

        Loads original images (not thumbnails) for accurate scoring.
        Uses threaded prefetching to overlap disk I/O with GPU inference:
        a ThreadPoolExecutor loads the next batch's images while the GPU
        scores the current batch.

        Returns:
            Number of images scored.
        """
        # Check model availability before querying DB (avoids repeated warnings)
        checkpoint_path = self._db.db_path.parent / '.nima-mobilenetv2-ava.pth'
        if not checkpoint_path.exists():
            if not getattr(self, '_nima_warned', False):
                logger.warning(
                    'NIMA checkpoint not found — aesthetic scoring disabled. '
                    'Run "python download_models.py" to download it.'
                )
                self._nima_warned = True
            return 0

        cursor = self._db.safe_conn.execute("""
            SELECT id, path FROM images
            WHERE aesthetic_nima IS NULL AND deleted = 0
              AND media_type = 'image'
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        self._set_stage('scoring', len(rows), 0)
        logger.info(f'Stage 4a: NIMA scoring {len(rows)} images...')

        try:
            from nima import load_nima_model, score_images_batch

            device = self._db.gpu_health.device

            logger.info('Loading NIMA model...')
            t0 = time.perf_counter()
            model = load_nima_model(str(checkpoint_path), device=device)
            logger.info('NIMA model loaded (%.1fs)', time.perf_counter() - t0)
        except (MemoryError, RuntimeError) as e:
            if is_gpu_error(e):
                logger.error(f'GPU error loading NIMA model: {e}')
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                if not is_oom_error(e):
                    self._db.gpu_health.report_failure('scoring')
                return 0
            raise
        except Exception as e:
            logger.warning(f'Failed to load NIMA model: {e}')
            return 0

        batch_size = self._db.config.nima_batch_size
        count = 0
        last_log = time.perf_counter()

        def _load_image(row) -> tuple[str, Image.Image | None]:
            """Load an original image for NIMA scoring (runs in worker thread)."""
            try:
                img = raw_open_image(row['path'])
                return (row['id'], img)
            except Exception as e:
                logger.debug(f'Failed to load image for NIMA: {row["id"]}: {e}')
                return (row['id'], None)

        # Double-buffered prefetch: load next batch on worker threads while
        # GPU scores current batch.  Each worker reads the original image
        # from disk; score_images_batch handles the Resize/Crop/Normalize.
        num_workers = min(4, batch_size)
        prefetch_executor = ThreadPoolExecutor(max_workers=num_workers)
        pending_futures: list[Future] | None = None

        try:
            for batch_start in range(0, len(rows), batch_size):
                if self._stopped() or self._db.gpu_health.state == STATE_DISABLED:
                    break

                batch = rows[batch_start : batch_start + batch_size]

                # Kick off prefetch for this batch (or collect already-running prefetch)
                if pending_futures is None:
                    # First batch — submit and wait
                    pending_futures = [prefetch_executor.submit(_load_image, row) for row in batch]

                # Collect prefetched images
                valid_ids = []
                pil_images = []
                failed_ids = []
                for future in pending_futures:
                    image_id, img = future.result()
                    if img is not None:
                        valid_ids.append(image_id)
                        pil_images.append(img)
                    else:
                        failed_ids.append(image_id)

                # Start prefetching the NEXT batch while we score this one
                next_start = batch_start + batch_size
                if next_start < len(rows) and not self._stopped():
                    next_batch = rows[next_start : next_start + batch_size]
                    pending_futures = [prefetch_executor.submit(_load_image, row) for row in next_batch]
                else:
                    pending_futures = None

                # Write sentinel score (0.0) for images that couldn't be loaded
                # so they aren't retried every pipeline cycle.
                if failed_ids:
                    ts = datetime.now().isoformat()
                    sentinel_updates = [(0.0, ts, fid) for fid in failed_ids]
                    try:
                        with self._db.safe_conn:
                            self._db.safe_conn.executemany(
                                'UPDATE images SET aesthetic_nima = ?, updated_at = ? WHERE id = ?',
                                sentinel_updates,
                            )
                            self._db.safe_conn.commit()
                    except Exception:
                        pass  # Best-effort — will retry next cycle

                if not pil_images:
                    self._update_done(batch_start + len(batch))
                    continue

                # Score batch — with OOM fallback (single-item) or CUDA error (abort)
                try:
                    scores = score_images_batch(model, pil_images, device=device)
                except (MemoryError, RuntimeError) as e:
                    if not is_gpu_error(e):
                        raise
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()

                    if not is_oom_error(e):
                        # Context/driver error — single-item fallback will also fail
                        logger.warning(f'GPU error scoring NIMA batch: {e} — aborting stage')
                        self._db.gpu_health.report_failure('scoring')
                        break

                    # OOM — try single-item fallback
                    logger.warning(f'OOM scoring NIMA batch of {len(pil_images)}, falling back to single')
                    scores = []
                    for i, img in enumerate(pil_images):
                        try:
                            single = score_images_batch(model, [img], device=device)
                            scores.append(single[0])
                        except (MemoryError, RuntimeError):
                            logger.error(f'OOM scoring single image {valid_ids[i]}, skipping')
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                            scores.append(None)

                    paired = [(s, vid) for s, vid in zip(scores, valid_ids, strict=True) if s is not None]
                    if not paired:
                        self._update_done(batch_start + len(batch))
                        continue
                    scores, valid_ids = zip(*paired, strict=True)
                    scores = list(scores)
                    valid_ids = list(valid_ids)

                # Close PIL images to release file handles
                for img in pil_images:
                    img.close()

                # Batch commit — if the DB is locked (e.g. by the log handler
                # or a concurrent Flask request), skip this batch rather than
                # crashing the entire stage.  The unscored images will be
                # picked up on the next pipeline cycle.
                ts = datetime.now().isoformat()
                updates = [(score, ts, vid) for score, vid in zip(scores, valid_ids, strict=True)]

                try:
                    with self._db.safe_conn:
                        self._db.safe_conn.executemany(
                            'UPDATE images SET aesthetic_nima = ?, updated_at = ? WHERE id = ?',
                            updates,
                        )
                        self._db.safe_conn.commit()
                    count += len(updates)
                except Exception as e:
                    logger.warning(f'Failed to commit NIMA batch ({len(updates)} scores): {e}')
                    try:
                        self._db.safe_conn.rollback()
                    except Exception:
                        pass
                done = batch_start + len(batch)
                self._update_done(done)
                elapsed = time.perf_counter()
                if done < len(rows) and elapsed - last_log >= 10.0:
                    logger.info(f'  NIMA scoring: {done}/{len(rows)}')
                    last_log = elapsed
                time.sleep(0.01)
        finally:
            prefetch_executor.shutdown(wait=False, cancel_futures=True)

        # Unload NIMA model to free GPU memory for subsequent stages
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        logger.info(f'Stage 4a complete: scored {count} images')
        if count > 0:
            self._db.event_queue.emit('nima_complete', {'scored_count': count})
        return count

    def _backfill_laion(self) -> int:
        """Compute LAION aesthetic scores for images with embeddings but no score.

        This is a cheap CPU operation — dot products on existing embedding blobs.

        Returns:
            Number of images scored.
        """
        # Check LAION head compatibility before querying the DB — the query
        # fetches all embedding BLOBs (~86MB for 44k images) which is wasted
        # if the head is incompatible with the current pretrained weights.
        clip = self._get_clip_model()
        laion_weight, laion_bias = self._load_laion_head(clip)
        if laion_weight is None:
            return 0

        cursor = self._db.safe_conn.execute("""
            SELECT id, embedding FROM images
            WHERE aesthetic_laion IS NULL AND embedding IS NOT NULL AND deleted = 0
              AND media_type = 'image'
        """)
        rows = cursor.fetchall()

        if not rows:
            return 0

        logger.info(f'Stage 4b: Computing LAION scores for {len(rows)} images...')

        updates = []
        now = datetime.now().isoformat()
        for row in rows:
            emb = np.frombuffer(row['embedding'], dtype=np.float32)
            score = float(emb @ laion_weight + laion_bias)
            updates.append((score, now, row['id']))

        if updates:
            # Process in chunks to avoid holding lock too long
            chunk_size = 1000
            for i in range(0, len(updates), chunk_size):
                chunk = updates[i : i + chunk_size]
                try:
                    with self._db.safe_conn:
                        self._db.safe_conn.executemany(
                            'UPDATE images SET aesthetic_laion = ?, updated_at = ? WHERE id = ?',
                            chunk,
                        )
                        self._db.safe_conn.commit()
                except Exception as e:
                    logger.warning(f'Failed to commit LAION batch ({len(chunk)} scores): {e}')
                    try:
                        self._db.safe_conn.rollback()
                    except Exception:
                        pass

        count = len(updates)
        logger.info(f'Stage 4b complete: scored {count} images')
        return count

    # =================================================================
    # Stage 5: Face Detection
    # =================================================================

    def _stage_faces(self) -> bool:
        """Detect faces in images using 400px thumbnails.

        Handles both new images (no face records) and rescan images
        (``needs_face_rescan = 1``, e.g. after a face detection config
        change).  Both go through the same batched GPU detection
        pipeline; the only difference is at write time — rescan images
        use ``reconcile_detected_faces()`` which preserves named/ignored
        faces and removes stale unnamed ones.  New images also use the
        same reconciliation path (no existing faces → all detections
        are simply inserted).

        Returns:
            True if any faces were detected.
        """
        if not self._db.config.face_detection_enabled:
            return False

        # New images: no face records at all
        cursor = self._db.safe_conn.execute("""
            SELECT i.id, i.checksum, i.width, i.height, 0 AS rescan
            FROM images i
            WHERE i.deleted = 0
              AND i.media_type = 'image'
              AND i.checksum IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.image_id = i.id)
        """)
        new_rows = cursor.fetchall()

        # Rescan images: flagged for re-detection after config change
        cursor = self._db.safe_conn.execute("""
            SELECT i.id, i.checksum, i.width, i.height, 1 AS rescan
            FROM images i
            WHERE i.deleted = 0
              AND i.media_type = 'image'
              AND i.checksum IS NOT NULL
              AND i.needs_face_rescan = 1
        """)
        rescan_rows = cursor.fetchall()

        rows = new_rows + rescan_rows

        if not rows:
            return False

        rescan_ids = {r['id'] for r in rescan_rows}
        if rescan_rows:
            logger.info('Stage 5: %d new images + %d rescan images', len(new_rows), len(rescan_rows))

        self._set_stage('faces', len(rows), 0)
        logger.info(f'Stage 5: Detecting faces in {len(rows)} images...')

        # Scale min_face_size for MTCNN: the user's config value is in
        # original-image pixels, but we feed 400px thumbnails to MTCNN.
        # MTCNN's min_face_size pre-filter operates on the input (thumbnail)
        # resolution, so we must scale down proportionally.  Use the largest
        # original image to compute the worst-case ratio — this ensures no
        # face that would pass the post-filter is rejected early by MTCNN.
        # The post-filter (which knows each image's exact scale) still
        # enforces the user's configured threshold precisely.
        configured_min = self._db.config.face_detection_min_size
        max_dim = max((max(r['width'] or 0, r['height'] or 0) for r in rows), default=400)
        if max_dim > 400:
            mtcnn_min = max(10, int(configured_min * 400 / max_dim))
        else:
            mtcnn_min = configured_min
        face_detector = FaceDetector(
            min_confidence=self._db.config.face_detection_min_confidence,
            min_face_size=mtcnn_min,
        )

        clip = self._get_clip_model()
        batch_size = self._db.config.face_detection_batch_size
        processed_count = 0
        faces_detected_count = 0
        last_log = time.perf_counter()

        def _prepare_batch(batch_rows, detector=face_detector):
            """Build path mappings and preload images for a face detection batch.

            Returns (id_to_path, path_to_id, id_to_orig_path, id_to_thumb_scale,
            loaded_images) or None if the batch has no loadable images.
            """
            id_to_path: dict[str, Path] = {}
            path_to_id: dict[Path, str] = {}
            id_to_orig_path: dict[str, Path] = {}
            id_to_thumb_scale: dict[str, float] = {}

            for row in batch_rows:
                image_id = row['id']
                checksum = row['checksum']
                orig_w = row['width'] or 0
                orig_h = row['height'] or 0

                thumb_path = get_thumbnail_cache_path(checksum, 400, thumbnail_dir=self._db.thumbnail_dir)
                if not Path(thumb_path).exists():
                    img_row = self._db.safe_conn.execute('SELECT path FROM images WHERE id = ?', (image_id,)).fetchone()
                    if img_row:
                        orig_path = Path(img_row['path'])
                        if orig_path.exists():
                            id_to_path[image_id] = orig_path
                            path_to_id[orig_path] = image_id
                            id_to_orig_path[image_id] = orig_path
                            id_to_thumb_scale[image_id] = 1.0
                    continue

                thumb_p = Path(thumb_path)
                id_to_path[image_id] = thumb_p
                path_to_id[thumb_p] = image_id
                orig_max = max(orig_w, orig_h)
                id_to_thumb_scale[image_id] = (400.0 / orig_max) if orig_max > 400 else 1.0
                img_row = self._db.safe_conn.execute('SELECT path FROM images WHERE id = ?', (image_id,)).fetchone()
                if img_row:
                    id_to_orig_path[image_id] = Path(img_row['path'])

            if not id_to_path:
                return None

            paths = list(id_to_path.values())
            loaded = detector.preload_images_batch(paths, num_workers=4)

            # Patch scale factors: preload returns scale relative to the
            # thumbnail; we need scale relative to the original image.
            patched = []
            for path, img, preload_scale in loaded:
                mid = path_to_id.get(path)
                ts = id_to_thumb_scale.get(mid, 1.0) if mid else 1.0
                patched.append((path, img, preload_scale * ts))

            if not patched:
                return None

            return (id_to_path, path_to_id, id_to_orig_path, id_to_thumb_scale, patched)

        # Double-buffered prefetch: prepare the next batch (path mapping +
        # threaded image loading) while the GPU detects faces in the current
        # batch and results are written to the DB.
        prefetch_executor = ThreadPoolExecutor(max_workers=1)
        pending_prefetch: Future | None = None

        for batch_start in range(0, len(rows), batch_size):
            if self._stopped():
                break

            batch = rows[batch_start : batch_start + batch_size]

            # Collect current batch's prepared data
            if pending_prefetch is not None:
                prep = pending_prefetch.result()
            else:
                prep = _prepare_batch(batch)

            # Start prefetching the NEXT batch while we process this one
            next_start = batch_start + batch_size
            if next_start < len(rows) and not self._stopped():
                next_batch = rows[next_start : next_start + batch_size]
                pending_prefetch = prefetch_executor.submit(_prepare_batch, next_batch)
            else:
                pending_prefetch = None

            if prep is None:
                processed_count += len(batch)
                self._update_done(processed_count)
                continue

            _id_to_path, path_to_id, id_to_orig_path, _id_to_thumb_scale, loaded_images = prep

            results = face_detector.detect_faces_from_preloaded(loaded_images, stop_event=self._stop_event)

            # Get known face embeddings for auto-recognition
            with self._db.safe_conn:
                known_embeddings = get_all_known_face_embeddings(self._db.safe_conn)
                cursor2 = self._db.safe_conn.execute('SELECT id, name, recognition_threshold FROM people')
                per_person_thresholds: dict[str, float | None] = {}
                ignored_person_ids: set[str] = set()
                for prow in cursor2.fetchall():
                    per_person_thresholds[prow['id']] = prow['recognition_threshold']
                    if prow['name'] == '-':
                        ignored_person_ids.add(prow['id'])

            # Process detection results via reconciliation (handles both
            # new images and rescan images through one code path).
            batch_faces_added = 0

            for path, detected_faces in results.items():
                if path not in path_to_id:
                    continue
                image_id = path_to_id[path]
                orig_path = id_to_orig_path.get(image_id, path)

                with self._db.safe_conn:
                    result = reconcile_detected_faces(
                        conn=self._db.safe_conn,
                        image_id=image_id,
                        orig_path=orig_path,
                        detected_faces=detected_faces or [],
                        known_embeddings=known_embeddings,
                        per_person_thresholds=per_person_thresholds,
                        ignored_person_ids=ignored_person_ids,
                        recognition_threshold=self._db.config.face_recognition_threshold,
                        thumbnail_dir=self._db.thumbnail_dir,
                        thumbnail_quality=self._db.config.thumbnail_quality,
                        clip_model=clip,
                    )

                    # Clear rescan flag if this was a rescan image
                    if image_id in rescan_ids:
                        self._db.safe_conn.execute(
                            'UPDATE images SET needs_face_rescan = 0 WHERE id = ?',
                            (image_id,),
                        )
                        self._db.safe_conn.commit()

                batch_faces_added += result['added']
                faces_detected_count += result['added'] + result['kept']
                processed_count += 1

            self._update_done(processed_count)
            now = time.perf_counter()
            if processed_count < len(rows) and now - last_log >= 10.0:
                logger.info(f'  Face detection: {processed_count}/{len(rows)} images')
                last_log = now

            time.sleep(0.01)

        prefetch_executor.shutdown(wait=False, cancel_futures=True)

        # Unload face detector to free GPU memory
        del face_detector
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if processed_count > 0:
            logger.info(f'Stage 5 complete: processed {processed_count} images, detected {faces_detected_count} faces')
        return faces_detected_count > 0

    # =================================================================
    # Stage 6: Grouping
    # =================================================================

    def _stage_grouping(self) -> bool:
        """Run directory groups, face reassessment, duplicates, face groups.

        Always returns False — grouping is post-processing housekeeping
        that should not cause the pipeline loop to re-cycle on its own.

        Returns:
            Always False (grouping alone doesn't warrant a re-run).
        """
        from imagedb import emit_processing_complete

        logger.info('Stage 6: Running grouping...')

        # 6a: Sync directory groups (always)
        try:
            self._db._duplicate_manager.sync_directory_groups(self._db.safe_conn)
        except Exception as e:
            logger.error(f'Failed to sync directory groups: {e}')

        # 6b: Face reassessment
        if self._db.config.face_detection_enabled and not self._stopped():
            try:
                with self._db.safe_conn:
                    delete_people_without_faces(self._db.safe_conn)
                self._db._reassess_faces_with_status()
            except Exception as e:
                logger.error(f'Failed during face reassessment: {e}')

        # 6c: Compute duplicates
        if not self._stopped():
            try:
                self._set_stage('grouping', 0, 0)
                self._db._compute_duplicates_with_status()
            except Exception as e:
                logger.error(f'Failed to compute duplicates: {e}')

        # 6d: Compute unknown face groups
        if self._db.config.face_detection_enabled and not self._stopped():
            try:
                compute_unknown_face_groups(
                    self._db.safe_conn,
                    threshold=self._db.config.face_recognition_threshold,
                )
            except Exception as e:
                logger.error(f'Failed to compute unknown face groups: {e}')

            # 6e: Backfill face semantic embeddings
            try:
                self._db.backfill_face_semantic_embeddings()
            except Exception as e:
                logger.error(f'Failed to backfill face semantic embeddings: {e}')

        emit_processing_complete(self._db.event_queue)
        logger.info('Stage 6 complete')
        return False

    # =================================================================
    # Stage 7: STT Transcription
    # =================================================================

    def _stage_stt(self) -> bool:
        """Transcribe videos with null transcriptions.

        Interruptible: checks _rerun_requested between videos so rescans
        don't wait for slow transcription to finish.

        Returns:
            True if any videos were transcribed.
        """
        # Backfill transcription embeddings regardless of whether STT is
        # enabled — a model change may have wiped embeddings for
        # transcriptions that were computed while STT was previously on.
        if not self._stopped():
            pending = self._db.safe_conn.execute("""
                SELECT COUNT(*) FROM scenes
                WHERE transcription IS NOT NULL AND transcription != ''
                  AND transcription_embedding IS NULL
            """).fetchone()[0]
            if pending > 0:
                self._set_stage('transcription', pending, 0)
                self._db._backfill_transcription_embeddings(
                    progress_fn=self._update_done,
                )

        if not self._db.config.stt_enabled:
            return False

        cursor = self._db.safe_conn.execute("""
            SELECT DISTINCT i.id, i.path, i.basename, i.stt_language
            FROM images i
            JOIN scenes s ON s.image_id = i.id
            WHERE i.deleted = 0
              AND i.media_type = 'video'
              AND s.transcription IS NULL
              AND i.preferred_scene_id IS NOT NULL
              AND i.embedding IS NOT NULL
        """)
        rows = cursor.fetchall()

        if not rows:
            return False

        self._set_stage('transcription', len(rows), 0)
        logger.info(f'Stage 7: Transcribing {len(rows)} videos...')

        count = 0
        for row in rows:
            if self._stopped() or self._rerun_requested:
                break

            image_id = row['id']
            video_path = Path(row['path'])
            basename = row['basename']

            if not video_path.exists():
                continue

            self._current_video = {
                'label': basename,
                'step': 'Transcribing',
                'step_index': 1,
                'total_steps': 1,
                'done': count,
                'total': len(rows),
            }

            # Load scene boundaries
            with self._db.safe_conn:
                scene_rows = self._db.safe_conn.execute(
                    'SELECT id, start_time, end_time FROM scenes WHERE image_id = ? ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

            if not scene_rows:
                continue

            scenes = [(r['start_time'], r['end_time']) for r in scene_rows]
            scene_ids = [r['id'] for r in scene_rows]

            logger.info(f'Transcribing {basename} ({len(scenes)} scenes)')
            clip = self._get_clip_model()
            self._transcribe_scenes(
                image_id,
                video_path,
                scenes,
                scene_ids,
                clip,
                language=row['stt_language'],
            )

            # Notify frontend clients that this video's scenes have been
            # (re-)transcribed so they can refresh the scene cache.
            self._db.event_queue.emit('images_changed', {'updated_ids': [image_id]})

            count += 1
            self._update_done(count)

        self._current_video = None
        if count > 0:
            logger.info(f'Stage 7 complete: transcribed {count} videos')
        return count > 0

    def _mark_scenes_silent(self, scene_ids: list[str], image_id: str) -> None:
        """Set ``transcription = ''`` on all given scenes.

        Called when a video has no audio stream or no detected speech, so
        that Stage 7's ``WHERE transcription IS NULL`` query won't
        perpetually re-select the video for transcription.
        """
        now = datetime.now().isoformat()
        with self._db.safe_conn:
            self._db.safe_conn.executemany(
                "UPDATE scenes SET transcription = '', updated_at = ? WHERE id = ?",
                [(now, sid) for sid in scene_ids],
            )
            self._db.safe_conn.commit()

    def _transcribe_scenes(
        self,
        image_id: str,
        video_path: Path,
        scenes: list[tuple[float, float]],
        scene_ids: list[str],
        clip: OpenCLIPModel,
        language: str | None = None,
    ) -> None:
        """Transcribe the entire video and assign text to scenes by overlap.

        Args:
            image_id: The video's image ID.
            video_path: Path to the video file.
            scenes: List of (start, end) scene boundaries.
            scene_ids: Corresponding scene UUIDs.
            clip: OpenCLIP model for computing text embeddings.
            language: Per-video language override from ``images.stt_language``.
                None or NULL means use the global ``stt_language`` config.
                Empty string means auto-detect.
        """
        stt = self._get_stt_backend()
        if stt is None:
            return

        if self._stopped():
            return

        try:
            # Extract audio for the entire clip
            tmp_fd, tmp_name = tempfile.mkstemp(suffix='.wav')
            os.close(tmp_fd)
            tmp_path = Path(tmp_name)

            total_start = scenes[0][0] if scenes else 0.0
            total_end = scenes[-1][1] if scenes else 0.0

            if not extract_audio_segment(video_path, tmp_path, total_start, total_end):
                tmp_path.unlink(missing_ok=True)
                # No audio stream — mark all scenes as transcribed (empty)
                # so Stage 7 won't re-select this video on the next run.
                self._mark_scenes_silent(scene_ids, image_id)
                return

            # Resolve effective language: per-video override > global config
            effective_language = language if language is not None else self._db.config.stt_language
            stt_result = stt.transcribe(tmp_path, language=effective_language)
            stt_segments = stt_result.segments
            detected_language = stt_result.language

            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

            if not stt_segments:
                # Audio present but no speech detected — mark all scenes
                # as transcribed (empty) to prevent re-selection.
                self._mark_scenes_silent(scene_ids, image_id)
                return

            # Assign STT segments to scenes by midpoint — each word lands
            # in exactly one scene, avoiding duplicates at boundaries.
            # STT timestamps are relative to the extracted audio clip, so
            # offset them by total_start to align with absolute scene times.
            transcription_updates: list[tuple] = []
            for (scene_start, scene_end), scene_id in zip(scenes, scene_ids, strict=False):
                if self._stopped():
                    return

                scene_texts: list[str] = []
                for seg in stt_segments:
                    seg_start = getattr(seg, 'start', 0.0) + total_start
                    seg_end = getattr(seg, 'end', 0.0) + total_start
                    mid = (seg_start + seg_end) / 2.0
                    if scene_start <= mid < scene_end:
                        text = getattr(seg, 'text', '').strip()
                        if text:
                            scene_texts.append(text)

                full_text = ' '.join(scene_texts) if scene_texts else ''
                text_emb = clip.encode_text(full_text) if clip and full_text else None
                text_emb_blob = text_emb.astype(np.float32).tobytes() if text_emb is not None else None

                now_ts = datetime.now().isoformat()
                transcription_updates.append((full_text, text_emb_blob, now_ts, scene_id))

            if transcription_updates:
                now = datetime.now().isoformat()
                with self._db.safe_conn:
                    self._db.safe_conn.executemany(
                        """UPDATE scenes SET transcription = ?, transcription_embedding = ?,
                               updated_at = ?
                        WHERE id = ?""",
                        transcription_updates,
                    )
                    # Write detected language back to images.stt_language,
                    # but only when the user hasn't explicitly chosen a language
                    if detected_language:
                        self._db.safe_conn.execute(
                            'UPDATE images SET stt_language = ?, updated_at = ? '
                            "WHERE id = ? AND (stt_language IS NULL OR stt_language = '')",
                            (detected_language, now, image_id),
                        )
                    self._db.safe_conn.commit()

        except Exception as e:
            # Rollback any uncommitted transaction to prevent cascade failures.
            # Without this, a failed commit (e.g. "database is locked") leaves
            # the connection dirty, causing ALL subsequent operations to fail.
            try:
                self._db.safe_conn.rollback()
            except Exception:
                pass
            logger.error(f'STT failed for video {video_path}: {e}')
