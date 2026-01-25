/**
 * @fileoverview Gallery screen module for the Imaginary application.
 *
 * This module handles all functionality specific to the Gallery screen,
 * which is the primary view for browsing the image catalogue. It registers
 * with the core App module and responds to screen lifecycle events.
 *
 * Uses shared infrastructure from thumbnails.js:
 * - VirtualGrid: Virtual scrolling for large image collections
 * - GridSelection: Unified selection handling (click, keyboard, drag-box)
 * - ThumbnailLoader: Priority-based thumbnail loading
 *
 * RESPONSIBILITIES:
 *
 * Thumbnail Grid:
 *   - Renders image thumbnails via VirtualGrid
 *   - Supports dynamic thumbnail size adjustment
 *   - Displays image basename beneath each thumbnail
 *   - Handles empty state when no images match the current filter
 *
 * Image Selection (via GridSelection):
 *   - Single/Ctrl/Shift click handling
 *   - Drag-box selection with auto-scroll
 *   - Keyboard navigation (arrow keys)
 *   - Select all / clear selection
 *
 * Info Panel:
 *   - Displays metadata for the currently selected image
 *   - Provides editable fields for Description and Rating
 *   - Saves edits to backend via API
 *
 * Sorting & Filtering:
 *   - Sort by date, rating, or content similarity
 *   - Applies filters set by the Search screen
 *
 * Navigation:
 *   - Double-click/Enter to open Full-screen view
 *   - Maintains scroll position when returning from other screens
 *
 * Deletion:
 *   - Delete key triggers deletion of selected images
 *   - Shows confirmation dialog before deletion
 *
 * @module gallery
 * @requires core
 * @requires thumbnails
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE
   ========================================================================== */

/**
 * Gallery screen module.
 * @namespace
 */
const Gallery = {
    /**
     * Local state for the gallery screen.
     * @type {Object}
     */
    state: {
        images: [],
        filteredImages: [],
        needsRefresh: true,
        contentSimilarities: null,
        contentReferenceId: null,
        refreshIntervalId: null,
        lastImageCount: 0
    },

    /**
     * VirtualGrid instance.
     * @type {Object|null}
     * @private
     */
    _grid: null,

    /**
     * GridSelection instance.
     * @type {Object|null}
     * @private
     */
    _selection: null,

    /**
     * Scroll indicator overlay element.
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
     * Scroll state when overlay was first shown.
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

        // Create scroll indicator overlay
        this._createScrollOverlay();

        // Track mouse position for overlay positioning
        this._mouseTracker = (e) => {
            this._mousePos.x = e.clientX;
            this._mousePos.y = e.clientY;
        };
        document.addEventListener('mousemove', this._mouseTracker, { passive: true });

        // Create VirtualGrid instance
        this._grid = VirtualGrid.create({
            container: this._els.grid,
            grid: this._els.grid,
            getItems: () => this.state.filteredImages,
            getItemId: (img) => img.id,
            createItem: (img, index) => this._createThumbnailItem(img),
            onItemVisible: (img, el) => {
                const imgEl = el.querySelector('img');
                if (imgEl) {
                    ThumbnailLoader.request(img.id, imgEl, 'visible');
                }
            },
            onItemRemoved: (id) => {
                ThumbnailLoader.cancel(id);
            },
            getThumbnailId: (img) => img.id,
            itemSelector: '.gallery-item',
            bufferRows: 3,
            retainRows: this._loadRetainRows()
        });

        // Create GridSelection instance
        this._selection = GridSelection.create({
            grid: this._grid,
            getItems: () => this.state.filteredImages,
            getItemId: (img) => img.id,
            itemSelector: '.gallery-item',
            onSelectionChanged: (ids) => {
                App.setSelectedImages(ids);
            },
            onItemActivated: (id) => {
                App.showFullscreen(id);
            },
            onDeleteRequested: (ids) => {
                this._deleteImages(ids);
            }
        });

        // Set up similarity slider handler
        this._initSimilaritySlider();

        // Subscribe to app events
        App.on('thumbnailSizeChanged', () => this._onThumbnailSizeChanged());
        App.on('sortChanged', () => this._onSortChanged());
        App.on('filterChanged', () => this._onFilterChanged());
        App.on('selectionChanged', (sel) => this._onSelectionChanged(sel));
        App.on('selectAll', () => this._selection.selectAll());
        App.on('imageRotated', (imageId) => this._onImageRotated(imageId));
    },

    /**
     * Loads retain rows setting from localStorage.
     * @returns {number} Retain rows value
     * @private
     */
    _loadRetainRows() {
        const saved = localStorage.getItem('imaginary-retainRows');
        if (saved) {
            const retain = parseInt(saved, 10);
            if (!isNaN(retain) && retain >= 0) {
                return retain;
            }
        }
        return 30; // Default
    },

    /**
     * Called when entering the gallery screen.
     */
    onEnter() {
        if (this.state.needsRefresh) {
            this._loadImages();
        } else {
            // Re-bind grid and selection
            this._grid.bind();
        }
        // Bind selection handlers
        this._selection.bind();
        // Start background refresh while database is updating
        this._startBackgroundRefresh();
    },

    /**
     * Called when leaving the gallery screen.
     */
    onLeave() {
        // Unbind selection handlers
        this._selection.unbind();
        // Unbind grid scroll handler
        this._grid.unbind();
        // Stop background refresh
        this._stopBackgroundRefresh();
        // Hide scroll indicator overlay
        if (this._scrollOverlayTimer) {
            clearTimeout(this._scrollOverlayTimer);
            this._scrollOverlayTimer = null;
        }
        if (this._scrollOverlay) {
            this._scrollOverlay.hidden = true;
        }
    },

    /**
     * Marks the gallery as needing a refresh on next enter.
     */
    markNeedsRefresh() {
        this.state.needsRefresh = true;
    },

    /* ----------------------------------------------------------------------
       DATA LOADING
       ---------------------------------------------------------------------- */

    /**
     * Loads images from the API and renders the grid.
     * @private
     */
    async _loadImages() {
        console.time('_loadImages total');

        // Show loading overlay on first load
        const isFirstLoad = App.getCachedImageCount() === 0;
        if (isFirstLoad) {
            App.showLoading('Loading images…');
        }

        try {
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
            App.showError('Failed to load images');
        } finally {
            if (isFirstLoad) {
                App.hideLoading();
            }
        }

        console.timeEnd('_loadImages total');
    },

    /**
     * Starts background refresh polling.
     * @private
     */
    _startBackgroundRefresh() {
        if (this.state.refreshIntervalId) return;

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
     * Checks if new images are available and refreshes.
     * @private
     */
    async _checkForNewImages() {
        try {
            const status = await App.apiGet('/status');
            if (status.status !== 'updating') return;

            const images = await App.getImages();
            if (images.length === this.state.lastImageCount) return;

            // Preserve state
            const scrollTop = this._els.grid?.scrollTop || 0;
            const currentSelection = App.getSelectedImages();

            // Update images
            this.state.images = this._sortImages(images);
            this.state.lastImageCount = images.length;

            // Re-render
            this._renderGrid();

            // Restore scroll position
            if (this._els.grid) {
                this._els.grid.scrollTop = scrollTop;
            }

            // Restore selection
            const existingIds = new Set(this.state.images.map(img => img.id));
            const validSelection = currentSelection.filter(id => existingIds.has(id));
            if (validSelection.length > 0) {
                App.setSelectedImages(validSelection);
            }
        } catch (error) {
            console.debug('Background refresh error:', error);
        }
    },

    /* ----------------------------------------------------------------------
       SORTING & FILTERING
       ---------------------------------------------------------------------- */

    /**
     * Sorts images based on current sort settings.
     * @param {Array<Object>} images
     * @returns {Array<Object>}
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
                const simA = this.state.contentSimilarities?.[a.id] ?? 0;
                const simB = this.state.contentSimilarities?.[b.id] ?? 0;
                cmp = simA - simB;
            }
            return direction === 'asc' ? cmp : -cmp;
        });

        return sorted;
    },

    /**
     * Loads content similarity data for sorting.
     * @private
     */
    async _loadContentSimilarities() {
        const selected = App.getSelectedImages();
        if (selected.length === 0) {
            App.showError('Select an image first to sort by visual similarity.');
            App.setSortBy('date');
            return;
        }

        const referenceId = selected[0];

        try {
            const response = await App.apiGet(`/similar/${referenceId}`);
            if (response && response.results) {
                this.state.contentSimilarities = {};
                this.state.contentReferenceId = referenceId;
                response.results.forEach(img => {
                    this.state.contentSimilarities[img.id] = img.similarity;
                });

                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();
                this._scrollToTop();
            }
        } catch (error) {
            console.error('Failed to load content similarities:', error);
            if (error.message && error.message.includes('404')) {
                App.showError('This image has no embedding yet. Wait for processing to complete.');
            } else {
                App.showError('Could not load similarity data.');
            }
            App.setSortBy('date');
        }
    },

    /**
     * Filters images based on current filter settings.
     * @param {Array<Object>} images
     * @returns {Array<Object>}
     * @private
     */
    _filterImages(images) {
        const filter = App.getFilter();
        if (!filter) return images;

        // Duplicates filter
        if (filter.type === 'duplicates' && Array.isArray(filter.imageIds)) {
            const idSet = new Set(filter.imageIds.map(String));
            return images.filter(img => idSet.has(String(img.id)));
        }

        // Semantic search filter
        if (filter.type === 'semantic' && Array.isArray(filter.imageIds)) {
            const idSet = new Set(filter.imageIds.map(String));
            const scores = filter.scores || {};

            let filtered = images.filter(img => idSet.has(String(img.id)));

            // Apply additional filters
            filtered = filtered.filter(img => {
                if (filter.dateStart) {
                    const imgDate = new Date(img.timestamp);
                    if (imgDate < new Date(filter.dateStart)) return false;
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

            // Sort by score
            filtered.sort((a, b) => (scores[b.id] || 0) - (scores[a.id] || 0));
            return filtered;
        }

        // Standard filters
        return images.filter(img => {
            if (filter.text && !(img.description || '').toLowerCase().includes(filter.text.toLowerCase())) {
                return false;
            }
            if (filter.dateStart && new Date(img.timestamp) < new Date(filter.dateStart)) {
                return false;
            }
            if (filter.dateEnd) {
                const endDate = new Date(filter.dateEnd);
                endDate.setHours(23, 59, 59, 999);
                if (new Date(img.timestamp) > endDate) return false;
            }
            if (filter.rating) {
                const filterEmoji = [...filter.rating];
                const hasMatch = filterEmoji.some(e => img.rating && img.rating.includes(e));
                if (!hasMatch) return false;
            }
            return true;
        });
    },

    /* ----------------------------------------------------------------------
       EVENT HANDLERS
       ---------------------------------------------------------------------- */

    /**
     * Handles thumbnail size changes.
     * @private
     */
    _onThumbnailSizeChanged() {
        this._updateGridStyle();
        // Clear ThumbnailLoader and refresh grid
        ThumbnailLoader.clear();
        if (this._grid && this.state.filteredImages.length > 0) {
            this._grid.refresh();
        }
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
            if (isSemanticFilter) {
                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();
                return;
            }

            const selected = App.getSelectedImages();
            const referenceId = selected.length > 0 ? selected[0] : null;

            if (referenceId && this.state.contentReferenceId === referenceId) {
                this.state.images = this._sortImages(this.state.images);
                this._renderGrid();
            } else {
                this._loadContentSimilarities();
            }
        } else {
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
        const filter = App.getFilter();
        const isSemanticFilter = filter && filter.type === 'semantic';

        if (this._els.similarityControl) {
            this._els.similarityControl.style.display = isSemanticFilter ? 'flex' : 'none';

            if (isSemanticFilter && filter.threshold) {
                const pct = Math.round(filter.threshold * 100);
                this._els.similaritySlider.value = pct;
                this._els.similarityValue.textContent = pct + '%';
            }
        }

        if (isSemanticFilter) {
            App.setSortBy('content');
            App.setSortDirection('desc');
        }

        if (App.getScreen() === 'gallery') {
            this._loadImages();
            this._scrollToTop();
        } else {
            this.state.needsRefresh = true;
        }
    },

    /**
     * Scrolls the gallery grid to the top.
     * @private
     */
    _scrollToTop() {
        if (this._els.grid) {
            this._els.grid.scrollTop = 0;
        }
    },

    /**
     * Handles selection changes from App.
     * @param {Array<string>} selection
     * @private
     */
    _onSelectionChanged(selection) {
        // Sync GridSelection if change came from external source
        if (this._selection) {
            this._selection.setSelected(selection);
        }
        // Update info panel
        this._updateInfoPanel(selection);
    },

    /**
     * Handles image rotation.
     * @param {string} imageId
     * @private
     */
    _onImageRotated(imageId) {
        // Use ThumbnailLoader cache bust
        ThumbnailLoader.bustCache(imageId);

        // Find and reload the thumbnail
        const item = this._els.grid.querySelector(`.gallery-item[data-id="${imageId}"]`);
        if (item) {
            const img = item.querySelector('img');
            if (img) {
                // Re-request with cache bust
                ThumbnailLoader.cancel(imageId, img);
                ThumbnailLoader.request(imageId, img, 'visible');
            }
        }
    },

    /* ----------------------------------------------------------------------
       GRID RENDERING
       ---------------------------------------------------------------------- */

    /**
     * Renders the thumbnail grid.
     * @private
     */
    _renderGrid() {
        console.time('_renderGrid total');
        const grid = this._els.grid;

        // Apply filter
        console.time('_renderGrid filter');
        this.state.filteredImages = this._filterImages(this.state.images);
        console.timeEnd('_renderGrid filter');
        console.log(`_renderGrid: ${this.state.filteredImages.length} images`);

        // Handle empty state
        if (this.state.filteredImages.length === 0) {
            grid.innerHTML = '<div class="empty-state"><span class="material-symbols-outlined">photo_library</span><p>No images to display</p></div>';
            console.timeEnd('_renderGrid total');
            return;
        }

        // Update grid CSS for thumbnail size
        this._updateGridStyle();

        // Clear and render via VirtualGrid
        ThumbnailLoader.clear();
        this._grid.render();

        // Add scroll indicator overlay
        if (this._scrollOverlay) {
            this._scrollOverlay.hidden = true;
            grid.appendChild(this._scrollOverlay);
        }

        // Bind selection
        this._selection.bind();

        // Set up scroll overlay updates
        this._bindScrollOverlay();

        console.timeEnd('_renderGrid total');
    },

    /**
     * Creates a thumbnail item element.
     * @param {Object} img
     * @returns {HTMLElement}
     * @private
     */
    _createThumbnailItem(img) {
        const item = App.createElement('div', {
            className: 'gallery-item',
            dataId: img.id
        });

        // Thumbnail image - src will be set by ThumbnailLoader
        const thumb = App.createElement('img', {
            alt: img.basename
        });

        // Basename label
        const label = App.createElement('span', { className: 'gallery-item-label' }, img.basename);

        item.appendChild(thumb);
        item.appendChild(label);

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

    /* ----------------------------------------------------------------------
       SCROLL OVERLAY
       ---------------------------------------------------------------------- */

    /**
     * Creates the scroll indicator overlay element.
     * @private
     */
    _createScrollOverlay() {
        this._scrollOverlay = document.createElement('div');
        this._scrollOverlay.className = 'scroll-overlay';
        this._scrollOverlay.hidden = true;
    },

    /**
     * Binds scroll overlay updates to grid scroll.
     * @private
     */
    _bindScrollOverlay() {
        const grid = this._els.grid;
        if (!grid) return;

        // Remove old listener if exists
        if (this._scrollOverlayHandler) {
            grid.removeEventListener('scroll', this._scrollOverlayHandler);
        }

        this._scrollOverlayHandler = () => {
            this._updateScrollOverlay(grid.scrollTop);
        };
        grid.addEventListener('scroll', this._scrollOverlayHandler, { passive: true });
    },

    /**
     * Updates the scroll indicator overlay.
     * @param {number} scrollTop
     * @private
     */
    _updateScrollOverlay(scrollTop) {
        const { by } = App.getSort();

        if (by !== 'date' && by !== 'rating') {
            if (this._scrollOverlay) {
                this._scrollOverlay.hidden = true;
            }
            return;
        }

        const filtered = this.state.filteredImages;
        if (filtered.length === 0) return;

        // Calculate first visible image
        const itemHeight = this._grid.getItemHeight();
        const itemsPerRow = this._grid.getItemsPerRow();
        if (!itemHeight || !itemsPerRow) return;

        const firstVisibleRow = Math.floor(scrollTop / itemHeight);
        const firstVisibleIndex = firstVisibleRow * itemsPerRow;

        if (firstVisibleIndex < 0 || firstVisibleIndex >= filtered.length) return;

        const img = filtered[firstVisibleIndex];
        if (!img) return;

        if (by === 'date' && img.timestamp) {
            const date = new Date(img.timestamp);
            const formatted = this._formatScrollDate(date);
            if (formatted) {
                this._showScrollOverlay(formatted, scrollTop);
            }
        } else if (by === 'rating') {
            const rating = img.rating || 'No rating';
            this._showScrollOverlay(rating, scrollTop);
        }
    },

    /**
     * Shows the scroll indicator overlay.
     * @param {string} text
     * @param {number} scrollTop
     * @private
     */
    _showScrollOverlay(text, scrollTop) {
        if (!this._scrollOverlay || !text) return;

        const wasHidden = this._scrollOverlay.hidden;
        this._scrollOverlay.textContent = text;
        this._scrollOverlay.hidden = false;

        if (wasHidden) {
            this._positionScrollOverlayAtMouse();
            this._scrollOverlayAnchor = {
                scrollTop: scrollTop,
                overlayY: parseFloat(this._scrollOverlay.style.top) || 0
            };
        } else {
            this._updateScrollOverlayFromScroll(scrollTop);
        }

        if (this._scrollOverlayTimer) {
            clearTimeout(this._scrollOverlayTimer);
        }

        this._scrollOverlayTimer = setTimeout(() => {
            this._scrollOverlay.hidden = true;
            this._scrollOverlayAnchor = null;
        }, 1000);
    },

    /**
     * Positions the scroll overlay at the mouse.
     * @private
     */
    _positionScrollOverlayAtMouse() {
        if (!this._scrollOverlay) return;

        const rect = this._scrollOverlay.getBoundingClientRect();
        const padding = 12;

        const left = this._mousePos.x - rect.width - padding;
        const top = this._mousePos.y - rect.height / 2;

        const clampedLeft = Math.max(8, left);
        const clampedTop = Math.max(8, Math.min(top, window.innerHeight - rect.height - 8));

        this._scrollOverlay.style.left = clampedLeft + 'px';
        this._scrollOverlay.style.top = clampedTop + 'px';
    },

    /**
     * Updates overlay position based on scroll.
     * @param {number} scrollTop
     * @private
     */
    _updateScrollOverlayFromScroll(scrollTop) {
        if (!this._scrollOverlay || !this._scrollOverlayAnchor) return;

        const grid = this._els.grid;
        if (!grid) return;

        const scrollDelta = scrollTop - this._scrollOverlayAnchor.scrollTop;
        const scrollableHeight = grid.scrollHeight - grid.clientHeight;

        if (scrollableHeight <= 0) return;

        const gridRect = grid.getBoundingClientRect();
        const trackHeight = gridRect.height;
        const thumbDelta = (scrollDelta / scrollableHeight) * trackHeight;

        const newTop = this._scrollOverlayAnchor.overlayY + thumbDelta;
        const rect = this._scrollOverlay.getBoundingClientRect();
        const clampedTop = Math.max(8, Math.min(newTop, window.innerHeight - rect.height - 8));

        this._scrollOverlay.style.top = clampedTop + 'px';
    },

    /**
     * Formats a date for the scroll overlay.
     * @param {Date} date
     * @returns {string}
     * @private
     */
    _formatScrollDate(date) {
        if (!(date instanceof Date) || isNaN(date)) return '';

        const now = new Date();
        const isThisYear = date.getFullYear() === now.getFullYear();

        const options = { month: 'short', day: 'numeric' };
        if (!isThisYear) {
            options.year = 'numeric';
        }

        return date.toLocaleDateString(undefined, options);
    },

    /* ----------------------------------------------------------------------
       SIMILARITY SLIDER
       ---------------------------------------------------------------------- */

    /**
     * Initialises the similarity slider.
     * @private
     */
    _initSimilaritySlider() {
        if (!this._els.similaritySlider) return;

        let debounceTimer = null;

        this._els.similaritySlider.addEventListener('input', () => {
            const value = this._els.similaritySlider.value;
            this._els.similarityValue.textContent = value + '%';

            // Sync search screen slider
            const searchSlider = App.$('filter-similarity');
            const searchValue = App.$('similarity-value');
            if (searchSlider) searchSlider.value = value;
            if (searchValue) searchValue.textContent = value + '%';
        });

        this._els.similaritySlider.addEventListener('change', async () => {
            const filter = App.getFilter();
            if (!filter || filter.type !== 'semantic' || !filter.text) return;

            if (debounceTimer) clearTimeout(debounceTimer);

            debounceTimer = setTimeout(async () => {
                const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

                App.showLoading('Searching...');
                try {
                    const response = await App.apiPost('/search', {
                        query: filter.text,
                        threshold: threshold,
                        limit: 500
                    });

                    if (response && response.results) {
                        filter.threshold = threshold;
                        filter.imageIds = response.results.map(r => r.id);
                        filter.scores = {};
                        response.results.forEach(r => {
                            filter.scores[r.id] = r.score;
                        });

                        App.setFilter(filter, { silent: true });
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

    /* ----------------------------------------------------------------------
       INFO PANEL
       ---------------------------------------------------------------------- */

    /**
     * Currently displayed image ID in info panel.
     * @type {string|null}
     * @private
     */
    _infoPanelImageId: null,

    /**
     * Updates the info panel based on selection.
     * @param {Array<string>} selection
     * @private
     */
    _updateInfoPanel(selection) {
        const content = this._els.infoContent;

        if (selection.length === 0) {
            this._infoPanelImageId = null;
            content.innerHTML = '<p class="info-placeholder">Select an image to view details</p>';
            return;
        }

        if (selection.length > 1) {
            this._infoPanelImageId = null;
            content.innerHTML = `
                <div class="info-section">
                    <p class="info-selection-count">${selection.length} images selected</p>
                </div>
            `;
            return;
        }

        const imageId = selection[0];
        if (imageId === this._infoPanelImageId) return;

        this._infoPanelImageId = imageId;
        this._renderInfoPanel(imageId);
    },

    /**
     * Renders the info panel for a single image.
     * @param {string} imageId
     * @private
     */
    async _renderInfoPanel(imageId) {
        const content = this._els.infoContent;
        content.innerHTML = '<p class="info-placeholder">Loading...</p>';

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

        this._bindInfoPanelEvents(imageId);
    },

    /**
     * Binds event listeners for info panel fields.
     * @param {string} imageId
     * @private
     */
    _bindInfoPanelEvents(imageId) {
        const descField = App.$('info-description');
        const ratingField = App.$('info-rating');
        const timestampField = App.$('info-timestamp');
        const emojiBtn = App.$('info-emoji-btn');

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
                const value = timestampField.value;
                if (value) {
                    const isoTimestamp = new Date(value).toISOString();
                    this._saveImageField(imageId, 'timestamp', isoTimestamp);
                }
            });
        }

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
     * Saves a field value for an image.
     * @param {string} imageId
     * @param {string} field
     * @param {string} value
     * @private
     */
    async _saveImageField(imageId, field, value) {
        const img = this.state.images.find(i => i.id === imageId);
        if (img) {
            img[field] = value;
        }

        try {
            await App.apiPost(`/images/${imageId}`, { [field]: value });
        } catch (error) {
            console.error(`Failed to save ${field}:`, error);
        }
    },

    /* ----------------------------------------------------------------------
       DELETION
       ---------------------------------------------------------------------- */

    /**
     * Deletes selected images after confirmation.
     * @param {Array<string>} ids
     * @private
     */
    async _deleteImages(ids) {
        if (ids.length === 0) return;

        const count = ids.length;
        const message = count === 1
            ? 'Are you sure you want to delete this image?'
            : `Are you sure you want to delete ${count} images?`;

        const confirmed = await App.confirm('Delete Images', message);
        if (!confirmed) return;

        for (const id of ids) {
            try {
                await App.apiDelete(`/images/${id}`);
                this.state.images = this.state.images.filter(img => img.id !== id);
                ThumbnailLoader.cancel(id);
            } catch (error) {
                console.error(`Failed to delete image ${id}:`, error);
            }
        }

        App.clearSelection();
        this._renderGrid();
    }
};

// Register module with App
App.registerModule('gallery', Gallery);
