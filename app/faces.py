"""
Face detection and recognition for the Photonarium image database.

This module provides face detection using MTCNN and face embeddings using
InceptionResnetV1 from the facenet-pytorch library. It handles:

1. Face detection in images with bounding boxes and confidence scores
2. 512D face embedding generation for recognition
3. Database schema and CRUD operations for people and faces
4. Auto-recognition by matching new faces against known people
5. Face thumbnail generation (200x200 crops from full images)
6. Background reassessment of unknown faces against known people
   (vectorised with numpy for GIL-friendly bulk matching)
7. Unknown face grouping by embedding similarity (union-find clustering)
8. Person face revalidation (ejecting faces that fall below threshold)

The face detection pipeline integrates with the existing image indexing
process and runs as an optional phase after OpenCLIP embedding generation.
Background reassessment and grouping run asynchronously and use optimistic
locking (updated_at) to avoid overwriting concurrent user edits.

Usage:
    from faces import FaceDetector, init_face_tables

    # Initialise database tables
    init_face_tables(conn)

    # Create detector (lazy loads models on first use)
    detector = FaceDetector(config)

    # Detect faces in an image
    faces = detector.detect_faces(image_path)

    # Generate embedding for a detected face
    embedding = detector.get_face_embedding(image_path, face_box)
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import torch
from facenet_pytorch import MTCNN, InceptionResnetV1
from PIL import Image, ImageFilter

from dbutil import sql_placeholders
from duplicates import UnionFind
from rawimage import open_image as raw_open_image
from safeconn import SafeConnection

if TYPE_CHECKING:
    from imagedb import ImageDatabase

# Configure module logger
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detection / recognition constants
# ---------------------------------------------------------------------------

# Minimum MTCNN confidence score to accept a detected face
FACE_DETECTION_MIN_CONFIDENCE = 0.95
# Minimum face dimension in pixels (MTCNN min_face_size)
FACE_DETECTION_MIN_SIZE_PX = 40
# Images larger than this are downscaled before detection
FACE_DETECTION_MAX_DIM = 4096
# MTCNN cascade thresholds (P-Net, R-Net, O-Net)
FACE_MTCNN_THRESHOLDS = [0.6, 0.7, 0.7]
# Extra padding around face crop as a fraction of the larger dimension
FACE_CROP_PADDING_RATIO = 0.1
# Aspect ratio tolerance: faces within ±this of 1.0 are treated as square
FACE_THUMB_SQUARE_TOLERANCE = 0.05
# Gaussian blur radius for the letterbox background of non-square face thumbs
FACE_THUMB_BG_BLUR_RADIUS = 8
# Blend alpha toward black for the letterbox background darkening
FACE_THUMB_BG_DARKEN_ALPHA = 0.4
# Default cosine-similarity threshold for face recognition
FACE_RECOGNITION_DEFAULT_THRESHOLD = 0.65


# =============================================================================
# DATABASE SCHEMA
# =============================================================================

# SQL schema for the people table (known identities)
_SQL_CREATE_PEOPLE = """
CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    preferred_face_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# SQL schema for the faces table (detected faces in images)
_SQL_CREATE_FACES = """
CREATE TABLE IF NOT EXISTS faces (
    id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    box_x REAL NOT NULL,
    box_y REAL NOT NULL,
    box_w REAL NOT NULL,
    box_h REAL NOT NULL,
    confidence REAL,
    embedding BLOB NOT NULL,
    person_id TEXT REFERENCES people(id) ON DELETE SET NULL,
    suppressed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

# Index definitions for face tables
_SQL_CREATE_FACE_INDEXES = [
    'CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id)',
    'CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id)',
    'CREATE INDEX IF NOT EXISTS idx_faces_suppressed ON faces(suppressed)',
    'CREATE INDEX IF NOT EXISTS idx_people_name ON people(name COLLATE NOCASE)',
    # Composite index for efficient face count queries (used by get_all_people)
    # Without this, SQLite uses idx_faces_suppressed which causes full scans
    'CREATE INDEX IF NOT EXISTS idx_faces_person_suppressed ON faces(person_id, suppressed)',
    # Index for unknown_group_id to speed up window functions in get_all_faces
    'CREATE INDEX IF NOT EXISTS idx_faces_unknown_group ON faces(unknown_group_id)',
]

# Migrations for schema updates
_MIGRATIONS = [
    # Add unknown_group_id column for grouping similar unknown faces
    ('faces', 'unknown_group_id', 'ALTER TABLE faces ADD COLUMN unknown_group_id TEXT'),
    # Add per-person recognition threshold (NULL = use global default)
    ('people', 'recognition_threshold', 'ALTER TABLE people ADD COLUMN recognition_threshold REAL'),
    # Add semantic embedding for text-based face search (OpenCLIP, distinct from face recognition embedding)
    ('faces', 'semantic_embedding', 'ALTER TABLE faces ADD COLUMN semantic_embedding BLOB'),
    # Add manually_tagged flag to track user-tagged vs auto-matched faces
    # 0 = auto-tagged (or not yet tagged), 1 = manually tagged by user
    ('faces', 'manually_tagged', 'ALTER TABLE faces ADD COLUMN manually_tagged INTEGER DEFAULT 0'),
    # Add updated_at for optimistic concurrency control in background processes
    # Allows background tasks to skip faces that were modified since they started
    # Note: SQLite ALTER TABLE doesn't allow function defaults, so we add with NULL default
    # and backfill in _run_migrations()
    ('faces', 'updated_at', 'ALTER TABLE faces ADD COLUMN updated_at TEXT'),
]


def init_face_tables(conn: SafeConnection) -> None:
    """Initialise the face recognition database tables.

    Creates the people and faces tables if they don't exist, along with
    necessary indexes. Also runs any pending migrations.

    Args:
        conn: Database connection.
    """
    conn.execute(_SQL_CREATE_PEOPLE)
    conn.execute(_SQL_CREATE_FACES)

    for index_sql in _SQL_CREATE_FACE_INDEXES:
        try:
            conn.execute(index_sql)
        except sqlite3.OperationalError as e:
            # Index already exists
            logger.debug(f'Face index creation skipped (already exists): {e}')

    # Run migrations for schema updates
    _run_migrations(conn)

    conn.commit()
    logger.info('Face recognition tables initialised')


def _run_migrations(conn: SafeConnection) -> None:
    """Run pending schema migrations.

    Args:
        conn: Database connection.
    """
    newly_added_columns = set()

    for table, column, sql in _MIGRATIONS:
        # Check if column exists
        cursor = conn.execute(f'PRAGMA table_info({table})')
        columns = [row[1] for row in cursor.fetchall()]
        if column not in columns:
            try:
                conn.execute(sql)
                newly_added_columns.add((table, column))
                logger.info(f'Migration: added {table}.{column}')
            except sqlite3.OperationalError as e:
                logger.warning(f'Migration failed for {table}.{column}: {e}')

    # Backfill updated_at for existing faces (set to created_at if NULL)
    # Always run this, not just when column is newly added, in case previous backfill failed
    cursor = conn.execute('UPDATE faces SET updated_at = created_at WHERE updated_at IS NULL')
    if cursor.rowcount > 0:
        logger.info(f'Migration: backfilled updated_at for {cursor.rowcount} existing faces')


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class DetectedFace:
    """A face detected in an image.

    Attributes:
        box_x: Normalised x coordinate of bounding box (0-1).
        box_y: Normalised y coordinate of bounding box (0-1).
        box_w: Normalised width of bounding box (0-1).
        box_h: Normalised height of bounding box (0-1).
        confidence: Detection confidence score (0-1).
        embedding: 512D face embedding as numpy array.
    """

    box_x: float
    box_y: float
    box_w: float
    box_h: float
    confidence: float
    embedding: np.ndarray


# =============================================================================
# FACE DETECTOR CLASS
# =============================================================================


class FaceDetector:
    """Face detection and embedding using facenet-pytorch.

    Uses MTCNN for face detection and InceptionResnetV1 for face embeddings.
    Models are loaded lazily on first use and cached for subsequent calls.

    Attributes:
        min_confidence: Minimum detection confidence threshold.
        min_face_size: Minimum face size in pixels.
        device: PyTorch device ('cuda' or 'cpu').
    """

    def __init__(
        self,
        min_confidence: float = FACE_DETECTION_MIN_CONFIDENCE,
        min_face_size: int = FACE_DETECTION_MIN_SIZE_PX,
        device: str | None = None,
    ):
        """Initialise the face detector.

        Args:
            min_confidence: Minimum MTCNN confidence for face detection.
            min_face_size: Minimum face size in pixels.
            device: PyTorch device. If None, auto-selects CUDA if available.
        """
        self.min_confidence = min_confidence
        self.min_face_size = min_face_size

        # Lazy-loaded models
        self._mtcnn = None
        self._resnet = None
        self._mtcnn_failed = False
        self._resnet_failed = False
        self._device = device
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        """Get the PyTorch device, auto-detecting if not set."""
        if self._device is None:
            # Priority: CUDA (NVIDIA GPU) > MPS (Apple Silicon) > CPU
            if torch.cuda.is_available():
                self._device = 'cuda'
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                self._device = 'mps'
            else:
                self._device = 'cpu'
            logger.info(f'Face detector using device: {self._device}')
        return self._device

    @property
    def mtcnn(self):
        """Get the MTCNN face detector, loading if necessary."""
        if self._mtcnn is None and not self._mtcnn_failed:
            with self._lock:
                if self._mtcnn is None and not self._mtcnn_failed:
                    logger.info('Loading MTCNN face detector...')
                    t0 = time.perf_counter()
                    try:
                        self._mtcnn = MTCNN(
                            keep_all=True,
                            device=self.device,
                            min_face_size=self.min_face_size,
                            thresholds=FACE_MTCNN_THRESHOLDS,
                            post_process=True,  # Apply standardization for ResNet input
                        )
                        logger.info('MTCNN loaded (%.1fs)', time.perf_counter() - t0)
                    except (MemoryError, RuntimeError) as e:
                        if not isinstance(e, MemoryError) and 'out of memory' not in str(e).lower():
                            raise
                        self._mtcnn_failed = True
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                        logger.error(f'Out of memory loading MTCNN: {e} — face detection disabled')
        return self._mtcnn

    @property
    def resnet(self):
        """Get the InceptionResnetV1 model, loading if necessary."""
        if self._resnet is None and not self._resnet_failed:
            with self._lock:
                if self._resnet is None and not self._resnet_failed:
                    logger.info('Loading InceptionResnetV1 for face recognition embeddings...')
                    t0 = time.perf_counter()
                    try:
                        self._resnet = InceptionResnetV1(
                            pretrained='vggface2',
                            device=self.device,
                        ).eval()
                        logger.info('InceptionResnetV1 loaded (%.1fs)', time.perf_counter() - t0)
                    except (MemoryError, RuntimeError) as e:
                        if not isinstance(e, MemoryError) and 'out of memory' not in str(e).lower():
                            raise
                        self._resnet_failed = True
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                        logger.error(f'Out of memory loading InceptionResnetV1: {e} — face embeddings disabled')
        return self._resnet

    def detect_faces(
        self,
        image_path: Path | str,
        max_dimension: int = FACE_DETECTION_MAX_DIM,
    ) -> list[DetectedFace]:
        """Detect faces in an image.

        Args:
            image_path: Path to the image file.
            max_dimension: Maximum image dimension for processing.
                Larger images are downscaled to improve performance.

        Returns:
            List of DetectedFace objects with normalised bounding boxes
            and embeddings. Returns empty list if no faces detected or
            on error.
        """
        image_path = Path(image_path)

        try:
            # Load and preprocess image (handles both standard and RAW formats)
            img = raw_open_image(image_path)

            # Convert to RGB
            if img.mode != 'RGB':
                img = img.convert('RGB')

            original_width, original_height = img.size

            # Downscale if needed for performance
            scale = 1.0
            if max(original_width, original_height) > max_dimension:
                scale = max_dimension / max(original_width, original_height)
                new_size = (int(original_width * scale), int(original_height * scale))
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                logger.debug(f'Downscaled image from {original_width}x{original_height} to {new_size[0]}x{new_size[1]}')

            # Detect faces using MTCNN
            # Returns: boxes (N x 4), probs (N,), landmarks (N x 5 x 2)
            boxes, probs = self.mtcnn.detect(img)

            if boxes is None or len(boxes) == 0:
                return []

            # Filter by confidence FIRST, then extract only valid faces
            # This ensures index correspondence between boxes and face tensors
            valid_mask = [prob is not None and prob >= self.min_confidence for prob in probs]
            valid_boxes = boxes[valid_mask]
            valid_probs = probs[valid_mask]

            if len(valid_boxes) == 0:
                return []

            # Extract aligned face crops ONLY for valid boxes
            # Using mtcnn.extract() with specific boxes guarantees correspondence
            faces_tensor = self.mtcnn.extract(img, valid_boxes, save_path=None)

            if faces_tensor is None:
                return []

            # Ensure tensor is in correct format
            if len(faces_tensor.shape) == 3:
                # Single face - add batch dimension
                faces_tensor = faces_tensor.unsqueeze(0)

            # Sanity check: boxes and faces should now match
            if len(valid_boxes) != len(faces_tensor):
                logger.error(
                    f'INDEX MISMATCH after extract! boxes={len(valid_boxes)}, '
                    f'faces_tensor={len(faces_tensor)} for {image_path.name}'
                )

            processed_width, processed_height = img.size

            # Process each valid face - indices now guaranteed to match
            valid_faces = []  # List of (tensor_idx, norm_box, confidence)
            for i in range(len(faces_tensor)):
                box = valid_boxes[i]
                prob = valid_probs[i]

                # Convert box from pixels to normalised coordinates (0-1)
                # MTCNN returns [x1, y1, x2, y2] format
                x1, y1, x2, y2 = box

                # Make box square (use larger dimension)
                box_width = x2 - x1
                box_height = y2 - y1
                box_size = max(box_width, box_height)

                # Centre the square box
                centre_x = (x1 + x2) / 2
                centre_y = (y1 + y2) / 2

                # Calculate square box coordinates
                sq_x1 = centre_x - box_size / 2
                sq_y1 = centre_y - box_size / 2

                # Normalise to 0-1 (relative to processed image size)
                norm_x = sq_x1 / processed_width
                norm_y = sq_y1 / processed_height
                norm_w = box_size / processed_width
                norm_h = box_size / processed_height

                # Clamp to valid range
                norm_x = max(0.0, min(1.0, norm_x))
                norm_y = max(0.0, min(1.0, norm_y))
                norm_w = max(0.0, min(1.0 - norm_x, norm_w))
                norm_h = max(0.0, min(1.0 - norm_y, norm_h))

                # Check minimum face size (in original pixels)
                # Use smaller dimension - both edges must meet minimum
                min_box_dim = min(box_width, box_height)
                face_pixels = min_box_dim / scale if scale != 1.0 else min_box_dim
                if face_pixels < self.min_face_size:
                    logger.debug(f'Skipping small face: {face_pixels:.0f}px')
                    continue

                valid_faces.append(
                    (
                        i,  # tensor index
                        (norm_x, norm_y, norm_w, norm_h),  # normalised box
                        float(prob),  # confidence
                    )
                )

            if not valid_faces:
                return []

            # Batch compute embeddings for all valid faces at once
            tensor_indices = [vf[0] for vf in valid_faces]
            batch_tensor = faces_tensor[tensor_indices].to(self.device)
            # Note: MTCNN with post_process=True already standardizes to [-1, 1] for ResNet

            with torch.no_grad():
                embeddings_batch = self.resnet(batch_tensor)
                embeddings_batch = embeddings_batch.cpu().numpy()

            # Normalise all embeddings (L2 normalisation for cosine similarity)
            norms = np.linalg.norm(embeddings_batch, axis=1, keepdims=True)
            norms[norms == 0] = 1  # Avoid division by zero
            embeddings_batch = embeddings_batch / norms

            # Build detected faces list
            detected_faces = []
            for idx, (_, norm_box, confidence) in enumerate(valid_faces):
                norm_x, norm_y, norm_w, norm_h = norm_box
                detected_faces.append(
                    DetectedFace(
                        box_x=float(norm_x),
                        box_y=float(norm_y),
                        box_w=float(norm_w),
                        box_h=float(norm_h),
                        confidence=confidence,
                        embedding=embeddings_batch[idx],
                    )
                )

            logger.debug(f'Detected {len(detected_faces)} faces in {image_path.name}')
            return detected_faces

        except Exception as e:
            logger.error(f'Face detection failed for {image_path}: {e}')
            return []

    def preload_images_batch(
        self,
        image_paths: list[Path | str],
        max_dimension: int = FACE_DETECTION_MAX_DIM,
        num_workers: int = 4,
    ) -> list[tuple[Path, Image.Image, float]]:
        """Preload and preprocess images in parallel on CPU.

        This is the first phase of face detection, separated out to allow
        prefetching the next batch while the GPU processes the current batch.

        Args:
            image_paths: List of paths to image files.
            max_dimension: Maximum image dimension for processing.
            num_workers: Number of parallel workers for image loading.

        Returns:
            List of (path, PIL.Image, scale) tuples for successfully loaded images.
        """
        if not image_paths:
            return []

        loaded_images = []  # List of (path, img, scale)

        def load_single_image(image_path: Path | str):
            """Load and preprocess image on CPU."""
            image_path = Path(image_path)
            try:
                # raw_open_image handles both standard and RAW formats
                img = raw_open_image(image_path)
                if img.mode != 'RGB':
                    img = img.convert('RGB')

                original_width, original_height = img.size
                scale = 1.0
                if max(original_width, original_height) > max_dimension:
                    scale = max_dimension / max(original_width, original_height)
                    new_size = (int(original_width * scale), int(original_height * scale))
                    img = img.resize(new_size, Image.Resampling.LANCZOS)

                return image_path, img, scale
            except Exception as e:
                logger.error(f'Failed to load image {image_path}: {e}')
                return image_path, None, None

        # Load images in parallel
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(load_single_image, p): p for p in image_paths}
            for future in as_completed(futures):
                try:
                    image_path, img, scale = future.result()
                    if img is not None:
                        loaded_images.append((image_path, img, scale))
                except Exception as e:
                    logger.error(f'Image loading worker failed: {e}')

        return loaded_images

    def detect_faces_from_preloaded(
        self,
        loaded_images: list[tuple[Path, Image.Image, float]],
        stop_event: threading.Event | None = None,
    ) -> dict[Path, list[DetectedFace]]:
        """Detect faces in pre-loaded images using GPU batch processing.

        This is the GPU phase of face detection, processing images that were
        already loaded by preload_images_batch().

        Args:
            loaded_images: List of (path, PIL.Image, scale) tuples from preload_images_batch().
            stop_event: Optional threading.Event to signal early termination.

        Returns:
            Dict mapping each image path to its list of DetectedFace objects.
        """

        def should_stop():
            return stop_event is not None and stop_event.is_set()

        results: dict[Path, list[DetectedFace]] = {}

        # Initialise results with empty lists
        for path, _, _ in loaded_images:
            results[path] = []

        if not loaded_images or should_stop():
            return results

        # Phase 2: Group images by dimension for MTCNN batching
        # MTCNN requires equal-dimension images for batch processing
        dimension_groups = defaultdict(list)
        for image_path, img, scale in loaded_images:
            dim_key = img.size  # (width, height)
            dimension_groups[dim_key].append((image_path, img, scale))

        # Phase 3: Process each dimension group through MTCNN
        all_faces_data = []  # (image_path, tensor, norm_box, confidence)

        for dim_key, group in dimension_groups.items():
            # Check for early termination between groups
            if should_stop():
                logger.debug('Face detection interrupted by stop event')
                break

            images_for_detection = [img for _, img, _ in group]

            try:
                if len(group) == 1:
                    # Single image - no batching needed
                    boxes_batch, probs_batch = self.mtcnn.detect(images_for_detection[0])
                    # Wrap in lists for consistent processing
                    if boxes_batch is not None:
                        boxes_batch = [boxes_batch]
                        probs_batch = [probs_batch]
                else:
                    # Batch detection for multiple same-dimension images
                    boxes_batch, probs_batch = self.mtcnn.detect(images_for_detection)
            except Exception as e:
                logger.error(f'MTCNN detection failed for dimension {dim_key}: {e}')
                continue

            # Process results for this group
            for img_idx, (image_path, img, scale) in enumerate(group):
                boxes = boxes_batch[img_idx] if boxes_batch is not None else None
                probs = probs_batch[img_idx] if probs_batch is not None else None

                if boxes is None or len(boxes) == 0:
                    continue

                # Filter by confidence FIRST
                valid_mask = [prob is not None and prob >= self.min_confidence for prob in probs]
                valid_boxes = boxes[valid_mask]
                valid_probs = probs[valid_mask]

                if len(valid_boxes) == 0:
                    continue

                # Extract aligned faces ONLY for valid boxes - guarantees correspondence
                faces_tensor = self.mtcnn.extract(img, valid_boxes, save_path=None)

                if faces_tensor is None:
                    continue

                # Handle single face case (no batch dimension)
                if len(faces_tensor.shape) == 3:
                    faces_tensor = faces_tensor.unsqueeze(0)

                # Sanity check
                if len(valid_boxes) != len(faces_tensor):
                    logger.error(
                        f'INDEX MISMATCH after extract! boxes={len(valid_boxes)}, '
                        f'faces_tensor={len(faces_tensor)} for {image_path.name}'
                    )

                processed_width, processed_height = img.size

                for i in range(len(faces_tensor)):
                    box = valid_boxes[i]
                    prob = valid_probs[i]

                    x1, y1, x2, y2 = box
                    box_width = x2 - x1
                    box_height = y2 - y1
                    box_size = max(box_width, box_height)

                    centre_x = (x1 + x2) / 2
                    centre_y = (y1 + y2) / 2
                    sq_x1 = centre_x - box_size / 2
                    sq_y1 = centre_y - box_size / 2

                    norm_x = max(0.0, min(1.0, sq_x1 / processed_width))
                    norm_y = max(0.0, min(1.0, sq_y1 / processed_height))
                    norm_w = max(0.0, min(1.0 - norm_x, box_size / processed_width))
                    norm_h = max(0.0, min(1.0 - norm_y, box_size / processed_height))

                    # Check minimum face size (in original pixels)
                    # Use smaller dimension - both edges must meet minimum
                    min_box_dim = min(box_width, box_height)
                    face_pixels = min_box_dim / scale if scale != 1.0 else min_box_dim
                    if face_pixels < self.min_face_size:
                        continue

                    # Clone tensor to CPU immediately to avoid keeping GPU tensor alive
                    all_faces_data.append(
                        (
                            image_path,
                            faces_tensor[i].clone().cpu(),
                            (norm_x, norm_y, norm_w, norm_h),
                            float(prob),
                        )
                    )

                # Release GPU memory for this image's face tensors
                del faces_tensor

            # Release GPU memory between dimension groups
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            # Release PIL images for this group (no longer needed)
            for _, img, _ in group:
                img.close()

        # Release references to loaded images (PIL images now closed)
        del loaded_images
        del dimension_groups

        if not all_faces_data or should_stop():
            return results

        # Phase 4: Batch compute embeddings for ALL faces across all images
        # Tensors are on CPU from Phase 3, move to GPU for ResNet.
        # Note: MTCNN with post_process=True already standardizes to [-1, 1] for ResNet
        if self.resnet is None:
            logger.warning('ResNet model unavailable — skipping face embeddings')
            return results

        try:
            all_tensors = torch.stack([fd[1] for fd in all_faces_data]).to(self.device)

            with torch.no_grad():
                embeddings_batch = self.resnet(all_tensors)
                embeddings_batch = embeddings_batch.cpu().numpy()

            # Release GPU memory from embedding computation
            del all_tensors
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

        except (MemoryError, RuntimeError) as e:
            if not isinstance(e, MemoryError) and 'out of memory' not in str(e).lower():
                raise
            logger.warning(
                f'OOM computing face embeddings for batch of {len(all_faces_data)} faces, '
                f'falling back to single-face processing'
            )
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            # Process one face at a time
            single_embeddings = []
            failed_indices = set()
            for i, fd in enumerate(all_faces_data):
                try:
                    single_tensor = fd[1].unsqueeze(0).to(self.device)
                    with torch.no_grad():
                        emb = self.resnet(single_tensor)
                        single_embeddings.append(emb.cpu().numpy().flatten())
                    del single_tensor
                except (MemoryError, RuntimeError):
                    logger.error(f'OOM computing embedding for single face {i}, skipping')
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                    failed_indices.add(i)

            if failed_indices:
                # Filter out failed faces
                all_faces_data = [fd for i, fd in enumerate(all_faces_data) if i not in failed_indices]
                embeddings_batch = np.array([e for i, e in enumerate(single_embeddings) if i not in failed_indices])
            else:
                embeddings_batch = np.array(single_embeddings)

            if len(embeddings_batch) == 0:
                return results

        # Clear CPU tensors from all_faces_data (keep only metadata)
        all_faces_metadata = [
            (fd[0], fd[2], fd[3])  # (image_path, norm_box, confidence)
            for fd in all_faces_data
        ]
        del all_faces_data

        # Normalise embeddings
        norms = np.linalg.norm(embeddings_batch, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings_batch = embeddings_batch / norms

        # Phase 5: Build results dict
        for idx, (image_path, norm_box, confidence) in enumerate(all_faces_metadata):
            norm_x, norm_y, norm_w, norm_h = norm_box
            results[image_path].append(
                DetectedFace(
                    box_x=float(norm_x),
                    box_y=float(norm_y),
                    box_w=float(norm_w),
                    box_h=float(norm_h),
                    confidence=confidence,
                    embedding=embeddings_batch[idx],
                )
            )

        return results


# =============================================================================
# FACE THUMBNAIL GENERATION
# =============================================================================


def _create_face_thumbnail(
    img: Image.Image,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    size: int = 200,
) -> Image.Image:
    """Create a face thumbnail from a pre-loaded image.

    Core thumbnail generation logic used by both generate_face_thumbnail
    and batch regeneration.

    Args:
        img: PIL Image (already RGB, EXIF-corrected).
        box_x: Normalised x coordinate of face box (0-1).
        box_y: Normalised y coordinate of face box (0-1).
        box_w: Normalised width of face box (0-1).
        box_h: Normalised height of face box (0-1).
        size: Output thumbnail size in pixels (square).

    Returns:
        Square PIL Image thumbnail.
    """
    width, height = img.size

    # Convert normalised coordinates to pixels
    px_x = int(box_x * width)
    px_y = int(box_y * height)
    px_w = int(box_w * width)
    px_h = int(box_h * height)

    # Expand crop region slightly for context
    padding = int(max(px_w, px_h) * FACE_CROP_PADDING_RATIO)
    px_x = max(0, px_x - padding)
    px_y = max(0, px_y - padding)
    px_w = min(width - px_x, px_w + 2 * padding)
    px_h = min(height - px_y, px_h + 2 * padding)

    # Crop the face region
    face_crop = img.crop((px_x, px_y, px_x + px_w, px_y + px_h))

    # Create square thumbnail without distortion
    crop_w, crop_h = face_crop.size
    aspect_ratio = crop_w / crop_h if crop_h > 0 else 1.0

    # If nearly square, just resize directly
    if (1 - FACE_THUMB_SQUARE_TOLERANCE) <= aspect_ratio <= (1 + FACE_THUMB_SQUARE_TOLERANCE):
        thumb = face_crop.resize((size, size), Image.Resampling.LANCZOS)
    else:
        # Non-square: create blurred/darkened background with centred face
        # Background: stretch to square, blur, darken
        background = face_crop.resize((size, size), Image.Resampling.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(radius=FACE_THUMB_BG_BLUR_RADIUS))
        # Darken by blending with black
        darkener = Image.new('RGB', (size, size), (0, 0, 0))
        background = Image.blend(background, darkener, FACE_THUMB_BG_DARKEN_ALPHA)

        # Foreground: resize proportionally to fit within square
        if crop_w > crop_h:
            # Wider than tall - fit to width
            new_w = size
            new_h = int(size * crop_h / crop_w)
        else:
            # Taller than wide - fit to height
            new_h = size
            new_w = int(size * crop_w / crop_h)

        foreground = face_crop.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # Centre foreground on background
        paste_x = (size - new_w) // 2
        paste_y = (size - new_h) // 2
        background.paste(foreground, (paste_x, paste_y))
        thumb = background

    # Apply subtle sharpening
    thumb = thumb.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=3))

    return thumb


def generate_face_thumbnail(
    source_path: Path | str,
    dest_path: Path | str,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    size: int = 200,
    quality: int = 85,
) -> bool:
    """Generate a face thumbnail from a source image.

    Crops the face region from the full-size image and saves as a square
    thumbnail. For non-square crops, creates a blurred background with the
    undistorted face centred. Applies sharpening to counteract downscale blur.

    Args:
        source_path: Path to the source image.
        dest_path: Path where thumbnail should be saved.
        box_x: Normalised x coordinate of face box (0-1).
        box_y: Normalised y coordinate of face box (0-1).
        box_w: Normalised width of face box (0-1).
        box_h: Normalised height of face box (0-1).
        size: Output thumbnail size in pixels (square).
        quality: JPEG quality (1-100).

    Returns:
        True if thumbnail was generated successfully, False otherwise.
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)

    try:
        # Ensure destination directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # raw_open_image handles both standard and RAW formats with EXIF rotation
        img = raw_open_image(source_path)

        # Convert to RGB
        if img.mode != 'RGB':
            img = img.convert('RGB')

        thumb = _create_face_thumbnail(img, box_x, box_y, box_w, box_h, size)
        thumb.save(dest_path, 'JPEG', quality=quality, optimize=True)

        logger.debug(f'Generated face thumbnail: {dest_path}')
        return True

    except Exception as e:
        logger.error(f'Failed to generate face thumbnail for {source_path}: {e}')
        return False


def generate_face_thumbnails_for_image(
    source_path: Path | str,
    faces: list[dict],
    thumbnail_dir: Path | str,
    size: int = 200,
    quality: int = 85,
) -> int:
    """Generate thumbnails for multiple faces from a single source image.

    Loads the image once and generates all face thumbnails efficiently.

    Args:
        source_path: Path to the source image.
        faces: List of face dicts with 'face_id', 'box_x', 'box_y', 'box_w', 'box_h'.
        thumbnail_dir: Root thumbnail cache directory.
        size: Output thumbnail size in pixels (square).
        quality: JPEG quality (1-100).

    Returns:
        Number of thumbnails successfully generated.
    """
    source_path = Path(source_path)

    if not faces:
        return 0

    try:
        # raw_open_image handles both standard and RAW formats with EXIF rotation
        img = raw_open_image(source_path)

        # Convert to RGB
        if img.mode != 'RGB':
            img = img.convert('RGB')

        count = 0
        for face in faces:
            dest_path = get_face_thumbnail_path(face['face_id'], thumbnail_dir)
            dest_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                thumb = _create_face_thumbnail(
                    img,
                    face['box_x'],
                    face['box_y'],
                    face['box_w'],
                    face['box_h'],
                    size,
                )
                thumb.save(dest_path, 'JPEG', quality=quality, optimize=True)
                count += 1
            except Exception as e:
                logger.warning(f'Failed to generate thumbnail for face {face["face_id"]}: {e}')

        return count

    except Exception as e:
        logger.error(f'Failed to load image {source_path}: {e}')
        return 0


def get_face_thumbnail_path(
    face_id: str,
    thumbnail_dir: Path | str = '.thumbnails',
) -> Path:
    """Get the cache path for a face thumbnail.

    Cache structure: <thumbnail_dir>/faces/<first2chars>/<face_id>.jpg

    Args:
        face_id: Face UUID.
        thumbnail_dir: Root thumbnail cache directory.

    Returns:
        Path where the face thumbnail should be cached.
    """
    thumbnail_dir = Path(thumbnail_dir)
    prefix = face_id[:2] if len(face_id) >= 2 else 'xx'
    return thumbnail_dir / 'faces' / prefix / f'{face_id}.jpg'


def delete_face_thumbnail(
    face_id: str,
    thumbnail_dir: Path | str = '.thumbnails',
) -> bool:
    """Delete a face thumbnail from the cache.

    Args:
        face_id: Face UUID.
        thumbnail_dir: Root thumbnail cache directory.

    Returns:
        True if deleted, False if not found or error.
    """
    thumb_path = get_face_thumbnail_path(face_id, thumbnail_dir)
    if thumb_path.exists():
        try:
            thumb_path.unlink()
            return True
        except OSError as e:
            logger.warning(f'Failed to delete face thumbnail {thumb_path}: {e}')
    return False


# =============================================================================
# PEOPLE CRUD OPERATIONS
# =============================================================================


def create_person(
    conn: SafeConnection,
    name: str,
    person_id: str | None = None,
) -> str:
    """Create a new person record.

    Args:
        conn: Database connection.
        name: Person's name.
        person_id: Optional UUID. If None, generates a new one.

    Returns:
        The person's UUID.
    """
    if person_id is None:
        person_id = str(uuid.uuid4())

    conn.execute("""INSERT INTO people (id, name) VALUES (?, ?)""", (person_id, name))
    conn.commit()
    logger.debug(f'Created person: {name} ({person_id})')
    return person_id


def get_person(
    conn: SafeConnection,
    person_id: str,
) -> dict[str, Any] | None:
    """Get a person by ID with face count.

    Args:
        conn: Database connection.
        person_id: Person's UUID.

    Returns:
        Person dict with face_count, or None if not found.
    """
    # DESIGN: Computed face_count in GET response - standard API efficiency pattern,
    # avoids frontend needing separate query for counts (see design-audit.md 1.5)
    cursor = conn.execute(
        """SELECT p.*, COUNT(f.id) as face_count
           FROM people p
           LEFT JOIN faces f ON f.person_id = p.id AND f.suppressed = 0
           WHERE p.id = ?
           GROUP BY p.id""",
        (person_id,),
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_person_by_name(
    conn: SafeConnection,
    name: str,
) -> dict[str, Any] | None:
    """Get a person by name (case-insensitive).

    Args:
        conn: Database connection.
        name: Person's name.

    Returns:
        Person dict or None if not found.
    """
    cursor = conn.execute("""SELECT * FROM people WHERE name = ? COLLATE NOCASE""", (name,))
    row = cursor.fetchone()
    return dict(row) if row else None


def get_all_people(conn: SafeConnection) -> list[dict[str, Any]]:
    """Get all people with their face counts.

    Args:
        conn: Database connection.

    Returns:
        List of person dicts with 'face_count' and 'preferred_face_updated_at' fields.
    """
    # DESIGN: Computed face_count in GET response - standard API efficiency pattern,
    # avoids frontend needing separate query for counts (see design-audit.md 1.5)
    cursor = conn.execute("""
        SELECT p.*, COUNT(f.id) as face_count, pf.updated_at as preferred_face_updated_at
        FROM people p
        LEFT JOIN faces f ON f.person_id = p.id AND f.suppressed = 0
        LEFT JOIN faces pf ON pf.id = p.preferred_face_id
        GROUP BY p.id
        ORDER BY p.name COLLATE NOCASE
    """)
    return [dict(row) for row in cursor.fetchall()]


# Sentinel value to distinguish "not passed" from "passed as None"
_NOT_SET = object()


def update_person(
    conn: SafeConnection,
    person_id: str,
    name: str | None = None,
    preferred_face_id: str | None = None,
    recognition_threshold: float | None = _NOT_SET,
) -> bool:
    """Update a person record.

    Args:
        conn: Database connection.
        person_id: Person's UUID.
        name: New name (optional).
        preferred_face_id: New preferred face ID (optional).
        recognition_threshold: Custom threshold (float), or None to clear override.

    Returns:
        True if updated, False if person not found.
    """
    updates = []
    params = []

    if name is not None:
        updates.append('name = ?')
        params.append(name)

    if preferred_face_id is not None:
        updates.append('preferred_face_id = ?')
        params.append(preferred_face_id)

    if recognition_threshold is not _NOT_SET:
        updates.append('recognition_threshold = ?')
        params.append(recognition_threshold)  # Can be float or None

    if not updates:
        return True

    updates.append("updated_at = datetime('now')")
    params.append(person_id)

    cursor = conn.execute(f"""UPDATE people SET {', '.join(updates)} WHERE id = ?""", params)
    conn.commit()
    return cursor.rowcount > 0


def revalidate_person_faces(
    conn: SafeConnection,
    person_id: str,
    threshold: float,
) -> list[str]:
    """Revalidate faces for a person against a threshold.

    Checks each face's similarity to other faces of the same person.
    Faces that don't meet the threshold are unassigned (ejected to unknown pool).

    DESIGN: This function implements atomic cascade behaviour - if ejecting faces would
    leave preferred_face_id invalid, auto-selects a new one. This maintains the data
    integrity invariant that person.preferred_face_id must always be valid.
    (see design-audit.md 1.4)

    Args:
        conn: Database connection.
        person_id: Person's UUID.
        threshold: Minimum similarity threshold.

    Returns:
        List of face IDs that were ejected.
    """
    # Get all faces for this person with embeddings
    # Include manually_tagged so we can skip locked faces during ejection,
    # but still use their embeddings for similarity comparison
    cursor = conn.execute(
        """SELECT id, embedding, manually_tagged FROM faces
           WHERE person_id = ? AND suppressed = 0""",
        (person_id,),
    )
    faces = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        faces.append((row['id'], embedding, bool(row['manually_tagged'])))

    if len(faces) <= 1:
        # Can't eject if only 0 or 1 face - nothing to compare against
        return []

    # Build embedding matrix (ALL faces, including locked, for similarity comparison)
    face_ids = [f[0] for f in faces]
    face_locked = [f[2] for f in faces]
    try:
        embeddings = np.vstack([f[1] for f in faces])
    except (MemoryError, RuntimeError):
        logger.error(f'OOM building embedding matrix for person {person_id} ({len(faces)} faces), skipping eject')
        return []

    # Ensure normalised (guard against zero-norm embeddings from corruption)
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if not np.allclose(norms, 1.0, atol=0.01):
        norms[norms == 0] = 1
        embeddings = embeddings / norms

    # Compute pairwise similarities (chunked for persons with many faces)
    n = len(faces)
    CHUNK_THRESHOLD = 500
    if n <= CHUNK_THRESHOLD:
        similarities = embeddings @ embeddings.T
    else:
        # Chunked: compute max-similarity per face without O(n²) memory
        logger.info(f'Person {person_id} has {n} faces, using chunked similarity')
        chunk_size = CHUNK_THRESHOLD
        similarities = np.full((n, n), -1.0, dtype=np.float32)
        for start in range(0, n, chunk_size):
            end = min(start + chunk_size, n)
            similarities[start:end] = embeddings[start:end] @ embeddings.T

    # For each face, find max similarity to OTHER faces (exclude self on diagonal)
    np.fill_diagonal(similarities, -1)  # Exclude self-similarity
    max_similarities = np.max(similarities, axis=1)

    # Find faces that don't meet threshold (skip locked faces - user confirmed these)
    ejected_ids = []
    for i, (face_id, max_sim) in enumerate(zip(face_ids, max_similarities, strict=True)):
        if face_locked[i]:
            continue  # Locked faces are never ejected
        if max_sim < threshold:
            logger.info(
                f'Ejecting face {face_id} from person {person_id}: '
                f'max similarity {max_sim:.3f} < threshold {threshold:.3f}'
            )
            ejected_ids.append(face_id)

    # Unassign ejected faces (clear manually_tagged so they're candidates for reassessment)
    if ejected_ids:
        for face_id in ejected_ids:
            update_face_person(conn, face_id, None, manually_tagged=False)

        # Check if preferred face was ejected - if so, select new preferred
        person = get_person(conn, person_id)
        if person and person.get('preferred_face_id') in ejected_ids:
            # Get remaining faces
            remaining = conn.execute(
                """SELECT id FROM faces
                   WHERE person_id = ? AND suppressed = 0
                   ORDER BY id""",
                (person_id,),
            ).fetchall()
            if remaining:
                new_preferred = remaining[0]['id']
                conn.execute('UPDATE people SET preferred_face_id = ? WHERE id = ?', (new_preferred, person_id))
                conn.commit()

        # Invalidate embedding cache since faces moved
        invalidate_embedding_cache()

    return ejected_ids


def delete_person(
    conn: SafeConnection,
    person_id: str,
) -> bool:
    """Delete a person record.

    This also unlinks all faces associated with this person
    (person_id set to NULL via ON DELETE SET NULL). We explicitly clear
    manually_tagged before deletion because the foreign key cascade only
    nulls person_id, leaving orphaned locked faces that would never be
    candidates for reassessment.

    Args:
        conn: Database connection.
        person_id: Person's UUID.

    Returns:
        True if deleted, False if person not found.
    """
    # Clear manually_tagged before delete - ON DELETE SET NULL only nulls person_id
    conn.execute(
        """UPDATE faces SET manually_tagged = 0, updated_at = datetime('now')
           WHERE person_id = ? AND manually_tagged = 1""",
        (person_id,),
    )
    cursor = conn.execute("""DELETE FROM people WHERE id = ?""", (person_id,))
    conn.commit()
    return cursor.rowcount > 0


def search_people(
    conn: SafeConnection,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search people by name (case-insensitive substring match).

    Args:
        conn: Database connection.
        query: Search query.
        limit: Maximum results to return.

    Returns:
        List of matching person dicts with preferred_face_updated_at.
    """
    # Escape LIKE wildcards (%, _) in user input to prevent them from
    # acting as pattern characters in the substring match.
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    cursor = conn.execute(
        """SELECT p.*, pf.updated_at as preferred_face_updated_at
           FROM people p
           LEFT JOIN faces pf ON pf.id = p.preferred_face_id
           WHERE p.name LIKE ? ESCAPE '\\' COLLATE NOCASE
           ORDER BY p.name COLLATE NOCASE
           LIMIT ?""",
        (f'%{escaped}%', limit),
    )
    return [dict(row) for row in cursor.fetchall()]


def delete_people_without_faces(conn: SafeConnection) -> int:
    """Delete all people who have no associated faces.

    Args:
        conn: Database connection.

    Returns:
        Number of people deleted.
    """
    cursor = conn.execute("""
        DELETE FROM people
        WHERE id NOT IN (
            SELECT DISTINCT person_id
            FROM faces
            WHERE person_id IS NOT NULL AND suppressed = 0
        )
    """)
    conn.commit()
    deleted = cursor.rowcount
    if deleted > 0:
        logger.info(f'Deleted {deleted} people with no faces')
    return deleted


# =============================================================================
# FACES CRUD OPERATIONS
# =============================================================================


def create_face(
    conn: SafeConnection,
    image_id: str,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    embedding: np.ndarray,
    confidence: float | None = None,
    person_id: str | None = None,
    face_id: str | None = None,
    semantic_embedding: np.ndarray | None = None,
) -> str:
    """Create a new face record.

    Args:
        conn: Database connection.
        image_id: ID of the image containing this face.
        box_x: Normalised x coordinate of bounding box.
        box_y: Normalised y coordinate of bounding box.
        box_w: Normalised width of bounding box.
        box_h: Normalised height of bounding box.
        embedding: 512D face embedding for recognition.
        confidence: Detection confidence (optional).
        person_id: Associated person ID (optional).
        face_id: Optional UUID. If None, generates a new one.
        semantic_embedding: OpenCLIP embedding for text search (optional).

    Returns:
        The face's UUID.
    """
    # DESIGN: Backend generates face IDs because faces are ML-detected entities that
    # frontend cannot pre-generate IDs for (see design-audit.md 1.10)
    if face_id is None:
        face_id = str(uuid.uuid4())

    # Convert embeddings to bytes
    embedding_bytes = embedding.astype(np.float32).tobytes()
    semantic_bytes = None
    if semantic_embedding is not None:
        semantic_bytes = semantic_embedding.astype(np.float32).tobytes()

    conn.execute(
        """INSERT INTO faces
           (id, image_id, box_x, box_y, box_w, box_h, confidence, embedding,
            person_id, semantic_embedding, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (face_id, image_id, box_x, box_y, box_w, box_h, confidence, embedding_bytes, person_id, semantic_bytes),
    )
    conn.commit()
    return face_id


def update_face_semantic_embedding(
    conn: SafeConnection,
    face_id: str,
    semantic_embedding: np.ndarray,
) -> bool:
    """Update a face's semantic embedding.

    Args:
        conn: Database connection.
        face_id: Face's UUID.
        semantic_embedding: OpenCLIP embedding for text search.

    Returns:
        True if updated, False if face not found.
    """
    semantic_bytes = semantic_embedding.astype(np.float32).tobytes()
    cursor = conn.execute(
        "UPDATE faces SET semantic_embedding = ?, updated_at = datetime('now') WHERE id = ?", (semantic_bytes, face_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def get_all_faces_for_thumbnail_regen(
    conn: SafeConnection,
) -> list[dict]:
    """Get all non-suppressed faces with info needed for thumbnail regeneration.

    Args:
        conn: Database connection.

    Returns:
        List of dicts with face_id, image_id, box_x, box_y, box_w, box_h.
    """
    cursor = conn.execute("""
        SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h, i.path
        FROM faces f
        JOIN images i ON f.image_id = i.id
        WHERE f.suppressed = 0 AND i.deleted = 0
    """)
    return [
        {
            'face_id': row['id'],
            'image_id': row['image_id'],
            'box_x': row['box_x'],
            'box_y': row['box_y'],
            'box_w': row['box_w'],
            'box_h': row['box_h'],
            'image_path': row['path'],
        }
        for row in cursor.fetchall()
    ]


def get_faces_without_semantic_embedding(
    conn: SafeConnection,
) -> list[str]:
    """Get IDs of faces that don't have semantic embeddings.

    Args:
        conn: Database connection.

    Returns:
        List of face IDs.
    """
    cursor = conn.execute(
        """SELECT f.id FROM faces f
           JOIN images i ON f.image_id = i.id
           WHERE f.semantic_embedding IS NULL AND f.suppressed = 0
             AND i.deleted = 0"""
    )
    return [row['id'] for row in cursor.fetchall()]


def get_face(
    conn: SafeConnection,
    face_id: str,
) -> dict[str, Any] | None:
    """Get a face by ID.

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        Face dict with person_name if identified, or None if not found.
    """
    cursor = conn.execute(
        """SELECT f.*, p.name as person_name
           FROM faces f
           LEFT JOIN people p ON f.person_id = p.id
           WHERE f.id = ?""",
        (face_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None

    face = dict(row)
    # Convert embedding bytes to numpy array
    if face.get('embedding'):
        face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
    return face


def get_faces_for_image(
    conn: SafeConnection,
    image_id: str,
    include_suppressed: bool = False,
) -> list[dict[str, Any]]:
    """Get all faces detected in an image.

    Args:
        conn: Database connection.
        image_id: Image's UUID.
        include_suppressed: If True, include suppressed faces.

    Returns:
        List of face dicts.
    """
    if include_suppressed:
        cursor = conn.execute(
            """SELECT f.*, p.name as person_name
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               WHERE f.image_id = ?""",
            (image_id,),
        )
    else:
        cursor = conn.execute(
            """SELECT f.*, p.name as person_name
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               WHERE f.image_id = ? AND f.suppressed = 0""",
            (image_id,),
        )

    faces = []
    for row in cursor.fetchall():
        face = dict(row)
        # Convert embedding bytes to numpy array
        if face.get('embedding'):
            face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
        faces.append(face)
    return faces


def get_faces_for_images(
    conn: SafeConnection,
    image_ids: list[str],
) -> list[dict[str, Any]]:
    """Get all non-suppressed faces for multiple images (batch operation).

    Args:
        conn: Database connection.
        image_ids: List of image UUIDs.

    Returns:
        List of face dicts with person_name. Does not include embedding blob.
    """
    if not image_ids:
        return []

    # Use parameterized query with placeholder for each ID
    placeholders = sql_placeholders(image_ids)
    cursor = conn.execute(
        f"""SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h,
                   f.confidence, f.person_id, f.created_at, f.manually_tagged,
                   f.unknown_group_id, f.suppressed,
                   p.name as person_name
            FROM faces f
            LEFT JOIN people p ON f.person_id = p.id
            WHERE f.image_id IN ({placeholders}) AND f.suppressed = 0
            ORDER BY f.image_id, f.created_at""",
        image_ids,
    )

    return [dict(row) for row in cursor.fetchall()]


def get_faces_for_person(
    conn: SafeConnection,
    person_id: str,
) -> list[dict[str, Any]]:
    """Get all faces for a person.

    Args:
        conn: Database connection.
        person_id: Person's UUID.

    Returns:
        List of face dicts with is_preferred and image_timestamp.
    """
    cursor = conn.execute(
        """SELECT f.*,
                  i.timestamp as image_timestamp,
                  i.basename as image_basename,
                  CASE WHEN f.id = p.preferred_face_id THEN 1 ELSE 0 END as is_preferred
           FROM faces f
           JOIN images i ON f.image_id = i.id
           JOIN people p ON f.person_id = p.id
           WHERE f.person_id = ? AND f.suppressed = 0 AND i.deleted = 0
           ORDER BY i.timestamp""",
        (person_id,),
    )

    faces = []
    for row in cursor.fetchall():
        face = dict(row)
        if face.get('embedding'):
            face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
        faces.append(face)
    return faces


def get_all_faces(
    conn: SafeConnection,
    unknown_only: bool = False,
) -> list[dict]:
    """Get all non-suppressed faces with person info.

    Returns faces ordered by person name (known faces first), then unknown faces.
    Unknown faces are sorted by group size (largest first), then by image timestamp.
    Does not include the embedding blob for efficiency.

    Args:
        conn: Database connection.
        unknown_only: If True, only return faces without a person_id.

    Returns:
        List of face dicts with person_name, is_preferred, and group info included.
    """
    if unknown_only:
        # Return unknown faces sorted by group size and timestamp
        # Note: When unknown_group_id is NULL, treat as singleton (group_size=1)
        cursor = conn.execute(
            """SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h,
                      f.confidence, f.person_id, f.created_at,
                      f.unknown_group_id,
                      NULL as person_name,
                      0 as is_preferred,
                      i.timestamp as image_timestamp,
                      i.basename as image_basename,
                      CASE WHEN f.unknown_group_id IS NULL THEN 1
                           ELSE COUNT(*) OVER (PARTITION BY f.unknown_group_id) END as group_size
               FROM faces f
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0 AND f.person_id IS NULL AND i.deleted = 0
               ORDER BY
                   CASE WHEN f.unknown_group_id IS NULL THEN 0 ELSE
                       COUNT(*) OVER (PARTITION BY f.unknown_group_id) END DESC,
                   f.unknown_group_id,
                   i.timestamp"""
        )
    else:
        cursor = conn.execute(
            """SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h,
                      f.confidence, f.person_id, f.created_at,
                      f.unknown_group_id,
                      p.name as person_name,
                      CASE WHEN f.id = p.preferred_face_id THEN 1 ELSE 0 END as is_preferred,
                      i.timestamp as image_timestamp,
                      i.basename as image_basename,
                      CASE WHEN f.person_id IS NOT NULL THEN NULL
                           WHEN f.unknown_group_id IS NULL THEN 1
                           ELSE COUNT(*) OVER (PARTITION BY f.unknown_group_id) END as group_size
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0 AND i.deleted = 0
               ORDER BY
                   CASE WHEN f.person_id IS NULL THEN 1 ELSE 0 END,
                   p.name COLLATE NOCASE,
                   CASE WHEN f.unknown_group_id IS NULL THEN 0 ELSE
                       CASE WHEN f.person_id IS NULL
                            THEN COUNT(*) OVER (PARTITION BY f.unknown_group_id)
                            ELSE 0 END END DESC,
                   f.unknown_group_id,
                   i.timestamp"""
        )

    return [dict(row) for row in cursor.fetchall()]


def get_all_known_face_embeddings(
    conn: SafeConnection,
) -> list[tuple[str, str, np.ndarray]]:
    """Get all face embeddings for known people (manually tagged only).

    Returns embeddings for faces that have been manually identified by the user
    (have a person_id, not suppressed, and manually_tagged=1).
    Used for auto-recognition of new faces.

    Only manually-tagged faces are used to prevent the "snowball effect"
    where auto-matched faces are used to match more faces, potentially
    propagating errors.

    Args:
        conn: Database connection.

    Returns:
        List of (face_id, person_id, embedding) tuples.
    """
    cursor = conn.execute(
        """SELECT f.id, f.person_id, f.embedding
           FROM faces f
           JOIN images i ON f.image_id = i.id
           WHERE f.person_id IS NOT NULL AND f.suppressed = 0
             AND f.manually_tagged = 1 AND i.deleted = 0"""
    )

    results = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        results.append((row['id'], row['person_id'], embedding))
    return results


def get_face_matches(
    conn: SafeConnection,
    face_id: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Get top matching people for a face based on similarity to locked faces.

    Compares the given face's embedding against all locked (manually_tagged=1)
    faces and returns the top N people with their best-matching face.

    Args:
        conn: Database connection.
        face_id: The face to find matches for.
        limit: Maximum number of matches to return.

    Returns:
        List of dicts with:
        - person_id: ID of the matching person
        - person_name: Name of the matching person
        - face_id: ID of their best-matching locked face
        - similarity: Cosine similarity score (0-1)
    """
    # Get the target face's embedding
    cursor = conn.execute('SELECT embedding FROM faces WHERE id = ? AND suppressed = 0', (face_id,))
    row = cursor.fetchone()
    if not row or not row['embedding']:
        return []

    target_embedding = np.frombuffer(row['embedding'], dtype=np.float32)

    # Get all locked face embeddings with person info
    cursor = conn.execute(
        """SELECT f.id, f.person_id, f.embedding, p.name as person_name
           FROM faces f
           JOIN people p ON f.person_id = p.id
           JOIN images i ON f.image_id = i.id
           WHERE f.suppressed = 0 AND f.manually_tagged = 1
             AND p.name != '-' AND i.deleted = 0"""
    )

    # Build list of (face_id, person_id, person_name, embedding)
    locked_faces = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        locked_faces.append((row['id'], row['person_id'], row['person_name'], embedding))

    if not locked_faces:
        return []

    # Compute similarities
    locked_matrix = np.vstack([emb for _, _, _, emb in locked_faces])

    # Ensure L2-normalised (guard against zero-norm from corruption)
    target_norm = np.linalg.norm(target_embedding)
    if target_norm == 0:
        return []  # Zero-norm embedding cannot produce meaningful matches
    if not np.isclose(target_norm, 1.0, atol=0.01):
        target_embedding = target_embedding / target_norm
    locked_norms = np.linalg.norm(locked_matrix, axis=1)
    if not np.allclose(locked_norms, 1.0, atol=0.01):
        locked_norms[locked_norms == 0] = 1
        locked_matrix = locked_matrix / locked_norms[:, np.newaxis]

    # Dot product = cosine similarity for L2-normalised vectors
    similarities = target_embedding @ locked_matrix.T

    # Group by person, keeping only the best match per person
    person_best: dict[str, tuple[str, str, float]] = {}  # person_id -> (face_id, name, similarity)
    for i, (fid, pid, pname, _) in enumerate(locked_faces):
        sim = float(similarities[i])
        if pid not in person_best or sim > person_best[pid][2]:
            person_best[pid] = (fid, pname, sim)

    # Sort by similarity descending and take top N
    sorted_matches = sorted(person_best.items(), key=lambda x: x[1][2], reverse=True)[:limit]

    return [
        {
            'person_id': pid,
            'person_name': data[1],
            'face_id': data[0],
            'similarity': data[2],
        }
        for pid, data in sorted_matches
    ]


def update_face_person(
    conn: SafeConnection,
    face_id: str,
    person_id: str | None,
    manually_tagged: bool | None = None,
) -> bool:
    """Update the person associated with a face.

    Args:
        conn: Database connection.
        face_id: Face's UUID.
        person_id: Person's UUID, or None to unlink.
        manually_tagged: If True, mark as manually tagged. If False, mark as auto-tagged.
            If None, don't change the manually_tagged flag.

    Returns:
        True if updated, False if face not found.
    """
    if manually_tagged is None:
        cursor = conn.execute(
            """UPDATE faces SET person_id = ?, updated_at = datetime('now') WHERE id = ?""", (person_id, face_id)
        )
    else:
        cursor = conn.execute(
            """UPDATE faces SET person_id = ?, manually_tagged = ?, updated_at = datetime('now') WHERE id = ?""",
            (person_id, 1 if manually_tagged else 0, face_id),
        )
    conn.commit()
    return cursor.rowcount > 0


def toggle_face_manual_tag(
    conn: SafeConnection,
    face_id: str,
) -> bool | None:
    """Toggle the manually_tagged flag for a face.

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        The new manually_tagged value (True/False), or None if face not found.
    """
    # Get current value
    cursor = conn.execute("""SELECT manually_tagged FROM faces WHERE id = ?""", (face_id,))
    row = cursor.fetchone()
    if row is None:
        return None

    # Toggle the value
    current = row['manually_tagged'] or 0
    new_value = 0 if current else 1

    conn.execute(
        """UPDATE faces SET manually_tagged = ?, updated_at = datetime('now') WHERE id = ?""", (new_value, face_id)
    )
    conn.commit()

    # Invalidate cache since manually_tagged affects which faces are used for matching
    invalidate_embedding_cache()

    return bool(new_value)


def suppress_face(
    conn: SafeConnection,
    face_id: str,
) -> bool:
    """Mark a face as suppressed (false positive).

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        True if updated, False if face not found.
    """
    cursor = conn.execute(
        """UPDATE faces SET suppressed = 1, person_id = NULL, updated_at = datetime('now') WHERE id = ?""", (face_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def unassign_faces_batch(
    conn: SafeConnection,
    face_ids: list[str],
) -> int:
    """Unassign multiple faces from their persons in a single transaction.

    Clears person_id and manually_tagged for all given faces, using
    ``executemany`` + one commit instead of per-item updates.

    Args:
        conn: Database connection.
        face_ids: List of face UUIDs to unassign.

    Returns:
        Number of faces actually updated.
    """
    if not face_ids:
        return 0
    conn.executemany(
        "UPDATE faces SET person_id = NULL, manually_tagged = 0, updated_at = datetime('now') WHERE id = ?",
        [(fid,) for fid in face_ids],
    )
    conn.commit()
    return len(face_ids)


def suppress_faces_batch(
    conn: SafeConnection,
    face_ids: list[str],
) -> int:
    """Suppress multiple faces in a single transaction.

    Marks faces as false positives (suppressed=1, person_id=NULL) using
    ``executemany`` + one commit instead of per-item updates.

    Args:
        conn: Database connection.
        face_ids: List of face UUIDs to suppress.

    Returns:
        Number of faces suppressed.
    """
    if not face_ids:
        return 0
    conn.executemany(
        "UPDATE faces SET suppressed = 1, person_id = NULL, updated_at = datetime('now') WHERE id = ?",
        [(fid,) for fid in face_ids],
    )
    conn.commit()
    return len(face_ids)


def mark_no_faces_detected(
    conn: SafeConnection,
    image_id: str,
) -> str:
    """Mark an image as having been processed with no faces found.

    Creates a sentinel face record that is pre-suppressed, with zero-size
    bounding box and a zero-vector embedding. This prevents the image from
    being re-queued for face detection on subsequent runs.

    Args:
        conn: Database connection.
        image_id: ID of the image.

    Returns:
        The sentinel face record's UUID.
    """
    face_id = str(uuid.uuid4())
    # Create a 512-dimensional zero vector as dummy embedding
    dummy_embedding = np.zeros(512, dtype=np.float32).tobytes()
    # NOTE: The 0x0 bounding box is intentional - these are sentinel records
    # marking "no faces found", not real faces. They appear in the database
    # as suppressed faces with zero dimensions. Do not treat as a bug.
    conn.execute(
        """INSERT INTO faces
           (id, image_id, box_x, box_y, box_w, box_h, confidence, embedding,
            person_id, suppressed, created_at, updated_at)
           VALUES (?, ?, 0, 0, 0, 0, 0, ?, NULL, 1, datetime('now'), datetime('now'))""",
        (face_id, image_id, dummy_embedding),
    )
    conn.commit()
    return face_id


def delete_face(
    conn: SafeConnection,
    face_id: str,
) -> bool:
    """Delete a face record entirely.

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        True if deleted, False if face not found.
    """
    cursor = conn.execute("""DELETE FROM faces WHERE id = ?""", (face_id,))
    conn.commit()
    return cursor.rowcount > 0


def rotate_faces_for_image(
    conn: SafeConnection,
    image_id: str,
    degrees: float,
) -> int:
    """Rotate all face bounding boxes for an image.

    When an image is rotated, the face bounding boxes need to be transformed
    to match the new orientation. This updates the box_x, box_y, box_w, box_h
    values in the database.

    For 90° clockwise rotation:
        new_x = 1 - old_y - old_h
        new_y = old_x
        new_w = old_h
        new_h = old_w

    For 180° rotation:
        new_x = 1 - old_x - old_w
        new_y = 1 - old_y - old_h
        new_w = old_w
        new_h = old_h

    For 270° (90° left) rotation:
        new_x = old_y
        new_y = 1 - old_x - old_w
        new_w = old_h
        new_h = old_w

    Args:
        conn: Database connection.
        image_id: Image's UUID.
        degrees: Rotation angle (clockwise positive). Supports 90, 180, 270.

    Returns:
        Number of faces updated.
    """
    # Normalise degrees to 0-360 range
    degrees = degrees % 360
    if degrees == 0:
        return 0

    # Get all faces for this image
    cursor = conn.execute("""SELECT id, box_x, box_y, box_w, box_h FROM faces WHERE image_id = ?""", (image_id,))
    faces = cursor.fetchall()

    if not faces:
        return 0

    updated_count = 0
    for face_id, box_x, box_y, box_w, box_h in faces:
        if degrees == 90:
            # 90° clockwise rotation
            new_x = 1.0 - box_y - box_h
            new_y = box_x
            new_w = box_h
            new_h = box_w
        elif degrees == 180:
            # 180° rotation
            new_x = 1.0 - box_x - box_w
            new_y = 1.0 - box_y - box_h
            new_w = box_w
            new_h = box_h
        elif degrees == 270:
            # 270° (90° left) rotation
            new_x = box_y
            new_y = 1.0 - box_x - box_w
            new_w = box_h
            new_h = box_w
        else:
            # Arbitrary angles not supported for face bboxes yet
            logger.warning(f'Arbitrary rotation angle {degrees}° not supported for face bboxes')
            continue

        logger.debug(
            f'rotate_faces_for_image: {face_id[:8]}... '
            f'({box_x:.3f},{box_y:.3f},{box_w:.3f},{box_h:.3f}) -> '
            f'({new_x:.3f},{new_y:.3f},{new_w:.3f},{new_h:.3f})'
        )
        conn.execute(
            """UPDATE faces SET box_x = ?, box_y = ?, box_w = ?, box_h = ?,
            updated_at = datetime('now') WHERE id = ?""",
            (new_x, new_y, new_w, new_h, face_id),
        )
        updated_count += 1

    conn.commit()
    logger.debug(f'rotate_faces_for_image: Committed {updated_count} bbox updates')
    return updated_count


def has_faces_detected(
    conn: SafeConnection,
    image_id: str,
) -> bool:
    """Check if an image has had face detection run.

    Returns True if any faces (including suppressed) exist for the image,
    indicating face detection has been performed.

    Args:
        conn: Database connection.
        image_id: Image's UUID.

    Returns:
        True if face detection has been run on this image.
    """
    cursor = conn.execute("""SELECT 1 FROM faces WHERE image_id = ? LIMIT 1""", (image_id,))
    return cursor.fetchone() is not None


# =============================================================================
# AUTO-RECOGNITION
# =============================================================================


def find_best_match(
    embedding: np.ndarray,
    known_embeddings: list[tuple[str, str, np.ndarray]],
    threshold: float = FACE_RECOGNITION_DEFAULT_THRESHOLD,
    person_thresholds: dict[str, float | None] | None = None,
    ignored_person_ids: set[str] | None = None,
) -> tuple[str, str, float] | None:
    """Find the best matching person for a face embedding.

    Compares the embedding against all known face embeddings, groups by
    person (keeping the best face per person), and returns the first
    person whose per-person threshold is met.  This ensures that a person
    with a low custom threshold can match even if a different person has
    a slightly higher raw similarity but a stricter threshold.

    Named people are always tried before ignored people (name == '-').
    The '-' person is a holding pen for unsorted faces and may contain
    faces of multiple real people, so a named match is always preferred.
    Ignored people are only matched as a fallback when no named person
    meets their threshold.

    Args:
        embedding: 512D face embedding to match.
        known_embeddings: List of (face_id, person_id, embedding) tuples.
        threshold: Default minimum cosine similarity for a match.
        person_thresholds: Optional dict mapping person_id to their custom
            recognition threshold.  ``None`` values mean use the default.
        ignored_person_ids: Optional set of person IDs whose name is '-'.
            These are tried only after all named people fail to match.

    Returns:
        Tuple of (face_id, person_id, similarity) for best match,
        or None if no match above threshold.
    """
    if not known_embeddings:
        return None

    # Ensure input embedding is normalised (guard against zero-norm)
    emb_norm = np.linalg.norm(embedding)
    if emb_norm == 0:
        return None  # Zero-norm embedding cannot produce meaningful matches
    if not np.isclose(emb_norm, 1.0, atol=0.01):
        embedding = embedding / emb_norm

    # Vectorised similarity computation — single matrix multiply releases
    # the GIL for the entire batch instead of holding it per-face in a
    # Python loop.
    face_ids_list = []
    person_ids_list = []
    emb_list = []
    for face_id, person_id, known_embedding in known_embeddings:
        known_norm = np.linalg.norm(known_embedding)
        if known_norm == 0:
            continue
        if not np.isclose(known_norm, 1.0, atol=0.01):
            known_embedding = known_embedding / known_norm
        face_ids_list.append(face_id)
        person_ids_list.append(person_id)
        emb_list.append(known_embedding)

    if not emb_list:
        return None

    # (num_known, 512) @ (512,) → (num_known,) — single GIL-releasing call
    known_matrix = np.vstack(emb_list)
    similarities = known_matrix @ embedding

    # Group by person, keeping best similarity per person
    person_best: dict[str, tuple[str, float]] = {}  # person_id -> (face_id, similarity)
    for idx, sim_val in enumerate(similarities):
        pid = person_ids_list[idx]
        fid = face_ids_list[idx]
        sim_f = float(sim_val)
        if pid not in person_best or sim_f > person_best[pid][1]:
            person_best[pid] = (fid, sim_f)

    # Sort persons by similarity descending
    sorted_persons = sorted(person_best.items(), key=lambda x: x[1][1], reverse=True)

    # Partition into named and ignored — named people are always tried
    # first so that a real person match takes priority over the '-'
    # holding pen, even if '-' has a slightly higher raw similarity.
    _ignored = ignored_person_ids or set()
    named = [(pid, data) for pid, data in sorted_persons if pid not in _ignored]
    ignored = [(pid, data) for pid, data in sorted_persons if pid in _ignored]

    for person_id, (face_id, similarity) in named + ignored:
        eff_threshold = threshold
        if person_thresholds:
            pt = person_thresholds.get(person_id)
            if pt is not None:
                eff_threshold = pt
        if similarity >= eff_threshold:
            return (face_id, person_id, similarity)

    # No match — log the best similarity for diagnostics
    if sorted_persons:
        best_pid = sorted_persons[0][0]
        best_sim = sorted_persons[0][1][1]
        eff = threshold
        if person_thresholds:
            pt = person_thresholds.get(best_pid)
            if pt is not None:
                eff = pt
        logger.debug(
            f'No face match (best person similarity: {best_sim:.3f}, '
            f'effective threshold: {eff:.3f}, gap: {eff - best_sim:.3f})'
        )

    return None


# =============================================================================
# IMAGE QUERIES WITH PEOPLE FILTER
# =============================================================================


def get_images_with_people(
    conn: SafeConnection,
    person_ids: list[str],
) -> list[str]:
    """Get image IDs containing ALL specified people.

    Args:
        conn: Database connection.
        person_ids: List of person UUIDs (AND logic).

    Returns:
        List of image IDs.
    """
    if not person_ids:
        return []

    # Build query to find images containing ALL specified people
    # Using INTERSECT to implement AND logic
    queries = []
    params = []
    for person_id in person_ids:
        queries.append("""
            SELECT DISTINCT image_id FROM faces
            WHERE person_id = ? AND suppressed = 0
        """)
        params.append(person_id)

    query = ' INTERSECT '.join(queries)
    cursor = conn.execute(query, params)
    return [row['image_id'] for row in cursor.fetchall()]


def get_people_names_bulk(
    conn: SafeConnection,
) -> dict[str, str]:
    """Get people names for all images in a single query.

    Used for "sort by people" functionality - returns a mapping of
    image_id to comma-separated names string.

    Args:
        conn: Database connection.

    Returns:
        Dict mapping image_id to comma-separated people names (sorted alphabetically).
    """
    cursor = conn.execute("""
        SELECT f.image_id, GROUP_CONCAT(DISTINCT p.name) as names
        FROM faces f
        JOIN people p ON f.person_id = p.id
        WHERE f.suppressed = 0
        GROUP BY f.image_id
    """)
    result = {}
    for row in cursor.fetchall():
        # Sort the names alphabetically (GROUP_CONCAT doesn't guarantee order)
        names = row['names'].split(',') if row['names'] else []
        names.sort(key=lambda n: n.lower())
        result[row['image_id']] = ', '.join(names)
    return result


# =============================================================================
# BATCH IDENTIFICATION AND AUTO-REASSESSMENT
# =============================================================================

# Global cache for embeddings (populated on demand)
_embedding_cache = {
    'known': None,  # List of (face_id, person_id, embedding)
    'unknown': None,  # List of (face_id, embedding)
    'lock': threading.Lock(),
    'valid': False,
}


def invalidate_embedding_cache() -> None:
    """Invalidate the embedding cache after changes."""
    with _embedding_cache['lock']:
        _embedding_cache['known'] = None
        _embedding_cache['unknown'] = None
        _embedding_cache['valid'] = False


def get_cached_known_embeddings(
    conn: SafeConnection,
) -> list[tuple[str, str, np.ndarray]]:
    """Get known face embeddings with RAM caching.

    Args:
        conn: Database connection.

    Returns:
        List of (face_id, person_id, embedding) tuples.
    """
    with _embedding_cache['lock']:
        if _embedding_cache['known'] is None:
            _embedding_cache['known'] = get_all_known_face_embeddings(conn)
        return _embedding_cache['known']


def batch_identify_faces(
    conn: SafeConnection,
    face_ids: list[str],
    name: str,
    preferred_face_id: str | None = None,
) -> dict:
    """Identify multiple faces with the same name in a single operation.

    Creates or finds the person, links all faces, and sets preferred face.

    Args:
        conn: Database connection.
        face_ids: List of face UUIDs to identify.
        name: Name for the person.
        preferred_face_id: Face ID to set as preferred (optional).

    Returns:
        Dict with 'person' and 'faces' (list of updated face IDs).
    """
    if not face_ids or not name:
        return {'person': None, 'faces': []}

    # Find or create person
    person = get_person_by_name(conn, name)
    if person is None:
        person_id = create_person(conn, name)
        person = get_person(conn, person_id)
    else:
        person_id = person['id']

    # Update all faces with person_id (manually tagged since user initiated)
    updated_faces = []
    for face_id in face_ids:
        face = get_face(conn, face_id)
        if face is not None:
            update_face_person(conn, face_id, person_id, manually_tagged=True)
            updated_faces.append(face_id)

    # Set preferred face if specified and person doesn't have one
    if preferred_face_id and preferred_face_id in updated_faces:  # noqa: SIM102
        if not person.get('preferred_face_id'):
            update_person(conn, person_id, preferred_face_id=preferred_face_id)
            person = get_person(conn, person_id)

    # Invalidate cache since we modified faces
    invalidate_embedding_cache()

    return {
        'person': person,
        'faces': updated_faces,
    }


def reassess_unknown_faces(
    conn: SafeConnection,
    db_lock: threading.Lock,
    threshold: float = FACE_RECOGNITION_DEFAULT_THRESHOLD,
    person_id: str | None = None,
) -> list[tuple[str, str, float]]:
    """Re-assess all unknown faces against known embeddings.

    Uses vectorized numpy operations for fast comparison.
    Supports per-person recognition thresholds (overrides global threshold).

    Uses READ → COMPUTE → WRITE pattern to minimise db_lock hold time:
    the lock is only held during DB reads and the final batched write,
    not during the matrix multiplication and matching loop.

    Args:
        conn: Database connection.
        db_lock: The database lock for thread safety.
        threshold: Default minimum cosine similarity for auto-match.
        person_id: If specified, only compare against this person's faces.

    Returns:
        List of (face_id, person_id, similarity) for matched faces.
    """
    logger.debug(f'Reassessing unknown faces with default threshold={threshold:.3f}, person_id={person_id}')

    # ── READ phase (lock): load all data needed for computation ──
    with db_lock:
        # Load per-person thresholds and identify ignored people (name == '-')
        person_thresholds: dict[str, float | None] = {}
        ignored_person_ids: set[str] = set()
        cursor = conn.execute('SELECT id, name, recognition_threshold FROM people')
        for row in cursor.fetchall():
            person_thresholds[row['id']] = row['recognition_threshold']
            if row['name'] == '-':
                ignored_person_ids.add(row['id'])

        # Get known embeddings
        if person_id:
            cursor = conn.execute(
                """SELECT f.id, f.person_id, f.embedding
                   FROM faces f
                   JOIN images i ON f.image_id = i.id
                   WHERE f.person_id = ? AND f.suppressed = 0 AND i.deleted = 0""",
                (person_id,),
            )
            known_embeddings = []
            for row in cursor.fetchall():
                embedding = np.frombuffer(row['embedding'], dtype=np.float32)
                known_embeddings.append((row['id'], row['person_id'], embedding))
        else:
            known_embeddings = get_cached_known_embeddings(conn)

        # Get candidate embeddings: unknown faces AND unlocked faces
        # This allows faces to be reassigned to better-matching people
        cursor = conn.execute(
            """SELECT f.id, f.embedding, f.person_id
               FROM faces f
               JOIN images i ON f.image_id = i.id
               WHERE (f.person_id IS NULL OR f.manually_tagged = 0)
                 AND f.suppressed = 0 AND f.embedding IS NOT NULL
                 AND i.deleted = 0"""
        )
        candidate_embeddings = []
        candidate_person_ids: dict[str, str | None] = {}  # face_id -> current person_id
        for row in cursor.fetchall():
            embedding = np.frombuffer(row['embedding'], dtype=np.float32)
            candidate_embeddings.append((row['id'], embedding))
            candidate_person_ids[row['id']] = row['person_id']

    if not known_embeddings or not candidate_embeddings:
        return []

    # ── COMPUTE phase (no lock): matrix operations and matching ──

    # Diagnostic: check embedding health (DEBUG level)
    def diagnose_embeddings(name, embeddings_list):
        """Check if embeddings are valid and diverse."""
        if not embeddings_list:
            logger.debug(f'{name}: no embeddings')
            return

        # Get just the embedding arrays
        if len(embeddings_list[0]) == 3:  # (face_id, person_id, embedding)
            embs = [e[2] for e in embeddings_list]
        else:  # (face_id, embedding)
            embs = [e[1] for e in embeddings_list]

        emb_matrix = np.vstack(embs)

        # Check shape
        logger.debug(f'{name}: {len(embs)} embeddings, shape {emb_matrix.shape}')

        # Check for zeros/constants
        std_vals = np.std(emb_matrix, axis=0)
        overall_std = np.std(emb_matrix)

        logger.debug(
            f'{name}: overall std={overall_std:.6f}, per-dim std range=[{std_vals.min():.6f}, {std_vals.max():.6f}]'
        )

        # Check pairwise similarity of first few
        if len(embs) >= 2:
            sample_size = min(5, len(embs))
            sample = emb_matrix[:sample_size]
            pairwise = sample @ sample.T
            # Get off-diagonal similarities
            off_diag = pairwise[np.triu_indices(sample_size, k=1)]
            logger.debug(f'{name}: sample pairwise similarities (should vary): {off_diag}')

    diagnose_embeddings('Known', known_embeddings)
    diagnose_embeddings('Candidates (sample)', candidate_embeddings[:100])  # Sample to avoid log spam

    # Build matrices for vectorized comparison
    # known_matrix: (num_known, 512)
    # candidate_matrix: (num_candidates, 512)
    known_ids = [(fid, pid) for fid, pid, _ in known_embeddings]
    known_matrix = np.vstack([emb for _, _, emb in known_embeddings])

    candidate_ids = [fid for fid, _ in candidate_embeddings]
    candidate_matrix = np.vstack([emb for _, emb in candidate_embeddings])

    # Verify embeddings are L2-normalised (norms should be ~1.0)
    known_norms = np.linalg.norm(known_matrix, axis=1)
    candidate_norms = np.linalg.norm(candidate_matrix, axis=1)
    if not np.allclose(known_norms, 1.0, atol=0.01):
        logger.warning(
            f'Known embeddings not normalised! norms: min={known_norms.min():.3f}, max={known_norms.max():.3f}'
        )
        # Re-normalise (guard against zero-norm from corruption)
        known_norms[known_norms == 0] = 1
        known_matrix = known_matrix / known_norms[:, np.newaxis]
    if not np.allclose(candidate_norms, 1.0, atol=0.01):
        logger.warning(
            f'Candidate embeddings not normalised! norms: '
            f'min={candidate_norms.min():.3f}, max={candidate_norms.max():.3f}'
        )
        # Re-normalise (guard against zero-norm from corruption)
        candidate_norms[candidate_norms == 0] = 1
        candidate_matrix = candidate_matrix / candidate_norms[:, np.newaxis]

    # Compute all similarities at once: (num_candidates, num_known)
    # Embeddings are L2-normalised, so dot product = cosine similarity
    similarities = candidate_matrix @ known_matrix.T

    # Pre-compute person -> known face indices for efficient per-person grouping
    person_face_indices: dict[str, list[int]] = {}
    for j, (_, pid) in enumerate(known_ids):
        person_face_indices.setdefault(pid, []).append(j)

    # Minimum possible threshold across all persons (for early termination)
    custom_thresholds = [pt for pt in person_thresholds.values() if pt is not None]
    min_threshold = min(custom_thresholds) if custom_thresholds else threshold
    min_threshold = min(min_threshold, threshold)

    # Find best match for each candidate face.
    # For each candidate, group similarities by person and try each person
    # in descending order until one meets its per-person threshold.  This
    # prevents a high-threshold person from "blocking" a lower-threshold
    # person who would have matched.
    #
    # Named people are always tried before ignored people (name == '-').
    # The '-' person is a holding pen for unsorted faces, so a real named
    # match is always preferred.  Ignored people are only matched as a
    # fallback when no named person meets their threshold.
    matched = []
    unmatched = []  # Faces that need to be unassigned (below all thresholds)
    for i, candidate_face_id in enumerate(candidate_ids):
        # Yield GIL periodically so other threads (Flask request handlers)
        # aren't starved during large reassessments.
        if i % 200 == 199:
            time.sleep(0)

        current_person_id = candidate_person_ids.get(candidate_face_id)
        row = similarities[i]

        # Get best similarity per person
        named_best: list[tuple[str, float]] = []
        ignored_best: list[tuple[str, float]] = []
        for pid, indices in person_face_indices.items():
            best_sim = float(np.max(row[indices]))
            if best_sim >= min_threshold:
                if pid in ignored_person_ids:
                    ignored_best.append((pid, best_sim))
                else:
                    named_best.append((pid, best_sim))

        # Sort each group by similarity descending, named first
        named_best.sort(key=lambda x: x[1], reverse=True)
        ignored_best.sort(key=lambda x: x[1], reverse=True)

        # Try named people first, then ignored as fallback
        matched_this = False
        for pid, best_sim in named_best + ignored_best:
            pt = person_thresholds.get(pid)
            eff_threshold = pt if pt is not None else threshold
            if best_sim >= eff_threshold:
                if current_person_id != pid:
                    matched.append((candidate_face_id, pid, best_sim))
                matched_this = True
                break

        if not matched_this and current_person_id is not None:
            unmatched.append(candidate_face_id)

    # Compute overall max similarity for diagnostics (useful when no matches)
    overall_max_similarity = float(np.max(similarities)) if similarities.size > 0 else 0.0

    # Log summary (single INFO line)
    if matched or unmatched:
        log_parts = []
        if matched:
            sims = [m[2] for m in matched]
            log_parts.append(f'matched {len(matched)} (similarity {min(sims):.2f}-{max(sims):.2f})')
        if unmatched:
            log_parts.append(f'unassigned {len(unmatched)}')
        logger.info(
            f'Face reassessment: {", ".join(log_parts)} of {len(candidate_ids)} candidates (threshold={threshold:.2f})'
        )
    else:
        logger.info(
            f'Face reassessment: no matches from {len(candidate_ids)} candidates '
            f'against {len(known_ids)} known faces '
            f'(best similarity: {overall_max_similarity:.3f}, threshold: {threshold:.2f})'
        )

    # ── WRITE phase (lock): apply matches in a single batched transaction ──
    if matched or unmatched:
        # Prepare batched update parameters
        assign_params = [(pid, face_id) for face_id, pid, _ in matched]
        unassign_params = [(face_id,) for face_id in unmatched]

        for face_id, matched_person_id, similarity in matched:
            logger.debug(f'Auto-matched face {face_id} to person {matched_person_id} (similarity: {similarity:.3f})')
        for face_id in unmatched:
            logger.debug(f'Unassigned face {face_id} (below all thresholds)')

        with db_lock:
            conn.executemany(
                "UPDATE faces SET person_id = ?, manually_tagged = 0, updated_at = datetime('now') WHERE id = ?",
                assign_params,
            )
            conn.executemany(
                "UPDATE faces SET person_id = NULL, manually_tagged = 0, updated_at = datetime('now') WHERE id = ?",
                unassign_params,
            )
            conn.commit()

        invalidate_embedding_cache()

    return matched


# Background thread for async reassessment
_reassess_thread: threading.Thread | None = None
_reassess_lock = threading.Lock()
_reassess_result: dict | None = None

# Background thread for unknown face grouping
_grouping_thread: threading.Thread | None = None
_grouping_lock = threading.Lock()
_grouping_status: dict | None = None  # {status: 'idle'|'computing'|'done', progress: 0-100}


# =============================================================================
# UNKNOWN FACE GROUPING
# =============================================================================


def compute_unknown_face_groups(
    conn: SafeConnection,
    threshold: float = FACE_RECOGNITION_DEFAULT_THRESHOLD,
) -> int:
    """Compute similarity groups for unknown faces using UnionFind clustering.

    Unknown faces that are similar to each other (above threshold) are grouped
    together. This helps users identify the same unknown person across images.

    Uses READ → COMPUTE → WRITE pattern to minimise lock hold time:
    the SafeConnection's context manager serialises DB access, while the
    O(n²) chunked similarity computation runs without holding the lock.

    The algorithm:
    1. Load all unknown face embeddings (already L2-normalised)
    2. Compute pairwise cosine similarity using chunked matrix multiplication
    3. Use UnionFind to cluster faces above the similarity threshold
    4. Assign group IDs and update the database

    Args:
        conn: SafeConnection for thread-safe DB access.
        threshold: Minimum cosine similarity to group faces together.

    Returns:
        Number of groups created.
    """
    global _grouping_status

    # Set status to computing
    with _grouping_lock:
        _grouping_status = {'status': 'computing'}

    try:
        return _compute_unknown_face_groups_impl(conn, threshold)
    finally:
        # Clear status when done
        with _grouping_lock:
            _grouping_status = None


def _compute_unknown_face_groups_impl(
    conn: SafeConnection,
    threshold: float,
) -> int:
    """Internal implementation of face grouping.

    Uses READ → COMPUTE → WRITE pattern with SafeConnection locking.
    """
    # ── READ phase (lock): load unknown face embeddings ──
    with conn:
        cursor = conn.execute("""
            SELECT f.id, f.embedding
            FROM faces f
            WHERE f.person_id IS NULL AND f.suppressed = 0
            ORDER BY f.id
        """)

        face_ids = []
        embeddings = []
        for row in cursor:
            face_ids.append(row[0])
            embedding = np.frombuffer(row[1], dtype=np.float32)
            embeddings.append(embedding)

    if not face_ids:
        logger.info('No unknown faces to group')
        return 0

    n_faces = len(face_ids)
    logger.info(f'Computing groups for {n_faces} unknown faces')

    # ── COMPUTE phase (no lock): matrix ops and clustering ──

    # Stack embeddings into a matrix (already L2-normalised)
    try:
        embedding_matrix = np.vstack(embeddings)
    except (MemoryError, RuntimeError):
        logger.error(f'OOM stacking {n_faces} face embeddings for grouping, skipping')
        return 0

    # Use UnionFind in ID mode
    uf = UnionFind(ids=face_ids)

    # Chunked similarity computation (similar to duplicates.py)
    chunk_size = 1000
    n_chunks = (n_faces + chunk_size - 1) // chunk_size
    logger.info(f'Computing pairwise similarities in {n_chunks} chunks...')

    for chunk_idx, i in enumerate(range(0, n_faces, chunk_size)):
        chunk_end = min(i + chunk_size, n_faces)
        chunk = embedding_matrix[i:chunk_end]

        # Progress logging every few chunks
        if chunk_idx % 5 == 0 or chunk_idx == n_chunks - 1:
            logger.info(f'  Processing chunk {chunk_idx + 1}/{n_chunks}...')

        # Compute similarities: chunk @ all.T
        similarities = chunk @ embedding_matrix.T

        # Vectorised: find all pairs above threshold in upper triangle
        # (mirrors the approach in duplicates.py to avoid O(n²) Python loops)
        for local_idx in range(chunk_end - i):
            # Yield GIL periodically so other threads aren't starved
            # during large pairwise comparisons.
            if local_idx % 500 == 499:
                time.sleep(0)

            global_idx = i + local_idx
            # Only check j > global_idx to avoid duplicate pairs
            if global_idx + 1 < n_faces:
                row_sims = similarities[local_idx, global_idx + 1 :]
                matches = np.where(row_sims >= threshold)[0]
                face_id_i = face_ids[global_idx]
                for match_offset in matches:
                    j = global_idx + 1 + match_offset
                    uf.union_ids(face_id_i, face_ids[j])

    # Extract groups and assign group IDs
    logger.info('Extracting groups from UnionFind structure...')
    groups = uf.extract_groups_by_id()
    logger.info(f'Found {len(groups)} distinct clusters, assigning group IDs...')

    # ── WRITE phase (lock): clear old groups and assign new ones ──
    with conn:
        # Clear all existing group IDs first (set updated_at per concurrency contract)
        conn.execute(
            "UPDATE faces SET unknown_group_id = NULL, updated_at = datetime('now') "
            'WHERE person_id IS NULL AND unknown_group_id IS NOT NULL'
        )

        # Assign new group IDs (batch to avoid SQLite variable limit of ~999)
        BATCH_SIZE = 500  # Leave room for the group_id parameter
        n_groups = 0
        for _root_id, members in groups.items():
            if len(members) > 1:
                # Generate a group ID
                group_id = str(uuid.uuid4())[:8]
                n_groups += 1

                # Update faces in batches to avoid "too many SQL variables" error
                for i in range(0, len(members), BATCH_SIZE):
                    batch = members[i : i + BATCH_SIZE]
                    placeholders = sql_placeholders(batch)
                    conn.execute(
                        f'UPDATE faces SET unknown_group_id = ? WHERE id IN ({placeholders})', [group_id] + batch
                    )

        conn.commit()

    logger.info(f'Created {n_groups} unknown face groups')
    return n_groups


def get_group_computation_status() -> dict:
    """Get status of async face grouping.

    Returns:
        Dict with 'status' ('idle', 'computing', 'done', 'error') and related info.
    """
    with _grouping_lock:
        if _grouping_status is None:
            return {'status': 'idle'}
        return _grouping_status.copy()


def search_unknown_faces_semantic(
    conn: SafeConnection,
    query_embedding: np.ndarray,
) -> list[dict]:
    """Search unknown faces by semantic similarity to a query embedding.

    Returns all unknown faces sorted by cosine similarity to the query
    (most similar first). Ignores group-based sorting.

    Args:
        conn: Database connection.
        query_embedding: Normalised query embedding from OpenCLIP.

    Returns:
        List of face dicts with 'similarity' field added.
    """
    cursor = conn.execute("""
        SELECT
            f.id,
            f.image_id,
            f.box_x,
            f.box_y,
            f.box_w,
            f.box_h,
            f.confidence,
            f.unknown_group_id,
            f.created_at,
            f.semantic_embedding,
            i.timestamp as image_timestamp,
            i.basename
        FROM faces f
        JOIN images i ON f.image_id = i.id
        WHERE f.person_id IS NULL
          AND f.suppressed = 0
          AND f.semantic_embedding IS NOT NULL
          AND i.deleted = 0
    """)

    faces = []
    norm = np.linalg.norm(query_embedding)
    if norm == 0:
        return []  # Zero-norm embedding cannot produce meaningful matches
    query_norm = query_embedding / norm

    for row in cursor:
        # Decode semantic embedding
        emb_bytes = row['semantic_embedding']
        emb = np.frombuffer(emb_bytes, dtype=np.float32)

        # Compute cosine similarity (embeddings are normalised)
        similarity = float(np.dot(query_norm, emb))

        faces.append(
            {
                'id': row['id'],
                'image_id': row['image_id'],
                'box_x': row['box_x'],
                'box_y': row['box_y'],
                'box_w': row['box_w'],
                'box_h': row['box_h'],
                'confidence': row['confidence'],
                'unknown_group_id': row['unknown_group_id'],
                'created_at': row['created_at'],
                'image_timestamp': row['image_timestamp'],
                'basename': row['basename'],
                'similarity': similarity,
            }
        )

    # Sort by similarity descending
    faces.sort(key=lambda f: f['similarity'], reverse=True)
    return faces


def get_reassessment_status() -> dict:
    """Get status of async reassessment.

    Returns:
        Dict with 'in_progress' and optionally 'last_result'.
    """
    with _reassess_lock:
        in_progress = _reassess_thread is not None and _reassess_thread.is_alive()
        return {
            'in_progress': in_progress,
            'last_result': _reassess_result,
        }


def clear_reassessment_result() -> None:
    """Clear the last reassessment result.

    Called by frontend after acknowledging a completed reassessment,
    to prevent stale 'completed' status on subsequent polls.
    """
    global _reassess_result
    with _reassess_lock:
        _reassess_result = None


def reassess_unknown_faces_async(
    db: ImageDatabase,
    threshold: float = FACE_RECOGNITION_DEFAULT_THRESHOLD,
    person_id: str | None = None,
    callback: callable | None = None,
) -> None:
    """Re-assess unknown faces in a background thread.

    Uses fine-grained locking to avoid blocking other database operations
    during the CPU-intensive similarity computation phase.

    The operation is split into three phases:
    1. READ (with lock): Fetch embeddings and thresholds from database
    2. COMPUTE (no lock): Matrix multiplication for similarity matching
    3. WRITE (with lock): Update matched faces and build response

    Args:
        db: ImageDatabase instance (provides conn and _db_lock).
        threshold: Minimum cosine similarity for auto-match.
        person_id: If specified, only compare against this person's faces.
        callback: Optional callback(matched_count) when done.
    """
    global _reassess_thread, _reassess_result

    def _worker():
        global _reassess_thread, _reassess_result
        try:
            # ================================================================
            # PHASE 1: READ (with lock) - fetch all data needed for computation
            # ================================================================
            with db._db_lock:
                logger.debug('Async reassessment: READ phase started')

                # Load per-person thresholds
                person_thresholds: dict[str, float | None] = {}
                cursor = db.safe_conn.execute('SELECT id, recognition_threshold FROM people')
                for row in cursor.fetchall():
                    person_thresholds[row['id']] = row['recognition_threshold']

                # Get known embeddings
                if person_id:
                    cursor = db.safe_conn.execute(
                        """SELECT f.id, f.person_id, f.embedding
                           FROM faces f
                           JOIN images i ON f.image_id = i.id
                           WHERE f.person_id = ? AND f.suppressed = 0
                             AND i.deleted = 0""",
                        (person_id,),
                    )
                    known_embeddings = []
                    for row in cursor.fetchall():
                        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
                        known_embeddings.append((row['id'], row['person_id'], embedding))
                else:
                    known_embeddings = get_cached_known_embeddings(db.safe_conn)

                # Get candidate embeddings WITH updated_at for optimistic concurrency
                # If updated_at changes between READ and WRITE, we skip that face
                #
                # Candidates include:
                # - Unknown faces (person_id IS NULL)
                # - Unlocked faces from OTHER people (person_id != target, manually_tagged = 0)
                # This allows threshold changes to pull faces from other people if they
                # match better. Locked faces (manually_tagged = 1) are never candidates.
                if person_id:
                    cursor = db.safe_conn.execute(
                        """SELECT f.id, f.embedding, f.updated_at, f.person_id
                           FROM faces f
                           JOIN images i ON f.image_id = i.id
                           WHERE (f.person_id IS NULL OR (f.person_id != ? AND f.manually_tagged = 0))
                             AND f.suppressed = 0 AND f.embedding IS NOT NULL
                             AND i.deleted = 0""",
                        (person_id,),
                    )
                else:
                    # Full sweep reassessment: unknown faces AND unlocked faces
                    # This allows faces to be reassigned to better-matching people
                    # or ejected to unknown if they no longer meet any threshold
                    cursor = db.safe_conn.execute(
                        """SELECT f.id, f.embedding, f.updated_at, f.person_id
                           FROM faces f
                           JOIN images i ON f.image_id = i.id
                           WHERE (f.person_id IS NULL OR f.manually_tagged = 0)
                             AND f.suppressed = 0 AND f.embedding IS NOT NULL
                             AND i.deleted = 0"""
                    )
                candidate_embeddings = []
                face_timestamps: dict[str, str | None] = {}  # face_id -> updated_at
                candidate_person_ids: dict[str, str | None] = {}  # face_id -> current person_id
                for row in cursor.fetchall():
                    embedding = np.frombuffer(row['embedding'], dtype=np.float32)
                    candidate_embeddings.append((row['id'], embedding))
                    face_timestamps[row['id']] = row['updated_at']
                    candidate_person_ids[row['id']] = row['person_id']

                logger.debug(
                    f'Async reassessment: READ phase done - '
                    f'{len(known_embeddings)} known, {len(candidate_embeddings)} candidates'
                )

            # Early exit if nothing to compare
            if not known_embeddings or not candidate_embeddings:
                logger.debug('Async reassessment: no embeddings to compare')
                with _reassess_lock:
                    _reassess_result = {'matched_count': 0, 'unassigned_count': 0, 'person_id': person_id}
                if callback:
                    callback(0)
                return

            # ================================================================
            # PHASE 2: COMPUTE (no lock) - CPU-intensive similarity matching
            # ================================================================
            logger.debug('Async reassessment: COMPUTE phase started (lock released)')

            # Build matrices for vectorized comparison
            known_ids = [(fid, pid) for fid, pid, _ in known_embeddings]
            known_matrix = np.vstack([emb for _, _, emb in known_embeddings])

            candidate_ids = [fid for fid, _ in candidate_embeddings]
            candidate_matrix = np.vstack([emb for _, emb in candidate_embeddings])

            # Ensure L2-normalised (guard against zero-norm from corruption)
            known_norms = np.linalg.norm(known_matrix, axis=1)
            candidate_norms = np.linalg.norm(candidate_matrix, axis=1)
            if not np.allclose(known_norms, 1.0, atol=0.01):
                known_norms[known_norms == 0] = 1
                known_matrix = known_matrix / known_norms[:, np.newaxis]
            if not np.allclose(candidate_norms, 1.0, atol=0.01):
                candidate_norms[candidate_norms == 0] = 1
                candidate_matrix = candidate_matrix / candidate_norms[:, np.newaxis]

            # Compute all similarities at once: (num_candidates, num_known)
            _compute_start = time.time()  # [PERF-LOG]
            similarities = candidate_matrix @ known_matrix.T

            # Vectorized best-match finding.  Replaces a pure-Python loop over
            # all candidates (50K+ at scale) with bulk numpy operations that
            # release the GIL during computation.
            n_candidates = len(candidate_ids)
            n_known = len(known_ids)

            # Best match index and score for every candidate (one numpy call)
            best_indices = np.argmax(similarities, axis=1)  # shape: (N,)
            best_scores = similarities[np.arange(n_candidates), best_indices]  # shape: (N,)

            # Build per-known-face threshold vector (one entry per column in
            # similarities).  Looked up from person_thresholds dict, falling
            # back to global threshold.  Built once, O(M) where M = known faces.
            known_thresholds = np.array(
                [threshold if person_thresholds.get(pid) is None else person_thresholds[pid] for _, pid in known_ids],
                dtype=np.float32,
            )  # shape: (M,)

            # Look up effective threshold for each candidate's best match
            effective_thresholds = known_thresholds[best_indices]  # shape: (N,) — numpy fancy indexing

            # Filter: which candidates beat their threshold?
            above = best_scores >= effective_thresholds
            match_indices = np.where(above)[0]

            matched = []
            for i in match_indices:
                face_id = candidate_ids[i]
                new_person_id = known_ids[best_indices[i]][1]
                current_person_id = candidate_person_ids.get(face_id)
                # Skip if already assigned to this person (no change needed)
                if current_person_id == new_person_id:
                    continue
                matched.append((face_id, new_person_id, float(best_scores[i])))

            # Build list of faces that need to be unassigned (currently assigned but
            # no longer meet any threshold). Only applicable for full sweep.
            unmatched = []
            if not person_id:  # Full sweep mode
                below = ~above
                below_indices = np.where(below)[0]
                for i in below_indices:
                    face_id = candidate_ids[i]
                    current_person_id = candidate_person_ids.get(face_id)
                    # Only unassign if currently assigned to someone
                    if current_person_id is not None:
                        unmatched.append(face_id)

            # [PERF-LOG] COMPUTE phase timing and stats
            _compute_elapsed = time.time() - _compute_start
            logger.info(
                f'[PERF] Reassessment COMPUTE: {_compute_elapsed:.2f}s '
                f'({n_candidates} candidates × {n_known} known, '
                f'{len(matched)} matches)'
            )

            logger.debug(
                f'Async reassessment: COMPUTE phase done - {len(matched)} matches from {len(candidate_ids)} candidates'
            )

            # ================================================================
            # PHASE 3: WRITE (with lock) - persist matches and build response
            # ================================================================
            # Use optimistic concurrency: only update faces whose updated_at
            # hasn't changed since READ phase. If the user (or another process)
            # modified a face, we skip it rather than overwriting their change.
            with db._db_lock:
                logger.debug('Async reassessment: WRITE phase started')

                actually_updated = []
                skipped_modified = 0

                for face_id, matched_person_id, similarity in matched:
                    original_timestamp = face_timestamps.get(face_id)

                    # Conditional update: only if updated_at hasn't changed
                    # This handles all cases: suppressed, identified, deleted, etc.
                    if original_timestamp is not None:
                        cursor = db.safe_conn.execute(
                            """UPDATE faces
                               SET person_id = ?, manually_tagged = 0, updated_at = datetime('now')
                               WHERE id = ? AND updated_at = ?""",
                            (matched_person_id, face_id, original_timestamp),
                        )
                    else:
                        # No timestamp (legacy row) - fall back to checking not locked
                        cursor = db.safe_conn.execute(
                            """UPDATE faces
                               SET person_id = ?, manually_tagged = 0, updated_at = datetime('now')
                               WHERE id = ? AND manually_tagged = 0 AND suppressed = 0""",
                            (matched_person_id, face_id),
                        )

                    if cursor.rowcount > 0:
                        logger.debug(
                            f'Auto-matched face {face_id} to person {matched_person_id} (similarity: {similarity:.3f})'
                        )
                        actually_updated.append((face_id, matched_person_id, similarity))
                    else:
                        skipped_modified += 1

                # Unassign faces that no longer meet any threshold (full sweep only)
                actually_unassigned = []
                for face_id in unmatched:
                    original_timestamp = face_timestamps.get(face_id)

                    if original_timestamp is not None:
                        cursor = db.safe_conn.execute(
                            """UPDATE faces
                               SET person_id = NULL, manually_tagged = 0, updated_at = datetime('now')
                               WHERE id = ? AND updated_at = ?""",
                            (face_id, original_timestamp),
                        )
                    else:
                        cursor = db.safe_conn.execute(
                            """UPDATE faces
                               SET person_id = NULL, manually_tagged = 0, updated_at = datetime('now')
                               WHERE id = ? AND manually_tagged = 0 AND suppressed = 0""",
                            (face_id,),
                        )

                    if cursor.rowcount > 0:
                        logger.debug(f'Unassigned face {face_id} (below all thresholds)')
                        actually_unassigned.append(face_id)
                    else:
                        skipped_modified += 1

                db.safe_conn.commit()

                if skipped_modified:
                    logger.debug(f'Async reassessment: skipped {skipped_modified} faces (modified since READ)')

                # Invalidate cache if we made changes
                if actually_updated or actually_unassigned:
                    invalidate_embedding_cache()

                # Build updated_faces list for frontend event (only actually updated faces)
                updated_faces = []
                if actually_updated:
                    person_ids_list = list(set(m[1] for m in actually_updated))
                    placeholders = sql_placeholders(person_ids_list)
                    cursor = db.safe_conn.execute(
                        f'SELECT id, name FROM people WHERE id IN ({placeholders})', person_ids_list
                    )
                    person_names = {row['id']: row['name'] for row in cursor.fetchall()}

                    for face_id, pid, _similarity in actually_updated:
                        updated_faces.append(
                            {
                                'face_id': face_id,
                                'person_id': pid,
                                'person_name': person_names.get(pid, ''),
                            }
                        )

                # Add unassigned faces to the event (person_id = None)
                for face_id in actually_unassigned:
                    updated_faces.append(
                        {
                            'face_id': face_id,
                            'person_id': None,
                            'person_name': None,
                        }
                    )

                logger.debug('Async reassessment: WRITE phase done')

            # Update matched to reflect what was actually updated (for logging/callback)
            matched = actually_updated

            # Log summary
            if matched or actually_unassigned:
                log_parts = []
                if matched:
                    sims = [m[2] for m in matched]
                    log_parts.append(f'matched {len(matched)} (similarity {min(sims):.2f}-{max(sims):.2f})')
                if actually_unassigned:
                    log_parts.append(f'unassigned {len(actually_unassigned)}')
                logger.info(
                    f'Async face reassessment: {", ".join(log_parts)} '
                    f'of {len(candidate_ids)} candidates (threshold={threshold:.2f})'
                )

            # Store result
            with _reassess_lock:
                _reassess_result = {
                    'matched_count': len(matched),
                    'unassigned_count': len(actually_unassigned),
                    'person_id': person_id,
                }

            logger.debug(f'Async reassessment complete: {len(matched)} matched, {len(actually_unassigned)} unassigned')

            # Emit event so frontend can update
            if hasattr(db, 'event_queue') and db.event_queue:
                db.event_queue.emit(
                    'faces_reassessed',
                    {
                        'matched_count': len(matched),
                        'unassigned_count': len(actually_unassigned),
                        'person_id': person_id,
                        'updated_faces': updated_faces,
                    },
                )

            if callback:
                callback(len(matched))
        except Exception as e:
            logger.error(f'Async reassessment failed: {e}')
            with _reassess_lock:
                _reassess_result = {'error': str(e)}
        finally:
            with _reassess_lock:
                _reassess_thread = None

    with _reassess_lock:
        # Only start if not already running
        if _reassess_thread is None or not _reassess_thread.is_alive():
            _reassess_result = None  # Clear previous result
            _reassess_thread = threading.Thread(target=_worker, daemon=True)
            _reassess_thread.start()
            logger.debug('Started async face reassessment')
        else:
            logger.debug('Reassessment already in progress, skipping')
