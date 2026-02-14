/**
 * AppState View Domain - UI Preferences
 * ======================================
 *
 * Manages user interface preferences that persist across sessions:
 * - Theme (light/dark)
 * - Thumbnail size
 * - Sort settings (by, direction)
 *
 * Persisted to localStorage.
 *
 * @fileoverview View preferences domain.
 */

'use strict';

AppState.view = (function() {
    const { createSubscriberSystem, storage } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {string} Current theme: 'light' or 'dark' */
    let _theme = storage.get('theme', null);

    /** @type {number} Thumbnail size in pixels (100-400) */
    let _thumbnailSize = storage.get('thumbnailSize', 200);

    /** @type {string} Sort field: 'date', 'rating', 'content', 'people', 'quality' */
    let _sortBy = storage.get('sortBy', 'date');

    // Validate persisted sort value
    if (!['date', 'rating', 'content', 'people', 'quality'].includes(_sortBy)) {
        _sortBy = 'date';
    }

    /** @type {string} Sort direction: 'asc' or 'desc' */
    let _sortDirection = storage.get('sortDirection', 'desc');

    /** @type {boolean} Whether the info panel is collapsed */
    let _infoPanelCollapsed = storage.get('infoPanelCollapsed', false);

    /** @type {boolean} Whether the user has explicitly toggled the info panel (vs auto-collapse) */
    let _infoPanelUserSet = storage.get('infoPanelUserSet', false);

    // Initialize theme from system preference if not set
    if (_theme === null) {
        _theme = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    /**
     * Apply theme to DOM by setting data-theme attribute.
     * @param {string} theme - 'light' or 'dark'
     * @private
     */
    function applyThemeToDOM(theme) {
        const app = document.getElementById('app');
        if (app) {
            app.dataset.theme = theme;
        }
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'view',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to view changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Initialize the view domain (apply theme to DOM).
         * Called once on app startup.
         */
        init() {
            applyThemeToDOM(_theme);
        },

        // --- Theme ---

        /**
         * Get current theme.
         * @returns {string} 'light' or 'dark'
         */
        getTheme() {
            return _theme;
        },

        /**
         * Set the theme.
         * @param {string} theme - 'light' or 'dark'
         */
        setTheme(theme) {
            if (theme !== 'light' && theme !== 'dark') return;
            if (_theme === theme) return;

            _theme = theme;
            storage.set('theme', theme);
            applyThemeToDOM(theme);
            broadcast({ type: 'changed', property: 'theme' });
        },

        /**
         * Toggle between light and dark theme.
         */
        toggleTheme() {
            this.setTheme(_theme === 'light' ? 'dark' : 'light');
        },

        // --- Thumbnail Size ---

        /**
         * Get current thumbnail size.
         * @returns {number} Size in pixels (100-400)
         */
        getThumbnailSize() {
            return _thumbnailSize;
        },

        /**
         * Set thumbnail size.
         * @param {number} size - Size in pixels (clamped to 100-400)
         */
        setThumbnailSize(size) {
            size = Math.max(100, Math.min(400, Number(size) || 200));
            if (_thumbnailSize === size) return;

            _thumbnailSize = size;
            storage.set('thumbnailSize', size);
            broadcast({ type: 'changed', property: 'thumbnailSize' });
        },

        // --- Sort Settings ---

        /**
         * Get current sort settings.
         * @returns {{by: string, direction: string}}
         */
        getSort() {
            return { by: _sortBy, direction: _sortDirection };
        },

        /**
         * Get sort field.
         * @returns {string} 'date', 'rating', 'content', 'people', or 'quality'
         */
        getSortBy() {
            return _sortBy;
        },

        /**
         * Set sort field.
         * @param {string} by - 'date', 'rating', 'content', 'people', or 'quality'
         */
        setSortBy(by) {
            const valid = ['date', 'rating', 'content', 'people', 'quality'];
            if (!valid.includes(by)) return;
            if (_sortBy === by) return;

            _sortBy = by;
            storage.set('sortBy', by);
            broadcast({ type: 'changed', property: 'sortBy' });
        },

        /**
         * Get sort direction.
         * @returns {string} 'asc' or 'desc'
         */
        getSortDirection() {
            return _sortDirection;
        },

        /**
         * Set sort direction.
         * @param {string} direction - 'asc' or 'desc'
         */
        setSortDirection(direction) {
            if (direction !== 'asc' && direction !== 'desc') return;
            if (_sortDirection === direction) return;

            _sortDirection = direction;
            storage.set('sortDirection', direction);
            broadcast({ type: 'changed', property: 'sortDirection' });
        },

        /**
         * Toggle sort direction between asc and desc.
         */
        toggleSortDirection() {
            this.setSortDirection(_sortDirection === 'asc' ? 'desc' : 'asc');
        },

        // --- Info Panel ---

        /**
         * Get whether the info panel is collapsed.
         * @returns {boolean}
         */
        isInfoPanelCollapsed() {
            return _infoPanelCollapsed;
        },

        /**
         * Set whether the info panel is collapsed.
         * @param {boolean} collapsed - Whether to collapse the panel
         */
        setInfoPanelCollapsed(collapsed) {
            collapsed = !!collapsed;
            if (_infoPanelCollapsed === collapsed) return;
            _infoPanelCollapsed = collapsed;
            storage.set('infoPanelCollapsed', collapsed);
            broadcast({ type: 'changed', property: 'infoPanelCollapsed' });
        },

        /**
         * Toggle the info panel collapsed state.
         * Also marks the preference as user-set so auto-collapse stops overriding.
         */
        toggleInfoPanel() {
            _infoPanelUserSet = true;
            storage.set('infoPanelUserSet', true);
            this.setInfoPanelCollapsed(!_infoPanelCollapsed);
        },

        /**
         * Get whether the user has explicitly set the info panel state.
         * Used to decide whether auto-collapse should apply.
         * @returns {boolean}
         */
        isInfoPanelUserSet() {
            return _infoPanelUserSet;
        },
    };
})();
