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
     * Sequence counter for LIFO ordering within priority levels.
     * Higher = more recent = should load first.
     * @type {number}
     * @private
     */
    _sequence: 0,

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
            // Update priority and sequence if higher priority (makes it "fresh" again)
            if (priorityNum > existing.priority) {
                existing.priority = priorityNum;
                existing.sequence = ++this._sequence;
            }
            return;
        }

        // New request - add to queue with sequence number for LIFO ordering
        this._queue.push({
            imageId,
            priority: priorityNum,
            sequence: ++this._sequence,
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

        // Update priorities in queue, refreshing sequence for items that become visible
        for (const item of this._queue) {
            const oldPriority = item.priority;
            if (visibleSet.has(item.imageId)) {
                item.priority = this.PRIORITY.visible;
                // Refresh sequence when becoming visible (makes it LIFO among visible)
                if (oldPriority !== this.PRIORITY.visible) {
                    item.sequence = ++this._sequence;
                }
            } else if (bufferSet.has(item.imageId)) {
                item.priority = this.PRIORITY.buffer;
            } else {
                item.priority = this.PRIORITY.background;
            }
        }

        // Sort queue: highest priority first, then LIFO within priority (higher sequence = more recent)
        this._queue.sort((a, b) => (b.priority - a.priority) || (b.sequence - a.sequence));

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
        this._sequence = 0;
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
    },

    /**
     * Re-sorts the queue and processes it.
     * Call after batch-adding items via request() to ensure proper ordering.
     */
    flush() {
        this._queue.sort((a, b) => (b.priority - a.priority) || (b.sequence - a.sequence));
        this._processQueue();
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

                // Calculate items per row (must match CSS grid auto-fill behavior)
                // CSS: grid-template-columns: repeat(auto-fill, minmax(var(--thumb-size), 1fr))
                // Formula: floor((availableWidth + gap) / (thumbSize + gap))
                this._state.itemsPerRow = Math.max(1, Math.floor((availableWidth + gap) / (thumbSize + gap)));

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

                // Build id->index map for O(1) lookups (avoids O(n) findIndex calls)
                const idToIndex = new Map();
                for (let i = retainStart; i < retainEnd; i++) {
                    idToIndex.set(config.getItemId(items[i]), i);
                }

                // Remove items outside retain zone
                const currentItems = grid.querySelectorAll(config.itemSelector);
                for (const el of currentItems) {
                    const id = el.dataset.id || el.dataset.groupHash;
                    const idx = idToIndex.get(id);
                    if (idx === undefined) {
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
                        this._insertItemAtPosition(el, i, idToIndex);
                        // Notify visibility
                        if (config.onItemVisible) {
                            config.onItemVisible(item, el);
                        }
                    }
                }

                // Update spacer heights based on render zone
                // (no need to iterate renderedItems - we know the range)
                const topRow = renderStartRow;
                const bottomRow = renderEndRow;
                const topHeight = topRow * state.itemHeight;
                const bottomHeight = Math.max(0, (totalRows - bottomRow) * state.itemHeight);

                this._topSpacer.style.height = topHeight + 'px';
                this._bottomSpacer.style.height = bottomHeight + 'px';

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

                    // Re-request thumbnails for visible items that were pruned from the queue.
                    // Items in retain zone keep their DOM element but may have had their
                    // thumbnail request pruned when they were in the background priority.
                    // When they become visible again, onItemVisible isn't called (already rendered),
                    // so we need to explicitly re-request their thumbnails here.
                    let requeued = false;
                    for (let i = visibleStart; i < visibleEnd; i++) {
                        const item = items[i];
                        const id = config.getItemId(item);
                        const el = state.renderedItems.get(id);
                        if (el && !el.classList.contains('loaded')) {
                            const thumbId = config.getThumbnailId(item);
                            if (thumbId) {
                                const img = el.querySelector('img');
                                if (img) {
                                    ThumbnailLoader.request(thumbId, img, 'visible');
                                    requeued = true;
                                }
                            }
                        }
                    }

                    // If we re-queued any items, flush to re-sort and process
                    if (requeued) {
                        ThumbnailLoader.flush();
                    }
                }
            },

            /**
             * Inserts an item at the correct position in the grid.
             * @param {HTMLElement} el - Element to insert
             * @param {number} targetIndex - Target index in items array
             * @private
             */
            _insertItemAtPosition(el, targetIndex, idToIndex) {
                const grid = this._config.grid;
                const selector = this._config.itemSelector;

                // Find the right position among existing items
                const existingItems = grid.querySelectorAll(selector);
                let insertBefore = this._bottomSpacer;

                for (const existing of existingItems) {
                    const existingId = existing.dataset.id || existing.dataset.groupHash;
                    const existingIdx = idToIndex.get(existingId);
                    if (existingIdx !== undefined && existingIdx > targetIndex) {
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


/* ==========================================================================
   GRID SELECTION

   Unified selection handling for thumbnail grids.
   Supports click, keyboard, long-press, and drag-box selection.
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
    AUTO_SCROLL_SPEED: 15,

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
                enableKeyboard: config.enableKeyboard !== false,
                enableDragBox: config.enableDragBox !== false,
                enableLongPress: config.enableLongPress !== false
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
             * Gets the container element from the grid.
             * @returns {HTMLElement}
             * @private
             */
            _getContainer() {
                return this._config.grid._config.container;
            },

            /**
             * Gets the grid element from the grid.
             * @returns {HTMLElement}
             * @private
             */
            _getGridElement() {
                return this._config.grid._config.grid;
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

                const id = this._getItemId(e.target);
                if (!id) return;

                this._longPressTriggered = false;
                this._longPressTimer = setTimeout(() => {
                    this._longPressTriggered = true;
                    // Add to selection without clearing
                    if (!this._selected.has(id)) {
                        this._selected.add(id);
                        this._updateItemVisualState(id, true);
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
                    scrollDirection: 0
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
                    const boxRect = box.getBoundingClientRect();
                    const gridEl = this._getGridElement();
                    const items = gridEl.querySelectorAll(this._config.itemSelector);
                    const idsInBox = [];

                    for (const item of items) {
                        const itemRect = item.getBoundingClientRect();
                        if (this._rectsIntersect(boxRect, itemRect)) {
                            const id = item.dataset.id || item.dataset.groupHash;
                            if (id) idsInBox.push(id);
                        }
                    }

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

            /**
             * Checks if two rectangles intersect.
             * @param {DOMRect} r1
             * @param {DOMRect} r2
             * @returns {boolean}
             * @private
             */
            _rectsIntersect(r1, r2) {
                return !(r1.right < r2.left || r1.left > r2.right ||
                         r1.bottom < r2.top || r1.top > r2.bottom);
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

                // Ignore if typing in an input field
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

                switch (e.key) {
                    case 'ArrowLeft':
                        e.preventDefault();
                        this.navigateRelative(-1, e.shiftKey);
                        break;
                    case 'ArrowRight':
                        e.preventDefault();
                        this.navigateRelative(1, e.shiftKey);
                        break;
                    case 'ArrowUp':
                        e.preventDefault();
                        this.navigateVertical(-1, e.shiftKey);
                        break;
                    case 'ArrowDown':
                        e.preventDefault();
                        this.navigateVertical(1, e.shiftKey);
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
                const changed = this._selected.size !== 1 || !this._selected.has(id);
                if (!changed) return;

                // Update visual state for previously selected
                for (const prevId of this._selected) {
                    this._updateItemVisualState(prevId, false);
                }

                this._selected.clear();
                this._selected.add(id);
                this._updateItemVisualState(id, true);
                this._notifySelectionChanged();
            },

            /**
             * Toggles selection of an item.
             * @param {string} id - Item ID to toggle
             */
            toggle(id) {
                if (this._selected.has(id)) {
                    this._selected.delete(id);
                    this._updateItemVisualState(id, false);
                } else {
                    this._selected.add(id);
                    this._updateItemVisualState(id, true);
                }
                this._notifySelectionChanged();
            },

            /**
             * Selects a range of items from anchor to target.
             * @param {string} anchorId - Starting item ID
             * @param {string} targetId - Ending item ID
             */
            selectRange(anchorId, targetId) {
                const items = this._config.getItems();
                const getItemId = this._config.getItemId;

                const anchorIdx = items.findIndex(item => getItemId(item) === anchorId);
                const targetIdx = items.findIndex(item => getItemId(item) === targetId);

                if (anchorIdx === -1 || targetIdx === -1) {
                    this.select(targetId);
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
                    const id = getItemId(items[i]);
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
                    this._selected.add(getItemId(item));
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
                return this._selected.has(id);
            },

            /**
             * Sets selection to specific IDs.
             * @param {Array<string>} ids - Item IDs to select
             */
            setSelected(ids) {
                // Clear previous selection visual state
                for (const prevId of this._selected) {
                    this._updateItemVisualState(prevId, false);
                }

                this._selected.clear();

                for (const id of ids) {
                    this._selected.add(id);
                    this._updateItemVisualState(id, true);
                }

                if (ids.length > 0) {
                    this._anchor = ids[ids.length - 1];
                }

                this._notifySelectionChanged();
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
                    currentIndex = items.findIndex(item => getItemId(item) === lastSelected);
                }

                let newIndex = currentIndex + delta;
                if (newIndex < 0) newIndex = items.length - 1;
                if (newIndex >= items.length) newIndex = 0;

                const newId = getItemId(items[newIndex]);

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
                    currentIndex = items.findIndex(item => getItemId(item) === lastSelected);
                }

                let newIndex = currentIndex + (delta * itemsPerRow);
                if (newIndex < 0) newIndex = 0;
                if (newIndex >= items.length) newIndex = items.length - 1;

                const newId = getItemId(items[newIndex]);

                if (extend && this._anchor) {
                    this.selectRange(this._anchor, newId);
                } else {
                    this.select(newId);
                    this._anchor = newId;
                }

                this._config.grid.scrollTo(newIndex);
            },

            /**
             * Binds all event listeners.
             */
            bind() {
                if (this._bound) return;

                const gridEl = this._getGridElement();

                // Click handlers
                this._handlers.click = (e) => this._handleClick(e);
                this._handlers.contextmenu = (e) => this._handleRightClick(e);
                this._handlers.dblclick = (e) => this._handleDoubleClick(e);

                gridEl.addEventListener('click', this._handlers.click);
                gridEl.addEventListener('contextmenu', this._handlers.contextmenu);
                gridEl.addEventListener('dblclick', this._handlers.dblclick);

                // Long-press handlers
                if (this._config.enableLongPress) {
                    this._handlers.pointerdown = (e) => this._handlePointerDown(e);
                    this._handlers.pointerup = () => this._handlePointerUp();
                    this._handlers.pointerleave = () => this._handlePointerUp();

                    gridEl.addEventListener('pointerdown', this._handlers.pointerdown);
                    gridEl.addEventListener('pointerup', this._handlers.pointerup);
                    gridEl.addEventListener('pointerleave', this._handlers.pointerleave);
                }

                // Drag-box handler
                if (this._config.enableDragBox) {
                    this._handlers.mousedown = (e) => this._handleDragStart(e);
                    gridEl.addEventListener('mousedown', this._handlers.mousedown);
                }

                // Keyboard handler
                if (this._config.enableKeyboard) {
                    this._handlers.keydown = (e) => this._handleKeyDown(e);
                    document.addEventListener('keydown', this._handlers.keydown);
                }

                this._bound = true;
            },

            /**
             * Unbinds all event listeners.
             */
            unbind() {
                if (!this._bound) return;

                const gridEl = this._getGridElement();

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

                if (this._config.enableKeyboard) {
                    document.removeEventListener('keydown', this._handlers.keydown);
                }

                // Cleanup any in-progress drag
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
             * Cleans up all resources.
             */
            destroy() {
                this.unbind();
                this._selected.clear();
                this._anchor = null;
            }
        };

        return instance;
    }
};

// Make GridSelection available globally
window.GridSelection = GridSelection;
