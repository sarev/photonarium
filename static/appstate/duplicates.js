/**
 * AppState Duplicates Domain - Duplicate Detection
 * ==================================================
 *
 * Manages duplicate image groups at different similarity levels:
 * - Level 0: Identical (same checksum)
 * - Level 1: Near-identical (perceptual hash)
 * - Level 2: Similar (high embedding similarity)
 * - Level 3: Related (lower embedding similarity)
 *
 * Handles async computation with polling.
 *
 * @fileoverview Duplicate detection domain.
 */

'use strict';

AppState.duplicates = (function() {
    const { createSubscriberSystem, markDirty } = AppState;
    const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Duplicate groups cache per level.
     * @type {Object.<number, Array>}
     */
    let _groupCache = {};

    /**
     * Computation status per level.
     * @type {Object.<number, {status: string, progress: number, total: number}>}
     */
    let _statusCache = {};

    /**
     * Cache timestamps per level.
     * @type {Object.<number, number>}
     */
    let _epochCache = {};

    /** @type {number} Currently selected similarity level */
    let _currentLevel = 2;

    /** @type {boolean} Whether computation is in progress */
    let _computing = false;

    /** @type {number|null} Polling timer ID */
    let _pollTimer = null;

    /** @type {number|null} Level being polled */
    let _pollLevel = null;

    /** Domain reference for transaction system */
    const domainRef = { _name: 'duplicates', _notify: notify };

    // =========================================================================
    // INTERNAL API
    // =========================================================================

    /**
     * Internal API for cross-domain operations.
     * Used by images domain for delete cascade.
     */
    const _internal = {
        /**
         * Remove an image from all cached duplicate groups.
         * Called when image is deleted.
         * @param {string} imageId - Image ID to remove
         */
        removeImage(imageId) {
            let changed = false;

            for (const level of Object.keys(_groupCache)) {
                const groups = _groupCache[level];
                if (!groups) continue;

                for (let i = groups.length - 1; i >= 0; i--) {
                    const group = groups[i];
                    const idx = group.image_ids.indexOf(imageId);

                    if (idx !== -1) {
                        group.image_ids.splice(idx, 1);
                        changed = true;

                        // Remove group if only 1 image left
                        if (group.image_ids.length <= 1) {
                            groups.splice(i, 1);
                        }
                    }
                }
            }

            if (changed) {
                markDirty(domainRef);
            }
        }
    };

    // =========================================================================
    // POLLING
    // =========================================================================

    /**
     * Start polling if computation is in progress.
     * @param {number} level - Level to poll
     * @param {string} status - Current status
     * @private
     */
    function _startPollingIfNeeded(level, status) {
        if (status !== 'computing' && status !== 'pending') return;
        if (_pollTimer && _pollLevel === level) return;

        _stopPolling();
        _pollLevel = level;
        _pollTimer = setInterval(async () => {
            try {
                const response = await App.apiGet(`/duplicates?level=${level}`);
                const data = response.data;
                const newStatus = data.status;

                _statusCache[level] = {
                    status: newStatus,
                    progress: data.progress,
                    total: data.total
                };

                if (newStatus !== 'computing' && newStatus !== 'pending') {
                    _stopPolling();
                    _computing = false;
                    _groupCache[level] = data.groups || [];
                    _epochCache[level] = Date.now();
                    broadcast({ type: 'changed', level });
                }
            } catch (err) {
                console.error('[AppState.duplicates] Poll error:', err);
            }
        }, 2000);
    }

    /**
     * Stop polling.
     * @private
     */
    function _stopPolling() {
        if (_pollTimer) {
            clearInterval(_pollTimer);
            _pollTimer = null;
            _pollLevel = null;
        }
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'duplicates',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /** Internal API for cross-domain operations */
        _internal,

        /**
         * Subscribe to duplicates changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Subscribe to errors.
         * @param {Function} callback - Called with error event
         * @returns {Function} Unsubscribe function
         */
        onError: subscribeError,

        /**
         * Load duplicate groups for a similarity level.
         * Automatically starts polling if computation is in progress.
         * @param {number} level - Similarity level (0-3)
         * @param {boolean} [force=false] - Force reload even if cached
         * @returns {Promise<Array>} Duplicate groups
         */
        async loadLevel(level, force = false) {
            // Return cached if available
            if (!force && _groupCache[level] !== undefined) {
                return _groupCache[level];
            }

            try {
                const response = await App.apiGet(`/duplicates?level=${level}`);
                const data = response.data;

                _groupCache[level] = data.groups || [];
                _statusCache[level] = {
                    status: data.status,
                    progress: data.progress,
                    total: data.total
                };
                _epochCache[level] = Date.now();

                const status = data.status;
                _computing = status === 'computing' || status === 'pending';

                // Start polling if computation in progress
                _startPollingIfNeeded(level, status);

                broadcast({ type: 'changed', level });
                return _groupCache[level];

            } catch (err) {
                console.error('[AppState.duplicates.loadLevel] Error:', err);
                broadcastError(err.message || 'Failed to load duplicates');
                throw err;
            }
        },

        /**
         * Reload duplicates for current or specified level.
         * @param {number} [level] - Level to reload, defaults to current
         * @returns {Promise<Array>}
         */
        reload(level) {
            return this.loadLevel(level ?? _currentLevel, true);
        },

        // --- Accessors ---

        /**
         * Get duplicate groups for a level.
         * @param {number} level - Similarity level
         * @returns {Array} Duplicate groups
         */
        getGroups(level) {
            return _groupCache[level] || [];
        },

        /**
         * Get computation status for a level.
         * @param {number} level - Similarity level
         * @returns {{status: string, progress: number, total: number}|null}
         */
        getStatus(level) {
            return _statusCache[level] || null;
        },

        /**
         * Get cache epoch for a level.
         * @param {number} level - Similarity level
         * @returns {number} Timestamp or 0
         */
        getEpoch(level) {
            return _epochCache[level] || 0;
        },

        /**
         * Get currently selected level.
         * @returns {number}
         */
        getCurrentLevel() {
            return _currentLevel;
        },

        /**
         * Set currently selected level.
         * @param {number} level - Similarity level (0-3)
         */
        setCurrentLevel(level) {
            _currentLevel = level;
        },

        /**
         * Check if computation is in progress.
         * @returns {boolean}
         */
        isComputing() {
            return _computing;
        },

        // --- Actions ---

        /**
         * Sort images by semantic similarity to a query.
         * Used for "keep best" selection in duplicate groups.
         * @param {string} query - Semantic query
         * @param {string[]} imageIds - Image IDs to score
         * @returns {Promise<Array<{image_id: string, score: number}>>}
         */
        async sortSemantic(query, imageIds) {
            const response = await App.apiPost('/duplicates/sort-semantic', {
                query,
                image_ids: imageIds
            });
            return response.data?.scores || [];
        },

        /**
         * Stop polling (for cleanup when leaving screen).
         */
        stopPolling() {
            _stopPolling();
        },

        /**
         * Invalidate cache for a level or all levels.
         * @param {number} [level] - Specific level, or all if undefined
         */
        invalidate(level) {
            if (level !== undefined) {
                delete _groupCache[level];
                delete _epochCache[level];
            } else {
                _groupCache = {};
                _epochCache = {};
            }
        }
    };
})();
