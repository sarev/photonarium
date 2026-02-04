/**
 * AppState Search Domain - Semantic Search
 * ==========================================
 *
 * Manages semantic search state:
 * - Execute searches against OpenCLIP embeddings
 * - Track search results and loading state
 * - Store last query for UI display
 *
 * Memory only (not persisted).
 *
 * @fileoverview Semantic search domain.
 */

'use strict';

AppState.search = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Search results from last query.
     * @type {Object|null}
     * @property {Array} results - Array of {id, similarity} objects
     */
    let _results = null;

    /** @type {boolean} Whether a search is in progress */
    let _loading = false;

    /** @type {string|null} Last search query */
    let _query = null;

    /** @type {number|null} Last search threshold */
    let _threshold = null;

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'search',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to search state changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Execute a semantic search.
         * @param {string} query - Search query text
         * @param {number} [threshold=0.2] - Minimum similarity threshold
         * @param {number} [limit=500] - Maximum results
         * @returns {Promise<Object>} Search results
         */
        async execute(query, threshold = 0.2, limit = 500) {
            console.log('[AppState.search.execute] query:', query);

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
                console.error('[AppState.search.execute] Error:', err);
                _loading = false;
                broadcast({ type: 'error', message: err.message });
                throw err;
            }
        },

        /**
         * Get search results.
         * @returns {Object|null} Results object with results array
         */
        getResults() {
            return _results;
        },

        /**
         * Get last search query.
         * @returns {string|null}
         */
        getQuery() {
            return _query;
        },

        /**
         * Get last search threshold.
         * @returns {number|null}
         */
        getThreshold() {
            return _threshold;
        },

        /**
         * Check if search is in progress.
         * @returns {boolean}
         */
        isLoading() {
            return _loading;
        },

        /**
         * Clear search results and query.
         */
        clear() {
            _results = null;
            _query = null;
            _threshold = null;
            broadcast({ type: 'changed' });
        }
    };
})();
