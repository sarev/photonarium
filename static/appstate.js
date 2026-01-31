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
     * @returns {Object} {subscribe, subscribeError, broadcast, notify, broadcastError}
     */
    function createSubscriberSystem() {
        const subscribers = new Set();
        const errorSubscribers = new Set();

        /**
         * Notify all subscribers of a state change.
         * This is the core notification function used by both broadcast() and the transaction system.
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
             * Alias for notify() - used by existing code.
             * @param {Object} event - Event object (default: {type: 'changed'})
             */
            broadcast: notify,

            /**
             * Notify function reference - for transaction system.
             * Domains store this as _notify for use by markDirty/flushDirty.
             */
            notify,

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
    // TRANSACTION SYSTEM
    // =========================================================================
    // Batches state changes and notifications within a single transaction.
    // External API methods will wrap operations in transactions.
    // Internal methods can mark domains dirty without triggering immediate notifications.
    //
    // Phase 1: Infrastructure only - existing code continues to use broadcast() directly.
    // Future phases will migrate external methods to use queueTransaction().

    let _txEpoch = 0;                           // Global transaction epoch counter
    let _inTransaction = false;                  // Are we inside a transaction?
    let _dirtyDomains = new Set();              // Domains that need notification
    let _transactionQueue = Promise.resolve();  // Sequential execution queue

    /**
     * Mark a domain as needing notification.
     * Called by internal API when state changes.
     * @param {Object} domain - Domain object with _notify method
     */
    function markDirty(domain) {
        if (_inTransaction) {
            _dirtyDomains.add(domain);
        } else {
            // Called outside transaction - warn in dev, notify immediately
            console.warn('AppState: State mutation outside transaction:', domain._name);
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Flush notifications for all dirty domains.
     * Called at end of transaction.
     */
    function flushDirty() {
        const domains = Array.from(_dirtyDomains);
        _dirtyDomains.clear();

        console.log('[AppState.flushDirty] Flushing', domains.length, 'dirty domains:', domains.map(d => d._name));

        // Notify synchronously - keeps GUI single-threaded and predictable
        for (const domain of domains) {
            console.log('[AppState.flushDirty] Notifying domain:', domain._name, 'epoch:', _txEpoch);
            domain._notify({ type: 'changed', epoch: _txEpoch });
        }
    }

    /**
     * Run a function within a transaction.
     * - Tracks dirty domains
     * - Batches notifications at end
     * - Handles sync and async functions
     * - Nested transactions are flattened (inner marks dirty, outer flushes)
     * @param {Function} fn - Function to run in transaction
     * @returns {*} Result of fn
     */
    function transaction(fn) {
        // If already in a transaction, just run (nested)
        if (_inTransaction) {
            console.log('[AppState.transaction] Nested call, epoch unchanged:', _txEpoch);
            return fn();
        }

        _inTransaction = true;
        _txEpoch++;
        _dirtyDomains.clear();
        console.log('[AppState.transaction] START epoch:', _txEpoch);

        try {
            const result = fn();

            // Handle async
            if (result && typeof result.then === 'function') {
                return result
                    .then(value => {
                        console.log('[AppState.transaction] END (async) epoch:', _txEpoch);
                        flushDirty();
                        _inTransaction = false;
                        return value;
                    })
                    .catch(err => {
                        console.log('[AppState.transaction] END (async error) epoch:', _txEpoch, err.message);
                        flushDirty(); // Still notify on error - state may have partially changed
                        _inTransaction = false;
                        throw err;
                    });
            }

            // Sync
            console.log('[AppState.transaction] END (sync) epoch:', _txEpoch);
            flushDirty();
            _inTransaction = false;
            return result;

        } catch (err) {
            console.log('[AppState.transaction] END (sync error) epoch:', _txEpoch, err.message);
            flushDirty();
            _inTransaction = false;
            throw err;
        }
    }

    /**
     * Queue a transaction to run after any pending transactions complete.
     * Ensures sequential execution of async operations.
     * @param {Function} fn - Function to run in transaction
     * @returns {Promise} Promise that resolves when transaction completes
     */
    function queueTransaction(fn) {
        _transactionQueue = _transactionQueue
            .then(() => transaction(fn))
            .catch(err => {
                console.error('AppState: Transaction failed:', err);
                throw err;
            });
        return _transactionQueue;
    }

    /**
     * Check if currently inside a transaction.
     * @returns {boolean}
     */
    function isInTransaction() {
        return _inTransaction;
    }

    /**
     * Get current transaction epoch.
     * @returns {number}
     */
    function getTransactionEpoch() {
        return _txEpoch;
    }

    // Debug helper - expose transaction state to console
    window._appStateDebug = {
        getEpoch: () => _txEpoch,
        isInTransaction: () => _inTransaction,
        getDirtyDomains: () => Array.from(_dirtyDomains).map(d => d._name),
    };

    // =========================================================================
    // VIEW DOMAIN
    // =========================================================================
    // Theme, thumbnail size, sort settings - persisted to localStorage

    const view = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        // State - loaded from localStorage on init
        let _theme = storage.get('theme', null);
        let _thumbnailSize = storage.get('thumbnailSize', 200);
        let _sortBy = storage.get('sortBy', 'date');
        let _sortDirection = storage.get('sortDirection', 'desc');

        // Apply system theme preference if no saved theme
        if (_theme === null) {
            _theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
        }

        /**
         * Apply theme to DOM. Called on init and when theme changes.
         */
        function applyThemeToDOM(theme) {
            const app = document.getElementById('app');
            if (app) {
                app.dataset.theme = theme;
            }
        }

        return {
            // --- Transaction system metadata ---
            _name: 'view',
            _notify: notify,

            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Initialization ---
            /**
             * Apply initial state to DOM. Call once after DOM is ready.
             */
            init() {
                applyThemeToDOM(_theme);
            },

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
                applyThemeToDOM(theme);
                broadcast({ type: 'changed', property: 'theme' });
            },
            toggleTheme() {
                this.setTheme(_theme === 'light' ? 'dark' : 'light');
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
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        // State
        // NOTE: _screen starts as null so the first navigateTo() actually runs
        // (if it defaulted to 'gallery', navigateTo('gallery') would return early)
        let _screen = null;
        let _previousScreen = null;
        let _history = [];
        let _fullscreenImageId = null;
        let _scrollPositions = {}; // screen → scrollTop

        return {
            // --- Transaction system metadata ---
            _name: 'nav',
            _notify: notify,

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

            // --- Fullscreen Image ID ---
            // Tracks which image is displayed in fullscreen overlay
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
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        // State
        let _filter = null; // {text, dateStart, dateEnd, rating, people, type, threshold, imageIds, scores}

        return {
            // --- Transaction system metadata ---
            _name: 'filter',
            _notify: notify,

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
    // STATUS DOMAIN
    // =========================================================================
    // Global processing status with polling - memory only
    //
    // Consolidates all /status API calls from database.js, gallery.js, search.js, faces.js.
    // Provides polling with configurable interval and automatic stop when status is up_to_date.

    const status = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        // State
        let _status = null;     // {status, indexing_queue, embedding_queue, face_queue, total_images, face_detection_enabled, ...}
        let _prevStatus = null; // Previous status for detecting transitions
        let _pollTimer = null;
        let _loading = false;

        return {
            // --- Transaction system metadata ---
            _name: 'status',
            _notify: notify,

            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Load ---
            async load() {
                if (_loading) return _status;
                _loading = true;
                try {
                    _prevStatus = _status;
                    _status = (await App.apiGet('/status')).data;

                    // Check for face reassessment completion transition
                    // When completed transitions from false to true, reload faces and ack
                    const wasCompleted = _prevStatus?.face_reassessment?.completed;
                    const isCompleted = _status?.face_reassessment?.completed;
                    if (isCompleted && !wasCompleted) {
                        // Acknowledge to clear the completed flag on backend
                        App.apiPost('/faces/reassess-ack').catch(err => {
                            console.warn('Failed to ack reassessment:', err);
                        });
                        // Force reload faces to pick up newly matched faces
                        // Use setTimeout to avoid blocking the status update
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

            // --- Accessors ---
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

            // --- Polling ---
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

            isPolling() {
                return _pollTimer !== null;
            }
        };
    })();

    // =========================================================================
    // SEARCH DOMAIN
    // =========================================================================
    // Semantic search - memory only
    //
    // Encapsulates semantic search (currently in gallery.js, search.js).

    const search = (function() {
        const { subscribe, broadcast, notify } = createSubscriberSystem();

        // State
        let _results = null;    // {results: [{id, score}, ...]}
        let _loading = false;
        let _query = null;
        let _threshold = null;

        return {
            // --- Transaction system metadata ---
            _name: 'search',
            _notify: notify,

            // --- Subscriptions ---
            onChanged: subscribe,

            // --- Execute ---
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

            // --- Accessors ---
            getResults() { return _results; },
            getQuery() { return _query; },
            getThreshold() { return _threshold; },
            isLoading() { return _loading; },

            // --- Clear ---
            clear() {
                _results = null;
                _query = null;
                _threshold = null;
                broadcast({ type: 'changed' });
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
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // State
        let _folders = [];      // [{path, count}, ...]
        let _status = null;     // {status, indexing_queue, embedding_queue, face_queue, total_images, ...}
        let _stats = null;      // {totalImages, totalFolders}
        let _loading = false;

        /**
         * Load folders from backend.
         */
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
            // --- Transaction system metadata ---
            _name: 'folders',
            _notify: notify,

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
                    // Backend returns {success: false, error} on failure
                    if (response && response.success === false) {
                        throw new Error(response.error || 'Failed to add folder');
                    }
                    // Reload folder list to get accurate data from backend
                    await load();
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
                    const response = await App.apiPost('/rescan');
                    // Backend returns {success: false, error} on failure
                    if (response && response.success === false) {
                        throw new Error(response.error || 'Failed to start rescan');
                    }
                    broadcast({ type: 'rescanStarted' });
                } catch (err) {
                    broadcastError(err.message || 'Failed to start rescan');
                    throw err;
                }
            },

            // --- Stats ---
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
            getStats() {
                return _stats;
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
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;          // Map<imageId, image>
        let _cacheEpoch = null;     // Backend epoch for delta updates
        let _loading = false;
        let _pendingLoad = null;    // Prevent concurrent loads

        // Display list state (sorted/filtered view of images)
        let _displayList = [];      // The actual state - read directly by GUI
        let _displayListDirty = true; // Lazy recomputation flag

        // Sort data (loaded on demand for content/people sorts)
        let _similarities = null;   // {referenceId, scores: Map<id, similarity>}
        let _peopleNames = null;    // {imageId: 'name1, name2'}

        // Domain reference for transaction system
        const domainRef = { _name: 'images', _notify: notify };

        /**
         * Mark display list as needing recomputation.
         * Called when images, sort settings, or filter changes.
         */
        function _markDisplayListDirty() {
            _displayListDirty = true;
        }

        /**
         * Recompute display list if dirty. Called lazily on access.
         */
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

        /**
         * Sort images based on current view settings.
         */
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

        /**
         * Filter images based on current filter settings.
         */
        function _filterImages(images) {
            const currentFilter = filter.get();
            if (!currentFilter) return images;

            // Duplicates filter
            if (currentFilter.type === 'duplicates' && Array.isArray(currentFilter.imageIds)) {
                const idSet = new Set(currentFilter.imageIds.map(String));
                return images.filter(img => idSet.has(String(img.id)));
            }

            // Semantic search filter
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
                    return true;
                });

                filtered.sort((a, b) => (scores[b.id] || 0) - (scores[a.id] || 0));
                return filtered;
            }

            // Standard filters
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

        // Subscribe to view and filter changes to invalidate display list
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

        // =====================================================================
        // INTERNAL API
        // =====================================================================
        // Used by other domains within transactions.

        const _internal = {
            /**
             * Update an image in the cache.
             * @param {string} id - Image ID
             * @param {Object} changes - Properties to merge
             */
            update(id, changes) {
                const image = _cache?.get(id);
                if (image) {
                    Object.assign(image, changes);
                    _markDisplayListDirty();
                    markDirty(domainRef);
                }
            },

            /**
             * Remove an image from the cache.
             * @param {string} id - Image ID
             */
            remove(id) {
                if (_cache?.delete(id)) {
                    _markDisplayListDirty();
                    markDirty(domainRef);
                }
            },

            /**
             * Get image by ID (sync read).
             * @param {string} id - Image ID
             * @returns {Object|null} Image or null
             */
            get(id) {
                return _cache?.get(id) || null;
            }
        };

        // =====================================================================
        // LOAD / CACHE MANAGEMENT
        // =====================================================================

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
                        const data = response.data;
                        _cache = new Map(data.images.map(img => [img.id, img]));
                        _cacheEpoch = data.epoch;
                    } else {
                        // Delta load
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

        /**
         * Helper: Handle face/person cleanup when deleting an image.
         * Called within a transaction.
         * @param {string} imageId - Image being deleted
         */
        function handleFaceCleanup(imageId) {
            // Get faces on this image
            const imageFaces = faces._internal.getForPerson ?
                faces.getForImage(imageId) : [];

            if (!imageFaces || imageFaces.length === 0) return;

            // Track persons that need face count updates
            const personUpdates = new Map(); // personId → {decrement, wasPreferred}

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
                // Remove face from faces cache
                faces._internal.remove(face.id);
            }

            // Update person face counts
            for (const [personId, updates] of personUpdates) {
                for (let i = 0; i < updates.decrement; i++) {
                    const newCount = people._internal.decrementFaceCount(personId);
                    // Delete person if no more faces
                    if (newCount === 0) {
                        people._internal.remove(personId);
                        break;
                    }
                }
                // Update preferred face if needed
                const person = people._internal.get(personId);
                if (person && updates.wasPreferred) {
                    const remainingFace = faces._internal.getFirstForPerson(personId, { excludingImageId: imageId });
                    if (remainingFace) {
                        people._internal.update(personId, { preferred_face_id: remainingFace.id });
                        people._internal.bustThumbnail(personId);
                        // Lock the new preferred face (backend does this too)
                        faces._internal.update(remainingFace.id, { manually_tagged: true });
                    }
                }
            }
        }

        // =====================================================================
        // PUBLIC API
        // =====================================================================

        return {
            // --- Transaction system metadata ---
            _name: 'images',
            _notify: notify,

            // --- Internal API (for cross-domain transactions) ---
            _internal,

            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            reload() {
                return load(true);
            },

            // --- Accessors (sync reads, no transaction needed) ---
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

            // --- Display List (sorted and filtered) ---

            /**
             * Get the display list: all images sorted and filtered per current settings.
             * This is the single source of truth for what images should be displayed.
             * Returns the actual state array - do not mutate directly.
             * @returns {Array<Object>} Sorted and filtered images
             */
            getDisplayList() {
                _ensureDisplayList();
                return _displayList;
            },

            // --- Mutations (wrapped in transactions) ---

            /**
             * Update one or more images (description, rating, etc.).
             * @param {Array|Object} updates - Single {id, ...changes} or array of them
             */
            update(updates) {
                if (!Array.isArray(updates)) updates = [updates];

                return queueTransaction(async () => {
                    // Backup for rollback
                    const backup = new Map();
                    for (const upd of updates) {
                        const image = _cache?.get(upd.id);
                        if (image) {
                            backup.set(upd.id, { ...image });
                            _internal.update(upd.id, upd);
                        }
                    }

                    // Persist
                    try {
                        for (const upd of updates) {
                            const { id, ...changes } = upd;
                            await App.apiPost(`/images/${id}`, changes);
                        }
                    } catch (err) {
                        // Rollback
                        for (const [id, img] of backup) {
                            _cache.set(id, img);
                            markDirty(domainRef);
                        }
                        broadcastError(err.message || 'Failed to update images');
                        throw err;
                    }
                });
            },

            /**
             * Delete one or more images.
             * Handles full cascade: faces → people → duplicates → images.
             * @param {Array|string} ids - Single ID or array of IDs
             * @param {Object} options - {deleteFiles: bool}
             */
            delete(ids, options = {}) {
                if (!Array.isArray(ids)) ids = [ids];
                const { deleteFiles = false } = options;

                return queueTransaction(async () => {
                    // Backup for rollback
                    const backup = new Map();
                    for (const id of ids) {
                        const img = _cache?.get(id);
                        if (img) {
                            backup.set(id, img);
                        }
                    }

                    // Handle cascade cleanup for each image
                    for (const id of ids) {
                        // 1. Handle faces on this image (updates people too)
                        handleFaceCleanup(id);

                        // 2. Remove from duplicate groups
                        duplicates._internal.removeImage(id);

                        // 3. Remove image from cache
                        _internal.remove(id);
                    }

                    // Persist
                    try {
                        const deleteFileParam = deleteFiles ? '?delete_file=true' : '';
                        for (const id of ids) {
                            await App.apiDelete(`/images/${id}${deleteFileParam}`);
                        }
                    } catch (err) {
                        // Rollback is complex with cascade - reload affected domains instead
                        broadcastError(err.message || 'Failed to delete images');
                        // Force reload to restore consistent state
                        faces.reload();
                        people.reload();
                        load(true);
                        throw err;
                    }
                });
            },

            /**
             * Rotate one or more images.
             * @param {Array|string} ids - Single ID or array of IDs
             * @param {number} degrees - Rotation degrees (90, 180, 270)
             */
            rotate(ids, degrees) {
                if (!Array.isArray(ids)) ids = [ids];

                return queueTransaction(async () => {
                    try {
                        await App.apiPost('/images/rotate', { ids, degrees });
                        // Swap dimensions in cache
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

            // --- Single image fetch ---
            /**
             * Fetch a single image by ID.
             * Returns from cache if available, otherwise fetches from backend.
             * @param {string} id - Image ID
             * @returns {Promise<Object>} Image data
             */
            async fetchById(id) {
                // Return from cache if available
                if (_cache?.has(id)) {
                    return _cache.get(id);
                }
                // Fetch from backend
                const response = await App.apiGet(`/images/${id}`);
                const image = response.data;
                // Store in cache if cache exists
                if (_cache && image) {
                    _cache.set(image.id, image);
                }
                return image;
            },

            // --- Similarity data for sort-by-similarity ---

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

            // --- People names for sort-by-people ---

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

            // --- Filter by people ---
            /**
             * Get image IDs filtered by people.
             * @param {Array<string>} peopleIds - Array of person IDs
             * @returns {Promise<Set<string>>} Set of image IDs containing those people
             */
            async getFilteredByPeople(peopleIds) {
                const response = await App.apiGet(`/images?people=${encodeURIComponent(peopleIds.join(','))}`);
                const images = response.data.images || [];
                return new Set(images.map(img => String(img.id)));
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
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;              // Map<personId, {id, name, face_count, threshold, preferred_face_id}>
        let _cacheTime = 0;             // Last fetch timestamp
        let _thumbnailBust = new Map(); // personId → timestamp for cache busting
        let _loading = false;
        let _pendingLoad = null;

        const CACHE_TTL = 30000;        // 30 seconds

        // Domain reference for transaction system
        const domainRef = { _name: 'people', _notify: notify };

        // =====================================================================
        // INTERNAL API
        // =====================================================================
        // Used by other domains (e.g., faces) within transactions.
        // These methods mutate state and call markDirty() but don't make API calls.
        // API calls are the responsibility of the caller.

        const _internal = {
            /**
             * Add a person to the cache.
             * Called after API creates the person.
             * @param {Object} person - Person object from API response
             */
            add(person) {
                if (_cache) {
                    _cache.set(person.id, person);
                    markDirty(domainRef);
                }
            },

            /**
             * Update a person in the cache.
             * @param {string} id - Person ID
             * @param {Object} changes - Properties to merge
             */
            update(id, changes) {
                const person = _cache?.get(id);
                if (person) {
                    Object.assign(person, changes);
                    markDirty(domainRef);
                }
            },

            /**
             * Remove a person from the cache.
             * Called after API deletes or when face_count reaches 0.
             * @param {string} id - Person ID
             */
            remove(id) {
                if (_cache?.delete(id)) {
                    markDirty(domainRef);
                }
            },

            /**
             * Get person by ID (sync read).
             * @param {string} id - Person ID
             * @returns {Object|null} Person or null
             */
            get(id) {
                return _cache?.get(id) || null;
            },

            /**
             * Find person by name (case-insensitive, sync read).
             * @param {string} name - Person name
             * @returns {Object|null} Person or null
             */
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

            /**
             * Increment face count for a person.
             * @param {string} id - Person ID
             */
            incrementFaceCount(id) {
                const person = _cache?.get(id);
                if (person) {
                    person.face_count = (person.face_count || 0) + 1;
                    markDirty(domainRef);
                }
            },

            /**
             * Decrement face count for a person.
             * @param {string} id - Person ID
             * @returns {number} New face count
             */
            decrementFaceCount(id) {
                const person = _cache?.get(id);
                if (person) {
                    person.face_count = Math.max(0, (person.face_count || 1) - 1);
                    markDirty(domainRef);
                    return person.face_count;
                }
                return 0;
            },

            /**
             * Bust thumbnail cache for a person.
             * @param {string} id - Person ID
             */
            bustThumbnail(id) {
                _thumbnailBust.set(id, Date.now());
            }
        };

        // =====================================================================
        // LOAD / CACHE MANAGEMENT
        // =====================================================================

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

        /**
         * Invalidate cache, forcing reload on next access.
         */
        function invalidate() {
            _cacheTime = 0;
        }

        // =====================================================================
        // PUBLIC API
        // =====================================================================

        return {
            // --- Transaction system metadata ---
            _name: 'people',
            _notify: notify,

            // --- Internal API (for cross-domain transactions) ---
            _internal,

            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            invalidate,
            reload() {
                return load(true);
            },

            // --- Accessors (sync reads, no transaction needed) ---
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

            /**
             * Search people by name using fuzzy subsequence matching.
             * Used by autocomplete. Query "sro" matches "Steve Rose".
             * @param {string} query - Search query
             * @returns {Array} Matching people sorted by: prefix match, face_count desc, name
             */
            search(query) {
                if (!_cache) return [];
                const lowerQuery = query.toLowerCase().trim();
                if (!lowerQuery) {
                    // Return all, sorted by face count
                    return Array.from(_cache.values())
                        .sort((a, b) => (b.face_count || 0) - (a.face_count || 0) || a.name.localeCompare(b.name));
                }

                // Fuzzy subsequence match: "sro" matches "Steve Rose"
                function fuzzyMatch(q, target) {
                    let qi = 0;
                    for (let ti = 0; ti < target.length && qi < q.length; ti++) {
                        if (target[ti] === q[qi]) {
                            qi++;
                        }
                    }
                    return qi === q.length;
                }

                return Array.from(_cache.values())
                    .filter(p => fuzzyMatch(lowerQuery, p.name.toLowerCase()))
                    .sort((a, b) => {
                        // Prefer prefix matches (exact start)
                        const aPrefix = a.name.toLowerCase().startsWith(lowerQuery);
                        const bPrefix = b.name.toLowerCase().startsWith(lowerQuery);
                        if (aPrefix !== bPrefix) return bPrefix - aPrefix;
                        // Then substring matches before fuzzy-only matches
                        const aSubstr = a.name.toLowerCase().includes(lowerQuery);
                        const bSubstr = b.name.toLowerCase().includes(lowerQuery);
                        if (aSubstr !== bSubstr) return bSubstr - aSubstr;
                        // Then by face count
                        return (b.face_count || 0) - (a.face_count || 0) || a.name.localeCompare(b.name);
                    });
            },

            /**
             * Fetch a single person by ID from the backend.
             * Unlike getById, this always fetches fresh data.
             * @param {string} id - Person ID
             * @returns {Promise<Object>} Person data
             */
            async fetchById(id) {
                const response = await App.apiGet(`/people/${id}`);
                // Unwrap the response (API returns {success, data})
                const person = response?.data;
                // Update cache if loaded
                if (_cache && person) {
                    _cache.set(person.id, person);
                }
                return person;
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

            // --- Mutations (wrapped in transactions) ---

            /**
             * Create a new person.
             * Note: Usually called internally by faces.identify() via findOrCreate pattern.
             * @param {string} name - Person name
             * @returns {Promise<Object>} Created person
             */
            create(name) {
                return queueTransaction(async () => {
                    const response = await App.apiPost('/people', { name });
                    const person = response.data;
                    _internal.add(person);
                    return person;
                });
            },

            /**
             * Rename a person.
             * @param {string} id - Person ID
             * @param {string} name - New name
             */
            rename(id, name) {
                return queueTransaction(async () => {
                    const person = _cache?.get(id);
                    if (!person) return;

                    const oldName = person.name;

                    // Optimistic update
                    _internal.update(id, { name });

                    try {
                        await App.apiPatch(`/people/${id}`, { name });
                    } catch (err) {
                        // Rollback
                        _internal.update(id, { name: oldName });
                        broadcastError(err.message || 'Failed to rename person');
                        throw err;
                    }
                });
            },

            /**
             * Delete a person.
             * Note: Usually happens automatically when face_count reaches 0.
             * @param {string} id - Person ID
             */
            delete(id) {
                return queueTransaction(async () => {
                    const person = _cache?.get(id);
                    if (!person && _cache !== null) return;

                    // Optimistic update
                    _internal.remove(id);

                    try {
                        await App.apiDelete(`/people/${id}`);
                    } catch (err) {
                        // Rollback
                        if (person) {
                            _internal.add(person);
                        }
                        broadcastError(err.message || 'Failed to delete person');
                        throw err;
                    }
                });
            },

            /**
             * Set preferred face for a person.
             * @param {string} personId - Person ID
             * @param {string} faceId - Face ID to use as preferred
             */
            setPreferredFace(personId, faceId) {
                return queueTransaction(async () => {
                    try {
                        await App.apiPost(`/people/${personId}/set-preferred`, { face_id: faceId });
                        _internal.update(personId, { preferred_face_id: faceId });
                        _internal.bustThumbnail(personId);
                        // Preferred face is automatically padlocked (backend sets manually_tagged=1)
                        faces._internal.update(faceId, { manually_tagged: true });
                    } catch (err) {
                        broadcastError(err.message || 'Failed to set preferred face');
                        throw err;
                    }
                });
            },

            /**
             * Set recognition threshold for a person.
             * @param {string} personId - Person ID
             * @param {number} threshold - New threshold value
             */
            setThreshold(personId, threshold) {
                return queueTransaction(async () => {
                    const person = _cache?.get(personId);
                    if (!person) return;

                    const oldThreshold = person.recognition_threshold;

                    // Optimistic update
                    _internal.update(personId, { recognition_threshold: threshold });

                    try {
                        const result = await App.apiPatch(`/people/${personId}`, { recognition_threshold: threshold });
                        return result;
                    } catch (err) {
                        // Rollback
                        _internal.update(personId, { recognition_threshold: oldThreshold });
                        broadcastError(err.message || 'Failed to update threshold');
                        throw err;
                    }
                });
            }
        };
    })();

    // =========================================================================
    // FACES DOMAIN
    // =========================================================================
    // All faces with derived views - persisted to backend

    const faces = (function() {
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // State
        let _cache = null;              // Map<faceId, face>
        let _loading = false;
        let _pendingLoad = null;

        // Derived view caches (invalidated on change)
        let _unknownFaces = null;       // Faces with no person_id
        let _facesByPerson = null;      // Map<personId, face[]>
        let _facesByImage = null;       // Map<imageId, face[]>

        // Domain reference for transaction system
        const domainRef = { _name: 'faces', _notify: notify };

        /**
         * Invalidate derived view caches.
         */
        function invalidateDerived() {
            _unknownFaces = null;
            _facesByPerson = null;
            _facesByImage = null;
        }

        // =====================================================================
        // INTERNAL API
        // =====================================================================
        // Used by other domains (e.g., images for delete cascade) within transactions.
        // These methods mutate state and call markDirty() but don't make API calls.

        const _internal = {
            /**
             * Update a face in the cache.
             * @param {string} id - Face ID
             * @param {Object} changes - Properties to merge
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
             * Remove a face from the cache.
             * @param {string} id - Face ID
             */
            remove(id) {
                if (_cache?.delete(id)) {
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Get face by ID (sync read).
             * @param {string} id - Face ID
             * @returns {Object|null} Face or null
             */
            get(id) {
                return _cache?.get(id) || null;
            },

            /**
             * Get faces for a person (sync read).
             * @param {string} personId - Person ID
             * @returns {Array} Faces for the person
             */
            getForPerson(personId) {
                if (!_cache) return [];
                // Build lookup if needed
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
             * Get first face for a person (for setting preferred).
             * @param {string} personId - Person ID
             * @param {Object} options - {excludingImageId: imageId to skip}
             * @returns {Object|null} First face or null
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
             * Link a face to a person (update cache only).
             * Also marks the face as manually_tagged since this is a user action.
             * @param {string} faceId - Face ID
             * @param {string} personId - Person ID
             * @param {string} personName - Person name
             */
            linkToPerson(faceId, personId, personName) {
                const face = _cache?.get(faceId);
                console.log('[AppState.faces._internal.linkToPerson]', faceId, '->', personId, personName, 'found in cache:', !!face);
                if (face) {
                    face.person_id = personId;
                    face.person_name = personName;
                    face.manually_tagged = true;
                    invalidateDerived();
                    markDirty(domainRef);
                } else {
                    console.warn('[AppState.faces._internal.linkToPerson] Face not in cache!', faceId);
                }
            },

            /**
             * Unlink a face from its person (update cache only).
             * Also clears the manually_tagged flag.
             * @param {string} faceId - Face ID
             * @returns {string|null} Previous person_id if any
             */
            unlinkFromPerson(faceId) {
                const face = _cache?.get(faceId);
                if (face && face.person_id) {
                    const oldPersonId = face.person_id;
                    face.person_id = null;
                    face.person_name = null;
                    face.manually_tagged = false;
                    invalidateDerived();
                    markDirty(domainRef);
                    return oldPersonId;
                }
                return null;
            },

            /**
             * Mark a face as suppressed (update cache only).
             * @param {string} faceId - Face ID
             */
            suppress(faceId) {
                const face = _cache?.get(faceId);
                if (face) {
                    face.suppressed = true;
                    invalidateDerived();
                    markDirty(domainRef);
                }
            },

            /**
             * Apply auto-matched face updates from backend reassessment.
             * Unlike linkToPerson, this does NOT set manually_tagged since these are auto-matches.
             * @param {Array<{face_id: string, person_id: string, person_name: string}>} updates
             * @returns {Array<{face_id: string, person_id: string}>} Actually updated faces (for people count updates)
             */
            applyAutoMatches(updates) {
                if (!_cache || !updates || updates.length === 0) return [];

                const applied = [];
                for (const { face_id, person_id, person_name } of updates) {
                    const face = _cache.get(face_id);
                    if (face && !face.person_id) {
                        // Only update if face is still unassigned (wasn't manually tagged in the meantime)
                        face.person_id = person_id;
                        face.person_name = person_name;
                        // Don't set manually_tagged - these are auto-matches
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
        // LOAD / CACHE MANAGEMENT
        // =====================================================================

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
        // PUBLIC API
        // =====================================================================

        return {
            // --- Transaction system metadata ---
            _name: 'faces',
            _notify: notify,

            // --- Internal API (for cross-domain transactions) ---
            _internal,

            // --- Subscriptions ---
            onChanged: subscribe,
            onError: subscribeError,

            // --- Load ---
            load,
            reload() {
                return load(true);
            },

            // --- Accessors (sync reads, no transaction needed) ---
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
                return _internal.getForPerson(personId);
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

            /**
             * Fetch faces for an image directly from the backend.
             * Use this when you need fresh data or the global cache isn't loaded.
             * @param {string} imageId - Image ID
             * @returns {Promise<Array>} Faces for the image
             */
            async fetchForImage(imageId) {
                // If cache is loaded, use it
                if (_cache) {
                    return this.getForImage(imageId);
                }
                // Otherwise fetch from backend
                return (await App.apiGet(`/images/${imageId}/faces`)).data;
            },

            /**
             * Fetch all faces for a person from the backend.
             * Used for pick-preferred mode in faces screen.
             * @param {string} personId - Person ID
             * @returns {Promise<Array>} Faces for the person
             */
            async fetchForPerson(personId) {
                return (await App.apiGet(`/people/${personId}/faces`)).data;
            },

            /**
             * Search faces by semantic similarity (OpenCLIP embeddings).
             * @param {string} query - Optional search query (empty returns all)
             * @returns {Promise<Array>} Matching faces sorted by similarity
             */
            search(query) {
                const url = query ? `/faces?search=${encodeURIComponent(query)}` : '/faces';
                return App.apiGet(url).then(r => r || []);
            },

            // --- Mutations (wrapped in transactions) ---

            /**
             * Identify faces - assign to a person.
             * Creates person if needed, updates face counts, sets preferred face.
             *
             * OPTIMISTIC UPDATE DESIGN:
             * All state changes (faces + people) happen immediately within one epoch:
             * 1. If new person needed: create temp person in people cache
             * 2. Link faces to person (faces cache)
             * 3. Update face counts and preferred face (people cache)
             * 4. Both domains marked dirty, subscribers notified together
             * 5. API call in background
             * 6. On success: replace temp IDs with real IDs
             * 7. On failure: rollback both domains
             *
             * @param {Array|string} faceIds - Single ID or array of IDs
             * @param {string} personName - Name for the person
             * @param {Object} options - {preferredFaceId, existingPersonId}
             */
            identify(faceIds, personName, options = {}) {
                console.log('[AppState.faces.identify] START:', { faceIds, personName, options }, 'currentEpoch:', _txEpoch);
                if (!Array.isArray(faceIds)) faceIds = [faceIds];
                const { preferredFaceId = null, existingPersonId = null } = options;

                return queueTransaction(async () => {
                    console.log('[AppState.faces.identify] Transaction executing, epoch:', _txEpoch);

                    // =========================================================
                    // BACKUP STATE FOR ROLLBACK
                    // =========================================================
                    const faceBackup = new Map();
                    for (const faceId of faceIds) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            faceBackup.set(faceId, {
                                person_id: face.person_id,
                                person_name: face.person_name,
                                manually_tagged: face.manually_tagged
                            });
                        }
                    }

                    // Track old person IDs for face count adjustments
                    const oldPersonIds = new Map();
                    for (const faceId of faceIds) {
                        const data = faceBackup.get(faceId);
                        if (data?.person_id) {
                            oldPersonIds.set(faceId, data.person_id);
                        }
                    }

                    // =========================================================
                    // FIND OR CREATE TARGET PERSON (OPTIMISTIC)
                    // =========================================================
                    let personId = existingPersonId;
                    let tempPersonId = null;
                    let createdTempPerson = false;

                    if (!personId) {
                        const existingPerson = people._internal.findByName(personName);
                        if (existingPerson) {
                            personId = existingPerson.id;
                        } else {
                            // New person - create temp person in people cache NOW (optimistic)
                            tempPersonId = `temp-${Date.now()}`;
                            personId = tempPersonId;

                            // Create temp person with temp ID, face count, and preferred face
                            const tempPerson = {
                                id: tempPersonId,
                                name: personName,
                                face_count: faceIds.length,
                                preferred_face_id: preferredFaceId || faceIds[0],
                                threshold: null
                            };
                            people._internal.add(tempPerson);
                            createdTempPerson = true;
                            console.log('  Created temp person:', tempPerson);
                        }
                    }
                    console.log('  Target person:', { personId, isExisting: !tempPersonId, isTemp: !!tempPersonId });

                    // =========================================================
                    // OPTIMISTIC UPDATE: FACES CACHE
                    // =========================================================
                    console.log('  Optimistic: linking', faceIds.length, 'faces to person', personId);
                    for (const faceId of faceIds) {
                        _internal.linkToPerson(faceId, personId, personName);
                    }

                    // =========================================================
                    // OPTIMISTIC UPDATE: PEOPLE CACHE (face counts, preferred)
                    // =========================================================
                    if (!createdTempPerson) {
                        // Existing person - increment face count
                        for (const faceId of faceIds) {
                            const oldPersonId = oldPersonIds.get(faceId);
                            if (oldPersonId !== personId) {
                                people._internal.incrementFaceCount(personId);
                            }
                        }

                        // Set preferred face if person doesn't have one
                        const person = people._internal.get(personId);
                        if (person && !person.preferred_face_id) {
                            people._internal.update(personId, {
                                preferred_face_id: preferredFaceId || faceIds[0]
                            });
                            people._internal.bustThumbnail(personId);
                        }
                    }

                    // Decrement face counts for old persons (optimistic)
                    const decrementedPersons = new Set();
                    for (const [faceId, oldPersonId] of oldPersonIds) {
                        if (oldPersonId !== personId && !decrementedPersons.has(oldPersonId)) {
                            const newCount = people._internal.decrementFaceCount(oldPersonId);
                            decrementedPersons.add(oldPersonId);
                            if (newCount === 0) {
                                people._internal.remove(oldPersonId);
                            }
                        }
                    }

                    // Log state after optimistic updates
                    const unknownCount = Array.from(_cache?.values() || []).filter(f => !f.person_id && !f.suppressed).length;
                    console.log('  After optimistic: unknown faces:', unknownCount, 'people count:', people.getAll().length);

                    // =========================================================
                    // API CALL
                    // =========================================================
                    try {
                        console.log('  Making API call to /faces/identify-batch');
                        const response = await App.apiPost('/faces/identify-batch', {
                            face_ids: faceIds,
                            name: personName,
                            person_id: tempPersonId ? null : personId,
                            preferred_face_id: preferredFaceId || faceIds[0]
                        });
                        const responseData = response.data;
                        console.log('  API response:', responseData);

                        const realPersonId = responseData.person_id;

                        // =====================================================
                        // RECONCILE: Replace temp IDs with real IDs
                        // =====================================================
                        if (tempPersonId && realPersonId !== tempPersonId) {
                            console.log('  Reconciling temp ID', tempPersonId, '→ real ID', realPersonId);

                            // Update faces to use real person ID
                            for (const faceId of faceIds) {
                                const face = _cache?.get(faceId);
                                if (face && face.person_id === tempPersonId) {
                                    face.person_id = realPersonId;
                                }
                            }
                            invalidateDerived();
                            markDirty(domainRef);

                            // Replace temp person with real person data
                            people._internal.remove(tempPersonId);
                            if (responseData.person) {
                                people._internal.add(responseData.person);
                            }
                        }

                        console.log('[AppState.faces.identify] Transaction complete, personId:', realPersonId);
                        return { personId: realPersonId };

                    } catch (err) {
                        // =====================================================
                        // ROLLBACK: Restore both faces and people caches
                        // =====================================================
                        console.error('[AppState.faces.identify] API ERROR, rolling back:', err);

                        // Rollback faces
                        for (const [faceId, data] of faceBackup) {
                            const face = _cache?.get(faceId);
                            if (face) {
                                face.person_id = data.person_id;
                                face.person_name = data.person_name;
                                face.manually_tagged = data.manually_tagged;
                            }
                        }
                        invalidateDerived();
                        markDirty(domainRef);

                        // Rollback people: remove temp person if created
                        if (createdTempPerson) {
                            people._internal.remove(tempPersonId);
                        }

                        // Rollback people: restore face counts
                        for (const [faceId, oldPersonId] of oldPersonIds) {
                            if (oldPersonId !== personId) {
                                people._internal.incrementFaceCount(oldPersonId);
                            }
                        }
                        if (!createdTempPerson) {
                            for (const faceId of faceIds) {
                                const oldPersonId = oldPersonIds.get(faceId);
                                if (oldPersonId !== personId) {
                                    people._internal.decrementFaceCount(personId);
                                }
                            }
                        }

                        broadcastError(err.message || 'Failed to identify faces');
                        throw err;
                    }
                });
            },

            /**
             * Unassign faces - return to unknown pool.
             * Decrements face counts, deletes person if count reaches 0.
             * @param {Array|string} faceIds - Single ID or array of IDs
             */
            unassign(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];

                return queueTransaction(async () => {
                    // Track old person IDs for rollback and face count updates
                    const backup = new Map();
                    const personFaceCountChanges = new Map(); // personId → count change

                    for (const faceId of faceIds) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            backup.set(faceId, { person_id: face.person_id, person_name: face.person_name });
                            if (face.person_id) {
                                const current = personFaceCountChanges.get(face.person_id) || 0;
                                personFaceCountChanges.set(face.person_id, current + 1);
                            }
                        }
                    }

                    // Optimistic update - unlink faces
                    for (const faceId of faceIds) {
                        _internal.unlinkFromPerson(faceId);
                    }

                    try {
                        // API call
                        await App.apiPost('/faces/unassign-batch', { face_ids: faceIds });

                        // Update people cache
                        for (const [personId, decrementCount] of personFaceCountChanges) {
                            for (let i = 0; i < decrementCount; i++) {
                                const newCount = people._internal.decrementFaceCount(personId);
                                // Delete person if no more faces (only on last decrement)
                                if (newCount === 0 && i === decrementCount - 1) {
                                    people._internal.remove(personId);
                                }
                            }
                            // Update preferred face if needed
                            const person = people._internal.get(personId);
                            if (person) {
                                const remainingFaces = _internal.getForPerson(personId);
                                if (remainingFaces.length > 0 && !remainingFaces.some(f => f.id === person.preferred_face_id)) {
                                    const newPreferredId = remainingFaces[0].id;
                                    people._internal.update(personId, { preferred_face_id: newPreferredId });
                                    people._internal.bustThumbnail(personId);
                                    // Lock the new preferred face (backend does this too)
                                    _internal.update(newPreferredId, { manually_tagged: true });
                                }
                            }
                        }
                    } catch (err) {
                        // Rollback faces
                        for (const [faceId, data] of backup) {
                            if (data.person_id) {
                                _internal.linkToPerson(faceId, data.person_id, data.person_name);
                            }
                        }
                        broadcastError(err.message || 'Failed to unassign faces');
                        throw err;
                    }
                });
            },

            /**
             * Suppress faces - mark as false positives.
             * If face was identified, handles person cleanup.
             * @param {Array|string} faceIds - Single ID or array of IDs
             */
            suppress(faceIds) {
                if (!Array.isArray(faceIds)) faceIds = [faceIds];

                return queueTransaction(async () => {
                    // Track faces for rollback
                    const backup = new Map();
                    const personFaceCountChanges = new Map();

                    for (const faceId of faceIds) {
                        const face = _cache?.get(faceId);
                        if (face) {
                            backup.set(faceId, {
                                suppressed: face.suppressed,
                                person_id: face.person_id,
                                person_name: face.person_name
                            });
                            if (face.person_id) {
                                const current = personFaceCountChanges.get(face.person_id) || 0;
                                personFaceCountChanges.set(face.person_id, current + 1);
                            }
                        }
                    }

                    // Optimistic update
                    for (const faceId of faceIds) {
                        const face = _cache?.get(faceId);
                        if (face?.person_id) {
                            _internal.unlinkFromPerson(faceId);
                        }
                        _internal.suppress(faceId);
                    }

                    try {
                        // API calls (TODO: batch endpoint)
                        for (const faceId of faceIds) {
                            await App.apiPost(`/faces/${faceId}/suppress`);
                        }

                        // Update people cache
                        for (const [personId, decrementCount] of personFaceCountChanges) {
                            for (let i = 0; i < decrementCount; i++) {
                                const newCount = people._internal.decrementFaceCount(personId);
                                if (newCount === 0 && i === decrementCount - 1) {
                                    people._internal.remove(personId);
                                }
                            }
                            // Update preferred face if needed
                            const person = people._internal.get(personId);
                            if (person) {
                                const remainingFaces = _internal.getForPerson(personId);
                                if (remainingFaces.length > 0 && !remainingFaces.some(f => f.id === person.preferred_face_id)) {
                                    const newPreferredId = remainingFaces[0].id;
                                    people._internal.update(personId, { preferred_face_id: newPreferredId });
                                    people._internal.bustThumbnail(personId);
                                    // Lock the new preferred face (backend does this too)
                                    _internal.update(newPreferredId, { manually_tagged: true });
                                }
                            }
                        }
                    } catch (err) {
                        // Rollback
                        for (const [faceId, data] of backup) {
                            const face = _cache?.get(faceId);
                            if (face) {
                                face.suppressed = data.suppressed;
                                if (data.person_id) {
                                    face.person_id = data.person_id;
                                    face.person_name = data.person_name;
                                }
                            }
                        }
                        invalidateDerived();
                        markDirty(domainRef);
                        broadcastError(err.message || 'Failed to suppress faces');
                        throw err;
                    }
                });
            },

            /**
             * Toggle the manually_tagged flag for a face.
             * Manually tagged faces are used as reference for auto-matching.
             * Auto-tagged faces are not used for matching (prevents snowball effect).
             * @param {string} faceId - Face ID
             * @returns {Promise<boolean>} The new manually_tagged value
             */
            toggleManualTag(faceId) {
                return queueTransaction(async () => {
                    const face = _cache?.get(faceId);
                    const currentValue = face?.manually_tagged || false;
                    const newValue = !currentValue;

                    // Optimistic update
                    if (face) {
                        face.manually_tagged = newValue;
                        markDirty(domainRef);
                    }

                    try {
                        // API call
                        const response = await App.apiPost(`/faces/${faceId}/toggle-manual`);
                        // Server returns the actual new value (wrapped in data field)
                        const serverValue = response.data.manually_tagged;
                        if (face && serverValue !== newValue) {
                            face.manually_tagged = serverValue;
                            markDirty(domainRef);
                        }
                        return serverValue;
                    } catch (err) {
                        // Rollback
                        if (face) {
                            face.manually_tagged = currentValue;
                            markDirty(domainRef);
                        }
                        broadcastError(err.message || 'Failed to toggle manual tag');
                        throw err;
                    }
                });
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
        const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

        // Per-level caching
        let _groupCache = {};           // level → groups array
        let _statusCache = {};          // level → status object
        let _epochCache = {};           // level → epoch timestamp

        let _currentLevel = 2;          // Default level (Similar)
        let _computing = false;
        let _pollTimer = null;
        let _pollLevel = null;          // Level being polled

        // Domain reference for transaction system
        const domainRef = { _name: 'duplicates', _notify: notify };

        // =====================================================================
        // INTERNAL API
        // =====================================================================
        // Used by images domain for delete cascade.

        const _internal = {
            /**
             * Remove an image from all cached duplicate groups.
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

        /**
         * Internal: Start polling for a level if computation is in progress.
         * Polling is automatic and internal - GUI should not call this.
         */
        function _startPollingIfNeeded(level, status) {
            // Only poll if computing or pending
            if (status !== 'computing' && status !== 'pending') {
                return;
            }

            // Already polling this level
            if (_pollTimer && _pollLevel === level) {
                return;
            }

            // Stop any existing poll for different level
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

                    // Check if computation finished
                    if (newStatus !== 'computing' && newStatus !== 'pending') {
                        _stopPolling();
                        _computing = false;
                        _groupCache[level] = data.groups || [];
                        _epochCache[level] = Date.now();
                        broadcast({ type: 'changed', level });
                    }
                    // Don't broadcast during polling - avoid unnecessary re-renders
                } catch (err) {
                    console.error('Duplicates poll error:', err);
                }
            }, 2000);
        }

        /**
         * Internal: Stop polling.
         */
        function _stopPolling() {
            if (_pollTimer) {
                clearInterval(_pollTimer);
                _pollTimer = null;
                _pollLevel = null;
            }
        }

        /**
         * Load duplicate groups for a level.
         * Automatically starts internal polling if computation is in progress.
         */
        async function loadLevel(level, force = false) {
            // Return cached if available and not forced
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

                // Update computing flag
                const status = data.status;
                _computing = status === 'computing' || status === 'pending';

                // Automatically start polling if computation in progress
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
            // --- Transaction system metadata ---
            _name: 'duplicates',
            _notify: notify,

            // --- Internal API (for cross-domain transactions) ---
            _internal,

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

            /**
             * Sort duplicate groups by semantic similarity to a query.
             * @param {string} query - Semantic query string
             * @param {Array<string>} imageIds - Image IDs to get similarity scores for
             * @returns {Promise<Array>} Array of {image_id, score} sorted by score
             */
            async sortSemantic(query, imageIds) {
                const response = await App.apiPost('/duplicates/sort-semantic', {
                    query,
                    image_ids: imageIds
                });
                return response.data?.scores || [];
            },

            // --- Lifecycle ---
            // stopPolling is exposed for cleanup when leaving the screen
            stopPolling() {
                _stopPolling();
            },

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
        const { subscribe, broadcast, notify } = createSubscriberSystem();

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
            // --- Transaction system metadata ---
            _name: 'selection',
            _notify: notify,

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
    // LOADING OVERLAY DOMAIN
    // =========================================================================

    /**
     * Global loading overlay with ownership tracking.
     * When something shows the loading overlay, it becomes the "owner".
     * When a new owner takes over, the previous owner is notified via the
     * 'repurposed' event, so it doesn't need to worry about hiding it.
     *
     * Usage:
     *   AppState.loading.show('gallery', 'Loading images…');
     *   // ... later ...
     *   AppState.loading.hide('gallery');  // Only hides if gallery is still owner
     */
    const loading = (function() {
        const { subscribe, broadcast } = createSubscriberSystem();

        let _owner = null;
        let _message = 'Loading…';
        let _visible = false;
        let _el = null;
        let _messageEl = null;

        // Lazy init DOM references
        function ensureElements() {
            if (!_el) {
                _el = document.getElementById('loading-overlay');
                _messageEl = document.getElementById('loading-message');
            }
        }

        return {
            onChanged: subscribe,

            /**
             * Shows the loading overlay and takes ownership.
             * If another owner already has it, that owner receives a 'repurposed' event.
             * @param {string} owner - Identifier for the caller (e.g., 'gallery', 'faces')
             * @param {string} [message='Loading…'] - Message to display
             */
            show(owner, message = 'Loading…') {
                ensureElements();

                const previousOwner = _owner;
                _owner = owner;
                _message = message;

                // Update DOM
                if (_messageEl) _messageEl.textContent = message;
                if (_el && !_visible) {
                    _el.classList.add('visible');
                    _visible = true;
                }

                // Notify if ownership changed (previous owner was repurposed)
                if (previousOwner && previousOwner !== owner) {
                    broadcast({ type: 'repurposed', previousOwner, newOwner: owner });
                }

                broadcast({ type: 'changed', visible: true, owner, message });
            },

            /**
             * Hides the loading overlay, but only if the caller is the current owner.
             * @param {string} owner - Identifier for the caller
             * @returns {boolean} True if hidden, false if caller wasn't owner
             */
            hide(owner) {
                if (_owner !== owner) return false;

                ensureElements();
                _owner = null;
                if (_el && _visible) {
                    _el.classList.remove('visible');
                    _visible = false;
                }

                broadcast({ type: 'changed', visible: false, owner: null });
                return true;
            },

            /**
             * Force hide regardless of owner. Use sparingly.
             */
            forceHide() {
                ensureElements();
                const previousOwner = _owner;
                _owner = null;
                if (_el && _visible) {
                    _el.classList.remove('visible');
                    _visible = false;
                }

                if (previousOwner) {
                    broadcast({ type: 'repurposed', previousOwner, newOwner: null });
                }
                broadcast({ type: 'changed', visible: false, owner: null });
            },

            /**
             * Update the message without changing ownership.
             * @param {string} message - New message to display
             */
            setMessage(message) {
                ensureElements();
                _message = message;
                if (_messageEl) _messageEl.textContent = message;
            },

            isVisible() { return _visible; },
            getOwner() { return _owner; },
            getMessage() { return _message; }
        };
    })();

    // =========================================================================
    // SERVER-SENT EVENTS (SSE) CONNECTION
    // =========================================================================
    // Real-time push notifications from backend.
    // Handles automatic reconnection with exponential backoff.

    // =========================================================================
    // EVENT POLLING
    // =========================================================================
    // Polls backend for events instead of using SSE (which blocks server threads).

    const events = (function() {
        const POLL_INTERVAL = 2000;  // Poll every 2 seconds
        let _pollTimer = null;
        let _polling = false;

        /**
         * Process a single event from the backend.
         * @param {Object} event - Event with 'type' and 'data' properties
         */
        function processEvent(event) {
            console.log('[Events] Processing:', event.type, event.data);

            switch (event.type) {
                case 'faces_reassessed':
                    // Backend completed face reassessment - apply updates to cache directly
                    console.log('[Events] Face reassessment complete, matched:', event.data.matched_count);
                    if (faces.isLoaded() && event.data.updated_faces?.length > 0) {
                        // Use transaction to batch the state updates
                        transaction(() => {
                            // Apply incremental updates (much faster than reloading all faces)
                            const applied = faces._internal.applyAutoMatches(event.data.updated_faces);
                            console.log('[Events] Applied', applied.length, 'face updates to cache');

                            // Also update people face counts if people cache is loaded
                            if (people.isLoaded() && applied.length > 0) {
                                // Count how many faces were assigned to each person
                                const personCounts = new Map();
                                for (const { person_id } of applied) {
                                    personCounts.set(person_id, (personCounts.get(person_id) || 0) + 1);
                                }
                                for (const [personId, count] of personCounts) {
                                    for (let i = 0; i < count; i++) {
                                        people._internal.incrementFaceCount(personId);
                                    }
                                }
                            }
                        });
                    }
                    break;

                case 'processing_complete':
                    // Full processing cycle complete - reload status and potentially images
                    status.load();
                    break;

                case 'image_ingested':
                    // New image added - may want to reload images if on gallery
                    // (For now, just log - full reload would be expensive)
                    console.log('[Events] Image ingested:', event.data.id);
                    break;

                case 'folder_added':
                case 'folder_removed':
                    // Folder changes - reload folders
                    folders.load();
                    break;

                case 'error':
                    console.error('[Events] Server error:', event.data.message);
                    break;

                default:
                    console.log('[Events] Unhandled event type:', event.type);
            }
        }

        /**
         * Poll backend for pending events.
         */
        async function poll() {
            try {
                const response = await App.apiGet('/events');
                const eventList = response.data?.events || [];
                for (const event of eventList) {
                    processEvent(event);
                }
            } catch (err) {
                // Silently ignore poll errors (backend might be restarting)
                console.debug('[Events] Poll error:', err.message);
            }
        }

        /**
         * Start polling for events.
         */
        function startPolling() {
            // Skip in mock mode
            if (typeof App !== 'undefined' && App.mockMode) {
                console.log('[Events] Skipping in mock mode');
                return;
            }

            if (_polling) return;
            _polling = true;

            console.log('[Events] Starting polling');
            // Poll immediately, then on interval
            poll();
            _pollTimer = setInterval(poll, POLL_INTERVAL);
        }

        /**
         * Stop polling for events.
         */
        function stopPolling() {
            if (_pollTimer) {
                clearInterval(_pollTimer);
                _pollTimer = null;
            }
            _polling = false;
            console.log('[Events] Stopped polling');
        }

        return {
            startPolling,
            stopPolling,
            isPolling: () => _polling
        };
    })();

    // =========================================================================
    // SPECULATIVE PRELOADING
    // =========================================================================

    /**
     * Starts event polling for backend notifications.
     *
     * Note: Speculative data preloading (people, faces, duplicates) was disabled
     * because it caused SQLite contention that blocked interactive requests,
     * making fullscreen navigation unresponsive. Data is now loaded on-demand
     * when screens that need it are visited.
     */
    function preloadAll() {
        // Start polling for backend events
        events.startPolling();
    }

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
        loading,

        // Utility functions
        preloadAll,

        // Event polling for backend notifications
        events,

        // Transaction system (for internal use by domains)
        // These will be used in Phase 2+ to wrap external methods
        _tx: {
            markDirty,
            transaction,
            queueTransaction,
            isInTransaction,
            getEpoch: getTransactionEpoch
        }
    };
})();

// Export globally
window.AppState = AppState;
