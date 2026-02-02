/**
 * AppState Events Domain - Backend Event Polling
 * ===============================================
 *
 * Polls backend for events and dispatches them to relevant domains.
 * This enables the frontend to react to backend-initiated changes:
 * - New images added during folder scanning
 * - Face detection/reassessment completion
 * - Folder changes
 * - Processing completion
 *
 * Event types:
 * - `faces_reassessed` - Async face matching completed
 * - `folder_added` / `folder_removed` - Folder registration changes
 * - `processing_complete` - Full processing cycle finished
 * - `image_ingested` - New image added to database
 * - `error` - Backend error occurred
 *
 * @fileoverview Backend event polling and dispatch.
 */

'use strict';

AppState.events = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {number|null} Polling interval timer */
    let _pollTimer = null;

    /** @type {boolean} Whether a poll is in progress */
    let _polling = false;

    /** @type {number} Poll interval in ms */
    let _intervalMs = 2000;

    // =========================================================================
    // EVENT HANDLERS
    // =========================================================================

    /**
     * Process a single event from the backend.
     * @param {Object} event - Event object with type and data
     */
    function processEvent(event) {
        const { type, data } = event;
        console.log('[AppState.events.processEvent]', type, data);

        switch (type) {
            case 'faces_reassessed':
                // Backend completed async face matching
                // data: { person_id, matched_count, matched_face_ids }
                handleFacesReassessed(data);
                break;

            case 'processing_complete':
                // Backend finished processing all queues
                handleProcessingComplete(data);
                break;

            case 'folder_added':
                // New folder registered
                // data: { folder }
                handleFolderAdded(data);
                break;

            case 'folder_removed':
                // Folder unregistered
                // data: { folder }
                handleFolderRemoved(data);
                break;

            case 'image_ingested':
                // New image added to database
                // data: { id, path }
                handleImageIngested(data);
                break;

            case 'error':
                // Backend error
                // data: { message }
                handleError(data);
                break;

            default:
                console.warn('[AppState.events.processEvent] Unknown event type:', type);
        }

        // Broadcast raw event for any listeners that want all events
        broadcast({ type: 'event', eventType: type, data });
    }

    /**
     * Handle faces_reassessed event.
     * Auto-matched faces should be added to the person.
     *
     * Uses incremental cache update (no full reload) for responsiveness.
     * The autoAssign() call updates the cache directly - backend already persisted.
     */
    function handleFacesReassessed(data) {
        const { person_id, matched_face_ids } = data || {};

        if (matched_face_ids?.length && person_id) {
            console.log('[AppState.events] Faces reassessed:',
                matched_face_ids.length, 'faces matched to', person_id);

            // Use autoAssign (no lock, no persist - backend already stored)
            // This updates the cache incrementally - no full reload needed
            if (AppState.faces?.autoAssign) {
                AppState.faces.autoAssign(matched_face_ids, person_id);
            }
        }
        // Note: No full reload here - autoAssign already updated the cache
    }

    /**
     * Handle processing_complete event.
     * Backend finished processing all queues (indexing, embedding, face detection).
     *
     * Unlike the aggressive approach of reloading all data, we only:
     * 1. Update status (shows "up to date" instead of "updating")
     * 2. Reload folder stats (total counts changed)
     *
     * Full data reloads happen when user navigates to screens, avoiding
     * disruptive background reloads while user is actively working.
     */
    function handleProcessingComplete(data) {
        console.log('[AppState.events] Processing complete');

        // Update status (will show "up to date")
        if (AppState.status?.load) {
            AppState.status.load();
        }

        // Reload folder stats (total image count changed)
        if (AppState.folders?.loadStats) {
            AppState.folders.loadStats();
        }

        // Notify folders domain that processing is complete
        // This triggers databaseChanged broadcast for screens that care
        if (AppState.folders?.setStatus) {
            AppState.folders.setStatus({ status: 'up_to_date' });
        }

        broadcast({ type: 'processingComplete' });
    }

    /**
     * Handle folder_added event.
     */
    function handleFolderAdded(data) {
        console.log('[AppState.events] Folder added:', data?.folder);

        // Reload folders list
        if (AppState.folders?.load) {
            AppState.folders.load();
        }

        broadcast({ type: 'folderAdded', folder: data?.folder });
    }

    /**
     * Handle folder_removed event.
     */
    function handleFolderRemoved(data) {
        console.log('[AppState.events] Folder removed:', data?.folder);

        // Reload folders list
        if (AppState.folders?.load) {
            AppState.folders.load();
        }

        // Images from this folder were removed - invalidate cache
        // so next access triggers a reload (avoids disruptive background reload)
        if (AppState.images?.invalidate) {
            AppState.images.invalidate();
        }

        broadcast({ type: 'folderRemoved', folder: data?.folder });
    }

    /**
     * Handle image_ingested event.
     * Individual image added - could batch these for efficiency.
     */
    function handleImageIngested(data) {
        // Don't log every image - could be thousands during scan
        // Just mark images as needing refresh when processing completes
    }

    /**
     * Handle error event.
     */
    function handleError(data) {
        console.error('[AppState.events] Backend error:', data?.message);
        broadcast({ type: 'error', message: data?.message });
    }

    // =========================================================================
    // POLLING
    // =========================================================================

    /**
     * Poll for events from backend.
     */
    async function poll() {
        if (_polling) return;
        _polling = true;

        try {
            const response = await App.apiGet('/events');
            const events = response?.data?.events || [];

            if (events.length > 0) {
                console.log('[AppState.events.poll] Got', events.length, 'events');
                for (const event of events) {
                    processEvent(event);
                }
            }
        } catch (err) {
            console.error('[AppState.events.poll] Error:', err);
        } finally {
            _polling = false;
        }
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'events',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to events.
         * @param {Function} callback - Called with event
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Start polling for events.
         * Safe to call multiple times - only starts one timer.
         * Skips polling in mock mode.
         * @param {number} [intervalMs=2000] - Polling interval in ms
         */
        startPolling(intervalMs = 2000) {
            // Skip in mock mode (no backend to poll)
            if (typeof App !== 'undefined' && App.mockMode) {
                console.log('[AppState.events.startPolling] Skipping in mock mode');
                return;
            }

            if (_pollTimer) return;

            _intervalMs = intervalMs;
            console.log('[AppState.events.startPolling] interval:', intervalMs);
            poll(); // Initial poll
            _pollTimer = setInterval(poll, intervalMs);
        },

        /**
         * Stop polling for events.
         */
        stopPolling() {
            if (_pollTimer) {
                console.log('[AppState.events.stopPolling]');
                clearInterval(_pollTimer);
                _pollTimer = null;
            }
        },

        /**
         * Check if currently polling.
         * @returns {boolean}
         */
        isPolling() {
            return _pollTimer !== null;
        },

        /**
         * Manually trigger a poll (e.g., after user action).
         * @returns {Promise<void>}
         */
        poll
    };
})();
