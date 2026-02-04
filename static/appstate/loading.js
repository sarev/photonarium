/**
 * AppState Loading Domain - Loading Overlay Management
 * =====================================================
 *
 * Manages a shared loading overlay with ownership tracking.
 * Only the current owner can hide the overlay, preventing
 * race conditions between concurrent operations.
 *
 * @fileoverview Loading overlay state management.
 */

'use strict';

AppState.loading = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {string|null} Current owner of the loading overlay */
    let _owner = null;

    /** @type {string} Current loading message */
    let _message = 'Loading…';

    /** @type {boolean} Whether overlay is visible */
    let _visible = false;

    /** @type {HTMLElement|null} Loading overlay element */
    let _el = null;

    /** @type {HTMLElement|null} Loading message element */
    let _messageEl = null;

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    /**
     * Lazy-initialize DOM references.
     * @private
     */
    function ensureElements() {
        if (!_el) {
            _el = document.getElementById('loading-overlay');
            _messageEl = document.getElementById('loading-message');
        }
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'loading',

        /**
         * Subscribe to loading changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Show the loading overlay and take ownership.
         * If another owner has it, they receive a 'repurposed' event.
         *
         * @param {string} owner - Identifier (e.g., 'gallery', 'faces')
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

            // Notify if ownership changed
            if (previousOwner && previousOwner !== owner) {
                broadcast({ type: 'repurposed', previousOwner, newOwner: owner });
            }

            broadcast({ type: 'changed', visible: true, owner, message });
        },

        /**
         * Hide the loading overlay (only if caller is current owner).
         *
         * @param {string} owner - Identifier for the caller
         * @returns {boolean} True if hidden, false if caller wasn't owner
         */
        hide(owner) {
            if (_owner !== owner) {
                return false;
            }

            ensureElements();

            _owner = null;
            if (_el && _visible) {
                _el.classList.remove('visible');
                _visible = false;
            }

            broadcast({ type: 'changed', visible: false });
            return true;
        },

        /**
         * Force hide regardless of owner (use sparingly).
         */
        forceHide() {
            ensureElements();

            const previousOwner = _owner;
            _owner = null;
            if (_el && _visible) {
                _el.classList.remove('visible');
                _visible = false;
            }

            // Notify previous owner they were repurposed
            if (previousOwner) {
                broadcast({ type: 'repurposed', previousOwner, newOwner: null });
            }
            broadcast({ type: 'changed', visible: false });
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

        /**
         * Check if loading overlay is visible.
         * @returns {boolean}
         */
        isVisible() {
            return _visible;
        },

        /**
         * Get current owner.
         * @returns {string|null}
         */
        getOwner() {
            return _owner;
        },

        /**
         * Get current message.
         * @returns {string}
         */
        getMessage() {
            return _message;
        }
    };
})();
