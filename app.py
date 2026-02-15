"""Flask backend for the Photonarium image catalogue application.

This module provides the REST API that the frontend communicates with.
It handles HTTP requests and delegates to the imagedb, faces, and
thumbnails modules for database operations and image processing.

Routes:
    /api/images         - Image listing, metadata updates, deletion
    /api/images/:id/thumbnail - Thumbnail retrieval (snapped to 200 or 400px)
    /api/images/:id/full      - Full-resolution image serving
    /api/folders        - Folder registration and removal
    /api/status         - Processing status (indexing, embedding, face queues)
    /api/rescan         - Trigger folder rescan
    /api/duplicates     - Duplicate group retrieval by similarity level
    /api/stats          - Database and cache statistics
    /api/people         - People CRUD, merge, dissolve
    /api/people/:id/thumbnail - Preferred face thumbnail for a person
    /api/faces          - Face listing, batch assign/unassign/suppress
    /api/faces/:id/thumbnail  - Cropped face thumbnail
    /api/events         - Backend event polling (faces_reassessed, etc.)

Example:
    To run the application::

        $ python app.py

    The server will start on port 5000 by default (accessible from your
    local network), using the waitress WSGI server if available. Server
    host and port are configurable in photonarium.yml (stored at the
    OS-appropriate config location — see config.py for details).
"""

# Force UTF-8 for console I/O on Windows.  Without this, Python defaults to
# the console code page (often cp1252 on Western systems) and any log message
# or print() containing a non-ASCII path — e.g. Japanese, Chinese, Cyrillic
# folder names — raises UnicodeEncodeError or prints garbled text.  Must run
# before logging.basicConfig() so the StreamHandler inherits UTF-8.
import sys

if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Disable tokenizers parallelism before any imports.
# Prevents Ctrl+C issues on Windows caused by Rust threads.
import os

os.environ['TOKENIZERS_PARALLELISM'] = 'false'

import argparse
import atexit
import base64
import io
import logging
import math
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path

import orjson
from flask import Flask, Response, abort, request, send_file
from flask import jsonify as flask_jsonify
from PIL import Image, ImageDraw
from werkzeug.exceptions import HTTPException

# flask_cors not needed — frontend is served from the same origin

# Toggle between orjson and stdlib json for testing
USE_ORJSON = True


def jsonify(data):
    """JSON response - uses orjson when USE_ORJSON is True."""
    if USE_ORJSON:
        return Response(orjson.dumps(data), mimetype='application/json')
    else:
        return flask_jsonify(data)


from caption import CaptionGenerator
from faces import (
    batch_identify_faces,
    clear_reassessment_result,
    create_person,
    delete_face,
    delete_people_without_faces,
    delete_person,
    generate_face_thumbnail,
    get_all_faces,
    get_all_people,
    get_face,
    get_face_matches,
    get_face_thumbnail_path,
    get_faces_for_image,
    get_faces_for_images,
    get_faces_for_person,
    get_group_computation_status,
    get_images_with_people,
    get_people_names_bulk,
    get_person,
    get_person_by_name,
    get_reassessment_status,
    reassess_unknown_faces_async,
    revalidate_person_faces,
    search_people,
    search_unknown_faces_semantic,
    suppress_face,
    toggle_face_manual_tag,
    update_face_person,
    update_person,
)
from imagedb import (
    EVENT_FACES_CHANGED,
    EVENT_GROUPS_CHANGED,
    EVENT_IMAGES_CHANGED,
    EVENT_PEOPLE_CHANGED,
    ImageDatabase,
    register_signal_handlers,
)
from rawimage import is_raw_format
from rawimage import open_image as raw_open_image
from thumbnails import (
    ThumbnailCache,
    generate_missing_thumbnails,
    generate_thumbnail,
    get_thumbnail_cache_path,
)
from trash import compute_quality_scores

# Configure logging - set root logger to WARNING, our modules to INFO
logging.basicConfig(
    level=logging.WARNING,  # Default to WARNING for third-party libraries
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# Set our modules to INFO level
for module in ['app', '__main__', 'imagedb', 'faces', 'thumbnails', 'duplicates', 'config', 'timestamps']:
    logging.getLogger(module).setLevel(logging.INFO)

app = Flask(__name__, static_folder='static', static_url_path='')

# CORS is not needed since the frontend is served from the same origin.
# If you need to run a separate frontend dev server, uncomment and restrict
# origins appropriately:
# CORS(app, origins=['http://localhost:5000', 'http://127.0.0.1:5000'])


# =============================================================================
# Configuration — resolved in __main__ before server starts
# =============================================================================

DATABASE_PATH = os.environ.get('PHOTONARIUM_DB', 'photonarium.db')
THUMBNAIL_CACHE_DIR = os.environ.get('PHOTONARIUM_THUMBNAILS', '.thumbnails')

# The loaded Config object — set once during startup, read-only afterwards.
# Stored at module level so get_db() can pass it to ImageDatabase without
# re-loading from disk.
_config: 'Config | None' = None

# The resolved config file path — set once during startup.
# Used by /api/config/reveal to locate the file on disk.
_config_file_path: str | None = None


# =============================================================================
# Database Instance
# =============================================================================

db: ImageDatabase | None = None
_run_scan = False  # Set via command-line args in __main__
_run_face_detection = False  # Set via command-line args in __main__
_run_face_grouping = False  # Set via command-line args in __main__

# Track face thumbnails currently being regenerated to avoid concurrent attempts
_face_thumb_regenerating: set[str] = set()
_face_thumb_regen_lock = threading.Lock()

# Cache for /api/images full response (avoids slow SQLite reads on every request)
_images_cache: dict | None = None
_images_cache_lock = threading.Lock()

# Caption generator (lazy-loaded to avoid startup delay)
_caption_generator: CaptionGenerator | None = None


def _get_images_cache():
    """Get cached images response if available."""
    with _images_cache_lock:
        return _images_cache


def _set_images_cache(epoch: str, json_bytes: bytes):
    """Cache the images JSON response."""
    global _images_cache
    with _images_cache_lock:
        _images_cache = {'epoch': epoch, 'bytes': json_bytes}


def invalidate_images_cache():
    """Invalidate the images cache (call when images change)."""
    global _images_cache
    with _images_cache_lock:
        _images_cache = None


_caption_generator_lock = threading.Lock()


def get_caption_generator() -> CaptionGenerator:
    """Get the caption generator, initializing if necessary.

    Thread-safe: uses a lock to prevent duplicate model loading when
    two concurrent caption requests arrive before the model is loaded.
    """
    global _caption_generator
    if _caption_generator is not None:
        return _caption_generator
    with _caption_generator_lock:
        # Double-checked locking: another thread may have initialised while we waited
        if _caption_generator is None:
            config = get_db().config
            _caption_generator = CaptionGenerator(
                model_name=config.caption_model,
                max_length=config.caption_max_length,
                min_length=config.caption_min_length,
                num_beams=config.caption_num_beams,
                british_english=config.caption_british_english,
            )
    return _caption_generator


def get_db() -> ImageDatabase:
    """Get the database instance, initializing if necessary."""
    global db
    if db is None:
        logger.info('Initialising ImageDatabase...')
        db = ImageDatabase(
            db_path=DATABASE_PATH,
            thumbnail_dir=THUMBNAIL_CACHE_DIR,
            config=_config,
            auto_start=True,
            run_scan=_run_scan,
            run_face_detection=_run_face_detection,
            run_face_grouping=_run_face_grouping,
        )
        register_signal_handlers(db)
        logger.info('ImageDatabase initialised')
        # Pre-populate images cache for fast first request
        _prepopulate_images_cache(db)
    return db


def _prepopulate_images_cache(database: ImageDatabase):
    """Pre-populate the images cache during startup."""
    t0 = time.perf_counter()
    images = database.get_all_images_lightweight()
    epoch = database.get_current_epoch()
    data = {'success': True, 'data': {'epoch': epoch, 'images': images}}
    json_bytes = orjson.dumps(data)
    _set_images_cache(epoch, json_bytes)
    elapsed = time.perf_counter() - t0
    logger.info(
        f'Images cache pre-populated: {len(images)} images, {len(json_bytes) // 1024 // 1024}MB, {elapsed * 1000:.0f}ms'
    )


def shutdown_db():
    """Shutdown the database on application exit."""
    global db
    if db is not None:
        logger.info('Shutting down ImageDatabase...')
        db.close()
        db = None
        logger.info('ImageDatabase shut down')


# Register shutdown handler
atexit.register(shutdown_db)


# =============================================================================
# Thumbnail RAM Cache Instance
# =============================================================================

# Global thumbnail cache instance (initialized lazily)
_thumbnail_cache: ThumbnailCache | None = None
_thumbnail_cache_lock = threading.Lock()


def get_thumbnail_cache() -> ThumbnailCache:
    """Get the thumbnail cache instance, initializing if necessary.

    Thread-safe: uses a lock to prevent duplicate initialization.
    """
    global _thumbnail_cache
    if _thumbnail_cache is not None:
        return _thumbnail_cache
    with _thumbnail_cache_lock:
        if _thumbnail_cache is None:
            config = get_db().config
            max_bytes = config.thumbnail_cache_size_mb * 1024 * 1024
            _thumbnail_cache = ThumbnailCache(max_bytes)
            if max_bytes > 0:
                logger.info(f'Thumbnail cache initialized: {config.thumbnail_cache_size_mb}MB')
            else:
                logger.info('Thumbnail cache disabled (size=0)')
    return _thumbnail_cache


# =============================================================================
# Helper Functions
# =============================================================================


def success_response(data=None, message=None):
    """Build a successful JSON response.

    Args:
        data: The data to include in the response. Can be any JSON-serializable
            type (dict, list, str, int, etc.).
        message: Optional success message string.

    Returns:
        A Flask Response object with JSON content and 200 status code.
    """
    response = {'success': True}
    if data is not None:
        response['data'] = data
    if message:
        response['message'] = message
    return jsonify(response)


def error_response(message, status_code=400):
    """Build an error JSON response.

    Args:
        message: Error message string to include in the response.
        status_code: HTTP status code. Defaults to 400 (Bad Request).

    Returns:
        A tuple of (Flask Response object, status code).
    """
    return jsonify({'success': False, 'error': message}), status_code


# =============================================================================
# Static File Serving (Development)
# =============================================================================


@app.route('/')
def serve_index():
    """Serve the main application HTML file.

    In production, this would typically be handled by a reverse proxy
    like nginx. This route is primarily for development convenience.

    Returns:
        The index.html file contents.
    """
    return send_file('static/index.html')


# =============================================================================
# Image Endpoints
# =============================================================================


@app.route('/api/images', methods=['GET'])
def get_images():
    """List images with support for incremental updates and people filtering.

    If 'since' query parameter is provided, returns only changes since that
    epoch (timestamp), allowing efficient incremental updates. Otherwise
    returns all images.

    Query Parameters:
        since: Optional ISO timestamp. If provided, returns delta update with
               only images changed since that time.
        people: Optional comma-separated list of person IDs. If provided,
               only returns images containing ALL specified people (AND logic).

    Returns:
        Without 'since': JSON object with 'epoch' and 'images' array.
        With 'since': JSON object with 'epoch', 'updated' array, and
                      'deleted_ids' array for incremental sync.
    """
    since = request.args.get('since')
    people_param = request.args.get('people', '').strip()

    # Parse people filter
    person_ids = []
    if people_param:
        person_ids = [p.strip() for p in people_param.split(',') if p.strip()]

    if since:
        # Delta update - return only changes since the given epoch
        delta = get_db().get_images_delta(since)

        # Apply people filter to delta if specified
        if person_ids:
            db = get_db()
            matching_image_ids = set(get_images_with_people(db.conn, person_ids))
            if 'updated' in delta:
                delta['updated'] = [img for img in delta['updated'] if img['id'] in matching_image_ids]

        return success_response(delta)
    else:
        # Full load - use cached response if available
        db = get_db()

        if person_ids:
            # Filter by people - can't use cache
            matching_image_ids = set(get_images_with_people(db.conn, person_ids))
            all_images = db.get_all_images_lightweight()
            images = [img for img in all_images if img['id'] in matching_image_ids]
            epoch = db.get_current_epoch()
            return success_response({'epoch': epoch, 'images': images})
        else:
            # Use cached JSON bytes if epoch matches
            epoch = db.get_current_epoch()
            cached = _get_images_cache()
            if cached and cached['epoch'] == epoch:
                return Response(cached['bytes'], mimetype='application/json')

            # Cache miss - build and cache response (with success wrapper for consistency)
            images = db.get_all_images_lightweight()
            data = {'success': True, 'data': {'epoch': epoch, 'images': images}}
            json_bytes = orjson.dumps(data)
            _set_images_cache(epoch, json_bytes)
            return Response(json_bytes, mimetype='application/json')


@app.route('/api/images/<image_id>', methods=['GET'])
def get_image(image_id):
    """Get metadata for a single image.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON object with full image metadata, or 404 if not found.
    """
    image = get_db().get_image(image_id)
    if image is None:
        return error_response('Image not found', 404)
    return success_response(image)


@app.route('/api/images/<image_id>/exif', methods=['GET'])
def get_image_exif(image_id):
    """Get EXIF metadata for a single image.

    Loaded lazily by the frontend when the metadata modal opens,
    separate from the main image endpoint to keep responses lightweight.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON object with exif_data dict, or 404 if image not found.
    """
    exif_data = get_db().get_image_exif(image_id)
    return success_response({'exif_data': exif_data})


@app.route('/api/images/<image_id>', methods=['POST'])
def update_image(image_id):
    """Update editable fields for an image.

    Currently supports updating description and rating fields.
    Other fields are computed and cannot be modified via API.

    Args:
        image_id: The unique identifier of the image.

    Request Body:
        JSON object with optional fields:
            - description: New description text
            - rating: New rating emoji string

    Returns:
        JSON object with updated image metadata, or 404 if not found.
    """
    data = request.get_json()
    if not data:
        return error_response('No data provided')

    # Only allow updating user-editable fields
    allowed_updates = {}
    if 'description' in data:
        allowed_updates['description'] = data['description']
    if 'rating' in data:
        allowed_updates['rating'] = data['rating']
    if 'timestamp' in data:
        allowed_updates['timestamp'] = data['timestamp']
        # User-assigned timestamp has highest confidence (0)
        allowed_updates['timestamp_confidence'] = 0

    if not allowed_updates:
        return error_response('No valid fields to update')

    db = get_db()
    image = db.update_image(image_id, allowed_updates)
    if image is None:
        return error_response('Image not found', 404)

    # Broadcast for other clients
    db.event_queue.emit(EVENT_IMAGES_CHANGED, {'updated_ids': [image_id]})

    return success_response(image)


@app.route('/api/images/<image_id>/generate-caption', methods=['POST'])
def generate_caption(image_id):
    """Generate an AI caption for an image.

    Uses the BLIP model to generate a natural language description
    of the image. Runs in a daemon thread to allow graceful shutdown
    during long-running PyTorch operations.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON object with:
            - caption: The generated caption text
        Or error if image not found or generation fails.
    """
    database = get_db()
    image = database.get_image(image_id)
    if image is None:
        return error_response('Image not found', 404)

    path = image.get('path')
    if not path:
        return error_response('Image path not found', 404)

    try:
        generator = get_caption_generator()

        # Run caption generation in a daemon thread so it can be abandoned
        # on shutdown (PyTorch CUDA operations block Python signal handlers)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='caption') as executor:
            future = executor.submit(generator.generate, path)

            # Poll for completion with short timeouts to allow shutdown checks
            while True:
                try:
                    caption = future.result(timeout=0.5)
                    break
                except FuturesTimeoutError:
                    # Check if shutdown was requested
                    if database.is_closed:
                        logger.info('Caption generation interrupted by shutdown')
                        return error_response('Server shutting down', 503)
                    # Continue waiting
                    continue

    except Exception as e:
        logger.exception(f'Failed to generate caption for image {image_id}')
        return error_response(f'Caption generation failed: {e}', 500)

    if caption is None:
        return error_response('Failed to generate caption', 500)

    return success_response({'caption': caption})


@app.route('/api/images/trash', methods=['POST'])
def trash_images():
    """Enqueue images for background trashing.

    Immediately soft-deletes from the database and invalidates caches.
    File moves to the trash directory happen asynchronously via the
    background TrashWorker thread.  Returns as soon as the enqueue is done.

    Request Body:
        JSON object with:
            - image_ids: Array of image IDs to trash

    Returns:
        JSON object with:
            - enqueued: Array of image IDs accepted for trashing
            - errors: Object mapping image_id to error message
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('At least one image_id is required')
    if not isinstance(image_ids, list):
        return error_response('image_ids must be an array')

    db = get_db()

    try:
        result = db.enqueue_trash(image_ids)
    except ValueError as e:
        return error_response(str(e))

    # Invalidate thumbnail RAM cache for enqueued images (fast dict removals)
    if result['enqueued']:
        cache = get_thumbnail_cache()
        for image_id in result['enqueued']:
            checksum = db.get_checksum(image_id)
            if checksum:
                cache.remove(checksum)

    # enqueue_trash() already emits EVENT_IMAGES_CHANGED and EVENT_GROUPS_CHANGED

    return success_response(result)


@app.route('/api/images/<image_id>/thumbnail', methods=['GET'])
def get_thumbnail(image_id):
    """Get a thumbnail for an image.

    Only two canonical sizes are cached: 200px and 400px. The requested
    size is snapped to the nearest canonical size. The frontend uses CSS
    to resize the thumbnail to the exact display size.

    Thumbnails are served from a RAM cache when available, falling back
    to disk. This eliminates filesystem reads for frequently-accessed
    thumbnails.
    """
    requested_size = request.args.get('size', 200, type=int)
    size = 400 if requested_size > 300 else 200

    db = get_db()
    cache = get_thumbnail_cache()

    # Fast path: checksum from RAM cache
    checksum = db.get_checksum(image_id)
    if checksum is None:
        abort(404)

    # Check RAM cache first - no disk access needed
    cached_bytes = cache.get(checksum, size)
    if cached_bytes is not None:
        return Response(cached_bytes, mimetype='image/jpeg')

    # Slow path: read from disk
    thumbnail_path = get_thumbnail_cache_path(checksum, size, db.thumbnail_dir)

    if not thumbnail_path.exists():
        # Need to generate - get source path from DB
        info = db.get_image_thumbnail_info(image_id)
        if info is None:
            abort(404)
        _, source_path = info
        if not generate_thumbnail(
            source_path, thumbnail_path, size, db.config.thumbnail_quality, db.config.max_image_dimension
        ):
            abort(404)

    # Read from disk and cache
    try:
        with open(thumbnail_path, 'rb') as f:
            data = f.read()
        cache.put(checksum, size, data)
        return Response(data, mimetype='image/jpeg')
    except OSError:
        abort(404)


@app.route('/api/images/<image_id>/histogram', methods=['GET'])
def get_histogram_images(image_id):
    """Get histogram images for all color channels.

    Returns a JSON object with base64-encoded PNG data URLs for each
    channel (red, green, blue). Each image is 1000x1000 with transparent
    background.

    Uses the thumbnail RAM cache when available to avoid disk reads.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON with {r: "data:image/png;base64,...", g: "...", b: "..."}
        or 404 if image not found.
    """
    try:
        db = get_db()
        cache = get_thumbnail_cache()
        size = 400

        # Get checksum for cache lookup
        checksum = db.get_checksum(image_id)
        if checksum is None:
            abort(404)

        # Try RAM cache first
        cached_bytes = cache.get(checksum, size)
        if cached_bytes is not None:
            # Load from cached bytes
            thumb = Image.open(io.BytesIO(cached_bytes))
        else:
            # Fall back to disk
            thumbnail_path = get_thumbnail_cache_path(checksum, size, db.thumbnail_dir)

            if not thumbnail_path.exists():
                # Generate thumbnail if missing
                info = db.get_image_thumbnail_info(image_id)
                if info is None:
                    abort(404)
                _, source_path = info
                if not generate_thumbnail(
                    source_path, thumbnail_path, size, db.config.thumbnail_quality, db.config.max_image_dimension
                ):
                    abort(404)

            # Read from disk and cache
            with open(thumbnail_path, 'rb') as f:
                data = f.read()
            cache.put(checksum, size, data)
            thumb = Image.open(io.BytesIO(data))

        # Compute histograms from thumbnail
        thumb_rgb = thumb.convert('RGB')
        histogram = thumb_rgb.histogram()
        r_hist = histogram[0:256]
        g_hist = histogram[256:512]
        b_hist = histogram[512:768]

        # Generate histogram images
        hist_size = 1000

        def create_histogram_image(hist, color):
            img = Image.new('RGBA', (hist_size, hist_size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            max_val = max(hist) if max(hist) > 0 else 1
            bin_width = hist_size / 256
            for i, count in enumerate(hist):
                if count == 0:
                    continue
                x1 = int(i * bin_width)
                x2 = int((i + 1) * bin_width)
                height = int((count / max_val) * hist_size)
                y1 = hist_size - height
                draw.rectangle([x1, y1, x2, hist_size], fill=color)
            return img

        red_img = create_histogram_image(r_hist, (255, 0, 0, 255))
        green_img = create_histogram_image(g_hist, (0, 255, 0, 255))
        blue_img = create_histogram_image(b_hist, (0, 0, 255, 255))

        def img_to_data_url(img):
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            b64 = base64.b64encode(buffer.getvalue()).decode('ascii')
            return f'data:image/png;base64,{b64}'

        return success_response(
            {
                'r': img_to_data_url(red_img),
                'g': img_to_data_url(green_img),
                'b': img_to_data_url(blue_img),
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Error generating histogram for {image_id}: {e}')
        traceback.print_exc()
        abort(500)


@app.route('/api/images/<image_id>/full', methods=['GET'])
def get_full_image(image_id):
    """Get the full-resolution image file.

    Serves the original image file directly. The response includes
    appropriate caching headers.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        Original image file with appropriate MIME type, or 404 if not found.
    """
    image = get_db().get_image(image_id)
    if image is None:
        abort(404)

    path = image['path']
    if not os.path.exists(path):
        return error_response('Image file not found on disk', 404)

    # Browsers cannot render camera RAW formats natively, so we decode
    # the RAW file on the fly and serve it as JPEG
    if is_raw_format(path):
        try:
            img = raw_open_image(path).convert('RGB')
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=92)
            buf.seek(0)
            return send_file(buf, mimetype='image/jpeg')
        except Exception as e:
            logger.error(f'Failed to convert RAW image {path}: {e}')
            return error_response('Failed to decode RAW image', 500)

    return send_file(path)


def _reveal_path(path):
    """Open a file or folder in the system file manager.

    For files, selects/highlights the file in its containing folder.
    For directories, opens them directly.

    Args:
        path: Absolute filesystem path to reveal.

    Raises:
        subprocess.CalledProcessError: If the file manager command fails
            (Windows ``explorer`` is called with ``check=False`` because it
            returns non-zero even on success).
    """
    if os.path.isdir(path):
        if sys.platform == 'win32':
            subprocess.run(['explorer', path], check=False)
        elif sys.platform == 'darwin':
            subprocess.run(['open', path], check=True)
        else:
            subprocess.run(['xdg-open', path], check=True)
    else:
        if sys.platform == 'win32':
            subprocess.run(['explorer', '/select,', path], check=False)
        elif sys.platform == 'darwin':
            subprocess.run(['open', '-R', path], check=True)
        else:
            folder = os.path.dirname(path)
            subprocess.run(['xdg-open', folder], check=True)


@app.route('/api/reveal', methods=['POST'])
def reveal():
    """Open the file manager for a known application resource.

    Accepts a JSON body with ``target`` identifying what to reveal:

    - ``image``: Reveal an image file (requires ``id``).
    - ``config``: Reveal the configuration YAML file.
    - ``trash``: Open the trash directory.

    The frontend never sends raw paths — the backend resolves them
    server-side from its own state, keeping the allowlist closed.

    Returns:
        Success response, or 404/400 on errors.
    """
    data = request.get_json() or {}
    target = data.get('target')

    if target == 'image':
        image_id = data.get('id')
        if not image_id:
            return error_response('Missing image id')
        image = get_db().get_image(image_id)
        if image is None:
            return error_response('Image not found', 404)
        path = image['path']
        if not os.path.exists(path):
            return error_response('Image file not found on disk', 404)

    elif target == 'config':
        path = _config_file_path
        if not path:
            from config import get_default_config_path

            path = str(get_default_config_path())
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return error_response('Configuration file not found', 404)

    elif target == 'trash':
        path = str(get_db().trash_dir)
        # Trash directory may not exist yet — that's fine, nothing to open
        if not os.path.isdir(path):
            return error_response('Trash directory does not exist', 404)

    else:
        return error_response('Unknown reveal target')

    try:
        _reveal_path(path)
        return success_response(message='Folder opened')
    except Exception as e:
        logger.exception('Failed to reveal %s', target)
        return error_response(f'Failed to open folder: {e!s}', 500)


@app.route('/api/images/rotate', methods=['POST'])
def rotate_images():
    """Rotate one or more image files.

    Performs lossless rotation for JPEG files when possible (using jpegtran),
    otherwise uses Pillow. Updates the database with new checksum, size, and
    dimensions. Old thumbnails are deleted and will regenerate on demand.

    Rotations are processed in parallel using the configured thread pool.

    Request Body:
        JSON object with:
            - image_ids: Array of image IDs to rotate
            - degrees: Rotation angle in degrees (clockwise positive).
                       Common values: 90 (right), 180, 270 or -90 (left).

    Returns:
        JSON object with:
            - results: Object mapping image_id to success boolean
            - rotated: Array of successfully rotated image IDs
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    if 'degrees' not in data:
        return error_response('degrees is required')

    degrees = data['degrees']
    if not isinstance(degrees, (int, float)):
        return error_response('degrees must be a number')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('At least one image_id is required')

    if not isinstance(image_ids, list):
        return error_response('image_ids must be an array')

    results = get_db().rotate_images(image_ids, degrees)

    # Invalidate thumbnail RAM cache for old checksums
    old_checksums = results.pop('old_checksums', [])
    if old_checksums:
        cache = get_thumbnail_cache()
        for checksum in old_checksums:
            cache.remove(checksum)

    return success_response(results)


@app.route('/api/images/people-names', methods=['GET'])
def get_images_people_names():
    """Get people names for all images in a single bulk query.

    Used for "sort by people" functionality in the gallery.
    Returns a mapping of image_id to comma-separated people names.

    Returns:
        JSON object mapping image_id to names string (sorted alphabetically).
    """
    db = get_db()
    names = get_people_names_bulk(db.conn)
    return success_response(names)


# =============================================================================
# Folder Endpoints
# =============================================================================


@app.route('/api/folders', methods=['GET'])
def get_folders():
    """List all registered image source folders.

    Returns folders along with the count of images from each folder
    currently in the database.

    Returns:
        JSON array of folder objects, each containing:
            - path: Folder path
            - count: Number of images from this folder
    """
    folders = get_db().get_folders()
    return success_response(folders)


@app.route('/api/pick-folder', methods=['POST'])
def pick_folder():
    """Open a native folder picker dialog and return the selected path.

    This uses tkinter to show a native OS folder selection dialog.
    The dialog runs in a separate thread to avoid blocking the server.

    Note: This endpoint only works on desktop environments with a display.
    On headless servers, it will fail silently and return null.

    Returns:
        JSON object with:
            - path: Selected folder path, or null if cancelled/failed
    """
    import tkinter as tk
    from tkinter import filedialog

    selected_path = None
    dialog_timeout_ms = 5 * 60 * 1000  # 5 minutes in milliseconds

    def show_dialog():
        nonlocal selected_path
        root = None
        try:
            # Create a hidden root window
            root = tk.Tk()
            root.withdraw()  # Hide the root window
            root.attributes('-topmost', True)  # Bring dialog to front

            # Schedule auto-close after timeout to prevent orphaned dialogs
            def on_timeout():
                if root:
                    root.destroy()

            root.after(dialog_timeout_ms, on_timeout)

            # Show folder selection dialog
            path = filedialog.askdirectory(
                title='Select Image Folder',
                mustexist=True,
            )

            if path:
                selected_path = path
        except Exception as e:
            # Handle tkinter failures (e.g., no display on headless systems)
            logger.debug(f'Folder picker failed: {e}')
        finally:
            if root:
                try:
                    root.destroy()
                except Exception:
                    pass  # Already destroyed by timeout or error

    # Run dialog in a daemon thread (won't block process shutdown)
    dialog_thread = threading.Thread(target=show_dialog, daemon=True)
    dialog_thread.start()
    dialog_thread.join(timeout=300)  # 5 minute timeout

    return success_response({'path': selected_path})


@app.route('/api/folders', methods=['POST'])
def add_folder():
    """Register a new image source folder.

    Adds a folder to the list of monitored directories and queues its
    images for processing.

    Request Body:
        JSON object with:
            - path: Absolute path to the folder

    Returns:
        Success response with folder info, or error if path is invalid.
    """
    data = request.get_json()
    if not data or 'path' not in data:
        return error_response('Path is required')

    path = data['path']

    # Validate path exists and is a directory
    if not os.path.isabs(path):
        return error_response('Path must be absolute')
    if not os.path.exists(path):
        return error_response('Path does not exist')
    if not os.path.isdir(path):
        return error_response('Path is not a directory')

    try:
        folder = get_db().add_folder(path)
        if folder is None:
            return error_response('Folder already registered')
        return success_response(folder)
    except ValueError as e:
        return error_response(str(e))


@app.route('/api/folders/<path:folder_path>', methods=['DELETE'])
def remove_folder(folder_path):
    """Remove a folder and all its images from the database.

    This removes the folder registration and marks all image entries
    that originated from this folder as deleted. Original image files
    are not deleted.

    Args:
        folder_path: URL-encoded path of the folder to remove.

    Returns:
        Success response, or 404 if folder not registered.
    """
    # The path comes URL-encoded, Flask decodes it automatically
    # Prepend '/' for absolute paths on Unix (Windows paths start with drive letter)
    if not folder_path.startswith('/') and ':' not in folder_path:
        folder_path = '/' + folder_path

    success = get_db().remove_folder(folder_path)
    if not success:
        return error_response('Folder not found', 404)
    return success_response(message='Folder removed')


# =============================================================================
# Status Endpoints
# =============================================================================


@app.route('/api/status', methods=['GET'])
def get_status():
    """Get the current processing status of the database.

    Returns the status of background processing threads, including
    queue sizes and Phase 4 post-processing statuses.

    Returns:
        JSON object with:
            - status: 'up_to_date' or 'updating'
            - indexing_queue: Number of images awaiting indexing
            - embedding_queue: Number of images awaiting image embedding
            - face_queue: Number of images awaiting face detection
            - total_images: Current count of images in database
            - face_detection_enabled: Whether face detection is enabled
            - duplicates: (if computing) {status, level} for duplicate detection
            - face_grouping: (if computing) {status} for face grouping
            - face_embeddings: (if computing) {status, current, total} for face CLIP embeddings
            - face_reassessment: {in_progress, completed, matched_count, person_id}
    """
    status = get_db().get_processing_status()

    # Add face reassessment status
    reassess = get_reassessment_status()
    last_result = reassess.get('last_result')
    status['face_reassessment'] = {
        'in_progress': reassess['in_progress'],
        'completed': last_result is not None and not reassess['in_progress'],
        'matched_count': last_result.get('matched_count') if last_result else None,
        'person_id': last_result.get('person_id') if last_result else None,
    }

    # Add trash status
    db = get_db()
    status['trash_enabled'] = db.is_trash_enabled()
    trash_progress = db.get_trash_progress()
    if trash_progress:
        status['trash_progress'] = trash_progress

    return success_response(status)


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get frontend configuration values.

    Returns configuration values needed by the frontend for thumbnail
    loading behaviour and quality scoring weights.

    Returns:
        JSON object with thumbnail settings (concurrent_requests, extra_rows,
        timeout_ms, scroll_throttle_ms) and quality scoring weights
        (quality_weight_aesthetic, quality_weight_sharpness, quality_weight_pixels,
        quality_weight_bpp, quality_alpha, nima_enabled).
    """
    db = get_db()
    config = db.config
    return success_response(
        {
            'thumbnail_concurrent_requests': config.thumbnail_concurrent_requests,
            'thumbnail_extra_rows': config.thumbnail_extra_rows,
            'thumbnail_timeout_ms': config.thumbnail_timeout_ms,
            'thumbnail_scroll_throttle_ms': config.thumbnail_scroll_throttle_ms,
            'quality_weight_aesthetic': config.quality_weight_aesthetic,
            'quality_weight_sharpness': config.quality_weight_sharpness,
            'quality_weight_pixels': config.quality_weight_pixels,
            'quality_weight_bpp': config.quality_weight_bpp,
            'quality_alpha': config.quality_alpha,
            'nima_enabled': config.nima_enabled,
            'trash_dir': str(db.trash_dir),
        }
    )


@app.route('/api/config/schema', methods=['GET'])
def get_config_schema_endpoint():
    """Get the full configuration schema for the settings editor.

    Loads the config fresh from the YAML file on disk so that values
    saved-but-not-yet-restarted are reflected correctly in the dialog.
    Falls back to the live in-memory config if the file can't be read.

    Returns:
        JSON with ``sections`` array and ``config_path`` string.
    """
    from config import get_config_schema, load_config

    # Read from disk so the dialog shows the on-disk state, not the
    # stale in-memory state from when the server started.
    config_path = _config_file_path or None
    try:
        disk_config = load_config(config_path)
    except Exception:
        logger.warning('Failed to reload config from disk, using in-memory values')
        disk_config = _config

    schema = get_config_schema(disk_config)
    schema['config_path'] = _config_file_path or ''
    return success_response(schema)


@app.route('/api/config/save', methods=['POST'])
def save_config_endpoint():
    """Save updated configuration values to disk.

    Accepts a dict of ``{key: value}`` pairs, coerces types from JSON to
    match the Config dataclass, validates via the Config constructor, and
    writes to the YAML file.  Does NOT hot-reload the running process —
    a restart is required for changes to take effect.

    Request Body:
        ``{values: {field_name: value, ...}}``

    Returns:
        Success response on valid save, or 400 with the validation error
        message if any value is out of range or cross-field checks fail.
    """
    from dataclasses import fields as dc_fields

    from config import Config, get_default_config_path, save_config

    data = request.get_json(silent=True)
    if not data or 'values' not in data:
        return error_response('Request must include "values" object')

    values = data['values']
    if not isinstance(values, dict):
        return error_response('"values" must be an object')

    # Build a mapping of field name → expected Python type for coercion
    type_map = {f.name: f.type for f in dc_fields(Config)}

    kwargs: dict = {}
    for key, raw in values.items():
        if key not in type_map:
            continue  # Skip unknown keys silently

        expected = type_map[key]
        try:
            if expected == 'bool':
                # JSON booleans come through fine; strings need conversion
                if isinstance(raw, str):
                    kwargs[key] = raw.lower() in ('true', '1', 'yes')
                else:
                    kwargs[key] = bool(raw)
            elif expected == 'int':
                kwargs[key] = int(raw)
            elif expected == 'float':
                kwargs[key] = float(raw)
            elif expected == 'str':
                kwargs[key] = str(raw) if raw is not None else ''
            elif expected == 'set[str]':
                # Frontend sends a list of strings
                if isinstance(raw, list):
                    kwargs[key] = set(raw)
                elif isinstance(raw, str):
                    # Textarea fallback: one value per line
                    kwargs[key] = {line.strip() for line in raw.splitlines() if line.strip()}
                else:
                    kwargs[key] = set(raw)
            else:
                kwargs[key] = raw
        except (ValueError, TypeError) as e:
            return error_response(f'Invalid value for {key}: {e}')

    # Construct a Config to trigger validation
    try:
        new_config = Config(**kwargs)
    except ValueError as e:
        return error_response(str(e))

    # Write to disk
    config_path = _config_file_path
    if not config_path:
        config_path = str(get_default_config_path())

    try:
        save_config(new_config, config_path)
    except Exception as e:
        logger.exception('Failed to save configuration')
        return error_response(f'Failed to write config file: {e}', 500)

    return success_response(message='Settings saved. Restart Photonarium for changes to take effect.')


@app.route('/api/rescan', methods=['POST'])
def rescan_folders():
    """Trigger a rescan of all registered folders.

    This queues all registered folders for re-indexing. The background
    ingestion thread will process new and changed files. Use GET /api/status
    to monitor progress.

    Returns:
        Success response confirming rescan has been queued.
    """
    get_db().queue_rescan_all()
    return success_response(message='Rescan queued')


# =============================================================================
# Duplicates Endpoints
# =============================================================================


@app.route('/api/duplicates', methods=['GET'])
def get_duplicates():
    """Get duplicate image groups at a specified similarity level.

    Duplicate groups are pre-computed during scanning. This endpoint
    returns lightweight group data for efficient grid display.

    Query Parameters:
        level: Similarity level (0-3). Defaults to 0.
            - 0: Identical (same SHA256 checksum)
            - 1: Near-identical (similar perceptual hash)
            - 2: Similar (high OpenCLIP similarity)
            - 3: Related (lower OpenCLIP threshold)
        since: Optional epoch timestamp. If provided and matches current
               epoch, returns empty groups (no changes).

    Returns:
        JSON object with:
            - groups: Array of lightweight duplicate groups, each containing:
                - group_hash: Unique group identifier
                - count: Number of images in the group
                - image_ids: Array of image IDs in the group
                - best_image: Object with id and basename for thumbnail
            - status: Computation status for this level
                - 'pending': Not yet computed
                - 'computing': Currently being computed
                - 'done': Computation finished
            - epoch: Current epoch timestamp for caching
    """
    level = request.args.get('level', 0, type=int)
    since = request.args.get('since')

    # Validate level (0-3 = auto-detected, 4 = directories, 5 = custom groups)
    if level < 0 or level > 5:
        return error_response('Level must be between 0 and 5')

    db = get_db()
    status = db.get_duplicate_status().get(level, 'done')
    epoch = db.get_duplicate_epoch()

    # Named groups (levels 4-5) skip epoch caching — always return fresh data
    if level < 4 and since and since == epoch and status == 'done':
        return success_response(
            {
                'groups': [],
                'status': status,
                'epoch': epoch,
                'unchanged': True,
            }
        )

    # Return lightweight group data
    groups = db.get_duplicate_groups_lightweight(level)
    return success_response(
        {
            'groups': groups,
            'status': status,
            'epoch': epoch,
        }
    )


@app.route('/api/duplicates/sort-semantic', methods=['POST'])
def sort_duplicates_semantic():
    """Get semantic similarity scores for duplicate group ordering.

    Takes a text query and a list of image IDs (one per group), returns
    similarity scores for each image to enable semantic sorting of groups.

    Request Body:
        JSON object with:
            - query: Text query to sort by similarity to
            - image_ids: Array of image IDs to score (typically best_image from each group)

    Returns:
        JSON object with:
            - scores: Array of {image_id, score} objects sorted by descending score
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    query = data.get('query', '').strip()
    if not query:
        return error_response('Query is required')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('image_ids array is required')
    if not isinstance(image_ids, list):
        return error_response('image_ids must be an array')

    try:
        scores = get_db().get_semantic_scores_for_images(query, image_ids)
        return success_response({'scores': scores})
    except Exception as e:
        logger.exception('Semantic sort failed')
        return error_response(f'Semantic sort failed: {e!s}', 500)


@app.route('/api/duplicates/prune', methods=['POST'])
def prune_duplicates():
    """Prune duplicate groups by trashing lower-quality images.

    For each group at the specified level, ranks images by a composite
    quality score (aesthetic, sharpness, resolution, compression quality)
    and keeps the top N, moving the rest to the trash directory.

    The quality scoring algorithm mirrors the frontend Quality sort
    (weighted percentile composite of NIMA+LAION aesthetic, sharpness,
    pixel count, and bits-per-pixel).

    Supports two mutually exclusive modes:
    - **Keep mode** (default): keep the best N images, trash the rest.
    - **Trash mode**: trash the worst N images, keep the rest.

    Request Body:
        JSON object with:
            - level: Similarity level (0-3, auto-detected only)
            - keep_count: Number of images to keep per group (default: 1)
            - keep_percent: Percentage of images to keep (overrides keep_count).
                          Rounded up so at least 1 image is always kept.
            - trash_count: Number of worst images to trash per group.
                          Mutually exclusive with keep_count/keep_percent.
            - trash_percent: Percentage of worst images to trash per group.
                           Mutually exclusive with keep_count/keep_percent.
            - group_hashes: Optional array of specific group hashes to prune.
                          If omitted, prunes all groups at the level.

    Returns:
        JSON object with:
            - trashed_count: Total number of images moved to trash
            - group_count: Number of groups that were pruned
            - errors: Array of error messages (if any)
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    level = data.get('level')
    if level is None:
        return error_response('level is required')
    if not isinstance(level, int) or level < 0 or level > 3:
        return error_response('level must be 0-3 (auto-detected duplicates only)')

    keep_count = data.get('keep_count')
    keep_percent = data.get('keep_percent')
    trash_count = data.get('trash_count')
    trash_percent = data.get('trash_percent')
    group_hashes = data.get('group_hashes')

    # Validate mutual exclusion: keep and trash params cannot both be provided
    has_keep = keep_count is not None or keep_percent is not None
    has_trash = trash_count is not None or trash_percent is not None
    if has_keep and has_trash:
        return error_response('Cannot specify both keep and trash parameters')

    # Validate individual params
    if keep_count is not None and (not isinstance(keep_count, int) or keep_count < 1):
        return error_response('keep_count must be a positive integer')
    if trash_count is not None and (not isinstance(trash_count, int) or trash_count < 1):
        return error_response('trash_count must be a positive integer')

    # Default to keep_count=1 when no mode specified
    if not has_keep and not has_trash:
        keep_count = 1

    db = get_db()

    if not db.is_trash_enabled():
        return error_response('Trash directory is disabled. Check that it does not overlap an indexed folder.')

    # Get groups with quality fields
    try:
        if group_hashes and isinstance(group_hashes, list):
            groups = []
            for gh in group_hashes:
                groups.extend(db._duplicate_manager.get_group_images_ranked(level, gh))
        else:
            groups = db._duplicate_manager.get_group_images_ranked(level)
    except Exception as e:
        logger.exception('Failed to get duplicate groups for pruning')
        return error_response(f'Failed to load groups: {e!s}', 500)

    if not groups:
        return success_response(
            {
                'trashed_count': 0,
                'group_count': 0,
                'errors': [],
            }
        )

    # Determine which images to trash across all groups
    all_trash_ids = []
    pruned_group_count = 0

    for group in groups:
        images = group['images']
        n = len(images)

        # Determine how many to keep for this group.
        # Trash mode: trash the worst N, keep the rest (always keep >= 1).
        # Keep mode: keep the best N, trash the rest.
        if trash_percent is not None:
            group_trash = math.ceil(n * trash_percent / 100)
            group_trash = max(0, min(group_trash, n - 1))
            group_keep = n - group_trash
        elif trash_count is not None:
            group_trash = max(0, min(trash_count, n - 1))
            group_keep = n - group_trash
        elif keep_percent is not None:
            group_keep = math.ceil(n * keep_percent / 100)
        else:
            group_keep = keep_count
        # Always keep at least 1
        group_keep = max(1, min(group_keep, n))

        if group_keep >= n:
            continue  # Nothing to trash in this group

        # Score and rank images
        scores = compute_quality_scores(images, db.config)

        # Sort by score descending — best first
        ranked = sorted(images, key=lambda img: scores.get(img['id'], 0), reverse=True)

        # Keep top N, trash the rest
        trash_ids = [img['id'] for img in ranked[group_keep:]]
        all_trash_ids.extend(trash_ids)
        pruned_group_count += 1

    if not all_trash_ids:
        return success_response(
            {
                'trashed_count': 0,
                'group_count': 0,
                'errors': [],
            }
        )

    # Enqueue all images for background trashing (instant return)
    try:
        result = db.enqueue_trash(all_trash_ids)
    except ValueError as e:
        return error_response(str(e))

    # Invalidate thumbnail RAM cache for enqueued images (fast dict removals)
    if result['enqueued']:
        cache = get_thumbnail_cache()
        for image_id in result['enqueued']:
            checksum = db.get_checksum(image_id)
            if checksum:
                cache.remove(checksum)

    # Collect error messages
    error_messages = [f'{img_id}: {msg}' for img_id, msg in result.get('errors', {}).items()]

    # enqueue_trash() already emits EVENT_IMAGES_CHANGED and EVENT_GROUPS_CHANGED
    # for all affected levels, so no need to emit here.

    return success_response(
        {
            'trashed_count': len(result['enqueued']),
            'group_count': pruned_group_count,
            'errors': error_messages,
        }
    )


# =============================================================================
# Custom Group Endpoints (Albums)
# =============================================================================


@app.route('/api/groups', methods=['POST'])
def create_group():
    """Create a custom group (album) with optional initial images.

    Request Body:
        JSON object with:
            - group_hash: Frontend-generated UUID for the group
            - name: Display name for the group (non-empty, max 255 chars)
            - image_ids: Optional array of image IDs to include initially

    Returns:
        Success response on creation.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    group_hash = data.get('group_hash', '').strip()
    if not group_hash:
        return error_response('group_hash is required')

    name = data.get('name', '').strip()
    if not name:
        return error_response('Group name is required')
    if len(name) > 255:
        return error_response('Group name must be 255 characters or fewer')

    image_ids = data.get('image_ids', [])

    try:
        db = get_db()
        db.create_custom_group(group_hash, name, image_ids)
        # Broadcast for other clients
        db.event_queue.emit(EVENT_GROUPS_CHANGED, {'level': 5, 'invalidate': True})
        return success_response(message='Group created')
    except Exception as e:
        logger.exception('Failed to create custom group')
        return error_response(f'Failed to create group: {e!s}', 500)


@app.route('/api/groups/<group_hash>', methods=['PATCH'])
def rename_group(group_hash):
    """Rename a custom group.

    Request Body:
        JSON object with:
            - name: New display name (non-empty, max 255 chars)

    Returns:
        Success response on rename.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    name = data.get('name', '').strip()
    if not name:
        return error_response('Group name is required')
    if len(name) > 255:
        return error_response('Group name must be 255 characters or fewer')

    try:
        db = get_db()
        db.rename_custom_group(group_hash, name)
        # Broadcast for other clients
        db.event_queue.emit(EVENT_GROUPS_CHANGED, {'level': 5, 'invalidate': True})
        return success_response(message='Group renamed')
    except Exception as e:
        logger.exception('Failed to rename custom group')
        return error_response(f'Failed to rename group: {e!s}', 500)


@app.route('/api/groups/<group_hash>', methods=['DELETE'])
def delete_group(group_hash):
    """Delete a custom group and all its image associations.

    The images themselves are not deleted — only the group membership.

    Returns:
        Success response on deletion.
    """
    try:
        db = get_db()
        db.delete_custom_group(group_hash)
        # Broadcast for other clients
        db.event_queue.emit(EVENT_GROUPS_CHANGED, {'level': 5, 'invalidate': True})
        return success_response(message='Group deleted')
    except Exception as e:
        logger.exception('Failed to delete custom group')
        return error_response(f'Failed to delete group: {e!s}', 500)


@app.route('/api/groups/<group_hash>/images', methods=['POST'])
def add_images_to_group(group_hash):
    """Add images to an existing custom group.

    Request Body:
        JSON object with:
            - image_ids: Array of image IDs to add

    Returns:
        Success response on addition.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('image_ids array is required')

    try:
        db = get_db()
        db.add_images_to_custom_group(group_hash, image_ids)
        # Broadcast for other clients
        db.event_queue.emit(EVENT_GROUPS_CHANGED, {'level': 5, 'invalidate': True})
        return success_response(message='Images added to group')
    except Exception as e:
        logger.exception('Failed to add images to custom group')
        return error_response(f'Failed to add images: {e!s}', 500)


@app.route('/api/groups/<group_hash>/images/remove', methods=['POST'])
def remove_images_from_group(group_hash):
    """Remove images from a custom group (group persists even if empty).

    Uses POST instead of DELETE to avoid DELETE-with-body issues.
    Matches existing pattern (e.g. /api/faces/unassign-batch).

    Request Body:
        JSON object with:
            - image_ids: Array of image IDs to remove

    Returns:
        Success response on removal.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('image_ids array is required')

    try:
        db = get_db()
        db.remove_images_from_custom_group(group_hash, image_ids)
        # Broadcast for other clients
        db.event_queue.emit(EVENT_GROUPS_CHANGED, {'level': 5, 'invalidate': True})
        return success_response(message='Images removed from group')
    except Exception as e:
        logger.exception('Failed to remove images from custom group')
        return error_response(f'Failed to remove images: {e!s}', 500)


# =============================================================================
# Search Endpoints
# =============================================================================


@app.route('/api/search', methods=['POST'])
def search_images():
    """Semantic search for images using OpenCLIP embeddings.

    Takes a text query, encodes it with OpenCLIP, and finds images
    with similar embeddings. Searches both image content embeddings
    and description embeddings.

    Request Body:
        JSON object with:
            - query: Text query to search for
            - threshold: (optional) Minimum similarity score (0.0-1.0, default 0.2)
            - limit: (optional) Maximum results (default 100)

    Returns:
        JSON object with:
            - results: Array of matching images with 'score' field
    """
    data = request.get_json()
    if not data or 'query' not in data:
        return error_response('Query is required')

    query = data['query'].strip()
    if not query:
        return error_response('Query cannot be empty')

    threshold = data.get('threshold', 0.2)
    limit = data.get('limit', 100)

    try:
        results = get_db().search_images(query, threshold=threshold, limit=limit)
        return success_response({'results': results})
    except Exception as e:
        logger.exception('Search failed')
        return error_response(f'Search failed: {e!s}', 500)


@app.route('/api/similar/<image_id>', methods=['GET'])
def get_similar_images(image_id):
    """Get all images sorted by visual similarity to a reference image.

    Uses OpenCLIP embeddings to compute cosine similarity between the
    reference image and all other images in the database.

    Args:
        image_id: The ID of the reference image.

    Returns:
        JSON object with:
            - results: Array of images with 'similarity' field, sorted descending
    """
    try:
        # Check if image exists first
        image = get_db().get_image(image_id)
        if image is None:
            return error_response('Image not found', 404)

        results = get_db().get_similar_images(image_id)
        if results is None:
            return error_response('Image embedding not yet computed. Please wait for processing to complete.', 404)
        return success_response({'results': results})
    except Exception as e:
        logger.exception('Similarity search failed')
        return error_response(f'Similarity search failed: {e!s}', 500)


# =============================================================================
# Metadata Search Endpoints
# =============================================================================


@app.route('/api/metadata-search', methods=['POST'])
def metadata_search():
    """Search for images matching EXIF metadata criteria.

    Uses subsequence matching on indexed metadata key-value pairs.
    Multiple criteria are ANDed together.

    Request Body:
        JSON object with:
            - criteria: Dictionary of {key: query_text} pairs.
              Each query uses subsequence matching (e.g. "nkn" matches "Nikon").

    Returns:
        JSON object with:
            - image_ids: Array of image IDs matching all criteria.
    """
    data = request.get_json()
    if not data or 'criteria' not in data:
        return error_response('criteria is required')

    criteria = data['criteria']
    if not isinstance(criteria, dict):
        return error_response('criteria must be a dictionary')

    try:
        image_ids = get_db().search_image_metadata(criteria)
        return success_response({'image_ids': image_ids})
    except Exception as e:
        logger.exception('Metadata search failed')
        return error_response(f'Metadata search failed: {e!s}', 500)


@app.route('/api/metadata-keys', methods=['GET'])
def metadata_keys():
    """Get all distinct metadata keys present in the database.

    Used to populate the writable metadata filter modal with available
    keys the user can search by.

    Returns:
        JSON object with:
            - keys: Sorted array of distinct key names.
    """
    try:
        keys = get_db().get_metadata_keys()
        return success_response({'keys': keys})
    except Exception as e:
        logger.exception('Failed to get metadata keys')
        return error_response(f'Failed to get metadata keys: {e!s}', 500)


@app.route('/api/metadata-values', methods=['GET'])
def metadata_values():
    """Get all distinct values for a given metadata key.

    Used for autocomplete dropdowns in the metadata filter modal.

    Query Parameters:
        key: The metadata key name (e.g. 'Camera').

    Returns:
        JSON object with:
            - values: Sorted array of distinct values for the key.
    """
    key = request.args.get('key', '').strip()
    if not key:
        return error_response('key parameter is required')

    try:
        values = get_db().get_metadata_values(key)
        return success_response({'values': values})
    except Exception as e:
        logger.exception('Failed to get metadata values')
        return error_response(f'Failed to get metadata values: {e!s}', 500)


# =============================================================================
# Stats Endpoints
# =============================================================================


@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Get database statistics.

    Returns aggregate information about the image database.

    Returns:
        JSON object with:
            - totalImages: Total number of images in database
            - totalFolders: Number of registered folders
    """
    stats = get_db().get_stats()
    return success_response(stats)


@app.route('/api/stats/cache', methods=['GET'])
def get_cache_stats():
    """Get thumbnail cache statistics.

    Returns information about the RAM thumbnail cache performance.

    Returns:
        JSON object with:
            - hits: Number of cache hits
            - misses: Number of cache misses
            - hit_rate: Cache hit rate (0.0-1.0)
            - size_bytes: Current cache size in bytes
            - size_mb: Current cache size in megabytes
            - count: Number of cached thumbnails
            - max_size_mb: Maximum cache size in megabytes
    """
    return success_response(get_thumbnail_cache().stats())


# =============================================================================
# Events Polling Endpoint
# =============================================================================


@app.route('/api/events', methods=['GET'])
def get_events():
    """Poll for pending events using cursor-based pagination.

    Multi-client safe: events are not drained on read. Each client
    passes its ``since`` cursor (from the previous response's
    ``server_time``) and receives only newer events.

    Query parameters:
        since (float): Unix timestamp cursor. Pass 0 for initial poll.

    Returns:
        JSON with 'events' array, 'server_time' (float — use as next
        cursor), and 'stale' (bool — if true, client must reload all
        state because it missed events).
    """
    since = float(request.args.get('since', 0))
    result = get_db().get_pending_events(since=since)
    return success_response(result)


@app.route('/api/events/count', methods=['GET'])
def get_event_count():
    """Get number of pending events without fetching them.

    Lightweight endpoint for checking if there are events to fetch.

    Returns:
        JSON with 'count' of pending events.
    """
    count = get_db().get_pending_event_count()
    return success_response({'count': count})


# =============================================================================
# People Endpoints
# =============================================================================


@app.route('/api/people', methods=['GET'])
def get_people():
    """List all people with face counts.

    Query Parameters:
        q: Optional search query (case-insensitive substring match).

    Returns:
        JSON array of person objects with face_count.
    """
    query = request.args.get('q', '').strip()
    db = get_db()

    if query:
        people = search_people(db.conn, query)
    else:
        people = get_all_people(db.conn)

    return success_response(people)


@app.route('/api/people', methods=['POST'])
def create_person_endpoint():
    """Create a new person with frontend-provided ID.

    The frontend generates UUIDs and orchestrates all application logic.
    This endpoint simply persists the person record.

    Request Body:
        JSON object with:
            - id: Person's UUID (required, frontend-generated)
            - name: Person's name (required)
            - preferred_face_id: Initial preferred face (optional)

    Returns:
        JSON object with success status.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    person_id = data.get('id')
    if not person_id:
        return error_response('id is required (frontend-generated UUID)')

    name = data.get('name', '').strip()
    if not name:
        return error_response('Name is required')

    preferred_face_id = data.get('preferred_face_id')

    db = get_db()

    with db._db_lock:
        # Check for ID collision (shouldn't happen with UUIDs)
        existing = get_person(db.conn, person_id)
        if existing:
            return error_response(f'Person with ID "{person_id}" already exists', 409)

        # Check for name collision
        # DESIGN: Defensive validation - rejects invalid request with error (see design-audit.md 1.7)
        existing_name = get_person_by_name(db.conn, name)
        if existing_name:
            return error_response(f'Person with name "{name}" already exists', 409)

        # Create person with provided ID
        create_person(db.conn, name, person_id=person_id)

        # Set preferred face if provided
        if preferred_face_id:
            update_person(db.conn, person_id, preferred_face_id=preferred_face_id)

        # Get the created person for the event payload
        created_person = get_person(db.conn, person_id)

    # Broadcast for other clients
    if created_person:
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'upserted': [dict(created_person)],
            },
        )

    return success_response(message='Person created')


@app.route('/api/people/<person_id>', methods=['GET'])
def get_person_endpoint(person_id):
    """Get a person by ID.

    Args:
        person_id: Person's UUID.

    Returns:
        JSON object with person details.
    """
    db = get_db()
    person = get_person(db.conn, person_id)

    if person is None:
        return error_response('Person not found', 404)

    return success_response(person)


@app.route('/api/people/<person_id>', methods=['PATCH'])
def update_person_endpoint(person_id):
    """Update a person's details.

    Args:
        person_id: Person's UUID.

    Request Body:
        JSON object with optional fields:
            - name: New name
            - preferred_face_id: ID of face to use as headshot

    Returns:
        JSON object with updated person.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    db = get_db()

    name = data.get('name')
    if name is not None:
        name = name.strip()
        if not name:
            return error_response('Name cannot be empty')

    preferred_face_id = data.get('preferred_face_id')

    # Handle recognition_threshold (or alias 'threshold'): present key means update, absent means don't change
    update_kwargs = {'name': name, 'preferred_face_id': preferred_face_id}
    threshold_changed = False
    threshold_value = None
    # Accept both 'recognition_threshold' and 'threshold' (frontend uses 'threshold')
    if 'recognition_threshold' in data or 'threshold' in data:
        threshold = data.get('recognition_threshold') if 'recognition_threshold' in data else data.get('threshold')
        # DESIGN: Defensive validation - rejects invalid input with error (see design-audit.md 1.9)
        if threshold is not None:
            try:
                threshold = float(threshold)
                if not (0.0 <= threshold <= 1.0):
                    return error_response('recognition_threshold must be between 0 and 1')
            except (ValueError, TypeError):
                return error_response('recognition_threshold must be a number')
        update_kwargs['recognition_threshold'] = threshold
        threshold_changed = True
        threshold_value = threshold

    ejected_face_ids = []
    faces_changed = False
    # All reads and writes under one lock to prevent TOCTOU races
    # (e.g. person deleted between existence check and update)
    with db._db_lock:
        person = get_person(db.conn, person_id)
        if person is None:
            return error_response('Person not found', 404)

        # DESIGN: Defensive validation - rejects invalid request with error (see design-audit.md 1.7)
        if name is not None:
            existing = get_person_by_name(db.conn, name)
            if existing and existing['id'] != person_id:
                return error_response(f'Person with name "{name}" already exists', 409)

        update_person(db.conn, person_id, **update_kwargs)

        # If threshold was changed to a non-null value, revalidate and reassess
        if threshold_changed and threshold_value is not None:
            # Eject faces that no longer meet the threshold
            ejected_face_ids = revalidate_person_faces(db.conn, person_id, threshold_value)

            # DESIGN: Atomic cascade - person with 0 faces is invalid state, so we clean up
            # atomically rather than requiring frontend roundtrip (see design-audit.md 1.1)
            if ejected_face_ids:
                remaining = get_faces_for_person(db.conn, person_id)
                if not remaining:
                    delete_person(db.conn, person_id)
                    # Broadcast for other clients — person deleted, faces ejected
                    db.event_queue.emit(EVENT_PEOPLE_CHANGED, {'removed': [person_id]})
                    db.event_queue.emit(
                        EVENT_FACES_CHANGED,
                        {
                            'updated': [{'id': fid, 'person_id': None} for fid in ejected_face_ids],
                        },
                    )
                    # DESIGN: Response flags report cascade results (see design-audit.md 1.6)
                    return success_response(
                        {
                            'deleted': True,
                            'unassigned': ejected_face_ids,  # AppState expects 'unassigned'
                            'message': 'All faces ejected, person deleted',
                        }
                    )
                faces_changed = True

        updated_person = get_person(db.conn, person_id)

    # DESIGN: Auto-trigger reassessment - the purpose of changing threshold is to re-evaluate
    # faces, so this avoids requiring a separate API call (see design-audit.md 1.8)
    # Use full sweep (person_id=None) so ejected faces can be reassigned to other people
    if threshold_changed and threshold_value is not None:
        reassess_unknown_faces_async(
            db,
            threshold=db.config.face_recognition_threshold,
            person_id=None,  # Full sweep
        )

    # DESIGN: Response flags report cascade results so frontend can update correctly
    # without refetching all state (see design-audit.md 1.6)
    response_data = dict(updated_person) if updated_person else {}
    # AppState expects 'assigned' and 'unassigned' arrays
    # - 'unassigned' = faces immediately ejected due to threshold change
    # - 'assigned' = faces matched via async reassessment (comes via polling)
    if ejected_face_ids:
        response_data['unassigned'] = ejected_face_ids
    response_data['faces_changed'] = faces_changed or (threshold_changed and threshold_value is not None)

    # Broadcast for other clients
    if updated_person:
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'upserted': [dict(updated_person)],
            },
        )
    if ejected_face_ids:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': None} for fid in ejected_face_ids],
            },
        )

    return success_response(response_data)


@app.route('/api/people/<person_id>', methods=['DELETE'])
def delete_person_endpoint(person_id):
    """Delete a person.

    Faces associated with this person will become untagged (person_id = NULL).

    Args:
        person_id: Person's UUID.

    Returns:
        Success message.
    """
    db = get_db()

    with db._db_lock:
        person = get_person(db.conn, person_id)
        if person is None:
            return error_response('Person not found', 404)

        # Get affected face IDs before deletion (they'll become untagged)
        affected_faces = get_faces_for_person(db.conn, person_id)
        affected_face_ids = [f['id'] for f in affected_faces] if affected_faces else []

        delete_person(db.conn, person_id)

    # Broadcast for other clients
    db.event_queue.emit(EVENT_PEOPLE_CHANGED, {'removed': [person_id]})
    if affected_face_ids:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': None} for fid in affected_face_ids],
            },
        )

    return success_response(message=f'Person "{person["name"]}" deleted')


@app.route('/api/people/<person_id>/faces', methods=['GET'])
def get_person_faces(person_id):
    """Get all faces for a person.

    Args:
        person_id: Person's UUID.

    Returns:
        JSON array of face objects.
    """
    db = get_db()

    person = get_person(db.conn, person_id)
    if person is None:
        return error_response('Person not found', 404)

    faces = get_faces_for_person(db.conn, person_id)

    # Remove embeddings from response (they're large and not needed in API)
    for face in faces:
        face.pop('embedding', None)
        face.pop('semantic_embedding', None)

    return success_response(faces)


@app.route('/api/people/<person_id>/thumbnail', methods=['GET'])
def get_person_thumbnail(person_id):
    """Get the preferred face thumbnail for a person.

    Returns the thumbnail for the person's preferred_face_id, or the first
    face if no preference is set.

    Args:
        person_id: Person's UUID.

    Returns:
        JPEG image.
    """
    db = get_db()

    person = get_person(db.conn, person_id)
    if person is None:
        return error_response('Person not found', 404)

    # Get preferred face or first face
    face_id = person.get('preferred_face_id')
    fallback_used = False
    if not face_id:
        faces = get_faces_for_person(db.conn, person_id)
        if not faces:
            return error_response('Person has no faces', 404)
        face_id = faces[0]['id']
        fallback_used = True

    # Get face thumbnail
    thumb_path = get_face_thumbnail_path(face_id, db.thumbnail_dir)
    thumb_exists = thumb_path.exists()
    thumb_mtime = thumb_path.stat().st_mtime if thumb_exists else None

    logger.debug(
        f'get_person_thumbnail: person={person_id[:8]}... '
        f'preferred_face_id='
        f'{person.get("preferred_face_id", "None")[:8] if person.get("preferred_face_id") else "None"}... '
        f'face_id={face_id[:8]}... fallback={fallback_used} '
        f'exists={thumb_exists} mtime={thumb_mtime} path={thumb_path}'
    )

    if not thumb_exists:
        return error_response('Face thumbnail not found', 404)

    return send_file(
        thumb_path,
        mimetype='image/jpeg',
        max_age=31536000,  # 1 year cache
    )


# =============================================================================
# Face Endpoints
# =============================================================================


@app.route('/api/images/<image_id>/faces', methods=['GET'])
def get_image_faces(image_id):
    """Get all faces detected in an image.

    Args:
        image_id: Image's UUID.

    Returns:
        JSON array of face objects with person_name if identified.
    """
    db = get_db()

    image = db.get_image(image_id)
    if image is None:
        return error_response('Image not found', 404)

    faces = get_faces_for_image(db.conn, image_id, include_suppressed=False)

    # Remove embeddings from response
    for face in faces:
        face.pop('embedding', None)
        face.pop('semantic_embedding', None)

    return success_response(faces)


@app.route('/api/faces', methods=['GET'])
def get_faces_list():
    """Get all non-suppressed faces.

    Query Parameters:
        unknown: If 'true', only return faces without a person_id.
        search: Text query for semantic search (unknown faces only).
                When provided, returns unknown faces sorted by similarity.
        image_ids: Comma-separated list of image IDs to filter by (batch operation).
                   When provided, returns only faces for those images.

    Returns:
        JSON array of face objects with person_name if identified.
        Ordered by: known faces (alphabetically by person name), then unknown faces.
        When search is provided: unknown faces sorted by similarity descending.
        When image_ids is provided: faces for those images, ordered by image then created_at.
    """
    unknown_only = request.args.get('unknown', '').lower() == 'true'
    search_query = request.args.get('search', '').strip()
    image_ids_param = request.args.get('image_ids', '').strip()

    db = get_db()

    # If image_ids provided, do batch fetch for specific images
    if image_ids_param:
        image_ids = [id.strip() for id in image_ids_param.split(',') if id.strip()]
        if image_ids:
            faces = get_faces_for_images(db.conn, image_ids)
            return success_response(faces)

    # If search query provided, do semantic search on unknown faces
    if search_query:
        try:
            # Encode query with CLIP (supports negative terms like "beach -face")
            query_embedding = db._get_clip_model().encode_semantic_query(search_query)
            # Search unknown faces by semantic similarity
            faces = search_unknown_faces_semantic(db.conn, query_embedding)
            return success_response(faces)
        except Exception as e:
            logger.error(f'Failed to encode search query: {e}')
            return error_response('Failed to encode search query', 500)

    faces = get_all_faces(db.conn, unknown_only=unknown_only)
    return success_response(faces)


@app.route('/api/faces/<face_id>', methods=['GET'])
def get_single_face(face_id):
    """Get a single face by ID.

    Used by AppState.faces.ensureFacesInCache() to fetch faces
    that are missing from the client-side cache.

    Returns:
        JSON face object with person_name if identified.
    """
    db = get_db()
    face = get_face(db.conn, face_id)
    if not face:
        return error_response('Face not found', 404)
    return success_response(dict(face))


@app.route('/api/faces/<face_id>/matches', methods=['GET'])
def get_face_matches_endpoint(face_id):
    """Get top matching people for a face.

    Compares the face's embedding against all locked faces and returns
    the top N matching people with their best-matching face.

    Query Parameters:
        limit: Maximum matches to return (default: 5)

    Returns:
        JSON array of match objects with:
        - person_id: ID of the matching person
        - person_name: Name of the matching person
        - face_id: ID of their best-matching locked face
        - similarity: Cosine similarity score (0-1)
    """
    db = get_db()
    limit = request.args.get('limit', 5, type=int)
    limit = max(1, min(limit, 10))  # Clamp to 1-10

    matches = get_face_matches(db.conn, face_id, limit=limit)
    return success_response(matches)


# =============================================================================
# Simple Face Endpoints (AppState-driven, no business logic)
# =============================================================================


@app.route('/api/faces/assign', methods=['POST'])
def assign_faces():
    """Assign faces to a person (simple batch operation).

    Frontend (AppState) handles application logic:
    - Find or create person
    - Update face counts
    - Set preferred face
    - Cleanup empty persons

    After assignment, triggers async reassessment to auto-match
    unknown faces against the person's newly assigned faces.

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to assign
            - person_id: Person's UUID
            - trigger_reassessment: Whether to trigger auto-matching (default: true)

    Returns:
        Success status with reassessment_triggered flag.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    person_id = data.get('person_id')
    trigger_reassessment = data.get('trigger_reassessment', True)

    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')
    if not person_id:
        return error_response('person_id is required')

    db = get_db()

    with db._db_lock:
        # Verify person exists
        person = get_person(db.conn, person_id)
        if person is None:
            return error_response('Person not found', 404)

        # Assign each face (just update person_id, don't touch manually_tagged)
        assigned_count = 0
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face is None:
                continue
            update_face_person(db.conn, face_id, person_id)
            assigned_count += 1

    # Trigger async reassessment to auto-match unknown faces against this person
    reassessment_triggered = False
    if trigger_reassessment and assigned_count > 0:
        reassess_unknown_faces_async(
            db,
            threshold=db.config.face_recognition_threshold,
            person_id=person_id,
        )
        reassessment_triggered = True

    # Broadcast for other clients
    if assigned_count > 0:
        person_name = person['name'] if person else None
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': person_id, 'person_name': person_name} for fid in face_ids],
            },
        )

    return success_response(
        message=f'{assigned_count} faces assigned', data={'reassessment_triggered': reassessment_triggered}
    )


@app.route('/api/faces/unassign', methods=['POST'])
def unassign_faces_simple():
    """Unassign faces from their persons (simple batch operation).

    This is a dumb persistence endpoint - no business logic.
    Frontend (AppState) handles all application logic:
    - Track affected persons
    - Update face counts
    - Reassign preferred faces
    - Delete empty persons

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to unassign

    Returns:
        Success status.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')

    db = get_db()

    with db._db_lock:
        unassigned_count = 0
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face is None:
                continue
            # Clear person_id and manually_tagged (face returns to unknown pool)
            update_face_person(db.conn, face_id, None, manually_tagged=False)
            unassigned_count += 1

    # Broadcast for other clients
    if unassigned_count > 0:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': None} for fid in face_ids],
            },
        )

    return success_response(message=f'{unassigned_count} faces unassigned')


@app.route('/api/faces/suppress', methods=['POST'])
def suppress_faces_batch():
    """Suppress faces (mark as false positives, simple batch operation).

    This is a dumb persistence endpoint - no business logic.
    Frontend (AppState) handles all application logic:
    - Unassign from persons first
    - Update face counts
    - Reassign preferred faces
    - Delete empty persons

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to suppress

    Returns:
        Success status.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')

    db = get_db()

    with db._db_lock:
        suppressed_count = 0
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face is None:
                continue
            suppress_face(db.conn, face_id)
            suppressed_count += 1

    # Broadcast for other clients
    if suppressed_count > 0:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'suppressed': True} for fid in face_ids],
            },
        )

    return success_response(message=f'{suppressed_count} faces suppressed')


@app.route('/api/faces', methods=['PATCH'])
def update_faces_batch():
    """Update face properties (batch operation).

    This is a dumb persistence endpoint - no business logic.

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to update
            - locked: New locked (manually_tagged) state (optional)

    Returns:
        Success status.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')

    locked = data.get('locked')

    db = get_db()

    with db._db_lock:
        updated_count = 0
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face is None:
                continue

            if locked is not None:
                # Update manually_tagged flag
                db.conn.execute(
                    "UPDATE faces SET manually_tagged = ?, updated_at = datetime('now') WHERE id = ?",
                    (1 if locked else 0, face_id),
                )
                updated_count += 1

        db.conn.commit()

    # Broadcast for other clients
    if updated_count > 0 and locked is not None:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'manually_tagged': locked} for fid in face_ids],
            },
        )

    return success_response(message=f'{updated_count} faces updated')


# =============================================================================
# Legacy Face Endpoints (complex business logic - being phased out)
# =============================================================================


@app.route('/api/faces/<face_id>/identify', methods=['POST'])
def identify_face(face_id):
    """Identify a face by assigning it to a person.

    Request Body:
        JSON object with ONE of:
            - person_id: Existing person's UUID
            - name: Name for new or existing person (case-insensitive match)

    If name is provided and matches an existing person (case-insensitive),
    the face is linked to that person. Otherwise, a new person is created.

    Returns:
        JSON object with the updated face and person details.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    db = get_db()

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)

        person_id = data.get('person_id')
        name = data.get('name', '').strip() if data.get('name') else None

        if person_id:
            # Link to existing person by ID
            person = get_person(db.conn, person_id)
            if person is None:
                return error_response('Person not found', 404)
        elif name:
            # Find or create person by name
            person = get_person_by_name(db.conn, name)
            if person is None:
                person_id = create_person(db.conn, name)
                person = get_person(db.conn, person_id)
            else:
                person_id = person['id']
        else:
            return error_response('Either person_id or name is required')

        # Update face with person_id (manually tagged since user initiated)
        update_face_person(db.conn, face_id, person_id, manually_tagged=True)

        # Get updated face
        face = get_face(db.conn, face_id)
        if 'embedding' in face:
            del face['embedding']

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [
                {'id': face_id, 'person_id': person_id, 'person_name': person['name'], 'manually_tagged': True}
            ],
        },
    )
    db.event_queue.emit(
        EVENT_PEOPLE_CHANGED,
        {
            'upserted': [{'id': person_id, 'name': person['name']}],
        },
    )

    return success_response(
        {
            'face': face,
            'person': person,
        }
    )


@app.route('/api/faces/identify-batch', methods=['POST'])
def identify_faces_batch():
    """Identify multiple faces with the same name in a single operation.

    This is more efficient than calling /identify for each face individually.
    After identification, triggers async re-assessment of remaining unknown
    faces to auto-match them against the newly identified person.

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to identify
            - name: Name for the person
            - preferred_face_id: Face to set as preferred (optional)

    Returns:
        JSON object with the person and list of updated face IDs.
    """
    logger.info('[FacesFlow] identify-batch START')
    data = request.get_json()
    if not data:
        logger.warning('[FacesFlow] identify-batch: No request body')
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    name = data.get('name', '').strip() if data.get('name') else None
    preferred_face_id = data.get('preferred_face_id')
    logger.info(f'[FacesFlow] identify-batch: face_ids={face_ids}, name={name}, preferred={preferred_face_id}')

    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')
    if not name:
        return error_response('name is required')

    db = get_db()

    # Batch identify all faces (use lock to avoid conflicts with background threads)
    with db._db_lock:
        # Track source persons before reassignment (for preferred face cleanup)
        source_person_ids = set()
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face and face.get('person_id'):
                source_person_ids.add(face['person_id'])

        result = batch_identify_faces(db.conn, face_ids, name, preferred_face_id)

        if result['person'] is None:
            return error_response('Failed to identify faces')

        # Exclude the target person from source cleanup
        target_person_id = result['person']['id']
        source_person_ids.discard(target_person_id)

        # Fix preferred faces for source persons (faces were moved away)
        for source_id in source_person_ids:
            person = get_person(db.conn, source_id)
            if not person:
                continue

            remaining_faces = get_faces_for_person(db.conn, source_id)
            if not remaining_faces:
                continue  # Person will be deleted below

            # Check if current preferred face is still valid
            current_preferred = person.get('preferred_face_id')
            remaining_ids = {f['id'] for f in remaining_faces}

            if current_preferred not in remaining_ids:
                # Select newest face (last in list, sorted by timestamp ASC)
                new_preferred = remaining_faces[-1]['id']
                update_person(db.conn, source_id, preferred_face_id=new_preferred)
                # Lock the new preferred face (prevents auto-reassignment)
                db.conn.execute(
                    "UPDATE faces SET manually_tagged = 1, updated_at = datetime('now') WHERE id = ?", (new_preferred,)
                )

        # Delete source persons with no more faces
        delete_people_without_faces(db.conn)

    # Trigger async re-assessment of unknown faces
    # This will match other unknown faces against the newly identified person
    reassess_unknown_faces_async(
        db,
        threshold=db.config.face_recognition_threshold,
        person_id=result['person']['id'],
    )

    response_data = {
        'person': result['person'],
        'identified_count': len(result['faces']),
        'face_ids': result['faces'],
        'reassessment_triggered': True,
    }

    # Broadcast for other clients
    target_person = result['person']
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [
                {
                    'id': fid,
                    'person_id': target_person['id'],
                    'person_name': target_person['name'],
                    'manually_tagged': True,
                }
                for fid in result['faces']
            ],
        },
    )
    db.event_queue.emit(
        EVENT_PEOPLE_CHANGED,
        {
            'upserted': [target_person],
            'removed': list(source_person_ids),  # Persons that may have been deleted
        },
    )

    logger.info(
        f'[FacesFlow] identify-batch SUCCESS: person_id={result["person"]["id"]}, '
        f'identified={len(result["faces"])} faces'
    )
    return success_response(response_data)


@app.route('/api/faces/reassess', methods=['POST'])
def trigger_full_reassessment():
    """Trigger a full face reassessment sweep.

    Performs a full sweep over ALL unknown and unlocked faces, comparing against
    ALL locked faces across all people. Each candidate is assigned to the
    best-matching person that meets that person's threshold.

    Returns:
        JSON object with 'reassessment_triggered' flag.
    """
    db = get_db()
    reassess_unknown_faces_async(
        db,
        threshold=db.config.face_recognition_threshold,
        person_id=None,  # Full sweep
    )
    return success_response({'reassessment_triggered': True})


@app.route('/api/faces/reassess-status', methods=['GET'])
def get_faces_reassess_status():
    """Get status of async face reassessment.

    Returns:
        JSON object with 'in_progress' bool and optionally 'last_result'.
    """
    status = get_reassessment_status()
    return success_response(status)


@app.route('/api/faces/reassess-ack', methods=['POST'])
def ack_reassessment():
    """Acknowledge face reassessment completion.

    Clears the 'completed' flag so subsequent status polls don't see
    stale completion data. Called by frontend after processing a
    reassessment result.

    Returns:
        Success message.
    """
    clear_reassessment_result()
    return success_response(message='Reassessment acknowledged')


@app.route('/api/faces/<face_id>/unidentify', methods=['POST'])
def unidentify_face(face_id):
    """Remove person identification from a face.

    Sets the face's person_id to NULL. If the person has no other faces,
    the person record is deleted.

    Args:
        face_id: Face's UUID.

    Returns:
        Success message.
    """
    db = get_db()

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)

        old_person_id = face.get('person_id')

        # Unlink face from person (clear manually_tagged so face is a candidate for reassessment)
        update_face_person(db.conn, face_id, None, manually_tagged=False)

        # Delete person if they have no more faces
        person_deleted = False
        if old_person_id:
            person_before = get_person(db.conn, old_person_id)
            delete_people_without_faces(db.conn)
            person_after = get_person(db.conn, old_person_id)
            person_deleted = person_before is not None and person_after is None

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [{'id': face_id, 'person_id': None}],
        },
    )
    if person_deleted:
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'removed': [old_person_id],
            },
        )

    return success_response(message='Face unidentified')


@app.route('/api/faces/<face_id>/suppress', methods=['POST'])
def suppress_face_endpoint(face_id):
    """Mark a face as a false positive (suppressed).

    Suppressed faces are excluded from all face-related queries and UI,
    but the bounding box is kept to prevent re-detection on reindex.

    If the face was associated with a person:
    - If it was their only face, the person is deleted
    - If it was their preferred face, a new preferred face is selected

    Args:
        face_id: Face's UUID.

    Returns:
        Success message with person_deleted flag if applicable.
    """
    db = get_db()
    person_deleted = False
    new_preferred_selected = False

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)

        old_person_id = face.get('person_id')

        # Check if this was the preferred face before suppressing
        was_preferred = False
        if old_person_id:
            person = get_person(db.conn, old_person_id)
            if person and person.get('preferred_face_id') == face_id:
                was_preferred = True

        suppress_face(db.conn, face_id)

        # Handle person cleanup if face was associated with someone
        if old_person_id:
            # Check if person still has faces
            remaining_faces = db.conn.execute(
                """SELECT id FROM faces
                   WHERE person_id = ? AND suppressed = 0
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (old_person_id,),
            ).fetchone()

            if not remaining_faces:
                # No faces left - delete the person
                delete_people_without_faces(db.conn)
                person_deleted = True
            elif was_preferred:
                # Person still has faces but lost their preferred - select new one
                new_preferred_id = remaining_faces['id']
                db.conn.execute(
                    "UPDATE people SET preferred_face_id = ?, updated_at = datetime('now') WHERE id = ?",
                    (new_preferred_id, old_person_id),
                )
                # Lock the new preferred face (prevents auto-reassignment)
                db.conn.execute(
                    "UPDATE faces SET manually_tagged = 1, updated_at = datetime('now') WHERE id = ?",
                    (new_preferred_id,),
                )
                db.conn.commit()
                new_preferred_selected = True

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [{'id': face_id, 'suppressed': True}],
        },
    )
    if person_deleted and old_person_id:
        db.event_queue.emit(EVENT_PEOPLE_CHANGED, {'removed': [old_person_id]})
    elif old_person_id and not person_deleted:
        # Person still exists — face count changed, possibly preferred face too
        updated_person = get_person(db.conn, old_person_id)
        if updated_person:
            db.event_queue.emit(
                EVENT_PEOPLE_CHANGED,
                {
                    'upserted': [dict(updated_person)],
                },
            )

    return success_response(
        message='Face suppressed',
        data={
            'person_deleted': person_deleted,
            'new_preferred_selected': new_preferred_selected,
        },
    )


@app.route('/api/faces/<face_id>/toggle-manual', methods=['POST'])
def toggle_face_manual_tag_endpoint(face_id):
    """Toggle the manually_tagged flag for a face.

    Manually tagged faces are used as reference for auto-matching.
    Auto-tagged faces are not used for matching (prevents snowball effect).

    Args:
        face_id: Face's UUID.

    Returns:
        Success message with the new manually_tagged value.
    """
    db = get_db()

    with db._db_lock:
        new_value = toggle_face_manual_tag(db.conn, face_id)

        if new_value is None:
            return error_response('Face not found', 404)

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [{'id': face_id, 'manually_tagged': new_value}],
        },
    )

    return success_response(message='Manual tag toggled', data={'manually_tagged': new_value})


@app.route('/api/faces/<face_id>', methods=['DELETE'])
def delete_face_endpoint(face_id):
    """Delete a face detection entirely.

    Unlike suppress, this removes the face record completely.
    The face may be re-detected on reindex.

    Args:
        face_id: Face's UUID.

    Returns:
        Success message.
    """
    db = get_db()

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)

        old_person_id = face.get('person_id')

        delete_face(db.conn, face_id)

        # Delete person if they have no more faces
        person_deleted = False
        if old_person_id:
            person_before = get_person(db.conn, old_person_id)
            delete_people_without_faces(db.conn)
            person_after = get_person(db.conn, old_person_id)
            person_deleted = person_before is not None and person_after is None

    # Broadcast for other clients
    db.event_queue.emit(EVENT_FACES_CHANGED, {'removed': [face_id]})
    if person_deleted and old_person_id:
        db.event_queue.emit(EVENT_PEOPLE_CHANGED, {'removed': [old_person_id]})

    return success_response(message='Face deleted')


@app.route('/api/faces/<face_id>/thumbnail', methods=['GET'])
def get_face_thumbnail(face_id):
    """Get the thumbnail for a face.

    If the thumbnail file is missing but the face and source image exist,
    regenerates the thumbnail on-demand. Concurrent requests for the same
    missing thumbnail will wait for the first to complete.

    Args:
        face_id: Face's UUID.

    Returns:
        JPEG image (200x200).
    """
    db = get_db()

    face = get_face(db.conn, face_id)
    if face is None:
        return error_response('Face not found', 404)

    thumb_path = get_face_thumbnail_path(face_id, db.thumbnail_dir)
    thumb_exists = thumb_path.exists()
    thumb_mtime = thumb_path.stat().st_mtime if thumb_exists else None
    logger.debug(f'get_face_thumbnail: {face_id[:8]}... exists={thumb_exists}, mtime={thumb_mtime}, path={thumb_path}')

    if not thumb_exists:
        # Check if another request is already regenerating this thumbnail
        should_regenerate = False
        with _face_thumb_regen_lock:
            if face_id not in _face_thumb_regenerating:
                _face_thumb_regenerating.add(face_id)
                should_regenerate = True

        if should_regenerate:
            # We're responsible for regenerating
            try:
                image = db.get_image(face['image_id'])
                if image and Path(image['path']).exists():
                    bbox = (face['box_x'], face['box_y'], face['box_w'], face['box_h'])
                    logger.debug(f'get_face_thumbnail: Regenerating {face_id[:8]}... bbox={bbox}')
                    success = generate_face_thumbnail(
                        source_path=image['path'],
                        dest_path=thumb_path,
                        box_x=face['box_x'],
                        box_y=face['box_y'],
                        box_w=face['box_w'],
                        box_h=face['box_h'],
                    )
                    if not success:
                        return error_response('Failed to regenerate face thumbnail', 500)
                else:
                    return error_response('Face thumbnail not found', 404)
            finally:
                with _face_thumb_regen_lock:
                    _face_thumb_regenerating.discard(face_id)
        else:
            # Another request is handling it - wait for file to appear
            for _ in range(20):  # Wait up to 2 seconds
                time.sleep(0.1)
                if thumb_path.exists():
                    break
            if not thumb_path.exists():
                return error_response('Face thumbnail not found', 404)

    return send_file(
        thumb_path,
        mimetype='image/jpeg',
        max_age=31536000,  # 1 year cache
    )


@app.route('/api/faces/<face_id>/unassign', methods=['POST'])
def unassign_face(face_id):
    """Remove a face from its person and return to unknown pool.

    Unlike unidentify, this is designed for the pick-preferred mode where
    the user is reviewing a person's faces and wants to remove incorrect ones.

    Args:
        face_id: Face's UUID.

    Returns:
        Success message with the updated person (new preferred face if needed).
    """
    db = get_db()

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)

        old_person_id = face.get('person_id')
        if not old_person_id:
            return error_response('Face is not assigned to any person', 400)

        # Get person details before unassigning
        person = get_person(db.conn, old_person_id)

        # Unlink face from person (clear manual flag since no longer assigned)
        update_face_person(db.conn, face_id, None, manually_tagged=False)

        # DESIGN: Data integrity invariant - person must have valid preferred_face_id for
        # thumbnails, so auto-select if current preferred was removed (see design-audit.md 1.2)
        if person and person.get('preferred_face_id') == face_id:
            remaining_faces = get_faces_for_person(db.conn, old_person_id)
            if remaining_faces:
                # Select newest face (last in list, sorted by timestamp ASC)
                new_preferred = remaining_faces[-1]['id']
                update_person(db.conn, old_person_id, preferred_face_id=new_preferred)
                # Lock the new preferred face (prevents auto-reassignment)
                db.conn.execute(
                    "UPDATE faces SET manually_tagged = 1, updated_at = datetime('now') WHERE id = ?", (new_preferred,)
                )

        # DESIGN: Global cleanup of empty people - prevents orphaned records (see design-audit.md 1.3)
        delete_people_without_faces(db.conn)

        # Get updated person (or None if deleted)
        updated_person = get_person(db.conn, old_person_id)

    # Note: We don't trigger group recalculation here - it's too expensive
    # for interactive use. Groups are computed during initial processing
    # or via explicit "Rescan" request.

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [{'id': face_id, 'person_id': None}],
        },
    )
    if updated_person:
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'upserted': [dict(updated_person)],
            },
        )
    else:
        # Person was deleted (no faces left)
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'removed': [old_person_id],
            },
        )

    return success_response(
        {
            'message': 'Face unassigned',
            'person': updated_person,  # Will be None if person was deleted
        }
    )


@app.route('/api/faces/unassign-batch', methods=['POST'])
def unassign_faces_batch():
    """Remove multiple faces from their person and return to unknown pool.

    More efficient than calling /unassign for each face individually.
    Only triggers one group recalculation at the end.

    Request Body:
        JSON object with:
            - face_ids: List of face UUIDs to unassign

    Returns:
        JSON object with count of unassigned faces.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_ids = data.get('face_ids', [])
    if not face_ids:
        return error_response('face_ids is required')
    if not isinstance(face_ids, list):
        return error_response('face_ids must be an array')

    db = get_db()
    unassigned_count = 0
    affected_person_ids = set()

    # Use lock to avoid conflicts with background threads
    with db._db_lock:
        # Phase 1: Unassign all faces and track affected persons
        for face_id in face_ids:
            face = get_face(db.conn, face_id)
            if face is None:
                continue

            old_person_id = face.get('person_id')
            if not old_person_id:
                continue

            affected_person_ids.add(old_person_id)

            # Unlink face from person (clear manual flag since no longer assigned)
            update_face_person(db.conn, face_id, None, manually_tagged=False)
            unassigned_count += 1

        # DESIGN: Data integrity invariant - person must have valid preferred_face_id for
        # thumbnails, so auto-select if current preferred was removed (see design-audit.md 1.2)
        # Phase 2: Fix preferred faces for affected persons
        # Select newest remaining face (by image timestamp) as preferred
        for person_id in affected_person_ids:
            person = get_person(db.conn, person_id)
            if not person:
                continue

            remaining_faces = get_faces_for_person(db.conn, person_id)
            if not remaining_faces:
                continue  # Person will be deleted below

            # Check if current preferred face is still valid
            current_preferred = person.get('preferred_face_id')
            remaining_ids = {f['id'] for f in remaining_faces}

            if current_preferred not in remaining_ids:
                # Select newest face (last in list, sorted by timestamp ASC)
                new_preferred = remaining_faces[-1]['id']
                update_person(db.conn, person_id, preferred_face_id=new_preferred)
                # Lock the new preferred face (prevents auto-reassignment)
                db.conn.execute(
                    "UPDATE faces SET manually_tagged = 1, updated_at = datetime('now') WHERE id = ?", (new_preferred,)
                )

        # DESIGN: Global cleanup of empty people - prevents orphaned records (see design-audit.md 1.3)
        # Phase 3: Delete people with no more faces
        delete_people_without_faces(db.conn)

    # Note: We don't trigger group recalculation here - it's too expensive
    # for interactive use (~minutes for 30k faces). Groups are computed
    # during initial processing or via explicit "Rescan" request.

    # Broadcast for other clients
    if unassigned_count > 0:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': None} for fid in face_ids],
            },
        )
        # People may have been deleted or had face counts change
        db.event_queue.emit(
            EVENT_PEOPLE_CHANGED,
            {
                'removed': [pid for pid in affected_person_ids if get_person(db.conn, pid) is None],
                'upserted': [dict(p) for pid in affected_person_ids if (p := get_person(db.conn, pid)) is not None],
            },
        )

    return success_response(
        {
            'message': f'{unassigned_count} faces unassigned',
            'unassigned_count': unassigned_count,
        }
    )


@app.route('/api/faces/group-status', methods=['GET'])
def get_faces_group_status():
    """Get status of async face grouping computation.

    Returns:
        JSON object with 'status' ('idle', 'computing', 'done', 'error').
    """
    status = get_group_computation_status()
    return success_response(status)


@app.route('/api/people/<person_id>/set-preferred', methods=['POST'])
def set_preferred_face(person_id):
    """Set the preferred face for a person.

    The preferred face is used as the person's thumbnail.

    Args:
        person_id: Person's UUID.

    Request Body:
        JSON object with:
            - face_id: UUID of the face to set as preferred

    Returns:
        JSON object with the updated person.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    face_id = data.get('face_id')
    if not face_id:
        return error_response('face_id is required')

    db = get_db()

    with db._db_lock:
        # Verify person exists
        person = get_person(db.conn, person_id)
        if person is None:
            return error_response('Person not found', 404)

        # Verify face exists and belongs to this person
        face = get_face(db.conn, face_id)
        if face is None:
            return error_response('Face not found', 404)
        if face.get('person_id') != person_id:
            return error_response('Face does not belong to this person', 400)

        # Update the preferred face
        update_person(db.conn, person_id, preferred_face_id=face_id)

        # Also mark the face as manually tagged (preferred implies manual selection)
        db.conn.execute("UPDATE faces SET manually_tagged = 1, updated_at = datetime('now') WHERE id = ?", (face_id,))
        db.conn.commit()

        # Get updated person
        updated_person = get_person(db.conn, person_id)

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_PEOPLE_CHANGED,
        {
            'upserted': [dict(updated_person)],
        },
    )
    db.event_queue.emit(
        EVENT_FACES_CHANGED,
        {
            'updated': [{'id': face_id, 'manually_tagged': True}],
        },
    )

    return success_response(updated_person)


@app.route('/api/people/<person_id>/merge', methods=['POST'])
def merge_person(person_id):
    """Merge one person into another.

    All faces from this person are moved to the target person, then this
    person is deleted. Preserves locked/preferred state on the target person.

    Args:
        person_id: Person's UUID (the person being merged/deleted).

    Request Body:
        JSON object with:
            - into: UUID of the target person to merge into

    Returns:
        Success message with updated target person.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    target_id = data.get('into')
    if not target_id:
        return error_response('into (target person ID) is required')

    if person_id == target_id:
        return error_response('Cannot merge a person into themselves')

    db = get_db()

    with db._db_lock:
        # Verify both persons exist
        from_person = get_person(db.conn, person_id)
        if from_person is None:
            return error_response('Source person not found', 404)

        to_person = get_person(db.conn, target_id)
        if to_person is None:
            return error_response('Target person not found', 404)

        # Move all faces from source to target
        db.conn.execute(
            """UPDATE faces SET person_id = ?, updated_at = datetime('now') WHERE person_id = ?""",
            (target_id, person_id),
        )

        # Delete the source person (face_count is computed dynamically via JOIN)
        delete_person(db.conn, person_id)
        db.conn.commit()

        # Get faces that moved to the target (for the event payload)
        merged_faces = get_faces_for_person(db.conn, target_id)
        merged_face_ids = [f['id'] for f in merged_faces] if merged_faces else []

        # Get updated target person
        updated_person = get_person(db.conn, target_id)

    # Broadcast for other clients
    db.event_queue.emit(
        EVENT_PEOPLE_CHANGED,
        {
            'removed': [person_id],
            'upserted': [dict(updated_person)] if updated_person else [],
        },
    )
    if merged_face_ids:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [
                    {'id': fid, 'person_id': target_id, 'person_name': updated_person['name']}
                    for fid in merged_face_ids
                ],
            },
        )

    return success_response(
        {'message': f'Merged "{from_person["name"]}" into "{to_person["name"]}"', 'person': updated_person}
    )


@app.route('/api/people/<person_id>/dissolve', methods=['POST'])
def dissolve_person(person_id):
    """Dissolve a person - unidentify all their faces and delete the person.

    All faces return to the unknown pool (person_id set to NULL).

    Args:
        person_id: Person's UUID.

    Returns:
        Success message with count of affected faces.
    """
    db = get_db()

    with db._db_lock:
        # Verify person exists
        person = get_person(db.conn, person_id)
        if person is None:
            return error_response('Person not found', 404)

        # Get face IDs before dissolving (for event payload)
        dissolved_faces = db.conn.execute(
            'SELECT id FROM faces WHERE person_id = ? AND suppressed = 0', (person_id,)
        ).fetchall()
        dissolved_face_ids = [f['id'] for f in dissolved_faces]
        face_count = len(dissolved_face_ids)

        # Unidentify all faces (set person_id to NULL)
        db.conn.execute(
            """UPDATE faces SET person_id = NULL, manually_tagged = 0,
            updated_at = datetime('now') WHERE person_id = ?""",
            (person_id,),
        )

        # Delete the person
        delete_person(db.conn, person_id)
        db.conn.commit()

    # Broadcast for other clients
    db.event_queue.emit(EVENT_PEOPLE_CHANGED, {'removed': [person_id]})
    if dissolved_face_ids:
        db.event_queue.emit(
            EVENT_FACES_CHANGED,
            {
                'updated': [{'id': fid, 'person_id': None} for fid in dissolved_face_ids],
            },
        )

    return success_response({'message': f'Dissolved "{person["name"]}"', 'faces_released': face_count})


# =============================================================================
# Error Handlers
# =============================================================================


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors with JSON response."""
    return error_response('Resource not found', 404)


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors with JSON response."""
    logger.exception('Internal server error: %s', _format_request_context())
    return error_response('Internal server error', 500)


def _format_request_context(max_url_len: int = 200, max_body_len: int = 200) -> str:
    """Format the current request as a concise string for error logs.

    Includes method, URL (truncated), and a body preview for mutations.
    Safe to call outside a request context (returns 'no request context').
    """
    try:
        method = request.method
        url = request.url
        if len(url) > max_url_len:
            url = url[:max_url_len] + '…'
        parts = [f'{method} {url}']

        # Include body preview for mutations (POST/PATCH/DELETE) to help
        # debug batch operations without swamping the terminal
        if method in ('POST', 'PATCH', 'DELETE'):
            body = request.get_data(as_text=True, cache=True)
            if body:
                preview = body[:max_body_len]
                if len(body) > max_body_len:
                    preview += f'… ({len(body)} bytes total)'
                parts.append(f'body={preview}')

        return ' | '.join(parts)
    except RuntimeError:
        return 'no request context'


# =============================================================================
# CLI Commands
# =============================================================================


def run_generate_thumbnails_cli():
    """CLI wrapper for generate_missing_thumbnails."""
    db = get_db()
    images = db.get_images_for_thumbnail_generation()
    generate_missing_thumbnails(
        images=images,
        thumbnail_dir=db.thumbnail_dir,
        quality=db.config.thumbnail_quality,
        max_source_dimension=db.config.max_image_dimension,
    )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == '__main__':
    from config import Config, get_default_config_path, load_config, save_config

    parser = argparse.ArgumentParser(description='Photonarium - Image Catalogue Server')
    parser.add_argument(
        '-s', '--scan', action='store_true', help='Scan folders and compute image CLIP embeddings on startup'
    )
    parser.add_argument(
        '-f',
        '--detect-faces',
        action='store_true',
        help='Run face detection after image CLIP embeddings complete (requires --scan)',
    )
    parser.add_argument(
        '-F',
        '--group-faces',
        action='store_true',
        help='Compute face/duplicate grouping after face detection (requires --detect-faces)',
    )
    parser.add_argument(
        '-p', '--port', type=int, default=None, help='Port to run the server on (overrides config, default: 5000)'
    )
    parser.add_argument(
        '-g', '--generate-thumbnails', action='store_true', help='Generate missing thumbnails for all images and exit'
    )
    parser.add_argument(
        '-r',
        '--rebuild-duplicates',
        action='store_true',
        help='Force full recomputation of all duplicate groups and exit',
    )
    parser.add_argument(
        '-e',
        '--generate-face-embeddings',
        action='store_true',
        help='Generate CLIP embeddings for faces (for text search) and exit',
    )
    parser.add_argument(
        '-t',
        '--regenerate-face-thumbnails',
        action='store_true',
        help='Regenerate all face thumbnails with non-distorted rendering and exit',
    )
    parser.add_argument(
        '-x', '--extract-exif', action='store_true', help='Extract EXIF metadata for all images missing it and exit'
    )
    parser.add_argument(
        '-m',
        '--list-models',
        action='store_true',
        help='Output required ML models as JSON and exit (for download_models.py)',
    )
    parser.add_argument(
        '-d',
        '--data-dir',
        type=str,
        default=None,
        help='Runtime override for data directory (database, thumbnails, models). '
        'Does not persist to config — use --init-config for that.',
    )
    parser.add_argument(
        '-c',
        '--config',
        type=str,
        default=None,
        dest='config_path',
        help='Path to configuration file (default: OS-appropriate location)',
    )
    parser.add_argument(
        '-a',
        '--add-folder',
        type=str,
        action='append',
        default=None,
        metavar='PATH',
        help='Register a folder for indexing (repeatable). '
        'Adds the folder to the database and exits. '
        'Useful for headless servers where the GUI folder picker is unavailable.',
    )
    parser.add_argument(
        '--init-config',
        type=str,
        default=None,
        metavar='DATA_DIR',
        help='Create/update config at OS default with the given data_dir and exit. '
        'Used by the installer to persist the chosen data directory.',
    )
    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # Phase 1: Resolve config path and load config
    # -------------------------------------------------------------------------
    # Resolution order: --config > PHOTONARIUM_CONFIG env > OS default
    _config_path_arg = args.config_path  # May be None

    # Handle --init-config: create/update config and exit immediately
    if args.init_config is not None:
        _init_data_dir = os.path.abspath(args.init_config)
        _config = load_config(
            config_path=_config_path_arg,
            initial_data_dir=_init_data_dir,
        )
        # If config already existed, update data_dir and re-save
        if _config.data_dir != _init_data_dir:
            _config.data_dir = _init_data_dir
            _resolved_cfg_path = Path(_config_path_arg) if _config_path_arg else get_default_config_path()
            save_config(_config, _resolved_cfg_path)

        _resolved_cfg_path = Path(_config_path_arg) if _config_path_arg else get_default_config_path()
        logger.info(f'Config initialised: {_resolved_cfg_path}')
        logger.info(f'  data_dir = {_init_data_dir}')
        sys.exit(0)

    # Normal startup: load config (creates default if needed)
    _config = load_config(
        config_path=_config_path_arg,
        initial_data_dir=args.data_dir,
    )

    # Store the resolved config file path for /api/config/reveal
    if _config_path_arg:
        _config_file_path = os.path.abspath(_config_path_arg)
    elif os.environ.get('PHOTONARIUM_CONFIG'):
        _config_file_path = os.path.abspath(os.environ['PHOTONARIUM_CONFIG'])
    else:
        _config_file_path = str(get_default_config_path())

    # -------------------------------------------------------------------------
    # Phase 2: Resolve data directory from config + CLI override
    # -------------------------------------------------------------------------
    # Resolution order: --data-dir CLI > config.data_dir > '.' (cwd)
    if args.data_dir is not None:
        _data_dir = os.path.abspath(args.data_dir)
    elif _config.data_dir:
        _data_dir = os.path.abspath(_config.data_dir)
    else:
        _data_dir = os.path.abspath('.')

    os.makedirs(_data_dir, exist_ok=True)

    # Apply data_dir to path globals (env vars still take precedence)
    if not os.environ.get('PHOTONARIUM_DB'):
        DATABASE_PATH = os.path.join(_data_dir, 'photonarium.db')
    if not os.environ.get('PHOTONARIUM_THUMBNAILS'):
        THUMBNAIL_CACHE_DIR = os.path.join(_data_dir, '.thumbnails')

    # -------------------------------------------------------------------------
    # Phase 3: Handle CLI commands
    # -------------------------------------------------------------------------

    # Handle list-models command (outputs JSON for download_models.py)
    if args.list_models:
        import json

        models = {
            'openclip': {
                'model': _config.openclip_model,
                'pretrained': _config.openclip_pretrained,
            },
            'caption': {
                'model': _config.caption_model,
            },
            'laion_head': {
                'model': _config.openclip_model,
                'pretrained': _config.openclip_pretrained,
                'data_dir': _data_dir,
            },
            'nima': {
                'enabled': _config.nima_enabled,
                'data_dir': _data_dir,
            },
        }
        print(json.dumps(models))
        sys.exit(0)

    # Handle add-folder command (register folders for indexing, then exit)
    if args.add_folder:
        db = get_db()
        added = 0
        for folder_path in args.add_folder:
            abs_path = os.path.abspath(folder_path)
            if not os.path.isdir(abs_path):
                logger.error(f'Not a directory: {abs_path}')
                continue
            result = db.add_folder(abs_path)
            if result is None:
                logger.info(f'Already registered: {abs_path}')
            else:
                logger.info(f'Added folder: {abs_path}')
                added += 1
        if added:
            logger.info(f'Registered {added} new folder(s). Run with --scan to index their images.')
        sys.exit(0)

    # Handle thumbnail generation command
    if args.generate_thumbnails:
        # Don't scan, just open database
        get_db()
        run_generate_thumbnails_cli()
        sys.exit(0)

    # Handle duplicate rebuild command
    if args.rebuild_duplicates:
        db = get_db()
        logger.info('Starting full duplicate group recomputation...')
        start_time = time.time()
        group_counts = db._duplicate_manager.compute_all(force_full=True)
        elapsed = time.time() - start_time
        logger.info(f'Duplicate recomputation completed in {elapsed:.1f}s')
        for level, count in sorted(group_counts.items()):
            logger.info(f'  Level {level}: {count} groups')
        sys.exit(0)

    # Handle face embedding generation command
    if args.generate_face_embeddings:
        db = get_db()
        logger.info('Starting face CLIP embedding generation (for text search)...')
        start_time = time.time()
        count = db.backfill_face_semantic_embeddings()
        elapsed = time.time() - start_time
        logger.info(f'Generated {count} face CLIP embeddings in {elapsed:.1f}s')
        sys.exit(0)

    # Handle face thumbnail regeneration command
    if args.regenerate_face_thumbnails:
        db = get_db()
        logger.info('Starting face thumbnail regeneration...')
        start_time = time.time()
        count = db.regenerate_face_thumbnails()
        elapsed = time.time() - start_time
        logger.info(f'Regenerated {count} face thumbnails in {elapsed:.1f}s')
        sys.exit(0)

    # Handle EXIF extraction backfill command
    if args.extract_exif:
        from concurrent.futures import as_completed

        db = get_db()
        logger.info('Extracting EXIF metadata for images missing it...')
        start_time = time.time()

        # Find images with no EXIF data (NULL = never extracted)
        rows = db.get_images_without_exif()
        total = len(rows)

        if total == 0:
            logger.info('All images already have EXIF data extracted.')
            sys.exit(0)

        logger.info(f'Found {total} images needing EXIF extraction')

        extracted = 0
        skipped = 0
        processed = 0
        interrupted = False
        num_workers = db.config.indexing_threads or 4
        executor = ThreadPoolExecutor(max_workers=num_workers)

        try:
            futures = {executor.submit(db.extract_exif_for_image, row['id']): row for row in rows}

            for future in as_completed(futures):
                try:
                    if future.result():
                        extracted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    row = futures[future]
                    logger.warning(f'EXIF extraction failed for {row["basename"]}: {e}')
                    skipped += 1
                processed += 1

                if processed % 100 == 0 or processed == total:
                    logger.info(f'  Progress: {processed}/{total} ({extracted} extracted, {skipped} no EXIF)')

        except KeyboardInterrupt:
            logger.warning('Interrupted! Cancelling pending tasks...')
            interrupted = True
            for future in futures:
                future.cancel()
        finally:
            executor.shutdown(wait=not interrupted, cancel_futures=interrupted)

        elapsed = time.time() - start_time
        status = 'interrupted' if interrupted else 'completed'
        logger.info(f'EXIF extraction {status} in {elapsed:.1f}s: {extracted} extracted, {skipped} no EXIF')
        sys.exit(0)

    # -------------------------------------------------------------------------
    # Phase 4: Start the server
    # -------------------------------------------------------------------------

    # Set module-level flags before initializing database
    _run_scan = args.scan
    _run_face_detection = args.detect_faces
    _run_face_grouping = args.group_faces

    # Initialise database before starting server
    get_db()

    # Resolve server host/port: CLI --port overrides config, config overrides defaults
    server_host = db.config.server_host
    server_port = args.port if args.port is not None else db.config.server_port

    # Print ready banner
    logger.info('=' * 60)
    logger.info('SERVER READY')
    logger.info('=' * 60)
    logger.info(f'Data directory: {_data_dir}')
    if server_host == '0.0.0.0':
        logger.info(f'Open http://localhost:{server_port} in your browser')
        logger.info('Also available to other devices on your network')
    else:
        logger.info(f'Open http://{server_host}:{server_port} in your browser')
    logger.info('=' * 60)

    # Try to use waitress (production WSGI server), fall back to Flask dev server
    try:
        from waitress import serve

        logger.info('Using waitress WSGI server')
        serve(app, host=server_host, port=server_port, threads=8)
    except ImportError:
        logger.warning('waitress not installed, using Flask dev server (slow!)')
        logger.warning('Install with: pip install waitress')
        app.run(
            host=server_host,
            port=server_port,
            debug=False,
            threaded=True,
        )
