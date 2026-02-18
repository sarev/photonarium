/**
 * AppState Filter Domain - Search/Filter Criteria
 * =================================================
 *
 * Manages the current filter/search criteria applied to the gallery:
 * - Text search
 * - Date range
 * - Rating filter
 * - People filter
 * - Semantic search results
 * - Duplicate group filter
 *
 * Memory only (not persisted).
 *
 * @fileoverview Filter criteria domain.
 */

'use strict';

AppState.filter = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Current filter object.
     * @type {Object|null}
     * @property {string} [type] - Filter type: 'semantic', 'duplicates', or undefined for standard
     * @property {string} [text] - Text search query
     * @property {string} [dateStart] - Start date (ISO string)
     * @property {string} [dateEnd] - End date (ISO string)
     * @property {string} [rating] - Rating emoji to filter by
     * @property {string[]} [people] - Person IDs to filter by
     * @property {Set<string>} [peopleImageIds] - Precomputed image IDs for people filter
     * @property {string[]} [imageIds] - Image IDs for semantic/duplicates filter
     * @property {Object} [scores] - Similarity scores for semantic search
     */
    let _filter = null;

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'filter',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to filter changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Get the current filter.
         * @returns {Object|null} Filter object or null if no filter active
         */
        get() {
            return _filter;
        },

        /**
         * Set the filter.
         * @param {Object|null} newFilter - Filter object or null to clear
         * @param {Object} [options]
         * @param {boolean} [options.silent=false] - Don't broadcast change
         */
        set(newFilter, options = {}) {
            const { silent = false } = options;

            console.log('[AppState.filter.set]', newFilter ?
                `type=${newFilter.type || 'standard'}` : 'null');

            _filter = newFilter;
            if (!silent) {
                broadcast({ type: 'changed' });
            }
        },

        /**
         * Clear the filter.
         */
        clear() {
            if (_filter === null) return;

            console.log('[AppState.filter.clear]');
            _filter = null;
            broadcast({ type: 'changed' });
        },

        /**
         * Check if any filter is active.
         * @returns {boolean}
         */
        isActive() {
            return _filter !== null;
        },

        // --- Accessors for specific filter properties ---

        /**
         * Get text search query.
         * @returns {string|null}
         */
        getText() {
            return _filter?.text || null;
        },

        /**
         * Get date range filter.
         * @returns {{start: string, end: string}|null}
         */
        getDateRange() {
            if (!_filter) return null;
            return { start: _filter.dateStart, end: _filter.dateEnd };
        },

        /**
         * Get rating filter.
         * @returns {string|null}
         */
        getRating() {
            return _filter?.rating || null;
        },

        /**
         * Get people filter (person IDs).
         * @returns {string[]|null}
         */
        getPeople() {
            return _filter?.people || null;
        },
    };
})();
