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
