/**
 * @fileoverview Shared thumbnail infrastructure for the Photonarium application.
 *
 * This module provides reusable components for thumbnail grid management:
 * - ThumbnailLoader: Fetches thumbnails with scroll-aware prioritization
 * - VirtualGrid: Virtual scrolling with absolute positioning
 * - GridSelection: Unified selection handling (click, keyboard, drag-box)
 *
 * ARCHITECTURE:
 * - DOM elements are only created AFTER their thumbnail blob URL is ready
 * - Items are absolutely positioned based on their index (no insertion order dependency)
 * - One unified buffer zone: visible rows ± extraRows (from ThumbnailConfig)
 * - Elements are destroyed when they leave this zone
 * - Priority is determined by absolute distance from the center of the visible area
 * - Re-prioritization happens each time a fetch slot becomes available
 * - A faint grid pattern shows placeholder positions during scroll
 *
 * INTEGRATION GUIDE:
 * To integrate VirtualGrid into a screen:
 *
 * 1. Create VirtualGrid with config (container, getItems, createItem, etc.)
 * 2. Call render() to populate the grid
 * 3. Create GridSelection with the grid instance
 * 4. Call grid.bind() and selection.bind() when screen becomes active
 * 5. Call grid.unbind() and selection.unbind() when screen becomes inactive
 * 6. Call grid.destroy() and selection.destroy() when recreating the grid
 *
 * CRITICAL GOTCHAS (learned from integration bugs):
 *
 * 1. UNBIND BEFORE DESTROYING DOM:
 *    If you clear the container's innerHTML without calling unbind() first,
 *    scroll listeners become orphaned and continue firing on the old container.
 *    This causes errors and memory leaks. Always: unbind() → destroy() → clear DOM.
 *
 * 2. HIDDEN CONTAINERS HAVE ZERO DIMENSIONS:
 *    When a screen is hidden (display:none or behind overlay like fullscreen),
 *    the container has clientWidth/clientHeight of 0. Calling render() will
 *    fail dimension calculation and defer via RAF. If the grid is unbound,
 *    it won't retry. If bound but hidden indefinitely, it was causing an
 *    infinite RAF loop (fixed with _bound checks). Best practice: don't call
 *    render() on hidden containers - set a needsRefresh flag instead.
 *
 * 3. BIND TRIGGERS _updateVisibleItems:
 *    bind() now calls _updateVisibleItems() to ensure thumbnails load.
 *    Previously, returning to a screen would show a blank grid until scroll.
 *
 * 4. SCROLL CONTAINER MUST BE THE CONFIG.CONTAINER:
 *    VirtualGrid attaches scroll listeners to config.container. If your
 *    layout has nested scroll containers (e.g., outer page scroll + inner
 *    grid scroll), ensure config.container is the actual scrolling element.
 *    Faces screen had this issue: .faces-grid was scrolling but VirtualGrid
 *    was listening on a different element.
 *
 * 5. BLOB URL MEMORY LEAK:
 *    Each thumbnail fetch creates a blob URL via URL.createObjectURL().
 *    These MUST be revoked when the item scrolls out of view or on destroy.
 *    renderedItems Map stores {el, blobUrl} to track both. If you only
 *    stored the element, blob URLs would leak and memory would grow
 *    during long browsing sessions.
 *
 * 6. SELECTION STATE PERSISTS ACROSS UNBIND/BIND:
 *    GridSelection keeps its _selected Set when unbound. This is intentional
 *    so selection persists when switching screens. But if you recreate the
 *    grid with new data, you may need to call selection.clear().
 *
 * 7. KEYBOARD HANDLER IS ON DOCUMENT:
 *    GridSelection's keydown handler is attached to document, not the grid.
 *    This means it captures keys even when focus is elsewhere. The handler
 *    checks if the grid's screen is active before processing. If you have
 *    multiple grids, ensure only the active one has selection bound.
 *
 * 8. O(n) LOOKUPS DURING SCROLL:
 *    Early versions had O(r × n) scroll performance where r = rendered items
 *    and n = total items. Fixed by building id→index Map once per scroll
 *    update. If you modify _updateVisibleItems, maintain this optimization.
 *
 * @module thumbnails
 * @requires core
 */

/* ==========================================================================
   THUMBNAIL LOADER

   Fetches thumbnails and triggers DOM creation via callbacks.
   DOM elements are only created after the blob URL is ready.
   ========================================================================== */

/**
 * Thumbnail loader with scroll-aware prioritization.
 *
 * FEATURES:
 * - Real-time prioritization: Sorts queue based on current scroll position
 * - Automatic pruning: Discards requests outside buffer zone
 * - Timeout protection: Aborts slow requests to free slots
 * - Scroll-abort: Cancels in-flight requests when user scrolls away
 * - Cache busting: Support for rotated images
 * - Callback-based: Calls onReady callback with blob URL when fetch completes
 *
 * HOW IT WORKS:
 * 1. VirtualGrid calls request(imageId, index, onReady) for items entering buffer
 * 2. Request is added to queue, sorted by distance from visible area center
 * 3. Loader fetches up to N concurrent requests (from config)
 * 4. On success: calls onReady(blobUrl), item creates DOM element
 * 5. On scroll: items leaving buffer are pruned from queue, in-flight aborted
 *
 * SINGLETON: There's one global ThumbnailLoader shared by all grids.
 * Call clear() when switching screens to cancel pending requests.
 *
 * @namespace
 */
const ThumbnailLoader = {
    /**
     * Pending request queue. Each entry: { imageId, index, onReady }
     * Sorted by priority (distance from visible center) on each processQueue.
     */
    _queue: [],

    /**
     * In-flight requests. Map of imageId → { controller: AbortController, index }
     * Used for aborting requests when items scroll out of view.
     */
    _inFlight: new Map(),

    /**
     * Cache-bust timestamps. Map of imageId → timestamp.
     * Set when image is rotated. Appended to URL as ?t=timestamp.
     * Cleared never (small memory footprint, only for rotated images).
     */
    _cacheBust: new Map(),

    /** Current number of active fetches (for concurrency limiting). */
    _activeCount: 0,

    /**
     * Current scroll state for prioritization.
     * Updated by VirtualGrid on each scroll via updateScrollState().
     * Used to calculate priority (distance from visible center).
     */
    _scrollState: {
        itemsPerRow: 1,
        visibleStartRow: 0,
        visibleEndRow: 0,
    },

    /**
     * Gets the current configuration from App.
     * @returns {Object} Config with concurrentRequests, extraRows, timeoutMs
     * @private
     */
    _getConfig() {
        return App.getThumbnailConfig();
    },

    /**
     * Updates the scroll state for prioritization.
     * Called by VirtualGrid on scroll.
     *
     * @param {number} itemsPerRow - Number of items per row
     * @param {number} visibleStartRow - First visible row index
     * @param {number} visibleEndRow - Last visible row index
     */
    updateScrollState(itemsPerRow, visibleStartRow, visibleEndRow) {
        this._scrollState = {
            itemsPerRow,
            visibleStartRow,
            visibleEndRow,
        };

        // Prune in-flight requests that are now outside buffer zone
        this._pruneInFlight();
    },

    /**
     * Requests a thumbnail load for an image.
     * When the thumbnail is ready, onReady(blobUrl) is called.
     *
     * @param {string} imageId - The image ID to load
     * @param {number} index - Index in the items array (for row calculation)
     * @param {Function} onReady - Callback: (blobUrl) => void, called when thumbnail is ready
     * @param {Function} [urlBuilder] - Optional custom URL builder: (id) => string
     */
    request(imageId, index, onReady, urlBuilder) {
        if (!imageId || !onReady) return;

        // Already fetching this image
        if (this._inFlight.has(imageId)) return;

        // Already in queue
        if (this._queue.some(item => item.imageId === imageId)) return;

        // Add to queue
        this._queue.push({ imageId, index, onReady, urlBuilder });

        // Process queue
        this._processQueue();
    },

    /**
     * Marks an image as needing cache-bust (e.g., after rotation).
     *
     * @param {string} imageId - The image ID that was modified
     */
    bustCache(imageId) {
        if (!imageId) return;
        this._cacheBust.set(imageId, Date.now());
    },

    /**
     * Gets the full image URL with cache-bust if needed.
     * Use this for fullscreen viewer to ensure rotated images are refreshed.
     *
     * @param {string} imageId - The image ID
     * @returns {string} The full image URL, with cache-bust parameter if needed
     */
    getFullImageUrl(imageId) {
        let url = App.imageUrl(imageId);
        const bustTime = this._cacheBust.get(imageId);
        if (bustTime) {
            url += (url.includes('?') ? '&' : '?') + '_t=' + bustTime;
        }
        return url;
    },

    /**
     * Gets the thumbnail URL for an image, with cache-bust if needed.
     *
     * @param {string} imageId - The image ID
     * @returns {string} The thumbnail URL
     * @private
     */
    _getThumbnailUrl(imageId) {
        let url = App.thumbnailUrl(imageId);
        const bustTime = this._cacheBust.get(imageId);
        if (bustTime) {
            url += (url.includes('?') ? '&' : '?') + '_t=' + bustTime;
        }
        return url;
    },

    /**
     * Calculates the row for an item index.
     * @param {number} index - Item index
     * @returns {number} Row number
     * @private
     */
    _getRow(index) {
        return Math.floor(index / this._scrollState.itemsPerRow);
    },

    /**
     * Checks if a row is within the buffer zone.
     * @param {number} row - Row number
     * @returns {boolean} True if in buffer zone
     * @private
     */
    _isInBuffer(row) {
        const { visibleStartRow, visibleEndRow } = this._scrollState;
        const extraRows = this._getConfig().extraRows;
        return row >= (visibleStartRow - extraRows) && row <= (visibleEndRow + extraRows);
    },

    /**
     * Checks if a row is in the visible zone.
     * @param {number} row - Row number
     * @returns {boolean} True if visible
     * @private
     */
    _isVisible(row) {
        const { visibleStartRow, visibleEndRow } = this._scrollState;
        return row >= visibleStartRow && row <= visibleEndRow;
    },

    /**
     * Prunes in-flight requests that are now outside the buffer zone.
     * Called when scroll state changes.
     * @private
     */
    _pruneInFlight() {
        for (const [imageId, { controller, index }] of this._inFlight) {
            const row = this._getRow(index);
            if (!this._isInBuffer(row)) {
                controller.abort();
                // Note: Don't delete from map here - finally block handles cleanup
            }
        }
    },

    /**
     * Processes the queue, filling available slots with highest priority items.
     * Re-prioritizes on each iteration based on current scroll position.
     * @private
     */
    _processQueue() {
        const config = this._getConfig();
        const { visibleStartRow, visibleEndRow } = this._scrollState;

        // Calculate center of visible area for distance calculation
        const centerRow = (visibleStartRow + visibleEndRow) / 2;

        // Prune items outside buffer zone
        this._queue = this._queue.filter(item => {
            const row = this._getRow(item.index);
            return this._isInBuffer(row);
        });

        // Fill available slots, re-prioritizing on each iteration
        while (this._activeCount < config.concurrentRequests && this._queue.length > 0) {
            // Find the item closest to center (minimum absolute distance)
            let bestIndex = 0;
            let bestDistance = Infinity;

            for (let i = 0; i < this._queue.length; i++) {
                const row = this._getRow(this._queue[i].index);
                const distance = Math.abs(row - centerRow);
                if (distance < bestDistance) {
                    bestDistance = distance;
                    bestIndex = i;
                }
            }

            // Remove best item from queue and load it
            const [item] = this._queue.splice(bestIndex, 1);
            this._loadThumbnail(item);
        }
    },

    /**
     * Loads a thumbnail for a queue item.
     *
     * @param {Object} item - Queue item with imageId, index, onReady, urlBuilder
     * @private
     */
    async _loadThumbnail(item) {
        const { imageId, index, onReady, urlBuilder } = item;
        const config = this._getConfig();

        // Create abort controller with timeout
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), config.timeoutMs);

        // Track as in-flight
        this._inFlight.set(imageId, { controller, index });
        this._activeCount++;

        // Use custom URL builder if provided, otherwise default
        const url = urlBuilder ? urlBuilder(imageId) : this._getThumbnailUrl(imageId);

        try {
            const response = await fetch(url, {
                signal: controller.signal,
            });

            clearTimeout(timeoutId);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const blob = await response.blob();
            const blobUrl = URL.createObjectURL(blob);

            // Check if still in buffer zone before calling callback
            const row = this._getRow(index);
            if (this._isInBuffer(row)) {
                onReady(blobUrl);
            } else {
                // Clean up blob URL if we're no longer in the zone
                URL.revokeObjectURL(blobUrl);
            }

        } catch (error) {
            clearTimeout(timeoutId);

            if (error.name !== 'AbortError') {
                console.error(`Failed to load thumbnail for ${imageId}:`, error);
                // Notify callback of failure so it can clean up pending state
                onReady(null);
            }
            // AbortError is expected (timeout or scroll-away) - don't notify
        } finally {
            this._inFlight.delete(imageId);
            this._activeCount--;
            this._processQueue();
        }
    },

    /**
     * Clears all pending requests and resets state.
     * Called when switching screens or doing a full refresh.
     */
    clear() {
        // Cancel all in-flight requests
        for (const [imageId, { controller }] of this._inFlight) {
            controller.abort();
        }

        this._queue = [];
        this._inFlight.clear();
        this._activeCount = 0;
        // Keep _cacheBust - those are still valid
    },

    /**
     * Gets statistics about the loader state (for debugging).
     * @returns {Object} Stats object
     */
    getStats() {
        return {
            queueLength: this._queue.length,
            inFlightCount: this._inFlight.size,
            activeCount: this._activeCount,
            cacheBustCount: this._cacheBust.size,
        };
    },
};

// Make ThumbnailLoader available globally
window.ThumbnailLoader = ThumbnailLoader;


/* ==========================================================================
   VIRTUAL GRID

   Virtual scrolling with absolute positioning.

   The grid container has a fixed total height based on total items.
   Each thumbnail is absolutely positioned at its calculated location.
   Elements don't depend on each other - they can load in any order.

   LIFECYCLE:
     create()  → Instance created, resize listener attached
     render()  → Clears DOM, calculates layout, requests visible thumbnails
     bind()    → Attaches scroll listener, triggers thumbnail loading
     unbind()  → Detaches scroll listener, cancels pending RAF
     destroy() → Full cleanup: listeners, blob URLs, DOM, state

   TYPICAL USAGE:
     // On screen enter:
     grid = VirtualGrid.create({...});
     grid.render();
     grid.bind();

     // On screen leave:
     grid.unbind();

     // When recreating with new data:
     grid.unbind();
     grid.destroy();
     grid = VirtualGrid.create({...});
     grid.render();
     grid.bind();

   GOTCHA: render() calls bind() internally if successful. If you call render()
   then bind(), you'll double-attach scroll listeners. Check the flow carefully.
   ========================================================================== */

/**
 * Factory for creating virtual scrolling grid instances.
 *
 * VirtualGrid uses absolute positioning for all items. The container has a fixed
 * total height based on the number of items, and each item is positioned at a
 * calculated (top, left) based on its index. Items can load in any order without
 * affecting each other's positions.
 *
 * The grid shows a faint placeholder pattern during scroll, so users see visual
 * feedback even before thumbnails load.
 *
 * @namespace
 */
const VirtualGrid = {
    /**
     * Creates a new VirtualGrid instance.
     *
     * @param {Object} config - Configuration object
     * @param {HTMLElement} config.container - Scroll container element (overflow-y: auto)
     * @param {Function} config.getItems - Returns current data array
     * @param {Function} config.getItemId - Extracts unique ID from an item: (item) => string
     * @param {Function} config.createItem - Creates DOM element when thumbnail ready: (item, index, blobUrl) => HTMLElement
     * @param {Function} config.getThumbnailId - Gets thumbnail imageId from item: (item) => string
     * @param {string} [config.itemSelector='.grid-item'] - CSS selector for items (used by GridSelection)
     * @param {number} [config.gap=16] - Gap between items in pixels
     * @param {number} [config.padding=16] - Container padding in pixels
     * @param {Function} [config.getItemHeight] - Custom item height calculator: (thumbSize, itemWidth) => height
     * @param {Function} [config.onItemCreated] - Called when item is added to DOM: (id, element) => void
     * @param {Function} [config.getThumbnailUrl] - Custom URL builder: (thumbId) => string (defaults to App.thumbnailUrl)
     * @param {Function} [config.getThumbSize] - Custom thumbnail size getter: () => number (defaults to App.getThumbnailSize)
     * @returns {Object} VirtualGrid instance
     */
    create(config) {
        const instance = {
            // Configuration
            _config: {
                container: config.container,
                getItems: config.getItems,
                getItemId: config.getItemId,
                createItem: config.createItem,
                getThumbnailId: config.getThumbnailId,
                itemSelector: config.itemSelector || '.grid-item',
                gap: config.gap ?? 16,
                padding: config.padding ?? 16,
                getItemHeight: config.getItemHeight || null,
                onItemCreated: config.onItemCreated || null,
                getThumbnailUrl: config.getThumbnailUrl || null,
                getThumbSize: config.getThumbSize || null,
            },

            // ---------------------------------------------------------------
            // LAYOUT STATE
            // ---------------------------------------------------------------
            // Recalculated on render() and resize. Used by position calculations
            // and buffer zone determination.
            _state: {
                itemHeight: 0,          // Height including gap (for row calculations)
                itemWidth: 0,           // Width of each item
                itemsPerRow: 0,         // Number of columns
                visibleRows: 0,         // Rows visible in viewport + 1
                totalHeight: 0,         // Total scrollable height (for scroll indicator)

                // RENDERED ITEMS MAP: id → {el: HTMLElement, blobUrl: string}
                // Stores both element and blob URL for proper cleanup.
                // CRITICAL: Blob URLs MUST be revoked when items are removed,
                // otherwise they leak memory during long browsing sessions.
                renderedItems: new Map(),

                // PENDING ITEMS: IDs that have thumbnail requests in flight.
                // Used to avoid duplicate requests for the same item.
                pendingItems: new Set(),

                lastScrollProcess: 0,    // Timestamp for scroll throttle
            },

            // Inner container: positioned relative, contains all absolutely
            // positioned items. Has background grid pattern as placeholder.
            _innerContainer: null,

            // Event handlers (stored for cleanup)
            _scrollHandler: null,
            _resizeHandler: null,
            _trailingScrollTimeout: null,
            _pendingRenderFrame: null,  // Track pending RAF for render retry
            _bound: false,

            /**
             * Initialises the virtual grid.
             */
            _init() {
                // Create inner container for absolute positioning
                this._innerContainer = document.createElement('div');
                this._innerContainer.className = 'virtual-grid-inner';
                this._innerContainer.style.position = 'relative';

                // Bind handlers
                this._scrollHandler = this._onScroll.bind(this);
                this._resizeHandler = App.debounce(() => {
                    if (this._bound && this._config.getItems().length > 0) {
                        this._onResize();
                    }
                }, 100);

                window.addEventListener('resize', this._resizeHandler);
            },

            /**
             * Handles scroll events with throttling and trailing call.
             * Ensures the final scroll position is always processed.
             * @param {Event} e - Scroll event
             * @private
             */
            _onScroll(e) {
                const now = Date.now();
                const throttleMs = App.getThumbnailConfig().scrollThrottleMs;
                const scrollTop = e.target.scrollTop;

                // Clear any pending trailing call
                if (this._trailingScrollTimeout) {
                    clearTimeout(this._trailingScrollTimeout);
                    this._trailingScrollTimeout = null;
                }

                // If within throttle window, schedule a trailing call
                if (now - this._state.lastScrollProcess < throttleMs) {
                    this._trailingScrollTimeout = setTimeout(() => {
                        this._trailingScrollTimeout = null;
                        this._state.lastScrollProcess = Date.now();
                        this._updateVisibleItems(this._config.container.scrollTop);
                    }, throttleMs);
                    return;
                }

                // Process immediately
                this._state.lastScrollProcess = now;
                this._updateVisibleItems(scrollTop);
            },

            /**
             * Handles resize - recalculates layout and repositions all items.
             * @private
             */
            _onResize() {
                const oldItemsPerRow = this._state.itemsPerRow;
                const oldItemWidth = this._state.itemWidth;
                const oldItemHeight = this._state.itemHeight;

                // Skip if container has no dimensions
                if (!this._calculateDimensions()) {
                    return;
                }

                // Update container height and grid pattern
                this._innerContainer.style.height = this._state.totalHeight + 'px';
                this._updateGridPattern();

                // Reposition all rendered items if layout changed
                const layoutChanged = oldItemsPerRow !== this._state.itemsPerRow ||
                                      oldItemWidth !== this._state.itemWidth ||
                                      oldItemHeight !== this._state.itemHeight;
                if (layoutChanged && this._state.renderedItems.size > 0) {
                    const items = this._config.getItems();
                    // Build id->index map once for O(1) lookups
                    const idToIndex = new Map();
                    for (let i = 0; i < items.length; i++) {
                        idToIndex.set(this._config.getItemId(items[i]), i);
                    }
                    for (const [id, {el}] of this._state.renderedItems) {
                        const index = idToIndex.get(id);
                        if (index !== undefined) {
                            this._positionElement(el, index);
                        }
                    }
                }

                // Update visible items (may need to load more or remove some)
                this._updateVisibleItems(this._config.container.scrollTop);
            },

            /**
             * Updates the background grid pattern to match current cell dimensions.
             * Shows a faint grid of rounded rectangles as placeholders.
             * @private
             */
            _updateGridPattern() {
                const { itemWidth, itemHeight } = this._state;
                const { gap, padding } = this._config;

                const cellWidth = itemWidth;
                const cellHeight = itemHeight - gap;
                const tileWidth = itemWidth + gap;
                const tileHeight = itemHeight;

                // Get colors from CSS custom properties (use dedicated placeholder vars for subtlety)
                const style = getComputedStyle(document.documentElement);
                const fillColor = style.getPropertyValue('--color-grid-placeholder-fill').trim() || 'rgba(128,128,128,0.04)';
                const strokeColor = style.getPropertyValue('--color-grid-placeholder-stroke').trim() || 'rgba(128,128,128,0.06)';

                // Create SVG pattern for one cell - a faint rounded rectangle
                const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${tileWidth}" height="${tileHeight}">
                    <rect x="0" y="0" width="${cellWidth}" height="${cellHeight}" rx="8"
                          fill="${fillColor}" stroke="${strokeColor}" stroke-width="1"/>
                </svg>`;

                const encoded = encodeURIComponent(svg)
                    .replace(/'/g, '%27')
                    .replace(/"/g, '%22');

                this._innerContainer.style.backgroundImage = `url("data:image/svg+xml,${encoded}")`;
                this._innerContainer.style.backgroundPosition = `${padding}px ${padding}px`;
                this._innerContainer.style.backgroundRepeat = 'repeat';
            },

            /**
             * Calculates dimensions for the grid layout.
             * @returns {boolean} True if dimensions are valid, false if container has no size
             * @private
             */
            _calculateDimensions() {
                const container = this._config.container;
                const thumbSize = this._config.getThumbSize ? this._config.getThumbSize() : App.getThumbnailSize();
                const gap = this._config.gap;
                const padding = this._config.padding;

                // Check for zero-size container (can happen during screen transitions)
                if (container.clientWidth === 0 || container.clientHeight === 0) {
                    return false;
                }

                // Calculate available width (account for scrollbar)
                const scrollbarWidth = container.offsetWidth - container.clientWidth;
                const availableWidth = container.clientWidth - padding * 2;

                // Calculate items per row
                this._state.itemsPerRow = Math.max(1, Math.floor((availableWidth + gap) / (thumbSize + gap)));

                // Actual item width (rounded to avoid floating point accumulation)
                const actualItemWidth = Math.floor((availableWidth - gap * (this._state.itemsPerRow - 1)) / this._state.itemsPerRow);
                this._state.itemWidth = Math.max(thumbSize, actualItemWidth); // Ensure minimum size

                // Item height - use custom calculator if provided (also rounded)
                if (this._config.getItemHeight) {
                    this._state.itemHeight = Math.floor(this._config.getItemHeight(thumbSize, this._state.itemWidth)) + gap;
                } else {
                    // Default: square thumbnail + label area
                    const thumbnailHeight = this._state.itemWidth - 16;
                    const labelHeight = 24;
                    this._state.itemHeight = thumbnailHeight + labelHeight + 16 + gap;
                }

                // Calculate visible rows
                const containerHeight = container.clientHeight;
                this._state.visibleRows = Math.ceil(containerHeight / this._state.itemHeight) + 1;

                // Calculate total height
                const items = this._config.getItems();
                const totalRows = Math.ceil(items.length / this._state.itemsPerRow);
                this._state.totalHeight = totalRows * this._state.itemHeight + padding;

                return true;
            },

            /**
             * Calculates the absolute position for an item by index.
             * @param {number} index - Item index
             * @returns {Object} { top, left }
             * @private
             */
            _getItemPosition(index) {
                const { itemsPerRow, itemWidth, itemHeight } = this._state;
                const { gap, padding } = this._config;

                const row = Math.floor(index / itemsPerRow);
                const col = index % itemsPerRow;

                const top = padding + row * itemHeight;
                const left = padding + col * (itemWidth + gap);

                return { top, left };
            },

            /**
             * Positions an element at the correct location.
             * @param {HTMLElement} el - Element to position
             * @param {number} index - Item index
             * @private
             */
            _positionElement(el, index) {
                const { top, left } = this._getItemPosition(index);
                const { itemWidth, itemHeight } = this._state;
                const { gap } = this._config;

                el.style.position = 'absolute';
                el.style.top = top + 'px';
                el.style.left = left + 'px';
                el.style.width = itemWidth + 'px';
                el.style.height = (itemHeight - gap) + 'px';
            },

            /**
             * Gets the buffer zone boundaries (visible + extraRows).
             * @param {number} firstVisibleRow - First visible row index
             * @param {number} totalRows - Total number of rows
             * @returns {Object} bufferStartRow, bufferEndRow
             * @private
             */
            _getBufferZone(firstVisibleRow, totalRows) {
                const extraRows = App.getThumbnailConfig().extraRows;
                const bufferStartRow = Math.max(0, firstVisibleRow - extraRows);
                const bufferEndRow = Math.min(totalRows, firstVisibleRow + this._state.visibleRows + extraRows);
                return { bufferStartRow, bufferEndRow };
            },

            /**
             * Updates visible items based on scroll position.
             * Requests thumbnails for items in buffer zone, removes items outside.
             * @param {number} scrollTop - Current scroll position
             * @private
             */
            _updateVisibleItems(scrollTop) {
                const state = this._state;
                const config = this._config;
                const items = config.getItems();

                if (items.length === 0) return;

                const totalRows = Math.ceil(items.length / state.itemsPerRow);
                const firstVisibleRow = Math.floor(scrollTop / state.itemHeight);

                // Buffer zone: visible + extraRows
                const { bufferStartRow, bufferEndRow } = this._getBufferZone(firstVisibleRow, totalRows);

                // Visible zone boundaries (for ThumbnailLoader priority)
                const visibleStartRow = firstVisibleRow;
                const visibleEndRow = Math.min(totalRows - 1, firstVisibleRow + state.visibleRows - 1);

                // Update ThumbnailLoader scroll state FIRST
                ThumbnailLoader.updateScrollState(state.itemsPerRow, visibleStartRow, visibleEndRow);

                // Convert to item indices
                const bufferStart = bufferStartRow * state.itemsPerRow;
                const bufferEnd = Math.min(bufferEndRow * state.itemsPerRow, items.length);

                // Build id->index map once for O(1) lookups (avoids O(n²) findIndex in loops)
                const idToIndex = new Map();
                for (let i = 0; i < items.length; i++) {
                    idToIndex.set(config.getItemId(items[i]), i);
                }

                // Build set of indices that should be in buffer zone
                const bufferIndices = new Set();
                for (let i = bufferStart; i < bufferEnd; i++) {
                    bufferIndices.add(i);
                }

                // Remove items outside buffer zone and revoke their blob URLs
                for (const [id, {el, blobUrl}] of state.renderedItems) {
                    const index = idToIndex.get(id);
                    if (index === undefined || !bufferIndices.has(index)) {
                        URL.revokeObjectURL(blobUrl);
                        el.remove();
                        state.renderedItems.delete(id);
                    }
                }

                // Clean up pending items outside buffer zone
                for (const id of state.pendingItems) {
                    const index = idToIndex.get(id);
                    if (index === undefined || !bufferIndices.has(index)) {
                        state.pendingItems.delete(id);
                    }
                }

                // Request thumbnails for items in buffer zone that aren't rendered or pending
                for (let i = bufferStart; i < bufferEnd; i++) {
                    const item = items[i];
                    const id = config.getItemId(item);

                    // Skip if already rendered or pending
                    if (state.renderedItems.has(id) || state.pendingItems.has(id)) continue;

                    const thumbId = config.getThumbnailId(item);

                    // No thumbnail available (e.g. empty custom group) —
                    // create the element immediately with a null blobUrl
                    // so the caller can render a placeholder.
                    if (!thumbId) {
                        const el = config.createItem(item, i, null);
                        this._positionElement(el, i);
                        this._innerContainer.appendChild(el);
                        state.renderedItems.set(id, {el, blobUrl: null});
                        if (config.onItemCreated) {
                            config.onItemCreated(id, el);
                        }
                        continue;
                    }

                    // Mark as pending
                    state.pendingItems.add(id);

                    // Capture index for closure
                    const itemIndex = i;

                    // Request thumbnail with callback that creates DOM element
                    // Pass custom URL builder if configured
                    ThumbnailLoader.request(thumbId, i, (blobUrl) => {
                        // Remove from pending
                        state.pendingItems.delete(id);

                        // Handle failed thumbnail (null blobUrl)
                        if (!blobUrl) {
                            return;
                        }

                        // Check if still in buffer zone
                        const currentItems = config.getItems();
                        const currentIndex = currentItems.findIndex(it => config.getItemId(it) === id);
                        if (currentIndex === -1) {
                            URL.revokeObjectURL(blobUrl);
                            return;
                        }

                        // Already rendered? (safety check)
                        if (state.renderedItems.has(id)) {
                            URL.revokeObjectURL(blobUrl);
                            return;
                        }

                        // Create DOM element with the blob URL
                        const el = config.createItem(item, currentIndex, blobUrl);

                        // Position it absolutely
                        this._positionElement(el, currentIndex);

                        // Add to container and track (store blobUrl for cleanup)
                        this._innerContainer.appendChild(el);
                        state.renderedItems.set(id, {el, blobUrl});

                        // Notify that item was created (for selection state sync)
                        if (config.onItemCreated) {
                            config.onItemCreated(id, el);
                        }
                    }, config.getThumbnailUrl);
                }
            },

            /**
             * Attaches scroll listener.
             * @private
             */
            _attachScrollListener() {
                const container = this._config.container;
                if (container && this._scrollHandler) {
                    container.removeEventListener('scroll', this._scrollHandler);
                    container.addEventListener('scroll', this._scrollHandler, { passive: true });
                }
            },

            /**
             * Detaches scroll listener.
             * @private
             */
            _detachScrollListener() {
                const container = this._config.container;
                if (container && this._scrollHandler) {
                    container.removeEventListener('scroll', this._scrollHandler);
                }
            },

            // ==================== Public API ====================

            /**
             * Performs a full render of the grid.
             *
             * WHAT IT DOES:
             * 1. Saves scroll position (innerHTML='' can reset it in some browsers)
             * 2. Revokes all blob URLs to prevent memory leaks
             * 3. Clears container DOM and internal state
             * 4. Calculates layout dimensions
             * 5. Creates inner container with placeholder grid pattern
             * 6. Restores scroll position (clamped to new max)
             * 7. Requests thumbnails for visible items
             * 8. Attaches scroll listener and sets _bound = true
             *
             * GOTCHA: If container has zero dimensions (hidden screen), dimension
             * calculation fails. In this case, render() defers via RAF and will
             * retry when container becomes visible. The RAF retry only happens if
             * _bound is true, preventing infinite loops on permanently hidden grids.
             *
             * IMPORTANT: render() calls bind() internally. Don't call bind() after
             * render() or you'll double-attach scroll listeners.
             */
            render() {
                const container = this._config.container;
                const items = this._config.getItems();

                // Save scroll position (innerHTML='' resets scrollTop in some browsers)
                const savedScrollTop = container.scrollTop;

                // CRITICAL: Revoke blob URLs to prevent memory leak
                for (const [, {blobUrl}] of this._state.renderedItems) {
                    URL.revokeObjectURL(blobUrl);
                }

                // Clear DOM and state
                container.innerHTML = '';
                this._state.renderedItems.clear();
                this._state.pendingItems.clear();

                // Handle empty state
                if (items.length === 0) {
                    return;
                }

                // Calculate dimensions - may fail if container not yet laid out
                if (!this._calculateDimensions()) {
                    // Defer render until container has dimensions (only if still bound)
                    if (!this._pendingRenderFrame && this._bound) {
                        this._pendingRenderFrame = requestAnimationFrame(() => {
                            this._pendingRenderFrame = null;
                            if (this._bound) {
                                this.render();
                            }
                        });
                    }
                    return;
                }

                // Set up inner container with total height and grid pattern
                this._innerContainer.innerHTML = '';
                this._innerContainer.style.height = this._state.totalHeight + 'px';
                this._updateGridPattern();
                container.appendChild(this._innerContainer);

                // Restore scroll position (clamped to new max)
                const maxScroll = Math.max(0, this._state.totalHeight - container.clientHeight);
                container.scrollTop = Math.min(savedScrollTop, maxScroll);

                // Request thumbnails for visible items
                this._updateVisibleItems(container.scrollTop);

                // Attach scroll listener
                this._attachScrollListener();
                this._bound = true;
            },

            /**
             * Refreshes the grid - recalculates layout.
             */
            refresh() {
                if (!this._bound) return;
                this._onResize();
            },

            /**
             * Invalidate a rendered item so its thumbnail is re-fetched.
             * Revokes the stale blob URL, removes the DOM element, and
             * triggers an update so the item is re-requested from the
             * ThumbnailLoader with a fresh URL.
             * @param {string} id - Item ID to invalidate
             */
            invalidateItem(id) {
                const state = this._state;
                if (!state) return;
                const entry = state.renderedItems.get(id);
                if (entry) {
                    if (entry.blobUrl) URL.revokeObjectURL(entry.blobUrl);
                    entry.el.remove();
                    state.renderedItems.delete(id);
                }
                state.pendingItems.delete(id);
                if (this._bound) {
                    this._updateVisibleItems(this._config.container.scrollTop);
                }
            },

            /**
             * Scrolls to show item at given index.
             * @param {number} index - Index in items array
             * @param {string} [behavior='smooth'] - Scroll behavior
             */
            scrollTo(index, behavior = 'smooth') {
                const container = this._config.container;
                const items = this._config.getItems();

                if (index < 0 || index >= items.length) return;

                const { top } = this._getItemPosition(index);

                // Check if item is already visible
                const viewTop = container.scrollTop;
                const viewBottom = viewTop + container.clientHeight;
                const itemBottom = top + this._state.itemHeight;

                if (top < viewTop) {
                    container.scrollTo({ top: top - this._config.padding, behavior });
                } else if (itemBottom > viewBottom) {
                    container.scrollTo({ top: itemBottom - container.clientHeight + this._config.padding, behavior });
                }
            },

            /**
             * Scrolls to show item with given ID.
             * @param {string} id - Item ID
             * @param {string} [behavior='smooth'] - Scroll behavior
             */
            scrollToId(id, behavior = 'smooth') {
                const items = this._config.getItems();
                const index = items.findIndex(item => this._config.getItemId(item) === id);
                if (index !== -1) {
                    this.scrollTo(index, behavior);
                }
            },

            /**
             * Gets the number of items per row.
             * @returns {number} Items per row
             */
            getItemsPerRow() {
                return this._state.itemsPerRow;
            },

            /**
             * Gets the item height including gap.
             * @returns {number} Item height
             */
            getItemHeight() {
                return this._state.itemHeight;
            },

            /**
             * Gets the number of visible rows.
             * @returns {number} Visible rows
             */
            getVisibleRows() {
                return this._state.visibleRows;
            },

            /**
             * Gets the current scroll offset.
             * @returns {number} Scroll offset in pixels
             */
            getScrollOffset() {
                return this._config.container?.scrollTop ?? 0;
            },

            /**
             * Sets the scroll offset.
             * @param {number} offset - Scroll offset in pixels
             */
            setScrollOffset(offset) {
                if (this._config.container) {
                    this._config.container.scrollTop = offset;
                }
            },

            /**
             * Gets the rendered item element for an ID.
             * @param {string} id - Item ID
             * @returns {HTMLElement|null} Element or null if not rendered
             */
            getRenderedElement(id) {
                const item = this._state.renderedItems.get(id);
                return item ? item.el : null;
            },

            /**
             * Updates visual state for a rendered item (e.g., selection).
             * @param {string} id - Item ID
             * @param {string} className - Class to toggle
             * @param {boolean} state - Add or remove
             */
            setItemClass(id, className, state) {
                const item = this._state.renderedItems.get(id);
                if (item) {
                    item.el.classList.toggle(className, state);
                }
            },

            /**
             * Returns IDs of all items whose layout position intersects the given
             * content-coordinate rectangle. Uses pure geometry — works for items
             * outside the rendered buffer zone (no DOM query needed).
             *
             * @param {{left: number, top: number, right: number, bottom: number}} rect
             *     Rectangle in content coordinates (scroll-adjusted, not viewport).
             * @returns {string[]} IDs of intersecting items
             */
            getItemIdsInRect(rect) {
                const { itemsPerRow, itemWidth, itemHeight } = this._state;
                const { gap, padding } = this._config;
                if (!itemsPerRow || !itemHeight) return [];

                const items = this._config.getItems();
                const totalCount = items.length;
                if (totalCount === 0) return [];

                const colStride = itemWidth + gap;
                const cellHeight = itemHeight - gap; // actual item height (no gap)

                // Row range that could intersect (each row occupies itemHeight px)
                const firstRow = Math.max(0, Math.floor((rect.top - padding) / itemHeight));
                const lastRow = Math.min(
                    Math.ceil(totalCount / itemsPerRow) - 1,
                    Math.floor((rect.bottom - padding) / itemHeight),
                );

                // Column range that could intersect
                const firstCol = Math.max(0, Math.floor((rect.left - padding) / colStride));
                const lastCol = Math.min(
                    itemsPerRow - 1,
                    Math.floor((rect.right - padding) / colStride),
                );

                const ids = [];
                for (let row = firstRow; row <= lastRow; row++) {
                    for (let col = firstCol; col <= lastCol; col++) {
                        const index = row * itemsPerRow + col;
                        if (index >= totalCount) break;

                        // Exact item bounds (same formula as _getItemPosition)
                        const itemTop = padding + row * itemHeight;
                        const itemLeft = padding + col * colStride;
                        const itemRight = itemLeft + itemWidth;
                        const itemBottom = itemTop + cellHeight;

                        // Intersection test
                        if (rect.right > itemLeft && rect.left < itemRight &&
                            rect.bottom > itemTop && rect.top < itemBottom) {
                            ids.push(String(this._config.getItemId(items[index])));
                        }
                    }
                }
                return ids;
            },

            /**
             * Unbinds scroll listener and cancels pending operations.
             *
             * Call when:
             * - Screen becomes inactive (onLeave)
             * - Before destroying the grid
             * - Before clearing container DOM
             *
             * CRITICAL: Always call before clearing container innerHTML.
             * Otherwise scroll listeners remain attached to orphaned elements.
             *
             * Does NOT clear rendered items or revoke blob URLs - those persist
             * so returning to the screen shows cached content. Use destroy()
             * for full cleanup.
             */
            unbind() {
                this._detachScrollListener();
                if (this._trailingScrollTimeout) {
                    clearTimeout(this._trailingScrollTimeout);
                    this._trailingScrollTimeout = null;
                }
                if (this._pendingRenderFrame) {
                    cancelAnimationFrame(this._pendingRenderFrame);
                    this._pendingRenderFrame = null;
                }
                this._bound = false;
            },

            /**
             * Rebinds scroll listener for screen enter.
             *
             * Call when:
             * - Screen becomes active (onEnter)
             * - ONLY if render() was NOT just called (render() binds internally)
             *
             * IMPORTANT: Also triggers _updateVisibleItems() to ensure thumbnails
             * load. Without this, returning to a screen would show a blank grid
             * until the user scrolled.
             */
            bind() {
                this._attachScrollListener();
                this._bound = true;

                // Ensure visible items are rendered (fixes blank grid on re-enter)
                if (this._config.container && this._state.itemHeight > 0) {
                    this._updateVisibleItems(this._config.container.scrollTop);
                }
            },

            /**
             * Full cleanup - call when discarding the grid instance.
             *
             * WHAT IT DOES:
             * - Removes scroll and resize listeners
             * - Cancels pending timeouts and RAF
             * - Revokes ALL blob URLs (prevents memory leak)
             * - Clears rendered items and pending items
             * - Clears container innerHTML
             *
             * Call when:
             * - Recreating the grid with new data/config
             * - Screen is being destroyed
             *
             * AFTER DESTROY: The instance is unusable. Create a new one.
             */
            destroy() {
                this._detachScrollListener();
                window.removeEventListener('resize', this._resizeHandler);
                if (this._trailingScrollTimeout) {
                    clearTimeout(this._trailingScrollTimeout);
                    this._trailingScrollTimeout = null;
                }
                if (this._pendingRenderFrame) {
                    cancelAnimationFrame(this._pendingRenderFrame);
                    this._pendingRenderFrame = null;
                }
                // CRITICAL: Revoke blob URLs to prevent memory leak
                for (const [, {blobUrl}] of this._state.renderedItems) {
                    URL.revokeObjectURL(blobUrl);
                }
                this._state.renderedItems.clear();
                this._state.pendingItems.clear();
                this._config.container.innerHTML = '';
                this._bound = false;
            },

            /**
             * Removes a single rendered item and revokes its blob URL.
             *
             * Use case: When an item is deleted/rotated and you need to remove
             * it from tracking without a full re-render.
             *
             * @param {string} id - Item ID
             * @param {boolean} [removeFromDom=false] - Also remove element from DOM
             */
            removeRenderedItem(id, removeFromDom = false) {
                const item = this._state.renderedItems.get(id);
                if (item) {
                    URL.revokeObjectURL(item.blobUrl);
                    if (removeFromDom) {
                        item.el.remove();
                    }
                    this._state.renderedItems.delete(id);
                }
            },
        };

        // Initialise
        instance._init();

        return instance;
    },
};

// Make VirtualGrid available globally
window.VirtualGrid = VirtualGrid;


/* ==========================================================================
   GRID SELECTION

   Unified selection handling for thumbnail grids.
   Supports click, keyboard, long-press, and drag-box selection.

   FEATURES:
   - Click: Select single item (clears previous selection)
   - Ctrl+Click: Toggle item in selection
   - Shift+Click: Select range from anchor to clicked item
   - Right-Click: Add to selection if not already selected
   - Drag-box: Draw rectangle to select multiple items
   - Long-press: Toggle selection (touch device support)
   - Arrow keys: Navigate and select (with Shift for extend)
   - Ctrl+A: Select all
   - Escape: Clear selection
   - Enter: Activate selected item
   - Delete: Request deletion of selected items

   LIFECYCLE:
   - create(config) → Instance created, handlers prepared but not attached
   - bind() → Attach all event listeners
   - unbind() → Detach all event listeners, cleanup in-progress operations
   - destroy() → Full cleanup, clear selection state

   GOTCHAS:

   1. KEYBOARD HANDLER ON DOCUMENT:
      The keydown handler is attached to document, not the grid element.
      This allows keyboard navigation without explicit focus. BUT it means
      the handler fires for ALL key presses. The handler should check if
      the grid's screen is active. If multiple grids exist, only the
      active one should have selection bound.

   2. SELECTION PERSISTS ACROSS UNBIND:
      Unlike VirtualGrid, GridSelection keeps its _selected Set when unbound.
      This is intentional - selection should persist when switching screens.
      Call clear() explicitly if you need to reset selection.

   3. ANCHOR FOR SHIFT-CLICK:
      _anchor tracks the last non-range-selected item. Shift+click selects
      everything between anchor and clicked item. Anchor is set on single
      clicks and arrow key navigation.

   4. DRAG-BOX USES DOCUMENT EVENTS:
      During drag, mousemove and mouseup are attached to document (not grid).
      This allows dragging outside the grid boundaries. These handlers are
      cleaned up in unbind() and when drag ends.

   5. VIRTUAL ITEMS:
      Selection works with item IDs, not DOM elements. When VirtualGrid
      scrolls items out of view, they're removed from DOM but remain in
      _selected. Selection visuals are reapplied via onItemCreated callback.
   ========================================================================== */

/**
 * Factory for creating grid selection managers.
 *
 * @namespace
 */
const GridSelection = {
    /**
     * Long-press threshold in milliseconds.
     * @type {number}
     * @constant
     */
    LONG_PRESS_MS: 500,

    /**
     * Auto-scroll edge zone in pixels.
     * @type {number}
     * @constant
     */
    AUTO_SCROLL_EDGE: 50,

    /**
     * Auto-scroll speed in pixels per frame.
     * @type {number}
     * @constant
     */
    AUTO_SCROLL_SPEED: 40,

    /**
     * Creates a new GridSelection instance.
     *
     * @param {Object} config - Configuration object
     * @param {Object} config.grid - VirtualGrid instance
     * @param {Function} config.getItems - Returns current data array
     * @param {Function} config.getItemId - Extracts unique ID from an item
     * @param {string} config.itemSelector - CSS selector for items
     * @param {string} [config.selectedClass='selected'] - Class for selected items
     * @param {Function} [config.onSelectionChanged] - Callback when selection changes (ids: string[])
     * @param {Function} [config.onItemActivated] - Callback for Enter/double-click (id: string)
     * @param {Function} [config.onDeleteRequested] - Callback for Delete key (ids: string[])
     * @param {Function} [config.onGroupNavigate] - Callback for Alt+Arrow group navigation (direction: -1|1)
     * @param {boolean} [config.enableKeyboard=true] - Enable keyboard navigation
     * @param {boolean} [config.enableDragBox=true] - Enable drag-box selection
     * @param {boolean} [config.enableLongPress=true] - Enable long-press selection
     * @returns {Object} GridSelection instance
     */
    create(config) {
        const instance = {
            // Configuration
            _config: {
                grid: config.grid,
                getItems: config.getItems,
                getItemId: config.getItemId,
                itemSelector: config.itemSelector,
                selectedClass: config.selectedClass || 'selected',
                onSelectionChanged: config.onSelectionChanged || null,
                onItemActivated: config.onItemActivated || null,
                onDeleteRequested: config.onDeleteRequested || null,
                onGroupNavigate: config.onGroupNavigate || null,
                enableKeyboard: config.enableKeyboard !== false,
                enableDragBox: config.enableDragBox !== false,
                enableLongPress: config.enableLongPress !== false,
                focusContainer: config.focusContainer || null,
            },

            // Selection state
            _selected: new Set(),
            _anchor: null,  // Anchor ID for shift-click ranges

            // Long-press state
            _longPressTimer: null,
            _longPressTriggered: false,

            // Drag-box state
            _dragState: null,
            _justDragged: false,

            // Bound handlers (for cleanup)
            _handlers: {},
            _bound: false,

            /**
             * Gets the scroll container element.
             * @returns {HTMLElement}
             * @private
             */
            _getContainer() {
                return this._config.grid._config.container;
            },

            /**
             * Gets the inner grid element where items are rendered.
             * @returns {HTMLElement}
             * @private
             */
            _getGridElement() {
                return this._config.grid._innerContainer;
            },

            /**
             * Gets the item ID from an element or its parent.
             * @param {HTMLElement} el - Element to check
             * @returns {string|null} Item ID or null
             * @private
             */
            _getItemId(el) {
                const item = el.closest(this._config.itemSelector);
                return item ? (item.dataset.id || item.dataset.groupHash) : null;
            },

            /**
             * Notifies listeners of selection change.
             * @private
             */
            _notifySelectionChanged() {
                if (this._config.onSelectionChanged) {
                    this._config.onSelectionChanged(Array.from(this._selected));
                }
            },

            // ==================== Click Handlers ====================

            /**
             * Handles click on grid.
             * @param {MouseEvent} e
             * @private
             */
            _handleClick(e) {
                // Ignore if this was a long-press or drag
                if (this._longPressTriggered || this._dragState?.dragged || this._justDragged) {
                    this._longPressTriggered = false;
                    this._justDragged = false;
                    return;
                }

                // Ignore clicks on input elements (user is focusing to type)
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
                    return;
                }

                const id = this._getItemId(e.target);
                if (id) {
                    if (e.ctrlKey || e.metaKey) {
                        // Ctrl+click: Toggle selection
                        this.toggle(id);
                        this._anchor = id;
                    } else if (e.shiftKey && this._anchor) {
                        // Shift+click: Select range from anchor
                        this.selectRange(this._anchor, id);
                    } else {
                        // Regular click: Select only this item
                        this.select(id);
                        this._anchor = id;
                    }
                } else {
                    // Clicked on empty space - clear selection
                    this.clear();
                    this._anchor = null;
                }
            },

            /**
             * Handles right-click on grid.
             * @param {MouseEvent} e
             * @private
             */
            _handleRightClick(e) {
                e.preventDefault();

                // After a right-button drag, contextmenu fires on the item under
                // the cursor. _handleDragEnd already toggled it, so skip here to
                // avoid double-toggling it back to its original state.
                if (this._justDragged) {
                    this._justDragged = false;
                    return;
                }

                const id = this._getItemId(e.target);
                if (id) {
                    this.toggle(id);
                }
            },

            /**
             * Handles double-click on grid.
             * @param {MouseEvent} e
             * @private
             */
            _handleDoubleClick(e) {
                // Ignore double-clicks on input elements (let native text selection work)
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
                    return;
                }

                const id = this._getItemId(e.target);
                if (id && this._config.onItemActivated) {
                    this._config.onItemActivated(id);
                }
            },

            // ==================== Long-Press Handlers ====================

            /**
             * Handles pointer down for long-press detection.
             * @param {PointerEvent} e
             * @private
             */
            _handlePointerDown(e) {
                if (!this._config.enableLongPress) return;

                // Don't start long-press on input fields (user may be placing cursor)
                if (e.target.matches('input, textarea')) return;

                const id = this._getItemId(e.target);
                if (!id) return;

                const normalizedId = String(id);
                this._longPressTriggered = false;
                this._longPressTimer = setTimeout(() => {
                    this._longPressTriggered = true;
                    // Add to selection without clearing
                    if (!this._selected.has(normalizedId)) {
                        this._selected.add(normalizedId);
                        this._updateItemVisualState(normalizedId, true);
                        this._notifySelectionChanged();
                    }
                }, GridSelection.LONG_PRESS_MS);
            },

            /**
             * Handles pointer up - cancels long-press timer.
             * @private
             */
            _handlePointerUp() {
                if (this._longPressTimer) {
                    clearTimeout(this._longPressTimer);
                    this._longPressTimer = null;
                }
            },

            // ==================== Drag-Box Selection ====================

            /**
             * Handles mouse down for drag-box selection.
             * @param {MouseEvent} e
             * @private
             */
            _handleDragStart(e) {
                if (!this._config.enableDragBox) return;

                // Only handle left or right mouse button on grid background
                if (e.button !== 0 && e.button !== 2) return;
                if (this._getItemId(e.target)) return; // Clicked on an item

                // Don't interfere with text selection in input fields
                if (e.target.matches('input, textarea')) return;

                e.preventDefault();
                const gridEl = this._getGridElement();
                const rect = gridEl.getBoundingClientRect();

                // Grid is always the scroll container (container === grid),
                // so rect is stable and we add scroll offset to get content position
                this._dragState = {
                    startX: e.clientX - rect.left + gridEl.scrollLeft,
                    startY: e.clientY - rect.top + gridEl.scrollTop,
                    isRightButton: e.button === 2,
                    dragged: false,
                    box: null,
                    autoScrollInterval: null,
                    lastMouseEvent: null,
                    scrollDirection: 0,
                };

                // Create selection box element
                const box = document.createElement('div');
                box.className = 'selection-box';
                gridEl.appendChild(box);
                this._dragState.box = box;

                // Bind move and up handlers
                this._handlers.dragMove = (e) => this._handleDragMove(e);
                this._handlers.dragEnd = (e) => this._handleDragEnd(e);
                document.addEventListener('mousemove', this._handlers.dragMove);
                document.addEventListener('mouseup', this._handlers.dragEnd);
            },

            /**
             * Handles drag move - updates selection box.
             * @param {MouseEvent} e
             * @private
             */
            _handleDragMove(e) {
                if (!this._dragState) return;

                this._dragState.lastMouseEvent = e;
                this._updateDragBox(e);
                this._updateAutoScroll(e);
            },

            /**
             * Updates the drag selection box position.
             * @param {MouseEvent} e
             * @private
             */
            _updateDragBox(e) {
                const gridEl = this._getGridElement();
                const rect = gridEl.getBoundingClientRect();

                const currentX = e.clientX - rect.left + gridEl.scrollLeft;
                const currentY = e.clientY - rect.top + gridEl.scrollTop;

                const x = Math.min(this._dragState.startX, currentX);
                const y = Math.min(this._dragState.startY, currentY);
                const w = Math.abs(currentX - this._dragState.startX);
                const h = Math.abs(currentY - this._dragState.startY);

                // Mark as dragged if moved enough
                if (w > 5 || h > 5) {
                    this._dragState.dragged = true;
                }

                // Update box position
                const box = this._dragState.box;
                box.style.left = x + 'px';
                box.style.top = y + 'px';
                box.style.width = w + 'px';
                box.style.height = h + 'px';
            },

            /**
             * Updates auto-scroll based on mouse position.
             * @param {MouseEvent} e
             * @private
             */
            _updateAutoScroll(e) {
                const container = this._getContainer();
                const rect = container.getBoundingClientRect();
                const mouseY = e.clientY;

                const distFromTop = mouseY - rect.top;
                const distFromBottom = rect.bottom - mouseY;

                let scrollDirection = 0;
                if (distFromTop < GridSelection.AUTO_SCROLL_EDGE && distFromTop >= 0) {
                    scrollDirection = -1;
                } else if (distFromBottom < GridSelection.AUTO_SCROLL_EDGE && distFromBottom >= 0) {
                    scrollDirection = 1;
                }

                if (scrollDirection !== 0) {
                    if (!this._dragState.autoScrollInterval) {
                        this._dragState.autoScrollInterval = setInterval(() => {
                            this._performAutoScroll();
                        }, 16);
                    }
                    this._dragState.scrollDirection = scrollDirection;
                } else {
                    this._stopAutoScroll();
                }
            },

            /**
             * Performs one step of auto-scrolling.
             * @private
             */
            _performAutoScroll() {
                if (!this._dragState) return;

                const container = this._getContainer();
                const direction = this._dragState.scrollDirection || 0;
                if (direction === 0) return;

                container.scrollTop += direction * GridSelection.AUTO_SCROLL_SPEED;

                if (this._dragState.lastMouseEvent) {
                    this._updateDragBox(this._dragState.lastMouseEvent);
                }
            },

            /**
             * Stops auto-scrolling.
             * @private
             */
            _stopAutoScroll() {
                if (this._dragState?.autoScrollInterval) {
                    clearInterval(this._dragState.autoScrollInterval);
                    this._dragState.autoScrollInterval = null;
                }
            },

            /**
             * Handles drag end - selects items in box.
             * @param {MouseEvent} e
             * @private
             */
            _handleDragEnd(e) {
                document.removeEventListener('mousemove', this._handlers.dragMove);
                document.removeEventListener('mouseup', this._handlers.dragEnd);

                this._stopAutoScroll();

                if (!this._dragState) return;

                const { box, isRightButton, dragged } = this._dragState;

                if (dragged) {
                    // Use geometry-based hit test via VirtualGrid so that items
                    // outside the rendered buffer zone (scrolled out of view
                    // during auto-scroll) are still included in the selection.
                    const boxStyle = box.style;
                    const contentRect = {
                        left: parseFloat(boxStyle.left),
                        top: parseFloat(boxStyle.top),
                        right: parseFloat(boxStyle.left) + parseFloat(boxStyle.width),
                        bottom: parseFloat(boxStyle.top) + parseFloat(boxStyle.height),
                    };
                    const idsInBox = this._config.grid.getItemIdsInRect(contentRect);

                    if (isRightButton) {
                        // Toggle selection for items in box
                        for (const id of idsInBox) {
                            this.toggle(id);
                        }
                    } else {
                        // Set selection to items in box
                        this.setSelected(idsInBox);
                    }

                    this._justDragged = true;
                }

                // Cleanup
                box.remove();
                this._dragState = null;
            },

            // ==================== Keyboard Navigation ====================

            /**
             * Handles keydown events.
             * @param {KeyboardEvent} e
             * @private
             */
            _handleKeyDown(e) {
                // Ignore if modal dialog is open
                if (document.querySelector('dialog[open]')) return;

                // Ignore if fullscreen overlay is open (it has its own keyboard handling)
                if (AppState.nav.isFullscreenOpen()) return;

                // Ignore if typing in an input field
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

                // If focusContainer is specified, only handle keys when it has focus
                if (this._config.focusContainer && document.activeElement !== this._config.focusContainer) {
                    return;
                }

                switch (e.key) {
                    case 'ArrowLeft':
                        if (e.altKey) {
                            if (this._config.onGroupNavigate) {
                                e.preventDefault();
                                this._config.onGroupNavigate(-1);
                            }
                            return;
                        }
                        e.preventDefault();
                        this.navigateRelative(-1, e.shiftKey);
                        break;
                    case 'ArrowRight':
                        if (e.altKey) {
                            if (this._config.onGroupNavigate) {
                                e.preventDefault();
                                this._config.onGroupNavigate(1);
                            }
                            return;
                        }
                        e.preventDefault();
                        this.navigateRelative(1, e.shiftKey);
                        break;
                    case 'ArrowUp':
                        e.preventDefault();
                        if (e.ctrlKey || e.metaKey) {
                            this.navigateToEnd(-1, e.shiftKey);
                        } else {
                            this.navigateVertical(-1, e.shiftKey);
                        }
                        break;
                    case 'ArrowDown':
                        e.preventDefault();
                        if (e.ctrlKey || e.metaKey) {
                            this.navigateToEnd(1, e.shiftKey);
                        } else {
                            this.navigateVertical(1, e.shiftKey);
                        }
                        break;
                    case 'PageUp':
                        e.preventDefault();
                        this.navigatePage(-1, e.shiftKey);
                        break;
                    case 'PageDown':
                        e.preventDefault();
                        this.navigatePage(1, e.shiftKey);
                        break;
                    case 'Enter':
                        e.preventDefault();
                        if (this._selected.size === 1 && this._config.onItemActivated) {
                            this._config.onItemActivated(Array.from(this._selected)[0]);
                        }
                        break;
                    case 'Delete':
                    case 'Backspace':
                        e.preventDefault();
                        if (this._selected.size > 0 && this._config.onDeleteRequested) {
                            this._config.onDeleteRequested(Array.from(this._selected));
                        }
                        break;
                    case 'Escape':
                        e.preventDefault();
                        this.clear();
                        break;
                    case 'a':
                        if (e.ctrlKey || e.metaKey) {
                            e.preventDefault();
                            this.selectAll();
                        }
                        break;
                }
            },

            // ==================== Visual State ====================

            /**
             * Updates visual state for a single item.
             * @param {string} id - Item ID
             * @param {boolean} selected - Whether selected
             * @private
             */
            _updateItemVisualState(id, selected) {
                this._config.grid.setItemClass(id, this._config.selectedClass, selected);
            },

            /**
             * Updates visual state for all rendered items.
             */
            updateVisualState() {
                const gridEl = this._getGridElement();
                const items = gridEl.querySelectorAll(this._config.itemSelector);
                const selectedClass = this._config.selectedClass;

                for (const item of items) {
                    const id = item.dataset.id || item.dataset.groupHash;
                    if (id) {
                        item.classList.toggle(selectedClass, this._selected.has(id));
                    }
                }
            },

            // ==================== Public API ====================

            /**
             * Selects a single item, clearing others.
             * @param {string} id - Item ID to select
             */
            select(id) {
                const normalizedId = String(id);
                const changed = this._selected.size !== 1 || !this._selected.has(normalizedId);
                if (!changed) return;

                // Update visual state for previously selected
                for (const prevId of this._selected) {
                    this._updateItemVisualState(prevId, false);
                }

                this._selected.clear();
                this._selected.add(normalizedId);
                this._updateItemVisualState(normalizedId, true);
                this._notifySelectionChanged();
            },

            /**
             * Toggles selection of an item.
             * @param {string} id - Item ID to toggle
             */
            toggle(id) {
                const normalizedId = String(id);
                if (this._selected.has(normalizedId)) {
                    this._selected.delete(normalizedId);
                    this._updateItemVisualState(normalizedId, false);
                } else {
                    this._selected.add(normalizedId);
                    this._updateItemVisualState(normalizedId, true);
                }
                this._notifySelectionChanged();
            },

            /**
             * Selects a range of items from anchor to target.
             * @param {string} anchorId - Starting item ID
             * @param {string} targetId - Ending item ID
             */
            selectRange(anchorId, targetId) {
                const normalizedAnchor = String(anchorId);
                const normalizedTarget = String(targetId);
                const items = this._config.getItems();
                const getItemId = this._config.getItemId;

                const anchorIdx = items.findIndex(item => String(getItemId(item)) === normalizedAnchor);
                const targetIdx = items.findIndex(item => String(getItemId(item)) === normalizedTarget);

                if (anchorIdx === -1 || targetIdx === -1) {
                    this.select(normalizedTarget);
                    return;
                }

                const startIdx = Math.min(anchorIdx, targetIdx);
                const endIdx = Math.max(anchorIdx, targetIdx);

                // Clear previous selection visual state
                for (const prevId of this._selected) {
                    this._updateItemVisualState(prevId, false);
                }

                this._selected.clear();

                for (let i = startIdx; i <= endIdx; i++) {
                    const id = String(getItemId(items[i]));
                    this._selected.add(id);
                    this._updateItemVisualState(id, true);
                }

                this._notifySelectionChanged();
            },

            /**
             * Selects all items.
             */
            selectAll() {
                const items = this._config.getItems();
                const getItemId = this._config.getItemId;

                this._selected.clear();
                for (const item of items) {
                    this._selected.add(String(getItemId(item)));
                }

                this.updateVisualState();
                this._notifySelectionChanged();
            },

            /**
             * Clears selection.
             */
            clear() {
                if (this._selected.size === 0) return;

                for (const id of this._selected) {
                    this._updateItemVisualState(id, false);
                }

                this._selected.clear();
                this._anchor = null;
                this._notifySelectionChanged();
            },

            /**
             * Gets the current selection.
             * @returns {Array<string>} Selected item IDs
             */
            getSelected() {
                return Array.from(this._selected);
            },

            /**
             * Checks if an item is selected.
             * @param {string} id - Item ID
             * @returns {boolean}
             */
            isSelected(id) {
                return this._selected.has(String(id));
            },

            /**
             * Sets selection to specific IDs.
             * @param {Array<string>} ids - Item IDs to select
             */
            setSelected(ids) {
                // Normalize to strings for consistent comparison
                const normalizedIds = ids.map(id => String(id));

                // Check if selection is actually changing to avoid feedback loops
                if (normalizedIds.length === this._selected.size &&
                    normalizedIds.every(id => this._selected.has(id))) {
                    return;
                }

                // Clear previous selection visual state
                for (const prevId of this._selected) {
                    this._updateItemVisualState(prevId, false);
                }

                this._selected.clear();

                for (const id of normalizedIds) {
                    this._selected.add(id);
                    this._updateItemVisualState(id, true);
                }

                if (normalizedIds.length > 0) {
                    this._anchor = normalizedIds[normalizedIds.length - 1];
                }

                this._notifySelectionChanged();
            },

            /**
             * Removes IDs from selection that no longer exist in the data.
             * Call this after data changes that may remove items (e.g., backend
             * reassessment matching unknown faces to a person).
             */
            pruneToValidIds() {
                const items = this._config.getItems();
                const getItemId = this._config.getItemId;

                // Build set of valid IDs
                const validIds = new Set();
                for (const item of items) {
                    validIds.add(String(getItemId(item)));
                }

                // Remove any selected IDs that are no longer valid
                let changed = false;
                for (const id of this._selected) {
                    if (!validIds.has(id)) {
                        this._selected.delete(id);
                        changed = true;
                    }
                }

                // Update anchor if it was pruned
                if (this._anchor && !validIds.has(this._anchor)) {
                    this._anchor = null;
                }

                if (changed) {
                    this.updateVisualState();
                    this._notifySelectionChanged();
                }
            },

            /**
             * Navigates selection horizontally.
             * @param {number} delta - Direction (-1 left, 1 right)
             * @param {boolean} [extend=false] - Extend selection instead of replacing
             */
            navigateRelative(delta, extend = false) {
                const items = this._config.getItems();
                if (items.length === 0) return;

                const getItemId = this._config.getItemId;
                let currentIndex = -1;

                if (this._selected.size > 0) {
                    const lastSelected = Array.from(this._selected).pop();
                    currentIndex = items.findIndex(item => String(getItemId(item)) === lastSelected);
                }

                let newIndex = currentIndex + delta;
                if (newIndex < 0) newIndex = items.length - 1;
                if (newIndex >= items.length) newIndex = 0;

                const newId = String(getItemId(items[newIndex]));

                if (extend && this._anchor) {
                    this.selectRange(this._anchor, newId);
                } else {
                    this.select(newId);
                    this._anchor = newId;
                }

                this._config.grid.scrollTo(newIndex);
            },

            /**
             * Navigates selection vertically.
             * @param {number} delta - Direction (-1 up, 1 down)
             * @param {boolean} [extend=false] - Extend selection instead of replacing
             */
            navigateVertical(delta, extend = false) {
                const items = this._config.getItems();
                if (items.length === 0) return;

                const getItemId = this._config.getItemId;
                const itemsPerRow = this._config.grid.getItemsPerRow() || 1;
                let currentIndex = -1;

                if (this._selected.size > 0) {
                    const lastSelected = Array.from(this._selected).pop();
                    currentIndex = items.findIndex(item => String(getItemId(item)) === lastSelected);
                }

                let newIndex = currentIndex + (delta * itemsPerRow);
                if (newIndex < 0) newIndex = 0;
                if (newIndex >= items.length) newIndex = items.length - 1;

                const newId = String(getItemId(items[newIndex]));

                if (extend && this._anchor) {
                    this.selectRange(this._anchor, newId);
                } else {
                    this.select(newId);
                    this._anchor = newId;
                }

                this._config.grid.scrollTo(newIndex);
            },

            /**
             * Navigates selection by a page (visible rows).
             * @param {number} delta - Direction (-1 up, 1 down)
             * @param {boolean} [extend=false] - Extend selection instead of replacing
             */
            navigatePage(delta, extend = false) {
                const items = this._config.getItems();
                if (items.length === 0) return;

                const getItemId = this._config.getItemId;
                const itemsPerRow = this._config.grid.getItemsPerRow() || 1;
                const visibleRows = this._config.grid.getVisibleRows() || 1;
                const pageSize = itemsPerRow * Math.max(1, visibleRows - 1); // Leave one row overlap
                let currentIndex = -1;

                if (this._selected.size > 0) {
                    const lastSelected = Array.from(this._selected).pop();
                    currentIndex = items.findIndex(item => String(getItemId(item)) === lastSelected);
                }

                let newIndex = currentIndex + (delta * pageSize);
                if (newIndex < 0) newIndex = 0;
                if (newIndex >= items.length) newIndex = items.length - 1;

                const newId = String(getItemId(items[newIndex]));

                if (extend && this._anchor) {
                    this.selectRange(this._anchor, newId);
                } else {
                    this.select(newId);
                    this._anchor = newId;
                }

                this._config.grid.scrollTo(newIndex);
            },

            /**
             * Navigates selection to the first or last item.
             * @param {number} delta - Direction (-1 for first, 1 for last)
             * @param {boolean} [extend=false] - Extend selection instead of replacing
             */
            navigateToEnd(delta, extend = false) {
                const items = this._config.getItems();
                if (items.length === 0) return;

                const getItemId = this._config.getItemId;
                const newIndex = delta < 0 ? 0 : items.length - 1;
                const newId = String(getItemId(items[newIndex]));

                if (extend && this._anchor) {
                    this.selectRange(this._anchor, newId);
                } else {
                    this.select(newId);
                    this._anchor = newId;
                }

                this._config.grid.scrollTo(newIndex);
            },

            /**
             * Binds all event listeners for selection handling.
             *
             * Call when:
             * - Screen becomes active (after VirtualGrid.bind())
             * - After creating a new GridSelection instance
             *
             * IMPORTANT: Only one GridSelection should be bound at a time if
             * keyboard handling is enabled, since keydown is on document.
             *
             * NOTE: Does not clear selection - existing selection persists.
             */
            bind() {
                if (this._bound) return;

                const gridEl = this._getGridElement();

                // Click handlers (on grid element)
                this._handlers.click = (e) => this._handleClick(e);
                this._handlers.contextmenu = (e) => this._handleRightClick(e);
                this._handlers.dblclick = (e) => this._handleDoubleClick(e);

                gridEl.addEventListener('click', this._handlers.click);
                gridEl.addEventListener('contextmenu', this._handlers.contextmenu);
                gridEl.addEventListener('dblclick', this._handlers.dblclick);

                // Long-press handlers (for touch devices)
                if (this._config.enableLongPress) {
                    this._handlers.pointerdown = (e) => this._handlePointerDown(e);
                    this._handlers.pointerup = () => this._handlePointerUp();
                    this._handlers.pointerleave = () => this._handlePointerUp();

                    gridEl.addEventListener('pointerdown', this._handlers.pointerdown);
                    gridEl.addEventListener('pointerup', this._handlers.pointerup);
                    gridEl.addEventListener('pointerleave', this._handlers.pointerleave);
                }

                // Drag-box handler (mousedown starts drag tracking)
                if (this._config.enableDragBox) {
                    this._handlers.mousedown = (e) => this._handleDragStart(e);
                    gridEl.addEventListener('mousedown', this._handlers.mousedown);
                }

                // Keyboard handler (on document - captures all key presses)
                if (this._config.enableKeyboard) {
                    this._handlers.keydown = (e) => this._handleKeyDown(e);
                    document.addEventListener('keydown', this._handlers.keydown);
                }

                this._bound = true;
            },

            /**
             * Unbinds all event listeners and cleans up in-progress operations.
             *
             * Call when:
             * - Screen becomes inactive (before VirtualGrid.unbind())
             * - Before destroying the selection instance
             *
             * NOTE: Selection state (_selected Set) is preserved. This allows
             * selection to persist when switching screens. Call clear() first
             * if you want to reset selection before unbinding.
             *
             * Cleans up:
             * - All grid event listeners
             * - Document keydown listener
             * - In-progress drag-box (removes box element, document listeners)
             * - Long-press timer
             */
            unbind() {
                if (!this._bound) return;

                const gridEl = this._getGridElement();

                // Remove grid element listeners
                gridEl.removeEventListener('click', this._handlers.click);
                gridEl.removeEventListener('contextmenu', this._handlers.contextmenu);
                gridEl.removeEventListener('dblclick', this._handlers.dblclick);

                if (this._config.enableLongPress) {
                    gridEl.removeEventListener('pointerdown', this._handlers.pointerdown);
                    gridEl.removeEventListener('pointerup', this._handlers.pointerup);
                    gridEl.removeEventListener('pointerleave', this._handlers.pointerleave);
                }

                if (this._config.enableDragBox) {
                    gridEl.removeEventListener('mousedown', this._handlers.mousedown);
                }

                // Remove document keydown listener
                if (this._config.enableKeyboard) {
                    document.removeEventListener('keydown', this._handlers.keydown);
                }

                // Cleanup in-progress drag (removes document listeners too)
                if (this._dragState) {
                    document.removeEventListener('mousemove', this._handlers.dragMove);
                    document.removeEventListener('mouseup', this._handlers.dragEnd);
                    this._stopAutoScroll();
                    if (this._dragState.box) {
                        this._dragState.box.remove();
                    }
                    this._dragState = null;
                }

                // Cleanup long-press timer
                if (this._longPressTimer) {
                    clearTimeout(this._longPressTimer);
                    this._longPressTimer = null;
                }

                this._bound = false;
            },

            /**
             * Full cleanup - call when discarding the selection instance.
             *
             * Unlike unbind(), this also clears selection state.
             * Call when recreating selection with new VirtualGrid.
             */
            destroy() {
                this.unbind();
                this._selected.clear();
                this._anchor = null;
            },
        };

        return instance;
    },
};

// Make GridSelection available globally
window.GridSelection = GridSelection;
