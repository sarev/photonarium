/**
 * @fileoverview Gallery screen module for the Photonarium application.
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
 *   - Priority based on absolute distance from centre of visible area
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
        pendingSelection: null,  // Selection to apply when item loads
        prevSort: null,            // Sort to restore when leaving group view
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
     * Image ID to throb when its thumbnail is created (one-shot).
     * Set by onEnter, consumed by onItemCreated.
     * @type {string|null}
     * @private
     */
    _throbTargetId: null,

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
                display: getComputedStyle(grid).display,
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
                pendingCount: vgState?.pendingItems?.size,
            });
            console.log('VirtualGrid._config:', {
                gap: vgConfig?.gap,
                padding: vgConfig?.padding,
                itemCount: vgConfig?.getItems?.()?.length,
            });
            console.log('VirtualGrid._bound:', this._grid._bound);

            // Inner container
            const inner = this._grid._innerContainer;
            if (inner) {
                console.log('InnerContainer:', {
                    clientHeight: inner.clientHeight,
                    styleHeight: inner.style.height,
                    childCount: inner.children.length,
                    backgroundPosition: inner.style.backgroundPosition,
                });
            }
        } else {
            console.log('VirtualGrid: NOT INITIALISED');
        }

        // Specific image info
        if (imageId && this._grid) {
            const displayList = AppState.images.getDisplayList();
            const index = displayList.findIndex(img => img.id === imageId);
            console.log(`Image ${imageId}:`, {
                indexInDisplayList: index,
                inRenderedItems: this._grid._state?.renderedItems?.has(imageId),
                inPendingItems: this._grid._state?.pendingItems?.has(imageId),
            });

            if (index >= 0 && this._grid._state) {
                const { itemsPerRow, itemWidth, itemHeight } = this._grid._state;
                const { gap, padding } = this._grid._config;
                const row = Math.floor(index / itemsPerRow);
                const col = index % itemsPerRow;
                const expectedTop = padding + row * itemHeight;
                const expectedLeft = padding + col * (itemWidth + gap);
                console.log('Image expected position:', {
                    row, col, expectedTop, expectedLeft,
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
                    className: el.className,
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
            galleryContainer: document.querySelector('.gallery-container'),
            infoPanel: App.$('info-panel'),
            infoContent: App.$('info-content'),
            btnToggleInfo: App.$('btn-toggle-info'),
            similarityControl: App.$('gallery-similarity-control'),
            similaritySlider: App.$('gallery-similarity-slider'),
            similarityValue: App.$('gallery-similarity-value'),
            // Duplicate group navigation
            btnPrevGroup: App.$('btn-prev-group'),
            btnNextGroup: App.$('btn-next-group'),
            dupGroupNavSeparator: document.querySelector('.dup-group-nav-separator'),
            // Remove from group button
            btnRemoveFromGroup: App.$('btn-remove-from-group'),
        };

        // Info panel collapse toggle
        this._initInfoPanelCollapse();

        // Delegated click handler for info panel interactive elements
        this._els.infoContent.addEventListener('click', async (e) => {
            // Copy filename to clipboard
            const copyBtn = e.target.closest('.info-copy-btn');
            if (copyBtn) {
                e.preventDefault();
                const text = copyBtn.dataset.copy;
                if (text) {
                    try {
                        await navigator.clipboard.writeText(text);
                    } catch (err) {
                        console.error('Failed to copy to clipboard:', err);
                    }
                }
                return;
            }

            // Reveal in file explorer
            const pathEl = e.target.closest('.info-path-clickable');
            if (pathEl) {
                const imageId = pathEl.dataset.imageId;
                if (!imageId) return;
                try {
                    await App.apiPost('/reveal', { target: 'image', id: imageId });
                } catch (error) {
                    console.error('Failed to open folder:', error);
                    App.showError('Failed to open containing folder.');
                }
            }
        });

        // Create VirtualGrid instance
        this._grid = VirtualGrid.create({
            container: this._els.grid,
            getItems: () => AppState.images.getDisplayList(),
            getItemId: (img) => img.id,
            createItem: (img, index, blobUrl) => this._createThumbnailItem(img, blobUrl),
            getThumbnailId: (img) => img.id,
            // Use preferred scene thumbnails for videos
            getThumbnailUrl: (thumbId) => {
                const img = AppState.images.getById(thumbId);
                if (img && img.media_type === 'video' && img.preferred_scene_id) {
                    return `/api/scenes/${img.preferred_scene_id}/thumbnail`;
                }
                return App.thumbnailUrl(thumbId);
            },
            itemSelector: '.gallery-item',
            getScrollOverlayText: (item) => {
                const { by } = App.getSort();
                if (by === 'date' && item.timestamp) {
                    return VirtualGrid.formatScrollDate(new Date(item.timestamp));
                }
                if (by === 'rating') {
                    return item.rating || 'No rating';
                }
                return null;
            },
            onItemCreated: (id, el) => {
                // Sync selection state when item is added to DOM
                if (this._selection && this._selection.isSelected(id)) {
                    el.classList.add('selected');
                }
                // Trigger one-shot throb if this is the target thumbnail
                if (this._throbTargetId && id === this._throbTargetId) {
                    this._throbTargetId = null;
                    el.classList.add('throb');
                    el.addEventListener('animationend', () => el.classList.remove('throb'), { once: true });
                }
            },
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
                // Backspace/Delete in a custom group context removes from group instead of deleting
                const filter = App.getFilter();
                if (filter && filter.type === 'duplicates' && filter.sourceLevel === 5 && filter.groupHash) {
                    this._removeFromGroup();
                } else {
                    this._deleteImages(ids);
                }
            },
            onGroupNavigate: (direction) => {
                this._navigateDupGroup(direction);
            },
        });

        // Set up similarity slider handler
        this._initSimilaritySlider();

        // Set up duplicate group navigation
        this._initDupGroupNav();

        // Set up group picker dialog
        this._initGroupPicker();

        // Set up remove-from-group button
        this._initRemoveFromGroup();

        // Subscribe to app events
        App.on('thumbnailSizeChanged', () => this._onThumbnailSizeChanged());
        App.on('sortChanged', () => this._onSortChanged());
        App.on('filterChanged', () => this._onFilterChanged());
        App.on('selectionChanged', (sel) => this._onSelectionChanged(sel));
        App.on('selectAll', () => this._selection.selectAll());
        App.on('trashSelected', () => this._deleteImages(App.getSelectedImages()));
        App.on('imagesModified', (imageIds) => this._onImagesModified(imageIds));

        // Subscribe to AppState for reactive updates
        this._unsubs.push(AppState.images.onChanged((event) => this._onImagesChanged(event)));
    },

    /**
     * Called when entering the gallery screen.
     */
    async onEnter() {
        if (this.state.needsRefresh) {
            // Check if there's a people filter - need to load filtered IDs first
            const filter = App.getFilter();
            const hasPeopleFilter = filter && filter.people && filter.people.length > 0;
            const isDupFilter = filter && filter.type === 'duplicates' && filter.groupHash;
            const showLoading = hasPeopleFilter || isDupFilter;
            if (showLoading) {
                this._showLoading(hasPeopleFilter ? 'Filtering by people…' : 'Loading group…');
                // Ensure people filter is loaded before rendering
                if (hasPeopleFilter && !filter.peopleImageIds) {
                    await this._loadPeopleFilteredImages(filter);
                }
            }
            await this._loadImages();
            if (showLoading) {
                this._hideLoading();
            }
            this._scrollToTop();
        } else {
            // Re-bind grid and selection
            this._grid.bind();
            // Refresh layout in case container size changed while away
            this._grid.refresh();
            // Background delta sync: picks up images added while on another
            // screen (e.g. user watched a scan on the Database screen then
            // navigated here before processing_complete fired).  Delta loads
            // are cheap — returns nothing if the cache is already current.
            // If new images arrive, the broadcast triggers _onImagesChanged()
            // which re-renders the grid automatically.
            AppState.images.load();
        }
        // Bind selection handlers
        this._selection.bind();
        // Update duplicate group nav button state
        this._updateDupGroupNavState();

        // If fullscreen was viewing an image, select it in the gallery
        // (if it's in the current display list) so the user returns to it.
        // consumeLastViewedImageId() is one-shot — returns null on subsequent calls.
        const lastViewedId = AppState.nav.consumeLastViewedImageId();
        let throbId = null;
        if (lastViewedId) {
            const displayList = AppState.images.getDisplayList();
            const inList = displayList.some(img => img.id === lastViewedId);
            if (inList) {
                this._selection.select(lastViewedId);
                throbId = lastViewedId;
            }
        }

        // Scroll the first selected thumbnail into view so the user can
        // see their selection when returning from another screen
        const selected = this._selection.getSelected();
        if (selected.length > 0) {
            this._grid.scrollToId(selected[0], 'instant');
        }

        // Throb the thumbnail so it's easy to spot after cross-screen navigation.
        // The element may not exist yet (thumbnails load asynchronously), so we
        // set a target ID that onItemCreated checks when the element is added.
        // Also try immediately in case it's already rendered.
        if (throbId) {
            this._throbTargetId = throbId;
            const el = this._grid._innerContainer?.querySelector(`[data-id="${throbId}"]`);
            if (el) {
                this._throbTargetId = null;
                el.classList.add('throb');
                el.addEventListener('animationend', () => el.classList.remove('throb'), { once: true });
            }
        }
    },

    /**
     * Called when leaving the gallery screen.
     */
    onLeave() {
        // Unbind selection handlers
        this._selection.unbind();
        // Unbind grid scroll handler (also hides scroll overlay)
        this._grid.unbind();
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
        const isDupFilter = filter && filter.type === 'duplicates' && filter.groupHash;

        // Group view: select the first image (best quality after quality sort)
        if (isDupFilter) {
            const displayList = AppState.images.getDisplayList();
            if (displayList.length > 0) {
                this.state.pendingSelection = new Set([displayList[0].id]);
            }
            return;
        }

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
     * @param {Object} [event] - Change event from AppState.images broadcast.
     * @private
     */
    _onImagesChanged(event) {
        // Only refresh if we're on the gallery screen
        if (App.getScreen() !== 'gallery') {
            this.state.needsRefresh = true;
            return;
        }

        const imageCount = AppState.images.getCount();

        // For delta updates (not a full reload), skip if count is unchanged.
        // Full reloads (e.g. from processing_complete) always refresh so
        // newly indexed images appear even if the count was already correct.
        if (!event?.reload && imageCount === this.state.lastImageCount) return;

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
                App.showError('This image is still being processed. Please wait.');
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
        } else if (by === 'quality') {
            // Quality sort uses data already in image objects — just re-render
            AppState.images.clearSimilarities();
            this._renderGrid();
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

        // When applying a filter, clear selection — the selected image may not
        // be in the filtered set.  When clearing a filter, keep it — the image
        // is guaranteed to be in the full (unfiltered) list.
        if (filter) {
            AppState.selection.clear('gallery');
        }

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

        const isDupFilter = filter && filter.type === 'duplicates' && filter.groupHash;

        if (isSemanticFilter || isDupFilter) {
            // Save current sort so we can restore when leaving the filter/group
            if (!this.state.prevSort) {
                this.state.prevSort = { ...App.getSort() };
            }
            if (isSemanticFilter) {
                App.setSortBy('content');
            } else {
                App.setSortBy('quality');
            }
            App.setSortDirection('desc');
        } else if (this.state.prevSort) {
            // Leaving semantic/group view — restore the sort that was active
            // before we auto-switched.  Only fires if prevSort was set above
            // (not when the user chose the sort manually).
            App.setSortBy(this.state.prevSort.by || 'date');
            App.setSortDirection(this.state.prevSort.direction || 'desc');
            this.state.prevSort = null;
        }

        // Determine if we need to show loading (async operations)
        const hasPeopleFilter = filter && filter.people && filter.people.length > 0;
        const hasSemanticFilter = filter && filter.type === 'semantic';
        const showLoading = App.getScreen() === 'gallery' && (hasPeopleFilter || hasSemanticFilter || isDupFilter);

        if (showLoading) {
            const msg = hasPeopleFilter ? 'Filtering by people…'
                : isDupFilter ? 'Loading group…'
                    : 'Applying filter…';
            this._showLoading(msg);
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
            // When clearing a filter, scroll to the preserved selection so the
            // user keeps their place.  When applying, scroll to top.
            const selected = AppState.selection.get('gallery');
            if (!filter && selected.length > 0 && this._grid) {
                this._grid.scrollToId(selected[0], 'auto');
            } else {
                this._scrollToTop();
            }
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
        this._updateContentSortButton(selection);
        // Defer info panel updates while fullscreen is open — the selection
        // tracks every image the user navigates to, but rendering the info
        // panel (including on-demand histogram generation) is wasted work
        // when the gallery is hidden behind the fullscreen overlay.
        // The fullscreenClosing handler triggers a catch-up update instead.
        if (AppState.nav.isFullscreenOpen()) return;
        this._updateInfoPanel(selection);
    },

    /**
     * Enables/disables the "Sort by content similarity" button based on
     * whether exactly one image is selected.
     * @param {string[]} selection - Currently selected image IDs
     * @private
     */
    _updateContentSortButton(selection) {
        const btn = document.getElementById('btn-sort-content');
        if (btn) btn.disabled = selection.length !== 1;
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

        // Handle empty state — unbind grid/selection so scroll listeners and
        // blob URLs from the previous render are cleaned up
        if (displayList.length === 0) {
            if (this._selection) this._selection.unbind();
            if (this._grid) this._grid.unbind();
            ThumbnailLoader.clear();
            const emptyMsg = AppState.filter.isActive()
                ? 'No images match the current filter'
                : 'No images in library';
            grid.innerHTML = `<div class="empty-state">${App.icon('photo_library', '\u{1F3DE}')}<p>${emptyMsg}</p></div>`;
            return;
        }

        // Update grid CSS for thumbnail size
        this._updateGridStyle();

        // Clear and render via VirtualGrid
        ThumbnailLoader.clear();
        this._grid.render();

        // Bind selection
        this._selection.bind();
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
            dataId: img.id,
        });

        // Thumbnail tooltip: always show path + raw aesthetic scores (zero cost,
        // data is already in the image object).  When quality-sorted, also show
        // the full percentile breakdown.
        let title = img.path;
        const hasScores = img.aesthetic_laion != null || img.aesthetic_nima != null;
        if (hasScores) {
            title += '\nLAION: ' + (img.aesthetic_laion != null ? img.aesthetic_laion.toFixed(2) : '–')
                + '  NIMA: ' + (img.aesthetic_nima != null ? img.aesthetic_nima.toFixed(2) : '–');
        }
        const qb = AppState.images.getQualityBreakdown(img.id);
        if (qb) {
            const pct = v => (v * 100).toFixed(0);
            title += `\n\nQuality: ${pct(qb.total)}%`
                + `  (aesthetic ${pct(qb.aesthetic)}%`
                + `, sharpness ${pct(qb.sharpness)}%`
                + `, pixels ${pct(qb.pixels)}%`
                + `, bpp ${pct(qb.bpp)}%)`;
        }
        const thumb = App.createElement('img', {
            src: blobUrl,
            alt: img.basename,
            title,
        });

        // Basename label
        const label = App.createElement('span', { className: 'gallery-item-label' }, img.basename);

        // Group hover button (opens group picker on click)
        const groupBtn = App.createElement('button', {
            className: 'gallery-item-group-btn',
            title: 'Manage groups',
        });
        groupBtn.innerHTML = App.icon('photo_prints', '\u{1F5C2}');
        groupBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            // Blur so the button loses :focus and its opacity returns to 0
            groupBtn.blur();
            const selectedIds = App.getSelectedImages();
            const imageIds = selectedIds.length > 0 && selectedIds.includes(img.id)
                ? selectedIds : [img.id];
            this._openGroupPicker(imageIds);
        });

        item.appendChild(thumb);

        // Enhanced-version badge: a small sparkle marking a derived image, with
        // the processing depth when it has been enhanced more than once.
        if (img.derived_from) {
            const depth = img.processing_depth || 1;
            const enhBadge = App.createElement('span', { className: 'gallery-enhanced-badge' });
            enhBadge.innerHTML = App.icon('auto_fix_high', '✨');
            enhBadge.title = depth > 1 ? `Enhanced version (×${depth})` : 'Enhanced version';
            if (depth > 1) {
                const count = App.createElement('span', { className: 'gallery-enhanced-depth' });
                count.textContent = depth;
                enhBadge.appendChild(count);
            }
            item.appendChild(enhBadge);
        }

        // Video overlays: duration badge and play icon
        if (img.media_type === 'video') {
            item.classList.add('gallery-item-video');
            if (img.duration != null) {
                const badge = App.createElement('span', { className: 'video-duration-badge' });
                const mins = Math.floor(img.duration / 60);
                const secs = Math.floor(img.duration % 60);
                badge.textContent = mins + ':' + String(secs).padStart(2, '0');
                item.appendChild(badge);
            }
            const playOverlay = App.createElement('div', { className: 'video-play-overlay' });
            playOverlay.innerHTML = App.icon('play_arrow', '▶');
            item.appendChild(playOverlay);
            // Codec compatibility warning badge (informational only —
            // transcode action is on the Videos screen)
            if (typeof isCodecBrowserCompatible === 'function'
                && !isCodecBrowserCompatible(img.codec_video, img.codec_audio)) {
                const warnBadge = App.createElement('span', { className: 'video-compat-badge' });
                warnBadge.innerHTML = App.icon('warning', '\u26A0');
                warnBadge.title = 'This video may not play correctly in the browser. Use the Videos screen to transcode.';
                item.appendChild(warnBadge);
            }
        }

        item.appendChild(label);
        item.appendChild(groupBtn);

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

    /**
     * Initialise info panel collapse behaviour.
     * Binds the toggle button, subscribes to state changes, handles auto-collapse
     * when the info panel would take more than 20% of the viewport width.
     * @private
     */
    _initInfoPanelCollapse() {
        const { btnToggleInfo } = this._els;
        if (!btnToggleInfo) return;

        // Click handler — delegates to AppState.view (marks preference as user-set)
        btnToggleInfo.addEventListener('click', () => AppState.view.toggleInfoPanel());

        // Apply initial state from stored preference
        this._applyInfoPanelCollapsed(AppState.view.isInfoPanelCollapsed());

        // Subscribe to state changes
        this._unsubs.push(AppState.view.onChanged((event) => {
            if (event.property === 'infoPanelCollapsed') {
                this._applyInfoPanelCollapsed(AppState.view.isInfoPanelCollapsed());
                // Let CSS reflow, then recalculate the virtual grid layout
                setTimeout(() => this._grid._onResize(), 0);
            }
        }));

        // When a video's preferred scene changes, invalidate its gallery
        // thumbnail so VirtualGrid re-fetches the new scene thumbnail URL.
        this._unsubs.push(AppState.videos.onChanged((event) => {
            if (event.property === 'preferredScene' && event.imageId && this._grid) {
                this._grid.invalidateItem(event.imageId);
            }
        }));

        // Auto-collapse: if info panel (300px) would take >20% of viewport width
        // and the user hasn't explicitly toggled the preference
        this._autoCollapseInfoPanel();
        this._resizeAutoCollapse = () => this._autoCollapseInfoPanel();
        window.addEventListener('resize', this._resizeAutoCollapse);
    },

    /**
     * Apply the collapsed/expanded visual state to the info panel and toggle button.
     * @param {boolean} collapsed - Whether the panel should be collapsed
     * @private
     */
    _applyInfoPanelCollapsed(collapsed) {
        const { infoPanel, galleryContainer } = this._els;
        if (infoPanel) {
            infoPanel.classList.toggle('collapsed', collapsed);
        }
        if (galleryContainer) {
            galleryContainer.classList.toggle('panel-collapsed', collapsed);
        }
    },

    /**
     * Auto-collapse the info panel if it would take >20% of the viewport width.
     * Only applies when the user hasn't explicitly set a preference.
     * @private
     */
    _autoCollapseInfoPanel() {
        if (AppState.view.isInfoPanelUserSet()) return;
        const infoPanelWidth = 300;
        const shouldCollapse = infoPanelWidth > window.innerWidth * 0.2;
        AppState.view.setInfoPanelCollapsed(shouldCollapse);
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

        // Guard against stale responses: if the user clicked a different image
        // while the fetch was in flight, discard this response
        if (this._infoPanelImageId !== imageId) return;

        if (!img) {
            content.innerHTML = '<p class="info-placeholder">Image not found</p>';
            return;
        }

        // In headless mode, the path is not clickable (no file manager to reveal in)
        const pathClass = App.isHeadless() ? 'info-path' : 'info-path info-path-clickable';
        const pathTitle = App.isHeadless() ? '' : 'title="Open containing folder"';
        const pathDataId = App.isHeadless() ? '' : `data-image-id="${img.id}"`;

        const isVideo = img.media_type === 'video';

        // Format video duration as M:SS
        let durationStr = '';
        if (isVideo && img.duration != null) {
            const mins = Math.floor(img.duration / 60);
            const secs = Math.floor(img.duration % 60);
            durationStr = mins + ':' + String(secs).padStart(2, '0');
        }

        // Derived-version lineage: shown only for enhanced images (derived_from set).
        // The original may itself have been renamed, so fall back gracefully.
        let derivedHtml = '';
        if (img.derived_from) {
            const original = AppState.images.getById(img.derived_from);
            const originalName = original ? original.basename : 'the original';
            const depth = img.processing_depth || 1;
            derivedHtml = `
            <div class="info-section">
                <div class="info-row">
                    <span class="info-label">Version</span>
                    <span class="info-value">Enhanced${depth > 1 ? ` (×${depth})` : ''}</span>
                </div>
                <div class="info-row${original ? ' info-row-clickable' : ''}" ${original ? `id="info-derived-from-btn" data-original-id="${App.escapeHtml(img.derived_from)}" title="Show the original"` : ''}>
                    <span class="info-label">Derived from</span>
                    <span class="info-value${original ? ' info-link' : ''}">${App.escapeHtml(originalName)}${original ? ' →' : ''}</span>
                </div>
            </div>`;
        }

        content.innerHTML = `
            <div class="info-section">
                <p class="info-filename">${App.escapeHtml(img.basename)}<button class="info-copy-btn" title="Copy full path to clipboard" data-copy="${App.escapeHtml(img.path)}"><span class="icon" data-icon="content_copy">\u{1F4CB}</span></button></p>
                <p class="${pathClass}" ${pathTitle} ${pathDataId}>${App.escapeHtml(img.path)}</p>
            </div>
            ${derivedHtml}

            <div class="info-section">
                ${isVideo ? `
                <div class="info-row">
                    <span class="info-label">Duration</span>
                    <span class="info-value">${durationStr}</span>
                </div>
                <div class="info-row">
                    <span class="info-label">Resolution</span>
                    <span class="info-value">${App.formatDimensions(img.width, img.height)}</span>
                </div>
                ${img.codec_video ? `<div class="info-row">
                    <span class="info-label">Video codec</span>
                    <span class="info-value">${App.escapeHtml(img.codec_video)}</span>
                </div>` : ''}
                ${img.codec_audio != null ? `<div class="info-row">
                    <span class="info-label">Audio codec</span>
                    <span class="info-value">${App.escapeHtml(img.codec_audio || 'None')}${
                        img.codec_audio && typeof isCodecBrowserCompatible === 'function'
                            && !isCodecBrowserCompatible(null, img.codec_audio)
                            ? ' <span class="info-warning" title="Not supported by browsers">\u26A0</span>' : ''
                    }</span>
                </div>` : ''}
                ` : `
                <div class="info-row">
                    <span class="info-label">Dimensions</span>
                    <span class="info-value">${App.formatDimensions(img.width, img.height)}</span>
                </div>
                `}
                <div class="info-row">
                    <span class="info-label">File size</span>
                    <span class="info-value">${App.formatFileSize(img.size)}</span>
                </div>
                ${!isVideo ? `
                <div class="info-row info-row-clickable" id="info-metadata-btn" title="View full image metadata" data-image-id="${img.id}">
                    <span class="info-label">Metadata</span>
                    <span class="info-value info-link">View EXIF data \u2192</span>
                </div>
                ` : ''}
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
                        ${App.icon('auto_awesome', '\u2728')}
                    </button>
                </div>
                <textarea id="info-description" class="info-input" rows="3">${App.escapeHtml(img.description || '')}</textarea>
            </div>

            <div class="info-section info-editable">
                <label class="info-label" for="info-rating">Rating</label>
                <div class="info-rating-row">
                    <input type="text" id="info-rating" class="info-input" value="${App.escapeHtml(img.rating || '')}">
                    <button type="button" id="info-emoji-btn" class="toolbar-btn" title="Add emoji">
                        ${App.icon('add_reaction', '\u{1F44D}')}
                    </button>
                </div>
            </div>

            ${!isVideo ? `
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
            ` : ''}
        `;

        // Upgrade Unicode fallback icons to Material Symbols if font is loaded
        if (document.fonts?.check('24px "Material Symbols Outlined"')) {
            content.querySelectorAll('.icon[data-icon]').forEach(el => {
                el.className = 'material-symbols-outlined';
                el.textContent = el.dataset.icon;
            });
        }

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

        // Metadata button — opens read-only EXIF dialog
        const metadataBtn = App.$('info-metadata-btn');
        if (metadataBtn) {
            metadataBtn.addEventListener('click', () => {
                this._showMetadataDialog(imageId, 'readonly');
            });
        }

        // "Derived from" → select the original image (updates the info panel to it)
        const derivedBtn = App.$('info-derived-from-btn');
        if (derivedBtn) {
            derivedBtn.addEventListener('click', () => {
                const originalId = derivedBtn.dataset.originalId;
                if (originalId && AppState.images.getById(originalId)) {
                    AppState.selection.set('gallery', [originalId]);
                }
            });
        }

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

    /* ------------------------------------------------------------------
       METADATA DIALOG

       Shared dialog for viewing EXIF data (read-only mode from Gallery)
       and selecting metadata filter criteria (writable mode from Search).
       In read-only mode, filter icons let users pick values to filter by.
       ------------------------------------------------------------------ */

    /**
     * Shows the metadata dialog for an image.
     * In 'readonly' mode, displays EXIF key-value pairs with filter icons.
     * In 'writable' mode, shows input fields for search criteria.
     * @param {string} imageId - Image ID to show metadata for
     * @param {'readonly'|'writable'} mode - Dialog mode
     * @param {Object} [prefill] - Pre-filled values for writable mode {key: value}
     * @private
     */
    async _showMetadataDialog(imageId, mode = 'readonly', prefill = null) {
        const dialog = App.$('dialog-metadata');
        const title = App.$('dialog-metadata-title');
        const body = App.$('dialog-metadata-body');
        const actions = App.$('dialog-metadata-actions');
        if (!dialog || !body) return;

        title.textContent = mode === 'writable' ? 'Metadata Filter' : 'Image Metadata';
        body.innerHTML = '<p class="metadata-empty">Loading\u2026</p>';

        // Track selected filter criteria in read-only mode
        const selectedFilters = new Map();

        // Lazy-load EXIF data from dedicated endpoint (not included in image cache)
        let exifData = null;
        try {
            exifData = await AppState.images.fetchExifData(imageId);
        } catch (e) {
            console.error('Failed to load image metadata:', e);
        }

        if (!exifData || Object.keys(exifData).length === 0) {
            body.innerHTML = '<p class="metadata-empty">No EXIF metadata found for this image.</p>';
        } else if (mode === 'readonly') {
            this._renderReadonlyMetadata(body, exifData, selectedFilters);
        }

        // Render action buttons
        this._renderMetadataActions(dialog, actions, mode, selectedFilters);

        // Keyboard: Enter triggers the primary action (Close or Done),
        // Escape closes/cancels (handled natively by <dialog>)
        const keyHandler = (e) => {
            if (e.key === 'Enter' && !e.target.matches('input, textarea')) {
                e.preventDefault();
                const primary = actions.querySelector('.action-btn.primary');
                if (primary) primary.click();
            }
        };
        dialog.addEventListener('keydown', keyHandler);
        dialog.addEventListener('close', () => {
            dialog.removeEventListener('keydown', keyHandler);
        }, { once: true });

        dialog.showModal();
    },

    /**
     * Renders read-only metadata rows with filter-from-example buttons.
     * @param {HTMLElement} container - Body element to render into
     * @param {Object} exifData - Key-value EXIF data
     * @param {Map} selectedFilters - Map to track selected filter criteria
     * @private
     */
    _renderReadonlyMetadata(container, exifData, selectedFilters) {
        container.innerHTML = '';

        // Preferred display order — keys not in this list appear at the end
        const keyOrder = [
            'Camera', 'Lens', 'Focal Length', 'Aperture', 'Shutter Speed',
            'ISO', 'Exposure Comp', 'Exposure Program', 'Metering', 'Flash',
            'White Balance', 'Color Space', 'Software', 'Artist', 'Copyright',
            'GPS', 'Date Taken',
        ];

        // Sort keys: ordered keys first, then any extras alphabetically
        const allKeys = Object.keys(exifData);
        const ordered = keyOrder.filter(k => allKeys.includes(k));
        const extras = allKeys.filter(k => !keyOrder.includes(k)).sort();
        const sortedKeys = [...ordered, ...extras];

        for (const key of sortedKeys) {
            const value = exifData[key];
            const row = document.createElement('div');
            row.className = 'metadata-row';

            const keyEl = document.createElement('span');
            keyEl.className = 'metadata-key';
            keyEl.textContent = key;
            row.appendChild(keyEl);

            const valueEl = document.createElement('span');
            valueEl.className = 'metadata-value';
            valueEl.textContent = value;
            row.appendChild(valueEl);

            // Filter icon button (skip for Date Taken — use the date filter instead)
            if (key !== 'Date Taken') {
                const filterBtn = document.createElement('button');
                filterBtn.className = 'metadata-filter-btn';
                filterBtn.title = 'Add to search filter';
                filterBtn.innerHTML = App.icon('filter_alt', '\u2767');

                filterBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    if (selectedFilters.has(key)) {
                        selectedFilters.delete(key);
                        row.classList.remove('metadata-selected');
                    } else {
                        selectedFilters.set(key, value);
                        row.classList.add('metadata-selected');
                    }
                    // Update footer buttons based on selection state
                    this._renderMetadataActions(
                        App.$('dialog-metadata'),
                        App.$('dialog-metadata-actions'),
                        'readonly',
                        selectedFilters,
                    );
                });

                row.appendChild(filterBtn);
            }

            container.appendChild(row);
        }
    },

    /**
     * Renders the action buttons for the metadata dialog.
     * In read-only mode: "Close" when nothing selected, "Cancel"+"Done" when filters selected.
     * In writable mode: always "Cancel"+"Done".
     * @param {HTMLDialogElement} dialog
     * @param {HTMLElement} actions - Actions container
     * @param {'readonly'|'writable'} mode
     * @param {Map} selectedFilters
     * @private
     */
    _renderMetadataActions(dialog, actions, mode, selectedFilters) {
        actions.innerHTML = '';

        if (mode === 'writable' || (mode === 'readonly' && selectedFilters.size > 0)) {
            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'action-btn';
            cancelBtn.textContent = 'Cancel';
            cancelBtn.addEventListener('click', () => dialog.close());

            const doneBtn = document.createElement('button');
            doneBtn.className = 'action-btn primary';
            doneBtn.textContent = 'Done';
            doneBtn.addEventListener('click', () => {
                dialog.close();
                if (mode === 'readonly' && selectedFilters.size > 0) {
                    // Navigate to Search with pre-filled metadata chips
                    this._applyMetadataFilters(selectedFilters);
                }
            });

            actions.appendChild(cancelBtn);
            actions.appendChild(doneBtn);
        } else {
            const closeBtn = document.createElement('button');
            closeBtn.className = 'action-btn primary';
            closeBtn.textContent = 'Close';
            closeBtn.addEventListener('click', () => dialog.close());
            actions.appendChild(closeBtn);
        }
    },

    /**
     * Applies selected metadata filters by navigating to Search screen.
     * Sets metadata criteria on the filter state and renders chips.
     * @param {Map} selectedFilters - Map of {key: value} pairs
     * @private
     */
    _applyMetadataFilters(selectedFilters) {
        // Convert Map to plain object
        const metadata = {};
        for (const [key, value] of selectedFilters) {
            metadata[key] = value;
        }

        // Navigate to Search screen with metadata pre-filled
        App.showSearch();

        // Set metadata on the Search module (deferred to allow screen to render)
        requestAnimationFrame(() => {
            const searchModule = App.getModule('search');
            if (searchModule && searchModule.setMetadataFilters) {
                searchModule.setMetadataFilters(metadata);
            }
        });
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

            // Videos don't have histograms — skip the request
            const img = AppState.images.getById(imageId);
            if (img?.media_type === 'video') return;

            const container = App.$('histogram-container');
            const loading = container?.querySelector('.histogram-loading');

            try {
                const response = await App.apiGet(`/images/${imageId}/histogram`);

                // Guard against stale response if user switched images during fetch
                if (this._infoPanelImageId !== imageId) return;

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
                // Consume lastViewedImageId so onEnter doesn't re-apply it —
                // this subscription already handled selection + scroll for
                // the Gallery-initiated fullscreen path
                AppState.nav.consumeLastViewedImageId();

                // Fullscreen is closing - check if we need to refresh
                if (this.state.needsRefresh) {
                    // Images were modified while fullscreen was open - do full refresh
                    this._loadImages();
                } else if (event.imageId && this._grid) {
                    // Just scroll to the last viewed image
                    this._grid.scrollToId(event.imageId);
                }

                // Catch-up info panel update — selection was tracked during
                // fullscreen but info panel updates were deferred (see
                // _onSelectionChanged).  Force a fresh render now.
                this._infoPanelImageId = null;
                this._updateInfoPanel(this._selection.getSelected());
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

        // Guard: trash must be enabled
        if (!AppState.status.isTrashEnabled()) {
            App.showError(
                'Cannot delete: trash directory is misconfigured. '
                + 'Check that it does not overlap an indexed folder.',
            );
            return;
        }

        const count = ids.length;
        const message = count === 1
            ? 'Move this image to the trash?'
            : `Move ${count} images to the trash?`;

        const confirmed = await App.confirm('Move to Trash', message, { okText: 'Move to Trash' });
        if (!confirmed) return;

        // Toast fires immediately — backend file moves may take a while
        const noun = count === 1 ? 'image' : 'images';
        App.showInfo(`Moving ${count} ${noun} to \u2018trash\u2019\u2026`);

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
        const isCustomGroup = isDupFilter && filter.sourceLevel === 5;

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

        // Show remove-from-group button only when viewing a custom group
        if (this._els.btnRemoveFromGroup) {
            this._els.btnRemoveFromGroup.hidden = !isCustomGroup;
            this._els.btnRemoveFromGroup.disabled = !isCustomGroup;
        }

        // Quality sort is always available (works on any set of images)
        const qualityBtn = document.getElementById('btn-sort-quality');
        if (qualityBtn) qualityBtn.hidden = false;
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
       GROUP PICKER DIALOG
       ---------------------------------------------------------------------- */

    /**
     * Initialises the group picker dialog event handlers.
     * @private
     */
    _initGroupPicker() {
        const dialog = document.getElementById('dialog-group-picker');
        if (!dialog) return;

        const doneBtn = App.$('dialog-group-done');
        const cancelBtn = App.$('dialog-group-cancel');
        const newBtn = App.$('group-picker-new');
        const searchInput = App.$('group-picker-search');

        if (cancelBtn) cancelBtn.addEventListener('click', () => this._closeGroupPicker(false));
        if (doneBtn) doneBtn.addEventListener('click', () => this._closeGroupPicker(true));
        if (newBtn) newBtn.addEventListener('click', () => this._onGroupPickerNew());

        if (searchInput) {
            searchInput.addEventListener('input', () => this._renderGroupPickerAvailable());
            searchInput.addEventListener('keydown', (e) => e.stopPropagation());
        }

        // Handle dialog keyboard shortcuts (Escape = cancel, Enter = done)
        dialog.addEventListener('cancel', (e) => {
            e.preventDefault();
            this._closeGroupPicker(false);
        });
        dialog.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.target.matches('input, textarea')) {
                e.preventDefault();
                this._closeGroupPicker(true);
            }
        });

        // Drag support for available panel
        const availableGrid = App.$('group-picker-available');
        const selectedGrid = App.$('group-picker-selected');

        if (availableGrid && selectedGrid) {
            // Drop targets
            selectedGrid.addEventListener('dragover', (e) => {
                e.preventDefault();
                selectedGrid.classList.add('drag-over');
            });
            selectedGrid.addEventListener('dragleave', () => {
                selectedGrid.classList.remove('drag-over');
            });
            selectedGrid.addEventListener('drop', (e) => {
                e.preventDefault();
                selectedGrid.classList.remove('drag-over');
                const hash = e.dataTransfer.getData('text/plain');
                if (hash) this._groupPickerSelect(hash);
            });

            availableGrid.addEventListener('dragover', (e) => {
                e.preventDefault();
                availableGrid.classList.add('drag-over');
            });
            availableGrid.addEventListener('dragleave', () => {
                availableGrid.classList.remove('drag-over');
            });
            availableGrid.addEventListener('drop', (e) => {
                e.preventDefault();
                availableGrid.classList.remove('drag-over');
                const hash = e.dataTransfer.getData('text/plain');
                if (hash) this._groupPickerDeselect(hash);
            });
        }
    },

    /**
     * Opens the group picker for the given image IDs.
     * Loads level 4 groups, determines initial membership, and shows modal.
     * @param {string[]} imageIds - Image IDs to manage group membership for
     * @private
     */
    async _openGroupPicker(imageIds) {
        if (!imageIds || imageIds.length === 0) return;

        const dialog = document.getElementById('dialog-group-picker');
        if (!dialog) return;

        // Ensure level 5 (custom groups) is loaded
        await AppState.duplicates.loadLevel(5);

        // Store state for the picker session
        this._groupPickerState = {
            imageIds: imageIds,
            // Groups that ALL selected images belong to (for batch operations)
            originalGroups: new Set(
                AppState.duplicates.getGroupsForImages(imageIds).map(g => g.group_hash),
            ),
            selectedGroups: new Set(
                AppState.duplicates.getGroupsForImages(imageIds).map(g => g.group_hash),
            ),
        };

        // Update title
        const titleEl = App.$('group-picker-title');
        if (titleEl) {
            titleEl.textContent = imageIds.length > 1
                ? `Manage Groups (${imageIds.length} images)`
                : 'Manage Groups';
        }

        // Clear search
        const searchInput = App.$('group-picker-search');
        if (searchInput) searchInput.value = '';

        // Render panels
        this._renderGroupPickerAvailable();
        this._renderGroupPickerSelected();

        dialog.showModal();
    },

    /**
     * Closes the group picker, optionally saving changes.
     * @param {boolean} save - If true, persist group membership changes
     * @private
     */
    async _closeGroupPicker(save) {
        const dialog = document.getElementById('dialog-group-picker');
        if (!dialog) return;

        if (save && this._groupPickerState) {
            const { imageIds, originalGroups, selectedGroups } = this._groupPickerState;

            // Find groups added and removed
            const added = [...selectedGroups].filter(h => !originalGroups.has(h));
            const removed = [...originalGroups].filter(h => !selectedGroups.has(h));

            // Apply changes
            try {
                for (const hash of added) {
                    await AppState.duplicates.addImages(hash, imageIds);
                }
                for (const hash of removed) {
                    await AppState.duplicates.removeImages(hash, imageIds);
                }
            } catch (err) {
                App.showError('Failed to update group membership: ' + err.message);
            }
        }

        this._groupPickerState = null;
        dialog.close();
    },

    /**
     * Selects a group in the picker (moves from available to selected).
     * @param {string} groupHash - Group hash to select
     * @private
     */
    _groupPickerSelect(groupHash) {
        if (!this._groupPickerState) return;
        this._groupPickerState.selectedGroups.add(groupHash);
        this._renderGroupPickerAvailable();
        this._renderGroupPickerSelected();
    },

    /**
     * Deselects a group in the picker (moves from selected to available).
     * @param {string} groupHash - Group hash to deselect
     * @private
     */
    _groupPickerDeselect(groupHash) {
        if (!this._groupPickerState) return;
        this._groupPickerState.selectedGroups.delete(groupHash);
        this._renderGroupPickerAvailable();
        this._renderGroupPickerSelected();
    },

    /**
     * Renders the available (left) panel of the group picker.
     * Filters out groups that are already selected.
     * @private
     */
    _renderGroupPickerAvailable() {
        const grid = App.$('group-picker-available');
        if (!grid || !this._groupPickerState) return;

        // Exclude smart groups — can't add static images to a dynamic group
        const allGroups = AppState.duplicates.getStaticCustomGroups();
        const selectedHashes = this._groupPickerState.selectedGroups;
        const searchInput = App.$('group-picker-search');
        const filter = (searchInput?.value || '').toLowerCase().trim();

        // Filter: not selected, matches search
        const available = allGroups.filter(g => {
            if (selectedHashes.has(g.group_hash)) return false;
            if (filter && !(g.name || '').toLowerCase().includes(filter)) return false;
            return true;
        });

        grid.innerHTML = '';

        if (available.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'entity-picker-empty';
            empty.textContent = filter ? 'No groups match the search.' : 'No more groups available.';
            grid.appendChild(empty);
            return;
        }

        for (const group of available) {
            grid.appendChild(this._createGroupPickerItem(group, 'available'));
        }
    },

    /**
     * Renders the selected (right) panel of the group picker.
     * @private
     */
    _renderGroupPickerSelected() {
        const grid = App.$('group-picker-selected');
        if (!grid || !this._groupPickerState) return;

        // Exclude smart groups — can't add static images to a dynamic group
        const allGroups = AppState.duplicates.getStaticCustomGroups();
        const selectedHashes = this._groupPickerState.selectedGroups;

        const selected = allGroups.filter(g => selectedHashes.has(g.group_hash));

        grid.innerHTML = '';

        if (selected.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'entity-picker-empty';
            empty.textContent = 'Not a member of any group.';
            grid.appendChild(empty);
            return;
        }

        for (const group of selected) {
            grid.appendChild(this._createGroupPickerItem(group, 'selected'));
        }
    },

    /**
     * Creates a DOM element for a group in the picker.
     * @param {Object} group - Group data
     * @param {string} panel - 'available' or 'selected'
     * @returns {HTMLElement}
     * @private
     */
    _createGroupPickerItem(group, panel) {
        const item = document.createElement('div');
        item.className = 'entity-picker-item';
        item.draggable = true;
        item.dataset.hash = group.group_hash;

        // Thumbnail — falls back to app logo for empty groups
        const img = document.createElement('img');
        img.alt = group.name || 'Group';
        if (group.best_image?.id) {
            img.src = `/api/images/${group.best_image.id}/thumbnail?size=200`;
        } else {
            img.src = 'logo.png';
            img.classList.add('placeholder-logo');
        }
        item.appendChild(img);

        // Name
        const nameEl = document.createElement('div');
        nameEl.className = 'entity-picker-item-name';
        nameEl.textContent = group.name || 'Untitled';
        nameEl.title = group.name || 'Untitled';
        item.appendChild(nameEl);

        // Count
        const countEl = document.createElement('div');
        countEl.className = 'entity-picker-item-count';
        countEl.textContent = group.count === 1 ? '1 image' : `${group.count} images`;
        item.appendChild(countEl);

        // Click: toggle between panels
        item.addEventListener('click', () => {
            if (panel === 'available') {
                this._groupPickerSelect(group.group_hash);
            } else {
                this._groupPickerDeselect(group.group_hash);
            }
        });

        // Drag start
        item.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('text/plain', group.group_hash);
            e.dataTransfer.effectAllowed = 'move';
        });

        return item;
    },

    /**
     * Handles "New Group..." button in the picker dialog.
     * Prompts for a name, creates the group, adds selected images, and updates picker.
     * @private
     */
    async _onGroupPickerNew() {
        if (!this._groupPickerState) return;

        const name = await App.prompt('New Group', 'Enter a name for the new group:');
        if (!name || !name.trim()) return;

        try {
            const groupHash = await AppState.duplicates.createGroup(
                name.trim(),
                this._groupPickerState.imageIds,
            );
            // Add to selected set
            this._groupPickerState.selectedGroups.add(groupHash);
            // Also add to original so it doesn't get double-added on close
            this._groupPickerState.originalGroups.add(groupHash);
            // Re-render panels
            this._renderGroupPickerAvailable();
            this._renderGroupPickerSelected();
        } catch (err) {
            App.showError('Failed to create group: ' + err.message);
        }
    },

    /* ----------------------------------------------------------------------
       REMOVE FROM GROUP
       ---------------------------------------------------------------------- */

    /**
     * Initialises the remove-from-group button.
     * @private
     */
    _initRemoveFromGroup() {
        if (this._els.btnRemoveFromGroup) {
            this._els.btnRemoveFromGroup.addEventListener('click', () => this._removeFromGroup());
        }
    },

    /**
     * Removes selected images from the current custom group.
     * Only active when viewing a level-5 group in the gallery.
     * @private
     */
    async _removeFromGroup() {
        const filter = App.getFilter();
        if (!filter || filter.type !== 'duplicates' || filter.sourceLevel !== 5 || !filter.groupHash) return;

        const selectedIds = App.getSelectedImages();
        if (selectedIds.length === 0) return;

        // Grab group name before the optimistic update removes it
        const groupBefore = AppState.duplicates.getCustomGroups().find(
            g => g.group_hash === filter.groupHash);
        const groupName = groupBefore?.name || 'group';
        const count = selectedIds.length;

        try {
            await AppState.duplicates.removeImages(filter.groupHash, selectedIds);

            // Optimistic update is instant — show feedback immediately
            const noun = count === 1 ? 'image' : 'images';
            App.showInfo(`${count} ${noun} removed from \u2018${groupName}\u2019`);

            // Update the filter's image list to reflect removal
            const group = AppState.duplicates.getCustomGroups().find(g => g.group_hash === filter.groupHash);
            if (group && group.image_ids.length > 0) {
                // Update filter with remaining images
                App.setFilter({
                    ...filter,
                    imageIds: group.image_ids,
                });
            } else {
                // Group is now empty - return to groups screen
                App.clearFilter();
                App.navigateTo('duplicates');
            }
        } catch (err) {
            App.showError('Failed to remove from group: ' + err.message);
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
    },
};

// Register module with App
App.registerModule('gallery', Gallery);
