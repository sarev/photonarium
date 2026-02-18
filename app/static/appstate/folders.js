/**
 * AppState Folders Domain - Folder Management
 * =============================================
 *
 * Manages registered folders for image scanning:
 * - List registered folders
 * - Add/remove folders
 * - Trigger rescans
 * - Track folder statistics
 *
 * Persisted to backend.
 *
 * @fileoverview Folder management domain.
 */

'use strict';

AppState.folders = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * List of registered folders.
     * @type {Array<{path: string, image_count: number}>}
     */
    let _folders = [];

    /**
     * Database statistics.
     * @type {Object|null}
     * @property {number} totalImages - Total image count
     * @property {number} totalFolders - Total folder count
     */
    let _stats = null;

    /**
     * Processing status (from AppState.status).
     * Used to detect databaseChanged events.
     * @type {Object|null}
     */
    let _status = null;

    /** @type {boolean} Whether folders are loading */
    let _loading = false;

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'folders',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to folder changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Subscribe to folder errors.
         * @param {Function} callback - Called with error event
         * @returns {Function} Unsubscribe function
         */
        onError: subscribeError,

        /**
         * Load folders from backend.
         * @returns {Promise<void>}
         */
        async load() {
            if (_loading) return;
            _loading = true;

            try {
                const response = await App.apiGet('/folders');
                _folders = response.data || [];
                broadcast({ type: 'changed' });

            } catch (err) {
                console.error('[AppState.folders.load] Error:', err);
                broadcastError(err.message || 'Failed to load folders');
            } finally {
                _loading = false;
            }
        },

        /**
         * Get all registered folders.
         * @returns {Array<{path: string, image_count: number}>}
         */
        getAll() {
            return _folders;
        },

        /**
         * Add a folder to the database.
         * @param {string} path - Folder path
         * @returns {Promise<void>}
         */
        async add(path) {
            if (!App.requireOnline()) return;
            console.log('[AppState.folders.add]', path);

            try {
                const response = await App.apiPost('/folders', { path });
                if (response && response.success === false) {
                    throw new Error(response.error || 'Failed to add folder');
                }
                await this.load();

            } catch (err) {
                console.error('[AppState.folders.add] Error:', err);
                broadcastError(err.message || 'Failed to add folder');
                throw err;
            }
        },

        /**
         * Remove a folder from the database.
         * @param {string} path - Folder path
         * @returns {Promise<void>}
         */
        async remove(path) {
            if (!App.requireOnline()) return;
            console.log('[AppState.folders.remove]', path);

            try {
                await App.apiDelete(`/folders/${encodeURIComponent(path)}`);
                _folders = _folders.filter(f => f.path !== path);
                broadcast({ type: 'changed' });

            } catch (err) {
                console.error('[AppState.folders.remove] Error:', err);
                broadcastError(err.message || 'Failed to remove folder');
                throw err;
            }
        },

        /**
         * Trigger a rescan of all folders.
         * @returns {Promise<void>}
         */
        async rescan() {
            console.log('[AppState.folders.rescan]');

            try {
                const response = await App.apiPost('/rescan');
                if (response && response.success === false) {
                    throw new Error(response.error || 'Failed to start rescan');
                }
                broadcast({ type: 'rescanStarted' });

            } catch (err) {
                console.error('[AppState.folders.rescan] Error:', err);
                broadcastError(err.message || 'Failed to start rescan');
                throw err;
            }
        },

        // --- Statistics ---

        /**
         * Load database statistics.
         * @returns {Promise<Object>} Stats object
         */
        async loadStats() {
            try {
                const response = await App.apiGet('/stats');
                _stats = response.data;
                broadcast({ type: 'changed', property: 'stats' });
                return _stats;

            } catch (err) {
                console.error('[AppState.folders.loadStats] Error:', err);
                broadcastError(err.message || 'Failed to load stats');
                throw err;
            }
        },

        /**
         * Get database statistics.
         * @returns {Object|null} Stats object
         */
        getStats() {
            return _stats;
        },

        // --- Status Tracking ---

        /**
         * Set status from AppState.status.
         * Detects when processing completes and broadcasts databaseChanged.
         * @param {Object} status - Status object
         */
        setStatus(status) {
            const wasUpdating = _status?.status === 'updating';
            const nowUpToDate = status?.status === 'up_to_date';

            _status = status;
            broadcast({ type: 'changed', property: 'status' });

            // Emit databaseChanged when processing completes
            if (wasUpdating && nowUpToDate) {
                broadcast({ type: 'databaseChanged' });
            }
        },

        /**
         * Get current status.
         * @returns {Object|null}
         */
        getStatus() {
            return _status;
        },

        /**
         * Check if database is updating.
         * @returns {boolean}
         */
        isUpdating() {
            return _status?.status === 'updating';
        },
    };
})();
