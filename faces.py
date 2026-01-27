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
from typing import Any

import logging
import numpy as np
import sqlite3
import threading
import uuid

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


def init_face_tables(conn: sqlite3.Connection) -> None:
    """Initialize the face recognition database tables.

    Creates the people and faces tables if they don't exist, along with
    necessary indexes.

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

    conn.commit()
    logger.info('Face recognition tables initialized')


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
                        post_process=False,  # Return raw tensors
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

                # Filter by confidence
                valid_indices = [
                    i for i, prob in enumerate(probs)
                    if prob is not None and prob >= self.min_confidence
                ]

                if not valid_indices:
                    return []

                # Get aligned face crops for embedding
                # MTCNN extract returns faces cropped and aligned, size 160x160
                faces_tensor = self.mtcnn(img)

                if faces_tensor is None:
                    return []

                # Ensure tensor is in correct format
                if len(faces_tensor.shape) == 3:
                    # Single face - add batch dimension
                    faces_tensor = faces_tensor.unsqueeze(0)

                processed_width, processed_height = img.size

                # First pass: collect valid faces and their metadata
                valid_faces = []  # List of (tensor_idx, norm_box, confidence)
                for i in valid_indices:
                    if i >= len(faces_tensor):
                        continue

                    box = boxes[i]
                    prob = probs[i]

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
    ) -> dict[Path, list[DetectedFace]]:
        """Detect faces in multiple images using parallel processing.

        This method processes multiple images in parallel using a thread pool
        for image loading and MTCNN detection, then batches the face embedding
        computation for all detected faces.

        Args:
            image_paths: List of paths to image files.
            max_dimension: Maximum image dimension for processing.
            num_workers: Number of parallel workers for detection.

        Returns:
            Dict mapping each image path to its list of DetectedFace objects.
            Images that fail to process will have empty lists.
        """
        import torch
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if not image_paths:
            return {}

        results: dict[Path, list[DetectedFace]] = {}

        # Initialize results with empty lists
        for p in image_paths:
            results[Path(p)] = []

        # Phase 1: Run MTCNN detection on all images in parallel
        # Each worker returns: (image_path, list of (tensor, norm_box, confidence))
        all_faces_data = []  # (image_path, tensor, norm_box, confidence)

        def process_single_image(image_path: Path | str):
            """Load image and run MTCNN detection. Returns face data."""
            image_path = Path(image_path)
            faces_data = []

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

                    # Detect faces
                    boxes, probs = self.mtcnn.detect(img)

                    if boxes is None or len(boxes) == 0:
                        return image_path, []

                    # Filter by confidence
                    valid_indices = [
                        i for i, prob in enumerate(probs)
                        if prob is not None and prob >= self.min_confidence
                    ]

                    if not valid_indices:
                        return image_path, []

                    # Get face crops
                    faces_tensor = self.mtcnn(img)
                    if faces_tensor is None:
                        return image_path, []

                    if len(faces_tensor.shape) == 3:
                        faces_tensor = faces_tensor.unsqueeze(0)

                    processed_width, processed_height = img.size

                    for i in valid_indices:
                        if i >= len(faces_tensor):
                            continue

                        box = boxes[i]
                        prob = probs[i]

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

                        faces_data.append((
                            faces_tensor[i],
                            (norm_x, norm_y, norm_w, norm_h),
                            float(prob),
                        ))

            except Exception as e:
                logger.error(f'Face detection failed for {image_path}: {e}')
                return image_path, []

            return image_path, faces_data

        # Run detection in parallel
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {executor.submit(process_single_image, p): p for p in image_paths}
            for future in as_completed(futures):
                try:
                    image_path, faces_data = future.result()
                    for tensor, norm_box, confidence in faces_data:
                        all_faces_data.append((image_path, tensor, norm_box, confidence))
                except Exception as e:
                    logger.error(f'Face detection worker failed: {e}')

        if not all_faces_data:
            return results

        # Phase 2: Batch compute embeddings for ALL faces across all images
        all_tensors = torch.stack([fd[1] for fd in all_faces_data]).to(self.device)

        with torch.no_grad():
            embeddings_batch = self.resnet(all_tensors)
            embeddings_batch = embeddings_batch.cpu().numpy()

        # Normalize embeddings
        norms = np.linalg.norm(embeddings_batch, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings_batch = embeddings_batch / norms

        # Phase 3: Build results dict
        for idx, (image_path, _, norm_box, confidence) in enumerate(all_faces_data):
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


def update_person(
    conn: sqlite3.Connection,
    person_id: str,
    name: str | None = None,
    preferred_face_id: str | None = None,
) -> bool:
    """Update a person record.

    Args:
        conn: Database connection.
        person_id: Person's UUID.
        name: New name (optional).
        preferred_face_id: New preferred face ID (optional).

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
        List of face dicts.
    """
    cursor = conn.execute(
        '''SELECT * FROM faces
           WHERE person_id = ? AND suppressed = 0
           ORDER BY created_at''',
        (person_id,)
    )

    faces = []
    for row in cursor.fetchall():
        face = dict(row)
        if face.get('embedding'):
            face['embedding'] = np.frombuffer(face['embedding'], dtype=np.float32)
        faces.append(face)
    return faces


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

    best_match = None
    best_similarity = threshold

    for face_id, person_id, known_embedding in known_embeddings:
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
