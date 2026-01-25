/**
 * @fileoverview Duplicates detection screen module for the Imaginary application.
 *
 * This module handles the Duplicates screen where users find and manage
 * duplicate or near-duplicate images. It registers with the core App module
 * and provides a specialised view for duplicate group management.
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
 *   - Double-click stack opens Gallery filtered to show only that group
 *   - Gallery pre-selects the "best" image in the group
 *   - Returning from Gallery restores Duplicates scroll position
 *   - Thumbnail size controls (smaller/larger) adjust stack preview size
 *
 * Dynamic Updates:
 *   - Changing similarity slider immediately recomputes and updates display
 *   - Backend provides pre-computed duplicate groups at each level
 *   - Smooth transition animation when groups appear/disappear
 *
 * Performance:
 *   - Duplicate groups are computed on backend during scan
 *   - Frontend caches group data for quick slider changes
 *   - Lazy loads stack thumbnails as they scroll into view
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Fetches duplicate groups from backend, renders stacks
 *   - onLeave(): Saves scroll position for restoration on return
 *
 * @module duplicates
 * @requires core
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
        minGroupSize: 2
    },

    /**
     * Virtual scrolling configuration.
     * @type {Object}
     * @private
     */
    _virtualScroll: {
        itemHeight: 0,      // Height of each stack item including gap
        itemWidth: 0,       // Width of each stack item including gap
        itemsPerRow: 0,     // Number of items per row
        visibleRows: 0,     // Number of visible rows
        bufferRows: 3,      // Extra rows to pre-render above/below viewport
        startIndex: 0,      // First rendered item index
        endIndex: 0,        // Last rendered item index
        renderedItems: new Map(), // Cache of rendered DOM elements by group_hash
        scrollHandler: null // Bound scroll handler for cleanup
    },

    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

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

        // Bind events
        this._bindEvents();

        // Set up virtual scrolling
        this._initVirtualScroll();

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

        // Load data if needed
        if (this.state.needsRefresh) {
            this._loadGroups();
        } else {
            // Attach scroll listener and restore position
            this._attachScrollListener();
            this._els.container.scrollTop = this.state.scrollTop;
            // Update visible items in case viewport changed
            this._updateVisibleItems(this.state.scrollTop);
        }
    },

    /**
     * Called when leaving the duplicates screen.
     * Saves scroll position for restoration on return.
     */
    onLeave() {
        this.state.scrollTop = this._els.container.scrollTop;
        this._detachScrollListener();
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
        // Thumbnail size buttons are handled globally by the toolbar (App.setThumbnailSize)

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

        // Grid click events (delegated)
        this._els.grid.addEventListener('dblclick', (e) => this._handleDoubleClick(e));
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

        // Re-render if currently visible
        if (App.getScreen() === 'duplicates' && this.state.groups.length > 0) {
            const container = this._els.container;
            if (container) {
                this._calculateVirtualDimensions(container);
                // Clear cache and force re-render
                this._virtualScroll.renderedItems.clear();
                const items = this._els.grid.querySelectorAll('.duplicate-stack');
                for (const item of items) {
                    item.remove();
                }
                this._virtualScroll.startIndex = -1;
                this._virtualScroll.endIndex = -1;
                this._updateVisibleItems(container.scrollTop);
            }
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
     * Initializes virtual scrolling.
     * @private
     */
    _initVirtualScroll() {
        // Create spacer elements for virtual scrolling
        this._topSpacer = document.createElement('div');
        this._topSpacer.className = 'virtual-spacer';
        this._bottomSpacer = document.createElement('div');
        this._bottomSpacer.className = 'virtual-spacer';

        // Bind scroll handler
        this._virtualScroll.scrollHandler = this._onScroll.bind(this);

        // Handle window resize
        this._resizeHandler = App.debounce(() => {
            if (App.getScreen() === 'duplicates' && this.state.groups.length > 0) {
                const container = this._els.container;
                if (container) {
                    this._calculateVirtualDimensions(container);
                    // Force re-render
                    this._virtualScroll.startIndex = -1;
                    this._virtualScroll.endIndex = -1;
                    this._updateVisibleItems(container.scrollTop);
                }
            }
        }, 100);
        window.addEventListener('resize', this._resizeHandler);
    },

    /**
     * Attaches the scroll listener for virtual scrolling.
     * @private
     */
    _attachScrollListener() {
        const container = this._els.container;
        if (container && this._virtualScroll.scrollHandler) {
            container.removeEventListener('scroll', this._virtualScroll.scrollHandler);
            container.addEventListener('scroll', this._virtualScroll.scrollHandler, { passive: true });
        }
    },

    /**
     * Detaches the scroll listener for virtual scrolling.
     * @private
     */
    _detachScrollListener() {
        const container = this._els.container;
        if (container && this._virtualScroll.scrollHandler) {
            container.removeEventListener('scroll', this._virtualScroll.scrollHandler);
        }
    },

    /**
     * Handles scroll events for virtual scrolling.
     * @param {Event} e - Scroll event
     * @private
     */
    _onScroll(e) {
        // Throttle scroll updates with requestAnimationFrame
        if (this._scrollRAF) return;
        const scrollTop = e.target.scrollTop;
        this._scrollRAF = requestAnimationFrame(() => {
            this._scrollRAF = null;
            this._updateVisibleItems(scrollTop);
        });
    },

    /**
     * Calculates dimensions for virtual scrolling.
     * Matches CSS grid's auto-fill calculation.
     * @param {HTMLElement} container - The scroll container
     * @private
     */
    _calculateVirtualDimensions(container) {
        const thumbSize = App.getThumbnailSize();
        const gap = 16; // 1rem gap (from CSS)
        const padding = 16; // 1rem padding (from CSS)

        // Calculate items per row matching CSS grid auto-fill behavior
        const availableWidth = container.clientWidth - padding * 2;
        const minItemWidth = thumbSize + 16; // Stack includes padding

        this._virtualScroll.itemsPerRow = Math.max(1, Math.floor((availableWidth + gap) / (minItemWidth + gap)));

        // Actual item width when using 1fr
        const actualItemWidth = (availableWidth - gap * (this._virtualScroll.itemsPerRow - 1)) / this._virtualScroll.itemsPerRow;

        // Item height: thumbnail (square) + count label + padding
        const thumbnailHeight = actualItemWidth;
        const labelHeight = 24;
        const itemHeight = thumbnailHeight + labelHeight + 16;

        this._virtualScroll.itemWidth = actualItemWidth;
        this._virtualScroll.itemHeight = itemHeight + gap;

        // Calculate visible rows
        const containerHeight = container.clientHeight;
        this._virtualScroll.visibleRows = Math.ceil(containerHeight / this._virtualScroll.itemHeight) + 1;

        // Calculate total height
        const totalRows = Math.ceil(this.state.groups.length / this._virtualScroll.itemsPerRow);
        this._virtualScroll.totalHeight = totalRows * this._virtualScroll.itemHeight;
    },

    /**
     * Updates visible items based on scroll position.
     * @param {number} scrollTop - Current scroll position
     * @private
     */
    _updateVisibleItems(scrollTop) {
        const vs = this._virtualScroll;
        const groups = this.state.groups;
        const grid = this._els.grid;

        if (groups.length === 0) return;

        const totalRows = Math.ceil(groups.length / vs.itemsPerRow);
        const firstVisibleRow = Math.floor(scrollTop / vs.itemHeight);

        // Render zone: must have these items in DOM
        const renderStartRow = Math.max(0, firstVisibleRow - vs.bufferRows);
        const renderEndRow = Math.min(totalRows, firstVisibleRow + vs.visibleRows + vs.bufferRows);

        // Convert to item indices
        const renderStart = renderStartRow * vs.itemsPerRow;
        const renderEnd = Math.min(renderEndRow * vs.itemsPerRow, groups.length);

        // Track what we need
        const neededHashes = new Set();
        for (let i = renderStart; i < renderEnd; i++) {
            neededHashes.add(groups[i].group_hash);
        }

        // Remove items outside render zone
        const currentItems = grid.querySelectorAll('.duplicate-stack');
        for (const item of currentItems) {
            const hash = item.dataset.groupHash;
            if (!neededHashes.has(hash)) {
                vs.renderedItems.delete(hash);
                item.remove();
            }
        }

        // Add missing items in render zone
        for (let i = renderStart; i < renderEnd; i++) {
            const group = groups[i];
            if (!vs.renderedItems.has(group.group_hash)) {
                const stack = this._createStackElement(group, i);
                vs.renderedItems.set(group.group_hash, stack);
                this._insertItemAtPosition(stack, i);
                // Load thumbnail immediately since we only render visible items
                this._loadStackThumbnail(stack, group);
            }
        }

        // Update spacer heights
        let minRenderedIdx = Infinity;
        let maxRenderedIdx = -1;
        for (const [hash] of vs.renderedItems) {
            const idx = groups.findIndex(g => g.group_hash === hash);
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
     * Inserts a stack element at the correct position in the grid.
     * @param {HTMLElement} stack - The stack element to insert
     * @param {number} targetIndex - The index in groups array
     * @private
     */
    _insertItemAtPosition(stack, targetIndex) {
        const grid = this._els.grid;
        const groups = this.state.groups;

        // Find the right position among existing items
        const existingItems = grid.querySelectorAll('.duplicate-stack');
        let insertBefore = this._bottomSpacer;

        for (const existing of existingItems) {
            const existingHash = existing.dataset.groupHash;
            const existingIdx = groups.findIndex(g => g.group_hash === existingHash);
            if (existingIdx > targetIndex) {
                insertBefore = existing;
                break;
            }
        }

        grid.insertBefore(stack, insertBefore);
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
        if (!this._els.container.offsetParent) return;

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
 * Renders the current duplicate groups using virtual scrolling.
 * Only renders visible items plus a buffer for smooth scrolling.
 * @private
 */
Duplicates._renderGroups = function() {
    const grid = this._els.grid;
    const empty = this._els.empty;
    const container = this._els.container;

    // Clear existing content and cache
    grid.innerHTML = '';
    this._virtualScroll.renderedItems.clear();
    this._virtualScroll.startIndex = -1;
    this._virtualScroll.endIndex = -1;

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

    // Calculate virtual scroll dimensions
    this._calculateVirtualDimensions(container);

    // Add spacers for virtual scrolling
    grid.appendChild(this._topSpacer);
    grid.appendChild(this._bottomSpacer);

    // Render initial visible items
    this._updateVisibleItems(container.scrollTop);

    // Attach scroll listener
    this._attachScrollListener();
};

/**
 * Creates a stack element for a duplicate group.
 * @param {Object} group - The duplicate group (lightweight format)
 * @param {number} index - Group index for data attribute
 * @returns {HTMLElement} The stack element
 * @private
 */
Duplicates._createStackElement = function(group, index) {
    const stack = document.createElement('div');
    stack.className = 'duplicate-stack';
    stack.dataset.groupIndex = index;
    stack.dataset.groupHash = group.group_hash;

    // Best image preview (thumbnail)
    const img = document.createElement('img');
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

/**
 * Loads the thumbnail image for a stack element.
 * Called when stack is rendered in the visible zone.
 * @param {HTMLElement} stack - The stack element
 * @param {Object} group - The group data (for additional context if needed)
 * @private
 */
Duplicates._loadStackThumbnail = function(stack, group) {
    const img = stack.querySelector('img');
    const imageId = img?.dataset.imageId;

    if (!img || !imageId) {
        stack.classList.add('error');
        return;
    }

    img.onload = () => {
        stack.classList.add('loaded');
    };

    img.onerror = () => {
        stack.classList.add('error');
    };

    img.src = App.thumbnailUrl(imageId);
};

/**
 * Adjusts the thumbnail size for duplicate stacks.
 * @param {number} delta - Size change direction (-1 smaller, +1 larger)
 * @private
 */
Duplicates._adjustThumbSize = function(delta) {
    // Deprecated: toolbar buttons call App.setThumbnailSize directly.
    const step = 50;
    App.setThumbnailSize(App.getThumbnailSize() + delta * step);
};

/* ==========================================================================
   STACK INTERACTION

   Double-click handling and navigation to gallery.
   ========================================================================== */

/**
 * Handles double-click on a duplicate stack.
 * Opens the gallery filtered to show only that group's images.
 * @param {MouseEvent} e - The double-click event
 * @private
 */
Duplicates._handleDoubleClick = function(e) {
    const stack = e.target.closest('.duplicate-stack');
    if (!stack) return;

    const index = parseInt(stack.dataset.groupIndex, 10);
    const group = this.state.groups[index];

    if (!group?.image_ids?.length) return;

    // Save scroll position before leaving
    this.state.scrollTop = this._els.container.scrollTop;

    const imageIds = group.image_ids;
    if (imageIds.length === 0) return;

    const bestId = group.best_image?.id;
    const selection = bestId ? [bestId] : [imageIds[0]];

    // Set a gallery filter to show only this group's images
    App.setFilter({
        type: 'duplicates',
        imageIds,
        sourceLevel: this.state.currentLevel
    });

    // Pre-select the best image for convenience
    App.setSelectedImages(selection);

    App.showGallery();
};

// Register module with App
App.registerModule('duplicates', Duplicates);
