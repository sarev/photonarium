/**
 * AppState - Centralized State Management
 * ========================================
 *
 * Single source of truth for all application state with reactive subscriptions.
 * Handles persistence abstraction (localStorage, backend, memory) transparently.
 *
 * ARCHITECTURE:
 * - Each domain (view, nav, filter, etc.) is a separate object with its own state
 * - Subscribers register via onChanged() and receive simple {type: 'changed'} events
 * - Epochs are internal for AppState ↔ Backend ↔ Database reconciliation
 * - Subscribers don't see epochs - they just react to "data changed"
 *
 * PERSISTENCE LAYERS:
 * ┌──────────────┐      ┌──────────────┐      ┌──────────────┐
 * │   AppState   │ ←──→ │   Backend    │ ←──→ │   Database   │
 * │  (frontend)  │epoch │  (Waitress)  │epoch │   (SQLite)   │
 * └──────────────┘      └──────────────┘      └──────────────┘
 *
 * USAGE:
 *   // Subscribe to changes
 *   const unsubscribe = AppState.view.onChanged(() => this.render());
 *
 *   // Read state
 *   const theme = AppState.view.getTheme();
 *
 *   // Mutate state (broadcasts to subscribers)
 *   AppState.view.setTheme('dark');
 *
 *   // Cleanup
 *   unsubscribe();
 */

'use strict';

const AppState = (function() {

    // =========================================================================
    // INTERNAL UTILITIES
    // =========================================================================

    /**
     * Create a subscriber management system for a domain.
     * @returns {Object} {subscribe, broadcast, broadcastError}
     */
    function createSubscriberSystem() {
        const subscribers = new Set();
        const errorSubscribers = new Set();

        return {
            /**
             * Subscribe to state changes.
             * @param {Function} callback - Called with {type: 'changed'} on changes
             * @returns {Function} Unsubscribe function
             */
            subscribe(callback) {
                subscribers.add(callback);
                return () => subscribers.delete(callback);
            },

            /**
             * Subscribe to errors (for error banner UI).
             * @param {Function} callback - Called with {type: 'error', message}
             * @returns {Function} Unsubscribe function
             */
            subscribeError(callback) {
                errorSubscribers.add(callback);
                return () => errorSubscribers.delete(callback);
            },

            /**
             * Broadcast state change to all subscribers.
             * @param {Object} event - Event object (default: {type: 'changed'})
             */
            broadcast(event = { type: 'changed' }) {
                for (const callback of subscribers) {
                    try {
                        callback(event);
                    } catch (err) {
                        console.error('AppState subscriber error:', err);
                    }
                }
            },

            /**
             * Broadcast error to error subscribers.
             * @param {string} message - Error message
             */
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
    // VIEW DOMAIN
    // =========================================================================
    // Theme, thumbnail size, sort settings - persisted to localStorage

    const view = (function() {
        const { subscribe, broadcast } = createSubscriberSystem();

        // State - loaded from localStorage on init
        let _theme = storage.get('theme', null);
        let _thumbnailSize = storage.get('thumbnailSize', 200);
        let _sortBy = storage.get('sortBy', 'date');
        let _sortDirection = storage.get('sortDirection', 'desc');

        // Apply system theme preference if no saved theme
        if (_theme === null) {
            _theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        return {
            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Theme ---
            getTheme() {
                return _theme;
            },
            setTheme(theme) {
                if (theme !== 'light' && theme !== 'dark') {
                    console.warn('AppState.view.setTheme: invalid theme', theme);
                    return;
                }
                if (_theme === theme) return;
                _theme = theme;
                storage.set('theme', theme);
                broadcast({ type: 'changed', property: 'theme' });
            },

            // --- Thumbnail Size ---
            getThumbnailSize() {
                return _thumbnailSize;
            },
            setThumbnailSize(size) {
                size = Math.max(100, Math.min(400, Number(size) || 200));
                if (_thumbnailSize === size) return;
                _thumbnailSize = size;
                storage.set('thumbnailSize', size);
                broadcast({ type: 'changed', property: 'thumbnailSize' });
            },

            // --- Sort ---
            getSort() {
                return { by: _sortBy, direction: _sortDirection };
            },
            getSortBy() {
                return _sortBy;
            },
            setSortBy(by) {
                const valid = ['date', 'rating', 'content', 'people'];
                if (!valid.includes(by)) {
                    console.warn('AppState.view.setSortBy: invalid sort field', by);
                    return;
                }
                if (_sortBy === by) return;
                _sortBy = by;
                storage.set('sortBy', by);
                broadcast({ type: 'changed', property: 'sortBy' });
            },
            getSortDirection() {
                return _sortDirection;
            },
            setSortDirection(direction) {
                if (direction !== 'asc' && direction !== 'desc') {
                    console.warn('AppState.view.setSortDirection: invalid direction', direction);
                    return;
                }
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
    // NAV DOMAIN
    // =========================================================================
    // Current screen, navigation history, fullscreen tracking - memory only

    const nav = (function() {
        const { subscribe, broadcast } = createSubscriberSystem();

        // State
        let _screen = 'gallery';
        let _previousScreen = null;
        let _history = [];
        let _fullscreenSourceScreen = 'gallery';
        let _fullscreenImageId = null;
        let _scrollPositions = {}; // screen → scrollTop

        return {
            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Screen ---
            getScreen() {
                return _screen;
            },
            getPreviousScreen() {
                return _previousScreen;
            },
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

            // --- History ---
            canGoBack() {
                return _history.length > 0;
            },
            goBack() {
                if (_history.length === 0) return false;
                const previous = _history.pop();
                _previousScreen = _screen;
                _screen = previous;
                broadcast({ type: 'changed', property: 'screen' });
                return true;
            },
            clearHistory() {
                _history = [];
            },

            // --- Fullscreen ---
            getFullscreenSourceScreen() {
                return _fullscreenSourceScreen;
            },
            setFullscreenSourceScreen(screen) {
                _fullscreenSourceScreen = screen;
            },
            getFullscreenImageId() {
                return _fullscreenImageId;
            },
            setFullscreenImageId(imageId) {
                if (_fullscreenImageId === imageId) return;
                _fullscreenImageId = imageId;
                broadcast({ type: 'changed', property: 'fullscreenImageId' });
            },

            // --- Scroll Positions ---
            getScrollPosition(screen) {
                return _scrollPositions[screen] || 0;
            },
            setScrollPosition(screen, position) {
                _scrollPositions[screen] = position;
            },
            clearScrollPositions() {
                _scrollPositions = {};
            }
        };
    })();

    // =========================================================================
    // FILTER DOMAIN
    // =========================================================================
    // Search/filter criteria - memory only

    const filter = (function() {
        const { subscribe, broadcast } = createSubscriberSystem();

        // State
        let _filter = null; // {text, dateStart, dateEnd, rating, people, type, threshold, imageIds, scores}

        return {
            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Filter ---
            get() {
                return _filter;
            },
            set(newFilter, options = {}) {
                const { silent = false } = options;
                _filter = newFilter;
                if (!silent) {
                    broadcast({ type: 'changed' });
                }
            },
            clear() {
                if (_filter === null) return;
                _filter = null;
                broadcast({ type: 'changed' });
            },
            isActive() {
                return _filter !== null;
            },

            // --- Convenience accessors ---
            getText() {
                return _filter?.text || null;
            },
            getDateRange() {
                if (!_filter) return null;
                return { start: _filter.dateStart, end: _filter.dateEnd };
            },
            getRating() {
                return _filter?.rating || null;
            },
            getPeople() {
                return _filter?.people || null;
            }
        };
    })();

    // =========================================================================
    // FOLDERS DOMAIN
    // =========================================================================
    // Folder list, scan/indexing status - persisted to backend
    //
    // Note: Folder operations are infrequent, so we use simple request/response
    // rather than optimistic updates with epoch reconciliation. The backend
    // doesn't support epochs for folder operations anyway.

    const folders = (function() {
        const { subscribe, subscribeError, broadcast, broadcastError } = createSubscriberSystem();

        // State
        let _folders = [];      // [{path, count}, ...]
        let _status = null;     // {status, indexing_queue, embedding_queue, face_queue, total_images, ...}
        let _loading = false;

        /**
         * Load folders from backend.
         */
        async function load() {
            if (_loading) return;
            _loading = true;
            try {
                const foldersResponse = await App.apiGet('/folders');
                _folders = foldersResponse || [];
                broadcast({ type: 'changed' });
            } catch (err) {
                console.error('AppState.folders load error:', err);
                broadcastError(err.message || 'Failed to load folders');
            } finally {
                _loading = false;
            }
        }

        return {
            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,

            // --- Folders ---
            getAll() {
                return _folders;
            },
            async add(path) {
                try {
                    const response = await App.apiPost('/folders', { path });
                    // Add to local list
                    if (response?.data) {
                        _folders = [..._folders, response.data];
                        broadcast({ type: 'changed' });
                    }
                    return response?.data;
                } catch (err) {
                    broadcastError(err.message || 'Failed to add folder');
                    throw err;
                }
            },
            async remove(path) {
                try {
                    await App.apiDelete(`/folders/${encodeURIComponent(path)}`);
                    // Remove from local list
                    _folders = _folders.filter(f => f.path !== path);
                    broadcast({ type: 'changed' });
                } catch (err) {
                    broadcastError(err.message || 'Failed to remove folder');
                    throw err;
                }
            },
            async rescan() {
                try {
                    await App.apiPost('/rescan');
                    broadcast({ type: 'rescanStarted' });
                } catch (err) {
                    broadcastError(err.message || 'Failed to start rescan');
                    throw err;
                }
            },

            // --- Status ---
            getStatus() {
                return _status;
            },
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
            isUpdating() {
                return _status?.status === 'updating';
            },
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
    // IMAGES DOMAIN
    // =========================================================================
    // Image metadata cache with delta sync - persisted to backend

    const images = (function() {
        const { subscribe, subscribeError, broadcast, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;          // Map<imageId, image>
        let _cacheEpoch = null;     // Backend epoch for delta updates
        let _loading = false;
        let _pendingLoad = null;    // Prevent concurrent loads

        /**
         * Load all images (full or delta based on cache state).
         */
        async function load(forceFullReload = false) {
            // Prevent concurrent loads - return existing promise if loading
            if (_pendingLoad) {
                return _pendingLoad;
            }

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    if (_cache === null || forceFullReload) {
                        // Full load
                        const response = await App.apiGet('/images');
                        _cache = new Map(response.images.map(img => [img.id, img]));
                        _cacheEpoch = response.epoch;
                    } else {
                        // Delta load
                        const response = await App.apiGet(`/images?since=${_cacheEpoch}`);
                        if (response.updated) {
                            for (const img of response.updated) {
                                _cache.set(img.id, img);
                            }
                        }
                        if (response.deleted_ids) {
                            for (const id of response.deleted_ids) {
                                _cache.delete(id);
                            }
                        }
                        _cacheEpoch = response.epoch;
                    }
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
            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            reload() {
                return load(true);
            },

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

            // --- Mutations (optimistic) ---
            async update(id, updates) {
                const image = _cache?.get(id);
                if (!image) return;

                // Optimistic update
                const updated = { ...image, ...updates };
                _cache.set(id, updated);
                broadcast({ type: 'changed', ids: [id] });

                // Persist
                try {
                    await App.apiPost(`/images/${id}`, updates);
                } catch (err) {
                    // Rollback
                    _cache.set(id, image);
                    broadcast({ type: 'changed', ids: [id] });
                    broadcastError(err.message || 'Failed to update image');
                }
            },

            async delete(ids) {
                if (!Array.isArray(ids)) ids = [ids];

                // Optimistic update
                const backup = new Map();
                for (const id of ids) {
                    const img = _cache?.get(id);
                    if (img) {
                        backup.set(id, img);
                        _cache.delete(id);
                    }
                }
                broadcast({ type: 'changed', ids });

                // Persist
                try {
                    for (const id of ids) {
                        await App.apiDelete(`/images/${id}`);
                    }
                } catch (err) {
                    // Rollback
                    for (const [id, img] of backup) {
                        _cache.set(id, img);
                    }
                    broadcast({ type: 'changed', ids });
                    broadcastError(err.message || 'Failed to delete images');
                }
            },

            async rotate(ids, degrees) {
                if (!Array.isArray(ids)) ids = [ids];

                try {
                    await App.apiPost('/images/rotate', { ids, degrees });
                    broadcast({ type: 'rotated', ids });
                } catch (err) {
                    broadcastError(err.message || 'Failed to rotate images');
                }
            },

            // --- Cache invalidation ---
            invalidate() {
                _cache = null;
                _cacheEpoch = null;
            }
        };
    })();

    // =========================================================================
    // PEOPLE DOMAIN
    // =========================================================================
    // People with face counts, cache-busted thumbnail URLs - persisted to backend

    const people = (function() {
        const { subscribe, subscribeError, broadcast, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;              // Map<personId, {id, name, face_count, threshold, preferred_face_id}>
        let _cacheTime = 0;             // Last fetch timestamp
        let _thumbnailBust = new Map(); // personId → timestamp for cache busting
        let _loading = false;
        let _pendingLoad = null;

        const CACHE_TTL = 30000;        // 30 seconds

        /**
         * Load people list from backend.
         */
        async function load(force = false) {
            // Return cached if fresh
            if (!force && _cache !== null && (Date.now() - _cacheTime) < CACHE_TTL) {
                return;
            }

            // Prevent concurrent loads
            if (_pendingLoad) {
                return _pendingLoad;
            }

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    const response = await App.apiGet('/people');
                    _cache = new Map(response.map(p => [p.id, p]));
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

        /**
         * Invalidate cache, forcing reload on next access.
         */
        function invalidate() {
            _cacheTime = 0;
        }

        return {
            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            invalidate,
            reload() {
                return load(true);
            },

            // --- Accessors ---
            getAll() {
                return _cache ? Array.from(_cache.values()) : [];
            },
            getById(id) {
                return _cache?.get(id) || null;
            },
            getByName(name) {
                if (!_cache) return null;
                const lowerName = name.toLowerCase();
                for (const person of _cache.values()) {
                    if (person.name.toLowerCase() === lowerName) {
                        return person;
                    }
                }
                return null;
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

            /**
             * Get thumbnail URL with cache busting.
             */
            getThumbnailUrl(personId, size = 200) {
                const bust = _thumbnailBust.get(personId);
                const bustParam = bust ? `&_=${bust}` : '';
                return `/api/people/${personId}/thumbnail?size=${size}${bustParam}`;
            },

            /**
             * Bust thumbnail cache for a person (e.g., after preferred face change).
             */
            bustThumbnailCache(personId) {
                _thumbnailBust.set(personId, Date.now());
            },

            // --- Mutations ---
            async create(name) {
                try {
                    const response = await App.apiPost('/people', { name });
                    const person = response;

                    // Update cache
                    if (_cache) {
                        _cache.set(person.id, person);
                    }
                    broadcast({ type: 'changed', ids: [person.id] });

                    return person;
                } catch (err) {
                    broadcastError(err.message || 'Failed to create person');
                    throw err;
                }
            },

            async rename(id, name) {
                const person = _cache?.get(id);
                if (!person) return;

                // Optimistic update
                const oldName = person.name;
                person.name = name;
                broadcast({ type: 'changed', ids: [id] });

                try {
                    await App.apiPatch(`/people/${id}`, { name });
                } catch (err) {
                    // Rollback
                    person.name = oldName;
                    broadcast({ type: 'changed', ids: [id] });
                    broadcastError(err.message || 'Failed to rename person');
                    throw err;
                }
            },

            async delete(id) {
                const person = _cache?.get(id);
                if (!person && _cache !== null) return;

                // Optimistic update
                if (_cache) {
                    _cache.delete(id);
                }
                broadcast({ type: 'changed', ids: [id] });

                try {
                    await App.apiDelete(`/people/${id}`);
                } catch (err) {
                    // Rollback
                    if (person && _cache) {
                        _cache.set(id, person);
                    }
                    broadcast({ type: 'changed', ids: [id] });
                    broadcastError(err.message || 'Failed to delete person');
                    throw err;
                }
            },

            async setPreferredFace(personId, faceId) {
                try {
                    await App.apiPost(`/people/${personId}/set-preferred`, { face_id: faceId });

                    // Update cache
                    const person = _cache?.get(personId);
                    if (person) {
                        person.preferred_face_id = faceId;
                    }

                    // Bust thumbnail cache
                    _thumbnailBust.set(personId, Date.now());

                    broadcast({ type: 'changed', ids: [personId] });
                } catch (err) {
                    broadcastError(err.message || 'Failed to set preferred face');
                    throw err;
                }
            },

            async setThreshold(personId, threshold) {
                const person = _cache?.get(personId);
                if (!person) return;

                const oldThreshold = person.threshold;

                // Optimistic update
                person.threshold = threshold;
                broadcast({ type: 'changed', ids: [personId] });

                try {
                    await App.apiPatch(`/people/${personId}`, { threshold });
                } catch (err) {
                    // Rollback
                    person.threshold = oldThreshold;
                    broadcast({ type: 'changed', ids: [personId] });
                    broadcastError(err.message || 'Failed to update threshold');
                    throw err;
                }
            }
        };
    })();

    // =========================================================================
    // FACES DOMAIN
    // =========================================================================
    // All faces with derived views - persisted to backend

    const faces = (function() {
        const { subscribe, subscribeError, broadcast, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;              // Map<faceId, face>
        let _loading = false;
        let _pendingLoad = null;

        // Derived view caches (invalidated on change)
        let _unknownFaces = null;       // Faces with no person_id
        let _facesByPerson = null;      // Map<personId, face[]>
        let _facesByImage = null;       // Map<imageId, face[]>

        /**
         * Invalidate derived view caches.
         */
        function invalidateDerived() {
            _unknownFaces = null;
            _facesByPerson = null;
            _facesByImage = null;
        }

        /**
         * Load all faces from backend.
         */
        async function load(force = false) {
            if (!force && _cache !== null) {
                return;
            }

            if (_pendingLoad) {
                return _pendingLoad;
            }

            _loading = true;
            _pendingLoad = (async () => {
                try {
                    const response = await App.apiGet('/faces');
                    _cache = new Map(response.map(f => [f.id, f]));
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

        return {
            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            reload() {
                return load(true);
            },

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

            /**
             * Get unknown faces (no person_id, not suppressed).
             * Cached for hot-path performance.
             */
            getUnknown() {
                if (!_cache) return [];
                if (_unknownFaces === null) {
                    _unknownFaces = Array.from(_cache.values())
                        .filter(f => !f.person_id && !f.suppressed);
                }
                return _unknownFaces;
            },

            /**
             * Get faces for a specific person.
             * Uses cached lookup table.
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
             * Get faces for a specific image.
             * Uses cached lookup table.
             */
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

            // --- Mutations ---

            /**
             * Identify faces - assign to a person.
             */
            async identify(faceIds, personName, options = {}) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                const { preferredFaceId = null, existingPersonId = null } = options;

                // Determine person (create new or use existing)
                let personId = existingPersonId;
                if (!personId) {
                    const existingPerson = people.getByName(personName);
                    if (existingPerson) {
                        personId = existingPerson.id;
                    }
                }

                try {
                    // Use batch identify API (note: endpoint is identify-batch, not batch-identify)
                    const response = await App.apiPost('/faces/identify-batch', {
                        face_ids: faceIds,
                        name: personName,
                        person_id: personId,
                        preferred_face_id: preferredFaceId
                    });

                    // Update local cache
                    const newPersonId = response.person_id;
                    for (const faceId of faceIds) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            face.person_id = newPersonId;
                            face.person_name = personName;
                        }
                    }

                    invalidateDerived();

                    // Also invalidate people cache (face counts changed)
                    people.invalidate();

                    broadcast({ type: 'changed', ids: faceIds });

                    return { personId: newPersonId };
                } catch (err) {
                    broadcastError(err.message || 'Failed to identify faces');
                    throw err;
                }
            },

            /**
             * Unassign faces - return to unknown pool.
             */
            async unassign(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];

                // Backup for rollback
                const backup = new Map();
                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.set(faceId, { person_id: face.person_id, person_name: face.person_name });
                        face.person_id = null;
                        face.person_name = null;
                    }
                }

                invalidateDerived();
                broadcast({ type: 'changed', ids: faceIds });

                try {
                    for (const faceId of faceIds) {
                        await App.apiPost(`/faces/${faceId}/unidentify`);
                    }

                    // Invalidate people cache (face counts changed)
                    people.invalidate();
                } catch (err) {
                    // Rollback
                    for (const [faceId, data] of backup) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            face.person_id = data.person_id;
                            face.person_name = data.person_name;
                        }
                    }
                    invalidateDerived();
                    broadcast({ type: 'changed', ids: faceIds });
                    broadcastError(err.message || 'Failed to unassign faces');
                    throw err;
                }
            },

            /**
             * Suppress faces - mark as false positives.
             */
            async suppress(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];

                // Backup for rollback
                const backup = new Map();
                for (const faceId of faceIds) {
                    const face = _cache?.get(faceId);
                    if (face) {
                        backup.set(faceId, { suppressed: face.suppressed });
                        face.suppressed = true;
                    }
                }

                invalidateDerived();
                broadcast({ type: 'changed', ids: faceIds });

                try {
                    for (const faceId of faceIds) {
                        await App.apiPost(`/faces/${faceId}/suppress`);
                    }
                } catch (err) {
                    // Rollback
                    for (const [faceId, data] of backup) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            face.suppressed = data.suppressed;
                        }
                    }
                    invalidateDerived();
                    broadcast({ type: 'changed', ids: faceIds });
                    broadcastError(err.message || 'Failed to suppress faces');
                    throw err;
                }
            },

            /**
             * Search faces by semantic query.
             * Uses the /faces endpoint with search parameter (not a separate endpoint).
             */
            async search(query) {
                try {
                    const response = await App.apiGet(`/faces?search=${encodeURIComponent(query)}`);
                    return response; // Returns unknown faces sorted by similarity
                } catch (err) {
                    broadcastError(err.message || 'Failed to search faces');
                    throw err;
                }
            },

            // --- Cache management ---
            invalidate() {
                _cache = null;
                invalidateDerived();
            }
        };
    })();

    // =========================================================================
    // DUPLICATES DOMAIN
    // =========================================================================
    // Duplicate groups by similarity level - persisted to backend

    const duplicates = (function() {
        const { subscribe, subscribeError, broadcast, broadcastError } = createSubscriberSystem();

        // Per-level caching
        let _groupCache = {};           // level → groups array
        let _statusCache = {};          // level → status object
        let _epochCache = {};           // level → epoch timestamp

        let _currentLevel = 2;          // Default level (Similar)
        let _computing = false;
        let _pollTimer = null;

        /**
         * Load duplicate groups for a level.
         */
        async function loadLevel(level, force = false) {
            // Return cached if available and not forced
            if (!force && _groupCache[level] !== undefined) {
                return _groupCache[level];
            }

            try {
                const response = await App.apiGet(`/duplicates?level=${level}`);
                _groupCache[level] = response.groups || [];
                _statusCache[level] = {
                    status: response.status,
                    progress: response.progress,
                    total: response.total
                };
                _epochCache[level] = Date.now();

                // Update computing flag
                _computing = response.status === 'computing';

                broadcast({ type: 'changed', level });

                return _groupCache[level];
            } catch (err) {
                console.error('AppState.duplicates load error:', err);
                broadcastError(err.message || 'Failed to load duplicates');
                throw err;
            }
        }

        /**
         * Poll status during computation.
         */
        function startPolling(level) {
            if (_pollTimer) return;

            _pollTimer = setInterval(async () => {
                try {
                    const response = await App.apiGet(`/duplicates?level=${level}`);
                    _statusCache[level] = {
                        status: response.status,
                        progress: response.progress,
                        total: response.total
                    };

                    if (response.status !== 'computing') {
                        // Computation complete
                        stopPolling();
                        _computing = false;
                        _groupCache[level] = response.groups || [];
                        _epochCache[level] = Date.now();
                        broadcast({ type: 'computationComplete', level });
                    } else {
                        broadcast({ type: 'progress', level });
                    }
                } catch (err) {
                    console.error('Duplicates poll error:', err);
                }
            }, 2000);
        }

        function stopPolling() {
            if (_pollTimer) {
                clearInterval(_pollTimer);
                _pollTimer = null;
            }
        }

        return {
            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            loadLevel,
            reload(level) {
                return loadLevel(level ?? _currentLevel, true);
            },

            // --- Accessors ---
            getGroups(level) {
                return _groupCache[level] || [];
            },
            getStatus(level) {
                return _statusCache[level] || null;
            },
            getEpoch(level) {
                return _epochCache[level] || 0;
            },
            getCurrentLevel() {
                return _currentLevel;
            },
            setCurrentLevel(level) {
                _currentLevel = level;
            },
            isComputing() {
                return _computing;
            },

            // --- Actions ---
            // Note: Duplicates are computed automatically during image scanning.
            // There's no manual recompute endpoint. To recompute duplicates,
            // trigger a rescan via AppState.folders.rescan().
            //
            // If manual recomputation is needed in the future, add:
            // POST /api/duplicates/recompute?level={level}

            // --- Lifecycle ---
            startPolling,
            stopPolling,

            // --- Cache management ---
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
    // Per-context selection state - memory only

    const selection = (function() {
        const { subscribe, broadcast } = createSubscriberSystem();

        // Per-context selection storage
        // Keys: 'gallery', 'duplicates', 'faces', 'faces-pick'
        const _contexts = new Map();

        /**
         * Get or create context state.
         */
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
            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Accessors ---
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

            // --- Mutations ---
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
                if (ctx.selected.size === 0) return;
                ctx.selected.clear();
                ctx.anchor = null;
                broadcast({ type: 'changed', context });
            },
            selectAll(context, ids) {
                const ctx = getContext(context);
                ctx.selected = new Set(ids);
                broadcast({ type: 'changed', context });
            },
            setAnchor(context, id) {
                getContext(context).anchor = id;
            },

            /**
             * Add a range from anchor to target (for shift+click).
             */
            addRange(context, ids, targetId) {
                const ctx = getContext(context);
                const anchor = ctx.anchor;
                if (!anchor) {
                    ctx.selected.add(targetId);
                    ctx.anchor = targetId;
                    broadcast({ type: 'changed', context });
                    return;
                }

                const anchorIdx = ids.indexOf(anchor);
                const targetIdx = ids.indexOf(targetId);
                if (anchorIdx === -1 || targetIdx === -1) return;

                const start = Math.min(anchorIdx, targetIdx);
                const end = Math.max(anchorIdx, targetIdx);

                for (let i = start; i <= end; i++) {
                    ctx.selected.add(ids[i]);
                }
                broadcast({ type: 'changed', context });
            },

            // --- Context management ---
            clearContext(context) {
                _contexts.delete(context);
            },
            clearAll() {
                _contexts.clear();
                broadcast({ type: 'changed', context: '*' });
            }
        };
    })();

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        view,
        nav,
        filter,
        folders,
        images,
        people,
        faces,
        duplicates,
        selection
    };
})();

// Export globally
window.AppState = AppState;
