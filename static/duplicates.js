/**
 * @fileoverview Groups screen module for the Imaginary application.
 *
 * This module handles the Groups screen where users find and manage
 * duplicate or near-duplicate images, as well as user-curated custom groups
 * (albums). It registers with the core App module and provides a specialised
 * view for group management.
 *
 * Uses shared infrastructure from thumbnails.js:
 * - VirtualGrid: Virtual scrolling with absolute positioning
 * - GridSelection: Unified selection handling (click, keyboard, drag-box)
 * - ThumbnailLoader: Scroll-aware thumbnail fetching with distance-based priority
 *
 * RESPONSIBILITIES:
 *
 * Duplicate Detection Levels (0-3):
 *   The similarity slider controls the strictness of duplicate matching.
 *   Slider moves from loose (left) to strict (right), matching filter sliders:
 *   - Custom (leftmost): User-curated groups/albums
 *   - Related: Lower OpenCLIP similarity threshold
 *     (catches thematically related images)
 *   - Similar: High OpenCLIP embedding cosine similarity
 *     (catches shot sequences, similar compositions)
 *   - Near-identical: Same or very similar perceptual hash
 *     (catches rescaled images, different compression levels)
 *   - Identical (right): Same file size and SHA256 checksum
 *
 * Custom Groups (Level 4):
 *   - User-curated named collections (albums)
 *   - Images can belong to multiple groups (overlap allowed)
 *   - Groups persist even when empty
 *   - CRUD: create, rename, delete groups; add/remove images
 *   - Alphabetical sorting and name-based filtering
 *
 * Stack Display:
 *   - Shows duplicate groups as stacked thumbnail cards
 *   - Each stack shows the "best" image as the top thumbnail
 *   - Stack displays count of images in the group (e.g., "3 images")
 *   - Level 4 stacks show the group name as primary label
 *   - Stacks are sorted by group size (largest groups first) or alphabetically
 *   - Empty state message when no groups found at current level
 *
 * Best Image Selection:
 *   The "best" image in each group is determined by:
 *   1. Highest resolution (width * height)
 *   2. Best Laplacian variance score (most in focus)
 *   3. Lossless compression preferred over lossy
 *   This image appears on top of the stack and is pre-selected when
 *   viewing the group in Gallery.
 *
 * Stack Interaction:
 *   - Click to select stacks (single, Ctrl+click, Shift+click, drag-box)
 *   - Double-click stack opens Gallery filtered to show only that group
 *   - Gallery pre-selects the "best" image in the group
 *   - Returning from Gallery restores Groups scroll position
 *   - Thumbnail size controls (smaller/larger) adjust stack preview size
 *   - Keyboard navigation: arrows, Ctrl+A, Escape, Enter
 *
 * Dynamic Updates:
 *   - Changing similarity slider immediately recomputes and updates display
 *   - Backend provides pre-computed duplicate groups at each level
 *   - Smooth transition animation when groups appear/disappear
 *
 * Performance:
 *   - Duplicate groups are computed on backend during scan
 *   - Frontend caches group data for quick slider changes
 *   - Thumbnails loaded via ThumbnailLoader with distance-based priority
 *   - Virtual scrolling via VirtualGrid with absolute positioning
 *   - DOM elements created only after thumbnail blob URL is ready
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Fetches duplicate groups from backend, renders stacks
 *   - onLeave(): Saves scroll position for restoration on return
 *
 * @module duplicates
 * @requires core
 * @requires thumbnails
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE

   Duplicates module registration, state, and lifecycle hooks.
   ========================================================================== */

/**
 * Duplicates screen module.
 * @namespace
 */
const Duplicates = {
    /**
     * Similarity level labels for the slider (ordered loose to strict).
     * Index 0 = leftmost (loosest/custom), Index 4 = rightmost (strictest).
     * @type {string[]}
     * @constant
     */
    SIMILARITY_LABELS: ['Custom', 'Related', 'Similar', 'Near-identical', 'Identical'],

    /**
     * Converts slider position to similarity level.
     * Slider: 0 (left/custom) to 4 (right/strict)
     * Level: 4 (custom) to 0 (identical)
     * @param {number} sliderValue - Slider position (0-4)
     * @returns {number} Similarity level (0-4)
     * @private
     */
    _sliderToLevel(sliderValue) {
        return 4 - sliderValue;
    },

    /**
     * Converts similarity level to slider position.
     * @param {number} level - Similarity level (0-4)
     * @returns {number} Slider position (0-4)
     * @private
     */
    _levelToSlider(level) {
        return 4 - level;
    },

    /**
     * Local state for the duplicates screen.
     * Note: Group caching is handled by AppState.duplicates.
     * @type {Object}
     * @property {number} currentLevel - Current similarity level (0-4)
     * @property {Array<Object>} groups - Current duplicate groups for display (filtered)
     * @property {Array<Object>} allGroups - All groups before min size filtering
     * @property {string} currentStatus - Status of current level ('pending', 'computing', 'done')
     * @property {number} scrollTop - Saved scroll position
     * @property {boolean} needsRefresh - Whether data needs to reload
     * @property {string} sortMode - Current sort mode: 'size', 'semantic', 'people', or 'alpha'
     * @property {string} semanticQuery - Current semantic query for sorting
     * @property {number} minGroupSize - Minimum group size to display
     * @property {Array<string>} selectedGroups - Currently selected group hashes
     * @property {string} groupFilter - Current name filter for custom groups
     */
    state: {
        // Note: groupCache, statusCache, epochCache moved to AppState.duplicates
        currentLevel: 0,
        groups: [],           // Filtered/sorted groups for display
        allGroups: [],        // All groups from current level (before filtering)
        currentStatus: 'pending',
        scrollTop: 0,
        needsRefresh: true,
        sortMode: 'size',
        semanticQuery: '',
        minGroupSize: 2,
        selectedGroups: [],
        groupFilter: ''
    },

    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * VirtualGrid instance for rendering.
     * @type {Object|null}
     * @private
     */
    _grid: null,

    /**
     * GridSelection instance for selection handling.
     * @type {Object|null}
     * @private
     */
    _selection: null,

    /**
     * Initialises the duplicates module.
     * Called once during app startup.
     */
    init() {
        // Cache DOM elements
        this._els = {
            container: document.querySelector('.duplicates-container'),
            grid: App.$('duplicates-grid'),
            empty: App.$('duplicates-empty'),
            slider: App.$('similarity-slider'),
            sliderLabel: App.$('similarity-label'),
            btnSmaller: App.$('btn-dup-thumb-smaller'),
            btnLarger: App.$('btn-dup-thumb-larger'),
            btnSortSize: App.$('btn-dup-sort-size'),
            btnSortAlpha: App.$('btn-dup-sort-alpha'),
            btnSortSemantic: App.$('btn-dup-sort-semantic'),
            btnSortPeople: App.$('btn-dup-sort-people'),
            semanticQuery: App.$('dup-semantic-query'),
            minGroupSize: App.$('dup-min-group-size'),
            // Custom group controls
            btnGroupNew: App.$('btn-group-new'),
            btnGroupRename: App.$('btn-group-rename'),
            btnGroupDelete: App.$('btn-group-delete'),
            groupFilter: App.$('dup-group-filter'),
        };

        // Create VirtualGrid instance
        this._grid = VirtualGrid.create({
            container: this._els.grid,
            getItems: () => this.state.groups,
            getItemId: (group) => group.group_hash,
            createItem: (group, index, blobUrl) => this._createStackElement(group, index, blobUrl),
            getThumbnailId: (group) => group.best_image?.id,
            itemSelector: '.duplicate-stack',
            getItemHeight: (thumbSize, itemWidth) => {
                // Stack height: thumbnail (square) + count label + optional name label + padding
                const thumbnailHeight = itemWidth;
                const labelHeight = this.state.currentLevel === 4 ? 44 : 24;
                return thumbnailHeight + labelHeight + 16;
            },
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
            getItems: () => this.state.groups,
            getItemId: (group) => group.group_hash,
            itemSelector: '.duplicate-stack',
            onSelectionChanged: (hashes) => {
                this.state.selectedGroups = hashes;
                this._updateCustomGroupButtons();
            },
            onItemActivated: (hash) => this._openGroupInGallery(hash),
            // Delete key triggers group deletion at level 4
            enableDeleteKey: false,
            onDeleteKey: () => {
                if (this.state.currentLevel === 4 && this.state.selectedGroups.length > 0) {
                    this._onDeleteGroup();
                }
            }
        });

        // Bind events
        this._bindEvents();

        // Subscribe to relevant app events
        this._subscribeToEvents();

        // Keep duplicates grid thumbnail size in sync with the global thumbnail size
        this._applyThumbSize(App.getThumbnailSize());
    },

    /**
     * Called when entering the duplicates screen.
     * Fetches groups if needed and restores scroll position.
     */
    onEnter() {
        // Sync slider with current level (invert: level 0=strict, slider 4=strict)
        const sliderPos = this._levelToSlider(this.state.currentLevel);
        this._els.slider.value = sliderPos;
        this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[sliderPos];

        // Show/hide level-specific controls
        this._updateLevelUI(this.state.currentLevel);

        // Sync sort mode UI
        this._syncSortModeUI();

        // Sync min group size dropdown
        this._els.minGroupSize.value = String(this.state.minGroupSize);

        // Sync group filter
        if (this._els.groupFilter) {
            this._els.groupFilter.value = this.state.groupFilter;
        }

        // Bind selection handlers
        this._selection.bind();

        // Load data if needed
        if (this.state.needsRefresh) {
            this._loadGroups();
        } else {
            // Rebind grid and restore position
            this._grid.bind();
            this._els.grid.scrollTop = this.state.scrollTop;
            // Refresh visible items in case viewport changed
            this._grid.refresh();
            // Restore selection visual state from state.selectedGroups
            this._selection.setSelected(this.state.selectedGroups);
            // Scroll to selected group if there is one
            if (this.state.selectedGroups.length > 0) {
                this._grid.scrollToId(this.state.selectedGroups[0], 'instant');
            }
        }

        // Bind Escape key to return to gallery
        this._escapeHandler = (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                App.showGallery();
            }
        };
        document.addEventListener('keydown', this._escapeHandler);
    },

    /**
     * Called when leaving the duplicates screen.
     * Saves scroll position for restoration on return.
     */
    onLeave() {
        this.state.scrollTop = this._els.grid.scrollTop;
        this._grid.unbind();
        this._selection.unbind();

        // Stop AppState polling for duplicates (cleanup)
        AppState.duplicates.stopPolling();

        // Remove Escape key handler
        if (this._escapeHandler) {
            document.removeEventListener('keydown', this._escapeHandler);
            this._escapeHandler = null;
        }
    },

    /**
     * Binds event listeners for controls.
     * @private
     */
    _bindEvents() {
        // Similarity slider (inverted: left=loose/custom, right=strict/identical)
        this._els.slider.addEventListener('input', () => {
            const sliderPos = parseInt(this._els.slider.value, 10);
            const level = this._sliderToLevel(sliderPos);
            this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[sliderPos];
            this._setLevel(level);
        });

        // Add hover tooltip showing similarity level label
        App.addSliderHoverTooltip(this._els.slider, {
            suffix: '',
            formatValue: (value) => this.SIMILARITY_LABELS[value] || ''
        });

        // Sort mode buttons
        this._els.btnSortSize.addEventListener('click', () => this._setSortMode('size'));
        if (this._els.btnSortAlpha) {
            this._els.btnSortAlpha.addEventListener('click', () => this._setSortMode('alpha'));
        }
        this._els.btnSortSemantic.addEventListener('click', () => this._setSortMode('semantic'));
        if (this._els.btnSortPeople) {
            this._els.btnSortPeople.addEventListener('click', () => this._setSortMode('people'));
        }

        // Semantic query input - recompute on blur or Enter
        this._els.semanticQuery.addEventListener('blur', () => this._onSemanticQueryChange());
        this._els.semanticQuery.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this._els.semanticQuery.blur();
            }
        });

        // Min group size dropdown
        this._els.minGroupSize.addEventListener('change', () => this._onMinGroupSizeChange());

        // Custom group controls
        if (this._els.btnGroupNew) {
            this._els.btnGroupNew.addEventListener('click', () => this._onNewGroup());
        }
        if (this._els.btnGroupRename) {
            this._els.btnGroupRename.addEventListener('click', () => this._onRenameGroup());
        }
        if (this._els.btnGroupDelete) {
            this._els.btnGroupDelete.addEventListener('click', () => this._onDeleteGroup());
        }

        // Group name filter
        if (this._els.groupFilter) {
            this._els.groupFilter.addEventListener('input', () => this._onGroupFilterChanged());
            this._els.groupFilter.addEventListener('keydown', (e) => {
                // Prevent keyboard nav while typing
                e.stopPropagation();
            });
        }
    },

    /**
     * Subscribes to app-level events.
     * @private
     */
    _subscribeToEvents() {
        // Thumbnail size sync
        App.on('thumbnailSizeChanged', (size) => this._onThumbnailSizeChanged(size));

        // Subscribe to AppState.duplicates for centralized state management
        // Reactive: refresh when data changes
        AppState.duplicates.onChanged((event) => {
            // event.level is set explicitly by loadLevel(); CRUD mutations use
            // markDirty() which omits level, so treat missing level as matching
            const levelMatch = (event.level === undefined) || (event.level === this.state.currentLevel);

            if (App.getScreen() !== 'duplicates') {
                // Not viewing this screen — flag for refresh on next visit
                if (levelMatch) this.state.needsRefresh = true;
                return;
            }

            if (levelMatch) {
                // Data changed in AppState - copy for sorting and re-render
                // DESIGN: Local presentation state - we maintain a sorted copy because sort
                // criteria (user preference, search query) are local UI state that AppState
                // doesn't know about (see design-audit.md 2.2/2.4)
                const groups = AppState.duplicates.getGroups(this.state.currentLevel);
                const statusObj = AppState.duplicates.getStatus(this.state.currentLevel);

                this.state.allGroups = [...groups];  // Copy for sorting
                this.state.currentStatus = statusObj?.status || 'done';

                // Apply current sort order and render
                this._applySortOrder();

                // Update button state (selection may have changed due to reload)
                this._updateCustomGroupButtons();
            }
        });

        // Subscribe to AppState.images for database changes
        // When images change, duplicates may need to refresh
        AppState.images.onChanged(() => {
            if (App.getScreen() !== 'duplicates') {
                this.state.needsRefresh = true;
            }
            // Note: AppState.duplicates._internal.removeImage is called by images.delete()
            // so the duplicates cache is already updated
        });
    },

    /**
     * Handles thumbnail size changes.
     * Recalculates virtual scroll dimensions and re-renders visible items.
     * @param {number} sizePx - New thumbnail size in pixels
     * @private
     */
    _onThumbnailSizeChanged(sizePx) {
        this._applyThumbSize(sizePx);

        // Refresh grid if currently visible
        if (App.getScreen() === 'duplicates' && this.state.groups.length > 0) {
            this._grid.refresh();
        }
    },

    /**
     * Applies the global thumbnail size to the duplicates grid.
     * The duplicates layout uses the CSS custom property --thumb-size.
     * @param {number} sizePx
     */
    _applyThumbSize(sizePx) {
        if (!this._els || !this._els.grid) {
            return;
        }

        const n = Number(sizePx);
        if (!Number.isFinite(n)) {
            return;
        }

        // Keep within a sensible range to avoid breaking layout.
        const clamped = Math.max(60, Math.min(260, Math.round(n)));
        this._els.grid.style.setProperty('--thumb-size', `${clamped}px`);
    },

    /**
     * Marks the module as needing a refresh on next enter.
     * Called by other modules when data changes.
     */
    markNeedsRefresh() {
        this.state.needsRefresh = true;
        // Invalidate all levels in AppState cache
        for (let level = 0; level <= 4; level++) {
            AppState.duplicates.invalidate(level);
        }
        this.state.allGroups = [];
        this.state.groups = [];
    },

    /**
     * Shows or hides level-specific UI controls based on the current level.
     *
     * Level 4 (custom groups): shows New/Rename/Delete buttons, alphabetical sort,
     * group name filter. Hides min-group-size, size sort, semantic query.
     *
     * Levels 0-3 (auto): shows min-group-size, size sort, semantic query.
     * Hides custom group controls.
     *
     * @param {number} level - The current similarity level
     * @private
     */
    _updateLevelUI(level) {
        const isCustom = level === 4;
        const container = document.querySelector('[data-for-screen="duplicates"]');
        if (!container) return;

        // Toggle auto-level controls (min-group-size, size sort)
        container.querySelectorAll('.auto-level-control').forEach(el => {
            el.hidden = isCustom;
        });

        // Toggle custom-group controls (new/rename/delete, alpha sort, name filter)
        container.querySelectorAll('.custom-group-control').forEach(el => {
            el.hidden = !isCustom;
        });

        // Also toggle in the right-side toolbar
        const rightToolbar = document.querySelector('.toolbar-right [data-for-screen="duplicates"]');
        if (rightToolbar) {
            rightToolbar.querySelectorAll('.auto-level-control').forEach(el => {
                el.hidden = isCustom;
            });
            rightToolbar.querySelectorAll('.custom-group-control').forEach(el => {
                el.hidden = !isCustom;
            });
        }

        // Also toggle in the left toolbar
        const leftToolbar = document.querySelector('.toolbar-left [data-for-screen="duplicates"]');
        if (leftToolbar) {
            leftToolbar.querySelectorAll('.auto-level-control').forEach(el => {
                el.hidden = isCustom;
            });
            leftToolbar.querySelectorAll('.custom-group-control').forEach(el => {
                el.hidden = !isCustom;
            });
        }

        // Update custom group buttons based on selection state
        this._updateCustomGroupButtons();

        // Enable delete key only for level 4
        if (this._selection) {
            this._selection.enableDeleteKey = isCustom;
        }
    },

    /**
     * Updates the enabled/disabled state of custom group Rename and Delete buttons
     * based on the current selection.
     * @private
     */
    _updateCustomGroupButtons() {
        if (this.state.currentLevel !== 4) return;

        const count = this.state.selectedGroups.length;
        if (this._els.btnGroupRename) {
            this._els.btnGroupRename.disabled = count !== 1;
        }
        if (this._els.btnGroupDelete) {
            this._els.btnGroupDelete.disabled = count === 0;
        }
    },

    /**
     * Syncs the sort mode UI buttons to the current sort mode state.
     * @private
     */
    _syncSortModeUI() {
        this._els.btnSortSize.classList.toggle('active', this.state.sortMode === 'size');
        if (this._els.btnSortAlpha) {
            this._els.btnSortAlpha.classList.toggle('active', this.state.sortMode === 'alpha');
        }
        this._els.btnSortSemantic.classList.toggle('active', this.state.sortMode === 'semantic');
        if (this._els.btnSortPeople) {
            this._els.btnSortPeople.classList.toggle('active', this.state.sortMode === 'people');
        }
        this._els.semanticQuery.disabled = (this.state.sortMode !== 'semantic');
        this._els.semanticQuery.value = this.state.semanticQuery;
    }
};

/* ==========================================================================
   DATA LOADING & GROUP MANAGEMENT

   Fetching duplicate groups, caching, and similarity level changes.
   ========================================================================== */

/**
 * Loads duplicate groups from the backend for the current level.
 * Uses AppState cache if available, otherwise fetches from API.
 * AppState handles polling internally if computation is in progress.
 * @private
 */
Duplicates._loadGroups = async function() {
    this._showLoading('Loading groups\u2026');

    try {
        // Load via AppState (handles caching and polling internally)
        await AppState.duplicates.loadLevel(this.state.currentLevel);

        // Copy from AppState for local sorting (sort mutates)
        const groups = AppState.duplicates.getGroups(this.state.currentLevel);
        const statusObj = AppState.duplicates.getStatus(this.state.currentLevel);

        this.state.allGroups = [...groups];  // Copy for sorting
        this.state.currentStatus = statusObj?.status || 'done';
        this.state.needsRefresh = false;

        // Apply current sort mode (also applies min group size / name filter)
        await this._applySortOrder();
    } catch (err) {
        App.showError('Failed to load groups: ' + err.message);
    } finally {
        this._hideLoading();
    }
};

/**
 * Changes the similarity level and updates the display.
 * @param {number} level - New similarity level (0-4)
 * @private
 */
Duplicates._setLevel = async function(level) {
    if (level === this.state.currentLevel && this.state.allGroups.length > 0 && this.state.currentStatus === 'done') {
        return;
    }

    // Monotonic counter to detect stale loads when the user moves the slider
    // faster than the backend can respond (each call gets its own token)
    this._levelLoadSeq = (this._levelLoadSeq || 0) + 1;
    const mySeq = this._levelLoadSeq;

    this.state.currentLevel = level;
    AppState.duplicates.setCurrentLevel(level);

    // Clear selection when changing level
    this._selection.clear();

    // Update level-specific UI (show/hide controls)
    this._updateLevelUI(level);

    // Set appropriate default sort mode when switching to level 4
    if (level === 4 && this.state.sortMode === 'size') {
        this.state.sortMode = 'alpha';
        this._syncSortModeUI();
    } else if (level !== 4 && this.state.sortMode === 'alpha') {
        this.state.sortMode = 'size';
        this._syncSortModeUI();
    }

    this._showLoading('Loading groups\u2026');

    try {
        // Load via AppState (handles caching and polling internally)
        await AppState.duplicates.loadLevel(level);

        // If the user moved the slider while we were loading, discard this result
        if (mySeq !== this._levelLoadSeq) return;

        // Copy from AppState for local sorting (sort mutates)
        const groups = AppState.duplicates.getGroups(level);
        const statusObj = AppState.duplicates.getStatus(level);

        this.state.allGroups = [...groups];  // Copy for sorting
        this.state.currentStatus = statusObj?.status || 'done';

        // Apply current sort mode (also applies min group size / name filter)
        await this._applySortOrder();
    } catch (err) {
        // Only show error if this is still the active load
        if (mySeq === this._levelLoadSeq) {
            App.showError('Failed to load groups: ' + err.message);
        }
    } finally {
        if (mySeq === this._levelLoadSeq) {
            this._hideLoading();
        }
    }
};

/**
 * Sets the sort mode for duplicate groups.
 * @param {string} mode - 'size', 'alpha', 'semantic', or 'people'
 * @private
 */
Duplicates._setSortMode = function(mode) {
    if (mode === this.state.sortMode) return;

    this.state.sortMode = mode;
    this._syncSortModeUI();

    if (mode === 'semantic') {
        // Focus the input when switching to semantic mode
        this._els.semanticQuery.focus();
        // If there's already a query, apply it
        if (this.state.semanticQuery) {
            this._applySemanticSort();
        }
    } else if (mode === 'people') {
        // Sort by people names
        this._sortGroupsByPeople();
    } else if (mode === 'alpha') {
        // Sort alphabetically by name
        this._sortGroupsByAlpha();
        this._renderGroups();
    } else {
        // Sort by size (default)
        this._sortGroupsBySize();
        this._renderGroups();
    }
};

/**
 * Handles changes to the semantic query input.
 * @private
 */
Duplicates._onSemanticQueryChange = function() {
    const query = this._els.semanticQuery.value.trim();

    if (query === this.state.semanticQuery) return;

    this.state.semanticQuery = query;

    if (this.state.sortMode === 'semantic' && query) {
        this._applySemanticSort();
    } else if (!query) {
        // Empty query - fall back to size sort
        this._sortGroupsBySize();
        this._renderGroups();
    }
};

/**
 * Handles changes to the min group size dropdown.
 * @private
 */
Duplicates._onMinGroupSizeChange = function() {
    const size = parseInt(this._els.minGroupSize.value, 10);

    if (size === this.state.minGroupSize) return;

    this.state.minGroupSize = size;
    this._applyMinGroupSizeFilter();
    this._renderGroups();
};

/**
 * Applies the min group size filter to allGroups and stores result in groups.
 * Only applies for levels 0-3. Level 4 uses name filter instead.
 * @private
 */
Duplicates._applyMinGroupSizeFilter = function() {
    if (this.state.currentLevel === 4) {
        this._applyGroupNameFilter();
        return;
    }
    const minSize = this.state.minGroupSize;
    this.state.groups = this.state.allGroups.filter(g => g.count >= minSize);
};

/**
 * Applies the group name filter for level 4 custom groups.
 * Uses case-insensitive substring matching.
 * @private
 */
Duplicates._applyGroupNameFilter = function() {
    const filter = this.state.groupFilter.toLowerCase().trim();
    if (!filter) {
        this.state.groups = [...this.state.allGroups];
        return;
    }
    this.state.groups = this.state.allGroups.filter(g => {
        const name = (g.name || '').toLowerCase();
        return name.includes(filter);
    });
};

/**
 * Handles changes to the group name filter input.
 * @private
 */
Duplicates._onGroupFilterChanged = function() {
    this.state.groupFilter = this._els.groupFilter.value;
    this._applyGroupNameFilter();
    this._renderGroups();
};

/**
 * Sorts groups by size (count) in descending order.
 * Sorts allGroups and re-applies min size filter.
 * DESIGN: Sorts local copy, not AppState (see design-audit.md 2.2)
 * @private
 */
Duplicates._sortGroupsBySize = function() {
    this.state.allGroups.sort((a, b) => b.count - a.count);
    this._applyMinGroupSizeFilter();
};

/**
 * Sorts groups alphabetically by name.
 * Used for level 4 custom groups.
 * @private
 */
Duplicates._sortGroupsByAlpha = function() {
    this.state.allGroups.sort((a, b) => {
        const nameA = a.name || '';
        const nameB = b.name || '';
        return nameA.localeCompare(nameB, undefined, { sensitivity: 'base' });
    });
    this._applyGroupNameFilter();
};

/**
 * Sorts groups by people names in alphabetical order.
 * Groups with people come first, sorted by names string.
 * Groups without people come last, sorted by size.
 * DESIGN: Sorts local copy, not AppState (see design-audit.md 2.2)
 * @private
 */
Duplicates._sortGroupsByPeople = async function() {
    if (this.state.allGroups.length === 0) {
        this._renderGroups();
        return;
    }

    // Collect all unique image IDs from best_image
    const imageIds = this.state.allGroups
        .map(g => g.best_image?.id)
        .filter(id => id);

    // Fetch faces for all images in a single batch request
    let facesByImage = new Map();
    if (imageIds.length > 0) {
        try {
            facesByImage = await AppState.faces.fetchForImages(imageIds);
        } catch (error) {
            console.warn('[Duplicates._sortGroupsByPeople] Batch fetch failed:', error);
        }
    }

    // Build people names map from batch results
    const peopleNames = {};
    for (const group of this.state.allGroups) {
        if (!group.best_image?.id) {
            peopleNames[group.group_hash] = '';
            continue;
        }
        const faces = facesByImage.get(group.best_image.id) || [];
        const names = faces
            .filter(f => f.person_name)
            .map(f => f.person_name)
            .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));
        const uniqueNames = [...new Set(names)];
        peopleNames[group.group_hash] = uniqueNames.join(', ');
    }

    // Sort groups: those with people first (alphabetically), then others by size
    this.state.allGroups.sort((a, b) => {
        const namesA = peopleNames[a.group_hash] || '';
        const namesB = peopleNames[b.group_hash] || '';

        // Groups with people come first
        if (namesA && !namesB) return -1;
        if (!namesA && namesB) return 1;

        // Both have people - sort alphabetically
        if (namesA && namesB) {
            return namesA.localeCompare(namesB, undefined, { sensitivity: 'base' });
        }

        // Neither have people - sort by size
        return b.count - a.count;
    });

    this._applyMinGroupSizeFilter();
    this._renderGroups();
};

/**
 * Applies the current sort order and re-renders.
 * @private
 */
Duplicates._applySortOrder = async function() {
    if (this.state.sortMode === 'semantic' && this.state.semanticQuery) {
        await this._applySemanticSort();
    } else if (this.state.sortMode === 'people') {
        await this._sortGroupsByPeople();
    } else if (this.state.sortMode === 'alpha') {
        this._sortGroupsByAlpha();
        this._renderGroups();
    } else {
        this._sortGroupsBySize();
        this._renderGroups();
    }
};

/**
 * Applies semantic sorting based on the current query.
 * Fetches similarity scores from the backend and reorders groups.
 * Sorts allGroups and re-applies min size filter.
 * DESIGN: Sorts local copy, not AppState (see design-audit.md 2.2)
 * @private
 */
Duplicates._applySemanticSort = async function() {
    const query = this.state.semanticQuery;
    if (!query || this.state.allGroups.length === 0) return;

    // Get the best image ID from each group for similarity comparison
    const groupImageIds = this.state.allGroups.map(g => ({
        group_hash: g.group_hash,
        image_id: g.best_image?.id
    })).filter(g => g.image_id);

    if (groupImageIds.length === 0) return;

    try {
        this._showLoading('Sorting by similarity\u2026');

        // Call backend to get similarity scores via AppState
        const scores = await AppState.duplicates.sortSemantic(
            query,
            groupImageIds.map(g => g.image_id)
        );

        if (scores && scores.length > 0) {
            // Create a map of image_id -> score
            const scoreMap = new Map(
                scores.map(s => [s.image_id, s.score])
            );

            // Sort allGroups by score (descending)
            this.state.allGroups.sort((a, b) => {
                const scoreA = scoreMap.get(a.best_image?.id) || 0;
                const scoreB = scoreMap.get(b.best_image?.id) || 0;
                return scoreB - scoreA;
            });

            // Re-apply min size filter and render
            this._applyMinGroupSizeFilter();
            this._renderGroups();
        }
    } catch (err) {
        App.showError('Failed to sort by similarity: ' + err.message);
    } finally {
        this._hideLoading();
    }
};

/* ==========================================================================
   CUSTOM GROUP ACTIONS (Level 4)

   Create, rename, delete custom groups.
   ========================================================================== */

/**
 * Creates a new custom group.
 * Prompts for a name, then creates via AppState.
 * @private
 */
Duplicates._onNewGroup = async function() {
    const name = await App.prompt('New Group', 'Enter a name for the new group:');
    if (!name || !name.trim()) return;

    try {
        await AppState.duplicates.createGroup(name.trim());
    } catch (err) {
        App.showError('Failed to create group: ' + err.message);
    }
};

/**
 * Renames the selected custom group.
 * Requires exactly one group selected.
 * @private
 */
Duplicates._onRenameGroup = async function() {
    if (this.state.selectedGroups.length !== 1) return;

    const hash = this.state.selectedGroups[0];
    const groups = this.state.allGroups;
    const group = groups.find(g => g.group_hash === hash);
    if (!group) return;

    const name = await App.prompt('Rename Group', 'Enter a new name:', group.name || '');
    if (name === null || !name.trim()) return;

    try {
        await AppState.duplicates.renameGroup(hash, name.trim());
    } catch (err) {
        App.showError('Failed to rename group: ' + err.message);
    }
};

/**
 * Deletes the selected custom group(s).
 * Shows a danger confirmation dialog.
 * @private
 */
Duplicates._onDeleteGroup = async function() {
    const hashes = this.state.selectedGroups;
    if (hashes.length === 0) return;

    const plural = hashes.length > 1 ? `${hashes.length} groups` : 'this group';
    const confirmed = await App.confirm(
        'Delete Group',
        `Are you sure you want to delete ${plural}? The images will not be affected.`,
        { danger: true, okText: 'Delete' }
    );

    if (!confirmed) return;

    try {
        for (const hash of hashes) {
            await AppState.duplicates.deleteGroup(hash);
        }
        this._selection.clear();
    } catch (err) {
        App.showError('Failed to delete group: ' + err.message);
    }
};

/* ==========================================================================
   RENDERING & DISPLAY

   Stack grid rendering, thumbnails, and visual updates.
   ========================================================================== */

/**
 * Renders the current duplicate groups using VirtualGrid.
 * @private
 */
Duplicates._renderGroups = function() {
    const grid = this._els.grid;
    const empty = this._els.empty;

    const level = this.state.currentLevel;
    const status = this.state.currentStatus;
    const sliderPos = this._levelToSlider(level);
    const levelLabel = this.SIMILARITY_LABELS[sliderPos].toLowerCase();

    // Show empty/status state if no groups
    if (this.state.groups.length === 0) {
        grid.hidden = true;
        empty.hidden = false;

        const p = empty.querySelector('p');
        if (p) {
            if (level === 4) {
                // Custom groups empty state
                if (this.state.groupFilter) {
                    p.textContent = 'No groups match the current filter.';
                } else {
                    p.textContent = 'No custom groups yet. Select images in the Gallery and use the group button to create one.';
                }
            } else if (status === 'computing') {
                p.textContent = `Computing ${levelLabel} duplicates\u2026 This may take a while for large collections.`;
            } else if (status === 'pending') {
                p.textContent = `Waiting to compute ${levelLabel} duplicates\u2026`;
            } else {
                p.textContent = `No ${levelLabel} duplicates found.`;
            }
        }
        return;
    }

    // Hide empty state, show grid
    empty.hidden = true;
    grid.hidden = false;

    // Update grid CSS for thumbnail size
    this._applyThumbSize(App.getThumbnailSize());

    // Render via VirtualGrid
    this._grid.render();

    // Bind selection handlers (ensures handlers are attached after render)
    this._selection.bind();
};

/**
 * Creates a stack element for a duplicate group with thumbnail already loaded.
 * For level 4 (custom groups), shows the group name as primary label and count below.
 * For levels 0-3, shows the count as the primary label.
 * @param {Object} group - The duplicate group (lightweight format)
 * @param {number} index - Group index for data attribute
 * @param {string} blobUrl - Blob URL for the thumbnail
 * @returns {HTMLElement} The stack element
 * @private
 */
Duplicates._createStackElement = function(group, index, blobUrl) {
    const stack = document.createElement('div');
    stack.className = 'duplicate-stack loaded';
    stack.dataset.groupIndex = index;
    stack.dataset.groupHash = group.group_hash;
    stack.title = 'Double-click to view this stack in the Gallery';

    // Best image preview (thumbnail) with blob URL already set
    const img = document.createElement('img');
    if (blobUrl) {
        img.src = blobUrl;
    }
    img.alt = group.name || group.best_image?.basename || 'Group preview';
    img.dataset.imageId = group.best_image?.id || '';
    stack.appendChild(img);

    if (this.state.currentLevel === 4) {
        // Custom groups: show name as primary label, count as subtitle
        const nameEl = document.createElement('div');
        nameEl.className = 'duplicate-stack-name';
        nameEl.textContent = group.name || 'Untitled';
        nameEl.title = group.name || 'Untitled';
        stack.appendChild(nameEl);

        const countEl = document.createElement('div');
        countEl.className = 'duplicate-stack-count-sub';
        countEl.textContent = group.count === 1 ? '1 image' : `${group.count} images`;
        stack.appendChild(countEl);
    } else {
        // Auto levels: show count label
        const count = document.createElement('div');
        count.className = 'duplicate-stack-count';
        count.textContent = `${group.count} images`;
        stack.appendChild(count);
    }

    return stack;
};

/* ==========================================================================
   STACK INTERACTION

   Double-click handling and navigation to gallery.
   ========================================================================== */

/**
 * Opens a duplicate group in the gallery.
 * Called when a stack is activated (double-click or Enter key).
 * @param {string} hash - The group hash to open
 * @private
 */
Duplicates._openGroupInGallery = function(hash) {
    // Save scroll position before leaving
    this.state.scrollTop = this._els.grid.scrollTop;

    // Use shared navigation logic
    if (this.navigateToGroup(hash)) {
        App.showGallery();
    }
};

/**
 * Gets the current list of duplicate groups (after filtering/sorting).
 * Used by Gallery for prev/next group navigation.
 * @returns {Array<Object>} Array of duplicate groups
 */
Duplicates.getGroups = function() {
    // Return the locally sorted/filtered groups for display
    // This is derived from AppState.duplicates.getGroups() with sorting applied
    return this.state.groups;
};

/**
 * Navigates to a duplicate group by hash.
 * Sets up the filter to show only that group's images and selects the best image.
 * Also updates the Duplicates screen selection so returning shows the correct group.
 * Used by both _openGroupInGallery and Gallery's prev/next navigation.
 * @param {string} hash - The group hash to navigate to
 * @returns {boolean} True if navigation successful
 */
Duplicates.navigateToGroup = function(hash) {
    const group = this.state.groups.find(g => g.group_hash === hash);
    if (!group?.image_ids?.length) return false;

    const imageIds = group.image_ids;

    // Update Duplicates screen selection state (visual sync happens in onEnter)
    this.state.selectedGroups = [hash];

    // Set filter to show only this group's images
    // Quality sort determines the best image — no initialSelection needed
    App.setFilter({
        type: 'duplicates',
        imageIds,
        groupHash: hash,
        sourceLevel: this.state.currentLevel,
    });

    return true;
};

/* ==========================================================================
   LOADING STATE
   ========================================================================== */

/**
 * Shows the global loading overlay with a message.
 * @param {string} message - The loading message to display
 * @private
 */
Duplicates._showLoading = function(message) {
    AppState.loading.show('duplicates', message);
};

/**
 * Hides the global loading overlay if duplicates is the owner.
 * @private
 */
Duplicates._hideLoading = function() {
    AppState.loading.hide('duplicates');
};

// Register module with App
App.registerModule('duplicates', Duplicates);
