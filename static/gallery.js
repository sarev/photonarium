/**
 * @fileoverview Gallery screen module for the Imaginary application.
 *
 * This module handles all functionality specific to the Gallery screen,
 * which is the primary view for browsing the image catalogue. It registers
 * with the core App module and responds to screen lifecycle events.
 *
 * Uses shared infrastructure from thumbnails.js:
 * - VirtualGrid: Virtual scrolling with absolute positioning
 * - GridSelection: Unified selection handling (click, keyboard, drag-box)
 * - ThumbnailLoader: Scroll-aware thumbnail fetching with distance-based priority
 *
 * Thumbnail Loading:
 *   - DOM elements are only created after their thumbnail blob URL is fetched
 *   - Items are absolutely positioned and can load in any order
 *   - A faint grid pattern shows placeholder positions during scroll
 *   - Priority based on absolute distance from center of visible area
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
     * Note: Image data comes from AppState.images (single source of truth).
     * This state only tracks UI concerns and derived/sorted data.
     * @type {Object}
     */
    state: {
        needsRefresh: true,
        lastImageCount: 0,
        pendingSelection: null  // Selection to apply when item loads
    },

    /**
     * AppState subscription cleanup functions.
     * @type {Array<Function>}
     * @private
     */
    _unsubs: [],

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
     * Fullscreen event subscription cleanup function.
     * @type {Function|null}
     * @private
     */
    _fullscreenUnsub: null,

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
     * Debug: dump state of gallery, grid, and specific image.
     * Call from console: Gallery._debugDump('image-id-here')
     * @param {string} [imageId] - Optional image ID to highlight
     * @param {string} [context] - Description of when this was called
     */
    _debugDump(imageId, context = 'manual') {
        console.group(`[Gallery._debugDump] ${context}`);

        // Gallery state
        console.log('Gallery.state:', { ...this.state });

        // Grid container
        const grid = this._els.grid;
        if (grid) {
            console.log('Grid container:', {
                clientWidth: grid.clientWidth,
                clientHeight: grid.clientHeight,
                scrollTop: grid.scrollTop,
                scrollHeight: grid.scrollHeight,
                offsetTop: grid.offsetTop,
                offsetLeft: grid.offsetLeft,
                hidden: grid.hidden,
                display: getComputedStyle(grid).display
            });
        } else {
            console.log('Grid container: NOT FOUND');
        }

        // VirtualGrid state
        if (this._grid) {
            const vgState = this._grid._state;
            const vgConfig = this._grid._config;
            console.log('VirtualGrid._state:', {
                itemsPerRow: vgState?.itemsPerRow,
                itemWidth: vgState?.itemWidth,
                itemHeight: vgState?.itemHeight,
                totalHeight: vgState?.totalHeight,
                visibleRows: vgState?.visibleRows,
                renderedCount: vgState?.renderedItems?.size,
                pendingCount: vgState?.pendingItems?.size
            });
            console.log('VirtualGrid._config:', {
                gap: vgConfig?.gap,
                padding: vgConfig?.padding,
                itemCount: vgConfig?.getItems?.()?.length
            });
            console.log('VirtualGrid._bound:', this._grid._bound);

            // Inner container
            const inner = this._grid._innerContainer;
            if (inner) {
                console.log('InnerContainer:', {
                    clientHeight: inner.clientHeight,
                    styleHeight: inner.style.height,
                    childCount: inner.children.length,
                    backgroundPosition: inner.style.backgroundPosition
                });
            }
        } else {
            console.log('VirtualGrid: NOT INITIALIZED');
        }

        // Specific image info
        if (imageId && this._grid) {
            const displayList = AppState.images.getDisplayList();
            const index = displayList.findIndex(img => img.id === imageId);
            console.log(`Image ${imageId}:`, {
                indexInDisplayList: index,
                inRenderedItems: this._grid._state?.renderedItems?.has(imageId),
                inPendingItems: this._grid._state?.pendingItems?.has(imageId)
            });

            if (index >= 0 && this._grid._state) {
                const { itemsPerRow, itemWidth, itemHeight } = this._grid._state;
                const { gap, padding } = this._grid._config;
                const row = Math.floor(index / itemsPerRow);
                const col = index % itemsPerRow;
                const expectedTop = padding + row * itemHeight;
                const expectedLeft = padding + col * (itemWidth + gap);
                console.log(`Image expected position:`, {
                    row, col, expectedTop, expectedLeft
                });
            }

            // Find actual DOM element
            const el = this._grid._innerContainer?.querySelector(`[data-id="${imageId}"]`);
            if (el) {
                console.log('Image DOM element:', {
                    styleTop: el.style.top,
                    styleLeft: el.style.left,
                    styleWidth: el.style.width,
                    styleHeight: el.style.height,
                    offsetTop: el.offsetTop,
                    offsetLeft: el.offsetLeft,
                    className: el.className
                });
            } else {
                console.log('Image DOM element: NOT IN DOM');
            }
        }

        // ThumbnailLoader state
        console.log('ThumbnailLoader:', ThumbnailLoader.getStats());

        console.groupEnd();
    },

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
            similarityValue: App.$('gallery-similarity-value'),
            // Duplicate group navigation
            btnPrevGroup: App.$('btn-prev-group'),
            btnNextGroup: App.$('btn-next-group'),
            dupGroupNavSeparator: document.querySelector('.dup-group-nav-separator')
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
            getItems: () => AppState.images.getDisplayList(),
            getItemId: (img) => img.id,
            createItem: (img, index, blobUrl) => this._createThumbnailItem(img, blobUrl),
            getThumbnailId: (img) => img.id,
            itemSelector: '.gallery-item',
            onItemCreated: (id, el) => {
                // Sync selection state when item is added to DOM
                if (this._selection && this._selection.isSelected(id)) {
                    el.classList.add('selected');
                }
            }
        });

        // Create GridSelection instance
        this._selection = GridSelection.create({
            grid: this._grid,
            getItems: () => AppState.images.getDisplayList(),
            getItemId: (img) => img.id,
            itemSelector: '.gallery-item',
            onSelectionChanged: (ids) => {
                App.setSelectedImages(ids);
            },
            onItemActivated: (id) => {
                this._openFullscreen(id);
            },
            onDeleteRequested: (ids) => {
                this._deleteImages(ids);
            },
            onGroupNavigate: (direction) => {
                this._navigateDupGroup(direction);
            }
        });

        // Set up similarity slider handler
        this._initSimilaritySlider();

        // Set up duplicate group navigation
        this._initDupGroupNav();

        // Subscribe to app events
        App.on('thumbnailSizeChanged', () => this._onThumbnailSizeChanged());
        App.on('sortChanged', () => this._onSortChanged());
        App.on('filterChanged', () => this._onFilterChanged());
        App.on('selectionChanged', (sel) => this._onSelectionChanged(sel));
        App.on('selectAll', () => this._selection.selectAll());
        App.on('imagesModified', (imageIds) => this._onImagesModified(imageIds));

        // Subscribe to AppState for reactive updates
        this._unsubs.push(AppState.images.onChanged(() => this._onImagesChanged()));
    },

    /**
     * Called when entering the gallery screen.
     */
    async onEnter() {
        if (this.state.needsRefresh) {
            // Check if there's a people filter - need to load filtered IDs first
            const filter = App.getFilter();
            const hasPeopleFilter = filter && filter.people && filter.people.length > 0;
            if (hasPeopleFilter) {
                this._showLoading('Filtering by people…');
                // Ensure people filter is loaded before rendering
                if (!filter.peopleImageIds) {
                    await this._loadPeopleFilteredImages(filter);
                }
            }
            await this._loadImages();
            if (hasPeopleFilter) {
                this._hideLoading();
            }
        } else {
            // Re-bind grid and selection
            this._grid.bind();
            // Refresh layout in case container size changed while away
            this._grid.refresh();
        }
        // Bind selection handlers
        this._selection.bind();
        // Update duplicate group nav button state
        this._updateDupGroupNavState();
    },

    /**
     * Called when leaving the gallery screen.
     */
    onLeave() {
        // Unbind selection handlers
        this._selection.unbind();
        // Unbind grid scroll handler
        this._grid.unbind();
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
     * Loads images from AppState and renders the grid.
     * @private
     */
    async _loadImages() {
        // Show inline loading on first load
        const isFirstLoad = App.getCachedImageCount() === 0;
        if (isFirstLoad) {
            this._showLoading('Loading images…');
        }

        try {
            // Load into AppState (single source of truth)
            await AppState.images.load();
            const imageCount = AppState.images.getCount();

            // Check if we're still on gallery screen after async fetch
            if (App.getScreen() !== 'gallery') {
                return;
            }

            this.state.lastImageCount = imageCount;
            this._renderGrid();
            this.state.needsRefresh = false;

            // Apply initial selection from filter (e.g., best image in duplicate group)
            this._applyInitialSelection();
        } catch (error) {
            console.error('Failed to load images:', error);
            App.showError('Failed to load images');
        } finally {
            if (isFirstLoad) {
                this._hideLoading();
            }
        }
    },

    /**
     * Stores initial selection from the current filter to be applied when items load.
     * The actual selection is applied in _createThumbnailItem when the DOM element is created.
     * @private
     */
    _applyInitialSelection() {
        const filter = App.getFilter();
        if (filter?.initialSelection?.length) {
            // Store the selection to apply when the item's thumbnail loads
            this.state.pendingSelection = new Set(filter.initialSelection);
            return;
        }

        // Check for persisted single selection from localStorage
        const persistedId = localStorage.getItem('gallery.selectedImageId');
        if (persistedId) {
            // Verify the image still exists in the display list
            const displayList = AppState.images.getDisplayList();
            const exists = displayList.some(img => img.id === persistedId);
            if (exists) {
                App.setSelectedImages([persistedId]);
                // Scroll to the selected image after a brief delay to ensure grid is rendered
                requestAnimationFrame(() => {
                    this._grid.scrollToId(persistedId, 'auto');
                });
                return;
            } else {
                // Image no longer exists, clear the persisted selection
                localStorage.removeItem('gallery.selectedImageId');
            }
        }

        this.state.pendingSelection = null;
    },

    /**
     * Handles AppState.images changes.
     * Reactive update when images are added, removed, or modified.
     * @private
     */
    _onImagesChanged() {
        // Only refresh if we're on the gallery screen
        if (App.getScreen() !== 'gallery') {
            this.state.needsRefresh = true;
            return;
        }

        const imageCount = AppState.images.getCount();
        if (imageCount === this.state.lastImageCount) return;

        // Preserve state
        const scrollTop = this._els.grid?.scrollTop || 0;
        const currentSelection = App.getSelectedImages();

        this.state.lastImageCount = imageCount;

        // Re-render (will recompute from AppState)
        this._renderGrid();

        // Restore scroll position
        if (this._els.grid) {
            this._els.grid.scrollTop = scrollTop;
        }

        // Restore selection (filter out deleted images)
        const displayList = AppState.images.getDisplayList();
        const existingIds = new Set(displayList.map(img => img.id));
        const validSelection = currentSelection.filter(id => existingIds.has(id));
        if (validSelection.length > 0) {
            App.setSelectedImages(validSelection);
        }
    },

    /* ----------------------------------------------------------------------
       SORTING & FILTERING
       ---------------------------------------------------------------------- */

    /**
     * Loads content similarity data for sorting.
     * Data is stored in AppState.images, not locally.
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
            // Load into AppState (stores internally)
            await AppState.images.loadSimilarities(referenceId);
            this._renderGrid();
            this._scrollToTop();
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

    /* ----------------------------------------------------------------------
       EVENT HANDLERS
       ---------------------------------------------------------------------- */

    /**
     * Handles thumbnail size changes.
     * @private
     */
    _onThumbnailSizeChanged() {
        // Only refresh if gallery is active screen
        if (App.getScreen() !== 'gallery') return;

        this._updateGridStyle();
        // Clear ThumbnailLoader and refresh grid
        ThumbnailLoader.clear();
        if (this._grid && AppState.images.getDisplayList().length > 0) {
            this._grid.refresh();
        }
    },

    /**
     * Handles sort changes.
     * @private
     */
    _onSortChanged() {
        // Only handle if gallery is active screen
        if (App.getScreen() !== 'gallery') return;

        const { by } = App.getSort();
        const filter = App.getFilter();
        const isSemanticFilter = filter && filter.type === 'semantic';

        if (by === 'content') {
            if (isSemanticFilter) {
                this._renderGrid();
                return;
            }

            const selected = App.getSelectedImages();
            const referenceId = selected.length > 0 ? selected[0] : null;
            const cachedReferenceId = AppState.images.getSimilarityReferenceId();

            if (referenceId && cachedReferenceId === referenceId) {
                // Already have similarities for this reference
                this._renderGrid();
            } else {
                this._loadContentSimilarities();
            }
        } else if (by === 'people') {
            // Load people names if not cached in AppState
            if (!AppState.images.hasPeopleNames()) {
                this._loadPeopleNames();
            } else {
                this._renderGrid();
            }
        } else {
            // Clear similarity data when switching away from content sort
            AppState.images.clearSimilarities();
            this._renderGrid();
        }
    },

    /**
     * Loads people names for all images for sorting by people.
     * Data is stored in AppState.images, not locally.
     * @private
     */
    async _loadPeopleNames() {
        try {
            // Load into AppState (stores internally)
            await AppState.images.loadPeopleNames();
            this._renderGrid();
            this._scrollToTop();
        } catch (error) {
            console.error('Failed to load people names:', error);
            App.showError('Could not load people data for sorting.');
            App.setSortBy('date');
        }
    },

    /**
     * Handles filter changes.
     * @private
     */
    async _onFilterChanged() {
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

        // Update duplicate group navigation button state
        this._updateDupGroupNavState();

        if (isSemanticFilter) {
            App.setSortBy('content');
            App.setSortDirection('desc');
        }

        // Determine if we need to show loading (async operations)
        const hasPeopleFilter = filter && filter.people && filter.people.length > 0;
        const hasSemanticFilter = filter && filter.type === 'semantic';
        const showLoading = App.getScreen() === 'gallery' && (hasPeopleFilter || hasSemanticFilter);

        if (showLoading) {
            this._showLoading(hasPeopleFilter ? 'Filtering by people…' : 'Applying filter…');
        }

        // If filter has people, load the filtered image IDs from the API
        if (hasPeopleFilter) {
            await this._loadPeopleFilteredImages(filter);
        }

        if (App.getScreen() === 'gallery') {
            await this._loadImages();
            if (showLoading) {
                this._hideLoading();
            }
            this._scrollToTop();
        } else {
            this.state.needsRefresh = true;
        }
    },

    /**
     * Loads image IDs filtered by people from the API.
     * @param {Object} filter - The current filter object
     * @private
     */
    async _loadPeopleFilteredImages(filter) {
        try {
            const peopleIds = filter.people.map(p => p.id);
            filter.peopleImageIds = await AppState.images.getFilteredByPeople(peopleIds);
        } catch (error) {
            console.error('Failed to load people-filtered images:', error);
            filter.peopleImageIds = null;
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
     * Handles images modified (rotation, rescan, etc.).
     * Called via imagesModified event from AppState.events.
     * @param {string[]} imageIds - IDs of modified images
     * @private
     */
    _onImagesModified(imageIds) {
        if (!imageIds?.length || !this._grid) return;

        // If gallery isn't visible (different screen or fullscreen overlay),
        // mark for full refresh on next enter to avoid stale state issues
        if (App.getScreen() !== 'gallery' || AppState.nav.isFullscreenOpen()) {
            this.state.needsRefresh = true;
            return;
        }

        // Remove rendered elements so they get re-fetched with new thumbnails
        // This also revokes old blob URLs to prevent memory leaks
        // Note: ThumbnailLoader.bustCache already called in event handler
        for (const imageId of imageIds) {
            this._grid.removeRenderedItem(imageId, true);
        }

        // Trigger a refresh to re-request the thumbnails
        this._grid.refresh();
    },

    /* ----------------------------------------------------------------------
       GRID RENDERING
       ---------------------------------------------------------------------- */

    /**
     * Renders the thumbnail grid.
     * @private
     */
    _renderGrid() {
        const grid = this._els.grid;

        // Get display images from AppState (single source of truth)
        const displayList = AppState.images.getDisplayList();

        // Handle empty state
        if (displayList.length === 0) {
            grid.innerHTML = '<div class="empty-state"><span class="material-symbols-outlined">photo_library</span><p>No images to display</p></div>';
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
    },

    /**
     * Creates a thumbnail item element with the thumbnail already loaded.
     * Also applies pending selection if this item should be selected.
     * @param {Object} img - Image data
     * @param {string} blobUrl - Blob URL for the thumbnail
     * @returns {HTMLElement}
     * @private
     */
    _createThumbnailItem(img, blobUrl) {
        const item = App.createElement('div', {
            className: 'gallery-item loaded',
            dataId: img.id
        });

        // Thumbnail image with blob URL already set
        const thumb = App.createElement('img', {
            src: blobUrl,
            alt: img.basename
        });

        // Basename label
        const label = App.createElement('span', { className: 'gallery-item-label' }, img.basename);

        item.appendChild(thumb);
        item.appendChild(label);

        // Check if this item has a pending selection
        if (this.state.pendingSelection?.has(img.id)) {
            // Clear pending selection (only apply once)
            this.state.pendingSelection = null;
            // Apply selection after DOM is fully attached
            const imageId = img.id;
            setTimeout(() => {
                App.setSelectedImages([imageId]);
            }, 0);
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

        const displayList = AppState.images.getDisplayList();
        if (displayList.length === 0) return;

        // Calculate first visible image
        const itemHeight = this._grid.getItemHeight();
        const itemsPerRow = this._grid.getItemsPerRow();
        if (!itemHeight || !itemsPerRow) return;

        const firstVisibleRow = Math.floor(scrollTop / itemHeight);
        const firstVisibleIndex = firstVisibleRow * itemsPerRow;

        if (firstVisibleIndex < 0 || firstVisibleIndex >= displayList.length) return;

        const img = displayList[firstVisibleIndex];
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
            // Cache rects when overlay first appears to avoid reflows during scroll
            this._cachedGridRect = this._els.grid?.getBoundingClientRect();
            this._cachedOverlayHeight = this._scrollOverlay.getBoundingClientRect().height;
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
            this._cachedGridRect = null;
            this._cachedOverlayHeight = null;
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

        // Use cached rects to avoid forced reflows on every scroll event
        const trackHeight = this._cachedGridRect?.height || grid.getBoundingClientRect().height;
        const thumbDelta = (scrollDelta / scrollableHeight) * trackHeight;

        const newTop = this._scrollOverlayAnchor.overlayY + thumbDelta;
        const overlayHeight = this._cachedOverlayHeight || 30;
        const clampedTop = Math.max(8, Math.min(newTop, window.innerHeight - overlayHeight - 8));

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

        // Add hover tooltip
        App.addSliderHoverTooltip(this._els.similaritySlider);

        this._els.similaritySlider.addEventListener('change', async () => {
            const filter = App.getFilter();
            if (!filter || filter.type !== 'semantic' || !filter.text) return;

            if (debounceTimer) clearTimeout(debounceTimer);

            debounceTimer = setTimeout(async () => {
                const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

                this._showLoading('Searching…');
                try {
                    const response = await AppState.search.execute(filter.text, threshold, 10000);

                    if (response && response.results) {
                        filter.threshold = threshold;
                        filter.imageIds = response.results.map(r => r.id);
                        filter.scores = {};
                        response.results.forEach(r => {
                            filter.scores[r.id] = r.score;
                        });

                        App.setFilter(filter);
                        this._renderGrid();
                    }
                } catch (error) {
                    console.error('Failed to update search:', error);
                    App.showError('Failed to update search results.');
                } finally {
                    this._hideLoading();
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
        content.innerHTML = '<p class="info-placeholder info-loading">Loading…</p>';

        let img;
        try {
            img = await AppState.images.fetchById(imageId);
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
                <div class="info-label-row">
                    <label class="info-label" for="info-description">Description</label>
                    <button type="button" id="info-generate-caption-btn" class="toolbar-btn toolbar-btn-small" title="Generate AI caption">
                        <span class="material-symbols-outlined">auto_awesome</span>
                    </button>
                </div>
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

            <div class="info-section info-histogram">
                <label class="info-label">Histogram</label>
                <div class="histogram-container" id="histogram-container">
                    <img id="histogram-r" class="histogram-channel" alt="Red">
                    <img id="histogram-g" class="histogram-channel" alt="Green">
                    <img id="histogram-b" class="histogram-channel" alt="Blue">
                    <div class="histogram-loading">Loading…</div>
                </div>
                <div class="histogram-toggles">
                    <button type="button" id="histogram-toggle-r" class="histogram-toggle histogram-toggle-r active" title="Toggle red channel">R</button>
                    <button type="button" id="histogram-toggle-g" class="histogram-toggle histogram-toggle-g active" title="Toggle green channel">G</button>
                    <button type="button" id="histogram-toggle-b" class="histogram-toggle histogram-toggle-b active" title="Toggle blue channel">B</button>
                </div>
            </div>
        `;

        this._bindInfoPanelEvents(imageId);
        this._bindHistogramToggles();
        this._loadHistogram(imageId);
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
            descField.addEventListener('keydown', (e) => {
                // Enter commits and blurs, Shift+Enter inserts newline
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    descField.blur();
                }
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

        const generateCaptionBtn = App.$('info-generate-caption-btn');
        if (generateCaptionBtn && descField) {
            generateCaptionBtn.addEventListener('click', async () => {
                generateCaptionBtn.disabled = true;
                generateCaptionBtn.classList.add('loading');
                try {
                    const response = await App.apiPost(`/images/${imageId}/generate-caption`);
                    const caption = response.data?.caption;
                    if (caption) {
                        descField.value = caption;
                        // Auto-save the generated caption
                        await this._saveImageField(imageId, 'description', caption);
                    }
                } catch (error) {
                    console.error('Failed to generate caption:', error);
                    App.showError('Failed to generate caption.');
                } finally {
                    generateCaptionBtn.disabled = false;
                    generateCaptionBtn.classList.remove('loading');
                }
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
        try {
            await AppState.images.update({ id: imageId, [field]: value });
        } catch (error) {
            console.error(`Failed to save ${field}:`, error);
            App.showError(`Failed to save ${field}.`);
        }
    },

    /**
     * Binds histogram channel toggle buttons.
     * @private
     */
    _bindHistogramToggles() {
        const channels = ['r', 'g', 'b'];
        channels.forEach(ch => {
            const btn = App.$(`histogram-toggle-${ch}`);
            const img = App.$(`histogram-${ch}`);
            if (btn && img) {
                btn.addEventListener('click', () => {
                    btn.classList.toggle('active');
                    img.classList.toggle('hidden');
                });
            }
        });
    },

    /**
     * Pending histogram load timer (for debouncing).
     * @type {number|null}
     * @private
     */
    _histogramTimer: null,

    /**
     * Loads histogram images for an image (debounced).
     * @param {string} imageId
     * @private
     */
    _loadHistogram(imageId) {
        // Cancel any pending histogram load
        if (this._histogramTimer) {
            clearTimeout(this._histogramTimer);
            this._histogramTimer = null;
        }

        // Debounce: wait 200ms before fetching
        this._histogramTimer = setTimeout(async () => {
            this._histogramTimer = null;

            const container = App.$('histogram-container');
            const loading = container?.querySelector('.histogram-loading');

            try {
                const response = await App.apiGet(`/images/${imageId}/histogram`);
                const histogramData = response.data;

                // Set image sources from data URLs
                const rImg = App.$('histogram-r');
                const gImg = App.$('histogram-g');
                const bImg = App.$('histogram-b');

                if (rImg) rImg.src = histogramData.r;
                if (gImg) gImg.src = histogramData.g;
                if (bImg) bImg.src = histogramData.b;

                // Hide loading indicator
                if (loading) loading.style.display = 'none';

            } catch (error) {
                console.error('Failed to load histogram:', error);
                if (loading) loading.textContent = 'Failed to load';
            }
        }, 200);
    },

    /* ----------------------------------------------------------------------
       FULLSCREEN INTEGRATION
       ---------------------------------------------------------------------- */

    /**
     * Opens fullscreen viewer and subscribes to navigation events.
     * Updates gallery selection to match fullscreen navigation.
     * @param {string} imageId - Image ID to open
     * @private
     */
    _openFullscreen(imageId) {
        // Clear any existing subscription (shouldn't happen, but be safe)
        if (this._fullscreenUnsub) {
            this._fullscreenUnsub();
            this._fullscreenUnsub = null;
        }

        // Subscribe to fullscreen navigation events
        this._fullscreenUnsub = AppState.nav.onChanged((event) => {
            if (event.property === 'fullscreenImageId') {
                // Fullscreen navigated to a new image - update our selection
                const newId = AppState.nav.getFullscreenImageId();
                if (newId && this._selection) {
                    this._selection.select(newId);
                }
            } else if (event.property === 'fullscreenClosing') {
                // Fullscreen is closing - check if we need to refresh
                if (this.state.needsRefresh) {
                    // Images were modified while fullscreen was open - do full refresh
                    this._loadImages();
                } else if (event.imageId && this._grid) {
                    // Just scroll to the last viewed image
                    this._grid.scrollToId(event.imageId);
                }
                // Unsubscribe
                if (this._fullscreenUnsub) {
                    this._fullscreenUnsub();
                    this._fullscreenUnsub = null;
                }
            }
        });

        // Clear multi-selection and select only the target image
        if (this._selection) {
            this._selection.select(imageId);
        }

        // Open fullscreen
        App.showFullscreen(imageId);
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

        // Find the index of the first deleted image (to select next image after deletion)
        const displayList = AppState.images.getDisplayList();
        const deletedSet = new Set(ids);
        let firstDeletedIndex = displayList.findIndex(img => deletedSet.has(img.id));

        try {
            // Delete via AppState (handles optimistic update and rollback)
            await AppState.images.delete(ids);
            // AppState updates its cache; _renderGrid will recompute from it
        } catch (error) {
            console.error('Failed to delete images:', error);
            // Error is already shown by AppState
            return;
        }

        this._renderGrid();

        // Select the image now at the position where the first deleted image was
        const newDisplayList = AppState.images.getDisplayList();
        if (newDisplayList.length > 0 && firstDeletedIndex >= 0) {
            // Clamp to valid range (in case we deleted images at the end)
            const newIndex = Math.min(firstDeletedIndex, newDisplayList.length - 1);
            const nextImage = newDisplayList[newIndex];
            App.setSelectedImages([nextImage.id]);
            // Scroll to keep the selection visible
            this._grid.scrollToId(nextImage.id, 'auto');
        } else {
            App.clearSelection();
        }
    },

    /* ----------------------------------------------------------------------
       DUPLICATE GROUP NAVIGATION
       ---------------------------------------------------------------------- */

    /**
     * Initialises duplicate group navigation controls.
     * @private
     */
    _initDupGroupNav() {
        // Button click handlers
        if (this._els.btnPrevGroup) {
            this._els.btnPrevGroup.addEventListener('click', () => this._navigateDupGroup(-1));
        }
        if (this._els.btnNextGroup) {
            this._els.btnNextGroup.addEventListener('click', () => this._navigateDupGroup(1));
        }
        // Keyboard shortcuts (Alt+Left/Right) are handled by GridSelection via onGroupNavigate
    },

    /**
     * Updates the visibility and enabled state of duplicate group nav buttons.
     * Called when filter changes.
     * @private
     */
    _updateDupGroupNavState() {
        const filter = App.getFilter();
        const isDupFilter = filter && filter.type === 'duplicates' && filter.groupHash;

        // Show/hide buttons and separator
        const show = isDupFilter;
        if (this._els.btnPrevGroup) {
            this._els.btnPrevGroup.hidden = !show;
            this._els.btnPrevGroup.disabled = !show;
        }
        if (this._els.btnNextGroup) {
            this._els.btnNextGroup.hidden = !show;
            this._els.btnNextGroup.disabled = !show;
        }
        if (this._els.dupGroupNavSeparator) {
            this._els.dupGroupNavSeparator.hidden = !show;
        }
    },

    /**
     * Navigates to the previous or next duplicate group.
     * @param {number} direction - -1 for previous, +1 for next
     * @private
     */
    _navigateDupGroup(direction) {
        const filter = App.getFilter();
        if (!filter || filter.type !== 'duplicates' || !filter.groupHash) return;

        // Get groups from AppState at the filter's source level
        const level = filter.sourceLevel ?? AppState.duplicates.getCurrentLevel();
        const groups = AppState.duplicates.getGroups(level);
        if (groups.length === 0) return;

        // Find current group index
        const currentHash = filter.groupHash;
        const currentIndex = groups.findIndex(g => g.group_hash === currentHash);
        if (currentIndex === -1) return;

        // Calculate new index with wrapping
        let newIndex = currentIndex + direction;
        if (newIndex < 0) {
            newIndex = groups.length - 1; // Wrap to last
        } else if (newIndex >= groups.length) {
            newIndex = 0; // Wrap to first
        }

        // Navigate to new group via Duplicates module (handles filter update)
        const newGroup = groups[newIndex];
        if (newGroup && typeof Duplicates !== 'undefined') {
            Duplicates.navigateToGroup(newGroup.group_hash);
        }
    },

    /* ----------------------------------------------------------------------
       LOADING STATE
       ---------------------------------------------------------------------- */

    /**
     * Shows the global loading overlay with a message.
     * @param {string} message - The loading message to display
     * @private
     */
    _showLoading(message) {
        AppState.loading.show('gallery', message);
    },

    /**
     * Hides the global loading overlay if gallery is the owner.
     * @private
     */
    _hideLoading() {
        AppState.loading.hide('gallery');
    }
};

// Register module with App
App.registerModule('gallery', Gallery);
