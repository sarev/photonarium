/**
 * AppState Filter Domain - Search/Filter Criteria
 * =================================================
 *
 * Manages the current filter/search criteria applied to the gallery:
 * - Text search
 * - Date filter (component-based with wildcard support)
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

/**
 * @typedef {Object} DateComponents
 * @property {number|null} year  - Year or null for "any year"
 * @property {number|null} month - Month 1-12 or null for "any month"
 * @property {number|null} day   - Day 1-31 or null for "any day"
 */

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
     * @property {DateComponents} [dateFrom] - From-date components (null fields = wildcard)
     * @property {DateComponents} [dateTo] - To-date components (null fields = wildcard)
     * @property {boolean} [dateRange] - Whether range mode is active
     * @property {string} [rating] - Rating emoji to filter by
     * @property {string[]} [people] - Person IDs to filter by
     * @property {Set<string>} [peopleImageIds] - Precomputed image IDs for people filter
     * @property {string[]} [imageIds] - Image IDs for semantic/duplicates filter
     * @property {Object} [scores] - Similarity scores for semantic search
     */
    let _filter = null;

    // =========================================================================
    // DATE MATCHING (shared by images.js and duplicates.js)
    // =========================================================================

    /**
     * Test whether a single date component tuple matches a DateComponents
     * spec (exact match on non-null fields).
     * @param {number} y - Image year
     * @param {number} m - Image month (1-12)
     * @param {number} d - Image day (1-31)
     * @param {DateComponents} dc - Date components to match against
     * @returns {boolean}
     */
    function _componentsMatch(y, m, d, dc) {
        if (dc.year != null && y !== dc.year) return false;
        if (dc.month != null && m !== dc.month) return false;
        if (dc.day != null && d !== dc.day) return false;
        return true;
    }

    /**
     * Compare image (y,m,d) against a bound, returning -1/0/1.
     * Null components are treated as -Infinity (lower) or +Infinity (upper)
     * depending on the `upper` flag.
     * @param {number} y - Image year
     * @param {number} m - Image month
     * @param {number} d - Image day
     * @param {DateComponents} dc - Bound components
     * @param {boolean} upper - If true, null = +Infinity; otherwise null = -Infinity
     * @returns {number} -1 if image < bound, 0 if equal, 1 if image > bound
     */
    function _compareBound(y, m, d, dc, upper) {
        const by = dc.year != null ? dc.year : (upper ? Infinity : -Infinity);
        if (y < by) return -1;
        if (y > by) return 1;

        const bm = dc.month != null ? dc.month : (upper ? 12 : 1);
        if (m < bm) return -1;
        if (m > bm) return 1;

        const bd = dc.day != null ? dc.day : (upper ? 31 : 1);
        if (d < bd) return -1;
        if (d > bd) return 1;

        return 0;
    }

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
         * Get date filter components.
         * @returns {{from: DateComponents|null, to: DateComponents|null, range: boolean}}
         */
        getDateFilter() {
            if (!_filter) return { from: null, to: null, range: false };
            return {
                from: _filter.dateFrom || null,
                to: _filter.dateTo || null,
                range: !!_filter.dateRange,
            };
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

        /**
         * Get the search mode.
         * @returns {'images'|'videos'|'all'}
         */
        getSearchMode() {
            return _filter?.searchMode || 'all';
        },

        /**
         * Test whether a timestamp matches date filter criteria.
         *
         * Supports wildcard (null) components and wrap-around month ranges
         * (e.g. Oct–Feb matching Oct, Nov, Dec, Jan, Feb of any year).
         *
         * @param {string} timestamp - ISO timestamp string from image
         * @param {DateComponents|null} dateFrom - From-date (null = no constraint)
         * @param {DateComponents|null} dateTo - To-date (null = no constraint)
         * @param {boolean} isRange - Whether range mode is active
         * @returns {boolean} True if the timestamp matches the filter
         */
        matchDate(timestamp, dateFrom, dateTo, isRange) {
            if (!timestamp) return false;

            const dt = new Date(timestamp);
            const y = dt.getFullYear();
            const m = dt.getMonth() + 1; // 1-12
            const d = dt.getDate();

            // Single-date mode: exact match on non-null components of dateFrom
            if (!isRange) {
                return dateFrom ? _componentsMatch(y, m, d, dateFrom) : true;
            }

            // Range mode — both years null = recurring (month/day) pattern
            const fromYearNull = !dateFrom || dateFrom.year == null;
            const toYearNull = !dateTo || dateTo.year == null;

            if (fromYearNull && toYearNull) {
                // Recurring range — compare month+day ordinals only
                const imgOrd = m * 100 + d;
                const fromOrd = dateFrom
                    ? (dateFrom.month || 1) * 100 + (dateFrom.day || 1)
                    : 100;  // Jan 1
                const toOrd = dateTo
                    ? (dateTo.month || 12) * 100 + (dateTo.day || 31)
                    : 1231; // Dec 31

                if (fromOrd <= toOrd) {
                    // Normal range (e.g. Mar–Jun)
                    return imgOrd >= fromOrd && imgOrd <= toOrd;
                }
                // Wrap-around range (e.g. Oct–Feb)
                return imgOrd >= fromOrd || imgOrd <= toOrd;
            }

            // At least one side has a year — compare as full dates
            if (dateFrom && _compareBound(y, m, d, dateFrom, false) < 0) return false;
            if (dateTo && _compareBound(y, m, d, dateTo, true) > 0) return false;
            return true;
        },

        /**
         * Normalise legacy ISO-string date filter format to structured
         * DateComponents.  Mutates the filter object in place.
         *
         * Legacy format: `{ dateStart: "2024-03-15", dateEnd: "2024-06-01" }`
         * New format:    `{ dateFrom: {year,month,day}, dateTo: {…}, dateRange: true }`
         *
         * @param {Object} filter - Filter object (mutated in place)
         */
        normaliseLegacyDates(filter) {
            if (typeof filter.dateStart === 'string') {
                const dt = new Date(filter.dateStart);
                filter.dateFrom = {
                    year: dt.getFullYear(),
                    month: dt.getMonth() + 1,
                    day: dt.getDate(),
                };
                delete filter.dateStart;
            }
            if (typeof filter.dateEnd === 'string') {
                const dt = new Date(filter.dateEnd);
                filter.dateTo = {
                    year: dt.getFullYear(),
                    month: dt.getMonth() + 1,
                    day: dt.getDate(),
                };
                filter.dateRange = true;
                delete filter.dateEnd;
            }
        },
    };
})();
