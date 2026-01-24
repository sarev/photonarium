"""
Thumbnail generation and caching for the Imaginary image database.

This module handles generating, caching, and managing image thumbnails.
It also includes image rotation utilities (which invalidate thumbnails).

The thumbnail cache structure is:
    <thumbnail_dir>/<size>/<first2chars>/<checksum>.jpg

Usage:
    from thumbnails import generate_thumbnail, get_thumbnail_cache_path

    # Get cache path for a thumbnail
    path = get_thumbnail_cache_path(checksum, size=200)

    # Generate a thumbnail
    generate_thumbnail(source_path, dest_path, size=200, quality=85)
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageOps

import logging
import shutil
import subprocess
import tempfile

# Configure module logger
logger = logging.getLogger(__name__)


# Default thumbnail directory
DEFAULT_THUMBNAIL_DIR = Path('.thumbnails')


def get_thumbnail_cache_path(
    checksum: str,
    size: int,
    thumbnail_dir: Path | str = DEFAULT_THUMBNAIL_DIR,
) -> Path:
    """Get the cache path for a thumbnail.

    Cache structure: <thumbnail_dir>/<size>/<first2chars>/<checksum>.jpg

    Args:
        checksum: Image checksum (used as filename).
        size: Thumbnail size in pixels.
        thumbnail_dir: Root thumbnail cache directory.

    Returns:
        Path where the thumbnail should be cached.
    """
    thumbnail_dir = Path(thumbnail_dir)
    prefix = checksum[:2] if len(checksum) >= 2 else 'xx'
    return thumbnail_dir / str(size) / prefix / f'{checksum}.jpg'


def generate_thumbnail(
    source_path: Path | str,
    dest_path: Path | str,
    size: int,
    quality: int = 85,
) -> bool:
    """Generate a thumbnail from a source image.

    Args:
        source_path: Path to the source image.
        dest_path: Path where thumbnail should be saved.
        size: Maximum dimension (longest edge) in pixels.
        quality: JPEG quality (1-100).

    Returns:
        True if thumbnail was generated successfully, False otherwise.
    """
    source_path = Path(source_path)
    dest_path = Path(dest_path)

    try:
        # Ensure destination directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Load image
        with Image.open(source_path) as img:
            # Handle EXIF orientation
            img = ImageOps.exif_transpose(img)

            # Convert to RGB (handles RGBA, palette, etc.)
            if img.mode in ('RGBA', 'LA', 'P'):
                # Create white background for transparent images
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            # Resize maintaining aspect ratio
            img.thumbnail((size, size), Image.Resampling.LANCZOS)

            # Save as JPEG
            img.save(dest_path, 'JPEG', quality=quality, optimize=True)

        logger.debug(f'Generated thumbnail: {dest_path}')
        return True

    except Exception as e:
        logger.error(f'Failed to generate thumbnail for {source_path}: {e}')
        return False


def rotate_image_file(
    path: Path | str,
    direction: str,
) -> bool:
    """Rotate an image file in place.

    Uses lossless rotation for JPEG files (via jpegtran if available),
    falls back to Pillow for other formats or if jpegtran is not installed.

    Args:
        path: Path to the image file.
        direction: 'cw' for clockwise (90 degrees right),
                   'ccw' for counter-clockwise (90 degrees left).

    Returns:
        True if rotation was successful, False otherwise.
    """
    path = Path(path)

    if direction not in ('cw', 'ccw'):
        logger.error(f'Invalid rotation direction: {direction}')
        return False

    if not path.exists():
        logger.error(f'Image file not found: {path}')
        return False

    # Check if JPEG (can use lossless rotation)
    suffix = path.suffix.lower()
    is_jpeg = suffix in ('.jpg', '.jpeg')

    if is_jpeg:
        # Try lossless JPEG rotation with jpegtran
        if _rotate_jpeg_lossless(path, direction):
            return True
        # Fall through to Pillow if jpegtran failed

    # Use Pillow for non-JPEG or if jpegtran unavailable
    return _rotate_with_pillow(path, direction)


def _rotate_jpeg_lossless(path: Path, direction: str) -> bool:
    """Attempt lossless JPEG rotation using jpegtran.

    Args:
        path: Path to the JPEG file.
        direction: 'cw' or 'ccw'.

    Returns:
        True if successful, False if jpegtran not available or failed.
    """
    # Map direction to jpegtran argument
    rotate_arg = '90' if direction == 'cw' else '270'

    try:
        # Create temp file for output
        with tempfile.NamedTemporaryFile(
            suffix='.jpg',
            delete=False,
            dir=path.parent,
        ) as tmp:
            tmp_path = Path(tmp.name)

        # Run jpegtran
        result = subprocess.run(
            ['jpegtran', '-rotate', rotate_arg, '-copy', 'all', '-outfile', str(tmp_path), str(path)],
            capture_output=True,
            timeout=30,
        )

        if result.returncode == 0 and tmp_path.exists():
            # Replace original with rotated version
            shutil.move(tmp_path, path)
            logger.debug(f'Lossless JPEG rotation successful: {path}')
            return True
        else:
            # jpegtran failed
            if tmp_path.exists():
                tmp_path.unlink()
            logger.debug(f'jpegtran failed: {result.stderr.decode() if result.stderr else "unknown error"}')
            return False

    except FileNotFoundError:
        # jpegtran not installed
        logger.debug('jpegtran not found, falling back to Pillow')
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f'jpegtran timed out for: {path}')
        return False
    except Exception as e:
        logger.warning(f'jpegtran error for {path}: {e}')
        if 'tmp_path' in locals() and tmp_path.exists():
            tmp_path.unlink()
        return False


def _rotate_with_pillow(path: Path, direction: str) -> bool:
    """Rotate an image using Pillow.

    Args:
        path: Path to the image file.
        direction: 'cw' or 'ccw'.

    Returns:
        True if successful, False otherwise.
    """
    try:
        with Image.open(path) as img:
            # Apply EXIF orientation first
            img = ImageOps.exif_transpose(img)

            # Rotate (Pillow uses counter-clockwise positive angles)
            if direction == 'cw':
                rotated = img.transpose(Image.Transpose.ROTATE_270)
            else:
                rotated = img.transpose(Image.Transpose.ROTATE_90)

            # Determine save format and options
            suffix = path.suffix.lower()
            save_kwargs = {}

            if suffix in ('.jpg', '.jpeg'):
                save_kwargs['quality'] = 95
                save_kwargs['optimize'] = True
            elif suffix == '.png':
                save_kwargs['optimize'] = True
            elif suffix == '.webp':
                save_kwargs['quality'] = 95

            # Save to temp file first, then replace
            with tempfile.NamedTemporaryFile(
                suffix=suffix,
                delete=False,
                dir=path.parent,
            ) as tmp:
                tmp_path = Path(tmp.name)

            rotated.save(tmp_path, **save_kwargs)
            shutil.move(tmp_path, path)

            logger.debug(f'Pillow rotation successful: {path}')
            return True

    except Exception as e:
        logger.error(f'Failed to rotate image {path}: {e}')
        if 'tmp_path' in locals() and tmp_path.exists():
            tmp_path.unlink()
        return False


def delete_thumbnails_for_checksum(
    checksum: str,
    thumbnail_dir: Path | str = DEFAULT_THUMBNAIL_DIR,
) -> int:
    """Delete all cached thumbnails for a given checksum.

    Args:
        checksum: The image checksum whose thumbnails should be deleted.
        thumbnail_dir: Root thumbnail cache directory.

    Returns:
        Number of thumbnails deleted.
    """
    thumbnail_dir = Path(thumbnail_dir)
    if not thumbnail_dir.exists():
        return 0

    count = 0
    prefix = checksum[:2] if len(checksum) >= 2 else 'xx'

    # Thumbnails are stored as: <thumbnail_dir>/<size>/<prefix>/<checksum>.jpg
    # We need to check all size directories
    for size_dir in thumbnail_dir.iterdir():
        if size_dir.is_dir():
            thumb_path = size_dir / prefix / f'{checksum}.jpg'
            if thumb_path.exists():
                try:
                    thumb_path.unlink()
                    count += 1
                except Exception as e:
                    logger.warning(f'Failed to delete thumbnail {thumb_path}: {e}')

    return count


def clear_thumbnail_cache(
    thumbnail_dir: Path | str = DEFAULT_THUMBNAIL_DIR,
    size: int | None = None,
) -> int:
    """Clear cached thumbnails.

    Args:
        thumbnail_dir: Root thumbnail cache directory.
        size: If specified, only clear thumbnails of this size.
            If None, clear all thumbnails.

    Returns:
        Number of thumbnails deleted.
    """
    thumbnail_dir = Path(thumbnail_dir)

    if not thumbnail_dir.exists():
        return 0

    count = 0

    if size is not None:
        # Clear specific size
        size_dir = thumbnail_dir / str(size)
        if size_dir.exists():
            for thumb_file in size_dir.rglob('*.jpg'):
                try:
                    thumb_file.unlink()
                    count += 1
                except OSError:
                    pass
    else:
        # Clear all sizes
        for thumb_file in thumbnail_dir.rglob('*.jpg'):
            try:
                thumb_file.unlink()
                count += 1
            except OSError:
                pass

    logger.info(f'Cleared {count} cached thumbnails')
    return count
