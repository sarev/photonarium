/**
 * AppState2 - Refactored State Management
 * ========================================
 *
 * New architecture where:
 * - AppState generates all IDs using crypto.randomUUID()
 * - Backend is a dumb persistence layer (validates and stores)
 * - Application logic lives only in AppState
 * - API returns success/error only
 *
 * See snippets/appstate-authority-design.md for full design.
 */

'use strict';

const AppState = (function() {

    // =========================================================================
    // INTERNAL UTILITIES
    // =========================================================================

    /**
     * Create a subscriber management system for a domain.
     * @returns {Object} {subscribe, subscribeError, broadcast, notify, broadcastError}
     */
    function createSubscriberSystem() {
        const subscribers = new Set();
        const errorSubscribers = new Set();

        /**
         * Notify all subscribers of a state change.
         * @param {Object} event - Event object (default: {type: 'changed'})
         */
        function notify(event = { type: 'changed' }) {
            for (const callback of subscribers) {
                try {
                    callback(event);
                } catch (err) {
                    console.error('AppState subscriber error:', err);
                }
            }
        }

        return {
            subscribe(callback) {
                subscribers.add(callback);
                return () => subscribers.delete(callback);
            },
            subscribeError(callback) {
                errorSubscribers.add(callback);
                return () => errorSubscribers.delete(callback);
            },
            broadcast: notify,
            notify,
            broadcastError(message) {
                const event = { type: 'error', message };
                for (const callback of errorSubscribers) {
                    try {
                        callback(event);
                    } catch (err) {
                        console.error('AppState error subscriber error:', err);
                    }
                }
            }
        };
    }

    /**
     * localStorage helper with JSON serialization.
     */
    const storage = {
        get(key, defaultValue) {
            try {
                const value = localStorage.getItem(`imaginary-${key}`);
                return value !== null ? JSON.parse(value) : defaultValue;
            } catch {
                return defaultValue;
            }
        },
        set(key, value) {
            try {
                localStorage.setItem(`imaginary-${key}`, JSON.stringify(value));
            } catch (err) {
                console.error('AppState localStorage error:', err);
            }
        }
    };

    // =========================================================================
    // TRANSACTION SYSTEM
    // =========================================================================

    let _txEpoch = 0;
    let _inTransaction = false;
    let _dirtyDomains = new Set();
    let _transactionQueue = Promise.resolve();

    /**
     * Mark a domain as needing notification.
     * @param {Object} domain - Domain object with _notify method
     */
    function markDirty(domain) {
        if (_inTransaction) {
            _dirtyDomains.add(domain);
        } else {
            console.warn('AppState: State mutation outside transaction:', domain._name);
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Flush notifications for all dirty domains.
     */
    function flushDirty() {
        const domains = Array.from(_dirtyDomains);
        _dirtyDomains.clear();

        for (const domain of domains) {
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Run a function within a transaction.
     * SYNCHRONOUS: use for optimistic cache updates.
     * @param {Function} fn - Sync function to run
     * @returns {*} Result of fn
     */
    function transaction(fn) {
        if (_inTransaction) {
            return fn();
        }

        _inTransaction = true;
        _txEpoch++;
        _dirtyDomains.clear();

        try {
            const result = fn();
            flushDirty();
            _inTransaction = false;
            return result;
        } catch (err) {
            flushDirty();
            _inTransaction = false;
            throw err;
        }
    }

    /**
     * Queue an async operation to run after pending operations complete.
     * @param {Function} fn - Async function
     * @returns {Promise}
     */
    function queueTransaction(fn) {
        _transactionQueue = _transactionQueue
            .then(() => fn())
            .catch(err => {
                console.error('AppState: Transaction failed:', err);
                throw err;
            });
        return _transactionQueue;
    }

    function isInTransaction() {
        return _inTransaction;
    }

    function getTransactionEpoch() {
        return _txEpoch;
    }

    // Debug helper
    window._appStateDebug = {
        getEpoch: () => _txEpoch,
        isInTransaction: () => _inTransaction,
        getDirtyDomains: () => Array.from(_dirtyDomains).map(d => d._name),
    };

    // =========================================================================
    // VIEW DOMAIN (localStorage persistence)
    // =========================================================================

    const view = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        let _theme = storage.get('theme', null);
        let _thumbnailSize = storage.get('thumbnailSize', 200);
        let _sortBy = storage.get('sortBy', 'date');
        let _sortDirection = storage.get('sortDirection', 'desc');

        if (_theme === null) {
            _theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        function applyThemeToDOM(theme) {
            const app = document.getElementById('app');
            if (app) {
                app.dataset.theme = theme;
            }
        }

        return {
            _name: 'view',
            _notify: notify,
            onChanged: subscribe,

            init() {
                applyThemeToDOM(_theme);
            },

            getTheme() { return _theme; },
            setTheme(theme) {
                if (theme !== 'light' && theme !== 'dark') return;
                if (_theme === theme) return;
                _theme = theme;
                storage.set('theme', theme);
                applyThemeToDOM(theme);
                broadcast({ type: 'changed', property: 'theme' });
            },
            toggleTheme() {
                this.setTheme(_theme === 'light' ? 'dark' : 'light');
            },

            getThumbnailSize() { return _thumbnailSize; },
            setThumbnailSize(size) {
                size = Math.max(100, Math.min(400, Number(size) || 200));
                if (_thumbnailSize === size) return;
                _thumbnailSize = size;
                storage.set('thumbnailSize', size);
                broadcast({ type: 'changed', property: 'thumbnailSize' });
            },

            getSort() { return { by: _sortBy, direction: _sortDirection }; },
            getSortBy() { return _sortBy; },
            setSortBy(by) {
                const valid = ['date', 'rating', 'content', 'people'];
                if (!valid.includes(by)) return;
                if (_sortBy === by) return;
                _sortBy = by;
                storage.set('sortBy', by);
                broadcast({ type: 'changed', property: 'sortBy' });
            },
            getSortDirection() { return _sortDirection; },
            setSortDirection(direction) {
                if (direction !== 'asc' && direction !== 'desc') return;
                if (_sortDirection === direction) return;
                _sortDirection = direction;
                storage.set('sortDirection', direction);
                broadcast({ type: 'changed', property: 'sortDirection' });
            },
            toggleSortDirection() {
                this.setSortDirection(_sortDirection === 'asc' ? 'desc' : 'asc');
            }
        };
    })();

    // =========================================================================
    // NAV DOMAIN (memory only)
    // =========================================================================

    const nav = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        let _screen = null;
        let _previousScreen = null;
        let _history = [];
        let _fullscreenImageId = null;
        let _scrollPositions = {};

        return {
            _name: 'nav',
            _notify: notify,
            onChanged: subscribe,

            getScreen() { return _screen; },
            getPreviousScreen() { return _previousScreen; },
            setScreen(screen, options = {}) {
                const { addToHistory = true, data = null } = options;
                if (_screen === screen) return;
                _previousScreen = _screen;
                if (addToHistory && _previousScreen) {
                    _history.push(_previousScreen);
                }
                _screen = screen;
                broadcast({ type: 'changed', property: 'screen', data });
            },

            canGoBack() { return _history.length > 0; },
            goBack() {
                if (_history.length === 0) return false;
                const previous = _history.pop();
                _previousScreen = _screen;
                _screen = previous;
                broadcast({ type: 'changed', property: 'screen' });
                return true;
            },
            clearHistory() { _history = []; },

            isFullscreenOpen() { return _fullscreenImageId !== null; },
            getFullscreenImageId() { return _fullscreenImageId; },
            setFullscreenImageId(imageId) {
                if (_fullscreenImageId === imageId) return;
                _fullscreenImageId = imageId;
                broadcast({ type: 'changed', property: 'fullscreenImageId' });
            },
            closeFullscreen() {
                if (_fullscreenImageId === null) return;
                const lastImageId = _fullscreenImageId;
                broadcast({ type: 'changed', property: 'fullscreenClosing', imageId: lastImageId });
                _fullscreenImageId = null;
            },

            getScrollPosition(screen) { return _scrollPositions[screen] || 0; },
            setScrollPosition(screen, position) { _scrollPositions[screen] = position; },
            clearScrollPositions() { _scrollPositions = {}; }
        };
    })();

    // =========================================================================
    // FILTER DOMAIN (memory only)
    // =========================================================================

    const filter = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        let _filter = null;

        return {
            _name: 'filter',
            _notify: notify,
            onChanged: subscribe,

            get() { return _filter; },
            set(newFilter, options = {}) {
                const { silent = false } = options;
                _filter = newFilter;
                if (!silent) broadcast({ type: 'changed' });
            },
            clear() {
                if (_filter === null) return;
                _filter = null;
                broadcast({ type: 'changed' });
            },
            isActive() { return _filter !== null; },

            getText() { return _filter?.text || null; },
            getDateRange() {
                if (!_filter) return null;
                return { start: _filter.dateStart, end: _filter.dateEnd };
            },
            getRating() { return _filter?.rating || null; },
            getPeople() { return _filter?.people || null; }
        };
    })();

    // =========================================================================
    // STATUS DOMAIN (polling)
    // =========================================================================

    const status = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        let _status = null;
        let _prevStatus = null;
        let _pollTimer = null;
        let _loading = false;

        return {
            _name: 'status',
            _notify: notify,
            onChanged: subscribe,

            async load() {
                if (_loading) return _status;
                _loading = true;
                try {
                    _prevStatus = _status;
                    _status = (await App.apiGet('/status')).data;

                    // Check for face reassessment completion
                    const wasCompleted = _prevStatus?.face_reassessment?.completed;
                    const isCompleted = _status?.face_reassessment?.completed;
                    if (isCompleted && !wasCompleted) {
                        App.apiPost('/faces/reassess-ack').catch(err => {
                            console.warn('Failed to ack reassessment:', err);
                        });
                        setTimeout(() => {
                            if (faces.isLoaded()) {
                                faces.load(true);
                            }
                        }, 0);
                    }

                    broadcast({ type: 'changed' });
                    return _status;
                } catch (err) {
                    console.error('AppState.status load error:', err);
                    throw err;
                } finally {
                    _loading = false;
                }
            },

            get() { return _status; },
            isFaceDetectionEnabled() { return _status?.face_detection_enabled !== false; },
            isUpdating() { return _status?.status === 'updating'; },
            getQueues() {
                return {
                    indexing: _status?.indexing_queue || 0,
                    embedding: _status?.embedding_queue || 0,
                    face: _status?.face_queue || 0
                };
            },

            startPolling(intervalMs = 1000) {
                if (_pollTimer) return;
                this.load();
                _pollTimer = setInterval(() => this.load(), intervalMs);
            },
            stopPolling() {
                if (_pollTimer) {
                    clearInterval(_pollTimer);
                    _pollTimer = null;
                }
            },
            isPolling() { return _pollTimer !== null; }
        };
    })();

    // =========================================================================
    // SEARCH DOMAIN (memory only)
    // =========================================================================

    const search = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        let _results = null;
        let _loading = false;
        let _query = null;
        let _threshold = null;

        return {
            _name: 'search',
            _notify: notify,
            onChanged: subscribe,

            async execute(query, threshold = 0.2, limit = 500) {
                _loading = true;
                _query = query;
                _threshold = threshold;
                broadcast({ type: 'loading' });
                try {
                    const response = await App.apiPost('/search', { query, threshold, limit });
                    _results = response.data;
                    _loading = false;
                    broadcast({ type: 'changed' });
                    return _results;
                } catch (err) {
                    _loading = false;
                    broadcast({ type: 'error', message: err.message });
                    throw err;
                }
            },

            getResults() { return _results; },
            getQuery() { return _query; },
            getThreshold() { return _threshold; },
            isLoading() { return _loading; },

            clear() {
                _results = null;
                _query = null;
                _threshold = null;
                broadcast({ type: 'changed' });
            }
        };
    })();

    // =========================================================================
    // FOLDERS DOMAIN (backend persistence)
    // =========================================================================

    const folders = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        let _folders = [];
        let _status = null;
        let _stats = null;
        let _loading = false;

        async function load() {
            if (_loading) return;
            _loading = true;
            try {
                const foldersResponse = await App.apiGet('/folders');
                _folders = foldersResponse.data || [];
                broadcast({ type: 'changed' });
            } catch (err) {
                console.error('AppState.folders load error:', err);
                broadcastError(err.message || 'Failed to load folders');
            } finally {
                _loading = false;
            }
        }

        return {
            _name: 'folders',
            _notify: notify,
            onChanged: subscribe,
            onError: subscribeError,

            load,

            getAll() { return _folders; },
            async add(path) {
                try {
                    const response = await App.apiPost('/folders', { path });
                    if (response && response.success === false) {
                        throw new Error(response.error || 'Failed to add folder');
                    }
                    await load();
                } catch (err) {
                    broadcastError(err.message || 'Failed to add folder');
                    throw err;
                }
            },
            async remove(path) {
                try {
                    await App.apiDelete(`/folders/${encodeURIComponent(path)}`);
                    _folders = _folders.filter(f => f.path !== path);
                    broadcast({ type: 'changed' });
                } catch (err) {
                    broadcastError(err.message || 'Failed to remove folder');
                    throw err;
                }
            },
            async rescan() {
                try {
                    const response = await App.apiPost('/rescan');
                    if (response && response.success === false) {
                        throw new Error(response.error || 'Failed to start rescan');
                    }
                    broadcast({ type: 'rescanStarted' });
                } catch (err) {
                    broadcastError(err.message || 'Failed to start rescan');
                    throw err;
                }
            },

            async loadStats() {
                try {
                    _stats = (await App.apiGet('/stats')).data;
                    broadcast({ type: 'changed', property: 'stats' });
                    return _stats;
                } catch (err) {
                    console.error('AppState.folders loadStats error:', err);
                    broadcastError(err.message || 'Failed to load stats');
                    throw err;
                }
            },
            getStats() { return _stats; },

            getStatus() { return _status; },
            setStatus(status) {
                const wasUpdating = _status?.status === 'updating';
                const nowUpToDate = status?.status === 'up_to_date';
                _status = status;
                broadcast({ type: 'changed', property: 'status' });
                if (wasUpdating && nowUpToDate) {
                    broadcast({ type: 'databaseChanged' });
                }
            },
            isUpdating() { return _status?.status === 'updating'; },
            getQueueCounts() {
                if (!_status) return { indexing: 0, embedding: 0, faces: 0 };
                return {
                    indexing: _status.indexing_queue || 0,
                    embedding: _status.embedding_queue || 0,
                    faces: _status.face_queue || 0
                };
            }
        };
    })();

    // =========================================================================
    // PEOPLE DOMAIN
    // =========================================================================
    // Forward declaration - faces domain references people._internal
    let people;

    // =========================================================================
    // FACES DOMAIN
    // =========================================================================

    const faces = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;
        let _loading = false;
        let _pendingLoad = null;

        // Derived view caches
        let _unknownFaces = null;
        let _facesByPerson = null;
        let _facesByImage = null;

        const domainRef = { _name: 'faces', _notify: notify };

        function invalidateDerived() {
            _unknownFaces = null;
            _facesByPerson = null;
            _facesByImage = null;
        }

        // =====================================================================
        // CACHE PRIMITIVES (_internal)
        // =====================================================================

        const _internal = {
            /**
             * Get face by ID.
             */
            get(id) {
                return _cache?.get(id) || null;
            },

            /**
             * Link a face to a person.
             * Does NOT set locked - that's controlled by the caller.
             */
            linkToPerson(faceId, personId, personName) {
                const face = _cache?.get(faceId);
                if (face) {
                    face.person_id = personId;
                    face.person_name = personName;
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Unlink a face from its person.
             * Does NOT change locked status - that's handled by unassignBatch.
             */
            unlinkFromPerson(faceId) {
                const face = _cache?.get(faceId);
                if (face && face.person_id) {
                    const oldPersonId = face.person_id;
                    face.person_id = null;
                    face.person_name = null;
                    invalidateDerived();
                    markDirty(domainRef);
                    return oldPersonId;
                }
                return null;
            },

            /**
             * Set the manually_tagged (locked) flag.
             */
            setLocked(faceId, locked) {
                const face = _cache?.get(faceId);
                if (face) {
                    face.manually_tagged = locked;
                    markDirty(domainRef);
                }
            },

            /**
             * Set the suppressed flag.
             */
            setSuppressed(faceId, suppressed) {
                const face = _cache?.get(faceId);
                if (face) {
                    face.suppressed = suppressed;
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Update denormalized person_name (used when person is renamed).
             */
            updateName(faceId, newName) {
                const face = _cache?.get(faceId);
                if (face && face.person_id) {
                    face.person_name = newName;
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Update arbitrary properties.
             */
            update(id, changes) {
                const face = _cache?.get(id);
                if (face) {
                    Object.assign(face, changes);
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Remove a face from cache.
             */
            remove(id) {
                if (_cache?.delete(id)) {
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            // =====================================================================
            // BATCH HELPERS
            // =====================================================================

            /**
             * Assign faces to a person. Returns set of old person IDs affected.
             */
            assignToPersonBatch(faceIds, personId, { lock = false } = {}) {
                const person = people._internal.get(personId);
                if (!person) return new Set();

                const affectedPersonIds = new Set();

                for (const faceId of faceIds) {
                    const face = this.get(faceId);
                    if (!face || face.suppressed) continue;

                    // Track old person
                    if (face.person_id && face.person_id !== personId) {
                        affectedPersonIds.add(face.person_id);
                    }

                    this.linkToPerson(faceId, personId, person.name);
                    if (lock) {
                        this.setLocked(faceId, true);
                    }
                }

                return affectedPersonIds;
            },

            /**
             * Unassign faces from their persons. Returns set of affected person IDs.
             */
            unassignBatch(faceIds) {
                const affectedPersonIds = new Set();

                for (const faceId of faceIds) {
                    const face = this.get(faceId);
                    if (!face?.person_id) continue;

                    affectedPersonIds.add(face.person_id);
                    this.unlinkFromPerson(faceId);
                    this.setLocked(faceId, false);
                }

                return affectedPersonIds;
            },

            /**
             * Pick face with newest image_timestamp.
             */
            pickNewestFace(faceIds) {
                let newest = null;
                let newestTime = null;
                for (const faceId of faceIds) {
                    const face = this.get(faceId);
                    const ts = face?.image_timestamp || 0;
                    if (newestTime === null || ts > newestTime) {
                        newestTime = ts;
                        newest = faceId;
                    }
                }
                return newest || faceIds[0];
            },

            /**
             * Get faces for a person (sync read).
             */
            getForPerson(personId) {
                if (!_cache) return [];
                if (_facesByPerson === null) {
                    _facesByPerson = new Map();
                    for (const face of _cache.values()) {
                        if (face.person_id) {
                            const list = _facesByPerson.get(face.person_id) || [];
                            list.push(face);
                            _facesByPerson.set(face.person_id, list);
                        }
                    }
                }
                return _facesByPerson.get(personId) || [];
            },

            /**
             * Get first face for a person.
             */
            getFirstForPerson(personId, options = {}) {
                const { excludingImageId = null } = options;
                const faces = this.getForPerson(personId);
                if (excludingImageId) {
                    return faces.find(f => f.image_id !== excludingImageId) || null;
                }
                return faces[0] || null;
            },

            /**
             * Apply auto-matches from backend (no lock, no markDirty per item).
             */
            applyAutoMatches(updates) {
                if (!_cache || !updates || updates.length === 0) return [];

                const applied = [];
                for (const { face_id, person_id, person_name } of updates) {
                    const face = _cache.get(face_id);
                    if (face && !face.person_id) {
                        face.person_id = person_id;
                        face.person_name = person_name;
                        applied.push({ face_id, person_id });
                    }
                }

                if (applied.length > 0) {
                    invalidateDerived();
                    markDirty(domainRef);
                }
                return applied;
            }
        };

        // =====================================================================
        // LOAD
        // =====================================================================

        async function load(force = false) {
            if (!force && _cache !== null) return;
            if (_pendingLoad) return _pendingLoad;

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    const response = await App.apiGet('/faces');
                    _cache = new Map(response.data.map(f => [f.id, f]));
                    invalidateDerived();
                    broadcast({ type: 'changed' });
                } catch (err) {
                    console.error('AppState.faces load error:', err);
                    broadcastError(err.message || 'Failed to load faces');
                    throw err;
                } finally {
                    _loading = false;
                    _pendingLoad = null;
                }
            })();

            return _pendingLoad;
        }

        // =====================================================================
        // PERSIST FUNCTIONS
        // =====================================================================

        async function _persistIdentify(faceIds, personId, createdPerson, personName, preferredFaceId) {
            // Create person if new
            if (createdPerson) {
                await App.apiPost('/people', {
                    id: personId,
                    name: personName,
                    preferred_face_id: preferredFaceId
                });
            }
            // Assign faces
            await App.apiPost('/faces/assign', {
                face_ids: faceIds,
                person_id: personId
            });
            // Lock faces
            await App.apiPatch('/faces', {
                face_ids: faceIds,
                locked: true
            });
        }

        async function _persistUnassign(faceIds) {
            await App.apiPost('/faces/unassign', { face_ids: faceIds });
        }

        async function _persistSuppress(faceIds) {
            await App.apiPost('/faces/suppress', { face_ids: faceIds });
        }

        async function _persistSetLocked(faceIds, locked) {
            await App.apiPatch('/faces', { face_ids: faceIds, locked });
        }

        // =====================================================================
        // PUBLIC API
        // =====================================================================

        return {
            _name: 'faces',
            _notify: notify,
            _internal,

            onChanged: subscribe,
            onError: subscribeError,

            load,
            reload() { return load(true); },

            // --- Accessors ---
            getAll() {
                return _cache ? Array.from(_cache.values()) : [];
            },
            getById(id) {
                return _cache?.get(id) || null;
            },
            getCount() {
                return _cache?.size || 0;
            },
            isLoaded() {
                return _cache !== null;
            },
            isLoading() {
                return _loading;
            },

            getUnknown() {
                if (!_cache) return [];
                if (_unknownFaces === null) {
                    _unknownFaces = Array.from(_cache.values())
                        .filter(f => !f.person_id && !f.suppressed);
                }
                return _unknownFaces;
            },

            getForPerson(personId) {
                return _internal.getForPerson(personId);
            },

            getForImage(imageId) {
                if (!_cache) return [];
                if (_facesByImage === null) {
                    _facesByImage = new Map();
                    for (const face of _cache.values()) {
                        const list = _facesByImage.get(face.image_id) || [];
                        list.push(face);
                        _facesByImage.set(face.image_id, list);
                    }
                }
                return _facesByImage.get(imageId) || [];
            },

            async fetchForImage(imageId) {
                if (_cache) return this.getForImage(imageId);
                return (await App.apiGet(`/images/${imageId}/faces`)).data;
            },

            async fetchForPerson(personId) {
                return (await App.apiGet(`/people/${personId}/faces`)).data;
            },

            // =====================================================================
            // PUBLIC MUTATIONS
            // =====================================================================

            /**
             * Identify faces - manual identification.
             * Locks faces, triggers backend similarity search.
             */
            identify(faceIds, personName, options = {}) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                if (!faceIds?.length) return Promise.resolve();

                // Empty name = unassign
                const trimmedName = personName?.trim() || '';
                if (!trimmedName) {
                    return this.unassign(faceIds);
                }

                const { preferredFaceId = null } = options;

                // Backup for rollback
                const backup = {
                    faces: new Map(),
                    people: new Map(),
                    createdPersonId: null
                };

                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.faces.set(faceId, {
                            person_id: face.person_id,
                            person_name: face.person_name,
                            manually_tagged: face.manually_tagged
                        });
                    }
                }

                let personId, createdPerson = false;

                // PHASE 1: Synchronous optimistic updates
                transaction(() => {
                    // Find or create person
                    let person = people._internal.findByName(trimmedName);
                    if (!person) {
                        personId = crypto.randomUUID();
                        person = {
                            id: personId,
                            name: trimmedName,
                            face_count: 0,
                            preferred_face_id: null,
                            threshold: null
                        };
                        people._internal.add(person);
                        createdPerson = true;
                        backup.createdPersonId = personId;
                    } else {
                        personId = person.id;
                    }

                    // Backup person state for rollback
                    backup.people.set(personId, { ...person });

                    // Assign faces (locked)
                    const affectedPersonIds = _internal.assignToPersonBatch(faceIds, personId, { lock: true });

                    // Backup affected persons
                    for (const pid of affectedPersonIds) {
                        const p = people._internal.get(pid);
                        if (p && !backup.people.has(pid)) {
                            backup.people.set(pid, { ...p });
                        }
                    }

                    // Set preferred if person has none
                    if (!people._internal.get(personId).preferred_face_id) {
                        const prefId = preferredFaceId || _internal.pickNewestFace(faceIds);
                        people._internal.setPreferred(personId, prefId);
                    }

                    // Reconcile
                    people._internal.reconcilePerson(personId);
                    people._internal.reconcileAll(affectedPersonIds);
                });

                // PHASE 2: Async persist
                const finalPersonId = personId;
                const finalPreferredId = preferredFaceId || _internal.pickNewestFace(faceIds);

                return queueTransaction(async () => {
                    try {
                        await _persistIdentify(faceIds, finalPersonId, createdPerson, trimmedName, finalPreferredId);
                        return { personId: finalPersonId };
                    } catch (err) {
                        // Rollback
                        transaction(() => {
                            // Restore faces
                            for (const [fid, data] of backup.faces) {
                                const face = _cache?.get(fid);
                                if (face) {
                                    face.person_id = data.person_id;
                                    face.person_name = data.person_name;
                                    face.manually_tagged = data.manually_tagged;
                                }
                            }
                            invalidateDerived();
                            markDirty(domainRef);

                            // Restore/remove people
                            if (backup.createdPersonId) {
                                people._internal.remove(backup.createdPersonId);
                            }
                            for (const [pid, data] of backup.people) {
                                if (pid !== backup.createdPersonId) {
                                    const p = people._internal.get(pid);
                                    if (p) Object.assign(p, data);
                                }
                            }
                        });

                        broadcastError(err.message || 'Failed to identify faces');
                        throw err;
                    }
                });
            },

            /**
             * Auto-assign faces - backend-triggered, no lock, no persist.
             */
            autoAssign(faceIds, personId) {
                if (!faceIds?.length) return;
                if (!people._internal.get(personId)) return;

                transaction(() => {
                    const affectedPersonIds = _internal.assignToPersonBatch(faceIds, personId, { lock: false });
                    people._internal.reconcilePerson(personId);
                    people._internal.reconcileAll(affectedPersonIds);
                });
            },

            /**
             * Unassign faces - return to unknown pool, unlock.
             */
            unassign(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                if (!faceIds?.length) return Promise.resolve();

                // Backup
                const backup = new Map();
                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.set(faceId, {
                            person_id: face.person_id,
                            person_name: face.person_name,
                            manually_tagged: face.manually_tagged
                        });
                    }
                }

                let affectedPersonIds;

                // PHASE 1: Optimistic
                transaction(() => {
                    affectedPersonIds = _internal.unassignBatch(faceIds);
                    people._internal.reconcileAll(affectedPersonIds);
                });

                // PHASE 2: Persist
                return queueTransaction(async () => {
                    try {
                        await _persistUnassign(faceIds);
                    } catch (err) {
                        // Rollback
                        transaction(() => {
                            for (const [fid, data] of backup) {
                                if (data.person_id) {
                                    _internal.linkToPerson(fid, data.person_id, data.person_name);
                                    _internal.setLocked(fid, data.manually_tagged);
                                }
                            }
                            // Restore people counts
                            for (const pid of affectedPersonIds) {
                                people._internal.reconcilePerson(pid);
                            }
                        });
                        broadcastError(err.message || 'Failed to unassign faces');
                        throw err;
                    }
                });
            },

            /**
             * Suppress faces - mark as false positives.
             */
            suppress(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                if (!faceIds?.length) return Promise.resolve();

                // Backup
                const backup = new Map();
                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.set(faceId, {
                            suppressed: face.suppressed,
                            person_id: face.person_id,
                            person_name: face.person_name,
                            manually_tagged: face.manually_tagged
                        });
                    }
                }

                let affectedPersonIds;

                // PHASE 1: Optimistic
                transaction(() => {
                    affectedPersonIds = _internal.unassignBatch(faceIds);
                    for (const faceId of faceIds) {
                        _internal.setSuppressed(faceId, true);
                    }
                    people._internal.reconcileAll(affectedPersonIds);
                });

                // PHASE 2: Persist
                return queueTransaction(async () => {
                    try {
                        await _persistSuppress(faceIds);
                    } catch (err) {
                        // Rollback
                        transaction(() => {
                            for (const [fid, data] of backup) {
                                const face = _cache?.get(fid);
                                if (face) {
                                    face.suppressed = data.suppressed;
                                    if (data.person_id) {
                                        face.person_id = data.person_id;
                                        face.person_name = data.person_name;
                                        face.manually_tagged = data.manually_tagged;
                                    }
                                }
                            }
                            invalidateDerived();
                            markDirty(domainRef);
                            for (const pid of affectedPersonIds) {
                                people._internal.reconcilePerson(pid);
                            }
                        });
                        broadcastError(err.message || 'Failed to suppress faces');
                        throw err;
                    }
                });
            },

            /**
             * Set locked status for faces.
             */
            setLocked(faceIds, locked) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                if (!faceIds?.length) return Promise.resolve();

                // Cannot unlock preferred faces
                if (!locked) {
                    for (const faceId of faceIds) {
                        const face = _internal.get(faceId);
                        if (face?.person_id) {
                            const person = people._internal.get(face.person_id);
                            if (person?.preferred_face_id === faceId) {
                                throw new Error('Cannot unlock the preferred face');
                            }
                        }
                    }
                }

                // Backup
                const backup = new Map();
                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.set(faceId, face.manually_tagged);
                    }
                }

                // PHASE 1: Optimistic
                transaction(() => {
                    for (const faceId of faceIds) {
                        _internal.setLocked(faceId, locked);
                    }
                });

                // PHASE 2: Persist
                return queueTransaction(async () => {
                    try {
                        await _persistSetLocked(faceIds, locked);
                    } catch (err) {
                        // Rollback
                        transaction(() => {
                            for (const [fid, wasLocked] of backup) {
                                _internal.setLocked(fid, wasLocked);
                            }
                        });
                        broadcastError(err.message || 'Failed to update lock status');
                        throw err;
                    }
                });
            },

            /**
             * Apply threshold changes from backend.
             */
            applyThresholdChanges(personId, assignedFaceIds, unassignedFaceIds) {
                if (!people._internal.get(personId)) return;

                transaction(() => {
                    if (assignedFaceIds?.length) {
                        _internal.assignToPersonBatch(assignedFaceIds, personId, { lock: false });
                    }

                    if (unassignedFaceIds?.length) {
                        // Filter out locked faces
                        const unlocked = unassignedFaceIds.filter(id => {
                            const face = _internal.get(id);
                            if (face?.manually_tagged) {
                                console.warn(`Backend tried to unassign locked face ${id}`);
                                return false;
                            }
                            return true;
                        });
                        _internal.unassignBatch(unlocked);
                    }

                    people._internal.reconcilePerson(personId);
                });
            },

            // Legacy method for compatibility
            toggleManualTag(faceId) {
                const face = _cache?.get(faceId);
                const newValue = !(face?.manually_tagged || false);
                return this.setLocked([faceId], newValue).then(() => newValue);
            },

            invalidate() {
                _cache = null;
                invalidateDerived();
            }
        };
    })();

    // =========================================================================
    // PEOPLE DOMAIN (initialized after faces)
    // =========================================================================

    people = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        let _cache = null;
        let _cacheTime = 0;
        let _thumbnailBust = new Map();
        let _loading = false;
        let _pendingLoad = null;

        const CACHE_TTL = 30000;
        const domainRef = { _name: 'people', _notify: notify };

        // =====================================================================
        // CACHE PRIMITIVES (_internal)
        // =====================================================================

        const _internal = {
            get(id) {
                return _cache?.get(id) || null;
            },

            add(person) {
                if (_cache) {
                    _cache.set(person.id, person);
                    markDirty(domainRef);
                }
            },

            remove(id) {
                if (_cache?.delete(id)) {
                    markDirty(domainRef);
                }
            },

            update(id, changes) {
                const person = _cache?.get(id);
                if (person) {
                    Object.assign(person, changes);
                    markDirty(domainRef);
                }
            },

            findByName(name) {
                if (!_cache) return null;
                const lowerName = name.toLowerCase().trim();
                for (const person of _cache.values()) {
                    if (person.name.toLowerCase() === lowerName) {
                        return person;
                    }
                }
                return null;
            },

            setName(personId, name) {
                const person = this.get(personId);
                if (!person) return;
                person.name = name;
                // Update denormalized name on faces
                const personFaces = faces._internal.getForPerson(personId);
                for (const face of personFaces) {
                    faces._internal.updateName(face.id, name);
                }
                markDirty(domainRef);
            },

            setPreferred(personId, faceId) {
                const person = this.get(personId);
                if (!person) return;
                person.preferred_face_id = faceId;
                // INVARIANT: preferred face must be locked
                faces._internal.setLocked(faceId, true);
                this.bustThumbnail(personId);
                markDirty(domainRef);
            },

            setThreshold(personId, threshold) {
                const person = this.get(personId);
                if (person) {
                    person.threshold = threshold;
                    markDirty(domainRef);
                }
            },

            bustThumbnail(personId) {
                _thumbnailBust.set(personId, Date.now());
            },

            incrementFaceCount(id) {
                const person = this.get(id);
                if (person) {
                    person.face_count = (person.face_count || 0) + 1;
                    markDirty(domainRef);
                }
            },

            decrementFaceCount(id) {
                const person = this.get(id);
                if (person) {
                    person.face_count = Math.max(0, (person.face_count || 1) - 1);
                    markDirty(domainRef);
                    return person.face_count;
                }
                return 0;
            },

            // =====================================================================
            // BATCH HELPERS
            // =====================================================================

            reconcilePerson(personId) {
                const person = this.get(personId);
                if (!person) return;

                const linkedFaces = faces._internal.getForPerson(personId);
                person.face_count = linkedFaces.length;

                if (person.face_count === 0) {
                    this.remove(personId);
                    return;
                }

                // Check if preferred face was removed
                const preferredStillExists = linkedFaces.some(f => f.id === person.preferred_face_id);
                if (!preferredStillExists) {
                    // Pick newest
                    const newest = linkedFaces.reduce((a, b) =>
                        (a.image_timestamp || 0) > (b.image_timestamp || 0) ? a : b
                    );
                    this.setPreferred(personId, newest.id);
                }
            },

            reconcileAll(personIds) {
                for (const personId of personIds) {
                    this.reconcilePerson(personId);
                }
            }
        };

        // =====================================================================
        // LOAD
        // =====================================================================

        async function load(force = false) {
            if (!force && _cache !== null && (Date.now() - _cacheTime) < CACHE_TTL) {
                return;
            }
            if (_pendingLoad) return _pendingLoad;

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    const response = await App.apiGet('/people');
                    _cache = new Map(response.data.map(p => [p.id, p]));
                    _cacheTime = Date.now();
                    broadcast({ type: 'changed' });
                } catch (err) {
                    console.error('AppState.people load error:', err);
                    broadcastError(err.message || 'Failed to load people');
                    throw err;
                } finally {
                    _loading = false;
                    _pendingLoad = null;
                }
            })();

            return _pendingLoad;
        }

        // =====================================================================
        // PERSIST FUNCTIONS
        // =====================================================================

        async function _persistRename(personId, name) {
            await App.apiPatch(`/people/${personId}`, { name });
        }

        async function _persistSetPreferred(personId, faceId) {
            await App.apiPatch(`/people/${personId}`, { preferred_face_id: faceId });
            // Also lock the face on backend
            await App.apiPatch('/faces', { face_ids: [faceId], locked: true });
        }

        async function _persistSetThreshold(personId, threshold) {
            return await App.apiPatch(`/people/${personId}`, { threshold });
        }

        async function _persistMerge(faceIds, fromId, toId) {
            // Order matters! Assign first, then delete
            await App.apiPost('/faces/assign', { face_ids: faceIds, person_id: toId });
            await App.apiDelete(`/people/${fromId}`);
        }

        async function _persistDissolve(faceIds, personId) {
            // Order matters! Unassign first, then delete
            if (faceIds.length > 0) {
                await App.apiPost('/faces/unassign', { face_ids: faceIds });
            }
            await App.apiDelete(`/people/${personId}`);
        }

        // =====================================================================
        // PUBLIC API
        // =====================================================================

        return {
            _name: 'people',
            _notify: notify,
            _internal,

            onChanged: subscribe,
            onError: subscribeError,

            load,
            invalidate() { _cacheTime = 0; },
            reload() { return load(true); },

            // --- Accessors ---
            getAll() {
                return _cache ? Array.from(_cache.values()) : [];
            },
            getById(id) {
                return _cache?.get(id) || null;
            },
            getByName(name) {
                return _internal.findByName(name);
            },
            getCount() {
                return _cache?.size || 0;
            },
            isLoaded() {
                return _cache !== null;
            },
            isLoading() {
                return _loading;
            },

            search(query) {
                if (!_cache) return [];
                const lowerQuery = query.toLowerCase().trim();
                if (!lowerQuery) {
                    return Array.from(_cache.values())
                        .sort((a, b) => (b.face_count || 0) - (a.face_count || 0) || a.name.localeCompare(b.name));
                }

                function fuzzyMatch(q, target) {
                    let qi = 0;
                    for (let ti = 0; ti < target.length && qi < q.length; ti++) {
                        if (target[ti] === q[qi]) qi++;
                    }
                    return qi === q.length;
                }

                return Array.from(_cache.values())
                    .filter(p => fuzzyMatch(lowerQuery, p.name.toLowerCase()))
                    .sort((a, b) => {
                        const aPrefix = a.name.toLowerCase().startsWith(lowerQuery);
                        const bPrefix = b.name.toLowerCase().startsWith(lowerQuery);
                        if (aPrefix !== bPrefix) return bPrefix - aPrefix;
                        const aSubstr = a.name.toLowerCase().includes(lowerQuery);
                        const bSubstr = b.name.toLowerCase().includes(lowerQuery);
                        if (aSubstr !== bSubstr) return bSubstr - aSubstr;
                        return (b.face_count || 0) - (a.face_count || 0) || a.name.localeCompare(b.name);
                    });
            },

            async fetchById(id) {
                const response = await App.apiGet(`/people/${id}`);
                const person = response?.data;
                if (_cache && person) {
                    _cache.set(person.id, person);
                }
                return person;
            },

            getThumbnailUrl(personId, size = 200) {
                const bust = _thumbnailBust.get(personId);
                const bustParam = bust ? `&_=${bust}` : '';
                return `/api/people/${personId}/thumbnail?size=${size}${bustParam}`;
            },

            bustThumbnailCache(personId) {
                _thumbnailBust.set(personId, Date.now());
            },

            // =====================================================================
            // PUBLIC MUTATIONS
            // =====================================================================

            /**
             * Rename a person. Handles merge/dissolve delegation.
             */
            rename(personId, newName) {
                const person = _internal.get(personId);
                if (!person) throw new Error('Person not found');

                const oldName = person.name;
                const trimmedNew = newName?.trim() || '';

                // Case A: No-op
                if (trimmedNew === oldName) {
                    return Promise.resolve();
                }

                // Case B: Error
                if (!oldName) {
                    throw new Error('Person has no name');
                }

                // Case C: Dissolve
                if (!trimmedNew) {
                    return this.dissolve(personId);
                }

                // Case D: Merge
                const collision = _internal.findByName(trimmedNew);
                if (collision && collision.id !== personId) {
                    return this.merge(personId, collision.id);
                }

                // Case E: Simple rename
                // Backup
                const backup = { name: oldName };

                transaction(() => {
                    _internal.setName(personId, trimmedNew);
                });

                return queueTransaction(async () => {
                    try {
                        await _persistRename(personId, trimmedNew);
                    } catch (err) {
                        transaction(() => {
                            _internal.setName(personId, backup.name);
                        });
                        broadcastError(err.message || 'Failed to rename person');
                        throw err;
                    }
                });
            },

            /**
             * Merge one person into another.
             */
            merge(fromId, toId) {
                if (fromId === toId) return Promise.resolve();

                const fromPerson = _internal.get(fromId);
                const toPerson = _internal.get(toId);
                if (!fromPerson || !toPerson) throw new Error('Person not found');

                const faceIds = faces.getForPerson(fromId).map(f => f.id);

                // Backup
                const backup = {
                    fromPerson: { ...fromPerson },
                    toPerson: { ...toPerson },
                    facePersonIds: new Map()
                };
                for (const faceId of faceIds) {
                    backup.facePersonIds.set(faceId, fromId);
                }

                transaction(() => {
                    for (const faceId of faceIds) {
                        faces._internal.linkToPerson(faceId, toId, toPerson.name);
                    }
                    toPerson.face_count += faceIds.length;
                    _internal.remove(fromId);
                });

                return queueTransaction(async () => {
                    try {
                        await _persistMerge(faceIds, fromId, toId);
                    } catch (err) {
                        transaction(() => {
                            // Restore fromPerson
                            _internal.add(backup.fromPerson);
                            // Restore face links
                            for (const faceId of faceIds) {
                                faces._internal.linkToPerson(faceId, fromId, backup.fromPerson.name);
                            }
                            // Restore toPerson count
                            Object.assign(toPerson, backup.toPerson);
                        });
                        broadcastError(err.message || 'Failed to merge people');
                        throw err;
                    }
                });
            },

            /**
             * Dissolve a person - unidentify all faces and delete.
             */
            dissolve(personId) {
                const person = _internal.get(personId);
                if (!person) throw new Error('Person not found');

                const faceIds = faces.getForPerson(personId).map(f => f.id);

                // Backup
                const backup = {
                    person: { ...person },
                    faces: new Map()
                };
                for (const faceId of faceIds) {
                    const face = faces._internal.get(faceId);
                    if (face) {
                        backup.faces.set(faceId, {
                            person_id: face.person_id,
                            person_name: face.person_name,
                            manually_tagged: face.manually_tagged
                        });
                    }
                }

                transaction(() => {
                    for (const faceId of faceIds) {
                        faces._internal.unlinkFromPerson(faceId);
                        faces._internal.setLocked(faceId, false);
                    }
                    _internal.remove(personId);
                });

                return queueTransaction(async () => {
                    try {
                        await _persistDissolve(faceIds, personId);
                    } catch (err) {
                        transaction(() => {
                            // Restore person
                            _internal.add(backup.person);
                            // Restore faces
                            for (const [fid, data] of backup.faces) {
                                faces._internal.linkToPerson(fid, data.person_id, data.person_name);
                                faces._internal.setLocked(fid, data.manually_tagged);
                            }
                        });
                        broadcastError(err.message || 'Failed to dissolve person');
                        throw err;
                    }
                });
            },

            /**
             * Set preferred face for a person.
             */
            setPreferredFace(personId, faceId) {
                const person = _internal.get(personId);
                if (!person) throw new Error('Person not found');

                const face = faces._internal.get(faceId);
                if (face?.person_id !== personId) {
                    throw new Error('Face does not belong to this person');
                }

                const backup = {
                    preferred_face_id: person.preferred_face_id,
                    face_locked: face?.manually_tagged
                };

                transaction(() => {
                    _internal.setPreferred(personId, faceId);
                });

                return queueTransaction(async () => {
                    try {
                        await _persistSetPreferred(personId, faceId);
                    } catch (err) {
                        transaction(() => {
                            person.preferred_face_id = backup.preferred_face_id;
                            if (face) {
                                face.manually_tagged = backup.face_locked;
                            }
                            _internal.bustThumbnail(personId);
                            markDirty(domainRef);
                        });
                        broadcastError(err.message || 'Failed to set preferred face');
                        throw err;
                    }
                });
            },

            /**
             * Set recognition threshold for a person.
             */
            setThreshold(personId, threshold) {
                const person = _internal.get(personId);
                if (!person) throw new Error('Person not found');

                const backup = person.threshold;

                transaction(() => {
                    _internal.setThreshold(personId, threshold);
                });

                return queueTransaction(async () => {
                    try {
                        const response = await _persistSetThreshold(personId, threshold);
                        // Handle threshold changes from backend
                        if (response?.data?.assigned || response?.data?.unassigned) {
                            faces.applyThresholdChanges(
                                personId,
                                response.data.assigned,
                                response.data.unassigned
                            );
                        }
                        return response;
                    } catch (err) {
                        transaction(() => {
                            _internal.setThreshold(personId, backup);
                        });
                        broadcastError(err.message || 'Failed to update threshold');
                        throw err;
                    }
                });
            },

            // Legacy create method
            create(name) {
                const personId = crypto.randomUUID();
                const person = {
                    id: personId,
                    name,
                    face_count: 0,
                    preferred_face_id: null,
                    threshold: null
                };

                transaction(() => {
                    _internal.add(person);
                });

                return queueTransaction(async () => {
                    try {
                        await App.apiPost('/people', { id: personId, name });
                        return person;
                    } catch (err) {
                        transaction(() => {
                            _internal.remove(personId);
                        });
                        broadcastError(err.message || 'Failed to create person');
                        throw err;
                    }
                });
            },

            // Legacy delete method
            delete(id) {
                return this.dissolve(id);
            }
        };
    })();

    // =========================================================================
    // IMAGES DOMAIN
    // =========================================================================

    const images = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        let _cache = null;
        let _cacheEpoch = null;
        let _loading = false;
        let _pendingLoad = null;

        let _displayList = [];
        let _displayListDirty = true;

        let _similarities = null;
        let _peopleNames = null;

        const domainRef = { _name: 'images', _notify: notify };

        function _markDisplayListDirty() {
            _displayListDirty = true;
        }

        function _ensureDisplayList() {
            if (!_displayListDirty) return;

            if (!_cache) {
                _displayList = [];
            } else {
                const all = Array.from(_cache.values());
                _displayList = _filterImages(_sortImages(all));
            }
            _displayListDirty = false;
        }

        function _sortImages(images) {
            const { by, direction } = view.getSort();
            const sorted = [...images];

            sorted.sort((a, b) => {
                let cmp = 0;
                if (by === 'date') {
                    cmp = new Date(a.timestamp) - new Date(b.timestamp);
                } else if (by === 'rating') {
                    cmp = (a.rating || '').localeCompare(b.rating || '');
                } else if (by === 'content') {
                    const simA = _similarities?.scores.get(a.id) || 0;
                    const simB = _similarities?.scores.get(b.id) || 0;
                    cmp = simA - simB;
                } else if (by === 'people') {
                    const namesA = _peopleNames?.[a.id] || '';
                    const namesB = _peopleNames?.[b.id] || '';
                    cmp = namesA.localeCompare(namesB, undefined, { sensitivity: 'base' });
                }
                return direction === 'asc' ? cmp : -cmp;
            });

            return sorted;
        }

        function _filterImages(images) {
            const currentFilter = filter.get();
            if (!currentFilter) return images;

            if (currentFilter.type === 'duplicates' && Array.isArray(currentFilter.imageIds)) {
                const idSet = new Set(currentFilter.imageIds.map(String));
                return images.filter(img => idSet.has(String(img.id)));
            }

            if (currentFilter.type === 'semantic' && Array.isArray(currentFilter.imageIds)) {
                const idSet = new Set(currentFilter.imageIds.map(String));
                const scores = currentFilter.scores || {};

                let filtered = images.filter(img => idSet.has(String(img.id)));

                filtered = filtered.filter(img => {
                    if (currentFilter.dateStart) {
                        const imgDate = new Date(img.timestamp);
                        if (imgDate < new Date(currentFilter.dateStart)) return false;
                    }
                    if (currentFilter.dateEnd) {
                        const imgDate = new Date(img.timestamp);
                        const endDate = new Date(currentFilter.dateEnd);
                        endDate.setHours(23, 59, 59, 999);
                        if (imgDate > endDate) return false;
                    }
                    if (currentFilter.rating) {
                        const filterEmoji = [...currentFilter.rating];
                        const hasMatch = filterEmoji.some(e => img.rating && img.rating.includes(e));
                        if (!hasMatch) return false;
                    }
                    if (currentFilter.people && currentFilter.peopleImageIds) {
                        if (!currentFilter.peopleImageIds.has(String(img.id))) return false;
                    }
                    return true;
                });

                filtered.sort((a, b) => (scores[b.id] || 0) - (scores[a.id] || 0));
                return filtered;
            }

            return images.filter(img => {
                if (currentFilter.text && !(img.description || '').toLowerCase().includes(currentFilter.text.toLowerCase())) {
                    return false;
                }
                if (currentFilter.dateStart && new Date(img.timestamp) < new Date(currentFilter.dateStart)) {
                    return false;
                }
                if (currentFilter.dateEnd) {
                    const endDate = new Date(currentFilter.dateEnd);
                    endDate.setHours(23, 59, 59, 999);
                    if (new Date(img.timestamp) > endDate) return false;
                }
                if (currentFilter.rating) {
                    const filterEmoji = [...currentFilter.rating];
                    const hasMatch = filterEmoji.some(e => img.rating && img.rating.includes(e));
                    if (!hasMatch) return false;
                }
                if (currentFilter.people && currentFilter.peopleImageIds) {
                    if (!currentFilter.peopleImageIds.has(String(img.id))) {
                        return false;
                    }
                }
                return true;
            });
        }

        // Subscribe to view and filter changes
        view.onChanged((event) => {
            if (event.property === 'sortBy' || event.property === 'sortDirection') {
                _markDisplayListDirty();
                broadcast({ type: 'changed', property: 'displayList' });
            }
        });

        filter.onChanged(() => {
            _markDisplayListDirty();
            broadcast({ type: 'changed', property: 'displayList' });
        });

        const _internal = {
            update(id, changes) {
                const image = _cache?.get(id);
                if (image) {
                    Object.assign(image, changes);
                    _markDisplayListDirty();
                    markDirty(domainRef);
                }
            },

            remove(id) {
                if (_cache?.delete(id)) {
                    _markDisplayListDirty();
                    markDirty(domainRef);
                }
            },

            get(id) {
                return _cache?.get(id) || null;
            }
        };

        function handleFaceCleanup(imageId) {
            const imageFaces = faces.getForImage(imageId);
            if (!imageFaces || imageFaces.length === 0) return;

            const personUpdates = new Map();

            for (const face of imageFaces) {
                if (face.person_id) {
                    const existing = personUpdates.get(face.person_id) || { decrement: 0, preferredFaces: [] };
                    existing.decrement++;
                    const person = people._internal.get(face.person_id);
                    if (person?.preferred_face_id === face.id) {
                        existing.wasPreferred = true;
                    }
                    personUpdates.set(face.person_id, existing);
                }
                faces._internal.remove(face.id);
            }

            for (const [personId, updates] of personUpdates) {
                for (let i = 0; i < updates.decrement; i++) {
                    const newCount = people._internal.decrementFaceCount(personId);
                    if (newCount === 0) {
                        people._internal.remove(personId);
                        break;
                    }
                }
                const person = people._internal.get(personId);
                if (person && updates.wasPreferred) {
                    const remainingFace = faces._internal.getFirstForPerson(personId, { excludingImageId: imageId });
                    if (remainingFace) {
                        people._internal.update(personId, { preferred_face_id: remainingFace.id });
                        people._internal.bustThumbnail(personId);
                        faces._internal.update(remainingFace.id, { manually_tagged: true });
                    }
                }
            }
        }

        async function load(forceFullReload = false) {
            if (_pendingLoad) return _pendingLoad;

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    if (_cache === null || forceFullReload) {
                        const response = await App.apiGet('/images');
                        const data = response.data;
                        _cache = new Map(data.images.map(img => [img.id, img]));
                        _cacheEpoch = data.epoch;
                    } else {
                        const response = await App.apiGet(`/images?since=${_cacheEpoch}`);
                        const data = response.data;
                        if (data.updated) {
                            for (const img of data.updated) {
                                _cache.set(img.id, img);
                            }
                        }
                        if (data.deleted_ids) {
                            for (const id of data.deleted_ids) {
                                _cache.delete(id);
                            }
                        }
                        _cacheEpoch = data.epoch;
                    }
                    _markDisplayListDirty();
                    broadcast({ type: 'changed' });
                } catch (err) {
                    console.error('AppState.images load error:', err);
                    broadcastError(err.message || 'Failed to load images');
                    throw err;
                } finally {
                    _loading = false;
                    _pendingLoad = null;
                }
            })();

            return _pendingLoad;
        }

        return {
            _name: 'images',
            _notify: notify,
            _internal,

            onChanged: subscribe,
            onError: subscribeError,

            load,
            reload() { return load(true); },

            getAll() {
                return _cache ? Array.from(_cache.values()) : [];
            },
            getById(id) {
                return _cache?.get(id) || null;
            },
            getCount() {
                return _cache?.size || 0;
            },
            isLoaded() {
                return _cache !== null;
            },
            isLoading() {
                return _loading;
            },

            getDisplayList() {
                _ensureDisplayList();
                return _displayList;
            },

            update(updates) {
                if (!Array.isArray(updates)) updates = [updates];

                return queueTransaction(async () => {
                    const backup = new Map();
                    for (const upd of updates) {
                        const image = _cache?.get(upd.id);
                        if (image) {
                            backup.set(upd.id, { ...image });
                            _internal.update(upd.id, upd);
                        }
                    }

                    try {
                        for (const upd of updates) {
                            const { id, ...changes } = upd;
                            await App.apiPost(`/images/${id}`, changes);
                        }
                    } catch (err) {
                        for (const [id, img] of backup) {
                            _cache.set(id, img);
                            markDirty(domainRef);
                        }
                        broadcastError(err.message || 'Failed to update images');
                        throw err;
                    }
                });
            },

            delete(ids, options = {}) {
                if (!Array.isArray(ids)) ids = [ids];
                const { deleteFiles = false } = options;

                return queueTransaction(async () => {
                    const backup = new Map();
                    for (const id of ids) {
                        const img = _cache?.get(id);
                        if (img) backup.set(id, img);
                    }

                    for (const id of ids) {
                        handleFaceCleanup(id);
                        duplicates._internal.removeImage(id);
                        _internal.remove(id);
                    }

                    try {
                        const deleteFileParam = deleteFiles ? '?delete_file=true' : '';
                        for (const id of ids) {
                            await App.apiDelete(`/images/${id}${deleteFileParam}`);
                        }
                    } catch (err) {
                        broadcastError(err.message || 'Failed to delete images');
                        faces.reload();
                        people.reload();
                        load(true);
                        throw err;
                    }
                });
            },

            rotate(ids, degrees) {
                if (!Array.isArray(ids)) ids = [ids];

                return queueTransaction(async () => {
                    try {
                        await App.apiPost('/images/rotate', { ids, degrees });
                        for (const id of ids) {
                            const image = _cache?.get(id);
                            if (image && (degrees === 90 || degrees === 270)) {
                                const temp = image.width;
                                image.width = image.height;
                                image.height = temp;
                            }
                        }
                        markDirty(domainRef);
                    } catch (err) {
                        broadcastError(err.message || 'Failed to rotate images');
                        throw err;
                    }
                });
            },

            async fetchById(id) {
                if (_cache?.has(id)) return _cache.get(id);
                const response = await App.apiGet(`/images/${id}`);
                const image = response.data;
                if (_cache && image) _cache.set(image.id, image);
                return image;
            },

            async loadSimilarities(referenceId) {
                const response = await App.apiGet(`/similar/${referenceId}`);
                _similarities = {
                    referenceId,
                    scores: new Map(response.data.results.map(r => [r.id, r.similarity]))
                };
                _markDisplayListDirty();
                broadcast({ type: 'changed', property: 'similarities' });
                return response;
            },

            getSimilarity(imageId) {
                return _similarities?.scores.get(imageId) || 0;
            },

            getSimilarityReferenceId() {
                return _similarities?.referenceId || null;
            },

            clearSimilarities() {
                _similarities = null;
                _markDisplayListDirty();
            },

            async loadPeopleNames() {
                const response = await App.apiGet('/images/people-names');
                _peopleNames = response.data;
                _markDisplayListDirty();
                broadcast({ type: 'changed', property: 'peopleNames' });
                return response.data;
            },

            getPeopleNames(imageId) {
                return _peopleNames?.[imageId] || '';
            },

            hasPeopleNames() {
                return _peopleNames !== null;
            },

            clearPeopleNames() {
                _peopleNames = null;
                _markDisplayListDirty();
            },

            async getFilteredByPeople(peopleIds) {
                const response = await App.apiGet(`/images?people=${encodeURIComponent(peopleIds.join(','))}`);
                const images = response.data.images || [];
                return new Set(images.map(img => String(img.id)));
            },

            invalidate() {
                _cache = null;
                _cacheEpoch = null;
            }
        };
    })();

    // =========================================================================
    // DUPLICATES DOMAIN
    // =========================================================================

    const duplicates = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        let _groupCache = {};
        let _statusCache = {};
        let _epochCache = {};

        let _currentLevel = 2;
        let _computing = false;
        let _pollTimer = null;
        let _pollLevel = null;

        const domainRef = { _name: 'duplicates', _notify: notify };

        const _internal = {
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
                            if (group.image_ids.length <= 1) {
                                groups.splice(i, 1);
                            }
                        }
                    }
                }
                if (changed) markDirty(domainRef);
            }
        };

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
                    console.error('Duplicates poll error:', err);
                }
            }, 2000);
        }

        function _stopPolling() {
            if (_pollTimer) {
                clearInterval(_pollTimer);
                _pollTimer = null;
                _pollLevel = null;
            }
        }

        async function loadLevel(level, force = false) {
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
                _startPollingIfNeeded(level, status);

                broadcast({ type: 'changed', level });
                return _groupCache[level];
            } catch (err) {
                console.error('AppState.duplicates load error:', err);
                broadcastError(err.message || 'Failed to load duplicates');
                throw err;
            }
        }

        return {
            _name: 'duplicates',
            _notify: notify,
            _internal,

            onChanged: subscribe,
            onError: subscribeError,

            loadLevel,
            reload(level) { return loadLevel(level ?? _currentLevel, true); },

            getGroups(level) { return _groupCache[level] || []; },
            getStatus(level) { return _statusCache[level] || null; },
            getEpoch(level) { return _epochCache[level] || 0; },
            getCurrentLevel() { return _currentLevel; },
            setCurrentLevel(level) { _currentLevel = level; },
            isComputing() { return _computing; },

            async sortSemantic(query, imageIds) {
                const response = await App.apiPost('/duplicates/sort-semantic', {
                    query,
                    image_ids: imageIds
                });
                return response.data?.scores || [];
            },

            stopPolling() { _stopPolling(); },

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

    // =========================================================================
    // SELECTION DOMAIN
    // =========================================================================

    const selection = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        const _contexts = new Map();

        function getContext(name) {
            if (!_contexts.has(name)) {
                _contexts.set(name, {
                    selected: new Set(),
                    anchor: null
                });
            }
            return _contexts.get(name);
        }

        return {
            _name: 'selection',
            _notify: notify,
            onChanged: subscribe,

            get(context) {
                return Array.from(getContext(context).selected);
            },
            getSet(context) {
                return getContext(context).selected;
            },
            getCount(context) {
                return getContext(context).selected.size;
            },
            has(context, id) {
                return getContext(context).selected.has(id);
            },
            getAnchor(context) {
                return getContext(context).anchor;
            },

            set(context, ids) {
                const ctx = getContext(context);
                ctx.selected = new Set(Array.isArray(ids) ? ids : [ids]);
                ctx.anchor = ids.length > 0 ? ids[ids.length - 1] : null;
                broadcast({ type: 'changed', context });
            },
            add(context, id) {
                const ctx = getContext(context);
                ctx.selected.add(id);
                ctx.anchor = id;
                broadcast({ type: 'changed', context });
            },
            remove(context, id) {
                const ctx = getContext(context);
                ctx.selected.delete(id);
                broadcast({ type: 'changed', context });
            },
            toggle(context, id) {
                const ctx = getContext(context);
                if (ctx.selected.has(id)) {
                    ctx.selected.delete(id);
                } else {
                    ctx.selected.add(id);
                    ctx.anchor = id;
                }
                broadcast({ type: 'changed', context });
            },
            clear(context) {
                const ctx = getContext(context);
                ctx.selected.clear();
                ctx.anchor = null;
                broadcast({ type: 'changed', context });
            },
            setAnchor(context, id) {
                getContext(context).anchor = id;
            },
            selectRange(context, items, toId) {
                const ctx = getContext(context);
                if (!ctx.anchor || !toId) return;

                const ids = items.map(item => typeof item === 'object' ? item.id : item);
                const anchorIdx = ids.indexOf(ctx.anchor);
                const toIdx = ids.indexOf(toId);

                if (anchorIdx === -1 || toIdx === -1) return;

                const start = Math.min(anchorIdx, toIdx);
                const end = Math.max(anchorIdx, toIdx);

                for (let i = start; i <= end; i++) {
                    ctx.selected.add(ids[i]);
                }
                broadcast({ type: 'changed', context });
            }
        };
    })();

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        // Domains
        view,
        nav,
        filter,
        status,
        search,
        folders,
        images,
        people,
        faces,
        duplicates,
        selection,

        // Transaction utilities (for advanced usage)
        transaction,
        queueTransaction,
        isInTransaction,
        getTransactionEpoch,

        // Utilities
        createSubscriberSystem
    };

})();
