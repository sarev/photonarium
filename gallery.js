/**
 * @fileoverview Gallery screen module for the Imaginary application.
 *
 * This module handles all functionality specific to the Gallery screen,
 * which is the primary view for browsing the image catalogue. It registers
 * with the core App module and responds to screen lifecycle events.
 *
 * RESPONSIBILITIES:
 *
 * Thumbnail Grid:
 *   - Renders image thumbnails in a responsive CSS grid layout
 *   - Supports dynamic thumbnail size adjustment (smaller/larger buttons)
 *   - Displays image basename beneath each thumbnail
 *   - Implements lazy loading for thumbnails as they scroll into view
 *   - Handles empty state when no images match the current filter
 *
 * Image Selection:
 *   - Single click/tap clears selection and selects the clicked image
 *   - Ctrl+click (or Cmd+click on Mac) toggles selection of clicked image
 *   - Shift+click selects range from last clicked image to current
 *   - Tap-and-hold (long press) adds image to existing selection
 *   - Right-click toggles selection state without affecting other selections
 *   - Drag-box selection: left-drag selects range, right-drag toggles range
 *   - Select all / clear selection toolbar buttons
 *   - Tracks selection state and updates toolbar button states accordingly
 *
 * Info Panel:
 *   - Displays metadata for the currently selected image (single selection only)
 *   - Shows selection count only when multiple images are selected
 *   - Shows: path, dimensions, file size, timestamps, checksum, perceptual hash
 *   - Shows computed values: Laplacian variance, lossless compression flag
 *   - Provides editable fields for Description and Rating
 *   - Rating field includes emoji keyboard trigger button
 *   - Saves edits to backend via API when fields lose focus
 *
 * Sorting:
 *   - Sort by date (newest/oldest first)
 *   - Sort by rating
 *   - Sort by content similarity (OpenCLIP embedding distance from reference)
 *   - Toggle sort direction with visual indicator
 *   - Persists sort preference to localStorage
 *
 * Filtering:
 *   - Applies filters set by the Search screen
 *   - Updates filter button appearance when filter is active
 *   - Clicking filter button when active clears the filter
 *
 * Navigation:
 *   - Double-click thumbnail to open Full-screen view
 *   - Keyboard navigation (arrow keys) through thumbnails
 *   - Maintains scroll position when returning from other screens
 *
 * Deletion:
 *   - Delete key or toolbar button triggers deletion of selected images
 *   - Shows confirmation dialog before deletion
 *   - Calls backend API to delete files and remove from database
 *   - Updates grid after successful deletion
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Refreshes grid if data has changed, restores scroll position
 *   - onLeave(): Saves scroll position for restoration on return
 *
 * @module gallery
 * @requires core
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE

   Gallery module registration, state, and lifecycle hooks.
   ========================================================================== */

/**
 * Gallery screen module.
 * @namespace
 */
const Gallery = {
    /**
     * Local state for the gallery screen.
     * @type {Object}
     * @property {Array<Object>} images - Current image list (sorted/filtered)
     * @property {Array<Object>} filteredImages - Currently filtered/displayed images
     * @property {boolean} needsRefresh - Whether grid needs to reload on next enter
     * @property {Object|null} dragState - Current drag selection state
     * @property {Object|null} contentSimilarities - Similarity scores by image ID for content sort
     * @property {string|null} contentReferenceId - Reference image ID for content sort
     */
    state: {
        images: [],
        filteredImages: [],
        needsRefresh: true,
        dragState: null,
        contentSimilarities: null,
        contentReferenceId: null,
        refreshIntervalId: null,
        lastImageCount: 0
    },

    /**
     * Virtual scrolling configuration.
     * @type {Object}
     * @private
     */
    _virtualScroll: {
        itemHeight: 0,      // Height of each item including gap
        itemWidth: 0,       // Width of each item including gap
        itemsPerRow: 0,     // Number of items per row
        visibleRows: 0,     // Number of visible rows
        bufferRows: 3,      // Extra rows to pre-render above/below viewport
        retainRows: 30,     // Extra rows to keep cached once rendered (configurable)
        startIndex: 0,      // First rendered item index
        endIndex: 0,        // Last rendered item index
        renderedItems: new Map(), // Cache of rendered DOM elements by image ID
        scrollHandler: null // Bound scroll handler for cleanup
    },

    /**
     * Scroll indicator overlay element (shows date or rating while scrolling).
     * @type {HTMLElement|null}
     * @private
     */
    _scrollOverlay: null,

    /**
     * Timer for hiding the scroll indicator overlay.
     * @type {number|null}
     * @private
     */
    _scrollOverlayTimer: null,

    /**
     * Current mouse position for overlay positioning.
     * @type {{x: number, y: number}}
     * @private
     */
    _mousePos: { x: 0, y: 0 },

    /**
     * Scroll state when overlay was first shown (for tracking during drag).
     * @type {{scrollTop: number, overlayY: number}|null}
     * @private
     */
    _scrollOverlayAnchor: null,

    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * Initialises the gallery module.
     * Called once during app startup.
     */
    init() {
        // Cache DOM elements
        this._els = {
            grid: App.$('gallery-grid'),
            infoPanel: App.$('info-panel'),
            infoContent: App.$('info-content'),
            similarityControl: App.$('gallery-similarity-control'),
            similaritySlider: App.$('gallery-similarity-slider'),
            similarityValue: App.$('gallery-similarity-value')
        };

        // Set up virtual scrolling
        this._initVirtualScroll();

        // Create scroll indicator overlay
        this._createScrollOverlay();

        // Track mouse position for overlay positioning
        this._mouseTracker = (e) => {
            this._mousePos.x = e.clientX;
            this._mousePos.y = e.clientY;
        };
        document.addEventListener('mousemove', this._mouseTracker, { passive: true });

        // Set up selection handlers
        this._initSelection();

        // Set up similarity slider handler
        this._initSimilaritySlider();

        // Subscribe to app events
        App.on('thumbnailSizeChanged', () => this._onThumbnailSizeChanged());
        App.on('sortChanged', () => this._onSortChanged());
        App.on('filterChanged', () => this._onFilterChanged());
        App.on('selectionChanged', (sel) => this._onSelectionChanged(sel));
        App.on('selectAll', () => this._selectAll());
        App.on('imageRotated', (imageId) => this._onImageRotated(imageId));
    },

    /**
     * Called when entering the gallery screen.
     * @param {*} data - Optional data passed from navigation
     */
    onEnter(data) {
        if (this.state.needsRefresh) {
            this._loadImages();
        } else {
            // Re-attach scroll listener (removed in onLeave)
            this._attachScrollListener();
        }
        // Bind keyboard events
        this._bindKeyboard();
        // Start background refresh while database is updating
        this._startBackgroundRefresh();
    },

    /**
     * Called when leaving the gallery screen.
     */
    onLeave() {
        // Unbind keyboard events
        this._unbindKeyboard();
        // Stop background refresh
        this._stopBackgroundRefresh();
        // Remove scroll listener from grid
        this._detachScrollListener();
        // Hide scroll indicator overlay and clear timer
        if (this._scrollOverlayTimer) {
            clearTimeout(this._scrollOverlayTimer);
            this._scrollOverlayTimer = null;
        }
        if (this._scrollOverlay) {
            this._scrollOverlay.hidden = true;
        }
    },

    /**
     * Attaches the scroll listener for virtual scrolling.
     * @private
     */
    _attachScrollListener() {
        const grid = this._els.grid;
        if (grid && this._virtualScroll.scrollHandler) {
            grid.removeEventListener('scroll', this._virtualScroll.scrollHandler);
            grid.addEventListener('scroll', this._virtualScroll.scrollHandler, { passive: true });
        }
    },

    /**
     * Detaches the scroll listener for virtual scrolling.
     * @private
     */
    _detachScrollListener() {
        const grid = this._els.grid;
        if (grid && this._virtualScroll.scrollHandler) {
            grid.removeEventListener('scroll', this._virtualScroll.scrollHandler);
        }
    },

    /**
     * Marks the gallery as needing a refresh on next enter.
     */
    markNeedsRefresh() {
        this.state.needsRefresh = true;
    },

    /**
     * Loads images from the API and renders the grid.
     * @private
     */
    async _loadImages() {
        console.time('_loadImages total');

        // Show loading indicator only on first load (cache empty)
        if (this._els.grid && App.getCachedImageCount() === 0) {
            this._els.grid.innerHTML = '<div class="gallery-loading">Loading images...</div>';
        }

        try {
            // Use cached images with delta updates for efficiency
            console.time('_loadImages fetch');
            const images = await App.getImages();
            console.timeEnd('_loadImages fetch');
            console.log(`_loadImages: fetched ${images.length} images`);

            console.time('_loadImages sort');
            this.state.images = this._sortImages(images);
            console.timeEnd('_loadImages sort');

            this.state.lastImageCount = images.length;
            this._renderGrid();
            this.state.needsRefresh = false;
        } catch (error) {
            console.error('Failed to load images:', error);
            this.state.images = [];
            if (this._els.grid) {
                this._els.grid.innerHTML = '<div class="gallery-loading">Failed to load images</div>';
            }
        }

        console.timeEnd('_loadImages total');
    },

    /**
     * Starts background refresh polling while database is updating.
     * @private
     */
    _startBackgroundRefresh() {
        // Don't start if already running
        if (this.state.refreshIntervalId) return;

        // Poll every 30 seconds
        this.state.refreshIntervalId = setInterval(() => {
            this._checkForNewImages();
        }, 30000);
    },

    /**
     * Stops background refresh polling.
     * @private
     */
    _stopBackgroundRefresh() {
        if (this.state.refreshIntervalId) {
            clearInterval(this.state.refreshIntervalId);
            this.state.refreshIntervalId = null;
        }
    },

    /**
     * Checks if new images are available and refreshes while preserving state.
     * Only refreshes when database is in updating state.
     * Uses delta updates for efficiency.
     * @private
     */
    async _checkForNewImages() {
        try {
            // Check database status
            const status = await App.apiGet('/status');
            if (status.status !== 'updating') {
                return; // Only refresh while updating
            }

            // Fetch latest images using delta updates
            const images = await App.getImages();

            // Only refresh if image count changed
            if (images.length === this.state.lastImageCount) {
                return;
            }

            // Preserve current state
            const scrollTop = this._els.grid?.scrollTop || 0;
            const currentSelection = App.getSelectedImages();

            // Update images
            this.state.images = this._sortImages(images);
            this.state.lastImageCount = images.length;

            // Re-render grid
            this._renderGrid();

            // Restore scroll position
            if (this._els.grid) {
                this._els.grid.scrollTop = scrollTop;
            }

            // Restore selection (filter to still-existing image IDs)
            const existingIds = new Set(this.state.images.map(img => img.id));
            const validSelection = currentSelection.filter(id => existingIds.has(id));
            if (validSelection.length > 0) {
                App.setSelectedImages(validSelection);
            }

        } catch (error) {
            // Silently ignore errors during background refresh
            console.debug('Background refresh error:', error);
        }
    },

    /**
     * Sorts images based on current sort settings.
     * @param {Array<Object>} images - Images to sort
     * @returns {Array<Object>} Sorted images
     * @private
     */
    _sortImages(images) {
        const { by, direction } = App.getSort();
        const sorted = [...images];

        sorted.sort((a, b) => {
            let cmp = 0;
            if (by === 'date') {
                cmp = new Date(a.timestamp) - new Date(b.timestamp);
            } else if (by === 'rating') {
                cmp = (a.rating || '').localeCompare(b.rating || '');
            } else if (by === 'content') {
                // Sort by content similarity (requires contentSimilarities to be populated)
                const simA = this.state.contentSimilarities?.[a.id] ?? 0;
                const simB = this.state.contentSimilarities?.[b.id] ?? 0;
                cmp = simA - simB;
            }
            return direction === 'asc' ? cmp : -cmp;
        });

        return sorted;
    },

    /**
     * Loads content similarity data for sorting by visual similarity.
     * Uses the first selected image as the reference.
     * @private
     */
    async _loadContentSimilarities() {
        const selected = App.getSelectedImages();
        if (selected.length === 0) {
            // No reference image - show message
            App.showError('Select an image first to sort by visual similarity.');
            // Revert to date sort
            App.setSortBy('date');
            return;
        }

        const referenceId = selected[0];

        try {
            const response = await App.apiGet(`/similar/${referenceId}`);
            if (response && response.results) {
                // Store similarities by image ID
                this.state.contentSimilarities = {};
                this.state.contentReferenceId = referenceId;
                response.results.forEach(img => {
                    this.state.contentSimilarities[img.id] = img.similarity;
                });

                // Re-sort and render
                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();

                // Scroll to top to show most similar images first
                this._scrollToTop();
            }
        } catch (error) {
            console.error('Failed to load content similarities:', error);
            // Check if it's a 404 (image has no embedding yet)
            if (error.message && error.message.includes('404')) {
                App.showError('This image has no embedding yet. Wait for processing to complete, or select a different image.');
            } else {
                App.showError('Could not load similarity data.');
            }
            App.setSortBy('date');
        }
    },

    /**
     * Filters images based on current filter settings.
     * @param {Array<Object>} images - Images to filter
     * @returns {Array<Object>} Filtered images
     * @private
     */
    _filterImages(images) {
        const filter = App.getFilter();
        if (!filter) return images;

        // Duplicates filter: show only a specific set of image IDs
        if (filter.type === 'duplicates' && Array.isArray(filter.imageIds)) {
            const idSet = new Set(filter.imageIds.map(String));
            return images.filter(img => idSet.has(String(img.id)));
        }

        // Semantic search filter: show matching images sorted by score
        if (filter.type === 'semantic' && Array.isArray(filter.imageIds)) {
            const idSet = new Set(filter.imageIds.map(String));
            const scores = filter.scores || {};

            // Filter to matching images
            let filtered = images.filter(img => idSet.has(String(img.id)));

            // Apply additional filters (date range, rating)
            filtered = filtered.filter(img => {
                if (filter.dateStart) {
                    const imgDate = new Date(img.timestamp);
                    const startDate = new Date(filter.dateStart);
                    if (imgDate < startDate) return false;
                }
                if (filter.dateEnd) {
                    const imgDate = new Date(img.timestamp);
                    const endDate = new Date(filter.dateEnd);
                    endDate.setHours(23, 59, 59, 999);
                    if (imgDate > endDate) return false;
                }
                if (filter.rating) {
                    const filterEmoji = [...filter.rating];
                    const hasMatch = filterEmoji.some(e => img.rating && img.rating.includes(e));
                    if (!hasMatch) return false;
                }
                return true;
            });

            // Sort by semantic similarity score (highest first)
            filtered.sort((a, b) => {
                const scoreA = scores[a.id] || 0;
                const scoreB = scores[b.id] || 0;
                return scoreB - scoreA;
            });

            return filtered;
        }

        return images.filter(img => {
            // Text filter (description) - simple substring match fallback
            if (filter.text && !(img.description || '').toLowerCase().includes(filter.text.toLowerCase())) {
                return false;
            }
            // Date range filter
            if (filter.dateStart) {
                const imgDate = new Date(img.timestamp);
                const startDate = new Date(filter.dateStart);
                if (imgDate < startDate) return false;
            }
            if (filter.dateEnd) {
                const imgDate = new Date(img.timestamp);
                const endDate = new Date(filter.dateEnd);
                endDate.setHours(23, 59, 59, 999); // Include entire end day
                if (imgDate > endDate) return false;
            }
            // Rating filter
            if (filter.rating) {
                const filterEmoji = [...filter.rating];
                const hasMatch = filterEmoji.some(e => img.rating && img.rating.includes(e));
                if (!hasMatch) return false;
            }
            return true;
        });
    },

    /* ----------------------------------------------------------------------
       Event Handlers for App Events
       ---------------------------------------------------------------------- */

    /**
     * Handles thumbnail size changes.
     * @private
     */
    _onThumbnailSizeChanged() {
        this._updateGridStyle();
        // Recalculate dimensions and re-render with new size
        this._reloadThumbnails();
    },

    /**
     * Handles sort changes.
     * @private
     */
    _onSortChanged() {
        const { by } = App.getSort();
        const filter = App.getFilter();
        const isSemanticFilter = filter && filter.type === 'semantic';

        if (by === 'content') {
            // For semantic search filters, scores are already in the filter
            // and results are sorted by _applyFilters() - no need to fetch
            if (isSemanticFilter) {
                // Just re-render, _applyFilters handles the sorting
                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();
                return;
            }

            // Content sort requires fetching similarity data from server
            const selected = App.getSelectedImages();
            const referenceId = selected.length > 0 ? selected[0] : null;

            // Check if we already have similarities for this reference
            if (referenceId && this.state.contentReferenceId === referenceId) {
                // Already have the data, just re-sort
                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();
            } else {
                // Need to fetch similarity data
                this._loadContentSimilarities();
            }
        } else {
            // Clear content similarity data when switching away
            this.state.contentSimilarities = null;
            this.state.contentReferenceId = null;
            this.state.images = this._sortImages(this.state.images);
            this._renderGrid();
        }
    },

    /**
     * Handles filter changes.
     * @private
     */
    _onFilterChanged() {
        // Show/hide similarity slider based on filter type
        const filter = App.getFilter();
        const isSemanticFilter = filter && filter.type === 'semantic';

        if (this._els.similarityControl) {
            this._els.similarityControl.style.display = isSemanticFilter ? 'flex' : 'none';

            // Sync slider value with filter threshold if available
            if (isSemanticFilter && filter.threshold) {
                const pct = Math.round(filter.threshold * 100);
                this._els.similaritySlider.value = pct;
                this._els.similarityValue.textContent = pct + '%';
            }
        }

        // Force "Sort by content similarity" (descending) for semantic searches
        if (isSemanticFilter) {
            App.setSortBy('content');
            App.setSortDirection('desc');
        }

        this._loadImages(); // Reload and apply new filter

        // Scroll to top when filter changes
        this._scrollToTop();
    },

    /**
     * Scrolls the gallery grid to the top.
     * @private
     */
    _scrollToTop() {
        if (this._els.grid) {
            this._els.grid.scrollTop = 0;
            // Ensure visible items are updated immediately
            this._updateVisibleItems(0);
        }
    },

    /**
     * Handles selection changes.
     * @param {Array<string>} selection - Selected image IDs
     * @private
     */
    _onSelectionChanged(selection) {
        // Update visual selection state on currently rendered thumbnails
        const items = this._els.grid.querySelectorAll('.gallery-item');
        for (const item of items) {
            item.classList.toggle('selected', selection.includes(item.dataset.id));
        }
        // Update info panel
        this._updateInfoPanel(selection);
    },

    /**
     * Selects all images in the current view.
     * @private
     */
    _selectAll() {
        const allIds = this.state.filteredImages.map(img => img.id);
        App.setSelectedImages(allIds);
    },

    /**
     * Handles image rotation - refreshes the thumbnail.
     * @param {string} imageId - ID of the rotated image
     * @private
     */
    _onImageRotated(imageId) {
        // Remove from virtual scroll cache
        this._virtualScroll.renderedItems.delete(imageId);

        // Find and update the DOM element if it exists
        const item = this._els.grid.querySelector(`.gallery-item[data-id="${imageId}"]`);
        if (item) {
            const img = item.querySelector('img');
            if (img) {
                // Add cache-busting timestamp to force reload
                const newSrc = App.thumbnailUrl(imageId) + '&_t=' + Date.now();
                img.src = newSrc;
            }
        }
    },

    /* ----------------------------------------------------------------------
       THUMBNAIL GRID - VIRTUAL SCROLLING

       Only renders visible items plus a buffer for smooth scrolling.
       This is essential for handling 30,000+ images without lag.
       ---------------------------------------------------------------------- */

    /**
     * Creates the scroll indicator overlay element.
     * @private
     */
    _createScrollOverlay() {
        this._scrollOverlay = document.createElement('div');
        this._scrollOverlay.className = 'scroll-overlay';
        this._scrollOverlay.hidden = true;
        // Will be appended to grid in _renderGrid
    },

    /**
     * Shows the scroll indicator overlay with the given text.
     * @param {string} text - Text to display
     * @param {number} scrollTop - Current scroll position
     * @private
     */
    _showScrollOverlay(text, scrollTop) {
        if (!this._scrollOverlay || !text) return;

        const wasHidden = this._scrollOverlay.hidden;
        this._scrollOverlay.textContent = text;
        this._scrollOverlay.hidden = false;

        // Position overlay
        if (wasHidden) {
            // First show - position based on mouse and anchor the scroll position
            this._positionScrollOverlayAtMouse();
            this._scrollOverlayAnchor = {
                scrollTop: scrollTop,
                overlayY: parseFloat(this._scrollOverlay.style.top) || 0
            };
        } else {
            // Already visible - track scrollbar thumb movement
            this._updateScrollOverlayFromScroll(scrollTop);
        }

        // Clear any existing hide timer
        if (this._scrollOverlayTimer) {
            clearTimeout(this._scrollOverlayTimer);
        }

        // Hide after 1 second of no scrolling
        this._scrollOverlayTimer = setTimeout(() => {
            this._scrollOverlay.hidden = true;
            this._scrollOverlayAnchor = null;
        }, 1000);
    },

    /**
     * Positions the scroll overlay at the current mouse position.
     * @private
     */
    _positionScrollOverlayAtMouse() {
        if (!this._scrollOverlay) return;

        const rect = this._scrollOverlay.getBoundingClientRect();
        const padding = 12; // Gap between overlay and mouse pointer

        // Position so right edge is to the left of mouse pointer
        const left = this._mousePos.x - rect.width - padding;
        // Vertically center on mouse Y
        const top = this._mousePos.y - rect.height / 2;

        // Clamp to viewport bounds
        const clampedLeft = Math.max(8, left);
        const clampedTop = Math.max(8, Math.min(top, window.innerHeight - rect.height - 8));

        this._scrollOverlay.style.left = clampedLeft + 'px';
        this._scrollOverlay.style.top = clampedTop + 'px';
    },

    /**
     * Updates overlay position based on scroll delta (tracks scrollbar thumb).
     * @param {number} scrollTop - Current scroll position
     * @private
     */
    _updateScrollOverlayFromScroll(scrollTop) {
        if (!this._scrollOverlay || !this._scrollOverlayAnchor) return;

        const grid = this._els.grid;
        if (!grid) return;

        // Calculate how much the scrollbar thumb has moved
        const scrollDelta = scrollTop - this._scrollOverlayAnchor.scrollTop;
        const scrollableHeight = grid.scrollHeight - grid.clientHeight;

        if (scrollableHeight <= 0) return;

        // Scrollbar thumb moves proportionally within the track (which is ~clientHeight)
        const gridRect = grid.getBoundingClientRect();
        const trackHeight = gridRect.height;
        const thumbDelta = (scrollDelta / scrollableHeight) * trackHeight;

        // Update overlay Y position
        const newTop = this._scrollOverlayAnchor.overlayY + thumbDelta;
        const rect = this._scrollOverlay.getBoundingClientRect();
        const clampedTop = Math.max(8, Math.min(newTop, window.innerHeight - rect.height - 8));

        this._scrollOverlay.style.top = clampedTop + 'px';
    },

    /**
     * Formats a date for the scroll overlay.
     * @param {Date} date - Date to format
     * @returns {string} Formatted date string
     * @private
     */
    _formatScrollDate(date) {
        if (!(date instanceof Date) || isNaN(date)) {
            return '';
        }

        const now = new Date();
        const isThisYear = date.getFullYear() === now.getFullYear();

        // Format options
        const options = {
            month: 'short',
            day: 'numeric'
        };

        if (!isThisYear) {
            options.year = 'numeric';
        }

        return date.toLocaleDateString(undefined, options);
    },

    /**
     * Initializes virtual scrolling.
     * @private
     */
    _initVirtualScroll() {
        // Create spacer elements for virtual scrolling
        this._topSpacer = document.createElement('div');
        this._topSpacer.className = 'virtual-spacer';
        this._bottomSpacer = document.createElement('div');
        this._bottomSpacer.className = 'virtual-spacer';

        // Load retention setting from localStorage (default: 30 rows)
        // Can be changed via browser console: localStorage.setItem('imaginary-retainRows', '50')
        const savedRetain = localStorage.getItem('imaginary-retainRows');
        if (savedRetain) {
            const retain = parseInt(savedRetain, 10);
            if (!isNaN(retain) && retain >= 0) {
                this._virtualScroll.retainRows = retain;
            }
        }

        // Bind scroll handler
        this._virtualScroll.scrollHandler = this._onScroll.bind(this);

        // Handle window resize
        this._resizeHandler = App.debounce(() => {
            if (App.getScreen() === 'gallery' && this.state.filteredImages.length > 0) {
                const grid = this._els.grid;
                if (grid) {
                    this._calculateVirtualDimensions(grid);
                    // Force re-render
                    this._virtualScroll.startIndex = -1;
                    this._virtualScroll.endIndex = -1;
                    this._updateVisibleItems(grid.scrollTop);
                }
            }
        }, 100);
        window.addEventListener('resize', this._resizeHandler);
    },

    /**
     * Renders the thumbnail grid using virtual scrolling.
     * Only renders visible items plus a buffer.
     * @private
     */
    _renderGrid() {
        console.time('_renderGrid total');
        const grid = this._els.grid;

        // Apply filter
        console.time('_renderGrid filter');
        this.state.filteredImages = this._filterImages(this.state.images);
        console.timeEnd('_renderGrid filter');
        console.log(`_renderGrid: ${this.state.filteredImages.length} images (virtual scroll)`);

        // Clear grid and cache
        grid.innerHTML = '';
        this._virtualScroll.renderedItems.clear();
        this._virtualScroll.startIndex = -1;
        this._virtualScroll.endIndex = -1;

        // Handle empty state
        if (this.state.filteredImages.length === 0) {
            grid.innerHTML = '<div class="empty-state"><span class="material-symbols-outlined">photo_library</span><p>No images to display</p></div>';
            console.timeEnd('_renderGrid total');
            return;
        }

        // Update grid CSS for thumbnail size
        this._updateGridStyle();

        // Calculate virtual scroll dimensions (grid is the scroll container)
        this._calculateVirtualDimensions(grid);

        // Add spacers
        grid.appendChild(this._topSpacer);
        grid.appendChild(this._bottomSpacer);

        // Add scroll indicator overlay
        if (this._scrollOverlay) {
            this._scrollOverlay.hidden = true;
            grid.appendChild(this._scrollOverlay);
        }

        // Render initial visible items
        this._updateVisibleItems(grid.scrollTop);

        // Attach scroll listener
        this._attachScrollListener();

        console.timeEnd('_renderGrid total');
    },

    /**
     * Calculates dimensions for virtual scrolling.
     * Matches CSS grid's auto-fill calculation: repeat(auto-fill, minmax(thumb-size, 1fr))
     * @param {HTMLElement} container - The scroll container
     * @private
     */
    _calculateVirtualDimensions(container) {
        const thumbSize = App.getThumbnailSize();
        const gap = 16; // 1rem gap (from CSS)
        const padding = 16; // 1rem padding (from CSS)
        const itemPadding = 8; // 0.5rem padding on .gallery-item

        // Calculate items per row matching CSS grid auto-fill behavior
        // CSS: repeat(auto-fill, minmax(thumbSize, 1fr))
        const availableWidth = container.clientWidth - padding * 2;
        const minItemWidth = thumbSize + itemPadding * 2; // Item includes its padding

        // auto-fill fits as many minItemWidth columns as possible
        this._virtualScroll.itemsPerRow = Math.max(1, Math.floor((availableWidth + gap) / (minItemWidth + gap)));

        // Actual item width when using 1fr (fills remaining space)
        const actualItemWidth = (availableWidth - gap * (this._virtualScroll.itemsPerRow - 1)) / this._virtualScroll.itemsPerRow;

        // Item height: thumbnail (aspect-ratio: 1) + label margin + label height
        // Thumbnail width = actualItemWidth - itemPadding*2, height = same (square)
        const thumbnailHeight = actualItemWidth - itemPadding * 2;
        const labelHeight = 24; // Approximate: 0.8rem font + 0.5rem margin
        const itemHeight = thumbnailHeight + labelHeight + itemPadding * 2;

        this._virtualScroll.itemWidth = actualItemWidth;
        this._virtualScroll.itemHeight = itemHeight + gap;

        // Calculate visible rows
        const containerHeight = container.clientHeight;
        this._virtualScroll.visibleRows = Math.ceil(containerHeight / this._virtualScroll.itemHeight) + 1;

        // Calculate total height
        const totalRows = Math.ceil(this.state.filteredImages.length / this._virtualScroll.itemsPerRow);
        this._virtualScroll.totalHeight = totalRows * this._virtualScroll.itemHeight;
    },

    /**
     * Handles scroll events for virtual scrolling.
     * @param {Event} e - Scroll event
     * @private
     */
    _onScroll(e) {
        // Throttle scroll updates
        if (this._scrollRAF) return;
        // Capture scrollTop immediately - event object may be recycled by RAF callback
        const scrollTop = e.target.scrollTop;
        this._scrollRAF = requestAnimationFrame(() => {
            this._scrollRAF = null;
            this._updateVisibleItems(scrollTop);
            this._updateScrollOverlay(scrollTop);
        });
    },

    /**
     * Updates the scroll indicator overlay based on the first visible image.
     * Shows date when sorting by date, rating when sorting by rating.
     * @param {number} scrollTop - Current scroll position
     * @private
     */
    _updateScrollOverlay(scrollTop) {
        const { by } = App.getSort();

        // Only show for date and rating sorts
        if (by !== 'date' && by !== 'rating') {
            if (this._scrollOverlay) {
                this._scrollOverlay.hidden = true;
            }
            return;
        }

        const filtered = this.state.filteredImages;
        if (filtered.length === 0) return;

        // Calculate first visible row and image index
        const vs = this._virtualScroll;
        const firstVisibleRow = Math.floor(scrollTop / vs.itemHeight);
        const firstVisibleIndex = firstVisibleRow * vs.itemsPerRow;

        if (firstVisibleIndex < 0 || firstVisibleIndex >= filtered.length) return;

        const img = filtered[firstVisibleIndex];
        if (!img) return;

        // Show appropriate content based on sort type
        if (by === 'date' && img.timestamp) {
            const date = new Date(img.timestamp);
            const formatted = this._formatScrollDate(date);
            if (formatted) {
                this._showScrollOverlay(formatted, scrollTop);
            }
        } else if (by === 'rating') {
            // Show rating (emoji string) or "No rating" if empty
            const rating = img.rating || 'No rating';
            this._showScrollOverlay(rating, scrollTop);
        }
    },

    /**
     * Updates visible items based on scroll position.
     * Uses a retention cache to keep previously-rendered items in DOM longer.
     * @param {number} scrollTop - Current scroll position
     * @private
     */
    _updateVisibleItems(scrollTop) {
        const vs = this._virtualScroll;
        const filtered = this.state.filteredImages;
        const grid = this._els.grid;

        if (filtered.length === 0) return;

        const totalRows = Math.ceil(filtered.length / vs.itemsPerRow);
        const firstVisibleRow = Math.floor(scrollTop / vs.itemHeight);

        // Render zone: must have these items in DOM for smooth scrolling
        const renderStartRow = Math.max(0, firstVisibleRow - vs.bufferRows);
        const renderEndRow = Math.min(totalRows, firstVisibleRow + vs.visibleRows + vs.bufferRows);

        // Retain zone: keep these items cached (larger buffer)
        const retainStartRow = Math.max(0, firstVisibleRow - vs.retainRows);
        const retainEndRow = Math.min(totalRows, firstVisibleRow + vs.visibleRows + vs.retainRows);

        // Convert to item indices
        const renderStart = renderStartRow * vs.itemsPerRow;
        const renderEnd = Math.min(renderEndRow * vs.itemsPerRow, filtered.length);
        const retainStart = retainStartRow * vs.itemsPerRow;
        const retainEnd = Math.min(retainEndRow * vs.itemsPerRow, filtered.length);

        // Track what we need
        const neededIds = new Set();
        for (let i = renderStart; i < renderEnd; i++) {
            neededIds.add(filtered[i].id);
        }

        // Remove items outside retain zone
        const currentItems = grid.querySelectorAll('.gallery-item');
        for (const item of currentItems) {
            const id = item.dataset.id;
            const idx = filtered.findIndex(img => img.id === id);
            if (idx === -1 || idx < retainStart || idx >= retainEnd) {
                vs.renderedItems.delete(id);
                item.remove();
            }
        }

        // Add missing items in render zone
        const selectedImages = App.getSelectedImages();
        for (let i = renderStart; i < renderEnd; i++) {
            const img = filtered[i];
            if (!vs.renderedItems.has(img.id)) {
                const item = this._createThumbnailItem(img, selectedImages);
                vs.renderedItems.set(img.id, item);
                this._insertItemAtPosition(item, i);
            }
        }

        // Update spacer heights based on actual rendered range
        // Find the actual min/max rendered indices
        let minRenderedIdx = Infinity;
        let maxRenderedIdx = -1;
        for (const [id] of vs.renderedItems) {
            const idx = filtered.findIndex(img => img.id === id);
            if (idx !== -1) {
                minRenderedIdx = Math.min(minRenderedIdx, idx);
                maxRenderedIdx = Math.max(maxRenderedIdx, idx);
            }
        }

        if (minRenderedIdx !== Infinity) {
            const topRow = Math.floor(minRenderedIdx / vs.itemsPerRow);
            const bottomRow = Math.floor(maxRenderedIdx / vs.itemsPerRow) + 1;
            const topHeight = topRow * vs.itemHeight;
            const bottomHeight = Math.max(0, (totalRows - bottomRow) * vs.itemHeight);

            this._topSpacer.style.height = topHeight + 'px';
            this._bottomSpacer.style.height = bottomHeight + 'px';
        }

        vs.startIndex = renderStart;
        vs.endIndex = renderEnd;
    },

    /**
     * Inserts an item at the correct position in the grid based on its index.
     * @param {HTMLElement} item - The item to insert
     * @param {number} targetIndex - The index in filteredImages
     * @private
     */
    _insertItemAtPosition(item, targetIndex) {
        const grid = this._els.grid;
        const filtered = this.state.filteredImages;

        // Find the right position among existing items
        const existingItems = grid.querySelectorAll('.gallery-item');
        let insertBefore = this._bottomSpacer;

        for (const existing of existingItems) {
            const existingId = existing.dataset.id;
            const existingIdx = filtered.findIndex(img => img.id === existingId);
            if (existingIdx > targetIndex) {
                insertBefore = existing;
                break;
            }
        }

        grid.insertBefore(item, insertBefore);
    },

    /**
     * Creates a thumbnail item element.
     * @param {Object} img - Image data object
     * @param {Array<string>} [selectedImages] - Currently selected image IDs
     * @returns {HTMLElement} The thumbnail item element
     * @private
     */
    _createThumbnailItem(img, selectedImages) {
        selectedImages = selectedImages || App.getSelectedImages();

        const item = App.createElement('div', {
            className: 'gallery-item',
            dataId: img.id
        });

        // Thumbnail image - load immediately since we only render visible items
        const thumb = App.createElement('img', {
            alt: img.basename,
            src: App.thumbnailUrl(img.id)
        });

        // Basename label
        const label = App.createElement('span', { className: 'gallery-item-label' }, img.basename);

        item.appendChild(thumb);
        item.appendChild(label);

        // Selection state
        if (selectedImages.includes(img.id)) {
            item.classList.add('selected');
        }

        return item;
    },

    /**
     * Updates the grid CSS custom property for thumbnail size.
     * @private
     */
    _updateGridStyle() {
        const size = App.getThumbnailSize();
        this._els.grid.style.setProperty('--thumb-size', size + 'px');
    },

    /**
     * Reloads thumbnails when size changes.
     * With virtual scrolling, we just re-render visible items.
     * @private
     */
    _reloadThumbnails() {
        const grid = this._els.grid;
        if (grid && this.state.filteredImages.length > 0) {
            this._calculateVirtualDimensions(grid);
            // Clear cache and force re-render (thumbnail URLs change with size)
            this._virtualScroll.renderedItems.clear();
            const items = grid.querySelectorAll('.gallery-item');
            for (const item of items) {
                item.remove();
            }
            this._virtualScroll.startIndex = -1;
            this._virtualScroll.endIndex = -1;
            this._updateVisibleItems(grid.scrollTop);
        }
    },

    /**
     * Gets the image ID from a thumbnail element or its children.
     * @param {HTMLElement} el - Element to check
     * @returns {string|null} Image ID or null
     * @private
     */
    _getImageId(el) {
        const item = el.closest('.gallery-item');
        return item ? item.dataset.id : null;
    },

    /**
     * Gets the thumbnail element for an image ID.
     * @param {string} id - Image ID
     * @returns {HTMLElement|null} Thumbnail element or null
     * @private
     */
    _getItemElement(id) {
        return this._els.grid.querySelector(`.gallery-item[data-id="${id}"]`);
    },

    /* ----------------------------------------------------------------------
       IMAGE SELECTION

       Click, right-click, long-press, and drag-box selection.
       ---------------------------------------------------------------------- */

    /**
     * Long-press threshold in milliseconds.
     * @type {number}
     * @constant
     */
    LONG_PRESS_MS: 500,

    /**
     * Binds selection event listeners to the grid.
     * Called during init.
     * @private
     */
    _initSelection() {
        const grid = this._els.grid;

        // Click handling
        grid.addEventListener('click', (e) => this._handleClick(e));
        grid.addEventListener('contextmenu', (e) => this._handleRightClick(e));
        grid.addEventListener('dblclick', (e) => this._handleDoubleClick(e));

        // Long-press handling
        grid.addEventListener('pointerdown', (e) => this._handlePointerDown(e));
        grid.addEventListener('pointerup', () => this._handlePointerUp());
        grid.addEventListener('pointerleave', () => this._handlePointerUp());

        // Drag-box selection
        grid.addEventListener('mousedown', (e) => this._handleDragStart(e));
    },

    /**
     * Initialises the similarity slider for semantic search filtering.
     * @private
     */
    _initSimilaritySlider() {
        if (!this._els.similaritySlider) return;

        // Debounce timer for slider changes
        let debounceTimer = null;

        // Update display value as slider moves and sync with search screen slider
        this._els.similaritySlider.addEventListener('input', () => {
            const value = this._els.similaritySlider.value;
            this._els.similarityValue.textContent = value + '%';

            // Sync search screen slider if it exists
            const searchSimilaritySlider = App.$('filter-similarity');
            const searchSimilarityValue = App.$('similarity-value');
            if (searchSimilaritySlider) {
                searchSimilaritySlider.value = value;
            }
            if (searchSimilarityValue) {
                searchSimilarityValue.textContent = value + '%';
            }
        });

        // Re-run search when slider value changes (debounced)
        this._els.similaritySlider.addEventListener('change', async () => {
            const filter = App.getFilter();
            if (!filter || filter.type !== 'semantic' || !filter.text) return;

            // Clear any pending debounce
            if (debounceTimer) clearTimeout(debounceTimer);

            debounceTimer = setTimeout(async () => {
                const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

                App.showLoading('Searching...');
                try {
                    // Re-run the search with new threshold
                    const response = await App.apiPost('/search', {
                        query: filter.text,
                        threshold: threshold,
                        limit: 500
                    });

                    if (response && response.results) {
                        // Update filter with new results
                        filter.threshold = threshold;
                        filter.imageIds = response.results.map(r => r.id);
                        filter.scores = {};
                        response.results.forEach(r => {
                            filter.scores[r.id] = r.score;
                        });

                        // Update filter without triggering full reload
                        App.setFilter(filter, { silent: true });

                        // Re-render the grid with new filter
                        this._renderGrid();
                    }
                } catch (error) {
                    console.error('Failed to update search:', error);
                    App.showError('Failed to update search results.');
                } finally {
                    App.hideLoading();
                }
            }, 100);
        });
    },

    /**
     * Handles left-click on grid.
     * Clears selection and selects clicked image.
     * @param {MouseEvent} e
     * @private
     */
    /**
     * Anchor image ID for shift-click range selection.
     * @type {string|null}
     * @private
     */
    _selectionAnchor: null,

    _handleClick(e) {
        // Ignore if this was a long-press or drag
        if (this._longPressTriggered || this.state.dragState?.dragged || this._justDragged) {
            this._longPressTriggered = false;
            this._justDragged = false;
            return;
        }

        const id = this._getImageId(e.target);
        if (id) {
            if (e.ctrlKey || e.metaKey) {
                // Ctrl+click: Toggle selection of clicked item
                App.toggleSelection(id);
                // Update anchor to this item
                this._selectionAnchor = id;
            } else if (e.shiftKey && this._selectionAnchor) {
                // Shift+click: Select range from anchor to clicked item
                this._selectRange(this._selectionAnchor, id);
            } else {
                // Regular click: Select only this item
                App.setSelectedImages([id]);
                // Set anchor for future shift-clicks
                this._selectionAnchor = id;
            }
        } else {
            // Clicked on empty space - clear selection
            App.clearSelection();
            this._selectionAnchor = null;
        }
    },

    /**
     * Selects all images in the range between two image IDs (inclusive).
     * Uses filteredImages to match the displayed order.
     * @param {string} anchorId - Starting image ID
     * @param {string} targetId - Ending image ID
     * @private
     */
    _selectRange(anchorId, targetId) {
        const images = this.state.filteredImages;
        const anchorIdx = images.findIndex(img => img.id === anchorId);
        const targetIdx = images.findIndex(img => img.id === targetId);

        if (anchorIdx === -1 || targetIdx === -1) {
            // Fallback: just select the target
            App.setSelectedImages([targetId]);
            return;
        }

        const startIdx = Math.min(anchorIdx, targetIdx);
        const endIdx = Math.max(anchorIdx, targetIdx);

        const rangeIds = [];
        for (let i = startIdx; i <= endIdx; i++) {
            rangeIds.push(images[i].id);
        }

        App.setSelectedImages(rangeIds);
    },

    /**
     * Handles right-click on grid.
     * Toggles selection without affecting other selections.
     * @param {MouseEvent} e
     * @private
     */
    _handleRightClick(e) {
        e.preventDefault();
        const id = this._getImageId(e.target);
        if (id) {
            App.toggleSelection(id);
        }
    },

    /**
     * Handles double-click on grid.
     * Opens full-screen view.
     * @param {MouseEvent} e
     * @private
     */
    _handleDoubleClick(e) {
        const id = this._getImageId(e.target);
        if (id) {
            App.showFullscreen(id);
        }
    },

    /**
     * Long-press timer reference.
     * @type {number|null}
     * @private
     */
    _longPressTimer: null,

    /**
     * Flag indicating long-press was triggered.
     * @type {boolean}
     * @private
     */
    _longPressTriggered: false,

    /**
     * Flag indicating drag-box selection just completed.
     * Prevents click handler from clearing the selection.
     * @type {boolean}
     * @private
     */
    _justDragged: false,

    /**
     * Handles pointer down for long-press detection.
     * @param {PointerEvent} e
     * @private
     */
    _handlePointerDown(e) {
        const id = this._getImageId(e.target);
        if (!id) return;

        this._longPressTriggered = false;
        this._longPressTimer = setTimeout(() => {
            this._longPressTriggered = true;
            App.addToSelection(id);
        }, this.LONG_PRESS_MS);
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

    /**
     * Handles drag start for drag-box selection.
     * @param {MouseEvent} e
     * @private
     */
    _handleDragStart(e) {
        // Only handle left or right mouse button on grid background
        if (e.button !== 0 && e.button !== 2) return;
        if (this._getImageId(e.target)) return; // Clicked on an item

        e.preventDefault();
        const rect = this._els.grid.getBoundingClientRect();

        this.state.dragState = {
            startX: e.clientX - rect.left + this._els.grid.scrollLeft,
            startY: e.clientY - rect.top + this._els.grid.scrollTop,
            isRightButton: e.button === 2,
            dragged: false,
            box: null,
            autoScrollInterval: null,
            lastMouseEvent: null
        };

        // Create selection box element
        const box = App.createElement('div', { className: 'selection-box' });
        this._els.grid.appendChild(box);
        this.state.dragState.box = box;

        // Bind move and up handlers
        this._onDragMove = (e) => this._handleDragMove(e);
        this._onDragEnd = (e) => this._handleDragEnd(e);
        document.addEventListener('mousemove', this._onDragMove);
        document.addEventListener('mouseup', this._onDragEnd);
    },

    /**
     * Handles drag move - updates selection box and auto-scrolls near edges.
     * @param {MouseEvent} e
     * @private
     */
    _handleDragMove(e) {
        if (!this.state.dragState) return;

        // Store last mouse event for auto-scroll updates
        this.state.dragState.lastMouseEvent = e;

        this._updateDragBox(e);
        this._updateAutoScroll(e);
    },

    /**
     * Updates the drag selection box position.
     * @param {MouseEvent} e
     * @private
     */
    _updateDragBox(e) {
        const rect = this._els.grid.getBoundingClientRect();
        const currentX = e.clientX - rect.left + this._els.grid.scrollLeft;
        const currentY = e.clientY - rect.top + this._els.grid.scrollTop;

        const x = Math.min(this.state.dragState.startX, currentX);
        const y = Math.min(this.state.dragState.startY, currentY);
        const w = Math.abs(currentX - this.state.dragState.startX);
        const h = Math.abs(currentY - this.state.dragState.startY);

        // Mark as dragged if moved enough
        if (w > 5 || h > 5) {
            this.state.dragState.dragged = true;
        }

        // Update box position
        const box = this.state.dragState.box;
        box.style.left = x + 'px';
        box.style.top = y + 'px';
        box.style.width = w + 'px';
        box.style.height = h + 'px';
    },

    /**
     * Edge zone size in pixels for auto-scroll trigger.
     * @type {number}
     * @private
     */
    _autoScrollEdge: 50,

    /**
     * Auto-scroll speed in pixels per interval.
     * @type {number}
     * @private
     */
    _autoScrollSpeed: 15,

    /**
     * Updates auto-scroll based on mouse position near container edges.
     * @param {MouseEvent} e
     * @private
     */
    _updateAutoScroll(e) {
        const grid = this._els.grid;
        if (!grid) return;

        const rect = grid.getBoundingClientRect();
        const mouseY = e.clientY;

        // Calculate distance from edges
        const distFromTop = mouseY - rect.top;
        const distFromBottom = rect.bottom - mouseY;

        let scrollDirection = 0;
        if (distFromTop < this._autoScrollEdge && distFromTop >= 0) {
            // Near top edge - scroll up
            scrollDirection = -1;
        } else if (distFromBottom < this._autoScrollEdge && distFromBottom >= 0) {
            // Near bottom edge - scroll down
            scrollDirection = 1;
        }

        if (scrollDirection !== 0) {
            // Start auto-scroll if not already running
            if (!this.state.dragState.autoScrollInterval) {
                this.state.dragState.autoScrollInterval = setInterval(() => {
                    this._performAutoScroll();
                }, 16); // ~60fps
            }
            this.state.dragState.scrollDirection = scrollDirection;
        } else {
            // Stop auto-scroll
            this._stopAutoScroll();
        }
    },

    /**
     * Performs one step of auto-scrolling during drag.
     * @private
     */
    _performAutoScroll() {
        if (!this.state.dragState) return;

        const grid = this._els.grid;
        if (!grid) return;

        const direction = this.state.dragState.scrollDirection || 0;
        if (direction === 0) return;

        // Scroll the grid
        grid.scrollTop += direction * this._autoScrollSpeed;

        // Update the drag box to account for new scroll position
        if (this.state.dragState.lastMouseEvent) {
            this._updateDragBox(this.state.dragState.lastMouseEvent);
        }
    },

    /**
     * Stops auto-scrolling.
     * @private
     */
    _stopAutoScroll() {
        if (this.state.dragState?.autoScrollInterval) {
            clearInterval(this.state.dragState.autoScrollInterval);
            this.state.dragState.autoScrollInterval = null;
        }
    },

    /**
     * Handles drag end - selects items in box.
     * @param {MouseEvent} e
     * @private
     */
    _handleDragEnd(e) {
        document.removeEventListener('mousemove', this._onDragMove);
        document.removeEventListener('mouseup', this._onDragEnd);

        // Stop any auto-scrolling
        this._stopAutoScroll();

        if (!this.state.dragState) return;

        const { box, isRightButton, dragged } = this.state.dragState;

        if (dragged) {
            const boxRect = box.getBoundingClientRect();
            const items = this._els.grid.querySelectorAll('.gallery-item');
            const idsInBox = [];

            for (const item of items) {
                const itemRect = item.getBoundingClientRect();
                if (this._rectsIntersect(boxRect, itemRect)) {
                    idsInBox.push(item.dataset.id);
                }
            }

            if (isRightButton) {
                // Toggle selection for items in box
                for (const id of idsInBox) {
                    App.toggleSelection(id);
                }
            } else {
                // Set selection to items in box
                App.setSelectedImages(idsInBox);
            }

            // Flag to prevent click handler from clearing selection
            this._justDragged = true;
        }

        // Cleanup
        box.remove();
        this.state.dragState = null;
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

    /* ----------------------------------------------------------------------
       INFO PANEL

       Displays image metadata and editable fields.
       ---------------------------------------------------------------------- */

    /**
     * Currently displayed image ID in info panel.
     * @type {string|null}
     * @private
     */
    _infoPanelImageId: null,

    /**
     * Updates the info panel based on selection.
     * Shows info for single selection, or just count for multiple.
     * @param {Array<string>} selection - Selected image IDs
     * @private
     */
    _updateInfoPanel(selection) {
        const content = this._els.infoContent;

        if (selection.length === 0) {
            this._infoPanelImageId = null;
            content.innerHTML = '<p class="info-placeholder">Select an image to view details</p>';
            return;
        }

        // Multiple selection: show only the count, no individual image details
        if (selection.length > 1) {
            this._infoPanelImageId = null;
            content.innerHTML = `
                <div class="info-section">
                    <p class="info-selection-count">${selection.length} images selected</p>
                </div>
            `;
            return;
        }

        // Single selection: show full details
        const imageId = selection[0];

        // Don't re-render if same image
        if (imageId === this._infoPanelImageId) {
            return;
        }

        this._infoPanelImageId = imageId;
        this._renderInfoPanel(imageId, 1);
    },

    /**
     * Renders the info panel for a single selected image.
     * Fetches full details from the API since the gallery list only has minimal fields.
     * @param {string} imageId - Image ID to display
     * @private
     */
    async _renderInfoPanel(imageId) {
        const content = this._els.infoContent;

        // Show loading state
        content.innerHTML = '<p class="info-placeholder">Loading...</p>';

        // Fetch full image details from API
        let img;
        try {
            img = await App.apiGet(`/images/${imageId}`);
        } catch (error) {
            console.error('Failed to load image details:', error);
            content.innerHTML = '<p class="info-placeholder">Failed to load details</p>';
            return;
        }

        if (!img) {
            content.innerHTML = '<p class="info-placeholder">Image not found</p>';
            return;
        }

        content.innerHTML = `
            <div class="info-section">
                <p class="info-filename">${App.escapeHtml(img.basename)}</p>
                <p class="info-path">${App.escapeHtml(img.path)}</p>
            </div>

            <div class="info-section">
                <div class="info-row">
                    <span class="info-label">Dimensions</span>
                    <span class="info-value">${App.formatDimensions(img.width, img.height)}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">File size</span>
                    <span class="info-value">${App.formatFileSize(img.size)}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Date</span>
                    <input type="datetime-local" id="info-timestamp" class="info-input info-timestamp"
                           value="${img.timestamp ? img.timestamp.slice(0, 16) : ''}">
                </div>
            </div>

            <div class="info-section info-editable">
                <label class="info-label" for="info-description">Description</label>
                <textarea id="info-description" class="info-input" rows="3">${App.escapeHtml(img.description || '')}</textarea>
            </div>

            <div class="info-section info-editable">
                <label class="info-label" for="info-rating">Rating</label>
                <div class="info-rating-row">
                    <input type="text" id="info-rating" class="info-input" value="${App.escapeHtml(img.rating || '')}">
                    <button type="button" id="info-emoji-btn" class="toolbar-btn" title="Add emoji">
                        <span class="material-symbols-outlined">add_reaction</span>
                    </button>
                </div>
            </div>
        `;

        // Bind editable field events
        this._bindInfoPanelEvents(imageId);
    },

    /**
     * Binds event listeners for info panel editable fields.
     * @param {string} imageId - Image ID being edited
     * @private
     */
    _bindInfoPanelEvents(imageId) {
        const descField = App.$('info-description');
        const ratingField = App.$('info-rating');
        const timestampField = App.$('info-timestamp');
        const emojiBtn = App.$('info-emoji-btn');

        // Save on blur
        if (descField) {
            descField.addEventListener('blur', () => {
                this._saveImageField(imageId, 'description', descField.value);
            });
        }

        if (ratingField) {
            ratingField.addEventListener('blur', () => {
                this._saveImageField(imageId, 'rating', ratingField.value);
            });
        }

        if (timestampField) {
            timestampField.addEventListener('change', () => {
                // Convert datetime-local value to ISO format
                const value = timestampField.value;
                if (value) {
                    const isoTimestamp = new Date(value).toISOString();
                    this._saveImageField(imageId, 'timestamp', isoTimestamp);
                }
            });
        }

        // Emoji picker
        if (emojiBtn && ratingField) {
            emojiBtn.addEventListener('click', () => {
                App.showEmojiPicker((emoji) => {
                    ratingField.value += emoji;
                    ratingField.focus();
                });
            });
        }
    },

    /**
     * Saves a field value for an image to the backend.
     * @param {string} imageId - Image ID
     * @param {string} field - Field name ('description' or 'rating')
     * @param {string} value - New value
     * @private
     */
    async _saveImageField(imageId, field, value) {
        // Update local state
        const img = this.state.images.find(i => i.id === imageId);
        if (img) {
            img[field] = value;
        }

        // Save to backend
        try {
            await App.apiPost(`/images/${imageId}`, { [field]: value });
        } catch (error) {
            console.error(`Failed to save ${field}:`, error);
        }
    },

    /* ----------------------------------------------------------------------
       KEYBOARD & NAVIGATION

       Arrow key navigation, delete key, and related keyboard handling.
       ---------------------------------------------------------------------- */

    /**
     * Bound keyboard handler reference for cleanup.
     * @type {Function|null}
     * @private
     */
    _keyHandler: null,

    /**
     * Binds keyboard event listeners.
     * Called when entering the gallery screen.
     * @private
     */
    _bindKeyboard() {
        this._keyHandler = (e) => this._handleKeyDown(e);
        document.addEventListener('keydown', this._keyHandler);
    },

    /**
     * Unbinds keyboard event listeners.
     * Called when leaving the gallery screen.
     * @private
     */
    _unbindKeyboard() {
        if (this._keyHandler) {
            document.removeEventListener('keydown', this._keyHandler);
            this._keyHandler = null;
        }
    },

    /**
     * Handles keydown events.
     * @param {KeyboardEvent} e
     * @private
     */
    _handleKeyDown(e) {
        // If a modal dialog is open, let it handle Escape and focus.
        if (document.querySelector('dialog[open]')) {
            return;
        }

        // Ignore if typing in an input field
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
            return;
        }

        switch (e.key) {
            case 'ArrowLeft':
                e.preventDefault();
                this._navigateSelection(-1);
                break;
            case 'ArrowRight':
                e.preventDefault();
                this._navigateSelection(1);
                break;
            case 'ArrowUp':
                e.preventDefault();
                this._navigateSelectionVertical(-1);
                break;
            case 'ArrowDown':
                e.preventDefault();
                this._navigateSelectionVertical(1);
                break;
            case 'Enter':
                e.preventDefault();
                this._openSelectedFullscreen();
                break;
            case 'Delete':
            case 'Backspace':
                e.preventDefault();
                this._deleteSelected();
                break;
            case 'Escape':
                e.preventDefault();
                App.clearSelection();
                break;
            case 'a':
                if (e.ctrlKey || e.metaKey) {
                    e.preventDefault();
                    this._selectAll();
                }
                break;
        }
    },

    /**
     * Navigates selection horizontally.
     * @param {number} delta - Direction (-1 for left, 1 for right)
     * @private
     */
    _navigateSelection(delta) {
        const filtered = this.state.filteredImages;
        if (filtered.length === 0) return;

        const selected = App.getSelectedImages();
        let currentIndex = -1;

        if (selected.length > 0) {
            const lastSelected = selected[selected.length - 1];
            currentIndex = filtered.findIndex(img => img.id === lastSelected);
        }

        let newIndex = currentIndex + delta;
        if (newIndex < 0) newIndex = filtered.length - 1;
        if (newIndex >= filtered.length) newIndex = 0;

        App.setSelectedImages([filtered[newIndex].id]);
        this._scrollToItem(filtered[newIndex].id, newIndex);
    },

    /**
     * Navigates selection vertically based on grid layout.
     * @param {number} delta - Direction (-1 for up, 1 for down)
     * @private
     */
    _navigateSelectionVertical(delta) {
        const filtered = this.state.filteredImages;
        if (filtered.length === 0) return;

        // Use virtual scroll's calculated items per row
        const itemsPerRow = this._virtualScroll.itemsPerRow || 1;

        const selected = App.getSelectedImages();
        let currentIndex = -1;

        if (selected.length > 0) {
            const lastSelected = selected[selected.length - 1];
            currentIndex = filtered.findIndex(img => img.id === lastSelected);
        }

        let newIndex = currentIndex + (delta * itemsPerRow);
        if (newIndex < 0) newIndex = 0;
        if (newIndex >= filtered.length) newIndex = filtered.length - 1;

        App.setSelectedImages([filtered[newIndex].id]);
        this._scrollToItem(filtered[newIndex].id, newIndex);
    },

    /**
     * Scrolls the grid to ensure an item is visible.
     * With virtual scrolling, calculates position from index.
     * @param {string} id - Image ID to scroll to
     * @param {number} [index] - Optional index in filteredImages for faster lookup
     * @private
     */
    _scrollToItem(id, index) {
        const grid = this._els.grid;
        if (!grid) return;

        // Find index if not provided
        if (index === undefined) {
            index = this.state.filteredImages.findIndex(img => img.id === id);
        }
        if (index === -1) return;

        // Calculate row and scroll position
        const vs = this._virtualScroll;
        const row = Math.floor(index / vs.itemsPerRow);
        const targetY = row * vs.itemHeight;

        // Check if item is already visible
        const viewTop = grid.scrollTop;
        const viewBottom = viewTop + grid.clientHeight;
        const itemBottom = targetY + vs.itemHeight;

        if (targetY < viewTop) {
            // Item is above viewport - scroll up
            grid.scrollTo({ top: targetY, behavior: 'smooth' });
        } else if (itemBottom > viewBottom) {
            // Item is below viewport - scroll down
            grid.scrollTo({ top: itemBottom - grid.clientHeight, behavior: 'smooth' });
        }
    },

    /**
     * Opens fullscreen view for the selected image.
     * Only works if exactly one image is selected.
     * @private
     */
    _openSelectedFullscreen() {
        const selected = App.getSelectedImages();
        if (selected.length === 1) {
            App.showFullscreen(selected[0]);
        }
    },

    /**
     * Deletes selected images after confirmation.
     * @private
     */
    async _deleteSelected() {
        const selected = App.getSelectedImages();
        if (selected.length === 0) return;

        const count = selected.length;
        const message = count === 1
            ? 'Are you sure you want to delete this image?'
            : `Are you sure you want to delete ${count} images?`;

        const confirmed = await App.confirm('Delete Images', message);
        if (!confirmed) return;

        // Delete each image
        for (const id of selected) {
            try {
                await App.apiDelete(`/images/${id}`);
                // Remove from local state
                this.state.images = this.state.images.filter(img => img.id !== id);
            } catch (error) {
                console.error(`Failed to delete image ${id}:`, error);
            }
        }

        // Clear selection and re-render
        App.clearSelection();
        this._renderGrid();
    }
};

// Register module with App
App.registerModule('gallery', Gallery);
