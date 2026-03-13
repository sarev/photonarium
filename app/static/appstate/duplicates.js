/**
 * AppState Duplicates Domain - Duplicate Detection & Custom Groups
 * =================================================================
 *
 * Manages duplicate image groups at different similarity levels:
 * - Level 0: Identical (same checksum)
 * - Level 1: Near-identical (perceptual hash)
 * - Level 2: Similar (high embedding similarity)
 * - Level 3: Related (lower embedding similarity)
 * - Level 4: Directory groups (auto-generated from folder structure)
 * - Level 5: Custom groups (user-curated albums)
 *
 * Levels 0-3 are auto-computed; levels 4-5 are named groups with overlap
 * allowed (same image in multiple groups) and groups that persist
 * even when empty.
 *
 * Handles async computation with polling for auto levels.
 *
 * @fileoverview Duplicate detection and custom groups domain.
 */

'use strict';

AppState.duplicates = (function() {
    // eslint-disable-next-line no-unused-vars -- queueTransaction reserved for future async mutations
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
    // HELPERS
    // =========================================================================

    /**
     * Wait for the background trash queue to drain completely.
     *
     * Polls ``/api/status`` at 1s intervals and updates the loading overlay
     * with live "Moving to trash: done / total…" progress.  Resolves when
     * ``trash_progress`` disappears from the status (queue empty).
     *
     * @returns {Promise<void>}
     */
    function _waitForTrashQueueDrain() {
        return new Promise((resolve) => {
            const POLL_MS = 1000;
            const TIMEOUT_MS = 10 * 60 * 1000; // 10 minutes safety limit
            const startedAt = Date.now();

            const poll = async () => {
                try {
                    const status = await AppState.status.load();
                    const progress = status?.trash_progress;

                    if (progress) {
                        const done = (progress.done || 0).toLocaleString();
                        const total = (progress.total || 0).toLocaleString();
                        AppState.loading.setMessage(`Moving to trash: ${done} / ${total}\u2026`);
                    }

                    // Queue drained — progress cleared by backend
                    if (!progress && (status?.trash_queue || 0) === 0) {
                        resolve();
                        return;
                    }

                    // Safety timeout
                    if (Date.now() - startedAt > TIMEOUT_MS) {
                        resolve(); // Don't fail — files will finish in background
                        return;
                    }

                    setTimeout(poll, POLL_MS);
                } catch (err) {
                    // Network blip — retry silently
                    console.warn('[_waitForTrashQueueDrain] poll error:', err);
                    setTimeout(poll, POLL_MS);
                }
            };

            // First poll after a short delay (give the worker time to start)
            setTimeout(poll, 500);
        });
    }

    // =========================================================================
    // SMART GROUP PREVIEW HELPERS
    // =========================================================================

    /**
     * Set of smart group hashes whose preview image was removed and needs
     * async re-evaluation.
     * @type {Set<string>}
     */
    const _stalePreviewHashes = new Set();

    /**
     * Debounced wrapper for _refreshStalePreviews to batch multiple
     * image removals into a single evaluation pass.
     */
    const _refreshStalePreviewsDebounced = App.debounce(() => _refreshStalePreviews(), 500);

    /**
     * Re-evaluates previews for smart groups whose thumbnail was removed.
     * @private
     */
    async function _refreshStalePreviews() {
        const hashes = [..._stalePreviewHashes];
        _stalePreviewHashes.clear();

        const groups = _groupCache[5] || [];
        for (const hash of hashes) {
            const group = groups.find(g => g.group_hash === hash);
            if (!group?.filter_json) continue;

            const imageId = await _evaluatePreview(group.filter_json);
            if (imageId) {
                const img = AppState.images.getById(imageId);
                transaction(() => {
                    group.best_image = img ? { id: img.id, basename: img.basename } : null;
                    markDirty(domainRef);
                });
            }
            // Persist to backend (fire-and-forget)
            App.apiPost(`/groups/${hash}/preview`, { image_id: imageId }).catch(e => console.warn('Preview persist failed:', e));
        }
    }

    /**
     * Evaluate a smart group's filter criteria and return matching images.
     *
     * Applies date, rating, people, text (semantic search), and metadata
     * filters against all non-deleted images.
     *
     * @param {Object|string} filterJson - Filter criteria (object or JSON string)
     * @returns {Promise<Array>} Matching image objects (may be empty)
     * @private
     */
    async function _evaluateFilter(filterJson) {
        const filter = typeof filterJson === 'string'
            ? JSON.parse(filterJson)
            : filterJson;

        // Start with all non-deleted images
        let candidates = AppState.images.getAll();
        if (!candidates.length) return [];

        // Normalise legacy ISO-string date format from older saved groups
        AppState.filter.normaliseLegacyDates(filter);

        // Date filter (component-based with wildcard support)
        if (filter.dateFrom || filter.dateTo) {
            const isRange = !!filter.dateRange;
            candidates = candidates.filter(img =>
                AppState.filter.matchDate(
                    img.timestamp, filter.dateFrom || null,
                    filter.dateTo || null, isRange,
                ),
            );
        }

        // Rating filter
        if (filter.rating) {
            const ratingChars = [...filter.rating];
            candidates = candidates.filter(img =>
                img.rating && ratingChars.some(r => img.rating.includes(r)),
            );
        }

        // People filter — uses backend endpoint for image-to-person mapping
        if (filter.people?.length) {
            try {
                const peopleIds = filter.people.map(p => p.id);
                const peopleImageIds = await AppState.images.getFilteredByPeople(peopleIds);
                candidates = candidates.filter(img => peopleImageIds.has(String(img.id)));
            } catch (err) {
                console.warn('[_evaluateFilter] People filter failed:', err);
            }
        }

        // Text search (semantic) — returns only matching IDs
        if (filter.text) {
            try {
                const threshold = filter.threshold || 0.2;
                // Strip people names for a CLIP-friendly query
                let searchText = filter.text;
                if (filter.people?.length) {
                    const sorted = [...filter.people].sort((a, b) => b.name.length - a.name.length);
                    for (const person of sorted) {
                        const escaped = person.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                        searchText = searchText.replace(new RegExp('\\b' + escaped + '\\b', 'gi'), '');
                    }
                    searchText = searchText.replace(/\s{2,}/g, ' ').trim();
                }
                if (searchText) {
                    const response = await AppState.search.execute(searchText, threshold, 10000);
                    if (response?.results) {
                        const matchIds = new Set(response.results.map(r => r.id));
                        candidates = candidates.filter(img => matchIds.has(img.id));
                    }
                }
            } catch (err) {
                console.warn('[_evaluateFilter] Semantic search failed:', err);
            }
        }

        // Metadata filter
        if (filter.metadata && Object.keys(filter.metadata).length > 0) {
            try {
                const response = await App.apiPost('/metadata-search', {
                    criteria: filter.metadata,
                });
                if (response?.data?.image_ids) {
                    const metaIds = new Set(response.data.image_ids);
                    candidates = candidates.filter(img => metaIds.has(img.id));
                }
            } catch (err) {
                console.warn('[_evaluateFilter] Metadata search failed:', err);
            }
        }

        return candidates;
    }

    /**
     * Evaluate a smart group's filter to find the best preview image.
     *
     * Delegates to {@link _evaluateFilter} and picks the highest-aesthetic-
     * scoring match.
     *
     * @param {Object|string} filterJson - Filter criteria (object or JSON string)
     * @returns {Promise<string|null>} Best image ID, or null if no matches
     * @private
     */
    async function _evaluatePreview(filterJson) {
        const candidates = await _evaluateFilter(filterJson);
        if (candidates.length === 0) return null;

        // Pick the best by aesthetic score (same ranking as backend best_image)
        candidates.sort((a, b) => {
            const aScore = a.aesthetic_laion ?? -1;
            const bScore = b.aesthetic_laion ?? -1;
            if (bScore !== aScore) return bScore - aScore;
            const aSharp = a.laplacian_var ?? -1;
            const bSharp = b.laplacian_var ?? -1;
            if (bSharp !== aSharp) return bSharp - aSharp;
            return a.id.localeCompare(b.id);
        });

        return candidates[0].id;
    }

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
         * For named groups (levels 4-5): removes image but keeps the group
         * (named groups persist even when empty).
         *
         * @param {string} imageId - Image ID to remove
         */
        removeImage(imageId) {
            let changed = false;

            for (const level of Object.keys(_groupCache)) {
                const groups = _groupCache[level];
                if (!groups) continue;
                const isNamed = parseInt(level, 10) >= 4;

                for (let i = groups.length - 1; i >= 0; i--) {
                    const group = groups[i];
                    const idx = group.image_ids.indexOf(imageId);

                    if (idx !== -1) {
                        group.image_ids.splice(idx, 1);
                        group.count = group.image_ids.length;
                        changed = true;

                        // Auto levels: remove group if only 1 image left
                        // Named groups (4-5): keep group even when empty
                        if (!isNamed && group.image_ids.length <= 1) {
                            groups.splice(i, 1);
                        }
                    }

                    // Clear smart group preview if it was the removed image
                    if (group.filter_json && group.best_image?.id === imageId) {
                        group.best_image = null;
                        changed = true;
                        // Queue async re-evaluation of this group's preview
                        _stalePreviewHashes.add(group.group_hash);
                    }
                }
            }

            if (changed) {
                markDirty(domainRef);
            }

            // Kick off async preview refresh for affected smart groups
            if (_stalePreviewHashes.size > 0) {
                _refreshStalePreviewsDebounced();
            }
        },
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
                    total: data.total,
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
     * Create a backup of level-5 (custom) groups for rollback.
     * @returns {Array} Deep copy of level-5 group cache
     * @private
     */
    function _backupLevel5() {
        const groups = _groupCache[5];
        if (!groups) return [];
        return groups.map(g => ({
            ...g,
            image_ids: [...g.image_ids],
        }));
    }

    /**
     * Restore level-5 (custom) groups from a backup.
     * @param {Array} backup - Previously saved backup
     * @private
     */
    function _restoreLevel5(backup) {
        transaction(() => {
            _groupCache[5] = backup;
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
         * Levels 4-5 (named groups) never poll — status is always 'done'.
         * @param {number} level - Similarity level (0-5)
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
                    total: data.total,
                };
                _epochCache[level] = Date.now();

                const status = data.status;
                _computing = status === 'computing' || status === 'pending';

                // Start polling if computation in progress (not for named groups)
                if (level < 4) {
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
         * Get custom groups (level 5).
         * @returns {Array} Custom group objects with name, count, image_ids, best_image
         */
        getCustomGroups() {
            return _groupCache[5] || [];
        },

        /**
         * Get static (non-smart) custom groups only.
         * Filters out smart groups (those with filter_json).
         * Used by the group picker since adding static images to a
         * dynamic smart group is semantically wrong.
         * @returns {Array} Static custom group objects
         */
        getStaticCustomGroups() {
            return (_groupCache[5] || []).filter(g => !g.filter_json);
        },

        /**
         * Get level-5 (custom) groups that contain ALL of the given image IDs.
         * Used by the Group Picker to show which groups the selected images share.
         * @param {string[]} imageIds - Image IDs to check
         * @returns {Array} Custom groups containing all the given images
         */
        getGroupsForImages(imageIds) {
            const groups = _groupCache[5] || [];
            if (!imageIds || imageIds.length === 0) return [];
            return groups.filter(g =>
                imageIds.every(id => g.image_ids.includes(id)),
            );
        },

        /**
         * Get computation status for a level.
         * @param {number} level - Similarity level
         * @returns {{status: string, progress: number, total: number}|null}
         */
        getStatus(level) {
            // Named groups (directory + custom) have no computation phase
            if (level >= 4) return { status: 'done', progress: 0, total: 0 };
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
         * @param {number} level - Similarity level (0-5)
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
                image_ids: imageIds,
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

        // --- Actions (custom groups, level 5) ---

        /**
         * Create a new custom group (album) or smart group.
         * Uses optimistic update: cache is updated synchronously, then
         * API call is made. On error, cache is rolled back.
         *
         * @param {string} name - Group display name
         * @param {string[]} [imageIds=[]] - Initial image IDs (ignored for smart groups)
         * @param {Object} [filterJson] - Filter criteria for smart groups.
         *   When provided, creates a smart group with virtual membership.
         * @returns {Promise<string>} The new group hash (UUID)
         */
        async createGroup(name, imageIds = [], filterJson) {
            if (!App.requireOnline()) return;
            const groupHash = crypto.randomUUID();
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                if (!_groupCache[5]) _groupCache[5] = [];
                const entry = {
                    group_hash: groupHash,
                    name: name,
                    count: filterJson ? 0 : imageIds.length,
                    image_ids: filterJson ? [] : [...imageIds],
                    best_image: null,
                };
                if (filterJson) entry.filter_json = filterJson;
                _groupCache[5].push(entry);
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                const body = {
                    group_hash: groupHash,
                    name: name,
                };
                if (filterJson) {
                    body.filter_json = filterJson;
                } else {
                    body.image_ids = imageIds;
                }
                await App.apiPost('/groups', body);
                // Reload to get best_image from backend (smart groups won't have one)
                await this.loadLevel(5, true);
            } catch (err) {
                _restoreLevel5(backup);
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
            if (!App.requireOnline()) return;
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[5] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) group.name = name;
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPatch(`/groups/${groupHash}`, { name });
            } catch (err) {
                _restoreLevel5(backup);
                broadcastError(err.message || 'Failed to rename group');
                throw err;
            }
        },

        /**
         * Update the filter criteria of a smart group.
         * Sends the new filter and name in a single PATCH request.
         *
         * @param {string} groupHash - The group identifier
         * @param {string} name - Current or updated group name
         * @param {Object} filterJson - New filter criteria object
         * @returns {Promise<void>}
         */
        async updateGroupFilter(groupHash, name, filterJson) {
            if (!App.requireOnline()) return;
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[5] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) {
                    group.filter_json = filterJson;
                    group.name = name;
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPatch(`/groups/${groupHash}`, {
                    name,
                    filter_json: filterJson,
                });
            } catch (err) {
                _restoreLevel5(backup);
                broadcastError(err.message || 'Failed to update smart group');
                throw err;
            }
        },

        /**
         * Update only the preview thumbnail of a smart group.
         * Optimistic update + fire-and-forget API call.
         *
         * @param {string} groupHash - The group identifier
         * @param {string|null} imageId - Image ID for the thumbnail
         * @returns {Promise<void>}
         */
        async updateGroupPreview(groupHash, imageId) {
            const groups = _groupCache[5] || [];
            const group = groups.find(g => g.group_hash === groupHash);
            if (!group) return;

            const img = imageId ? AppState.images.getById(imageId) : null;
            transaction(() => {
                group.best_image = img ? { id: img.id, basename: img.basename } : null;
                markDirty(domainRef);
            });

            // Persist (fire-and-forget)
            App.apiPost(`/groups/${groupHash}/preview`, { image_id: imageId }).catch(e => console.warn('Preview persist failed:', e));
        },

        /**
         * Evaluate a smart group's filter and set its preview image.
         * Called after creating or updating a smart group, and after
         * image deletions that invalidate a smart group's thumbnail.
         *
         * @param {string} groupHash - The group identifier
         * @param {Object|string} filterJson - Filter criteria
         * @returns {Promise<void>}
         */
        async evaluateAndSetPreview(groupHash, filterJson) {
            const imageId = await _evaluatePreview(filterJson);
            await this.updateGroupPreview(groupHash, imageId);
        },

        /**
         * Delete a custom group.
         *
         * @param {string} groupHash - The group identifier
         * @returns {Promise<void>}
         */
        async deleteGroup(groupHash) {
            if (!App.requireOnline()) return;
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                if (_groupCache[5]) {
                    _groupCache[5] = _groupCache[5].filter(g => g.group_hash !== groupHash);
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiDelete(`/groups/${groupHash}`);
            } catch (err) {
                _restoreLevel5(backup);
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
            if (!App.requireOnline()) return;
            if (!imageIds || imageIds.length === 0) return;
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[5] || [];
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
                await this.loadLevel(5, true);
            } catch (err) {
                _restoreLevel5(backup);
                broadcastError(err.message || 'Failed to add images to group');
                throw err;
            }
        },

        /**
         * Preview which images would be kept/trashed by a quality-based
         * filter across groups.  Read-only — no side effects.
         *
         * Smart groups don't store memberships in the ``duplicate_groups``
         * table, so their image IDs must be resolved on the frontend.
         * Callers can pass pre-resolved IDs via ``options.resolvedGroups``
         * (from the refine dialog's async evaluation) to avoid
         * re-evaluating filters.  If not provided and smart groups are
         * in scope, this method falls back to evaluating them here.
         *
         * @param {number} level - Group level (0-5)
         * @param {Object} [options]
         * @param {number} [options.keepCount] - Images to keep per group
         * @param {number} [options.keepPercent] - Percentage to keep
         * @param {number} [options.trashCount] - Images to trash per group
         * @param {number} [options.trashPercent] - Percentage to trash
         * @param {string[]} [options.groupHashes] - Specific groups to preview
         * @param {Array<{group_hash: string, image_ids: string[]}>} [options.resolvedGroups]
         *     Pre-resolved smart group image IDs (avoids re-evaluation)
         * @returns {Promise<{keepIds: string[], trashIds: string[]}>}
         */
        async previewGroups(level, options = {}) {
            // Use pre-resolved smart group IDs if the caller already
            // evaluated them (e.g. the refine dialog).  Otherwise fall
            // back to evaluating here for callers that don't pre-resolve.
            let explicitGroups = options.resolvedGroups || undefined;

            if (!explicitGroups && level === 5) {
                const targetHashes = options.groupHashes
                    ? new Set(options.groupHashes)
                    : null;
                const smartGroups = (_groupCache[5] || []).filter(g =>
                    g.filter_json && (!targetHashes || targetHashes.has(g.group_hash)),
                );
                if (smartGroups.length > 0) {
                    explicitGroups = [];
                    for (const sg of smartGroups) {
                        const matches = await _evaluateFilter(sg.filter_json);
                        if (matches.length > 0) {
                            explicitGroups.push({
                                group_hash: sg.group_hash,
                                image_ids: matches.map(img => img.id),
                            });
                        }
                    }
                }
            }

            const response = await App.apiPost('/groups/preview', {
                level,
                keep_count: options.keepCount,
                keep_percent: options.keepPercent,
                trash_count: options.trashCount,
                trash_percent: options.trashPercent,
                group_hashes: options.groupHashes,
                explicit_groups: explicitGroups,
            });
            return {
                keepIds: response.data?.keep_ids || [],
                trashIds: response.data?.trash_ids || [],
            };
        },

        /**
         * Evaluate a smart group's filter criteria and return matching
         * image IDs.  Used by the refine dialog to resolve dynamic group
         * membership counts before displaying.
         *
         * @param {Object|string} filterJson - Filter criteria
         * @returns {Promise<string[]>} Matching image IDs
         */
        async evaluateSmartGroupFilter(filterJson) {
            const matches = await _evaluateFilter(filterJson);
            return matches.map(img => img.id);
        },

        /**
         * Prune duplicate groups by trashing lower-quality images.
         *
         * The backend endpoint returns quickly after enqueueing (soft-delete
         * + cache invalidation are immediate).  File moves happen
         * asynchronously via the TrashWorker.  If images were enqueued,
         * this method waits for the trash queue to drain while updating
         * the loading overlay with live progress.
         *
         * Supports two mutually exclusive modes:
         * - **Keep mode** (default): keep the best N images, trash the rest.
         * - **Trash mode**: trash the worst N images, keep the rest.
         *
         * @param {number} level - Similarity level (0-4)
         * @param {Object} [options]
         * @param {number} [options.keepCount] - Images to keep per group
         * @param {number} [options.keepPercent] - Percentage to keep (overrides keepCount)
         * @param {number} [options.trashCount] - Images to trash per group (inverse of keepCount)
         * @param {number} [options.trashPercent] - Percentage to trash (inverse of keepPercent)
         * @param {string[]} [options.groupHashes] - Specific groups to prune
         * @returns {Promise<{trashedCount: number, groupCount: number}>}
         */
        async pruneGroups(level, options = {}) {
            const response = await App.apiPost('/duplicates/prune', {
                level,
                keep_count: options.keepCount,
                keep_percent: options.keepPercent,
                trash_count: options.trashCount,
                trash_percent: options.trashPercent,
                group_hashes: options.groupHashes,
            });

            const data = response.data;

            if (data.trashed_count > 0) {
                // Wait for background file moves to complete
                await _waitForTrashQueueDrain();
            }

            // Reload affected data (reflects soft-deletes)
            await AppState.images.load(true);
            await this.loadLevel(level, true);

            return {
                trashedCount: data.trashed_count,
                groupCount: data.group_count,
            };
        },

        /**
         * Remove images from a custom group (group persists even if empty).
         *
         * @param {string} groupHash - The group identifier
         * @param {string[]} imageIds - Image IDs to remove
         * @returns {Promise<void>}
         */
        async removeImages(groupHash, imageIds) {
            if (!App.requireOnline()) return;
            if (!imageIds || imageIds.length === 0) return;
            const backup = _backupLevel5();

            // Phase 1: Synchronous optimistic update
            transaction(() => {
                const groups = _groupCache[5] || [];
                const group = groups.find(g => g.group_hash === groupHash);
                if (group) {
                    const removeSet = new Set(imageIds);
                    group.image_ids = group.image_ids.filter(id => !removeSet.has(id));
                    group.count = group.image_ids.length;
                    // Replace best_image if it was one of the removed images
                    // (pick from remaining; the reload below will set the real one)
                    if (group.best_image && removeSet.has(group.best_image.id)) {
                        const fallbackId = group.image_ids[0];
                        group.best_image = fallbackId
                            ? { id: fallbackId }
                            : null;
                    }
                }
                markDirty(domainRef);
            });

            // Phase 2: Async API call
            try {
                await App.apiPost(`/groups/${groupHash}/images/remove`, { image_ids: imageIds });
                // Reload to get updated best_image from backend
                await this.loadLevel(5, true);
            } catch (err) {
                _restoreLevel5(backup);
                broadcastError(err.message || 'Failed to remove images from group');
                throw err;
            }
        },
    };
})();
