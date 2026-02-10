/**
 * AppState Duplicates Domain - Duplicate Detection & Custom Groups
 * =================================================================
 *
 * Manages duplicate image groups at different similarity levels:
 * - Level 0: Identical (same checksum)
 * - Level 1: Near-identical (perceptual hash)
 * - Level 2: Similar (high embedding similarity)
 * - Level 3: Related (lower embedding similarity)
 * - Level 4: Custom groups (user-curated albums)
 *
 * Levels 0-3 are auto-computed; level 4 is user-managed with overlap
 * allowed (same image in multiple groups) and groups that persist
 * even when empty.
 *
 * Handles async computation with polling for auto levels.
 *
 * @fileoverview Duplicate detection and custom groups domain.
 */

'use strict';

AppState.duplicates = (function() {
    const { createSubscriberSystem, markDirty, transaction, queueTransaction } = AppState;
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
         *
         * For auto levels (0-3): dissolves groups with <= 1 image.
         * For custom groups (level 4): removes image but keeps the group
         * (custom groups persist even when empty).
         *
         * @param {string} imageId - Image ID to remove
         */
        removeImage(imageId) {
            let changed = false;

            for (const level of Object.keys(_groupCache)) {
                const groups = _groupCache[level];
                if (!groups) continue;
                const isCustom = parseInt(level, 10) === 4;

                for (let i = groups.length - 1; i >= 0; i--) {
                    const group = groups[i];
                    const idx = group.image_ids.indexOf(imageId);

                    if (idx !== -1) {
                        group.image_ids.splice(idx, 1);
                        group.count = group.image_ids.length;
                        changed = true;

                        // Auto levels: remove group if only 1 image left
                        // Custom groups: keep group even when empty
                        if (!isCustom && group.image_ids.length <= 1) {
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
    // CUSTOM GROUP HELPERS
    // =========================================================================

    /**
     * Create a backup of level-4 groups for rollback.
     * @returns {Array} Deep copy of level-4 group cache
     * @private
     */
    function _backupLevel4() {
        const groups = _groupCache[4];
        if (!groups) return [];
        return groups.map(g => ({
            ...g,
            image_ids: [...g.image_ids]
        }));
    }

    /**
     * Restore level-4 groups from a backup.
     * @param {Array} backup - Previously saved backup
     * @private
     */
    function _restoreLevel4(backup) {
        transaction(() => {
            _groupCache[4] = backup;
            markDirty(domainRef);
        });
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
         * Level 4 (custom groups) never polls — status is always 'done'.
         * @param {number} level - Similarity level (0-4)
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

                // Start polling if computation in progress (not for custom groups)
                if (level !== 4) {
                    _startPollingIfNeeded(level, status);
                }

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
         * Get custom groups (level 4).
         * @returns {Array} Custom group objects with name, count, image_ids, best_image
         */
        getCustomGroups() {
            return _groupCache[4] || [];
        },

        /**
         * Get level-4 groups that contain ALL of the given image IDs.
         * Used by the Group Picker to show which groups the selected images share.
         * @param {string[]} imageIds - Image IDs to check
         * @returns {Array} Custom groups containing all the given images
         */
        getGroupsForImages(imageIds) {
            const groups = _groupCache[4] || [];
            if (!imageIds || imageIds.length === 0) return [];
            return groups.filter(g =>
                imageIds.every(id => g.image_ids.includes(id))
            );
        },

        /**
         * Get computation status for a level.
         * @param {number} level - Similarity level
         * @returns {{status: string, progress: number, total: number}|null}
         */
        getStatus(level) {
            // Custom groups have no computation phase
            if (level === 4) return { status: 'done', progress: 0, total: 0 };
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
         * @param {number} level - Similarity level (0-4)
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

        // --- Actions (auto levels) ---

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
        },

        // --- Actions (custom groups, level 4) ---

        /**
         * Create a new custom group (album).
         * Uses optimistic update: cache is updated synchronously, then
         * API call is made. On error, cache is rolled back.
         *
         * @param {string} name - Group display name
         * @param {string[]} [imageIds=[]] - Initial image IDs
         * @returns {Promise<string>} The new group hash (UUID)
         */
        async createGroup(name, imageIds = []) {
            const groupHash = crypto.randomUUID();
            const backup = _backupLevel4();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                if (!_groupCache[4]) _groupCache[4] = [];
                _groupCache[4].push({
                    group_hash: groupHash,
                    name: name,
                    count: imageIds.length,
                    image_ids: [...imageIds],
                    best_image: null,  // Will be resolved on next load
                });
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPost('/groups', {
                    group_hash: groupHash,
                    name: name,
                    image_ids: imageIds,
                });
                // Reload to get best_image from backend
                await this.loadLevel(4, true);
            } catch (err) {
                _restoreLevel4(backup);
                broadcastError(err.message || 'Failed to create group');
                throw err;
            }

            return groupHash;
        },

        /**
         * Rename a custom group.
         *
         * @param {string} groupHash - The group identifier
         * @param {string} name - New display name
         * @returns {Promise<void>}
         */
        async renameGroup(groupHash, name) {
            const backup = _backupLevel4();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[4] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) group.name = name;
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPatch(`/groups/${groupHash}`, { name });
            } catch (err) {
                _restoreLevel4(backup);
                broadcastError(err.message || 'Failed to rename group');
                throw err;
            }
        },

        /**
         * Delete a custom group.
         *
         * @param {string} groupHash - The group identifier
         * @returns {Promise<void>}
         */
        async deleteGroup(groupHash) {
            const backup = _backupLevel4();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                if (_groupCache[4]) {
                    _groupCache[4] = _groupCache[4].filter(g => g.group_hash !== groupHash);
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiDelete(`/groups/${groupHash}`);
            } catch (err) {
                _restoreLevel4(backup);
                broadcastError(err.message || 'Failed to delete group');
                throw err;
            }
        },

        /**
         * Add images to an existing custom group.
         *
         * @param {string} groupHash - The group identifier
         * @param {string[]} imageIds - Image IDs to add
         * @returns {Promise<void>}
         */
        async addImages(groupHash, imageIds) {
            if (!imageIds || imageIds.length === 0) return;
            const backup = _backupLevel4();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[4] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) {
                    // Only add images not already in the group
                    const existing = new Set(group.image_ids);
                    for (const id of imageIds) {
                        if (!existing.has(id)) {
                            group.image_ids.push(id);
                        }
                    }
                    group.count = group.image_ids.length;
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPost(`/groups/${groupHash}/images`, { image_ids: imageIds });
                // Reload to update best_image
                await this.loadLevel(4, true);
            } catch (err) {
                _restoreLevel4(backup);
                broadcastError(err.message || 'Failed to add images to group');
                throw err;
            }
        },

        /**
         * Remove images from a custom group (group persists even if empty).
         *
         * @param {string} groupHash - The group identifier
         * @param {string[]} imageIds - Image IDs to remove
         * @returns {Promise<void>}
         */
        async removeImages(groupHash, imageIds) {
            if (!imageIds || imageIds.length === 0) return;
            const backup = _backupLevel4();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[4] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) {
                    const removeSet = new Set(imageIds);
                    group.image_ids = group.image_ids.filter(id => !removeSet.has(id));
                    group.count = group.image_ids.length;
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPost(`/groups/${groupHash}/images/remove`, { image_ids: imageIds });
            } catch (err) {
                _restoreLevel4(backup);
                broadcastError(err.message || 'Failed to remove images from group');
                throw err;
            }
        }
    };
})();
