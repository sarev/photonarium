"""Flask backend for the Imaginary image catalogue application.

This module provides the REST API that the frontend communicates with.
It handles HTTP requests and delegates to the imagedb module for
database operations and image processing.

Routes:
    /api/images         - Image listing and management
    /api/folders        - Folder registration and removal
    /api/scan           - Database scanning with progress tracking
    /api/duplicates     - Duplicate group retrieval
    /api/stats          - Database statistics

Example:
    To run the development server::

        $ python app.py

    The server will start on http://localhost:5000 by default.
"""

import os
import uuid
from flask import Flask, jsonify, request, send_file, abort
from flask_cors import CORS

# Application logic will be handled by imagedb module (to be implemented)
# from imagedb import ImageDatabase

app = Flask(__name__, static_folder='.', static_url_path='')

# Enable CORS for development (frontend on different port)
# In production, remove or restrict to specific origins
CORS(app)


# =============================================================================
# Configuration
# =============================================================================

# TODO: Move to config file or environment variables
DATABASE_PATH = 'imaginary.db'
THUMBNAIL_CACHE_DIR = '.thumbnails'


# =============================================================================
# Database Instance
# =============================================================================

# Placeholder for the database instance
# db = ImageDatabase(DATABASE_PATH)
db = None  # Stubbed until imagedb is implemented


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
    return send_file('index.html')


# =============================================================================
# Image Endpoints
# =============================================================================

@app.route('/api/images', methods=['GET'])
def get_images():
    """List all images in the database with their metadata.

    The response includes all image metadata needed for the gallery view.
    Results can be filtered and sorted by the frontend.

    Query Parameters:
        None currently. Filtering/sorting is done client-side.

    Returns:
        JSON array of image objects, each containing:
            - id: Unique image identifier
            - path: Full file path
            - basename: Filename only
            - width, height: Dimensions in pixels
            - size: File size in bytes
            - timestamp: Best-guess date (ISO format)
            - description: User-editable text
            - rating: User-editable emoji string
            - checksum: SHA256 hash
            - laplacian_variance: Focus quality score
            - lossless: Boolean compression flag
    """
    # TODO: Implement with imagedb
    # images = db.get_all_images()
    # return jsonify(images)

    # Stub response
    return jsonify([])


@app.route('/api/images/<image_id>', methods=['GET'])
def get_image(image_id):
    """Get metadata for a single image.

    Args:
        image_id: The unique identifier of the image.

    Returns:
        JSON object with full image metadata, or 404 if not found.
    """
    # TODO: Implement with imagedb
    # image = db.get_image(image_id)
    # if image is None:
    #     return error_response('Image not found', 404)
    # return jsonify(image)

    # Stub response
    return error_response('Image not found', 404)


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
    # TODO: Implement with imagedb
    # data = request.get_json()
    # if not data:
    #     return error_response('No data provided')
    #
    # image = db.update_image(image_id, data)
    # if image is None:
    #     return error_response('Image not found', 404)
    # return jsonify(image)

    # Stub response
    return error_response('Image not found', 404)


@app.route('/api/images/<image_id>', methods=['DELETE'])
def delete_image(image_id):
    """Delete an image from the database and optionally from disk.

    This removes the image entry from the database. The actual file
    deletion behaviour is configurable (TODO: add config option).

    Args:
        image_id: The unique identifier of the image.

    Query Parameters:
        delete_file: If 'true', also delete the file from disk.
                    Defaults to 'false'.

    Returns:
        Success response, or 404 if image not found.
    """
    # TODO: Implement with imagedb
    # delete_file = request.args.get('delete_file', 'false').lower() == 'true'
    # success = db.delete_image(image_id, delete_file=delete_file)
    # if not success:
    #     return error_response('Image not found', 404)
    # return success_response(message='Image deleted')

    # Stub response
    return error_response('Image not found', 404)


@app.route('/api/images/<image_id>/thumbnail', methods=['GET'])
def get_thumbnail(image_id):
    """Get a thumbnail for an image.

    Thumbnails are generated on-demand and cached. If a cached thumbnail
    exists at the requested size, it is served directly. Otherwise, a new
    thumbnail is generated from the original image.

    Args:
        image_id: The unique identifier of the image.

    Query Parameters:
        size: Thumbnail size in pixels (longest edge). Defaults to 200.

    Returns:
        JPEG image data, or 404 if image not found.
    """
    size = request.args.get('size', 200, type=int)

    # Clamp size to reasonable bounds
    size = max(50, min(800, size))

    # TODO: Implement with imagedb
    # thumbnail_path = db.get_or_create_thumbnail(image_id, size)
    # if thumbnail_path is None:
    #     return error_response('Image not found', 404)
    # return send_file(thumbnail_path, mimetype='image/jpeg')

    # Stub response
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
    # TODO: Implement with imagedb
    # image = db.get_image(image_id)
    # if image is None:
    #     return error_response('Image not found', 404)
    #
    # path = image['path']
    # if not os.path.exists(path):
    #     return error_response('Image file not found on disk', 404)
    #
    # return send_file(path)

    # Stub response
    abort(404)


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
    # TODO: Implement with imagedb
    # folders = db.get_folders()
    # return jsonify(folders)

    # Stub response
    return jsonify([])


@app.route('/api/folders', methods=['POST'])
def add_folder():
    """Register a new image source folder.

    Adds a folder to the list of monitored directories. This does not
    immediately scan the folder; use /api/scan to trigger scanning.

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

    # TODO: Implement with imagedb
    # folder = db.add_folder(path)
    # if folder is None:
    #     return error_response('Folder already registered')
    # return success_response(folder)

    # Stub response
    return success_response({'path': path, 'count': 0})


@app.route('/api/folders/<path:folder_path>', methods=['DELETE'])
def remove_folder(folder_path):
    """Remove a folder and all its images from the database.

    This removes the folder registration and deletes all image entries
    that originated from this folder. Original image files are not deleted.

    Args:
        folder_path: URL-encoded path of the folder to remove.

    Returns:
        Success response, or 404 if folder not registered.
    """
    # The path comes URL-encoded, Flask decodes it automatically
    # Prepend '/' for absolute paths on Unix (Windows paths start with drive letter)
    if not folder_path.startswith('/') and ':' not in folder_path:
        folder_path = '/' + folder_path

    # TODO: Implement with imagedb
    # success = db.remove_folder(folder_path)
    # if not success:
    #     return error_response('Folder not found', 404)
    # return success_response(message='Folder removed')

    # Stub response
    return success_response(message='Folder removed')


# =============================================================================
# Scan Endpoints
# =============================================================================

# In-memory store for scan job status (would use proper job queue in production)
_scan_jobs = {}


@app.route('/api/scan', methods=['POST'])
def start_scan():
    """Start a database scan operation.

    Scans can target a specific folder or all registered folders.
    The scan runs asynchronously; use GET /api/scan/<job_id> to
    check progress.

    Request Body:
        JSON object with optional fields:
            - folder: Single folder path to scan
            - folders: Array of folder paths to scan
            If neither specified, scans all registered folders.

    Returns:
        JSON object with:
            - jobId: Unique identifier for tracking scan progress
    """
    data = request.get_json() or {}

    # Generate a job ID
    job_id = str(uuid.uuid4())

    # Determine what to scan
    folders_to_scan = []
    if 'folder' in data:
        folders_to_scan = [data['folder']]
    elif 'folders' in data:
        folders_to_scan = data['folders']
    else:
        # TODO: Get all registered folders from db
        # folders_to_scan = [f['path'] for f in db.get_folders()]
        pass

    # TODO: Start async scan job
    # db.start_scan(job_id, folders_to_scan)

    # Store initial job status
    _scan_jobs[job_id] = {
        'status': 'running',
        'progress': 0,
        'message': 'Starting scan...',
        'folders': folders_to_scan
    }

    return jsonify({'jobId': job_id})


@app.route('/api/scan/<job_id>', methods=['GET'])
def get_scan_status(job_id):
    """Get the status of a scan operation.

    Args:
        job_id: The unique identifier returned by POST /api/scan.

    Returns:
        JSON object with:
            - status: 'running', 'complete', or 'error'
            - progress: Percentage complete (0-100)
            - message: Human-readable status message
    """
    # TODO: Get actual status from imagedb
    # status = db.get_scan_status(job_id)
    # if status is None:
    #     return error_response('Scan job not found', 404)
    # return jsonify(status)

    # Check in-memory store
    if job_id not in _scan_jobs:
        return error_response('Scan job not found', 404)

    job = _scan_jobs[job_id]

    # Stub: Simulate progress
    if job['status'] == 'running':
        job['progress'] = min(100, job['progress'] + 10)
        if job['progress'] >= 100:
            job['status'] = 'complete'
            job['message'] = 'Scan complete'
        else:
            job['message'] = f"Scanning... {job['progress']}%"

    return jsonify({
        'status': job['status'],
        'progress': job['progress'],
        'message': job['message']
    })


# =============================================================================
# Duplicates Endpoints
# =============================================================================

@app.route('/api/duplicates', methods=['GET'])
def get_duplicates():
    """Get duplicate image groups at a specified similarity level.

    Duplicate groups are pre-computed during scanning. This endpoint
    returns groups for the requested similarity level.

    Query Parameters:
        level: Similarity level (0-3). Defaults to 0.
            - 0: Identical (same SHA256 checksum)
            - 1: Near-identical (similar perceptual hash)
            - 2: Similar (high OpenCLIP similarity)
            - 3: Related (lower OpenCLIP threshold)

    Returns:
        JSON object with:
            - groups: Array of duplicate groups, each containing:
                - images: Array of image objects in the group
    """
    level = request.args.get('level', 0, type=int)

    # Validate level
    if level < 0 or level > 3:
        return error_response('Level must be between 0 and 3')

    # TODO: Implement with imagedb
    # groups = db.get_duplicate_groups(level)
    # return jsonify({'groups': groups})

    # Stub response
    return jsonify({'groups': []})


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
    # TODO: Implement with imagedb
    # stats = db.get_stats()
    # return jsonify(stats)

    # Stub response
    return jsonify({
        'totalImages': 0,
        'totalFolders': 0
    })


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
    return error_response('Internal server error', 500)


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == '__main__':
    # Ensure thumbnail cache directory exists
    os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)

    # Run development server
    # In production, use a proper WSGI server like gunicorn
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=True
    )
