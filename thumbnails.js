/**
 * @fileoverview Shared thumbnail infrastructure for the Imaginary application.
 *
 * This module provides reusable components for thumbnail grid management:
 * - ThumbnailLoader: Centralized, priority-based thumbnail loading with LIFO queue
 * - VirtualGrid: Reusable virtual scrolling infrastructure (Phase 2)
 * - GridSelection: Unified selection handling (Phase 3)
 *
 * @module thumbnails
 * @requires core
 */

/* ==========================================================================
   THUMBNAIL LOADER

   Centralized thumbnail loading with LIFO priority queue, deduplication,
   and request cancellation support.
   ========================================================================== */

/**
 * Centralized thumbnail loader with priority-based LIFO queue.
 *
 * Features:
 * - LIFO queue: Most recent requests processed first
 * - Priority tiers: 'visible' > 'buffer' > 'background'
 * - Deduplication: Multiple elements can listen for the same thumbnail
 * - Cancellation: Pending/in-flight requests can be cancelled
 * - Cache busting: Support for rotated images
 *
 * @namespace
 */
const ThumbnailLoader = {
    /**
     * Priority levels (higher number = higher priority).
     * @type {Object<string, number>}
     * @constant
     */
    PRIORITY: {
        background: 0,
        buffer: 1,
        visible: 2
    },

    /**
     * Pending request queue.
     * Each entry: { imageId, priority, listeners: Set<HTMLImageElement> }
     * @type {Array<Object>}
     * @private
     */
    _queue: [],

    /**
     * In-flight requests.
     * Map of imageId -> { controller: AbortController, listeners: Set<HTMLImageElement> }
     * @type {Map<string, Object>}
     * @private
     */
    _inFlight: new Map(),

    /**
     * Cache-bust timestamps for rotated images.
     * Map of imageId -> timestamp
     * @type {Map<string, number>}
     * @private
     */
    _cacheBust: new Map(),

    /**
     * Maximum concurrent thumbnail fetches.
     * @type {number}
     * @private
     */
    _maxConcurrent: 6,

    /**
     * Current number of active fetches.
     * @type {number}
     * @private
     */
    _activeCount: 0,

    /**
     * Requests a thumbnail load for an image.
     *
     * If the imageId is already queued, adds the element as a listener and
     * updates priority if the new priority is higher.
     *
     * If the imageId is already in-flight, adds the element as a listener
     * to receive the result when it completes.
     *
     * @param {string} imageId - The image ID to load
     * @param {HTMLImageElement} imgElement - The img element to populate
     * @param {string} [priority='visible'] - Priority: 'visible', 'buffer', or 'background'
     */
    request(imageId, imgElement, priority = 'visible') {
        if (!imageId || !imgElement) return;

        const priorityNum = this.PRIORITY[priority] ?? this.PRIORITY.visible;

        // Check if already in-flight
        const inFlight = this._inFlight.get(imageId);
        if (inFlight) {
            // Add element as listener - it will receive the result when fetch completes
            inFlight.listeners.add(imgElement);
            return;
        }

        // Check if already in queue
        const existing = this._queue.find(item => item.imageId === imageId);
        if (existing) {
            // Add element as listener
            existing.listeners.add(imgElement);
            // Update priority if higher
            if (priorityNum > existing.priority) {
                existing.priority = priorityNum;
            }
            return;
        }

        // New request - add to front of queue (LIFO)
        this._queue.unshift({
            imageId,
            priority: priorityNum,
            listeners: new Set([imgElement])
        });

        // Process queue
        this._processQueue();
    },

    /**
     * Cancels a thumbnail request for a specific element.
     *
     * Removes the element from the listeners. If no listeners remain,
     * removes from queue or aborts the in-flight request.
     *
     * @param {string} imageId - The image ID
     * @param {HTMLImageElement} [imgElement] - Specific element to remove, or all if omitted
     */
    cancel(imageId, imgElement) {
        if (!imageId) return;

        // Check queue first
        const queueIdx = this._queue.findIndex(item => item.imageId === imageId);
        if (queueIdx !== -1) {
            const item = this._queue[queueIdx];
            if (imgElement) {
                item.listeners.delete(imgElement);
            } else {
                item.listeners.clear();
            }

            // Remove from queue if no listeners left
            if (item.listeners.size === 0) {
                this._queue.splice(queueIdx, 1);
            }
            return;
        }

        // Check in-flight
        const inFlight = this._inFlight.get(imageId);
        if (inFlight) {
            if (imgElement) {
                inFlight.listeners.delete(imgElement);
            } else {
                inFlight.listeners.clear();
            }

            // Abort if no listeners left
            if (inFlight.listeners.size === 0) {
                inFlight.controller.abort();
                this._inFlight.delete(imageId);
                this._activeCount--;
                // Process next items in queue
                this._processQueue();
            }
        }
    },

    /**
     * Re-prioritizes the queue based on what's currently visible.
     *
     * Called on scroll updates to ensure visible items are processed first.
     * Items in visibleIds get 'visible' priority, bufferIds get 'buffer',
     * everything else becomes 'background'.
     *
     * After updating priorities, the queue is re-sorted so highest priority
     * items are at the front (LIFO within each priority tier).
     *
     * @param {Array<string>} visibleIds - Image IDs currently visible in viewport
     * @param {Array<string>} bufferIds - Image IDs in buffer zone (not visible but nearby)
     */
    prioritize(visibleIds, bufferIds) {
        const visibleSet = new Set(visibleIds);
        const bufferSet = new Set(bufferIds);

        // Update priorities in queue
        for (const item of this._queue) {
            if (visibleSet.has(item.imageId)) {
                item.priority = this.PRIORITY.visible;
            } else if (bufferSet.has(item.imageId)) {
                item.priority = this.PRIORITY.buffer;
            } else {
                item.priority = this.PRIORITY.background;
            }
        }

        // Sort queue: highest priority first, then LIFO within priority
        // Since we unshift new items, original order is reverse insertion order
        // We want: visible items first (most recent first), then buffer, then background
        this._queue.sort((a, b) => b.priority - a.priority);

        // Cancel background items that are no longer needed
        // Keep queue focused on visible and buffer items
        const maxQueueSize = (visibleIds.length + bufferIds.length) * 2;
        if (this._queue.length > maxQueueSize) {
            // Remove excess background items from the end
            const removed = this._queue.splice(maxQueueSize);
            // These items are dropped - their img elements will remain without src
        }
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
     * Processes the queue, starting new fetches up to the concurrency limit.
     * @private
     */
    _processQueue() {
        while (this._activeCount < this._maxConcurrent && this._queue.length > 0) {
            // Take from front (highest priority due to sort)
            const item = this._queue.shift();
            this._loadThumbnail(item);
        }
    },

    /**
     * Loads a thumbnail for a queue item.
     *
     * @param {Object} item - Queue item with imageId, priority, listeners
     * @private
     */
    async _loadThumbnail(item) {
        const { imageId, listeners } = item;

        // Skip if no listeners (were all cancelled)
        if (listeners.size === 0) {
            this._processQueue();
            return;
        }

        // Create abort controller
        const controller = new AbortController();

        // Track as in-flight
        this._inFlight.set(imageId, {
            controller,
            listeners
        });
        this._activeCount++;

        const url = this._getThumbnailUrl(imageId);

        try {
            const response = await fetch(url, {
                signal: controller.signal
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const blob = await response.blob();

            // Create object URL for the blob
            const objectUrl = URL.createObjectURL(blob);

            // Apply to all listeners that are still connected to DOM
            const inFlight = this._inFlight.get(imageId);
            if (inFlight) {
                for (const img of inFlight.listeners) {
                    if (img.isConnected) {
                        img.src = objectUrl;
                        // Mark parent as loaded if it has the pattern
                        const parent = img.closest('.gallery-item, .duplicate-stack');
                        if (parent) {
                            parent.classList.add('loaded');
                        }
                    }
                }
            }

            // Note: We don't revoke the object URL immediately because
            // the browser needs it to display the image. The URLs will be
            // cleaned up when the page is navigated away or tab is closed.

        } catch (error) {
            if (error.name === 'AbortError') {
                // Request was cancelled - this is expected
                return;
            }

            console.error(`Failed to load thumbnail for ${imageId}:`, error);

            // Mark as error for all listeners
            const inFlight = this._inFlight.get(imageId);
            if (inFlight) {
                for (const img of inFlight.listeners) {
                    const parent = img.closest('.gallery-item, .duplicate-stack');
                    if (parent) {
                        parent.classList.add('error');
                    }
                }
            }
        } finally {
            // Cleanup
            this._inFlight.delete(imageId);
            this._activeCount--;

            // Process next items
            this._processQueue();
        }
    },

    /**
     * Clears all pending requests and resets state.
     * Called when switching screens or doing a full refresh.
     */
    clear() {
        // Cancel all in-flight requests
        for (const [imageId, inFlight] of this._inFlight) {
            inFlight.controller.abort();
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
            cacheBustCount: this._cacheBust.size
        };
    }
};

// Make ThumbnailLoader available globally
window.ThumbnailLoader = ThumbnailLoader;


/* ==========================================================================
   VIRTUAL GRID

   Reusable virtual scrolling infrastructure for thumbnail grids.
   Only renders visible items plus a buffer for smooth scrolling.
   ========================================================================== */

/**
 * Factory for creating virtual scrolling grid instances.
 *
 * @namespace
 */
const VirtualGrid = {
    /**
     * Creates a new VirtualGrid instance.
     *
     * @param {Object} config - Configuration object
     * @param {HTMLElement} config.container - Scroll container element
     * @param {HTMLElement} config.grid - Grid element for items (can be same as container)
     * @param {Function} config.getItems - Returns current data array
     * @param {Function} config.getItemId - Extracts unique ID from an item
     * @param {Function} config.createItem - Creates DOM element for an item (item, index) => HTMLElement
     * @param {Function} [config.onItemVisible] - Called when item enters render zone (item, element)
     * @param {Function} [config.onItemRemoved] - Called when item leaves retain zone (id)
     * @param {Function} [config.getThumbnailId] - Gets thumbnail imageId from item for ThumbnailLoader (item) => string
     * @param {string} [config.itemSelector='.grid-item'] - CSS selector for items
     * @param {number} [config.bufferRows=3] - Extra rows to pre-render above/below viewport
     * @param {number} [config.retainRows=30] - Extra rows to keep cached once rendered
     * @param {number} [config.gap=16] - Gap between items in pixels
     * @param {number} [config.padding=16] - Container padding in pixels
     * @param {Function} [config.getItemHeight] - Custom item height calculator (thumbSize, itemWidth) => height
     * @returns {Object} VirtualGrid instance
     */
    create(config) {
        const instance = {
            // Configuration
            _config: {
                container: config.container,
                grid: config.grid || config.container,
                getItems: config.getItems,
                getItemId: config.getItemId,
                createItem: config.createItem,
                onItemVisible: config.onItemVisible || null,
                onItemRemoved: config.onItemRemoved || null,
                getThumbnailId: config.getThumbnailId || null,
                itemSelector: config.itemSelector || '.grid-item',
                bufferRows: config.bufferRows ?? 3,
                retainRows: config.retainRows ?? 30,
                gap: config.gap ?? 16,
                padding: config.padding ?? 16,
                getItemHeight: config.getItemHeight || null
            },

            // Virtual scroll state
            _state: {
                itemHeight: 0,
                itemWidth: 0,
                itemsPerRow: 0,
                visibleRows: 0,
                totalHeight: 0,
                startIndex: -1,
                endIndex: -1,
                renderedItems: new Map(),  // id -> HTMLElement
                scrollRAF: null
            },

            // DOM elements
            _topSpacer: null,
            _bottomSpacer: null,
            _scrollHandler: null,
            _resizeHandler: null,
            _bound: false,

            /**
             * Initializes the virtual grid (creates spacers, binds handlers).
             */
            _init() {
                // Create spacer elements
                this._topSpacer = document.createElement('div');
                this._topSpacer.className = 'virtual-spacer';
                this._bottomSpacer = document.createElement('div');
                this._bottomSpacer.className = 'virtual-spacer';

                // Bind handlers
                this._scrollHandler = this._onScroll.bind(this);
                this._resizeHandler = App.debounce(() => {
                    if (this._bound && this._config.getItems().length > 0) {
                        this._calculateDimensions();
                        this._state.startIndex = -1;
                        this._state.endIndex = -1;
                        this._updateVisibleItems(this._config.container.scrollTop);
                    }
                }, 100);

                window.addEventListener('resize', this._resizeHandler);
            },

            /**
             * Handles scroll events with RAF throttling.
             * @param {Event} e - Scroll event
             * @private
             */
            _onScroll(e) {
                if (this._state.scrollRAF) return;
                const scrollTop = e.target.scrollTop;
                this._state.scrollRAF = requestAnimationFrame(() => {
                    this._state.scrollRAF = null;
                    this._updateVisibleItems(scrollTop);
                });
            },

            /**
             * Calculates dimensions for virtual scrolling.
             * @private
             */
            _calculateDimensions() {
                const container = this._config.container;
                const thumbSize = App.getThumbnailSize();
                const gap = this._config.gap;
                const padding = this._config.padding;

                // Calculate available width
                const availableWidth = container.clientWidth - padding * 2;

                // Calculate items per row (CSS grid auto-fill behavior)
                const minItemWidth = thumbSize + 16; // Item includes some padding
                this._state.itemsPerRow = Math.max(1, Math.floor((availableWidth + gap) / (minItemWidth + gap)));

                // Actual item width when using 1fr
                const actualItemWidth = (availableWidth - gap * (this._state.itemsPerRow - 1)) / this._state.itemsPerRow;
                this._state.itemWidth = actualItemWidth;

                // Item height - use custom calculator if provided
                if (this._config.getItemHeight) {
                    this._state.itemHeight = this._config.getItemHeight(thumbSize, actualItemWidth) + gap;
                } else {
                    // Default: square thumbnail + label area
                    const thumbnailHeight = actualItemWidth - 16; // Minus item padding
                    const labelHeight = 24;
                    this._state.itemHeight = thumbnailHeight + labelHeight + 16 + gap;
                }

                // Calculate visible rows
                const containerHeight = container.clientHeight;
                this._state.visibleRows = Math.ceil(containerHeight / this._state.itemHeight) + 1;

                // Calculate total height
                const items = this._config.getItems();
                const totalRows = Math.ceil(items.length / this._state.itemsPerRow);
                this._state.totalHeight = totalRows * this._state.itemHeight;
            },

            /**
             * Updates visible items based on scroll position.
             * @param {number} scrollTop - Current scroll position
             * @private
             */
            _updateVisibleItems(scrollTop) {
                const state = this._state;
                const config = this._config;
                const items = config.getItems();
                const grid = config.grid;

                if (items.length === 0) return;

                const totalRows = Math.ceil(items.length / state.itemsPerRow);
                const firstVisibleRow = Math.floor(scrollTop / state.itemHeight);

                // Render zone: must have these items in DOM
                const renderStartRow = Math.max(0, firstVisibleRow - config.bufferRows);
                const renderEndRow = Math.min(totalRows, firstVisibleRow + state.visibleRows + config.bufferRows);

                // Retain zone: keep these items cached longer
                const retainStartRow = Math.max(0, firstVisibleRow - config.retainRows);
                const retainEndRow = Math.min(totalRows, firstVisibleRow + state.visibleRows + config.retainRows);

                // Convert to item indices
                const renderStart = renderStartRow * state.itemsPerRow;
                const renderEnd = Math.min(renderEndRow * state.itemsPerRow, items.length);
                const retainStart = retainStartRow * state.itemsPerRow;
                const retainEnd = Math.min(retainEndRow * state.itemsPerRow, items.length);

                // Visible zone (for ThumbnailLoader priority)
                const visibleStart = firstVisibleRow * state.itemsPerRow;
                const visibleEnd = Math.min((firstVisibleRow + state.visibleRows) * state.itemsPerRow, items.length);

                // Track what we need
                const neededIds = new Set();
                for (let i = renderStart; i < renderEnd; i++) {
                    neededIds.add(config.getItemId(items[i]));
                }

                // Remove items outside retain zone
                const currentItems = grid.querySelectorAll(config.itemSelector);
                for (const el of currentItems) {
                    const id = el.dataset.id || el.dataset.groupHash;
                    const idx = items.findIndex(item => config.getItemId(item) === id);
                    if (idx === -1 || idx < retainStart || idx >= retainEnd) {
                        state.renderedItems.delete(id);
                        el.remove();
                        // Notify removal
                        if (config.onItemRemoved) {
                            config.onItemRemoved(id);
                        }
                    }
                }

                // Add missing items in render zone
                for (let i = renderStart; i < renderEnd; i++) {
                    const item = items[i];
                    const id = config.getItemId(item);
                    if (!state.renderedItems.has(id)) {
                        const el = config.createItem(item, i);
                        state.renderedItems.set(id, el);
                        this._insertItemAtPosition(el, i);
                        // Notify visibility
                        if (config.onItemVisible) {
                            config.onItemVisible(item, el);
                        }
                    }
                }

                // Update spacer heights
                let minRenderedIdx = Infinity;
                let maxRenderedIdx = -1;
                for (const [id] of state.renderedItems) {
                    const idx = items.findIndex(item => config.getItemId(item) === id);
                    if (idx !== -1) {
                        minRenderedIdx = Math.min(minRenderedIdx, idx);
                        maxRenderedIdx = Math.max(maxRenderedIdx, idx);
                    }
                }

                if (minRenderedIdx !== Infinity) {
                    const topRow = Math.floor(minRenderedIdx / state.itemsPerRow);
                    const bottomRow = Math.floor(maxRenderedIdx / state.itemsPerRow) + 1;
                    const topHeight = topRow * state.itemHeight;
                    const bottomHeight = Math.max(0, (totalRows - bottomRow) * state.itemHeight);

                    this._topSpacer.style.height = topHeight + 'px';
                    this._bottomSpacer.style.height = bottomHeight + 'px';
                }

                state.startIndex = renderStart;
                state.endIndex = renderEnd;

                // Update ThumbnailLoader priorities if we have a getThumbnailId function
                if (config.getThumbnailId) {
                    const visibleIds = [];
                    const bufferIds = [];

                    for (let i = renderStart; i < renderEnd; i++) {
                        const thumbId = config.getThumbnailId(items[i]);
                        if (thumbId) {
                            if (i >= visibleStart && i < visibleEnd) {
                                visibleIds.push(thumbId);
                            } else {
                                bufferIds.push(thumbId);
                            }
                        }
                    }

                    ThumbnailLoader.prioritize(visibleIds, bufferIds);
                }
            },

            /**
             * Inserts an item at the correct position in the grid.
             * @param {HTMLElement} el - Element to insert
             * @param {number} targetIndex - Target index in items array
             * @private
             */
            _insertItemAtPosition(el, targetIndex) {
                const grid = this._config.grid;
                const items = this._config.getItems();
                const getItemId = this._config.getItemId;
                const selector = this._config.itemSelector;

                // Find the right position among existing items
                const existingItems = grid.querySelectorAll(selector);
                let insertBefore = this._bottomSpacer;

                for (const existing of existingItems) {
                    const existingId = existing.dataset.id || existing.dataset.groupHash;
                    const existingIdx = items.findIndex(item => getItemId(item) === existingId);
                    if (existingIdx > targetIndex) {
                        insertBefore = existing;
                        break;
                    }
                }

                grid.insertBefore(el, insertBefore);
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
             * Clears existing items and re-renders visible items.
             */
            render() {
                const grid = this._config.grid;
                const items = this._config.getItems();

                // Clear existing content and cache
                grid.innerHTML = '';
                this._state.renderedItems.clear();
                this._state.startIndex = -1;
                this._state.endIndex = -1;

                // Handle empty state
                if (items.length === 0) {
                    return;
                }

                // Calculate dimensions
                this._calculateDimensions();

                // Add spacers
                grid.appendChild(this._topSpacer);
                grid.appendChild(this._bottomSpacer);

                // Render initial visible items
                this._updateVisibleItems(this._config.container.scrollTop);

                // Attach scroll listener
                this._attachScrollListener();
                this._bound = true;
            },

            /**
             * Refreshes the grid without full re-render.
             * Recalculates dimensions and updates visible items.
             */
            refresh() {
                if (!this._bound) return;

                const container = this._config.container;
                this._calculateDimensions();
                this._state.startIndex = -1;
                this._state.endIndex = -1;
                this._updateVisibleItems(container.scrollTop);
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

                const row = Math.floor(index / this._state.itemsPerRow);
                const targetY = row * this._state.itemHeight;

                // Check if item is already visible
                const viewTop = container.scrollTop;
                const viewBottom = viewTop + container.clientHeight;
                const itemBottom = targetY + this._state.itemHeight;

                if (targetY < viewTop) {
                    container.scrollTo({ top: targetY, behavior });
                } else if (itemBottom > viewBottom) {
                    container.scrollTo({ top: itemBottom - container.clientHeight, behavior });
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
             * Gets the currently visible item range.
             * @returns {{start: number, end: number}} Start and end indices
             */
            getVisibleRange() {
                return {
                    start: this._state.startIndex,
                    end: this._state.endIndex
                };
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
             * Gets the rendered item element for an ID.
             * @param {string} id - Item ID
             * @returns {HTMLElement|null} Element or null if not rendered
             */
            getRenderedElement(id) {
                return this._state.renderedItems.get(id) || null;
            },

            /**
             * Updates visual state for a rendered item (e.g., selection).
             * @param {string} id - Item ID
             * @param {string} className - Class to toggle
             * @param {boolean} state - Add or remove
             */
            setItemClass(id, className, state) {
                const el = this._state.renderedItems.get(id);
                if (el) {
                    el.classList.toggle(className, state);
                }
            },

            /**
             * Unbinds scroll listener (for screen leave).
             */
            unbind() {
                this._detachScrollListener();
                this._bound = false;
            },

            /**
             * Rebinds scroll listener (for screen enter).
             */
            bind() {
                this._attachScrollListener();
                this._bound = true;
            },

            /**
             * Cleans up all resources.
             */
            destroy() {
                this._detachScrollListener();
                window.removeEventListener('resize', this._resizeHandler);

                if (this._state.scrollRAF) {
                    cancelAnimationFrame(this._state.scrollRAF);
                }

                this._state.renderedItems.clear();
                this._config.grid.innerHTML = '';
                this._bound = false;
            }
        };

        // Initialize
        instance._init();

        return instance;
    }
};

// Make VirtualGrid available globally
window.VirtualGrid = VirtualGrid;
