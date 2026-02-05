"""
Thumbnail generation and caching for the Imaginary image database.

This module handles generating, caching, and managing image thumbnails.
It also includes image rotation utilities (which invalidate thumbnails).

Components:
    - generate_thumbnail: Generate a single thumbnail with sharpening
    - get_thumbnail_cache_path: Compute cache path for a thumbnail
    - generate_missing_thumbnails: Bulk generate thumbnails for many images
    - ThumbnailCache: Thread-safe LRU RAM cache for thumbnail bytes
    - rotate_image_file: Rotate an image and invalidate its thumbnails

The thumbnail cache structure is:
    <thumbnail_dir>/<size>/<first2chars>/<checksum>.jpg

Only two canonical sizes are generated: 200px and 400px. The frontend
uses CSS to scale to the exact display size.

Usage:
    from thumbnails import generate_thumbnail, get_thumbnail_cache_path

    # Get cache path for a thumbnail
    path = get_thumbnail_cache_path(checksum, size=200)

    # Generate a thumbnail
    generate_thumbnail(source_path, dest_path, size=200, quality=85)
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from PIL import Image, ImageFilter, ImageOps

import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configure module logger
logger = logging.getLogger(__name__)


# Default thumbnail directory
DEFAULT_THUMBNAIL_DIR = Path('.thumbnails')


def _move_with_retry(src: Path, dst: Path, max_retries: int = 5, delay: float = 0.1) -> None:
    """Move a file with retry logic for Windows file locking issues.

    On Windows, files can be temporarily locked by antivirus scanners,
    search indexers, or cloud sync services. This function retries
    the move operation with exponential backoff.

    Args:
        src: Source file path.
        dst: Destination file path.
        max_retries: Maximum number of retry attempts.
        delay: Initial delay between retries (doubles each attempt).

    Raises:
        OSError: If all retry attempts fail.
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            shutil.move(src, dst)
            return
        except OSError as e:
            last_error = e
            # Only retry on Windows file locking errors
            if sys.platform == 'win32' and e.winerror == 32:
                if attempt < max_retries - 1:
                    logger.warning(
                        f'File locked, retry {attempt + 1}/{max_retries} '
                        f'(waiting {delay:.1f}s): {src.name}'
                    )
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                    continue
            raise
    raise last_error


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
    max_source_dimension: int = 16384,
) -> bool:
    """Generate a thumbnail from a source image.

    Uses LANCZOS resampling for high-quality downscaling, followed by
    subtle UnsharpMask sharpening to counteract the blur introduced by
    downscaling.

    For large images, uses PIL's draft mode to load at reduced resolution,
    which is much faster and uses less memory.

    Args:
        source_path: Path to the source image.
        dest_path: Path where thumbnail should be saved.
        size: Maximum dimension (longest edge) in pixels.
        quality: JPEG quality (1-100).
        max_source_dimension: Max dimension before using draft mode (0 to disable).

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
            # For very large images, use draft mode to load at reduced resolution
            # This is much faster and uses much less memory
            if max_source_dimension > 0:
                w, h = img.size
                max_dim = max(w, h)
                if max_dim > max_source_dimension:
                    # Calculate scale to bring down to max dimension
                    # Then further reduce to thumbnail size for efficiency
                    target_size = max(size * 2, 1024)  # Load at 2x thumbnail size for quality
                    scale = target_size / max_dim
                    draft_size = (int(w * scale), int(h * scale))
                    logger.info(
                        f'Using draft mode for oversized image {source_path}: '
                        f'{w}x{h} -> {draft_size[0]}x{draft_size[1]}'
                    )
                    # draft() only works for certain formats (JPEG, MPO)
                    # For others, we fall through to normal processing
                    try:
                        img.draft('RGB', draft_size)
                    except Exception:
                        pass  # Draft not supported for this format

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

            # Apply subtle sharpening to counteract downscale blur
            img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=60, threshold=3))

            # Save as JPEG
            img.save(dest_path, 'JPEG', quality=quality, optimize=True)

        logger.debug(f'Generated thumbnail: {dest_path}')
        return True

    except Exception as e:
        logger.error(f'Failed to generate thumbnail for {source_path}: {e}')
        return False


def rotate_image_file(
    path: Path | str,
    degrees: float,
) -> bool:
    """Rotate an image file in place.

    Uses lossless rotation for JPEG files (via jpegtran if available),
    falls back to Pillow for other formats or if jpegtran is not installed.

    Args:
        path: Path to the image file.
        degrees: Rotation angle in degrees (clockwise positive).
                 Common values: 90 (right), -90 or 270 (left), 180.

    Returns:
        True if rotation was successful, False otherwise.
    """
    path = Path(path)

    if not path.exists():
        logger.error(f'Image file not found: {path}')
        return False

    # Normalise degrees to 0-360 range
    degrees = degrees % 360
    if degrees == 0:
        return True  # No rotation needed

    # Check if JPEG (can use lossless rotation)
    suffix = path.suffix.lower()
    is_jpeg = suffix in ('.jpg', '.jpeg')

    # Lossless JPEG rotation only supports 90, 180, 270
    if is_jpeg and degrees in (90, 180, 270):
        if _rotate_jpeg_lossless(path, degrees):
            return True
        # Fall through to Pillow if jpegtran failed

    # Use Pillow for non-JPEG, arbitrary angles, or if jpegtran unavailable
    return _rotate_with_pillow(path, degrees)


def _reset_exif_orientation(path: Path) -> bool:
    """Reset EXIF orientation tag to 1 (normal) in a JPEG file.

    This is necessary after jpegtran rotation because jpegtran preserves
    the original EXIF orientation tag even though it has physically rotated
    the pixels. This causes issues when other code applies exif_transpose().

    Args:
        path: Path to the JPEG file.

    Returns:
        True if successful, False otherwise.
    """
    try:
        with Image.open(path) as img:
            # Get existing EXIF data
            exif = img.getexif()
            if not exif:
                return True  # No EXIF data, nothing to fix

            # Check if orientation tag exists (tag 0x0112 = 274)
            ORIENTATION_TAG = 274
            if ORIENTATION_TAG not in exif:
                return True  # No orientation tag, nothing to fix

            if exif[ORIENTATION_TAG] == 1:
                return True  # Already normal orientation

            # Set orientation to 1 (normal)
            exif[ORIENTATION_TAG] = 1

            # Save with updated EXIF
            # Need to save to temp file then replace (can't overwrite while open)
            with tempfile.NamedTemporaryFile(
                suffix='.jpg',
                delete=False,
                dir=path.parent,
            ) as tmp:
                tmp_path = Path(tmp.name)

            img.save(tmp_path, 'JPEG', quality=95, exif=exif, optimize=True)

        _move_with_retry(tmp_path, path)
        logger.debug(f'Reset EXIF orientation to normal: {path}')
        return True

    except Exception as e:
        logger.warning(f'Failed to reset EXIF orientation for {path}: {e}')
        if 'tmp_path' in locals() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return False


def _rotate_jpeg_lossless(path: Path, degrees: float) -> bool:
    """Attempt lossless JPEG rotation using jpegtran.

    Args:
        path: Path to the JPEG file.
        degrees: Rotation angle (must be 90, 180, or 270).

    Returns:
        True if successful, False if jpegtran not available or failed.
    """
    # jpegtran only supports 90, 180, 270
    if degrees not in (90, 180, 270):
        return False

    rotate_arg = str(int(degrees))

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
            # Replace original with rotated version (retry on Windows file locking)
            _move_with_retry(tmp_path, path)
            # Reset EXIF orientation to normal (jpegtran preserves old orientation
            # tag even though pixels are now rotated, which causes double-rotation
            # when code applies exif_transpose later)
            _reset_exif_orientation(path)
            logger.debug(f'Lossless JPEG rotation successful: {path}')
            return True
        else:
            # jpegtran failed
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass  # Best-effort cleanup
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
            try:
                tmp_path.unlink()
            except OSError:
                pass  # Best-effort cleanup
        return False


def _rotate_with_pillow(path: Path, degrees: float) -> bool:
    """Rotate an image using Pillow.

    Args:
        path: Path to the image file.
        degrees: Rotation angle in degrees (clockwise positive).

    Returns:
        True if successful, False otherwise.
    """
    try:
        with Image.open(path) as img:
            # Apply EXIF orientation first
            img = ImageOps.exif_transpose(img)

            # Use fast transpose for 90-degree increments, rotate() for arbitrary angles
            # Note: Pillow's rotate() uses positive = left rotation, so negate degrees
            if degrees == 90:
                rotated = img.transpose(Image.Transpose.ROTATE_270)
            elif degrees == 180:
                rotated = img.transpose(Image.Transpose.ROTATE_180)
            elif degrees == 270:
                rotated = img.transpose(Image.Transpose.ROTATE_90)
            else:
                # Arbitrary rotation - use rotate() with expand=True to fit result
                # Negate degrees because Pillow uses positive = left rotation
                rotated = img.rotate(-degrees, expand=True, resample=Image.Resampling.BICUBIC)

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
            _move_with_retry(tmp_path, path)

            logger.debug(f'Pillow rotation successful: {path}')
            return True

    except Exception as e:
        logger.error(f'Failed to rotate image {path}: {e}')
        if 'tmp_path' in locals() and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass  # Best-effort cleanup
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


# =============================================================================
# RAM CACHE FOR THUMBNAIL BYTES
# =============================================================================

class ThumbnailCache:
    """Thread-safe LRU cache for thumbnail bytes.

    Caches recently-accessed thumbnails in RAM to avoid repeated disk reads.
    Uses a simple LRU eviction policy based on access order.

    Attributes:
        max_size: Maximum cache size in bytes.
    """

    def __init__(self, max_size_bytes: int):
        """Initialise the cache.

        Args:
            max_size_bytes: Maximum cache size in bytes. Set to 0 to disable.
        """
        self._max_size = max_size_bytes
        self._cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()  # LRU: oldest first
        self._current_size = 0
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, checksum: str, size: int) -> bytes | None:
        """Get thumbnail bytes from cache.

        Args:
            checksum: Image checksum.
            size: Thumbnail size.

        Returns:
            Cached bytes, or None if not in cache.
        """
        if self._max_size == 0:
            return None

        key = (checksum, size)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)  # O(1) LRU update
                self._hits += 1
                return self._cache[key]
            self._misses += 1
        return None

    def put(self, checksum: str, size: int, data: bytes) -> None:
        """Add thumbnail bytes to cache.

        Evicts least-recently-used items if cache is full.

        Args:
            checksum: Image checksum.
            size: Thumbnail size.
            data: Thumbnail JPEG bytes.
        """
        if self._max_size == 0:
            return

        key = (checksum, size)
        data_size = len(data)

        # Don't cache items larger than max cache size
        if data_size > self._max_size:
            return

        with self._lock:
            # Evict LRU items until we have room
            while self._current_size + data_size > self._max_size and self._cache:
                _, evicted = self._cache.popitem(last=False)  # O(1) pop oldest
                self._current_size -= len(evicted)

            # Update if already exists
            if key in self._cache:
                self._current_size -= len(self._cache[key])

            # Add/update item at end (most recently used)
            self._cache[key] = data
            self._cache.move_to_end(key)
            self._current_size += data_size

    def remove(self, checksum: str) -> int:
        """Remove all cached thumbnails for a checksum.

        Called when an image is deleted or rotated (checksum changes).

        Args:
            checksum: Image checksum to remove.

        Returns:
            Number of entries removed.
        """
        if self._max_size == 0:
            return 0

        removed = 0
        with self._lock:
            # Remove all sizes for this checksum
            keys_to_remove = [k for k in self._cache if k[0] == checksum]
            for key in keys_to_remove:
                data = self._cache.pop(key, None)
                if data:
                    self._current_size -= len(data)
                    removed += 1
        return removed

    def stats(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with hits, misses, size, count, and hit_rate.
        """
        with self._lock:
            total = self._hits + self._misses
            return {
                'hits': self._hits,
                'misses': self._misses,
                'hit_rate': self._hits / total if total > 0 else 0.0,
                'size_bytes': self._current_size,
                'size_mb': round(self._current_size / (1024 * 1024), 2),
                'count': len(self._cache),
                'max_size_mb': self._max_size // (1024 * 1024),
            }


# =============================================================================
# BULK THUMBNAIL GENERATION
# =============================================================================

def _generate_thumbnails_for_image(
    img: dict,
    thumbnail_dir: Path,
    quality: int,
    sizes: tuple[int, ...],
    max_source_dimension: int = 0,
) -> tuple[int, int, int]:
    """Generate thumbnails for a single image (worker function).

    Args:
        img: Image dict with 'checksum', 'path', and 'basename' keys.
        thumbnail_dir: Root thumbnail cache directory.
        quality: JPEG quality.
        sizes: Tuple of sizes to generate.
        max_source_dimension: Max dimension for draft mode (0 to disable).

    Returns:
        Tuple of (generated, skipped, errors) counts.
    """
    generated = 0
    skipped = 0
    errors = 0

    source_path = Path(img['path'])
    source_exists = None  # Lazy check

    for size in sizes:
        cache_path = get_thumbnail_cache_path(
            img['checksum'], size=size, thumbnail_dir=thumbnail_dir
        )

        if cache_path.exists():
            skipped += 1
            continue

        # Check source exists (once per image)
        if source_exists is None:
            source_exists = source_path.exists()
            if not source_exists:
                logger.warning(f'Source not found: {img["basename"]}')
                errors += len(sizes) - skipped
                break

        if generate_thumbnail(
            source_path, cache_path, size=size,
            quality=quality, max_source_dimension=max_source_dimension
        ):
            generated += 1
        else:
            errors += 1

    return generated, skipped, errors


def generate_missing_thumbnails(
    images: list[dict],
    thumbnail_dir: Path | str,
    quality: int = 85,
    max_workers: int = 8,
    max_source_dimension: int = 0,
) -> dict:
    """Generate thumbnails for images that don't have them cached.

    Generates both 200px and 400px thumbnails for each image using a
    thread pool for parallelization. Skips thumbnails that already exist.

    Args:
        images: List of image dicts with 'checksum', 'path', and 'basename' keys.
        thumbnail_dir: Root thumbnail cache directory.
        quality: JPEG quality for generated thumbnails.
        max_workers: Maximum number of parallel worker threads.
        max_source_dimension: Max dimension for draft mode (0 to disable).

    Returns:
        Dict with 'generated', 'skipped', and 'errors' counts.
    """
    sizes = (200, 400)
    thumbnail_dir = Path(thumbnail_dir)

    total = len(images)
    generated = 0
    skipped = 0
    errors = 0

    logger.info(
        f'Generating thumbnails for {total} images '
        f'(sizes: {sizes}, workers: {max_workers})...'
    )

    interrupted = False
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(
                _generate_thumbnails_for_image,
                img, thumbnail_dir, quality, sizes, max_source_dimension
            ): img
            for img in images
        }

        for future in as_completed(futures):
            try:
                g, s, e = future.result()
                generated += g
                skipped += s
                errors += e

                # Progress every 100 generated
                if generated > 0 and generated % 100 == 0:
                    logger.info(f'Generated {generated} thumbnails...')
            except Exception as exc:
                img = futures[future]
                logger.error(f'Thumbnail generation failed for {img["basename"]}: {exc}')
                errors += len(sizes)

    except KeyboardInterrupt:
        logger.warning('Interrupted! Cancelling pending tasks...')
        interrupted = True
        # Cancel pending futures
        for future in futures:
            future.cancel()
    finally:
        # Shutdown executor (don't wait if interrupted)
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)

    if interrupted:
        logger.info(
            f'Interrupted. Generated {generated}, skipped {skipped} existing, '
            f'{errors} errors before interruption.'
        )
    else:
        logger.info(
            f'Done. Generated {generated}, skipped {skipped} existing, '
            f'{errors} errors.'
        )

    return {'generated': generated, 'skipped': skipped, 'errors': errors}
