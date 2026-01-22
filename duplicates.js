/**
 * @fileoverview Duplicates detection screen module for the Imaginary application.
 *
 * This module handles the Duplicates screen where users find and manage
 * duplicate or near-duplicate images. It registers with the core App module
 * and provides a specialized view for duplicate group management.
 *
 * RESPONSIBILITIES:
 *
 * Duplicate Detection Levels:
 *   The similarity slider controls the strictness of duplicate matching:
 *   - Level 0 (Identical): Same file size and SHA256 checksum
 *   - Level 1 (Perceptual): Same or very similar perceptual hash
 *     (catches rescaled images, different compression levels)
 *   - Level 2 (Similar): High OpenCLIP embedding cosine similarity
 *     (catches shot sequences, similar compositions)
 *   - Level 3 (Related): Lower OpenCLIP similarity threshold
 *     (catches thematically related images)
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
     * Similarity level labels for the slider.
     * @type {string[]}
     * @constant
     */
    SIMILARITY_LABELS: ['Identical', 'Perceptual', 'Similar', 'Related'],

    /**
     * Local state for the duplicates screen.
     * @type {Object}
     * @property {Object<number, Array>} groupCache - Cached groups by similarity level
     * @property {number} currentLevel - Current similarity level (0-3)
     * @property {Array<Object>} groups - Current duplicate groups for display
     * @property {number} scrollTop - Saved scroll position
     * @property {boolean} needsRefresh - Whether data needs to reload
     * @property {IntersectionObserver|null} lazyLoader - Observer for lazy loading
     */
    state: {
        groupCache: {},
        currentLevel: 0,
        groups: [],
        scrollTop: 0,
        needsRefresh: true,
        lazyLoader: null
    },

    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * Initializes the duplicates module.
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
            btnLarger: App.$('btn-dup-thumb-larger')
        };

        this._els.grid.style.setProperty('--thumb-size', App.getThumbnailSize() + 'px');

        // Bind events
        this._bindEvents();

        // Set up lazy loading observer
        this._setupLazyLoader();

        // Subscribe to relevant app events
        this._subscribeToEvents();
    },

    /**
     * Called when entering the duplicates screen.
     * Fetches groups if needed and restores scroll position.
     */
    onEnter() {
        // Sync slider with current level
        this._els.slider.value = this.state.currentLevel;
        this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[this.state.currentLevel];

        // Load data if needed
        if (this.state.needsRefresh) {
            this._loadGroups();
        } else {
            // Restore scroll position
            this._els.container.scrollTop = this.state.scrollTop;
        }
    },

    /**
     * Called when leaving the duplicates screen.
     * Saves scroll position for restoration on return.
     */
    onLeave() {
        this.state.scrollTop = this._els.container.scrollTop;
    },

    /**
     * Binds event listeners for controls.
     * @private
     */
    _bindEvents() {
        // Similarity slider
        this._els.slider.addEventListener('input', () => {
            const level = parseInt(this._els.slider.value, 10);
            this._els.sliderLabel.textContent = this.SIMILARITY_LABELS[level];
            this._setLevel(level);
        });

        // Thumbnail size buttons
        // this._els.btnSmaller.addEventListener('click', () => this._adjustThumbSize(-1));
        // this._els.btnLarger.addEventListener('click', () => this._adjustThumbSize(1));

        // Grid click events (delegated)
        this._els.grid.addEventListener('dblclick', (e) => this._handleDoubleClick(e));
    },

    /**
     * Subscribes to app-level events.
     * @private
     */
    _subscribeToEvents() {
        App.on('thumbnailSizeChanged', (size) => {
          this._els.grid.style.setProperty('--thumb-size', size + 'px');
        });

        // Database changes require refresh
        App.on('databaseChanged', () => {
            this.state.needsRefresh = true;
            this.state.groupCache = {};
        });
    },

    /**
     * Sets up the IntersectionObserver for lazy loading thumbnails.
     * @private
     */
    _setupLazyLoader() {
        this.state.lazyLoader = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) {
                        const stack = entry.target;
                        this._loadStackThumbnail(stack);
                        this.state.lazyLoader.unobserve(stack);
                    }
                });
            },
            {
                root: this._els.container,
                rootMargin: '100px',
                threshold: 0
            }
        );
    },

    /**
     * Marks the module as needing a refresh on next enter.
     * Called by other modules when data changes.
     */
    markNeedsRefresh() {
        this.state.needsRefresh = true;
        this.state.groupCache = {};
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
        const groups = await this._getGroupsForLevel(this.state.currentLevel);
        this.state.groups = groups;
        this.state.needsRefresh = false;
        this._renderGroups();
    } catch (err) {
        App.showError('Failed to load duplicates: ' + err.message);
    } finally {
        App.hideLoading();
    }
};

/**
 * Gets duplicate groups for a given similarity level.
 * Returns from cache if available, otherwise fetches from backend.
 * @param {number} level - Similarity level (0-3)
 * @returns {Promise<Array<Object>>} Array of duplicate groups
 * @private
 */
Duplicates._getGroupsForLevel = async function(level) {
    // Return cached if available
    if (this.state.groupCache[level]) {
        return this.state.groupCache[level];
    }

    // Fetch from backend
    const response = await App.apiGet(`/duplicates?level=${level}`);
    const groups = response.groups || [];

    // Process each group to determine the "best" image
    groups.forEach((group) => {
        group.bestImage = this._selectBestImage(group.images);
    });

    // Sort by group size (largest first)
    groups.sort((a, b) => b.images.length - a.images.length);

    // Cache the result
    this.state.groupCache[level] = groups;

    return groups;
};

/**
 * Changes the similarity level and updates the display.
 * @param {number} level - New similarity level (0-3)
 * @private
 */
Duplicates._setLevel = async function(level) {
    if (level === this.state.currentLevel && this.state.groups.length > 0) {
        return;
    }

    this.state.currentLevel = level;

    try {
        const groups = await this._getGroupsForLevel(level);
        this.state.groups = groups;
        this._renderGroups();
    } catch (err) {
        App.showError('Failed to load duplicates: ' + err.message);
    }
};

/**
 * Selects the "best" image from a group of duplicates.
 * Criteria in order:
 *   1. Highest resolution (width × height)
 *   2. Best Laplacian variance (sharpness/focus)
 *   3. Lossless compression preferred
 * @param {Array<Object>} images - Array of image objects in the group
 * @returns {Object} The best image from the group
 * @private
 */
Duplicates._selectBestImage = function(images) {
    if (!images || images.length === 0) {
        return null;
    }

    return images.reduce((best, img) => {
        // Compare resolution
        const bestRes = (best.width || 0) * (best.height || 0);
        const imgRes = (img.width || 0) * (img.height || 0);

        if (imgRes > bestRes) {
            return img;
        }
        if (imgRes < bestRes) {
            return best;
        }

        // Resolution equal, compare Laplacian variance (sharpness)
        const bestLap = best.laplacian_variance || 0;
        const imgLap = img.laplacian_variance || 0;

        if (imgLap > bestLap) {
            return img;
        }
        if (imgLap < bestLap) {
            return best;
        }

        // Sharpness equal, prefer lossless
        if (img.lossless && !best.lossless) {
            return img;
        }

        return best;
    });
};

/* ==========================================================================
   RENDERING & DISPLAY

   Stack grid rendering, thumbnails, and visual updates.
   ========================================================================== */

/**
 * Renders the current duplicate groups as stacked cards.
 * @private
 */
Duplicates._renderGroups = function() {
    const grid = this._els.grid;
    const empty = this._els.empty;

    grid.innerHTML = '';

    if (this.state.groups.length === 0) {
        grid.hidden = true;
        empty.hidden = false;

        const p = empty.querySelector('p');
        const msg = `No ${this.SIMILARITY_LABELS[this.state.currentLevel].toLowerCase()} duplicates found`;
        if (p) p.textContent = msg;
        return;
    }

    empty.hidden = true;
    grid.hidden = false;

    this.state.groups.forEach((group, index) => {
        const stack = this._createStackElement(group, index);
        grid.appendChild(stack);
        this.state.lazyLoader.observe(stack);
    });
};

/**
 * Creates a stack element for a duplicate group.
 * @param {Object} group - The duplicate group
 * @param {number} index - Group index for data attribute
 * @returns {HTMLElement} The stack element
 * @private
 */
Duplicates._createStackElement = function(group, index) {
    const stack = document.createElement('div');
    stack.className = 'duplicate-stack';
    stack.dataset.groupIndex = index;

    // Placeholder thumbnail (loaded lazily)
    const thumb = document.createElement('div');
    thumb.className = 'stack-thumbnail';
    thumb.dataset.imageId = group.bestImage?.id || '';

    // Stack shadow layers for visual depth
    const shadow1 = document.createElement('div');
    shadow1.className = 'stack-shadow stack-shadow-1';
    const shadow2 = document.createElement('div');
    shadow2.className = 'stack-shadow stack-shadow-2';

    // Count badge
    const count = document.createElement('div');
    count.className = 'stack-count';
    count.textContent = `${group.images.length} images`;

    stack.appendChild(shadow2);
    stack.appendChild(shadow1);
    stack.appendChild(thumb);
    stack.appendChild(count);

    return stack;
};

/**
 * Loads the thumbnail image for a stack element.
 * Called by IntersectionObserver when stack enters viewport.
 * @param {HTMLElement} stack - The stack element
 * @private
 */
Duplicates._loadStackThumbnail = function(stack) {
    const thumb = stack.querySelector('.stack-thumbnail');
    const imageId = thumb.dataset.imageId;

    if (!imageId) {
      thumb.classList.add('error');
      return;
    }

    const img = document.createElement('img');
    img.alt = 'Duplicate group preview';

    img.onload = () => {
      thumb.appendChild(img);
      thumb.classList.add('loaded');
    };

    img.onerror = () => {
      thumb.classList.add('error');
    };

    img.src = App.thumbnailUrl(imageId);
};

/**
 * Adjusts the thumbnail size for duplicate stacks.
 * @param {number} delta - Size change direction (-1 smaller, +1 larger)
 * @private
 */
Duplicates._adjustThumbSize = function(delta) {
    // Get current size from CSS variable or use default
    const root = document.documentElement;
    const currentSize = parseInt(
        getComputedStyle(root).getPropertyValue('--dup-thumb-size') || '150',
        10
    );

    // Calculate new size with bounds
    const minSize = 100;
    const maxSize = 300;
    const step = 25;
    const newSize = Math.max(minSize, Math.min(maxSize, currentSize + delta * step));

    // Apply new size
    root.style.setProperty('--dup-thumb-size', `${newSize}px`);
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
    if (!stack) {
        return;
    }

    const index = parseInt(stack.dataset.groupIndex, 10);
    const group = this.state.groups[index];

    if (!group || !group.images || group.images.length === 0) {
        return;
    }

    // Save scroll position before leaving
    this.state.scrollTop = this._els.container.scrollTop;

    // Get image paths for the group
    const paths = group.images.map((img) => img.path);

    // Navigate to gallery with this group as the filter
    // Pre-select the best image
    App.showGalleryWithFilter({
        type: 'duplicates',
        paths: paths,
        selectedPath: group.bestImage?.path
    });
};

// Register module with App
App.registerModule('duplicates', Duplicates);
