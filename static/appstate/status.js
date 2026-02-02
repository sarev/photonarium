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

                // Check for face reassessment completion
                const wasCompleted = _prevStatus?.face_reassessment?.completed;
                const isCompleted = _status?.face_reassessment?.completed;

                if (isCompleted && !wasCompleted) {
                    console.log('[AppState.status] Face reassessment completed, acknowledging...');

                    // Acknowledge the reassessment
                    App.apiPost('/faces/reassess-ack').catch(err => {
                        console.warn('[AppState.status] Failed to ack reassessment:', err);
                    });

                    // Trigger faces reload if loaded
                    setTimeout(() => {
                        if (AppState.faces?.isLoaded()) {
                            console.log('[AppState.status] Reloading faces after reassessment');
                            AppState.faces.load(true);
                        }
                    }, 0);
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
         * Check if backend is currently processing.
         * @returns {boolean}
         */
        isUpdating() {
            return _status?.status === 'updating';
        },

        /**
         * Get queue counts.
         * @returns {{indexing: number, embedding: number, face: number}}
         */
        getQueues() {
            return {
                indexing: _status?.indexing_queue || 0,
                embedding: _status?.embedding_queue || 0,
                face: _status?.face_queue || 0
            };
        },

        // --- Polling Control ---

        /**
         * Start polling for status updates.
         * Safe to call multiple times - only starts one timer.
         * @param {number} [intervalMs=1000] - Polling interval in ms
         */
        startPolling(intervalMs = 1000) {
            if (_pollTimer) return;

            console.log('[AppState.status.startPolling] interval:', intervalMs);
            this.load(); // Initial load
            _pollTimer = setInterval(() => this.load(), intervalMs);
        },

        /**
         * Stop polling for status updates.
         */
        stopPolling() {
            if (_pollTimer) {
                console.log('[AppState.status.stopPolling]');
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
        }
    };
})();
