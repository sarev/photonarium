"""Flask backend for the Imaginary image catalogue application.

This module provides the REST API that the frontend communicates with.
It handles HTTP requests and delegates to the imagedb module for
database operations and image processing.

Routes:
    /api/images         - Image listing and management
    /api/folders        - Folder registration and removal
    /api/status         - Processing status
    /api/rescan         - Trigger folder rescan
    /api/duplicates     - Duplicate group retrieval
    /api/stats          - Database statistics

Example:
    To run the development server::

        $ python app.py

    The server will start on http://localhost:5000 by default.
"""

# Disable tokenizers parallelism before any imports.
# Prevents Ctrl+C issues on Windows caused by Rust threads.
import os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

import argparse
import atexit
import base64
import io
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from PIL import Image, ImageDraw

import orjson
from flask import Flask, Response, request, send_file, abort
from flask import jsonify as flask_jsonify
# flask_cors not needed for localhost-only deployment (same-origin requests)

# Toggle between orjson and stdlib json for testing
USE_ORJSON = True

def jsonify(data):
    """JSON response - uses orjson when USE_ORJSON is True."""
    if USE_ORJSON:
        return Response(
            orjson.dumps(data),
            mimetype='application/json'
        )
    else:
        return flask_jsonify(data)

from caption import CaptionGenerator
from imagedb import ImageDatabase, register_signal_handlers
from thumbnails import (
    get_thumbnail_cache_path,
    generate_thumbnail,
    generate_missing_thumbnails,
    ThumbnailCache,
)
from faces import (
    get_all_people,
    get_person,
    get_person_by_name,
    create_person,
    update_person,
    delete_person,
    search_people,
    get_face,
    get_all_faces,
    get_faces_for_image,
    get_faces_for_person,
    update_face_person,
    toggle_face_manual_tag,
    suppress_face,
    delete_face,
    get_face_thumbnail_path,
    generate_face_thumbnail,
    get_images_with_people,
    get_people_names_bulk,
    delete_people_without_faces,
    batch_identify_faces,
    reassess_unknown_faces_async,
    get_reassessment_status,
    clear_reassessment_result,
    compute_unknown_face_groups_async,
    get_group_computation_status,
    revalidate_person_faces,
    search_unknown_faces_semantic,
)

# Configure logging - set root logger to WARNING, our modules to INFO
logging.basicConfig(
    level=logging.WARNING,  # Default to WARNING for third-party libraries
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

# Set our modules to INFO level
for module in ['app', 'imagedb', 'faces', 'thumbnails', 'duplicates', 'config', 'timestamps']:
    logging.getLogger(module).setLevel(logging.INFO)

app = Flask(__name__, static_folder='static', static_url_path='')

# CORS is not needed for localhost-only deployment since the frontend
# is served from the same origin. If you need to run a separate frontend
# dev server, uncomment and restrict origins appropriately:
# CORS(app, origins=['http://localhost:5000', 'http://127.0.0.1:5000'])


# =============================================================================
# Configuration
# =============================================================================

DATABASE_PATH = os.environ.get('IMAGINARY_DB', 'imaginary.db')
THUMBNAIL_CACHE_DIR = os.environ.get('IMAGINARY_THUMBNAILS', '.thumbnails')
CONFIG_PATH = os.environ.get('IMAGINARY_CONFIG', None)


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


def get_caption_generator() -> CaptionGenerator:
    """Get the caption generator, initializing if necessary."""
    global _caption_generator
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
            config_path=CONFIG_PATH,
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
    data = {'epoch': epoch, 'images': images}
    json_bytes = orjson.dumps(data)
    _set_images_cache(epoch, json_bytes)
    elapsed = time.perf_counter() - t0
    logger.info(f'Images cache pre-populated: {len(images)} images, {len(json_bytes)//1024//1024}MB, {elapsed*1000:.0f}ms')


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


def get_thumbnail_cache() -> ThumbnailCache:
    """Get the thumbnail cache instance, initializing if necessary."""
    global _thumbnail_cache
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
                delta['updated'] = [
                    img for img in delta['updated']
                    if img['id'] in matching_image_ids
                ]

        return jsonify(delta)
    else:
        # Full load - use cached response if available
        db = get_db()

        if person_ids:
            # Filter by people - can't use cache
            matching_image_ids = set(get_images_with_people(db.conn, person_ids))
            all_images = db.get_all_images_lightweight()
            images = [img for img in all_images if img['id'] in matching_image_ids]
            epoch = db.get_current_epoch()
            return jsonify({'epoch': epoch, 'images': images})
        else:
            # Use cached JSON bytes if epoch matches
            epoch = db.get_current_epoch()
            cached = _get_images_cache()
            if cached and cached['epoch'] == epoch:
                return Response(cached['bytes'], mimetype='application/json')

            # Cache miss - build and cache response
            images = db.get_all_images_lightweight()
            data = {'epoch': epoch, 'images': images}
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
    return jsonify(image)


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

    image = get_db().update_image(image_id, allowed_updates)
    if image is None:
        return error_response('Image not found', 404)
    return jsonify(image)


@app.route('/api/images/<image_id>/generate-caption', methods=['POST'])
def generate_caption(image_id):
    """Generate an AI caption for an image.

    Uses the BLIP model to generate a natural language description
    of the image. The temperature setting from config controls the
    creativity/diversity of the generated text.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON object with:
            - caption: The generated caption text
        Or error if image not found or generation fails.
    """
    db = get_db()
    image = db.get_image(image_id)
    if image is None:
        return error_response('Image not found', 404)

    path = image.get('path')
    if not path:
        return error_response('Image path not found', 404)

    try:
        generator = get_caption_generator()
        caption = generator.generate(path)
    except Exception as e:
        logger.exception(f'Failed to generate caption for image {image_id}')
        return error_response(f'Caption generation failed: {e}', 500)

    if caption is None:
        return error_response('Failed to generate caption', 500)

    return jsonify({'caption': caption})


@app.route('/api/images/<image_id>', methods=['DELETE'])
def delete_image(image_id):
    """Delete an image from the database and optionally from disk.

    This removes the image entry from the database. The actual file
    deletion behaviour is controlled by the delete_file parameter.

    Args:
        image_id: The unique identifier of the image.

    Query Parameters:
        delete_file: If 'true', also delete the file from disk.
                    Defaults to 'false'.

    Returns:
        Success response, or 404 if image not found.
    """
    delete_file = request.args.get('delete_file', 'false').lower() == 'true'

    # Get checksum before deletion for cache invalidation
    db = get_db()
    checksum = db.get_checksum(image_id)

    success = db.delete_image(image_id, from_disk=delete_file)
    if not success:
        return error_response('Image not found', 404)

    # Invalidate thumbnail RAM cache
    if checksum:
        get_thumbnail_cache().remove(checksum)

    return success_response(message='Image deleted')


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
            source_path, thumbnail_path, size,
            db.config.thumbnail_quality, db.config.max_image_dimension
        ):
            abort(404)

    # Read from disk and cache
    try:
        with open(thumbnail_path, 'rb') as f:
            data = f.read()
        cache.put(checksum, size, data)
        return Response(data, mimetype='image/jpeg')
    except IOError:
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
                    source_path, thumbnail_path, size,
                    db.config.thumbnail_quality, db.config.max_image_dimension
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

        return jsonify({
            'r': img_to_data_url(red_img),
            'g': img_to_data_url(green_img),
            'b': img_to_data_url(blue_img),
        })

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

    return send_file(path)


@app.route('/api/images/<image_id>/reveal', methods=['POST'])
def reveal_image(image_id):
    """Open the containing folder and select the image file.

    Uses platform-specific commands to reveal the image in the file manager:
    - Windows: explorer /select
    - macOS: open -R
    - Linux: xdg-open (opens folder only)

    Args:
        image_id: The unique identifier of the image.

    Returns:
        Success response, or 404 if image not found.
    """
    image = get_db().get_image(image_id)
    if image is None:
        return error_response('Image not found', 404)

    path = image['path']
    if not os.path.exists(path):
        return error_response('Image file not found on disk', 404)

    try:
        if sys.platform == 'win32':
            # Windows: explorer /select highlights the file
            subprocess.run(['explorer', '/select,', path], check=False)
        elif sys.platform == 'darwin':
            # macOS: open -R reveals file in Finder
            subprocess.run(['open', '-R', path], check=True)
        else:
            # Linux: open the containing folder (no file selection)
            folder = os.path.dirname(path)
            subprocess.run(['xdg-open', folder], check=True)
        return success_response(message='Folder opened')
    except Exception as e:
        logger.exception('Failed to open folder')
        return error_response(f'Failed to open folder: {str(e)}', 500)


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
            - direction: 'cw' for clockwise, 'ccw' for counter-clockwise

    Returns:
        JSON object with:
            - results: Object mapping image_id to success boolean
            - rotated: Array of successfully rotated image IDs
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    if 'direction' not in data:
        return error_response('Direction is required (cw or ccw)')

    direction = data['direction']
    if direction not in ('cw', 'ccw'):
        return error_response('Direction must be "cw" or "ccw"')

    image_ids = data.get('image_ids', [])
    if not image_ids:
        return error_response('At least one image_id is required')

    if not isinstance(image_ids, list):
        return error_response('image_ids must be an array')

    results = get_db().rotate_images(image_ids, direction)

    # Invalidate thumbnail RAM cache for old checksums
    old_checksums = results.pop('old_checksums', [])
    if old_checksums:
        cache = get_thumbnail_cache()
        for checksum in old_checksums:
            cache.remove(checksum)

    return jsonify(results)


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
    return jsonify(names)


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
    return jsonify(folders)


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

    return jsonify({'path': selected_path})


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

    return jsonify(status)


@app.route('/api/config', methods=['GET'])
def get_config():
    """Get frontend configuration values.

    Returns configuration values needed by the frontend, particularly
    for thumbnail loading behaviour.

    Returns:
        JSON object with:
            - thumbnail_concurrent_requests: Max concurrent fetches
            - thumbnail_extra_rows: Buffer rows above/below viewport
            - thumbnail_timeout_ms: Fetch timeout in milliseconds
            - thumbnail_scroll_throttle_ms: Scroll throttle in milliseconds
    """
    config = get_db().config
    return jsonify({
        'thumbnail_concurrent_requests': config.thumbnail_concurrent_requests,
        'thumbnail_extra_rows': config.thumbnail_extra_rows,
        'thumbnail_timeout_ms': config.thumbnail_timeout_ms,
        'thumbnail_scroll_throttle_ms': config.thumbnail_scroll_throttle_ms,
    })


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

    # Validate level
    if level < 0 or level > 3:
        return error_response('Level must be between 0 and 3')

    db = get_db()
    status = db.get_duplicate_status().get(level, 'pending')
    epoch = db.get_duplicate_epoch()

    # If client has current data, return minimal response
    if since and since == epoch and status == 'done':
        return jsonify({
            'groups': [],
            'status': status,
            'epoch': epoch,
            'unchanged': True,
        })

    # Return lightweight group data
    groups = db.get_duplicate_groups_lightweight(level)
    return jsonify({
        'groups': groups,
        'status': status,
        'epoch': epoch,
    })


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

    try:
        scores = get_db().get_semantic_scores_for_images(query, image_ids)
        return jsonify({'scores': scores})
    except Exception as e:
        logger.exception('Semantic sort failed')
        return error_response(f'Semantic sort failed: {str(e)}', 500)


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
        return jsonify({'results': results})
    except Exception as e:
        logger.exception('Search failed')
        return error_response(f'Search failed: {str(e)}', 500)


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
        return jsonify({'results': results})
    except Exception as e:
        logger.exception('Similarity search failed')
        return error_response(f'Similarity search failed: {str(e)}', 500)


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
    return jsonify(stats)


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
    return jsonify(get_thumbnail_cache().stats())


# =============================================================================
# SSE Events Endpoint (Optional)
# =============================================================================

@app.route('/api/events', methods=['GET'])
def event_stream():
    """Server-Sent Events endpoint for real-time updates.

    Clients can connect to this endpoint to receive real-time
    notifications about processing status, folder changes, etc.

    Returns:
        SSE event stream.
    """
    def generate():
        for event in get_db().get_event_stream(timeout=30.0):
            yield event

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',  # Disable nginx buffering
        }
    )


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

    return jsonify(people)


@app.route('/api/people', methods=['POST'])
def create_person_endpoint():
    """Create a new person.

    Request Body:
        JSON object with:
            - name: Person's name (required)

    Returns:
        JSON object with the created person.
    """
    data = request.get_json()
    if not data:
        return error_response('Request body is required')

    name = data.get('name', '').strip()
    if not name:
        return error_response('Name is required')

    db = get_db()

    # Check if person with this name already exists and create (with lock)
    with db._db_lock:
        existing = get_person_by_name(db.conn, name)
        if existing:
            return error_response(f'Person with name "{name}" already exists', 409)

        person_id = create_person(db.conn, name)
        person = get_person(db.conn, person_id)

    return success_response(person)


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

    person = get_person(db.conn, person_id)
    if person is None:
        return error_response('Person not found', 404)

    name = data.get('name')
    if name is not None:
        name = name.strip()
        if not name:
            return error_response('Name cannot be empty')
        # Check if another person has this name
        existing = get_person_by_name(db.conn, name)
        if existing and existing['id'] != person_id:
            return error_response(f'Person with name "{name}" already exists', 409)

    preferred_face_id = data.get('preferred_face_id')

    # Handle recognition_threshold: present key means update, absent means don't change
    update_kwargs = {'name': name, 'preferred_face_id': preferred_face_id}
    threshold_changed = False
    threshold_value = None
    if 'recognition_threshold' in data:
        threshold = data['recognition_threshold']
        # Validate threshold if provided
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
    with db._db_lock:
        update_person(db.conn, person_id, **update_kwargs)

        # If threshold was changed to a non-null value, revalidate and reassess
        if threshold_changed and threshold_value is not None:
            # Eject faces that no longer meet the threshold
            ejected_face_ids = revalidate_person_faces(db.conn, person_id, threshold_value)

            # If all faces were ejected, delete the person
            if ejected_face_ids:
                remaining = get_faces_for_person(db.conn, person_id)
                if not remaining:
                    delete_person(db.conn, person_id)
                    return success_response({
                        'deleted': True,
                        'ejected_face_ids': ejected_face_ids,
                        'message': 'All faces ejected, person deleted'
                    })
                faces_changed = True

        updated_person = get_person(db.conn, person_id)

    # Trigger async reassessment to potentially add matching unknowns
    if threshold_changed and threshold_value is not None:
        reassess_unknown_faces_async(
            db,
            threshold=threshold_value,
            person_id=person_id,
        )

    response_data = dict(updated_person) if updated_person else {}
    if ejected_face_ids:
        response_data['ejected_face_ids'] = ejected_face_ids
    response_data['faces_changed'] = faces_changed or (threshold_changed and threshold_value is not None)

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

        delete_person(db.conn, person_id)

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

    return jsonify(faces)


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
    if not face_id:
        faces = get_faces_for_person(db.conn, person_id)
        if not faces:
            return error_response('Person has no faces', 404)
        face_id = faces[0]['id']

    # Get face thumbnail
    thumb_path = get_face_thumbnail_path(face_id, db.thumbnail_dir)
    if not thumb_path.exists():
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

    return jsonify(faces)


@app.route('/api/faces', methods=['GET'])
def get_faces_list():
    """Get all non-suppressed faces.

    Query Parameters:
        unknown: If 'true', only return faces without a person_id.
        search: Text query for semantic search (unknown faces only).
                When provided, returns unknown faces sorted by similarity.

    Returns:
        JSON array of face objects with person_name if identified.
        Ordered by: known faces (alphabetically by person name), then unknown faces.
        When search is provided: unknown faces sorted by similarity descending.
    """
    unknown_only = request.args.get('unknown', '').lower() == 'true'
    search_query = request.args.get('search', '').strip()

    db = get_db()

    # If search query provided, do semantic search on unknown faces
    if search_query:
        try:
            # Encode query with CLIP
            query_embedding = db._get_clip_model().encode_text(search_query)
            # Search unknown faces by semantic similarity
            faces = search_unknown_faces_semantic(db.conn, query_embedding)
            return jsonify(faces)
        except Exception as e:
            logger.error(f'Failed to encode search query: {e}')
            return error_response('Failed to encode search query', 500)

    faces = get_all_faces(db.conn, unknown_only=unknown_only)
    return jsonify(faces)


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

    return success_response({
        'face': face,
        'person': person,
    })


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

        result = batch_identify_faces(
            db.conn,
            face_ids,
            name,
            preferred_face_id
        )

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
    logger.info(f'[FacesFlow] identify-batch SUCCESS: person_id={result["person"]["id"]}, identified={len(result["faces"])} faces')
    return success_response(response_data)


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

        # Unlink face from person
        update_face_person(db.conn, face_id, None)

        # Delete person if they have no more faces
        if old_person_id:
            delete_people_without_faces(db.conn)

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
                '''SELECT id FROM faces
                   WHERE person_id = ? AND suppressed = 0
                   ORDER BY created_at DESC
                   LIMIT 1''',
                (old_person_id,)
            ).fetchone()

            if not remaining_faces:
                # No faces left - delete the person
                delete_people_without_faces(db.conn)
                person_deleted = True
            elif was_preferred:
                # Person still has faces but lost their preferred - select new one
                db.conn.execute(
                    'UPDATE people SET preferred_face_id = ? WHERE id = ?',
                    (remaining_faces['id'], old_person_id)
                )
                db.conn.commit()
                new_preferred_selected = True

    return success_response(
        message='Face suppressed',
        data={
            'person_deleted': person_deleted,
            'new_preferred_selected': new_preferred_selected,
        }
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

    return success_response(
        message='Manual tag toggled',
        data={'manually_tagged': new_value}
    )


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
        if old_person_id:
            delete_people_without_faces(db.conn)

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
    if not thumb_path.exists():
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
                    logger.info(f'Regenerating missing face thumbnail: {face_id}')
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

        # If this was the preferred face, auto-select a new one
        if person and person.get('preferred_face_id') == face_id:
            remaining_faces = get_faces_for_person(db.conn, old_person_id)
            if remaining_faces:
                # Select newest face (last in list, sorted by timestamp ASC)
                new_preferred = remaining_faces[-1]['id']
                update_person(db.conn, old_person_id, preferred_face_id=new_preferred)

        # Delete person if they have no more faces
        delete_people_without_faces(db.conn)

        # Get updated person (or None if deleted)
        updated_person = get_person(db.conn, old_person_id)

    # Note: We don't trigger group recalculation here - it's too expensive
    # for interactive use. Groups are computed during initial processing
    # or via explicit "Rescan" request.

    return success_response({
        'message': 'Face unassigned',
        'person': updated_person,  # Will be None if person was deleted
    })


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

        # Phase 3: Delete people with no more faces
        delete_people_without_faces(db.conn)

    # Note: We don't trigger group recalculation here - it's too expensive
    # for interactive use (~minutes for 30k faces). Groups are computed
    # during initial processing or via explicit "Rescan" request.

    return success_response({
        'message': f'{unassigned_count} faces unassigned',
        'unassigned_count': unassigned_count,
    })


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
        db.conn.execute('UPDATE faces SET manually_tagged = 1 WHERE id = ?', (face_id,))
        db.conn.commit()

        # Get updated person
        updated_person = get_person(db.conn, person_id)

    return success_response(updated_person)


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
    logger.exception('Internal server error')
    return error_response('Internal server error', 500)


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
    parser = argparse.ArgumentParser(description='Imaginary - Image Catalogue Server')
    parser.add_argument(
        '-s', '--scan',
        action='store_true',
        help='Scan folders and compute image CLIP embeddings on startup'
    )
    parser.add_argument(
        '-f', '--detect-faces',
        action='store_true',
        help='Run face detection after image CLIP embeddings complete (requires --scan)'
    )
    parser.add_argument(
        '-F', '--group-faces',
        action='store_true',
        help='Compute face/duplicate grouping after face detection (requires --detect-faces)'
    )
    parser.add_argument(
        '-p', '--port',
        type=int,
        default=5000,
        help='Port to run the server on (default: 5000)'
    )
    parser.add_argument(
        '-g', '--generate-thumbnails',
        action='store_true',
        help='Generate missing thumbnails for all images and exit'
    )
    parser.add_argument(
        '-r', '--rebuild-duplicates',
        action='store_true',
        help='Force full recomputation of all duplicate groups and exit'
    )
    parser.add_argument(
        '-e', '--generate-face-embeddings',
        action='store_true',
        help='Generate CLIP embeddings for faces (for text search) and exit'
    )
    parser.add_argument(
        '-t', '--regenerate-face-thumbnails',
        action='store_true',
        help='Regenerate all face thumbnails with non-distorted rendering and exit'
    )
    parser.add_argument(
        '-m', '--list-models',
        action='store_true',
        help='Output required ML models as JSON and exit (for download_models.py)'
    )
    args = parser.parse_args()

    # Handle list-models command (outputs JSON for download_models.py)
    if args.list_models:
        import json
        from config import load_config
        config = load_config(CONFIG_PATH)
        models = {
            'openclip': {
                'model': config.openclip_model,
                'pretrained': config.openclip_pretrained,
            },
            'caption': {
                'model': config.caption_model,
            },
        }
        print(json.dumps(models))
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

    # Set module-level flags before initializing database
    _run_scan = args.scan
    _run_face_detection = args.detect_faces
    _run_face_grouping = args.group_faces

    # Initialise database before starting server
    get_db()

    # Print ready banner
    logger.info('=' * 60)
    logger.info('SERVER READY')
    logger.info('=' * 60)
    logger.info(f'Open http://localhost:{args.port} in your browser')
    logger.info('=' * 60)

    # Try to use waitress (production WSGI server), fall back to Flask dev server
    # Bind to 127.0.0.1 (localhost only) for security - no network exposure
    try:
        from waitress import serve
        logger.info('Using waitress WSGI server')
        serve(app, host='127.0.0.1', port=args.port, threads=8)
    except ImportError:
        logger.warning('waitress not installed, using Flask dev server (slow!)')
        logger.warning('Install with: pip install waitress')
        app.run(
            host='127.0.0.1',
            port=args.port,
            debug=False,
            threaded=True,
        )
