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
     * @property {boolean} needsRefresh - Whether grid needs to reload on next enter
     * @property {IntersectionObserver|null} lazyLoader - Observer for lazy loading
     * @property {Object|null} dragState - Current drag selection state
     * @property {Object|null} contentSimilarities - Similarity scores by image ID for content sort
     * @property {string|null} contentReferenceId - Reference image ID for content sort
     */
    state: {
        images: [],
        needsRefresh: true,
        lazyLoader: null,
        dragState: null,
        contentSimilarities: null,
        contentReferenceId: null,
        refreshIntervalId: null,
        lastImageCount: 0
    },

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

        // Set up lazy loading observer
        this._initLazyLoader();

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
    },

    /**
     * Called when entering the gallery screen.
     * @param {*} data - Optional data passed from navigation
     */
    onEnter(data) {
        if (this.state.needsRefresh) {
            this._loadImages();
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
        try {
            const images = await App.apiGet('/images');
            this.state.images = this._sortImages(images);
            this.state.lastImageCount = images.length;
            this._renderGrid();
            this.state.needsRefresh = false;
        } catch (error) {
            console.error('Failed to load images:', error);
            this.state.images = [];
            this._renderGrid();
        }
    },

    /**
     * Starts background refresh polling while database is updating.
     * @private
     */
    _startBackgroundRefresh() {
        // Don't start if already running
        if (this.state.refreshIntervalId) return;

        // Poll every 5 seconds
        this.state.refreshIntervalId = setInterval(() => {
            this._checkForNewImages();
        }, 5000);
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
     * @private
     */
    async _checkForNewImages() {
        try {
            // Check database status
            const status = await App.apiGet('/status');
            if (status.status !== 'updating') {
                return; // Only refresh while updating
            }

            // Fetch latest images
            const images = await App.apiGet('/images');

            // Only refresh if there are new images
            if (images.length === this.state.lastImageCount) {
                return;
            }

            // Preserve current state
            const scrollTop = this._els.grid?.closest('.gallery-container')?.scrollTop || 0;
            const currentSelection = App.getSelection();

            // Update images
            this.state.images = this._sortImages(images);
            this.state.lastImageCount = images.length;

            // Re-render grid
            this._renderGrid();

            // Restore scroll position
            const container = this._els.grid?.closest('.gallery-container');
            if (container) {
                container.scrollTop = scrollTop;
            }

            // Restore selection (filter to still-existing image IDs)
            const existingIds = new Set(this.state.images.map(img => img.id));
            const validSelection = currentSelection.filter(id => existingIds.has(id));
            if (validSelection.length > 0) {
                App.setSelection(validSelection);
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
        this._reloadThumbnails();
    },

    /**
     * Handles sort changes.
     * @private
     */
    _onSortChanged() {
        const { by } = App.getSort();

        if (by === 'content') {
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

        this._loadImages(); // Reload and apply new filter
    },

    /**
     * Handles selection changes.
     * @param {Array<string>} selection - Selected image IDs
     * @private
     */
    _onSelectionChanged(selection) {
        // Update visual selection state on thumbnails
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
        const visible = this._filterImages(this.state.images);
        const allIds = visible.map(img => img.id);
        App.setSelectedImages(allIds);
    },

    /* ----------------------------------------------------------------------
       THUMBNAIL GRID

       Rendering, lazy loading, and grid management.
       ---------------------------------------------------------------------- */

    /**
     * Renders the thumbnail grid.
     * @private
     */
    _renderGrid() {
        const grid = this._els.grid;
        grid.innerHTML = '';

        // Apply filter
        const filtered = this._filterImages(this.state.images);

        // Handle empty state
        if (filtered.length === 0) {
            grid.innerHTML = '<div class="empty-state"><span class="material-symbols-outlined">photo_library</span><p>No images to display</p></div>';
            return;
        }

        // Update grid CSS for thumbnail size
        this._updateGridStyle();

        // Create thumbnail items
        const fragment = document.createDocumentFragment();
        for (const img of filtered) {
            fragment.appendChild(this._createThumbnailItem(img));
        }
        grid.appendChild(fragment);

        // Observe all images for lazy loading
        const images = grid.querySelectorAll('.gallery-item img');
        for (const img of images) {
            this.state.lazyLoader.observe(img);
        }
    },

    /**
     * Creates a thumbnail item element.
     * @param {Object} img - Image data object
     * @returns {HTMLElement} The thumbnail item element
     * @private
     */
    _createThumbnailItem(img) {
        const item = App.createElement('div', {
            className: 'gallery-item',
            dataId: img.id
        });

        // Thumbnail image (src set by lazy loader)
        const thumb = App.createElement('img', {
            alt: img.basename,
            dataSrc: App.thumbnailUrl(img.id)
        });

        // Basename label
        const label = App.createElement('span', { className: 'gallery-item-label' }, img.basename);

        item.appendChild(thumb);
        item.appendChild(label);

        // Selection state
        if (App.getSelectedImages().includes(img.id)) {
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
     * Reloads all thumbnail images with new size.
     * @private
     */
    _reloadThumbnails() {
        const images = this._els.grid.querySelectorAll('.gallery-item img');
        for (const img of images) {
            const id = img.closest('.gallery-item').dataset.id;
            if (img.src && !img.src.startsWith('data:')) {
                img.src = App.thumbnailUrl(id);
            } else {
                img.dataset.src = App.thumbnailUrl(id);
            }
        }
    },

    /**
     * Initialises the IntersectionObserver for lazy loading.
     * Uses LIFO ordering so the most recently viewed images load first.
     * @private
     */
    _initLazyLoader() {
        // Pending images to load (LIFO - process from end)
        this._pendingLoads = [];
        this._loadScheduled = false;

        // Track currently visible images
        this._visibleImages = new Set();

        this.state.lazyLoader = new IntersectionObserver((entries) => {
            for (const entry of entries) {
                const img = entry.target;
                if (entry.isIntersecting) {
                    // Image entered viewport - add to pending queue
                    if (img.dataset.src) {
                        this._visibleImages.add(img);
                        // Remove if already in queue, then add to end (LIFO priority)
                        const idx = this._pendingLoads.indexOf(img);
                        if (idx !== -1) this._pendingLoads.splice(idx, 1);
                        this._pendingLoads.push(img);
                    }
                } else {
                    // Image left viewport - remove from visible set
                    this._visibleImages.delete(img);
                }
            }
            this._scheduleLoad();
        }, {
            root: this._els.grid?.closest('.gallery-container') || null,
            rootMargin: '100px'
        });
    },

    /**
     * Schedules processing of the pending load queue.
     * @private
     */
    _scheduleLoad() {
        if (this._loadScheduled) return;
        this._loadScheduled = true;

        // Small delay to let scrolling settle, then load in LIFO order
        requestAnimationFrame(() => {
            this._loadScheduled = false;
            this._processLoadQueue();
        });
    },

    /**
     * Processes the pending load queue in LIFO order.
     * Prioritizes images that are still visible.
     * @private
     */
    _processLoadQueue() {
        // Load up to N images per frame to avoid jank
        const BATCH_SIZE = 4;
        let loaded = 0;

        // Process from end of queue (LIFO) - most recently viewed first
        while (this._pendingLoads.length > 0 && loaded < BATCH_SIZE) {
            const img = this._pendingLoads.pop();

            // Skip if already loaded or no longer has data-src
            if (!img.dataset.src) continue;

            // Prioritize if still visible, otherwise put back at front (low priority)
            if (!this._visibleImages.has(img)) {
                this._pendingLoads.unshift(img);
                continue;
            }

            // Load this image
            img.src = img.dataset.src;
            delete img.dataset.src;
            this.state.lazyLoader.unobserve(img);
            this._visibleImages.delete(img);
            loaded++;
        }

        // If more pending, schedule another batch
        if (this._pendingLoads.length > 0) {
            this._scheduleLoad();
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

        // Update display value as slider moves
        this._els.similaritySlider.addEventListener('input', () => {
            const value = this._els.similaritySlider.value;
            this._els.similarityValue.textContent = value + '%';
        });

        // Re-run search when slider value changes (debounced)
        this._els.similaritySlider.addEventListener('change', async () => {
            const filter = App.getFilter();
            if (!filter || filter.type !== 'semantic' || !filter.text) return;

            // Clear any pending debounce
            if (debounceTimer) clearTimeout(debounceTimer);

            debounceTimer = setTimeout(async () => {
                const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

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
    _handleClick(e) {
        // Ignore if this was a long-press or drag
        if (this._longPressTriggered || this.state.dragState?.dragged || this._justDragged) {
            this._longPressTriggered = false;
            this._justDragged = false;
            return;
        }

        const id = this._getImageId(e.target);
        if (id) {
            App.setSelectedImages([id]);
        } else {
            // Clicked on empty space - clear selection
            App.clearSelection();
        }
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
            box: null
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
     * Handles drag move - updates selection box.
     * @param {MouseEvent} e
     * @private
     */
    _handleDragMove(e) {
        if (!this.state.dragState) return;

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
     * Handles drag end - selects items in box.
     * @param {MouseEvent} e
     * @private
     */
    _handleDragEnd(e) {
        document.removeEventListener('mousemove', this._onDragMove);
        document.removeEventListener('mouseup', this._onDragEnd);

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
     * @param {string} imageId - Image ID to display
     * @private
     */
    async _renderInfoPanel(imageId) {
        const content = this._els.infoContent;
        const img = this.state.images.find(i => i.id === imageId);

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
        const filtered = this._filterImages(this.state.images);
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
        this._scrollToItem(filtered[newIndex].id);
    },

    /**
     * Navigates selection vertically based on grid layout.
     * @param {number} delta - Direction (-1 for up, 1 for down)
     * @private
     */
    _navigateSelectionVertical(delta) {
        const filtered = this._filterImages(this.state.images);
        if (filtered.length === 0) return;

        // Calculate items per row based on grid
        const gridWidth = this._els.grid.clientWidth;
        const thumbSize = App.getThumbnailSize();
        const gap = 16; // Approximate gap
        const itemsPerRow = Math.floor(gridWidth / (thumbSize + gap)) || 1;

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
        this._scrollToItem(filtered[newIndex].id);
    },

    /**
     * Scrolls the grid to ensure an item is visible.
     * @param {string} id - Image ID to scroll to
     * @private
     */
    _scrollToItem(id) {
        const item = this._getItemElement(id);
        if (item) {
            item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
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
