/**
 * AppState Nav Domain - Navigation State
 * =======================================
 *
 * Manages application navigation state:
 * - Current screen
 * - Screen history for back navigation
 * - Fullscreen image viewer state
 * - Scroll positions per screen
 *
 * Memory only (not persisted).
 *
 * @fileoverview Navigation state domain.
 */

'use strict';

AppState.nav = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {string|null} Current screen name */
    let _screen = null;

    /** @type {string|null} Previous screen (for transitions) */
    let _previousScreen = null;

    /** @type {string[]} Screen history stack for back navigation */
    let _history = [];

    /** @type {string|null} Image ID shown in fullscreen, null if closed */
    let _fullscreenImageId = null;

    /** @type {string|null} Last image viewed in fullscreen (consumed by Gallery on enter) */
    let _lastViewedImageId = null;

    /** @type {Object.<string, number>} Scroll positions by screen name */
    let _scrollPositions = {};

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'nav',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to navigation changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        // --- Screen Navigation ---

        /**
         * Get current screen name.
         * @returns {string|null}
         */
        getScreen() {
            return _screen;
        },

        /**
         * Get previous screen name.
         * @returns {string|null}
         */
        getPreviousScreen() {
            return _previousScreen;
        },

        /**
         * Navigate to a screen.
         * @param {string} screen - Screen name to navigate to
         * @param {Object} [options]
         * @param {boolean} [options.addToHistory=true] - Add previous screen to history
         * @param {*} [options.data=null] - Data to pass with navigation event
         */
        setScreen(screen, options = {}) {
            const { addToHistory = true, data = null } = options;
            if (_screen === screen) return;

            console.log('[AppState.nav.setScreen]', _screen, '->', screen,
                addToHistory ? '(added to history)' : '');

            _previousScreen = _screen;
            if (addToHistory && _previousScreen) {
                _history.push(_previousScreen);
            }
            _screen = screen;
            broadcast({ type: 'changed', property: 'screen', data });
        },

        /**
         * Check if back navigation is possible.
         * @returns {boolean}
         */
        canGoBack() {
            return _history.length > 0;
        },

        /**
         * Navigate back to previous screen.
         * @returns {boolean} True if navigation happened
         */
        goBack() {
            if (_history.length === 0) return false;

            const previous = _history.pop();
            console.log('[AppState.nav.goBack]', _screen, '->', previous);

            _previousScreen = _screen;
            _screen = previous;
            broadcast({ type: 'changed', property: 'screen' });
            return true;
        },

        /**
         * Clear navigation history.
         */
        clearHistory() {
            _history = [];
        },

        // --- Fullscreen Viewer ---

        /**
         * Check if fullscreen viewer is open.
         * @returns {boolean}
         */
        isFullscreenOpen() {
            return _fullscreenImageId !== null;
        },

        /**
         * Get the image ID shown in fullscreen.
         * @returns {string|null}
         */
        getFullscreenImageId() {
            return _fullscreenImageId;
        },

        /**
         * Open fullscreen viewer with an image.
         * @param {string} imageId - Image ID to show
         */
        setFullscreenImageId(imageId) {
            if (_fullscreenImageId === imageId) return;

            console.log('[AppState.nav.setFullscreenImageId]', _fullscreenImageId, '->', imageId);
            _fullscreenImageId = imageId;
            // Track last viewed image so Gallery can select it on entry
            if (imageId) _lastViewedImageId = imageId;
            broadcast({ type: 'changed', property: 'fullscreenImageId' });
        },

        /**
         * Consume the last viewed fullscreen image ID.
         * Returns the ID and clears it (one-shot). Gallery calls this on
         * entry to select and scroll to the last viewed image.
         * @returns {string|null}
         */
        consumeLastViewedImageId() {
            const id = _lastViewedImageId;
            _lastViewedImageId = null;
            return id;
        },

        /**
         * Close fullscreen viewer.
         * Broadcasts 'fullscreenClosing' event with last image ID.
         */
        closeFullscreen() {
            if (_fullscreenImageId === null) return;

            const lastImageId = _fullscreenImageId;
            console.log('[AppState.nav.closeFullscreen] imageId:', lastImageId);

            broadcast({ type: 'changed', property: 'fullscreenClosing', imageId: lastImageId });
            _fullscreenImageId = null;
        },

        // --- Scroll Positions ---

        /**
         * Get saved scroll position for a screen.
         * @param {string} screen - Screen name
         * @returns {number} Scroll position (0 if not saved)
         */
        getScrollPosition(screen) {
            return _scrollPositions[screen] || 0;
        },

        /**
         * Save scroll position for a screen.
         * @param {string} screen - Screen name
         * @param {number} position - Scroll position
         */
        setScrollPosition(screen, position) {
            _scrollPositions[screen] = position;
        },

        /**
         * Clear all saved scroll positions.
         */
        clearScrollPositions() {
            _scrollPositions = {};
        },
    };
})();
