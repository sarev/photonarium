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

    /** @type {string} Sort field: 'date', 'rating', 'content', 'people' */
    let _sortBy = storage.get('sortBy', 'date');

    /** @type {string} Sort direction: 'asc' or 'desc' */
    let _sortDirection = storage.get('sortDirection', 'desc');

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
            console.log('[AppState.view] Theme applied to DOM:', theme);
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
            console.log('[AppState.view] Initialized with theme:', _theme,
                'thumbnailSize:', _thumbnailSize, 'sort:', _sortBy, _sortDirection);
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

            console.log('[AppState.view.setTheme]', _theme, '->', theme);
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

            console.log('[AppState.view.setThumbnailSize]', _thumbnailSize, '->', size);
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
         * @returns {string} 'date', 'rating', 'content', or 'people'
         */
        getSortBy() {
            return _sortBy;
        },

        /**
         * Set sort field.
         * @param {string} by - 'date', 'rating', 'content', or 'people'
         */
        setSortBy(by) {
            const valid = ['date', 'rating', 'content', 'people'];
            if (!valid.includes(by)) return;
            if (_sortBy === by) return;

            console.log('[AppState.view.setSortBy]', _sortBy, '->', by);
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

            console.log('[AppState.view.setSortDirection]', _sortDirection, '->', direction);
            _sortDirection = direction;
            storage.set('sortDirection', direction);
            broadcast({ type: 'changed', property: 'sortDirection' });
        },

        /**
         * Toggle sort direction between asc and desc.
         */
        toggleSortDirection() {
            this.setSortDirection(_sortDirection === 'asc' ? 'desc' : 'asc');
        }
    };
})();
