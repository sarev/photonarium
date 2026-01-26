/**
 * @fileoverview Duplicates detection screen module for the Imaginary application.
 *
 * This module handles the Duplicates screen where users find and manage
 * duplicate or near-duplicate images. It registers with the core App module
 * and provides a specialised view for duplicate group management.
 *
 * Uses shared infrastructure from thumbnails.js:
 * - VirtualGrid: Virtual scrolling with absolute positioning
 * - GridSelection: Unified selection handling (click, keyboard, drag-box)
 * - ThumbnailLoader: Scroll-aware thumbnail fetching with distance-based priority
 *
 * RESPONSIBILITIES:
 *
 * Duplicate Detection Levels:
 *   The similarity slider controls the strictness of duplicate matching.
 *   Slider moves from loose (left) to strict (right), matching filter sliders:
 *   - Related (left): Lower OpenCLIP similarity threshold
 *     (catches thematically related images)
 *   - Similar: High OpenCLIP embedding cosine similarity
 *     (catches shot sequences, similar compositions)
 *   - Near-identical: Same or very similar perceptual hash
 *     (catches rescaled images, different compression levels)
 *   - Identical (right): Same file size and SHA256 checksum
 *
 * Stack Display:
 *   - Shows duplicate groups as stacked thumbnail cards
 *   - Each stack shows the "best" image as the top thumbnail
 *   - Stack displays count of images in the group (e.g., "3 images")
 *   - Stacks are sorted by group size (largest groups first)
 *   - Empty state message when no duplicates found at current level
 *
 * Best Image Selection:
 *   The "best" image in each group is determined by:
 *   1. Highest resolution (width × height)
 *   2. Best Laplacian variance score (most in focus)
 *   3. Lossless compression preferred over lossy
 *   This image appears on top of the stack and is pre-selected when
 *   viewing the group in Gallery.
 *
 * Stack Interaction:
 *   - Click to select stacks (single, Ctrl+click, Shift+click, drag-box)
 *   - Double-click stack opens Gallery filtered to show only that group
 *   - Gallery pre-selects the "best" image in the group
 *   - Returning from Gallery restores Duplicates scroll position
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
     * Index 0 = leftmost (loosest), Index 3 = rightmost (strictest).
     * @type {string[]}
     * @constant
     */
    SIMILARITY_LABELS: ['Related', 'Similar', 'Near-identical', 'Identical'],

    /**
     * Converts slider position to similarity level.
     * Slider: 0 (left/loose) to 3 (right/strict)
     * Level: 3 (related) to 0 (identical)
     * @param {number} sliderValue - Slider position (0-3)
     * @returns {number} Similarity level (0-3)
     * @private
     */
    _sliderToLevel(sliderValue) {
        return 3 - sliderValue;
    },

    /**
     * Converts similarity level to slider position.
     * @param {number} level - Similarity level (0-3)
     * @returns {number} Slider position (0-3)
     * @private
     */
    _levelToSlider(level) {
        return 3 - level;
    },

    /**
     * Local state for the duplicates screen.
     * @type {Object}
     * @property {Object<number, Array>} groupCache - Cached groups by similarity level
     * @property {Object<number, string>} statusCache - Cached status by similarity level
     * @property {Object<number, string>} epochCache - Cached epoch by similarity level
     * @property {number} currentLevel - Current similarity level (0-3)
     * @property {Array<Object>} groups - Current duplicate groups for display (filtered)
     * @property {Array<Object>} allGroups - All groups before min size filtering
     * @property {string} currentStatus - Status of current level ('pending', 'computing', 'done')
     * @property {number} scrollTop - Saved scroll position
     * @property {boolean} needsRefresh - Whether data needs to reload
     * @property {string} sortMode - Current sort mode: 'size' or 'semantic'
     * @property {string} semanticQuery - Current semantic query for sorting
     * @property {number} minGroupSize - Minimum group size to display
     * @property {Array<string>} selectedGroups - Currently selected group hashes
     */
    state: {
        groupCache: {},
        statusCache: {},
        epochCache: {},
        currentLevel: 0,
        groups: [],
        allGroups: [],
        currentStatus: 'pending',
        scrollTop: 0,
        needsRefresh: true,
        sortMode: 'size',
        semanticQuery: '',
        minGroupSize: 2,
        selectedGroups: []
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
            btnSortSemantic: App.$('btn-dup-sort-semantic'),
            semanticQuery: App.$('dup-semantic-query'),
            minGroupSize: App.$('dup-min-group-size')
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
                // Stack height: thumbnail (square) + count label + padding
                const thumbnailHeight = itemWidth;
                const labelHeight = 24;
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
            },
            onItemActivated: (hash) => this._openGroupInGallery(hash),
            // No delete handler for duplicate groups
            enableDeleteKey: false
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
        // Sync slider with current level (invert: level 0=strict, slider 3=strict)
        const sliderPos = this._levelToSlider(this.state.currentLevel);
        this._els.slider.value = sliderPos;
        this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[sliderPos];

        // Sync sort mode UI
        this._els.btnSortSize.classList.toggle('active', this.state.sortMode === 'size');
        this._els.btnSortSemantic.classList.toggle('active', this.state.sortMode === 'semantic');
        this._els.semanticQuery.disabled = (this.state.sortMode !== 'semantic');
        this._els.semanticQuery.value = this.state.semanticQuery;

        // Sync min group size dropdown
        this._els.minGroupSize.value = String(this.state.minGroupSize);

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
        // Similarity slider (inverted: left=loose/related, right=strict/identical)
        this._els.slider.addEventListener('input', () => {
            const sliderPos = parseInt(this._els.slider.value, 10);
            const level = this._sliderToLevel(sliderPos);
            this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[sliderPos];
            this._setLevel(level);
        });

        // Sort mode buttons
        this._els.btnSortSize.addEventListener('click', () => this._setSortMode('size'));
        this._els.btnSortSemantic.addEventListener('click', () => this._setSortMode('semantic'));

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
    },

    /**
     * Subscribes to app-level events.
     * @private
     */
    _subscribeToEvents() {
        // Thumbnail size sync
        App.on('thumbnailSizeChanged', (size) => this._onThumbnailSizeChanged(size));

        // Database changes require refresh
        App.on('databaseChanged', () => {
            this.state.needsRefresh = true;
            this.state.groupCache = {};
            this.state.epochCache = {};
            this.state.allGroups = [];
            this.state.groups = [];
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
        this.state.groupCache = {};
        this.state.statusCache = {};
        this.state.epochCache = {};
        this.state.allGroups = [];
        this.state.groups = [];
    }
};

/* ==========================================================================
   DATA LOADING & GROUP MANAGEMENT

   Fetching duplicate groups, caching, and similarity level changes.
   ========================================================================== */

/**
 * Loads duplicate groups from the backend for the current level.
 * Uses cache if available, otherwise fetches from API.
 * @private
 */
Duplicates._loadGroups = async function() {
    App.showLoading('Loading duplicates…');

    try {
        const { groups, status } = await this._getGroupsForLevel(this.state.currentLevel);
        this.state.allGroups = groups;
        this.state.currentStatus = status;
        this.state.needsRefresh = false;

        // Apply current sort mode (also applies min group size filter)
        await this._applySortOrder();

        // If still computing, poll for updates
        if (status === 'computing' || status === 'pending') {
            this._scheduleStatusPoll(this.state.currentLevel);
        }
    } catch (err) {
        App.showError('Failed to load duplicates: ' + err.message);
    } finally {
        App.hideLoading();
    }
};

/**
 * Gets duplicate groups for a given similarity level.
 * Uses epoch-based caching to avoid re-fetching unchanged data.
 * @param {number} level - Similarity level (0-3)
 * @returns {Promise<{groups: Array<Object>, status: string}>} Groups and computation status
 * @private
 */
Duplicates._getGroupsForLevel = async function(level) {
    // Check cache status
    const cachedStatus = this.state.statusCache[level];
    const cachedEpoch = this.state.epochCache[level];
    const cachedGroups = this.state.groupCache[level];

    // If we have cached data with 'done' status, include epoch in request
    let url = `/duplicates?level=${level}`;
    if (cachedStatus === 'done' && cachedEpoch && cachedGroups) {
        url += `&since=${encodeURIComponent(cachedEpoch)}`;
    }

    // Fetch from backend
    const response = await App.apiGet(url);

    // If unchanged, return cached data
    if (response.unchanged && cachedGroups) {
        return {
            groups: cachedGroups,
            status: response.status || 'done'
        };
    }

    // New data - the API now returns lightweight format with best_image already selected
    const groups = response.groups || [];
    const status = response.status || 'done';
    const epoch = response.epoch || '';

    // Groups are already sorted by count (largest first) from the API

    // Only cache if computation is done
    if (status === 'done') {
        this.state.groupCache[level] = groups;
        this.state.epochCache[level] = epoch;
    }
    this.state.statusCache[level] = status;

    return { groups, status };
};

/**
 * Changes the similarity level and updates the display.
 * @param {number} level - New similarity level (0-3)
 * @private
 */
Duplicates._setLevel = async function(level) {
    if (level === this.state.currentLevel && this.state.allGroups.length > 0 && this.state.currentStatus === 'done') {
        return;
    }

    this.state.currentLevel = level;

    // Clear selection when changing level
    this._selection.clear();

    App.showLoading('Loading duplicates…');

    try {
        const { groups, status } = await this._getGroupsForLevel(level);
        this.state.allGroups = groups;
        this.state.currentStatus = status;

        // Apply current sort mode (also applies min group size filter)
        await this._applySortOrder();

        // If still computing, poll for updates
        if (status === 'computing' || status === 'pending') {
            this._scheduleStatusPoll(level);
        }
    } catch (err) {
        App.showError('Failed to load duplicates: ' + err.message);
    } finally {
        App.hideLoading();
    }
};

/**
 * Schedules a poll to check for updated duplicate computation status.
 * @param {number} level - The level to poll for
 * @private
 */
Duplicates._scheduleStatusPoll = function(level) {
    // Clear any existing poll
    if (this._pollTimeout) {
        clearTimeout(this._pollTimeout);
    }

    // Poll again in 2 seconds
    this._pollTimeout = setTimeout(async () => {
        // Only poll if still on same level and screen is visible
        if (this.state.currentLevel !== level) return;
        if (!this._els.grid.offsetParent) return;

        try {
            const { groups, status } = await this._getGroupsForLevel(level);
            this.state.allGroups = groups;
            this.state.currentStatus = status;
            this._applyMinGroupSizeFilter();
            this._renderGroups();

            // Continue polling if still not done
            if (status === 'computing' || status === 'pending') {
                this._scheduleStatusPoll(level);
            }
        } catch (err) {
            // Silently fail polls, user can manually refresh
        }
    }, 2000);
};

/**
 * Sets the sort mode for duplicate groups.
 * @param {string} mode - 'size' or 'semantic'
 * @private
 */
Duplicates._setSortMode = function(mode) {
    if (mode === this.state.sortMode) return;

    this.state.sortMode = mode;

    // Update button states
    this._els.btnSortSize.classList.toggle('active', mode === 'size');
    this._els.btnSortSemantic.classList.toggle('active', mode === 'semantic');

    // Enable/disable semantic input
    this._els.semanticQuery.disabled = (mode !== 'semantic');

    if (mode === 'semantic') {
        // Focus the input when switching to semantic mode
        this._els.semanticQuery.focus();
        // If there's already a query, apply it
        if (this.state.semanticQuery) {
            this._applySemanticSort();
        }
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
 * @private
 */
Duplicates._applyMinGroupSizeFilter = function() {
    const minSize = this.state.minGroupSize;
    this.state.groups = this.state.allGroups.filter(g => g.count >= minSize);
};

/**
 * Sorts groups by size (count) in descending order.
 * Sorts allGroups and re-applies min size filter.
 * @private
 */
Duplicates._sortGroupsBySize = function() {
    this.state.allGroups.sort((a, b) => b.count - a.count);
    this._applyMinGroupSizeFilter();
};

/**
 * Applies the current sort order and re-renders.
 * @private
 */
Duplicates._applySortOrder = async function() {
    if (this.state.sortMode === 'semantic' && this.state.semanticQuery) {
        await this._applySemanticSort();
    } else {
        this._sortGroupsBySize();
        this._renderGroups();
    }
};

/**
 * Applies semantic sorting based on the current query.
 * Fetches similarity scores from the backend and reorders groups.
 * Sorts allGroups and re-applies min size filter.
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
        App.showLoading('Sorting by similarity...');

        // Call backend to get similarity scores
        const response = await App.apiPost('/duplicates/sort-semantic', {
            query: query,
            image_ids: groupImageIds.map(g => g.image_id)
        });

        if (response.scores) {
            // Create a map of image_id -> score
            const scoreMap = new Map(
                response.scores.map(s => [s.image_id, s.score])
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
        App.hideLoading();
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

    const status = this.state.currentStatus;
    const sliderPos = this._levelToSlider(this.state.currentLevel);
    const levelLabel = this.SIMILARITY_LABELS[sliderPos].toLowerCase();

    // Show empty/status state if no groups
    if (this.state.groups.length === 0) {
        grid.hidden = true;
        empty.hidden = false;

        const p = empty.querySelector('p');
        if (p) {
            if (status === 'computing') {
                p.textContent = `Computing ${levelLabel} duplicates... This may take a while for large collections.`;
            } else if (status === 'pending') {
                p.textContent = `Waiting to compute ${levelLabel} duplicates...`;
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

    // Best image preview (thumbnail) with blob URL already set
    const img = document.createElement('img');
    img.src = blobUrl;
    img.alt = group.best_image?.basename || 'Duplicate group preview';
    img.dataset.imageId = group.best_image?.id || '';
    stack.appendChild(img);

    // Count label
    const count = document.createElement('div');
    count.className = 'duplicate-stack-count';
    count.textContent = `${group.count} images`;
    stack.appendChild(count);

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
 * Gets the current list of duplicate groups (after filtering).
 * Used by Gallery for prev/next group navigation.
 * @returns {Array<Object>} Array of duplicate groups
 */
Duplicates.getGroups = function() {
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
    const bestId = group.best_image?.id;

    // Update Duplicates screen selection state (visual sync happens in onEnter)
    this.state.selectedGroups = [hash];

    // Set filter to show only this group's images
    // Include initialSelection so Gallery can apply it after loading
    App.setFilter({
        type: 'duplicates',
        imageIds,
        groupHash: hash,
        sourceLevel: this.state.currentLevel,
        initialSelection: bestId ? [bestId] : [imageIds[0]]
    });

    return true;
};

// Register module with App
App.registerModule('duplicates', Duplicates);
