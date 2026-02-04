/**
 * AppState Selection Domain - Multi-Select State
 * ================================================
 *
 * Manages selection state across different contexts:
 * - Gallery image selection
 * - Duplicates image selection
 * - Faces selection
 * - Faces picker selection
 *
 * Each context has independent selection and anchor state.
 * Memory only (not persisted).
 *
 * @fileoverview Per-context selection state domain.
 */

'use strict';

AppState.selection = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Per-context selection state.
     * Keys: 'gallery', 'duplicates', 'faces', 'faces-pick'
     * @type {Map<string, {selected: Set<string>, anchor: string|null}>}
     */
    const _contexts = new Map();

    // =========================================================================
    // PRIVATE HELPERS
    // =========================================================================

    /**
     * Get or create context state.
     * @param {string} name - Context name
     * @returns {{selected: Set<string>, anchor: string|null}}
     * @private
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

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'selection',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to selection changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        // --- Accessors ---

        /**
         * Get selected IDs as array.
         * @param {string} context - Context name
         * @returns {string[]}
         */
        get(context) {
            return Array.from(getContext(context).selected);
        },

        /**
         * Get selected IDs as Set (for fast lookup).
         * @param {string} context - Context name
         * @returns {Set<string>}
         */
        getSet(context) {
            return getContext(context).selected;
        },

        /**
         * Get selection count.
         * @param {string} context - Context name
         * @returns {number}
         */
        getCount(context) {
            return getContext(context).selected.size;
        },

        /**
         * Check if an ID is selected.
         * @param {string} context - Context name
         * @param {string} id - ID to check
         * @returns {boolean}
         */
        has(context, id) {
            return getContext(context).selected.has(id);
        },

        /**
         * Get the anchor ID (last clicked for shift-select).
         * @param {string} context - Context name
         * @returns {string|null}
         */
        getAnchor(context) {
            return getContext(context).anchor;
        },

        // --- Mutations ---

        /**
         * Set selection to specific IDs (replaces existing).
         * @param {string} context - Context name
         * @param {string|string[]} ids - ID or array of IDs
         */
        set(context, ids) {
            const idArray = Array.isArray(ids) ? ids : [ids];
            const ctx = getContext(context);
            ctx.selected = new Set(idArray);
            ctx.anchor = idArray.length > 0 ? idArray[idArray.length - 1] : null;

            // Persist single gallery selections to localStorage for page reload
            if (context === 'gallery') {
                if (idArray.length === 1) {
                    localStorage.setItem('gallery.selectedImageId', idArray[0]);
                } else {
                    localStorage.removeItem('gallery.selectedImageId');
                }
            }

            broadcast({ type: 'changed', context });
        },

        /**
         * Add an ID to selection.
         * @param {string} context - Context name
         * @param {string} id - ID to add
         */
        add(context, id) {
            const ctx = getContext(context);
            ctx.selected.add(id);
            ctx.anchor = id;
            broadcast({ type: 'changed', context });
        },

        /**
         * Remove an ID from selection.
         * @param {string} context - Context name
         * @param {string} id - ID to remove
         */
        remove(context, id) {
            const ctx = getContext(context);
            ctx.selected.delete(id);
            broadcast({ type: 'changed', context });
        },

        /**
         * Toggle an ID's selection state.
         * @param {string} context - Context name
         * @param {string} id - ID to toggle
         */
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

        /**
         * Clear all selection.
         * @param {string} context - Context name
         */
        clear(context) {
            const ctx = getContext(context);
            if (ctx.selected.size === 0) return;
            ctx.selected.clear();
            ctx.anchor = null;

            // Clear persisted gallery selection
            if (context === 'gallery') {
                localStorage.removeItem('gallery.selectedImageId');
            }

            broadcast({ type: 'changed', context });
        },

        /**
         * Set the anchor without changing selection.
         * @param {string} context - Context name
         * @param {string} id - Anchor ID
         */
        setAnchor(context, id) {
            getContext(context).anchor = id;
        },

        /**
         * Select a range from anchor to target ID.
         * Used for shift-click selection.
         * @param {string} context - Context name
         * @param {Array} items - Array of items (with id property) or IDs
         * @param {string} toId - Target ID
         */
        selectRange(context, items, toId) {
            const ctx = getContext(context);
            if (!ctx.anchor || !toId) return;

            // Extract IDs from items
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
