/**
 * @fileoverview Database management screen module for the Imaginary application.
 *
 * This module handles the Database screen where users manage image source
 * folders and trigger database scans. It registers with the core App module
 * and is shown by default when the database is empty.
 *
 * RESPONSIBILITIES:
 *
 * Folder Management:
 *   - Displays list of currently registered image source folders
 *   - Add folder button opens native folder picker dialog
 *   - Each folder entry shows path and image count from that folder
 *   - Remove button on each folder (with confirmation dialog)
 *   - Removing a folder removes all its images from the database
 *
 * Database Scanning:
 *   - "Rescan All Folders" button triggers a full database rescan
 *   - Adding a new folder automatically triggers a scan of that folder
 *   - Scans are performed asynchronously on the backend
 *   - Detects new, modified, and deleted images
 *   - Modified images are detected by timestamp or file size changes
 *
 * Progress Reporting:
 *   - Shows progress bar during scan operations
 *   - Displays current status text (e.g., "Scanning folder X..." or "Processing image Y...")
 *   - Progress bar shows percentage completion
 *   - Polls backend for progress updates during scan
 *   - Hides progress bar when scan completes
 *
 * Database Status:
 *   - Displays total image count in database
 *   - Updates count after scan completion or folder removal
 *   - Shows last scan timestamp
 *
 * Startup Behavior:
 *   - If database is empty on app start, this screen is shown automatically
 *   - Prompts user to add at least one folder to begin
 *
 * Error Handling:
 *   - Displays error messages if folder cannot be added (e.g., doesn't exist)
 *   - Shows warning if scan encounters unreadable files
 *   - Handles backend connection errors gracefully
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Fetches current folder list and database stats from backend
 *   - onLeave(): Cancels any pending progress polling
 *
 * @module database
 * @requires core
 */
