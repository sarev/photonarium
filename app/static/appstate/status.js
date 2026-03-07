/**
 * AppState Status Domain - Backend Processing Status
 * ====================================================
 *
 * Tracks backend processing status via polling:
 * - Indexing queue size
 * - Embedding queue size
 * - Face detection queue size
 * - Face reassessment completion
 *
 * Provides polling control for screens that need live status.
 *
 * @fileoverview Backend status polling domain.
 */

'use strict';

AppState.status = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Current status from backend.
     * @type {Object|null}
     * @property {string} status - 'updating' or 'up_to_date'
     * @property {number} indexing_queue - Images waiting for indexing
     * @property {number} embedding_queue - Images waiting for embeddings
     * @property {number} face_queue - Images waiting for face detection
     * @property {boolean} face_detection_enabled - Whether face detection is on
     * @property {Object} [face_reassessment] - Reassessment status if running
     */
    let _status = null;

    /** @type {Object|null} Previous status for change detection */
    let _prevStatus = null;

    /** @type {number|null} Polling interval timer */
    let _pollTimer = null;

    /** @type {number} Current polling interval in ms */
    let _currentInterval = 0;

    /** @type {number} Fast interval when backend is processing (ms) */
    const POLL_FAST_MS = 1000;

    /** @type {number} Slow interval when backend is idle (ms) */
    const POLL_SLOW_MS = 5000;

    /** @type {boolean} Whether a load is in progress */
    let _loading = false;

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'status',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to status changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Load status from backend.
         * @returns {Promise<Object>} Status object
         */
        async load() {
            if (_loading) return _status;
            _loading = true;

            try {
                _prevStatus = _status;
                const response = await App.apiGet('/status');
                _status = response.data;

                // Track connectivity for offline detection
                App.markOnline();

                // Check for face reassessment completion
                const wasCompleted = _prevStatus?.face_reassessment?.completed;
                const isCompleted = _status?.face_reassessment?.completed;

                if (isCompleted && !wasCompleted) {
                    // Acknowledge the reassessment
                    App.apiPost('/faces/reassess-ack').catch(e => console.warn('Reassess ACK failed:', e));

                    // Trigger faces reload if loaded
                    setTimeout(() => {
                        if (AppState.faces?.isLoaded()) {
                            AppState.faces.load(true);
                        }
                    }, 0);
                }

                // Adapt polling speed: fast when processing, slow when idle
                if (_pollTimer) {
                    const desired = _status.status === 'updating' ? POLL_FAST_MS : POLL_SLOW_MS;
                    if (desired !== _currentInterval) {
                        clearInterval(_pollTimer);
                        _currentInterval = desired;
                        _pollTimer = setInterval(() => this.load(), desired);
                    }
                }

                broadcast({ type: 'changed' });
                return _status;

            } catch (err) {
                console.error('[AppState.status.load] Error:', err);
                throw err;
            } finally {
                _loading = false;
            }
        },

        /**
         * Get current status.
         * @returns {Object|null}
         */
        get() {
            return _status;
        },

        /**
         * Check if face detection is enabled.
         * @returns {boolean}
         */
        isFaceDetectionEnabled() {
            return _status?.face_detection_enabled !== false;
        },

        /**
         * Check if the trash directory is configured and valid.
         * @returns {boolean}
         */
        isTrashEnabled() {
            return _status?.trash_enabled !== false;
        },

        /**
         * Check if backend is currently processing.
         * @returns {boolean}
         */
        isUpdating() {
            return _status?.status === 'updating';
        },

        /**
         * Get queue counts.
         * @returns {{indexing: number, embedding: number, face: number, nima: number, video: number, import: number}}
         */
        getQueues() {
            return {
                indexing: _status?.indexing_queue || 0,
                embedding: _status?.embedding_queue || 0,
                face: _status?.face_queue || 0,
                nima: _status?.nima_queue || 0,
                video: _status?.video_queue || 0,
                import: _status?.import_queue || 0,
            };
        },

        // --- Polling Control ---

        /**
         * Start polling for status updates.
         * Safe to call multiple times - only starts one timer.
         * Starts at the fast interval; load() automatically switches to
         * the slow interval once the backend reports idle.
         */
        startPolling() {
            if (_pollTimer) return;

            _currentInterval = POLL_FAST_MS;
            console.log('[AppState.status.startPolling] interval:', _currentInterval);
            this.load(); // Initial load (may adjust interval via adaptive logic)
            _pollTimer = setInterval(() => this.load(), _currentInterval);
        },

        /**
         * Stop polling for status updates.
         */
        stopPolling() {
            if (_pollTimer) {
                console.log('[AppState.status.stopPolling]');
                clearInterval(_pollTimer);
                _pollTimer = null;
                _currentInterval = 0;
            }
        },

        /**
         * Check if currently polling.
         * @returns {boolean}
         */
        isPolling() {
            return _pollTimer !== null;
        },
    };
})();
