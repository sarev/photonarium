"""
Face detection and recognition for the Imaginary image database.

This module provides face detection using MTCNN and face embeddings using
InceptionResnetV1 from the facenet-pytorch library. It handles:

1. Face detection in images with bounding boxes
2. 512D face embedding generation for recognition
3. Database schema and CRUD operations for people and faces
4. Auto-recognition by matching new faces against known people
5. Face thumbnail generation (200x200 crops from full images)

The face detection pipeline integrates with the existing image indexing
process and runs as an optional phase after OpenCLIP embedding generation.

Usage:
    from faces import FaceDetector, init_face_tables

    # Initialize database tables
    init_face_tables(conn)

    # Create detector (lazy loads models on first use)
    detector = FaceDetector(config)

    # Detect faces in an image
    faces = detector.detect_faces(image_path)

    # Generate embedding for a detected face
    embedding = detector.get_face_embedding(image_path, face_box)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter
from typing import Any, TYPE_CHECKING

import logging
import numpy as np
import sqlite3
import threading
import time
import uuid

from duplicates import UnionFind

if TYPE_CHECKING:
    from imagedb import ImageDatabase

# Configure module logger
logger = logging.getLogger(__name__)


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
    "CREATE INDEX IF NOT EXISTS idx_faces_image ON faces(image_id)",
    "CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_faces_suppressed ON faces(suppressed)",
    "CREATE INDEX IF NOT EXISTS idx_people_name ON people(name COLLATE NOCASE)",
]

# Migrations for schema updates
_MIGRATIONS = [
    # Add unknown_group_id column for grouping similar unknown faces
    ("faces", "unknown_group_id", "ALTER TABLE faces ADD COLUMN unknown_group_id TEXT"),
    # Add per-person recognition threshold (NULL = use global default)
    ("people", "recognition_threshold", "ALTER TABLE people ADD COLUMN recognition_threshold REAL"),
]


def init_face_tables(conn: sqlite3.Connection) -> None:
    """Initialize the face recognition database tables.

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
        except sqlite3.OperationalError:
            # Index already exists
            pass

    # Run migrations for schema updates
    _run_migrations(conn)

    conn.commit()
    logger.info('Face recognition tables initialized')


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Run pending schema migrations.

    Args:
        conn: Database connection.
    """
    for table, column, sql in _MIGRATIONS:
        # Check if column exists
        cursor = conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if column not in columns:
            try:
                conn.execute(sql)
                logger.info(f"Migration: added {table}.{column}")
            except sqlite3.OperationalError as e:
                logger.warning(f"Migration failed for {table}.{column}: {e}")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DetectedFace:
    """A face detected in an image.

    Attributes:
        box_x: Normalized x coordinate of bounding box (0-1).
        box_y: Normalized y coordinate of bounding box (0-1).
        box_w: Normalized width of bounding box (0-1).
        box_h: Normalized height of bounding box (0-1).
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
        min_confidence: float = 0.95,
        min_face_size: int = 40,
        device: str | None = None,
    ):
        """Initialize the face detector.

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
        self._device = device
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        """Get the PyTorch device, auto-detecting if not set."""
        if self._device is None:
            import torch
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
            logger.info(f'Face detector using device: {self._device}')
        return self._device

    @property
    def mtcnn(self):
        """Get the MTCNN face detector, loading if necessary."""
        if self._mtcnn is None:
            with self._lock:
                if self._mtcnn is None:
                    logger.info('Loading MTCNN face detector...')
                    from facenet_pytorch import MTCNN
                    self._mtcnn = MTCNN(
                        keep_all=True,
                        device=self.device,
                        min_face_size=self.min_face_size,
                        thresholds=[0.6, 0.7, 0.7],  # Default MTCNN thresholds
                        post_process=True,  # Apply standardization for ResNet input
                    )
                    logger.info('MTCNN loaded')
        return self._mtcnn

    @property
    def resnet(self):
        """Get the InceptionResnetV1 model, loading if necessary."""
        if self._resnet is None:
            with self._lock:
                if self._resnet is None:
                    logger.info('Loading InceptionResnetV1 for face embeddings...')
                    from facenet_pytorch import InceptionResnetV1
                    self._resnet = InceptionResnetV1(
                        pretrained='vggface2',
                        device=self.device,
                    ).eval()
                    logger.info('InceptionResnetV1 loaded')
        return self._resnet

    def detect_faces(
        self,
        image_path: Path | str,
        max_dimension: int = 4096,
    ) -> list[DetectedFace]:
        """Detect faces in an image.

        Args:
            image_path: Path to the image file.
            max_dimension: Maximum image dimension for processing.
                Larger images are downscaled to improve performance.

        Returns:
            List of DetectedFace objects with normalized bounding boxes
            and embeddings. Returns empty list if no faces detected or
            on error.
        """
        import torch

        image_path = Path(image_path)

        try:
            # Load and preprocess image
            with Image.open(image_path) as img:
                # Handle EXIF orientation
                img = ImageOps.exif_transpose(img)

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
                valid_mask = [
                    prob is not None and prob >= self.min_confidence
                    for prob in probs
                ]
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

                    # Convert box from pixels to normalized coordinates (0-1)
                    # MTCNN returns [x1, y1, x2, y2] format
                    x1, y1, x2, y2 = box

                    # Make box square (use larger dimension)
                    box_width = x2 - x1
                    box_height = y2 - y1
                    box_size = max(box_width, box_height)

                    # Center the square box
                    center_x = (x1 + x2) / 2
                    center_y = (y1 + y2) / 2

                    # Calculate square box coordinates
                    sq_x1 = center_x - box_size / 2
                    sq_y1 = center_y - box_size / 2

                    # Normalize to 0-1 (relative to processed image size)
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
                    face_pixels = box_size / scale if scale != 1.0 else box_size
                    if face_pixels < self.min_face_size:
                        logger.debug(f'Skipping small face: {face_pixels:.0f}px')
                        continue

                    valid_faces.append((
                        i,  # tensor index
                        (norm_x, norm_y, norm_w, norm_h),  # normalized box
                        float(prob),  # confidence
                    ))

                if not valid_faces:
                    return []

                # Batch compute embeddings for all valid faces at once
                tensor_indices = [vf[0] for vf in valid_faces]
                batch_tensor = faces_tensor[tensor_indices].to(self.device)
                # Note: MTCNN with post_process=True already standardizes to [-1, 1] for ResNet

                with torch.no_grad():
                    embeddings_batch = self.resnet(batch_tensor)
                    embeddings_batch = embeddings_batch.cpu().numpy()

                # Normalize all embeddings (L2 normalization for cosine similarity)
                norms = np.linalg.norm(embeddings_batch, axis=1, keepdims=True)
                norms[norms == 0] = 1  # Avoid division by zero
                embeddings_batch = embeddings_batch / norms

                # Build detected faces list
                detected_faces = []
                for idx, (_, norm_box, confidence) in enumerate(valid_faces):
                    norm_x, norm_y, norm_w, norm_h = norm_box
                    detected_faces.append(DetectedFace(
                        box_x=float(norm_x),
                        box_y=float(norm_y),
                        box_w=float(norm_w),
                        box_h=float(norm_h),
                        confidence=confidence,
                        embedding=embeddings_batch[idx],
                    ))

                logger.debug(f'Detected {len(detected_faces)} faces in {image_path.name}')
                return detected_faces

        except Exception as e:
            logger.error(f'Face detection failed for {image_path}: {e}')
            return []

    def detect_faces_batch(
        self,
        image_paths: list[Path | str],
        max_dimension: int = 4096,
        num_workers: int = 4,
        stop_event: threading.Event | None = None,
    ) -> dict[Path, list[DetectedFace]]:
        """Detect faces in multiple images using GPU batch processing.

        This method loads images in parallel on CPU, groups them by dimension
        (MTCNN requires equal-sized images for batching), then processes each
        group through MTCNN on GPU. Images from the same camera typically share
        dimensions, so this provides good batching in practice.

        Args:
            image_paths: List of paths to image files.
            max_dimension: Maximum image dimension for processing.
            num_workers: Number of parallel workers for image loading.
            stop_event: Optional threading.Event to signal early termination.

        Returns:
            Dict mapping each image path to its list of DetectedFace objects.
            Images that fail to process will have empty lists.
        """
        import torch
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from collections import defaultdict

        if not image_paths:
            return {}

        def should_stop():
            return stop_event is not None and stop_event.is_set()

        results: dict[Path, list[DetectedFace]] = {}

        # Initialize results with empty lists
        for p in image_paths:
            results[Path(p)] = []

        # Phase 1: Load and preprocess all images in parallel on CPU
        loaded_images = []  # List of (path, img, scale)

        def load_single_image(image_path: Path | str):
            """Load and preprocess image on CPU."""
            image_path = Path(image_path)
            try:
                with Image.open(image_path) as img:
                    img = ImageOps.exif_transpose(img)
                    if img.mode != 'RGB':
                        img = img.convert('RGB')

                    original_width, original_height = img.size
                    scale = 1.0
                    if max(original_width, original_height) > max_dimension:
                        scale = max_dimension / max(original_width, original_height)
                        new_size = (int(original_width * scale), int(original_height * scale))
                        img = img.resize(new_size, Image.Resampling.LANCZOS)

                    # Return a copy since we're inside a context manager
                    return image_path, img.copy(), scale
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
                valid_mask = [
                    prob is not None and prob >= self.min_confidence
                    for prob in probs
                ]
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

                    center_x = (x1 + x2) / 2
                    center_y = (y1 + y2) / 2
                    sq_x1 = center_x - box_size / 2
                    sq_y1 = center_y - box_size / 2

                    norm_x = max(0.0, min(1.0, sq_x1 / processed_width))
                    norm_y = max(0.0, min(1.0, sq_y1 / processed_height))
                    norm_w = max(0.0, min(1.0 - norm_x, box_size / processed_width))
                    norm_h = max(0.0, min(1.0 - norm_y, box_size / processed_height))

                    face_pixels = box_size / scale if scale != 1.0 else box_size
                    if face_pixels < self.min_face_size:
                        continue

                    # Clone tensor to CPU immediately to avoid keeping GPU tensor alive
                    all_faces_data.append((
                        image_path,
                        faces_tensor[i].clone().cpu(),
                        (norm_x, norm_y, norm_w, norm_h),
                        float(prob),
                    ))

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
        # Tensors are on CPU from Phase 3, move to GPU for ResNet
        all_tensors = torch.stack([fd[1] for fd in all_faces_data]).to(self.device)
        # Note: MTCNN with post_process=True already standardizes to [-1, 1] for ResNet

        with torch.no_grad():
            embeddings_batch = self.resnet(all_tensors)
            embeddings_batch = embeddings_batch.cpu().numpy()

        # Release GPU memory from embedding computation
        del all_tensors
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # Clear CPU tensors from all_faces_data (keep only metadata)
        all_faces_metadata = [
            (fd[0], fd[2], fd[3])  # (image_path, norm_box, confidence)
            for fd in all_faces_data
        ]
        del all_faces_data

        # Normalize embeddings
        norms = np.linalg.norm(embeddings_batch, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings_batch = embeddings_batch / norms

        # Phase 5: Build results dict
        for idx, (image_path, norm_box, confidence) in enumerate(all_faces_metadata):
            norm_x, norm_y, norm_w, norm_h = norm_box
            results[image_path].append(DetectedFace(
                box_x=float(norm_x),
                box_y=float(norm_y),
                box_w=float(norm_w),
                box_h=float(norm_h),
                confidence=confidence,
                embedding=embeddings_batch[idx],
            ))

        return results

    def compute_similarity(
        self,
        embedding1: np.ndarray,
        embedding2: np.ndarray,
    ) -> float:
        """Compute cosine similarity between two face embeddings.

        Args:
            embedding1: First 512D face embedding.
            embedding2: Second 512D face embedding.

        Returns:
            Cosine similarity score (0-1).
        """
        # Embeddings should already be L2-normalized
        return float(np.dot(embedding1, embedding2))


# =============================================================================
# FACE THUMBNAIL GENERATION
# =============================================================================

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
    thumbnail. Applies sharpening to counteract downscale blur.

    Args:
        source_path: Path to the source image.
        dest_path: Path where thumbnail should be saved.
        box_x: Normalized x coordinate of face box (0-1).
        box_y: Normalized y coordinate of face box (0-1).
        box_w: Normalized width of face box (0-1).
        box_h: Normalized height of face box (0-1).
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

        with Image.open(source_path) as img:
            # Handle EXIF orientation
            img = ImageOps.exif_transpose(img)

            # Convert to RGB
            if img.mode != 'RGB':
                img = img.convert('RGB')

            width, height = img.size

            # Convert normalized coordinates to pixels
            px_x = int(box_x * width)
            px_y = int(box_y * height)
            px_w = int(box_w * width)
            px_h = int(box_h * height)

            # Expand crop region slightly for context (10% padding)
            padding = int(max(px_w, px_h) * 0.1)
            px_x = max(0, px_x - padding)
            px_y = max(0, px_y - padding)
            px_w = min(width - px_x, px_w + 2 * padding)
            px_h = min(height - px_y, px_h + 2 * padding)

            # Crop the face region
            face_crop = img.crop((px_x, px_y, px_x + px_w, px_y + px_h))

            # Resize to target size (square)
            face_crop = face_crop.resize((size, size), Image.Resampling.LANCZOS)

            # Apply subtle sharpening
            face_crop = face_crop.filter(
                ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=3)
            )

            # Save as JPEG
            face_crop.save(dest_path, 'JPEG', quality=quality, optimize=True)

        logger.debug(f'Generated face thumbnail: {dest_path}')
        return True

    except Exception as e:
        logger.error(f'Failed to generate face thumbnail for {source_path}: {e}')
        return False


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
    conn: sqlite3.Connection,
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

    conn.execute(
        '''INSERT INTO people (id, name) VALUES (?, ?)''',
        (person_id, name)
    )
    conn.commit()
    logger.debug(f'Created person: {name} ({person_id})')
    return person_id


def get_person(
    conn: sqlite3.Connection,
    person_id: str,
) -> dict[str, Any] | None:
    """Get a person by ID.

    Args:
        conn: Database connection.
        person_id: Person's UUID.

    Returns:
        Person dict or None if not found.
    """
    cursor = conn.execute(
        '''SELECT * FROM people WHERE id = ?''',
        (person_id,)
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_person_by_name(
    conn: sqlite3.Connection,
    name: str,
) -> dict[str, Any] | None:
    """Get a person by name (case-insensitive).

    Args:
        conn: Database connection.
        name: Person's name.

    Returns:
        Person dict or None if not found.
    """
    cursor = conn.execute(
        '''SELECT * FROM people WHERE name = ? COLLATE NOCASE''',
        (name,)
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def get_all_people(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Get all people with their face counts.

    Args:
        conn: Database connection.

    Returns:
        List of person dicts with 'face_count' field.
    """
    cursor = conn.execute('''
        SELECT p.*, COUNT(f.id) as face_count
        FROM people p
        LEFT JOIN faces f ON f.person_id = p.id AND f.suppressed = 0
        GROUP BY p.id
        ORDER BY p.name COLLATE NOCASE
    ''')
    return [dict(row) for row in cursor.fetchall()]


# Sentinel value to distinguish "not passed" from "passed as None"
_NOT_SET = object()


def update_person(
    conn: sqlite3.Connection,
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

    cursor = conn.execute(
        f'''UPDATE people SET {', '.join(updates)} WHERE id = ?''',
        params
    )
    conn.commit()
    return cursor.rowcount > 0


def revalidate_person_faces(
    conn: sqlite3.Connection,
    person_id: str,
    threshold: float,
) -> list[str]:
    """Revalidate faces for a person against a threshold.

    Checks each face's similarity to other faces of the same person.
    Faces that don't meet the threshold are unassigned (ejected to unknown pool).

    Args:
        conn: Database connection.
        person_id: Person's UUID.
        threshold: Minimum similarity threshold.

    Returns:
        List of face IDs that were ejected.
    """
    # Get all faces for this person with embeddings
    cursor = conn.execute(
        '''SELECT id, embedding FROM faces
           WHERE person_id = ? AND suppressed = 0''',
        (person_id,)
    )
    faces = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        faces.append((row['id'], embedding))

    if len(faces) <= 1:
        # Can't eject if only 0 or 1 face - nothing to compare against
        return []

    # Build embedding matrix
    face_ids = [f[0] for f in faces]
    embeddings = np.vstack([f[1] for f in faces])

    # Ensure normalized
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    if not np.allclose(norms, 1.0, atol=0.01):
        embeddings = embeddings / norms

    # Compute pairwise similarities
    similarities = embeddings @ embeddings.T

    # For each face, find max similarity to OTHER faces (exclude self on diagonal)
    np.fill_diagonal(similarities, -1)  # Exclude self-similarity
    max_similarities = np.max(similarities, axis=1)

    # Find faces that don't meet threshold
    ejected_ids = []
    for i, (face_id, max_sim) in enumerate(zip(face_ids, max_similarities)):
        if max_sim < threshold:
            ejected_ids.append(face_id)
            logger.info(
                f'Ejecting face {face_id} from person {person_id}: '
                f'max similarity {max_sim:.3f} < threshold {threshold:.3f}'
            )

    # Unassign ejected faces
    if ejected_ids:
        for face_id in ejected_ids:
            update_face_person(conn, face_id, None)

        # Check if preferred face was ejected - if so, select new preferred
        person = get_person(conn, person_id)
        if person and person.get('preferred_face_id') in ejected_ids:
            # Get remaining faces
            remaining = conn.execute(
                '''SELECT id FROM faces
                   WHERE person_id = ? AND suppressed = 0
                   ORDER BY id''',
                (person_id,)
            ).fetchall()
            if remaining:
                new_preferred = remaining[0]['id']
                conn.execute(
                    'UPDATE people SET preferred_face_id = ? WHERE id = ?',
                    (new_preferred, person_id)
                )
                conn.commit()

        # Invalidate embedding cache since faces moved
        invalidate_embedding_cache()

    return ejected_ids


def delete_person(
    conn: sqlite3.Connection,
    person_id: str,
) -> bool:
    """Delete a person record.

    This also unlinks all faces associated with this person
    (person_id set to NULL via ON DELETE SET NULL).

    Args:
        conn: Database connection.
        person_id: Person's UUID.

    Returns:
        True if deleted, False if person not found.
    """
    cursor = conn.execute(
        '''DELETE FROM people WHERE id = ?''',
        (person_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def search_people(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search people by name (case-insensitive substring match).

    Args:
        conn: Database connection.
        query: Search query.
        limit: Maximum results to return.

    Returns:
        List of matching person dicts.
    """
    cursor = conn.execute(
        '''SELECT * FROM people
           WHERE name LIKE ? COLLATE NOCASE
           ORDER BY name COLLATE NOCASE
           LIMIT ?''',
        (f'%{query}%', limit)
    )
    return [dict(row) for row in cursor.fetchall()]


def delete_people_without_faces(conn: sqlite3.Connection) -> int:
    """Delete all people who have no associated faces.

    Args:
        conn: Database connection.

    Returns:
        Number of people deleted.
    """
    cursor = conn.execute('''
        DELETE FROM people
        WHERE id NOT IN (
            SELECT DISTINCT person_id
            FROM faces
            WHERE person_id IS NOT NULL AND suppressed = 0
        )
    ''')
    conn.commit()
    deleted = cursor.rowcount
    if deleted > 0:
        logger.info(f'Deleted {deleted} people with no faces')
    return deleted


# =============================================================================
# FACES CRUD OPERATIONS
# =============================================================================

def create_face(
    conn: sqlite3.Connection,
    image_id: str,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    embedding: np.ndarray,
    confidence: float | None = None,
    person_id: str | None = None,
    face_id: str | None = None,
) -> str:
    """Create a new face record.

    Args:
        conn: Database connection.
        image_id: ID of the image containing this face.
        box_x: Normalized x coordinate of bounding box.
        box_y: Normalized y coordinate of bounding box.
        box_w: Normalized width of bounding box.
        box_h: Normalized height of bounding box.
        embedding: 512D face embedding.
        confidence: Detection confidence (optional).
        person_id: Associated person ID (optional).
        face_id: Optional UUID. If None, generates a new one.

    Returns:
        The face's UUID.
    """
    if face_id is None:
        face_id = str(uuid.uuid4())

    # Convert embedding to bytes
    embedding_bytes = embedding.astype(np.float32).tobytes()

    conn.execute(
        '''INSERT INTO faces
           (id, image_id, box_x, box_y, box_w, box_h, confidence, embedding, person_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (face_id, image_id, box_x, box_y, box_w, box_h, confidence,
         embedding_bytes, person_id)
    )
    conn.commit()
    return face_id


def get_face(
    conn: sqlite3.Connection,
    face_id: str,
) -> dict[str, Any] | None:
    """Get a face by ID.

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        Face dict or None if not found.
    """
    cursor = conn.execute(
        '''SELECT * FROM faces WHERE id = ?''',
        (face_id,)
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
    conn: sqlite3.Connection,
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
            '''SELECT f.*, p.name as person_name
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               WHERE f.image_id = ?''',
            (image_id,)
        )
    else:
        cursor = conn.execute(
            '''SELECT f.*, p.name as person_name
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               WHERE f.image_id = ? AND f.suppressed = 0''',
            (image_id,)
        )

    faces = []
    for row in cursor.fetchall():
        face = dict(row)
        # Convert embedding bytes to numpy array
        if face.get('embedding'):
            face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
        faces.append(face)
    return faces


def get_faces_for_person(
    conn: sqlite3.Connection,
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
        '''SELECT f.*,
                  i.timestamp as image_timestamp,
                  CASE WHEN f.id = p.preferred_face_id THEN 1 ELSE 0 END as is_preferred
           FROM faces f
           JOIN images i ON f.image_id = i.id
           JOIN people p ON f.person_id = p.id
           WHERE f.person_id = ? AND f.suppressed = 0
           ORDER BY i.timestamp''',
        (person_id,)
    )

    faces = []
    for row in cursor.fetchall():
        face = dict(row)
        if face.get('embedding'):
            face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
        faces.append(face)
    return faces


def get_all_faces(
    conn: sqlite3.Connection,
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
            '''SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h,
                      f.confidence, f.person_id, f.created_at,
                      f.unknown_group_id,
                      NULL as person_name,
                      0 as is_preferred,
                      i.timestamp as image_timestamp,
                      CASE WHEN f.unknown_group_id IS NULL THEN 1
                           ELSE COUNT(*) OVER (PARTITION BY f.unknown_group_id) END as group_size
               FROM faces f
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0 AND f.person_id IS NULL
               ORDER BY
                   CASE WHEN f.unknown_group_id IS NULL THEN 0 ELSE
                       COUNT(*) OVER (PARTITION BY f.unknown_group_id) END DESC,
                   f.unknown_group_id,
                   i.timestamp'''
        )
    else:
        cursor = conn.execute(
            '''SELECT f.id, f.image_id, f.box_x, f.box_y, f.box_w, f.box_h,
                      f.confidence, f.person_id, f.created_at,
                      f.unknown_group_id,
                      p.name as person_name,
                      CASE WHEN f.id = p.preferred_face_id THEN 1 ELSE 0 END as is_preferred,
                      i.timestamp as image_timestamp,
                      CASE WHEN f.person_id IS NOT NULL THEN NULL
                           WHEN f.unknown_group_id IS NULL THEN 1
                           ELSE COUNT(*) OVER (PARTITION BY f.unknown_group_id) END as group_size
               FROM faces f
               LEFT JOIN people p ON f.person_id = p.id
               JOIN images i ON f.image_id = i.id
               WHERE f.suppressed = 0
               ORDER BY
                   CASE WHEN f.person_id IS NULL THEN 1 ELSE 0 END,
                   p.name COLLATE NOCASE,
                   CASE WHEN f.unknown_group_id IS NULL THEN 0 ELSE
                       CASE WHEN f.person_id IS NULL
                            THEN COUNT(*) OVER (PARTITION BY f.unknown_group_id)
                            ELSE 0 END END DESC,
                   f.unknown_group_id,
                   i.timestamp'''
        )

    return [dict(row) for row in cursor.fetchall()]


def get_all_known_face_embeddings(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, np.ndarray]]:
    """Get all face embeddings for known people.

    Returns embeddings for faces that have been identified (have a person_id)
    and are not suppressed. Used for auto-recognition of new faces.

    Args:
        conn: Database connection.

    Returns:
        List of (face_id, person_id, embedding) tuples.
    """
    cursor = conn.execute(
        '''SELECT id, person_id, embedding
           FROM faces
           WHERE person_id IS NOT NULL AND suppressed = 0'''
    )

    results = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        results.append((row['id'], row['person_id'], embedding))
    return results


def update_face_person(
    conn: sqlite3.Connection,
    face_id: str,
    person_id: str | None,
) -> bool:
    """Update the person associated with a face.

    Args:
        conn: Database connection.
        face_id: Face's UUID.
        person_id: Person's UUID, or None to unlink.

    Returns:
        True if updated, False if face not found.
    """
    cursor = conn.execute(
        '''UPDATE faces SET person_id = ? WHERE id = ?''',
        (person_id, face_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def suppress_face(
    conn: sqlite3.Connection,
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
        '''UPDATE faces SET suppressed = 1, person_id = NULL WHERE id = ?''',
        (face_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_no_faces_detected(
    conn: sqlite3.Connection,
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
    conn.execute(
        '''INSERT INTO faces
           (id, image_id, box_x, box_y, box_w, box_h, confidence, embedding,
            person_id, suppressed)
           VALUES (?, ?, 0, 0, 0, 0, 0, ?, NULL, 1)''',
        (face_id, image_id, dummy_embedding)
    )
    conn.commit()
    return face_id


def delete_face(
    conn: sqlite3.Connection,
    face_id: str,
) -> bool:
    """Delete a face record entirely.

    Args:
        conn: Database connection.
        face_id: Face's UUID.

    Returns:
        True if deleted, False if face not found.
    """
    cursor = conn.execute(
        '''DELETE FROM faces WHERE id = ?''',
        (face_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_faces_for_image(
    conn: sqlite3.Connection,
    image_id: str,
) -> int:
    """Delete all faces for an image.

    Args:
        conn: Database connection.
        image_id: Image's UUID.

    Returns:
        Number of faces deleted.
    """
    cursor = conn.execute(
        '''DELETE FROM faces WHERE image_id = ?''',
        (image_id,)
    )
    conn.commit()
    return cursor.rowcount


def rotate_faces_for_image(
    conn: sqlite3.Connection,
    image_id: str,
    direction: str,
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

    For 90° counter-clockwise rotation:
        new_x = old_y
        new_y = 1 - old_x - old_w
        new_w = old_h
        new_h = old_w

    Args:
        conn: Database connection.
        image_id: Image's UUID.
        direction: 'cw' for clockwise, 'ccw' for counter-clockwise.

    Returns:
        Number of faces updated.
    """
    # Get all faces for this image
    cursor = conn.execute(
        '''SELECT id, box_x, box_y, box_w, box_h FROM faces WHERE image_id = ?''',
        (image_id,)
    )
    faces = cursor.fetchall()

    if not faces:
        return 0

    updated_count = 0
    for face_id, box_x, box_y, box_w, box_h in faces:
        if direction == 'cw':
            # 90° clockwise rotation
            new_x = 1.0 - box_y - box_h
            new_y = box_x
            new_w = box_h
            new_h = box_w
        else:  # ccw
            # 90° counter-clockwise rotation
            new_x = box_y
            new_y = 1.0 - box_x - box_w
            new_w = box_h
            new_h = box_w

        conn.execute(
            '''UPDATE faces SET box_x = ?, box_y = ?, box_w = ?, box_h = ? WHERE id = ?''',
            (new_x, new_y, new_w, new_h, face_id)
        )
        updated_count += 1

    conn.commit()
    return updated_count


def has_faces_detected(
    conn: sqlite3.Connection,
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
    cursor = conn.execute(
        '''SELECT 1 FROM faces WHERE image_id = ? LIMIT 1''',
        (image_id,)
    )
    return cursor.fetchone() is not None


# =============================================================================
# AUTO-RECOGNITION
# =============================================================================

def find_best_match(
    embedding: np.ndarray,
    known_embeddings: list[tuple[str, str, np.ndarray]],
    threshold: float = 0.65,
) -> tuple[str, str, float] | None:
    """Find the best matching person for a face embedding.

    Compares the embedding against all known face embeddings and returns
    the best match above the threshold.

    Args:
        embedding: 512D face embedding to match.
        known_embeddings: List of (face_id, person_id, embedding) tuples.
        threshold: Minimum cosine similarity for a match.

    Returns:
        Tuple of (face_id, person_id, similarity) for best match,
        or None if no match above threshold.
    """
    if not known_embeddings:
        return None

    # Ensure input embedding is normalized
    emb_norm = np.linalg.norm(embedding)
    if not np.isclose(emb_norm, 1.0, atol=0.01):
        embedding = embedding / emb_norm

    best_match = None
    best_similarity = threshold

    for face_id, person_id, known_embedding in known_embeddings:
        # Ensure known embedding is normalized
        known_norm = np.linalg.norm(known_embedding)
        if not np.isclose(known_norm, 1.0, atol=0.01):
            known_embedding = known_embedding / known_norm

        # Cosine similarity (embeddings are L2-normalized)
        similarity = float(np.dot(embedding, known_embedding))

        if similarity > best_similarity:
            best_similarity = similarity
            best_match = (face_id, person_id, similarity)

    return best_match


def auto_recognize_face(
    conn: sqlite3.Connection,
    face_id: str,
    threshold: float = 0.65,
) -> str | None:
    """Attempt to auto-recognize a face.

    Compares the face embedding against all known faces and assigns
    the person_id if a match is found above the threshold.

    Args:
        conn: Database connection.
        face_id: Face's UUID.
        threshold: Minimum cosine similarity for auto-match.

    Returns:
        Matched person_id, or None if no match found.
    """
    # Get the face embedding
    face = get_face(conn, face_id)
    if face is None or face.get('embedding') is None:
        return None

    # Skip if already assigned
    if face.get('person_id'):
        return face['person_id']

    # Get all known face embeddings
    known_embeddings = get_all_known_face_embeddings(conn)

    # Find best match
    match = find_best_match(face['embedding'], known_embeddings, threshold)

    if match:
        matched_face_id, person_id, similarity = match
        logger.debug(
            f'Auto-matched face {face_id} to person {person_id} '
            f'(similarity: {similarity:.3f})'
        )
        update_face_person(conn, face_id, person_id)
        return person_id

    return None


# =============================================================================
# IMAGE QUERIES WITH PEOPLE FILTER
# =============================================================================

def get_images_with_people(
    conn: sqlite3.Connection,
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
        queries.append('''
            SELECT DISTINCT image_id FROM faces
            WHERE person_id = ? AND suppressed = 0
        ''')
        params.append(person_id)

    query = ' INTERSECT '.join(queries)
    cursor = conn.execute(query, params)
    return [row['image_id'] for row in cursor.fetchall()]


def get_people_names_for_image(
    conn: sqlite3.Connection,
    image_id: str,
) -> list[str]:
    """Get sorted list of people names for an image.

    Used for "sort by people" functionality.

    Args:
        conn: Database connection.
        image_id: Image's UUID.

    Returns:
        List of person names, sorted alphabetically (case-insensitive).
    """
    cursor = conn.execute('''
        SELECT DISTINCT p.name
        FROM faces f
        JOIN people p ON f.person_id = p.id
        WHERE f.image_id = ? AND f.suppressed = 0
        ORDER BY p.name COLLATE NOCASE
    ''', (image_id,))
    return [row['name'] for row in cursor.fetchall()]


# =============================================================================
# BATCH IDENTIFICATION AND AUTO-REASSESSMENT
# =============================================================================

# Global cache for embeddings (populated on demand)
_embedding_cache = {
    'known': None,      # List of (face_id, person_id, embedding)
    'unknown': None,    # List of (face_id, embedding)
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
    conn: sqlite3.Connection,
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


def get_all_unknown_face_embeddings(
    conn: sqlite3.Connection,
) -> list[tuple[str, np.ndarray]]:
    """Get all face embeddings for unknown (unidentified) faces.

    Returns embeddings for faces that have NOT been identified (no person_id)
    and are not suppressed.

    Args:
        conn: Database connection.

    Returns:
        List of (face_id, embedding) tuples.
    """
    cursor = conn.execute(
        '''SELECT id, embedding
           FROM faces
           WHERE person_id IS NULL AND suppressed = 0 AND embedding IS NOT NULL'''
    )

    results = []
    for row in cursor.fetchall():
        embedding = np.frombuffer(row['embedding'], dtype=np.float32)
        results.append((row['id'], embedding))
    return results


def get_cached_unknown_embeddings(
    conn: sqlite3.Connection,
) -> list[tuple[str, np.ndarray]]:
    """Get unknown face embeddings with RAM caching.

    Args:
        conn: Database connection.

    Returns:
        List of (face_id, embedding) tuples.
    """
    with _embedding_cache['lock']:
        if _embedding_cache['unknown'] is None:
            _embedding_cache['unknown'] = get_all_unknown_face_embeddings(conn)
        return _embedding_cache['unknown']


def batch_identify_faces(
    conn: sqlite3.Connection,
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

    # Update all faces with person_id
    updated_faces = []
    for face_id in face_ids:
        face = get_face(conn, face_id)
        if face is not None:
            update_face_person(conn, face_id, person_id)
            updated_faces.append(face_id)

    # Set preferred face if specified and person doesn't have one
    if preferred_face_id and preferred_face_id in updated_faces:
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
    conn: sqlite3.Connection,
    threshold: float = 0.65,
    person_id: str | None = None,
) -> list[tuple[str, str, float]]:
    """Re-assess all unknown faces against known embeddings.

    Uses vectorized numpy operations for fast comparison.
    Supports per-person recognition thresholds (overrides global threshold).

    Args:
        conn: Database connection.
        threshold: Default minimum cosine similarity for auto-match.
        person_id: If specified, only compare against this person's faces.

    Returns:
        List of (face_id, person_id, similarity) for matched faces.
    """
    logger.info(f'Reassessing unknown faces with default threshold={threshold:.3f}, person_id={person_id}')

    # Load per-person thresholds (person_id -> threshold, None means use default)
    person_thresholds: dict[str, float | None] = {}
    cursor = conn.execute('SELECT id, recognition_threshold FROM people')
    for row in cursor.fetchall():
        person_thresholds[row['id']] = row['recognition_threshold']

    # Diagnostic: check embedding health
    def diagnose_embeddings(name, embeddings_list):
        """Check if embeddings are valid and diverse."""
        if not embeddings_list:
            logger.warning(f'{name}: no embeddings')
            return

        # Get just the embedding arrays
        if len(embeddings_list[0]) == 3:  # (face_id, person_id, embedding)
            embs = [e[2] for e in embeddings_list]
        else:  # (face_id, embedding)
            embs = [e[1] for e in embeddings_list]

        emb_matrix = np.vstack(embs)

        # Check shape
        logger.info(f'{name}: {len(embs)} embeddings, shape {emb_matrix.shape}')

        # Check for zeros/constants
        mean_vals = np.mean(emb_matrix, axis=0)
        std_vals = np.std(emb_matrix, axis=0)
        overall_std = np.std(emb_matrix)

        logger.info(f'{name}: overall std={overall_std:.6f}, per-dim std range=[{std_vals.min():.6f}, {std_vals.max():.6f}]')

        # Check pairwise similarity of first few
        if len(embs) >= 2:
            sample_size = min(5, len(embs))
            sample = emb_matrix[:sample_size]
            pairwise = sample @ sample.T
            # Get off-diagonal similarities
            off_diag = pairwise[np.triu_indices(sample_size, k=1)]
            logger.info(f'{name}: sample pairwise similarities (should vary): {off_diag}')

    # Get embeddings (from cache if available)
    if person_id:
        # Only get embeddings for the specified person
        cursor = conn.execute(
            '''SELECT id, person_id, embedding
               FROM faces
               WHERE person_id = ? AND suppressed = 0''',
            (person_id,)
        )
        known_embeddings = []
        for row in cursor.fetchall():
            embedding = np.frombuffer(row['embedding'], dtype=np.float32)
            known_embeddings.append((row['id'], row['person_id'], embedding))
    else:
        known_embeddings = get_cached_known_embeddings(conn)

    unknown_embeddings = get_all_unknown_face_embeddings(conn)

    if not known_embeddings or not unknown_embeddings:
        return []

    # Diagnostic: check embedding health
    diagnose_embeddings('Known', known_embeddings)
    diagnose_embeddings('Unknown (sample)', unknown_embeddings[:100])  # Sample to avoid log spam

    # Build matrices for vectorized comparison
    # known_matrix: (num_known, 512)
    # unknown_matrix: (num_unknown, 512)
    known_ids = [(fid, pid) for fid, pid, _ in known_embeddings]
    known_matrix = np.vstack([emb for _, _, emb in known_embeddings])

    unknown_ids = [fid for fid, _ in unknown_embeddings]
    unknown_matrix = np.vstack([emb for _, emb in unknown_embeddings])

    # Verify embeddings are L2-normalized (norms should be ~1.0)
    known_norms = np.linalg.norm(known_matrix, axis=1)
    unknown_norms = np.linalg.norm(unknown_matrix, axis=1)
    if not np.allclose(known_norms, 1.0, atol=0.01):
        logger.warning(f'Known embeddings not normalized! norms: min={known_norms.min():.3f}, max={known_norms.max():.3f}')
        # Re-normalize
        known_matrix = known_matrix / known_norms[:, np.newaxis]
    if not np.allclose(unknown_norms, 1.0, atol=0.01):
        logger.warning(f'Unknown embeddings not normalized! norms: min={unknown_norms.min():.3f}, max={unknown_norms.max():.3f}')
        # Re-normalize
        unknown_matrix = unknown_matrix / unknown_norms[:, np.newaxis]

    # Compute all similarities at once: (num_unknown, num_known)
    # Embeddings are L2-normalized, so dot product = cosine similarity
    similarities = unknown_matrix @ known_matrix.T

    # Find best match for each unknown face
    matched = []
    for i, unknown_face_id in enumerate(unknown_ids):
        best_idx = np.argmax(similarities[i])
        best_similarity = similarities[i, best_idx]

        _, matched_person_id = known_ids[best_idx]

        # Use per-person threshold if set, otherwise use global default
        person_threshold = person_thresholds.get(matched_person_id)
        effective_threshold = person_threshold if person_threshold is not None else threshold

        if best_similarity >= effective_threshold:
            matched.append((unknown_face_id, matched_person_id, float(best_similarity)))

    # Log summary with similarity distribution
    if matched:
        sims = [m[2] for m in matched]
        logger.info(
            f'Reassessment found {len(matched)} matches out of {len(unknown_ids)} unknown faces '
            f'(min={min(sims):.3f}, max={max(sims):.3f}, threshold={threshold:.3f})'
        )
    else:
        logger.info(f'Reassessment found 0 matches out of {len(unknown_ids)} unknown faces')

    # Apply matches
    for face_id, matched_person_id, similarity in matched:
        logger.debug(
            f'Auto-matched face {face_id} to person {matched_person_id} '
            f'(similarity: {similarity:.3f})'
        )
        update_face_person(conn, face_id, matched_person_id)

    # Invalidate cache if we made matches
    if matched:
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
    conn: sqlite3.Connection,
    threshold: float = 0.65,
) -> int:
    """Compute similarity groups for unknown faces using UnionFind clustering.

    Unknown faces that are similar to each other (above threshold) are grouped
    together. This helps users identify the same unknown person across images.

    The algorithm:
    1. Load all unknown face embeddings (already L2-normalized)
    2. Compute pairwise cosine similarity using chunked matrix multiplication
    3. Use UnionFind to cluster faces above the similarity threshold
    4. Assign group IDs and update the database

    Args:
        conn: Database connection.
        threshold: Minimum cosine similarity to group faces together.

    Returns:
        Number of groups created.
    """
    # Load unknown faces with embeddings
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

    # Stack embeddings into a matrix (already L2-normalized)
    embedding_matrix = np.vstack(embeddings)

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

        # Find pairs above threshold
        for local_idx in range(chunk_end - i):
            global_idx = i + local_idx
            face_id_i = face_ids[global_idx]

            # Only check j > global_idx to avoid duplicate pairs
            for j in range(global_idx + 1, n_faces):
                if similarities[local_idx, j] >= threshold:
                    face_id_j = face_ids[j]
                    uf.union_ids(face_id_i, face_id_j)

    # Extract groups and assign group IDs
    logger.info('Extracting groups from UnionFind structure...')
    groups = uf.extract_groups_by_id()
    logger.info(f'Found {len(groups)} distinct clusters, assigning group IDs...')

    # Clear all existing group IDs first
    conn.execute("UPDATE faces SET unknown_group_id = NULL WHERE person_id IS NULL")

    # Assign new group IDs (batch to avoid SQLite variable limit of ~999)
    BATCH_SIZE = 500  # Leave room for the group_id parameter
    n_groups = 0
    for root_id, members in groups.items():
        if len(members) > 1:
            # Generate a group ID
            group_id = str(uuid.uuid4())[:8]
            n_groups += 1

            # Update faces in batches to avoid "too many SQL variables" error
            for i in range(0, len(members), BATCH_SIZE):
                batch = members[i:i + BATCH_SIZE]
                placeholders = ','.join('?' * len(batch))
                conn.execute(
                    f"UPDATE faces SET unknown_group_id = ? WHERE id IN ({placeholders})",
                    [group_id] + batch
                )

    conn.commit()
    logger.info(f'Created {n_groups} unknown face groups')
    return n_groups


def compute_unknown_face_groups_async(
    db: 'ImageDatabase',
    threshold: float = 0.65,
    callback: callable = None,
) -> None:
    """Compute unknown face groups in a background thread.

    Uses the shared database connection and lock from ImageDatabase to avoid
    "database is locked" errors when other threads are accessing the database.

    Args:
        db: ImageDatabase instance (provides conn and _db_lock).
        threshold: Minimum cosine similarity for grouping.
        callback: Optional callback(n_groups) when done.
    """
    global _grouping_thread, _grouping_status

    def _worker():
        global _grouping_thread, _grouping_status
        try:
            with _grouping_lock:
                _grouping_status = {'status': 'computing', 'progress': 0}

            # Use shared connection with lock from ImageDatabase
            with db._db_lock:
                n_groups = compute_unknown_face_groups(db.conn, threshold)

            # Store result
            with _grouping_lock:
                _grouping_status = {
                    'status': 'done',
                    'n_groups': n_groups,
                }

            logger.info(f'Async face grouping complete: {n_groups} groups')

            if callback:
                callback(n_groups)
        except Exception as e:
            logger.error(f'Async face grouping failed: {e}')
            with _grouping_lock:
                _grouping_status = {'status': 'error', 'error': str(e)}
        finally:
            with _grouping_lock:
                _grouping_thread = None

    with _grouping_lock:
        # Only start if not already running
        if _grouping_thread is None or not _grouping_thread.is_alive():
            _grouping_status = {'status': 'starting'}
            _grouping_thread = threading.Thread(target=_worker, daemon=True)
            _grouping_thread.start()
            logger.info('Started async face grouping')
        else:
            logger.debug('Face grouping already in progress, skipping')


def get_group_computation_status() -> dict:
    """Get status of async face grouping.

    Returns:
        Dict with 'status' ('idle', 'computing', 'done', 'error') and related info.
    """
    with _grouping_lock:
        if _grouping_status is None:
            return {'status': 'idle'}
        return _grouping_status.copy()


def get_unknown_faces_grouped(conn: sqlite3.Connection) -> list[dict]:
    """Get unknown faces sorted by group size and timestamp.

    Returns faces with group information, sorted so that larger groups
    appear first, and within groups, faces are sorted by image timestamp.

    Args:
        conn: Database connection.

    Returns:
        List of face dicts with group_size field.
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
            i.timestamp as image_timestamp,
            i.basename,
            COUNT(*) OVER (PARTITION BY f.unknown_group_id) as group_size
        FROM faces f
        JOIN images i ON f.image_id = i.id
        WHERE f.person_id IS NULL
          AND f.suppressed = 0
        ORDER BY
            CASE WHEN f.unknown_group_id IS NULL THEN 0 ELSE group_size END DESC,
            f.unknown_group_id,
            i.timestamp
    """)

    faces = []
    for row in cursor:
        faces.append({
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
            'group_size': row['group_size'] if row['unknown_group_id'] else 1,
        })

    return faces


def is_reassessment_in_progress() -> bool:
    """Check if async reassessment is currently running."""
    with _reassess_lock:
        return _reassess_thread is not None and _reassess_thread.is_alive()


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


def reassess_unknown_faces_async(
    db: 'ImageDatabase',
    threshold: float = 0.65,
    person_id: str | None = None,
    callback: callable = None,
) -> None:
    """Re-assess unknown faces in a background thread.

    Uses the shared database connection and lock from ImageDatabase to avoid
    "database is locked" errors when other threads are accessing the database.

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
            # Use shared connection with lock from ImageDatabase
            with db._db_lock:
                matched = reassess_unknown_faces(db.conn, threshold, person_id)

            # Store result
            with _reassess_lock:
                _reassess_result = {
                    'matched_count': len(matched),
                    'person_id': person_id,
                }

            logger.info(f'Async reassessment complete: {len(matched)} faces matched')

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
            logger.info('Started async face reassessment')
        else:
            logger.debug('Reassessment already in progress, skipping')
