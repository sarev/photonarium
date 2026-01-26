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
    """List images with support for incremental updates.

    If 'since' query parameter is provided, returns only changes since that
    epoch (timestamp), allowing efficient incremental updates. Otherwise
    returns all images.

    Query Parameters:
        since: Optional ISO timestamp. If provided, returns delta update with
               only images changed since that time.

    Returns:
        Without 'since': JSON object with 'epoch' and 'images' array.
        With 'since': JSON object with 'epoch', 'updated' array, and
                      'deleted_ids' array for incremental sync.
    """
    since = request.args.get('since')

    if since:
        # Delta update - return only changes since the given epoch
        delta = get_db().get_images_delta(since)
        return jsonify(delta)
    else:
        # Full load - return all images with current epoch
        images = get_db().get_all_images_lightweight()
        epoch = get_db().get_current_epoch()
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
        if not generate_thumbnail(source_path, thumbnail_path, size, db.config.thumbnail_quality):
            abort(404)

    # Read from disk and cache
    try:
        with open(thumbnail_path, 'rb') as f:
            data = f.read()
        cache.put(checksum, size, data)
        return Response(data, mimetype='image/jpeg')
    except IOError:
        abort(404)


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
        '--generate-thumbnails',
        action='store_true',
        help='Generate missing thumbnails for all images and exit'
    )
    args = parser.parse_args()

    # Handle thumbnail generation command
    if args.generate_thumbnails:
        # Skip scanning, just open database
        _skip_scan = True
        get_db()
        run_generate_thumbnails_cli()
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
