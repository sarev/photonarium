"""Sequential Pipeline Orchestrator for Photonarium.

Replaces the five concurrent processing threads (IngestionThread,
EmbeddingThread, FaceDetectionThread, NimaThread, VideoProcessingThread)
with a single thread that runs seven stages sequentially.

Benefits:
- No GPU contention (only one model loaded at a time)
- No DB lock contention between stages
- Self-healing: each stage queries DB for items with null values, so
  interrupted processing resumes naturally on restart
- Simpler control flow (no callback chains)

Stages:
1. Ingestion — walk folders, create/update DB records
2. Thumbnails — generate missing image + scene thumbnails
3. Embeddings — OpenCLIP on originals (images) / scene thumbs (videos)
4. Scoring — NIMA + LAION aesthetic scores
5. Faces — MTCNN + InceptionResnetV1 on 400px image thumbnails
6. Grouping — directory groups, face reassessment, duplicates, face groups
7. STT — transcribe videos with null transcription
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import torch
from PIL import Image

from faces import (
    FaceDetector,
    compute_unknown_face_groups,
    delete_people_without_faces,
    find_best_match,
    generate_face_thumbnail,
    get_all_known_face_embeddings,
    get_face_thumbnail_path,
)
from metadata import derive_timestamp_with_confidence, extract_exif_data
from thumbnails import generate_thumbnail, get_thumbnail_cache_path
from video import extract_keyframe_thumbnail, get_video_metadata, is_video_supported

if TYPE_CHECKING:
    from imagedb import ImageDatabase, OpenCLIPModel

logger = logging.getLogger(__name__)


class PipelineOrchestrator(threading.Thread):
    """Sequential pipeline — runs 7 stages one at a time.

    Self-healing: each stage queries DB for items with null values,
    so interrupted processing resumes on restart.
    """

    def __init__(
        self,
        db: ImageDatabase,
        stop_event: threading.Event,
        pause_event: threading.Event,
        run_face_detection: bool = False,
        run_face_grouping: bool = False,
    ):
        """Initialise the pipeline orchestrator.

        Args:
            db: ImageDatabase instance (provides conn, config, locks, etc.).
            stop_event: Event to signal thread shutdown.
            pause_event: Event to temporarily pause ingestion.
            run_face_detection: Whether to run face detection (Stage 5).
            run_face_grouping: Whether to run grouping (Stage 6).
        """
        super().__init__(name='PipelineOrchestrator', daemon=True)
        self._db = db
        self._stop_event = stop_event
        self._pause_event = pause_event
        self._run_face_detection = run_face_detection
        self._run_face_grouping = run_face_grouping

        # Stage progress (read by get_processing_status)
        self._current_stage: str | None = None
        self._stage_total = 0
        self._stage_done = 0
        self._stage_lock = threading.Lock()

        # Re-entrancy: request_rerun() sets this to restart from Stage 1
        self._rerun_requested = False

        # Lazy-loaded models (one at a time — no concurrent GPU contention)
        self._clip_model: OpenCLIPModel | None = None
        self._stt_backend = None
        self._stt_loaded = False

        # Per-video progress tracking (for status endpoint)
        self._current_video: dict[str, Any] | None = None

        # Finalization flag: when True, stages 6-7 (grouping/STT) will run
        # even if data stages found no work.  Set on startup (self-healing
        # for interrupted grouping/STT) and by request_rerun() (rescans,
        # --transcribe-videos, etc.).
        self._finalization_requested = True

        # Thread-local storage for per-worker DB connections (Stage 1)
        self._thread_local = threading.local()
        self._worker_conns: list[sqlite3.Connection] = []
        self._worker_conns_lock = threading.Lock()

    # -----------------------------------------------------------------
    # Public interface
    # -----------------------------------------------------------------

    def request_rerun(self) -> None:
        """Called from rescan methods to trigger a new pipeline cycle."""
        self._rerun_requested = True
        self._finalization_requested = True

    @property
    def run_face_detection(self) -> bool:
        """Whether face detection is enabled for this cycle."""
        return self._run_face_detection

    @run_face_detection.setter
    def run_face_detection(self, value: bool) -> None:
        self._run_face_detection = value

    @property
    def run_face_grouping(self) -> bool:
        """Whether grouping is enabled for this cycle."""
        return self._run_face_grouping

    @run_face_grouping.setter
    def run_face_grouping(self, value: bool) -> None:
        self._run_face_grouping = value

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
                self._stop_event.wait(timeout=2.0)
            # If _rerun_requested was set during pipeline, loop immediately

        # Clean up worker connections
        self._close_worker_conns()
        logger.info('Pipeline orchestrator stopped')

    def _run_pipeline(self) -> bool:
        """Run all stages. Returns True if any stage did work.

        Stages 1-5 are "data" stages that find items needing work via DB
        queries.  Stage 6 (grouping) and Stage 7 (STT) only run when a
        preceding stage did work, avoiding repeated no-op grouping every
        poll cycle.
        """
        had_work = False

        data_stages = [
            ('ingestion', self._stage_ingestion),
            ('thumbnails', self._stage_thumbnails),
            ('embeddings', self._stage_embeddings),
            ('scoring', self._stage_scoring),
            ('faces', self._stage_faces),
        ]

        for stage_name, stage_fn in data_stages:
            if self._stop_event.is_set():
                break
            try:
                stage_did_work = stage_fn()
                if stage_did_work:
                    had_work = True
                    logger.debug(f'Pipeline stage "{stage_name}" reported work')
            except Exception:
                logger.exception(f'Error in pipeline stage "{stage_name}"')
                try:
                    self._db.conn.rollback()
                except Exception:
                    pass

        # Run grouping and STT when data stages did work, OR when
        # finalization was explicitly requested (startup self-healing,
        # rescans, --transcribe-videos, etc.).
        run_finalization = had_work or self._finalization_requested
        self._finalization_requested = False

        if run_finalization and not self._stop_event.is_set():
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
                        self._db.conn.rollback()
                    except Exception:
                        pass

        return had_work

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

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
        """Get or create the shared OpenCLIP model."""
        if self._clip_model is None:
            from imagedb import OpenCLIPModel

            self._clip_model = OpenCLIPModel(
                model_name=self._db.config.openclip_model,
                pretrained=self._db.config.openclip_pretrained,
                max_dimension=self._db.config.max_image_dimension,
            )
        return self._clip_model

    def _get_worker_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local database connection for worker threads.

        Each ThreadPoolExecutor worker gets its own SQLite connection, avoiding
        contention on the shared connection.
        """
        conn = getattr(self._thread_local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(str(self._db.db_path), timeout=10.0)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=10000')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.row_factory = sqlite3.Row
            self._thread_local.conn = conn
            with self._worker_conns_lock:
                self._worker_conns.append(conn)
        return conn

    def _close_worker_conns(self) -> None:
        """Close all per-thread worker connections."""
        with self._worker_conns_lock:
            for wconn in self._worker_conns:
                try:
                    wconn.close()
                except Exception:
                    pass
            self._worker_conns.clear()

    def _get_stt_backend(self):
        """Lazy-load the STT backend."""
        if not self._stt_loaded:
            self._stt_loaded = True
            from stt import get_stt_backend

            self._stt_backend = get_stt_backend(self._db.config)
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

        folders = get_folders(self._db.conn)
        if not folders:
            return False

        folder_paths = [f['path'] for f in folders]

        # Collect all file paths from registered folders
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

        # Retry tracking for transient "database is locked" errors
        retry_counts: dict[Path, int] = {}
        max_retries = 5

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

                # Check for completed futures
                if pending_futures:
                    done_futures = [f for f in pending_futures if f.done()]
                    for future in done_futures:
                        path = pending_futures.pop(future)
                        try:
                            was_changed = future.result()
                            processed_count += 1
                            if was_changed:
                                changed_count += 1
                            retry_counts.pop(path, None)
                        except Exception as e:
                            retries = retry_counts.get(path, 0)
                            if 'database is locked' in str(e) and retries < max_retries:
                                retry_counts[path] = retries + 1
                                # Re-submit for retry
                                found_paths.add(str(path))
                                future2 = executor.submit(
                                    self._process_file,
                                    path,
                                    extract_image_metadata,
                                    create_image,
                                    update_image_metadata,
                                    get_image_by_path,
                                    canonicalise_path,
                                    _upsert_image_metadata,
                                )
                                pending_futures[future2] = path
                                logger.debug(
                                    f'Re-queued {path} after "database is locked" (attempt {retries + 1}/{max_retries})'
                                )
                            else:
                                logger.error(f'Error processing {path}: {e}')
                                retry_counts.pop(path, None)
                                error_count += 1

                        self._update_done(processed_count + error_count)

                # Exit when all work is done
                if paths_exhausted and not pending_futures:
                    break

                # Brief sleep to avoid busy-waiting
                if not pending_futures:
                    time.sleep(0.05)
                else:
                    time.sleep(0.01)

        # Close worker connections after ingestion
        self._close_worker_conns()

        # Mark missing files as deleted
        self._mark_deleted_files(folder_paths, found_paths)

        if changed_count > 0 or error_count > 0:
            logger.info(
                f'Stage 1 complete: {processed_count} checked, {changed_count} new/changed, {error_count} errors'
            )
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

        Runs in a worker thread with its own DB connection. Handles
        new files, changed files, and unchanged files (backfill checks).

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
            self._process_new_file(
                conn,
                path,
                current_size,
                current_mtime,
                extract_image_metadata,
                create_image,
                canonicalise_path,
            )
            return True

    def _process_existing_file(
        self,
        conn: sqlite3.Connection,
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

        # Backfill mtime if missing
        if existing_mtime is None and existing['size'] == current_size:
            conn.execute('UPDATE images SET mtime = ? WHERE id = ?', (current_mtime, existing['id']))
            conn.commit()
            existing_mtime = current_mtime

        if existing['size'] == current_size and existing_mtime == current_mtime:
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
            return False

        # File changed (size or mtime differ)
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
        conn: sqlite3.Connection,
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
        now_ts = datetime.now().isoformat()
        conn.execute(
            """UPDATE images SET size = ?, width = ?, height = ?, duration = ?,
               mtime = ?, checksum = ?, embedding = NULL,
               aesthetic_nima = NULL, aesthetic_laion = NULL, updated_at = ?
               WHERE id = ?""",
            (current_size, vmeta.width, vmeta.height, vmeta.duration, current_mtime, checksum, now_ts, existing['id']),
        )
        # Delete old scenes and faces
        conn.execute('DELETE FROM scenes WHERE image_id = ?', (existing['id'],))
        conn.execute('DELETE FROM faces WHERE image_id = ?', (existing['id'],))
        conn.commit()

        if checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[existing['id']] = checksum

    def _reingest_changed_image(
        self,
        conn: sqlite3.Connection,
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

        # Clear embedding and scores so Stages 3-5 re-process
        conn.execute(
            'UPDATE images SET embedding = NULL, aesthetic_nima = NULL, aesthetic_laion = NULL WHERE id = ?',
            (existing['id'],),
        )
        # Delete faces so Stage 5 re-detects
        conn.execute('DELETE FROM faces WHERE image_id = ?', (existing['id'],))
        conn.commit()

        if metadata.checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[existing['id']] = metadata.checksum

    def _process_new_file(
        self,
        conn: sqlite3.Connection,
        path: Path,
        current_size: int,
        current_mtime: float,
        extract_image_metadata,
        create_image,
        canonicalise_path,
    ) -> None:
        """Ingest a completely new file (image or video)."""
        ext = path.suffix.lower()
        is_video = ext in self._db.config.video_extensions

        if is_video:
            self._ingest_new_video(conn, path, current_size, current_mtime, create_image, canonicalise_path)
        else:
            self._ingest_new_image(
                conn, path, current_size, current_mtime, extract_image_metadata, create_image, canonicalise_path
            )

    def _ingest_new_video(
        self,
        conn: sqlite3.Connection,
        path: Path,
        current_size: int,
        current_mtime: float,
        create_image,
        canonicalise_path,
    ) -> None:
        """Ingest a new video file."""
        if not is_video_supported():
            logger.warning(f'Skipping video (PyAV not installed): {path}')
            return

        vmeta = get_video_metadata(path)
        if vmeta is None:
            logger.warning(f'Failed to extract video metadata: {path}')
            return

        image_id = str(uuid.uuid4())
        checksum = self._compute_checksum(path)

        ts, ts_conf = derive_timestamp_with_confidence(
            path,
            exif_data=None,
            filename_date_overrides=self._db.config.filename_date_overrides,
            date_order=self._db.config.date_order,
        )

        path_str_canon = str(canonicalise_path(path))
        with self._db._import_names_lock:
            import_name = self._db._import_names.pop(path_str_canon, None)

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
        )

        if checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[image_id] = checksum

        logger.debug(f'Ingested new video: {path}')

    def _ingest_new_image(
        self,
        conn: sqlite3.Connection,
        path: Path,
        current_size: int,
        current_mtime: float,
        extract_image_metadata,
        create_image,
        canonicalise_path,
    ) -> None:
        """Ingest a new image file."""
        metadata = extract_image_metadata(
            path,
            self._db.config.max_image_dimension,
            self._db.config.filename_date_overrides,
            self._db.config.date_order,
        )
        if metadata is None:
            logger.warning(f'Failed to extract metadata for new image: {path}')
            return

        image_id = str(uuid.uuid4())

        path_str_canon = str(canonicalise_path(path))
        with self._db._import_names_lock:
            import_name = self._db._import_names.pop(path_str_canon, None)

        create_image(
            conn,
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

        if metadata.checksum:
            with self._db._checksum_cache_lock:
                self._db._checksum_cache[image_id] = metadata.checksum

        logger.debug(f'Ingested new image: {path}')

    def _mark_deleted_files(self, folder_paths: list[str], found_paths: set[str]) -> None:
        """Mark files in DB but not on disk as deleted."""
        from imagedb import get_all_images

        all_images = get_all_images(self._db.conn, include_deleted=False)
        known_paths = {img['path'] for img in all_images}

        # Only consider paths under registered folders
        registered_prefixes = [fp.rstrip('/\\') + '/' for fp in folder_paths]
        known_under_folders = {p for p in known_paths if any(p.startswith(prefix) for prefix in registered_prefixes)}

        missing_paths = known_under_folders - found_paths
        if missing_paths:
            logger.info(f'Marking {len(missing_paths)} missing images as deleted')
            now = datetime.now().isoformat()
            with self._db._db_lock:
                for path in missing_paths:
                    self._db.conn.execute(
                        'UPDATE images SET deleted = 1, updated_at = ? WHERE path = ? AND deleted = 0',
                        (now, path),
                    )
                self._db.conn.commit()

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
        """Generate missing 200px and 400px thumbnails for images.

        Returns:
            Number of thumbnails generated.
        """
        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT id, path, checksum FROM images
                WHERE deleted = 0 AND checksum IS NOT NULL AND media_type = 'image'
            """)
            rows = cursor.fetchall()

        # Filter to those actually missing thumbnails on disk
        need_thumbnails: list[tuple[str, str]] = []  # (path, checksum)
        for row in rows:
            checksum = row['checksum']
            for size in (200, 400):
                cache_path = get_thumbnail_cache_path(checksum, size, thumbnail_dir=self._db.thumbnail_dir)
                if not cache_path.exists():
                    need_thumbnails.append((row['path'], checksum))
                    break

        if not need_thumbnails:
            return 0

        self._set_stage('thumbnails', len(need_thumbnails), 0)
        logger.info(f'Stage 2a: Generating thumbnails for {len(need_thumbnails)} images...')

        count = 0
        num_threads = max(1, min(8, self._db.config.indexing_threads))

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = {}
            for path_str, checksum in need_thumbnails:
                if self._stopped():
                    break
                future = executor.submit(self._gen_thumb, Path(path_str), checksum)
                futures[future] = path_str

            for future in futures:
                if self._stopped():
                    break
                try:
                    if future.result():
                        count += 1
                except Exception as e:
                    logger.warning(f'Thumbnail generation failed: {e}')
                self._update_done(count)

        if count > 0:
            logger.info(f'Stage 2a complete: generated thumbnails for {count} images')
        return count

    def _gen_thumb(self, source_path: Path, checksum: str) -> bool:
        """Generate 200px and 400px thumbnails for a single image."""
        # Skip videos (poster thumbnails handled separately)
        if source_path.suffix.lower() in self._db.config.video_extensions:
            return True

        success = True
        for size in (200, 400):
            cache_path = get_thumbnail_cache_path(checksum, size, thumbnail_dir=self._db.thumbnail_dir)
            if cache_path.exists():
                continue
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
        from video import (
            detect_scenes,
            extract_scene_keyframes,
            generate_scene_thumbnails,
        )

        if not is_video_supported():
            return 0

        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT i.id, i.path, i.duration, i.checksum
                FROM images i
                WHERE i.deleted = 0
                  AND i.media_type = 'video'
                  AND NOT EXISTS (SELECT 1 FROM scenes s WHERE s.image_id = i.id)
            """)
            rows = cursor.fetchall()

        if not rows:
            return 0

        logger.info(f'Stage 2b: Processing scenes for {len(rows)} videos...')
        count = 0

        for row in rows:
            if self._stopped():
                break

            image_id = row['id']
            path = Path(row['path'])
            duration = row['duration'] or 0.0
            checksum = row['checksum']

            if not path.exists():
                continue

            basename = path.name
            self._current_video = {'basename': basename, 'step': 'scene_detection', 'step_index': 1, 'total_steps': 3}

            # Generate poster-frame thumbnail if missing
            if checksum:
                poster_offset = min(1.0, duration / 2) if duration > 0 else 0
                for size_px in (200, 400):
                    thumb_path = get_thumbnail_cache_path(checksum, size_px, thumbnail_dir=self._db.thumbnail_dir)
                    if not thumb_path.exists():
                        extract_keyframe_thumbnail(path, thumb_path, size_px, time_offset=poster_offset)

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

            with self._db._db_lock:
                self._db.conn.executemany(
                    """INSERT OR REPLACE INTO scenes
                        (id, image_id, scene_index, start_time, end_time,
                         keyframe_time, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    insert_params,
                )
                self._db.conn.commit()

            # Extract keyframes
            self._current_video = {'basename': basename, 'step': 'keyframes', 'step_index': 2, 'total_steps': 3}
            keyframes = extract_scene_keyframes(path, scenes)

            # Generate scene thumbnails
            self._current_video = {'basename': basename, 'step': 'thumbnails', 'step_index': 3, 'total_steps': 3}
            thumbnail_dir = self._db.thumbnail_dir
            for scene_idx, midpoint, _pil in keyframes:
                if self._stopped():
                    break
                if scene_idx < len(scene_ids):
                    generate_scene_thumbnails(
                        path,
                        scene_ids[scene_idx],
                        midpoint,
                        thumbnail_dir,
                        quality=self._db.config.thumbnail_quality,
                    )

            count += 1
            logger.debug(f'Processed scenes for video: {basename} ({len(scenes)} scenes)')

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

        return did_work

    def _embed_images(self) -> int:
        """Compute OpenCLIP embeddings for images missing them.

        Also computes LAION aesthetic scores (dot product with stored embedding).

        Returns:
            Number of images embedded.
        """
        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT id, path FROM images
                WHERE embedding IS NULL AND deleted = 0 AND media_type = 'image'
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
        for batch_start in range(0, len(rows), batch_size):
            if self._stopped():
                break

            batch = rows[batch_start : batch_start + batch_size]
            batch_ids = [r['id'] for r in batch]
            batch_paths = [Path(r['path']) for r in batch]

            results = clip.encode_images_batch(batch_paths)

            updates = []
            for (_idx, embedding), image_id in zip(results, batch_ids, strict=True):
                if embedding is not None:
                    embedding_bytes = embedding.astype(np.float32).tobytes()
                    aesthetic = None
                    if laion_weight is not None:
                        aesthetic = float(embedding @ laion_weight + laion_bias)
                    updates.append((embedding_bytes, aesthetic, datetime.now().isoformat(), image_id))
                    count += 1

            if updates:
                with self._db._db_lock:
                    self._db.conn.executemany(
                        'UPDATE images SET embedding = ?, aesthetic_laion = ?, updated_at = ? WHERE id = ?',
                        updates,
                    )
                    self._db.conn.commit()

            self._update_done(batch_start + len(batch))
            time.sleep(0.01)  # Yield GIL

        if count > 0:
            logger.info(f'Stage 3a complete: embedded {count} images')
        return count

    def _embed_video_scenes(self) -> int:
        """Compute embeddings for video scenes and set preferred scene.

        Returns:
            Number of videos with scenes embedded.
        """
        with self._db._db_lock:
            # Find videos with scenes missing embeddings, OR videos whose
            # scenes are all embedded but the image-level embedding is still
            # NULL (self-healing for interrupted processing).
            cursor = self._db.conn.execute("""
                SELECT DISTINCT i.id, i.path
                FROM images i
                JOIN scenes s ON s.image_id = i.id
                WHERE i.deleted = 0
                  AND i.media_type = 'video'
                  AND (
                      s.embedding IS NULL
                      OR (i.embedding IS NULL AND i.preferred_scene_id IS NULL)
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

            with self._db._db_lock:
                scene_rows = self._db.conn.execute(
                    'SELECT id, scene_index FROM scenes WHERE image_id = ? AND embedding IS NULL ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

            if not scene_rows:
                continue

            embedding_updates: list[tuple] = []
            scene_embeddings: dict[int, np.ndarray] = {}

            for scene_row in scene_rows:
                if self._stopped():
                    break

                scene_id = scene_row['id']
                scene_idx = scene_row['scene_index']

                # Load 400px scene thumbnail
                prefix = scene_id[:2]
                thumb_path = self._db.thumbnail_dir / 'scenes' / '400' / prefix / f'{scene_id}.jpg'
                if not thumb_path.exists():
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
                    if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
                        logger.warning(f'OOM embedding video scene: {e}')
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    else:
                        raise
                except Exception as e:
                    logger.error(f'Error embedding scene {scene_id}: {e}')

            # Write scene embeddings + image embedding in one transaction.
            # This prevents a kill between the two commits from leaving a
            # video with embedded scenes but no image-level embedding
            # (which would be invisible to the re-query on restart).
            with self._db._db_lock:
                if embedding_updates:
                    self._db.conn.executemany(
                        'UPDATE scenes SET embedding = ?, updated_at = ? WHERE id = ?',
                        embedding_updates,
                    )

                # Set preferred scene and image embedding
                all_scenes = self._db.conn.execute(
                    'SELECT id, scene_index FROM scenes WHERE image_id = ? ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

                if all_scenes:
                    preferred_scene_id = all_scenes[0]['id']
                    # Get embedding for scene 0 (either just computed or already in DB)
                    preferred_emb = scene_embeddings.get(0)
                    if preferred_emb is None:
                        emb_row = self._db.conn.execute(
                            'SELECT embedding FROM scenes WHERE id = ?', (preferred_scene_id,)
                        ).fetchone()
                        if emb_row and emb_row['embedding']:
                            preferred_emb = np.frombuffer(emb_row['embedding'], dtype=np.float32)

                    if preferred_emb is not None:
                        rep_blob = preferred_emb.astype(np.float32).tobytes()
                        now_ts = datetime.now().isoformat()
                        self._db.conn.execute(
                            'UPDATE images SET embedding = ?, preferred_scene_id = ?, updated_at = ? WHERE id = ?',
                            (rep_blob, preferred_scene_id, now_ts, image_id),
                        )

                self._db.conn.commit()

            count += 1

        if count > 0:
            logger.info(f'Stage 3b complete: embedded scenes for {count} videos')
        return count

    def _load_laion_head(self, clip: OpenCLIPModel) -> tuple[np.ndarray | None, float | None]:
        """Load the LAION aesthetic predictor head weights.

        Returns:
            Tuple of (weight, bias) or (None, None) if unavailable.
        """
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

        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT id, checksum FROM images
                WHERE aesthetic_nima IS NULL AND deleted = 0 AND checksum IS NOT NULL
            """)
            rows = cursor.fetchall()

        if not rows:
            return 0

        self._set_stage('scoring', len(rows), 0)
        logger.info(f'Stage 4a: NIMA scoring {len(rows)} images...')

        try:
            from nima import load_nima_model, score_images_batch

            if torch.cuda.is_available():
                device = 'cuda'
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                device = 'mps'
            else:
                device = 'cpu'

            model = load_nima_model(str(checkpoint_path), device=device)
        except (MemoryError, RuntimeError) as e:
            if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
                logger.error(f'OOM loading NIMA model: {e}')
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                return 0
            raise
        except Exception as e:
            logger.warning(f'Failed to load NIMA model: {e}')
            return 0

        batch_size = self._db.config.nima_batch_size
        count = 0

        for batch_start in range(0, len(rows), batch_size):
            if self._stopped():
                break

            batch = rows[batch_start : batch_start + batch_size]

            # Load 400px thumbnails
            valid_ids = []
            pil_images = []
            for row in batch:
                checksum = row['checksum']
                thumb_path = get_thumbnail_cache_path(checksum, 400, thumbnail_dir=self._db.thumbnail_dir)
                if not Path(thumb_path).exists():
                    continue
                try:
                    img = Image.open(thumb_path).convert('RGB')
                    valid_ids.append(row['id'])
                    pil_images.append(img)
                except Exception as e:
                    logger.warning(f'Failed to load thumbnail for NIMA: {row["id"]}: {e}')

            if not pil_images:
                self._update_done(batch_start + len(batch))
                continue

            # Score batch — with OOM fallback
            try:
                scores = score_images_batch(model, pil_images, device=device)
            except (MemoryError, RuntimeError) as e:
                if not isinstance(e, MemoryError) and 'out of memory' not in str(e).lower():
                    raise
                logger.warning(f'OOM scoring NIMA batch of {len(pil_images)}, falling back to single')
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

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

            # Batch commit
            now = datetime.now().isoformat()
            updates = [(score, now, vid) for score, vid in zip(scores, valid_ids, strict=True)]

            with self._db._db_lock:
                self._db.conn.executemany(
                    'UPDATE images SET aesthetic_nima = ?, updated_at = ? WHERE id = ?',
                    updates,
                )
                self._db.conn.commit()

            count += len(updates)
            self._update_done(batch_start + len(batch))
            time.sleep(0.01)

        # Unload NIMA model to free GPU memory for subsequent stages
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        if count > 0:
            logger.info(f'Stage 4a complete: scored {count} images')
            self._db.event_queue.emit('nima_complete', {'scored_count': count})
        return count

    def _backfill_laion(self) -> int:
        """Compute LAION aesthetic scores for images with embeddings but no score.

        This is a cheap CPU operation — dot products on existing embedding blobs.

        Returns:
            Number of images scored.
        """
        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT id, embedding FROM images
                WHERE aesthetic_laion IS NULL AND embedding IS NOT NULL AND deleted = 0
            """)
            rows = cursor.fetchall()

        if not rows:
            return 0

        clip = self._get_clip_model()
        laion_weight, laion_bias = self._load_laion_head(clip)
        if laion_weight is None:
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
                with self._db._db_lock:
                    self._db.conn.executemany(
                        'UPDATE images SET aesthetic_laion = ?, updated_at = ? WHERE id = ?',
                        chunk,
                    )
                    self._db.conn.commit()

        count = len(updates)
        if count > 0:
            logger.info(f'Stage 4b complete: scored {count} images')
        return count

    # =================================================================
    # Stage 5: Face Detection
    # =================================================================

    def _stage_faces(self) -> bool:
        """Detect faces in images using 400px thumbnails.

        Returns:
            True if any faces were detected.
        """
        if not self._run_face_detection or not self._db.config.face_detection_enabled:
            return False

        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT i.id, i.checksum, i.width, i.height
                FROM images i
                WHERE i.deleted = 0
                  AND i.media_type = 'image'
                  AND i.checksum IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.image_id = i.id)
            """)
            rows = cursor.fetchall()

        if not rows:
            return False

        self._set_stage('faces', len(rows), 0)
        logger.info(f'Stage 5: Detecting faces in {len(rows)} images...')

        face_detector = FaceDetector(
            min_confidence=self._db.config.face_detection_min_confidence,
            min_face_size=self._db.config.face_detection_min_size,
        )

        clip = self._get_clip_model()
        batch_size = self._db.config.face_detection_batch_size
        processed_count = 0
        faces_detected_count = 0

        for batch_start in range(0, len(rows), batch_size):
            if self._stopped():
                break

            batch = rows[batch_start : batch_start + batch_size]

            # Build path mappings — use 400px thumbnails
            id_to_path: dict[str, Path] = {}
            path_to_id: dict[Path, str] = {}
            id_to_orig_path: dict[str, Path] = {}  # For face thumbnail generation

            for row in batch:
                image_id = row['id']
                checksum = row['checksum']

                thumb_path = get_thumbnail_cache_path(checksum, 400, thumbnail_dir=self._db.thumbnail_dir)
                if not Path(thumb_path).exists():
                    # Try to use original path as fallback
                    with self._db._db_lock:
                        img_row = self._db.conn.execute('SELECT path FROM images WHERE id = ?', (image_id,)).fetchone()
                    if img_row:
                        orig_path = Path(img_row['path'])
                        if orig_path.exists():
                            id_to_path[image_id] = orig_path
                            path_to_id[orig_path] = image_id
                            id_to_orig_path[image_id] = orig_path
                    continue

                thumb_p = Path(thumb_path)
                id_to_path[image_id] = thumb_p
                path_to_id[thumb_p] = image_id
                # Store original path for face thumbnail generation
                with self._db._db_lock:
                    img_row = self._db.conn.execute('SELECT path FROM images WHERE id = ?', (image_id,)).fetchone()
                if img_row:
                    id_to_orig_path[image_id] = Path(img_row['path'])

            if not id_to_path:
                processed_count += len(batch)
                self._update_done(processed_count)
                continue

            # Preload and detect faces
            paths = list(id_to_path.values())
            loaded_images = face_detector.preload_images_batch(paths, num_workers=4)

            if not loaded_images:
                processed_count += len(batch)
                self._update_done(processed_count)
                continue

            results = face_detector.detect_faces_from_preloaded(loaded_images, stop_event=self._stop_event)

            # Get known face embeddings for auto-recognition
            with self._db._db_lock:
                known_embeddings = get_all_known_face_embeddings(self._db.conn)
                cursor2 = self._db.conn.execute('SELECT id, name, recognition_threshold FROM people')
                per_person_thresholds: dict[str, float | None] = {}
                ignored_person_ids: set[str] = set()
                for prow in cursor2.fetchall():
                    per_person_thresholds[prow['id']] = prow['recognition_threshold']
                    if prow['name'] == '-':
                        ignored_person_ids.add(prow['id'])

            # Process detection results — commit all faces for one image
            # atomically so partial face sets can't occur on crash.
            batch_faces_total = 0
            batch_matched = 0

            for path, detected_faces in results.items():
                if path not in path_to_id:
                    continue
                image_id = path_to_id[path]
                orig_path = id_to_orig_path.get(image_id, path)

                if not detected_faces:
                    # Sentinel record — single insert, atomic by itself
                    dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
                    sentinel_id = str(uuid.uuid4())
                    with self._db._db_lock:
                        self._db.conn.execute(
                            """INSERT INTO faces
                               (id, image_id, box_x, box_y, box_w, box_h,
                                confidence, embedding, person_id, suppressed,
                                created_at, updated_at)
                               VALUES (?, ?, 0, 0, 0, 0, 0, ?, NULL, 1,
                                       datetime('now'), datetime('now'))""",
                            (sentinel_id, image_id, dummy_embedding),
                        )
                        self._db.conn.commit()
                    processed_count += 1
                    continue

                # Prepare all face rows for this image before committing
                face_rows: list[tuple] = []
                for face in detected_faces:
                    batch_faces_total += 1

                    # Auto-match
                    person_id = None
                    match = find_best_match(
                        face.embedding,
                        known_embeddings,
                        threshold=self._db.config.face_recognition_threshold,
                        person_thresholds=per_person_thresholds,
                        ignored_person_ids=ignored_person_ids,
                    )
                    if match:
                        _, person_id, _similarity = match
                        batch_matched += 1

                    # Generate face thumbnail from original image
                    face_id = str(uuid.uuid4())
                    thumb_path = get_face_thumbnail_path(face_id, self._db.thumbnail_dir)
                    generate_face_thumbnail(
                        orig_path,
                        thumb_path,
                        box_x=face.box_x,
                        box_y=face.box_y,
                        box_w=face.box_w,
                        box_h=face.box_h,
                        size=200,
                        quality=self._db.config.thumbnail_quality,
                    )

                    # Generate semantic embedding from face thumbnail
                    semantic_embedding = None
                    if thumb_path.exists():
                        semantic_embedding = clip.encode_image(thumb_path)

                    embedding_bytes = face.embedding.astype(np.float32).tobytes()
                    semantic_bytes = None
                    if semantic_embedding is not None:
                        semantic_bytes = semantic_embedding.astype(np.float32).tobytes()

                    face_rows.append(
                        (
                            face_id,
                            image_id,
                            face.box_x,
                            face.box_y,
                            face.box_w,
                            face.box_h,
                            face.confidence,
                            embedding_bytes,
                            person_id,
                            semantic_bytes,
                        )
                    )

                # Atomically insert all faces for this image in one transaction
                with self._db._db_lock:
                    self._db.conn.executemany(
                        """INSERT INTO faces
                           (id, image_id, box_x, box_y, box_w, box_h,
                            confidence, embedding, person_id, semantic_embedding,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   datetime('now'), datetime('now'))""",
                        face_rows,
                    )
                    self._db.conn.commit()

                faces_detected_count += len(face_rows)
                processed_count += 1

            self._update_done(processed_count)

            # Log batch summary
            if batch_faces_total > 0:
                logger.info(
                    f'Face auto-match: {batch_faces_total} faces detected, '
                    f'{len(known_embeddings)} known references, {batch_matched} auto-matched'
                )

            time.sleep(0.01)

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

        # 6a: Sync directory groups (always)
        try:
            self._db._duplicate_manager.sync_directory_groups(self._db.conn, self._db._db_lock)
        except Exception as e:
            logger.error(f'Failed to sync directory groups: {e}')

        if not self._run_face_grouping:
            logger.info('Skipping grouping phase (use --group-faces or GUI Rescan)')
            emit_processing_complete(self._db.event_queue)
            return False

        # 6b: Face reassessment
        if self._db.config.face_detection_enabled and not self._stopped():
            try:
                with self._db._db_lock:
                    delete_people_without_faces(self._db.conn)
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
                with self._db._db_lock:
                    compute_unknown_face_groups(
                        self._db.conn,
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
        if not self._db.config.stt_enabled:
            return False

        with self._db._db_lock:
            cursor = self._db.conn.execute("""
                SELECT DISTINCT i.id, i.path, i.basename
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
                'basename': basename,
                'step': 'transcribing',
                'step_index': 1,
                'total_steps': 1,
            }

            # Load scene boundaries
            with self._db._db_lock:
                scene_rows = self._db.conn.execute(
                    'SELECT id, start_time, end_time FROM scenes WHERE image_id = ? ORDER BY scene_index',
                    (image_id,),
                ).fetchall()

            if not scene_rows:
                continue

            scenes = [(r['start_time'], r['end_time']) for r in scene_rows]
            scene_ids = [r['id'] for r in scene_rows]

            logger.info(f'Transcribing {basename} ({len(scenes)} scenes)')
            clip = self._get_clip_model()
            self._transcribe_scenes(image_id, video_path, scenes, scene_ids, clip)

            count += 1
            self._update_done(count)

        self._current_video = None
        if count > 0:
            logger.info(f'Stage 7 complete: transcribed {count} videos')
        return count > 0

    def _transcribe_scenes(
        self,
        image_id: str,
        video_path: Path,
        scenes: list[tuple[float, float]],
        scene_ids: list[str],
        clip: OpenCLIPModel,
    ) -> None:
        """Transcribe the entire video and assign text to scenes by overlap.

        Args:
            image_id: The video's image ID.
            video_path: Path to the video file.
            scenes: List of (start, end) scene boundaries.
            scene_ids: Corresponding scene UUIDs.
            clip: OpenCLIP model for computing text embeddings.
        """
        import tempfile

        from video import extract_audio_segment

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
                return

            # Transcribe whole clip
            stt_segments = stt.transcribe(tmp_path, language=self._db.config.stt_language)

            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

            if not stt_segments:
                return

            # Assign STT segments to scenes by temporal overlap
            transcription_updates: list[tuple] = []
            for (scene_start, scene_end), scene_id in zip(scenes, scene_ids, strict=False):
                if self._stopped():
                    return

                scene_texts: list[str] = []
                for seg in stt_segments:
                    seg_start = getattr(seg, 'start', 0.0)
                    seg_end = getattr(seg, 'end', 0.0)
                    if seg_start < scene_end and seg_end > scene_start:
                        text = getattr(seg, 'text', '').strip()
                        if text:
                            scene_texts.append(text)

                if not scene_texts:
                    continue

                full_text = ' '.join(scene_texts)
                text_emb = clip.encode_text(full_text) if clip else None
                text_emb_blob = text_emb.astype(np.float32).tobytes() if text_emb is not None else None

                now_ts = datetime.now().isoformat()
                transcription_updates.append((full_text, text_emb_blob, now_ts, scene_id))

            if transcription_updates:
                with self._db._db_lock:
                    self._db.conn.executemany(
                        """UPDATE scenes SET transcription = ?, transcription_embedding = ?,
                               updated_at = ?
                        WHERE id = ?""",
                        transcription_updates,
                    )
                    self._db.conn.commit()

        except Exception as e:
            logger.error(f'STT failed for video {video_path}: {e}')
