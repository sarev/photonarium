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

import atexit
import logging
import os
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, abort
# flask_cors not needed for localhost-only deployment (same-origin requests)

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
    get_faces_for_image,
    get_faces_for_person,
    update_face_person,
    suppress_face,
    delete_face,
    get_face_thumbnail_path,
    get_images_with_people,
    delete_people_without_faces,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

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
_skip_scan: bool = False  # Set via command-line args in __main__


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
            skip_scan=_skip_scan,
        )
        register_signal_handlers(db)
        logger.info('ImageDatabase initialised')
    return db


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
        # Full load - return all images with current epoch
        db = get_db()

        if person_ids:
            # Filter by people
            matching_image_ids = set(get_images_with_people(db.conn, person_ids))
            all_images = db.get_all_images_lightweight()
            images = [img for img in all_images if img['id'] in matching_image_ids]
        else:
            images = db.get_all_images_lightweight()

        epoch = db.get_current_epoch()
        return jsonify({
            'epoch': epoch,
            'images': images,
        })


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
    success = get_db().delete_image(image_id, from_disk=delete_file)
    if not success:
        return error_response('Image not found', 404)
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
    import io
    import base64
    from PIL import Image

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
        from PIL import ImageDraw
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
        import traceback
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
    import subprocess
    import sys

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
    return jsonify(results)


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
    the number of items remaining in the indexing and embedding queues.

    Returns:
        JSON object with:
            - status: 'up_to_date' or 'updating'
            - indexing_queue: Number of images awaiting indexing
            - embedding_queue: Number of images awaiting embedding
    """
    status = get_db().get_processing_status()
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

    # Check if person with this name already exists
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

    update_person(db.conn, person_id, name=name, preferred_face_id=preferred_face_id)
    updated_person = get_person(db.conn, person_id)

    return success_response(updated_person)


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

    # Remove embedding from response (it's large and not needed in API)
    for face in faces:
        if 'embedding' in face:
            del face['embedding']

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

    # Remove embedding from response
    for face in faces:
        if 'embedding' in face:
            del face['embedding']

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

    # Update face with person_id
    update_face_person(db.conn, face_id, person_id)

    # Get updated face
    face = get_face(db.conn, face_id)
    if 'embedding' in face:
        del face['embedding']

    return success_response({
        'face': face,
        'person': person,
    })


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

    Args:
        face_id: Face's UUID.

    Returns:
        Success message.
    """
    db = get_db()

    face = get_face(db.conn, face_id)
    if face is None:
        return error_response('Face not found', 404)

    old_person_id = face.get('person_id')

    suppress_face(db.conn, face_id)

    # Delete person if they have no more faces
    if old_person_id:
        delete_people_without_faces(db.conn)

    return success_response(message='Face suppressed')


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
        return error_response('Face thumbnail not found', 404)

    return send_file(
        thumb_path,
        mimetype='image/jpeg',
        max_age=31536000,  # 1 year cache
    )


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
    import argparse

    parser = argparse.ArgumentParser(description='Imaginary - Image Catalogue Server')
    parser.add_argument(
        '-n', '--no-scan',
        action='store_true',
        help='Skip the startup folder scan (faster startup when nothing changed)'
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
        '-f', '--detect-faces',
        action='store_true',
        help='Run face detection on images that haven\'t been processed and exit'
    )
    args = parser.parse_args()

    # Handle thumbnail generation command
    if args.generate_thumbnails:
        # Skip scanning, just open database
        _skip_scan = True
        get_db()
        run_generate_thumbnails_cli()
        sys.exit(0)

    # Handle duplicate rebuild command
    if args.rebuild_duplicates:
        import time
        _skip_scan = True
        db = get_db()
        logger.info('Starting full duplicate group recomputation...')
        start_time = time.time()
        group_counts = db._duplicate_manager.compute_all(force_full=True)
        elapsed = time.time() - start_time
        logger.info(f'Duplicate recomputation completed in {elapsed:.1f}s')
        for level, count in sorted(group_counts.items()):
            logger.info(f'  Level {level}: {count} groups')
        sys.exit(0)

    # Handle face detection command
    if args.detect_faces:
        import time
        from faces import has_faces_detected
        _skip_scan = True
        db = get_db()

        if not db.config.face_detection_enabled:
            logger.error('Face detection is disabled in config. Enable it first.')
            sys.exit(1)

        logger.info('Starting face detection for unprocessed images...')
        start_time = time.time()

        # Get all images that haven't been processed for faces
        with db._db_lock:
            cursor = db.conn.execute('SELECT id, path FROM images')
            all_images = cursor.fetchall()

        unprocessed = []
        for image_id, path in all_images:
            with db._db_lock:
                if not has_faces_detected(db.conn, image_id):
                    unprocessed.append(image_id)

        total = len(unprocessed)
        logger.info(f'Found {total} images without face detection')

        if total == 0:
            logger.info('No images need face detection.')
            sys.exit(0)

        # Queue all unprocessed images
        for image_id in unprocessed:
            db.queue_image_for_face_detection(image_id)

        # Wait for face detection to complete
        logger.info('Processing faces (this may take a while)...')
        while True:
            queue_size = db._face_queue.qsize()
            if queue_size == 0:
                # Give thread time to finish current item
                time.sleep(0.5)
                if db._face_queue.qsize() == 0:
                    break
            if queue_size % 10 == 0:
                logger.info(f'  Remaining: {queue_size}')
            time.sleep(0.5)

        elapsed = time.time() - start_time
        logger.info(f'Face detection completed in {elapsed:.1f}s')
        sys.exit(0)

    # Set module-level flag before initializing database
    _skip_scan = args.no_scan

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
