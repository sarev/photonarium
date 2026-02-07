"""
Central RAW image loading module for the Imaginary image database.

This module provides a unified interface for loading both standard image formats
(via Pillow) and camera RAW formats (via rawpy/LibRaw). All code that previously
called ``Image.open()`` or ``cv2.imread()`` should migrate to the functions here
so that RAW files are handled transparently.

RAW support covers the most common camera manufacturer formats:
Canon (.cr2, .cr3), Nikon (.nef, .nrw), Sony (.arw, .srf), Adobe (.dng),
Fujifilm (.raf), Panasonic (.rw2), Olympus (.orf), Pentax (.pef),
Samsung (.srw), Sigma (.x3f), Hasselblad (.3fr), Phase One (.iiq),
Leica (.rwl), Kodak (.kdc, .dcr), and Epson (.erf).

Key design decisions:
    - ``open_image()`` replaces ``Image.open()`` everywhere. It returns a
      fully-decoded PIL Image with EXIF orientation already applied, so callers
      no longer need ``ImageOps.exif_transpose()``.
    - RAW files are decoded via rawpy with ``use_camera_wb=True`` for sensible
      colours and ``output_bps=8`` for 8-bit RGB compatible with PIL/OpenCV.
    - ``get_raw_dimensions()`` reads width/height from the RAW header without
      full demosaicing, which is much faster for indexing.
    - ``extract_raw_exif()`` uses the pure-Python ``exifread`` library to pull
      EXIF timestamps from RAW files (Pillow cannot read RAW EXIF).

Usage:
    from rawimage import open_image, open_image_as_numpy, is_raw_format

    img = open_image('/path/to/photo.cr2')       # PIL Image, RGB
    arr = open_image_as_numpy('/path/to/photo.nef')  # numpy BGR array
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from PIL import Image, ImageOps

import cv2
import logging
import numpy as np

# Configure module logger
logger = logging.getLogger(__name__)

# Try to import rawpy — it's required for RAW support but we degrade
# gracefully if it's missing (RAW files simply won't load).
try:
    import rawpy
    _HAS_RAWPY = True
except ImportError:
    _HAS_RAWPY = False
    logger.warning('rawpy not installed — RAW image support is disabled')

# Try to import exifread — needed for RAW EXIF timestamps.
try:
    import exifread
    _HAS_EXIFREAD = True
except ImportError:
    _HAS_EXIFREAD = False
    logger.warning('exifread not installed — RAW EXIF timestamps unavailable')


# ============================================================================
# RAW FORMAT DETECTION
# ============================================================================

# All recognised camera RAW file extensions (lowercase, with leading dot).
# This is a frozenset for fast O(1) membership tests during folder scanning.
RAW_EXTENSIONS: frozenset[str] = frozenset({
    '.cr2',   # Canon (older)
    '.cr3',   # Canon (newer, HEIF-based)
    '.nef',   # Nikon
    '.nrw',   # Nikon (compact cameras)
    '.arw',   # Sony
    '.srf',   # Sony (older)
    '.dng',   # Adobe Digital Negative (universal)
    '.raf',   # Fujifilm
    '.rw2',   # Panasonic
    '.orf',   # Olympus (OM System)
    '.pef',   # Pentax
    '.srw',   # Samsung
    '.x3f',   # Sigma (Foveon sensor)
    '.3fr',   # Hasselblad
    '.iiq',   # Phase One
    '.rwl',   # Leica
    '.kdc',   # Kodak
    '.dcr',   # Kodak (older)
    '.erf',   # Epson
})


def is_raw_format(path: Path | str) -> bool:
    """Check whether a file path has a camera RAW extension.

    Args:
        path: Path to the image file (only the extension is examined).

    Returns:
        True if the extension is in RAW_EXTENSIONS, False otherwise.
    """
    return Path(path).suffix.lower() in RAW_EXTENSIONS


# ============================================================================
# IMAGE LOADING — PIL
# ============================================================================

def open_image(path: Path | str) -> Image.Image:
    """Open an image file and return a PIL Image with correct orientation.

    For standard formats (JPEG, PNG, etc.) this uses Pillow with automatic
    EXIF orientation correction. For camera RAW formats this uses rawpy to
    demosaic the sensor data into an 8-bit RGB image.

    The returned image is fully loaded into memory (not lazily decoded), so
    callers do not need a ``with`` context manager.

    Args:
        path: Path to the image file.

    Returns:
        PIL Image (typically RGB). Callers should ``.convert('RGB')`` if they
        need guaranteed RGB mode (e.g. for ML models).

    Raises:
        FileNotFoundError: If the file does not exist.
        OSError: If the file cannot be read or decoded.
        RuntimeError: If the file is RAW but rawpy is not installed.
    """
    path = Path(path)

    if is_raw_format(path):
        return _open_raw_as_pil(path)

    # Standard format — use Pillow
    img = Image.open(path)
    # Apply EXIF orientation so downstream code sees correctly-rotated pixels
    img = ImageOps.exif_transpose(img)
    # Force full decode so the file handle is released immediately
    img.load()
    return img


def _open_raw_as_pil(path: Path) -> Image.Image:
    """Decode a RAW file to a PIL RGB Image via rawpy.

    Uses camera white balance for natural colours and 8-bit output
    for compatibility with PIL and downstream processing.

    Args:
        path: Path to the RAW file.

    Returns:
        PIL Image in RGB mode.

    Raises:
        RuntimeError: If rawpy is not installed.
        rawpy.LibRawError: If the RAW file cannot be decoded.
    """
    if not _HAS_RAWPY:
        raise RuntimeError(
            f'Cannot open RAW file {path}: rawpy is not installed. '
            f'Install it with: pip install rawpy'
        )

    with rawpy.imread(str(path)) as raw:
        # use_camera_wb=True  — use the white balance recorded by the camera
        # output_bps=8        — 8-bit per channel (compatible with PIL uint8)
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)

    # rawpy returns a numpy array (H, W, 3) in RGB order
    return Image.fromarray(rgb, 'RGB')


# ============================================================================
# IMAGE LOADING — NUMPY / OPENCV
# ============================================================================

def open_image_as_numpy(path: Path | str) -> np.ndarray | None:
    """Open an image file and return a BGR numpy array for OpenCV processing.

    For standard formats this uses ``cv2.imread()`` (which returns BGR).
    For RAW formats this uses rawpy and converts RGB → BGR.

    Args:
        path: Path to the image file.

    Returns:
        numpy array in BGR colour order (H, W, 3), or None if the file
        cannot be read.
    """
    path = Path(path)

    if is_raw_format(path):
        try:
            return _open_raw_as_bgr(path)
        except Exception as e:
            logger.warning(f'Failed to read RAW image as numpy: {path}: {e}')
            return None

    # Standard format — cv2.imread returns BGR natively
    img = cv2.imread(str(path))
    if img is None:
        logger.warning(f'OpenCV failed to read image: {path}')
    return img


def _open_raw_as_bgr(path: Path) -> np.ndarray:
    """Decode a RAW file to a BGR numpy array.

    Args:
        path: Path to the RAW file.

    Returns:
        numpy array (H, W, 3) in BGR order.

    Raises:
        RuntimeError: If rawpy is not installed.
    """
    if not _HAS_RAWPY:
        raise RuntimeError(
            f'Cannot open RAW file {path}: rawpy is not installed.'
        )

    with rawpy.imread(str(path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)

    # Convert RGB → BGR for OpenCV compatibility
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


# ============================================================================
# RAW METADATA — DIMENSIONS
# ============================================================================

def get_raw_dimensions(path: Path | str) -> tuple[int, int] | None:
    """Get the output dimensions of a RAW file without full demosaicing.

    Reads the RAW header to determine the final image size after orientation
    correction. This is much faster than a full decode (~ms vs ~seconds).

    Args:
        path: Path to the RAW file.

    Returns:
        Tuple of (width, height) in pixels, or None if dimensions cannot
        be determined.
    """
    if not _HAS_RAWPY:
        logger.warning(f'Cannot read RAW dimensions for {path}: rawpy not installed')
        return None

    try:
        with rawpy.imread(str(path)) as raw:
            sizes = raw.sizes
            # sizes.width/height are the raw sensor dimensions; we want the
            # output dimensions which account for the camera's orientation flag.
            # sizes.flip encodes orientation: 0=normal, 3=180, 5=90CW, 6=90CCW
            if sizes.flip in (5, 6):
                # 90° rotation swaps width and height
                return (sizes.height, sizes.width)
            return (sizes.width, sizes.height)
    except Exception as e:
        logger.warning(f'Failed to read RAW dimensions for {path}: {e}')
        return None


# ============================================================================
# RAW METADATA — EXIF TIMESTAMPS
# ============================================================================

def extract_raw_exif(path: Path | str) -> datetime | None:
    """Extract a timestamp from a RAW file's EXIF data using exifread.

    Pillow cannot read EXIF from camera RAW formats, so we use the
    pure-Python exifread library instead. Tries DateTimeOriginal first
    (when the photo was taken), then DateTime (last software modification).

    Args:
        path: Path to the RAW file.

    Returns:
        datetime object if a valid timestamp was found, None otherwise.
    """
    if not _HAS_EXIFREAD:
        logger.debug(f'Cannot extract RAW EXIF from {path}: exifread not installed')
        return None

    try:
        with open(path, 'rb') as f:
            tags = exifread.process_file(f, stop_tag='DateTime', details=False)

        # Try DateTimeOriginal first (when photo was actually taken)
        for tag_name in ('EXIF DateTimeOriginal', 'Image DateTime'):
            value = tags.get(tag_name)
            if value:
                result = _parse_exif_datetime(str(value))
                if result:
                    return result

    except Exception as e:
        logger.debug(f'Failed to extract EXIF from RAW file {path}: {e}')

    return None


def _parse_exif_datetime(exif_value: str) -> datetime | None:
    """Parse an EXIF datetime string into a datetime object.

    Replicates the logic from timestamps._parse_exif_datetime() to avoid
    a circular import (timestamps.py imports from us for RAW detection).

    EXIF datetime format is typically ``YYYY:MM:DD HH:MM:SS``.

    Args:
        exif_value: EXIF datetime string.

    Returns:
        datetime object if parsing succeeds, None otherwise.
    """
    if not exif_value or not isinstance(exif_value, str):
        return None

    # Standard EXIF format: "2024:01:15 14:30:00"
    formats = [
        '%Y:%m:%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S',
        '%Y:%m:%d %H:%M',
        '%Y-%m-%d %H:%M',
    ]
    for fmt in formats:
        try:
            return datetime.strptime(exif_value.strip(), fmt)
        except ValueError:
            continue

    return None
