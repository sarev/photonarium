/**
 * @fileoverview Core framework for the Imaginary application.
 *
 * This module provides the central infrastructure that all screen-specific
 * modules depend on. It initialises first and exposes a global `App` object
 * that other modules register with.
 *
 * RESPONSIBILITIES:
 *
 * Application State Management:
 *   - Maintains the current screen state (gallery, fullscreen, database, search, duplicates)
 *   - Manages theme state (light/dark) with persistence to localStorage
 *   - Tracks global UI state such as thumbnail size preferences
 *   - Provides a simple pub/sub event system for cross-module communication
 *
 * Screen Navigation:
 *   - Handles transitions between screens by updating the data-screen attribute
 *   - Shows/hides appropriate toolbar groups based on active screen
 *   - Maintains navigation history for back-button functionality
 *   - Calls screen lifecycle hooks (onEnter, onLeave) when switching screens
 *
 * API Communication:
 *   - Provides wrapper functions for all Flask backend API calls
 *   - Handles request/response serialization and error handling
 *   - Implements a mock mode for frontend development without the backend
 *   - Manages loading states during async operations
 *
 * Toolbar Management:
 *   - Binds event listeners to common toolbar buttons (theme toggle, navigation)
 *   - Updates toolbar button states (enabled/disabled, active/inactive)
 *   - Coordinates toolbar visibility based on current screen
 *
 * Dialog System:
 *   - Provides functions to show/hide modal dialogs
 *   - Manages confirmation dialogs with Promise-based responses
 *   - Handles the emoji picker dialog state
 *
 * Utility Functions:
 *   - DOM helper functions for common operations
 *   - Debounce and throttle utilities for event handling
 *   - Image URL builders for thumbnails and full images
 *   - Date and file size formatting helpers
 *
 * Module Registration:
 *   - Exposes App.registerModule() for screen modules to register themselves
 *   - Ensures proper initialization order
 *   - Provides App.ready() callback for post-initialization logic
 *
 * MOCK MODE:
 *   When App.mockMode is true, API calls return simulated data instead of
 *   contacting the Flask backend. This allows frontend development and testing
 *   without running the Python server. Mock data includes sample images,
 *   folders, and duplicate groups.
 *
 * @module core
 */

/* ==========================================================================
   APPLICATION STATE MANAGEMENT

   Maintains application state including current screen, theme, UI preferences,
   and provides a pub/sub event system for cross-module communication.
   ========================================================================== */

/**
 * Global application object.
 * All modules interact through this object.
 * @namespace
 */
const App = {
    /**
     * Enable mock mode for frontend development without backend.
     * Set to true to use simulated API responses.
     * @type {boolean}
     */
    mockMode: false,

    /**
     * Application state object.
     * @type {Object}
     * @property {string} screen - Current active screen name
     * @property {string} theme - Current theme ('light' or 'dark')
     * @property {number} thumbnailSize - Current thumbnail size in pixels
     * @property {string} sortBy - Current sort field ('date', 'rating', 'content')
     * @property {string} sortDirection - Sort direction ('asc' or 'desc')
     * @property {Object|null} filter - Active filter criteria or null if no filter
     * @property {Array<string>} selectedImages - Array of selected image IDs
     * @property {string|null} currentImageId - ID of image being viewed in fullscreen
     * @property {Object} scrollPositions - Saved scroll positions per screen
     */
    state: {
        screen: null,
        theme: 'light',
        thumbnailSize: 200,
        sortBy: 'date',
        sortDirection: 'desc',
        filter: null,
        selectedImages: [],
        currentImageId: null,
        scrollPositions: {},
        // Image cache for efficient incremental updates
        imageCache: null,       // Map of id -> image object
        imageCacheEpoch: null,  // Last sync timestamp
    },

    /**
     * Event subscribers organized by event name.
     * @type {Object<string, Array<Function>>}
     * @private
     */
    _subscribers: {},

    /**
     * Registered screen modules.
     * @type {Object<string, Object>}
     * @private
     */
    _modules: {},

    /**
     * Thumbnail loading configuration from backend.
     * Loaded async on init; defaults used until loaded.
     * @type {Object}
     * @private
     */
    _thumbnailConfig: {
        concurrentRequests: 6,
        extraRows: 5,
        timeoutMs: 10000,
        scrollThrottleMs: 250
    },

    /**
     * Callbacks to run when App is ready.
     * @type {Array<Function>}
     * @private
     */
    _readyCallbacks: [],

    /**
     * Whether the App has completed initialization.
     * @type {boolean}
     * @private
     */
    _isReady: false,

    /* ----------------------------------------------------------------------
       State Getters and Setters
       ---------------------------------------------------------------------- */

    /**
     * Gets the current screen name.
     * @returns {string} The current screen name
     */
    getScreen() {
        return this.state.screen;
    },

    /**
     * Gets the current image ID being viewed in fullscreen.
     * @returns {string|null} The current image ID or null if not in fullscreen
     */
    getCurrentImageId() {
        return this.state.currentImageId;
    },

    /**
     * Gets the current theme.
     * @returns {string} The current theme ('light' or 'dark')
     */
    getTheme() {
        return this.state.theme;
    },

    /**
     * Sets the theme and persists to localStorage.
     * Updates the data-theme attribute on the app container.
     * @param {string} theme - The theme to set ('light' or 'dark')
     * @fires App#themeChanged
     */
    setTheme(theme) {
        if (theme !== 'light' && theme !== 'dark') {
            console.warn(`Invalid theme: ${theme}. Using 'light'.`);
            theme = 'light';
        }
        this.state.theme = theme;
        localStorage.setItem('imaginary-theme', theme);
        document.getElementById('app').dataset.theme = theme;
        this.emit('themeChanged', theme);
    },

    /**
     * Toggles between light and dark theme.
     */
    toggleTheme() {
        this.setTheme(this.state.theme === 'light' ? 'dark' : 'light');
    },

    /**
     * Gets the current thumbnail size in pixels.
     * @returns {number} The thumbnail size
     */
    getThumbnailSize() {
        return this.state.thumbnailSize;
    },

    /**
     * Sets the thumbnail size and persists to localStorage.
     * Clamps value between minimum and maximum allowed sizes.
     * @param {number} size - The thumbnail size in pixels
     * @fires App#thumbnailSizeChanged
     */
    setThumbnailSize(size) {
        const MIN_SIZE = 100;
        const MAX_SIZE = 400;
        size = Math.max(MIN_SIZE, Math.min(MAX_SIZE, size));
        this.state.thumbnailSize = size;
        localStorage.setItem('imaginary-thumbnailSize', size.toString());
        this.emit('thumbnailSizeChanged', size);
    },

    /**
     * Gets the current sort configuration.
     * @returns {{by: string, direction: string}} Sort configuration
     */
    getSort() {
        return {
            by: this.state.sortBy,
            direction: this.state.sortDirection
        };
    },

    /**
     * Sets the sort field.
     * @param {string} sortBy - The field to sort by ('date', 'rating', 'content')
     * @fires App#sortChanged
     */
    setSortBy(sortBy) {
        if (!['date', 'rating', 'content'].includes(sortBy)) {
            console.warn(`Invalid sortBy: ${sortBy}. Using 'date'.`);
            sortBy = 'date';
        }
        this.state.sortBy = sortBy;
        localStorage.setItem('imaginary-sortBy', sortBy);
        this.emit('sortChanged', this.getSort());
    },

    /**
     * Sets the sort direction.
     * @param {string} direction - The sort direction ('asc' or 'desc')
     * @fires App#sortChanged
     */
    setSortDirection(direction) {
        if (direction !== 'asc' && direction !== 'desc') {
            console.warn(`Invalid sortDirection: ${direction}. Using 'desc'.`);
            direction = 'desc';
        }
        this.state.sortDirection = direction;
        localStorage.setItem('imaginary-sortDirection', direction);
        this.emit('sortChanged', this.getSort());
    },

    /**
     * Toggles the sort direction between ascending and descending.
     */
    toggleSortDirection() {
        this.setSortDirection(this.state.sortDirection === 'asc' ? 'desc' : 'asc');
    },

    /**
     * Gets the current filter criteria.
     * @returns {Object|null} The filter object or null if no filter active
     */
    getFilter() {
        return this.state.filter;
    },

    /**
     * Sets the filter criteria.
     * @param {Object|null} filter - The filter criteria or null to clear
     * @param {string} [filter.text] - Text to search in descriptions
     * @param {string} [filter.dateStart] - Start date (ISO string)
     * @param {string} [filter.dateEnd] - End date (ISO string)
     * @param {string} [filter.rating] - Rating emoji to filter by
     * @param {Object} [options] - Options for filter setting
     * @param {boolean} [options.silent] - If true, don't emit filterChanged event
     * @fires App#filterChanged
     */
    setFilter(filter, options = {}) {
        this.state.filter = filter;
        if (!options.silent) {
            this.emit('filterChanged', filter);
        }
    },

    /**
     * Checks if a filter is currently active.
     * @returns {boolean} True if a filter is active
     */
    hasActiveFilter() {
        return this.state.filter !== null;
    },

    /**
     * Clears the current filter.
     * @fires App#filterChanged
     */
    clearFilter() {
        this.setFilter(null);
    },

    /**
     * Gets the array of selected image IDs.
     * @returns {Array<string>} Array of selected image IDs
     */
    getSelectedImages() {
        return [...this.state.selectedImages];
    },

    /**
     * Sets the selected images.
     * @param {Array<string>} imageIds - Array of image IDs to select
     * @fires App#selectionChanged
     */
    setSelectedImages(imageIds) {
        this.state.selectedImages = [...imageIds];
        this.emit('selectionChanged', this.state.selectedImages);
    },

    /**
     * Adds an image to the selection.
     * @param {string} imageId - The image ID to add
     * @fires App#selectionChanged
     */
    addToSelection(imageId) {
        if (!this.state.selectedImages.includes(imageId)) {
            this.state.selectedImages.push(imageId);
            this.emit('selectionChanged', this.state.selectedImages);
        }
    },

    /**
     * Removes an image from the selection.
     * @param {string} imageId - The image ID to remove
     * @fires App#selectionChanged
     */
    removeFromSelection(imageId) {
        const index = this.state.selectedImages.indexOf(imageId);
        if (index !== -1) {
            this.state.selectedImages.splice(index, 1);
            this.emit('selectionChanged', this.state.selectedImages);
        }
    },

    /**
     * Toggles an image's selection state.
     * @param {string} imageId - The image ID to toggle
     * @fires App#selectionChanged
     */
    toggleSelection(imageId) {
        if (this.state.selectedImages.includes(imageId)) {
            this.removeFromSelection(imageId);
        } else {
            this.addToSelection(imageId);
        }
    },

    /**
     * Clears all selected images.
     * @fires App#selectionChanged
     */
    clearSelection() {
        this.state.selectedImages = [];
        this.emit('selectionChanged', this.state.selectedImages);
    },

    /* ----------------------------------------------------------------------
       Pub/Sub Event System
       ---------------------------------------------------------------------- */

    /**
     * Subscribes to an event.
     * @param {string} event - The event name to subscribe to
     * @param {Function} callback - The callback function to invoke
     * @returns {Function} Unsubscribe function
     */
    on(event, callback) {
        if (!this._subscribers[event]) {
            this._subscribers[event] = [];
        }
        this._subscribers[event].push(callback);

        // Return unsubscribe function
        return () => {
            this.off(event, callback);
        };
    },

    /**
     * Unsubscribes from an event.
     * @param {string} event - The event name to unsubscribe from
     * @param {Function} callback - The callback function to remove
     */
    off(event, callback) {
        if (!this._subscribers[event]) return;
        const index = this._subscribers[event].indexOf(callback);
        if (index !== -1) {
            this._subscribers[event].splice(index, 1);
        }
    },

    /**
     * Emits an event to all subscribers.
     * @param {string} event - The event name to emit
     * @param {...*} args - Arguments to pass to subscribers
     */
    emit(event, ...args) {
        if (!this._subscribers[event]) return;
        for (const callback of this._subscribers[event]) {
            try {
                callback(...args);
            } catch (error) {
                console.error(`Error in event handler for '${event}':`, error);
            }
        }
    },

    /* ----------------------------------------------------------------------
       State Persistence
       ---------------------------------------------------------------------- */

    /**
     * Loads persisted state from localStorage.
     * Called during initialization.
     * @private
     */
    _loadPersistedState() {
        // Load theme
        const savedTheme = localStorage.getItem('imaginary-theme');
        if (savedTheme === 'light' || savedTheme === 'dark') {
            this.state.theme = savedTheme;
        } else {
            // Check system preference
            if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
                this.state.theme = 'dark';
            }
        }

        // Load thumbnail size
        const savedSize = localStorage.getItem('imaginary-thumbnailSize');
        if (savedSize) {
            const size = parseInt(savedSize, 10);
            if (!isNaN(size) && size >= 100 && size <= 400) {
                this.state.thumbnailSize = size;
            }
        }

        // Load sort preferences
        const savedSortBy = localStorage.getItem('imaginary-sortBy');
        if (['date', 'rating', 'content'].includes(savedSortBy)) {
            this.state.sortBy = savedSortBy;
        }

        const savedSortDirection = localStorage.getItem('imaginary-sortDirection');
        if (savedSortDirection === 'asc' || savedSortDirection === 'desc') {
            this.state.sortDirection = savedSortDirection;
        }
    },

    /* ----------------------------------------------------------------------
       SCREEN NAVIGATION

       Handles transitions between screens, toolbar visibility,
       navigation history, and screen lifecycle hooks.
       ---------------------------------------------------------------------- */

    /**
     * Valid screen names.
     * @type {Array<string>}
     * @constant
     */
    SCREENS: ['gallery', 'fullscreen', 'database', 'search', 'duplicates'],

    /**
     * Navigation history stack for back-button functionality.
     * @type {Array<{screen: string, data: *}>}
     * @private
     */
    _navigationHistory: [],

    /**
     * Navigates to a screen.
     * Calls lifecycle hooks, updates DOM, and manages history.
     * @param {string} screen - The screen name to navigate to
     * @param {Object} [options={}] - Navigation options
     * @param {*} [options.data] - Data to pass to the screen's onEnter hook
     * @param {boolean} [options.pushHistory=true] - Whether to add to history stack
     * @fires App#screenChanged
     */
    navigateTo(screen, options = {}) {
        const { data = null, pushHistory = true } = options;

        // Validate screen name
        if (!this.SCREENS.includes(screen)) {
            console.error(`Invalid screen: ${screen}`);
            return;
        }

        // Don't navigate if already on this screen (unless fullscreen with different image)
        if (screen === this.state.screen && screen !== 'fullscreen') {
            return;
        }

        const previousScreen = this.state.screen;

        // Call onLeave hook for current screen
        this._callScreenHook(previousScreen, 'onLeave');

        // Save scroll position for scrollable screens
        this._saveScrollPosition(previousScreen);

        // Push to history if enabled and not going to fullscreen
        if (pushHistory && screen !== 'fullscreen' && previousScreen !== 'fullscreen') {
            this._navigationHistory.push({
                screen: previousScreen,
                data: null
            });
        }

        // Update state
        this.state.screen = screen;

        // Update DOM
        this._updateScreenVisibility(screen);
        this._updateToolbarVisibility(screen);

        // Call onEnter hook for new screen
        this._callScreenHook(screen, 'onEnter', data);

        // Restore scroll position if returning to a screen
        this._restoreScrollPosition(screen);

        // Emit event
        this.emit('screenChanged', screen, previousScreen);
    },

    /**
     * Navigates back to the previous screen in history.
     * If history is empty, navigates to gallery.
     */
    navigateBack() {
        if (this._navigationHistory.length > 0) {
            const previous = this._navigationHistory.pop();
            this.navigateTo(previous.screen, {
                data: previous.data,
                pushHistory: false
            });
        } else {
            // Default to gallery if no history
            this.navigateTo('gallery', { pushHistory: false });
        }
    },

    /**
     * Clears the navigation history.
     */
    clearHistory() {
        this._navigationHistory = [];
    },

    /**
     * Checks if there is navigation history to go back to.
     * @returns {boolean} True if back navigation is possible
     */
    canGoBack() {
        return this._navigationHistory.length > 0;
    },

    /**
     * Calls a lifecycle hook on a screen module if it exists.
     * @param {string} screen - The screen name
     * @param {string} hook - The hook name ('onEnter' or 'onLeave')
     * @param {*} [data] - Data to pass to the hook
     * @private
     */
    _callScreenHook(screen, hook, data) {
        const module = this._modules[screen];
        if (module && typeof module[hook] === 'function') {
            try {
                module[hook](data);
            } catch (error) {
                console.error(`Error in ${screen}.${hook}():`, error);
            }
        }
    },

    /**
     * Updates the visibility of screen elements in the DOM.
     * Sets the data-screen attribute on the app container.
     * @param {string} activeScreen - The screen to show
     * @private
     */
    _updateScreenVisibility(activeScreen) {
        const appEl = document.getElementById('app');
        appEl.dataset.screen = activeScreen;

        // Hide all screens, show active one
        for (const screen of this.SCREENS) {
            const screenEl = document.getElementById(`screen-${screen}`);
            if (screenEl) {
                screenEl.hidden = (screen !== activeScreen);
            }
        }
    },

    /**
     * Updates toolbar visibility based on the active screen.
     * Shows/hides toolbar groups using data-for-screen attributes.
     * Hides navigation buttons for the current screen.
     * Hides entire toolbar for fullscreen view.
     * @param {string} activeScreen - The current active screen
     * @private
     */
    _updateToolbarVisibility(activeScreen) {
        const toolbar = document.getElementById('toolbar');

        // Hide toolbar entirely in fullscreen mode
        if (activeScreen === 'fullscreen') {
            toolbar.hidden = true;
            return;
        }
        toolbar.hidden = false;

        // Show/hide toolbar groups based on data-for-screen attribute
        const groups = toolbar.querySelectorAll('[data-for-screen]');
        for (const group of groups) {
            const forScreens = group.dataset.forScreen.split(' ');
            group.hidden = !forScreens.includes(activeScreen);
        }

        // Hide navigation buttons for the current screen
        // (no point showing a button to go to the screen you're already on)
        const screenButtons = {
            'database': 'btn-database',
            'duplicates': 'btn-duplicates',
            'search': 'btn-filter'
        };

        for (const [screen, btnId] of Object.entries(screenButtons)) {
            const btn = document.getElementById(btnId);
            if (btn) {
                btn.hidden = (activeScreen === screen);
            }
        }
    },

    /**
     * Saves the current scroll position for a screen.
     * @param {string} screen - The screen name
     * @private
     */
    _saveScrollPosition(screen) {
        const screenEl = document.getElementById(`screen-${screen}`);
        if (screenEl) {
            // Find the scrollable container within the screen
            const scrollable = screenEl.querySelector('.gallery-container, .duplicates-container, .database-container, .search-container');
            if (scrollable) {
                this.state.scrollPositions[screen] = scrollable.scrollTop;
            }
        }
    },

    /**
     * Restores a previously saved scroll position for a screen.
     * @param {string} screen - The screen name
     * @private
     */
    _restoreScrollPosition(screen) {
        const savedPosition = this.state.scrollPositions[screen];
        if (savedPosition !== undefined) {
            const screenEl = document.getElementById(`screen-${screen}`);
            if (screenEl) {
                const scrollable = screenEl.querySelector('.gallery-container, .duplicates-container, .database-container, .search-container');
                if (scrollable) {
                    // Use requestAnimationFrame to ensure DOM is ready
                    requestAnimationFrame(() => {
                        scrollable.scrollTop = savedPosition;
                    });
                }
            }
        }
    },

    /**
     * Navigates to gallery screen.
     * Convenience method.
     */
    showGallery() {
        this.navigateTo('gallery');
    },

    /**
     * Navigates to fullscreen view for a specific image.
     * @param {string} imageId - The ID of the image to view
     */
    showFullscreen(imageId) {
        this.state.currentImageId = imageId;
        this.navigateTo('fullscreen', { data: imageId, pushHistory: false });
    },

    /**
     * Exits fullscreen view and returns to gallery.
     * Convenience method.
     */
    hideFullscreen() {
        this.navigateTo('gallery');
    },

    /**
     * Navigates to database screen.
     * Convenience method.
     */
    showDatabase() {
        this.navigateTo('database');
    },

    /**
     * Navigates to search screen.
     * Convenience method.
     */
    showSearch() {
        this.navigateTo('search');
    },

    /**
     * Navigates to duplicates screen.
     * Convenience method.
     */
    showDuplicates() {
        this.navigateTo('duplicates');
    },

    /**
     * Exits fullscreen view and returns to gallery.
     * Ensures the viewed image remains visible in gallery.
     */
    exitFullscreen() {
        if (this.state.screen === 'fullscreen') {
            this.navigateTo('gallery', { pushHistory: false });
        }
    },

    /* ----------------------------------------------------------------------
       API COMMUNICATION

       Simple fetch wrappers for backend calls. Uses mock data when
       App.mockMode is true.
       ---------------------------------------------------------------------- */

    /**
     * Base URL for API calls.
     * @type {string}
     */
    apiBase: '/api',

    /**
     * Makes an API request.
     * @param {string} endpoint - API endpoint (without base)
     * @param {Object} [options={}] - Fetch options
     * @returns {Promise<*>} Response data
     * @throws {Error} On network or API error
     */
    async api(endpoint, options = {}) {
        if (this.mockMode) {
            return this._mockApi(endpoint, options);
        }

        const url = this.apiBase + endpoint;
        const response = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });

        if (!response.ok) {
            throw new Error(`API error: ${response.status} ${response.statusText}`);
        }

        return response.json();
    },

    /**
     * GET request helper.
     * @param {string} endpoint - API endpoint
     * @returns {Promise<*>} Response data
     */
    async apiGet(endpoint) {
        return this.api(endpoint, { method: 'GET' });
    },

    /**
     * POST request helper.
     * @param {string} endpoint - API endpoint
     * @param {Object} data - Request body
     * @returns {Promise<*>} Response data
     */
    async apiPost(endpoint, data) {
        return this.api(endpoint, {
            method: 'POST',
            body: JSON.stringify(data)
        });
    },

    /**
     * DELETE request helper.
     * @param {string} endpoint - API endpoint
     * @returns {Promise<*>} Response data
     */
    async apiDelete(endpoint) {
        return this.api(endpoint, { method: 'DELETE' });
    },

    /* ----------------------------------------------------------------------
       Thumbnail Configuration
       ---------------------------------------------------------------------- */

    /**
     * Gets the thumbnail loading configuration.
     * Returns cached config (defaults until loadThumbnailConfig completes).
     * @returns {Object} Thumbnail config with concurrentRequests, extraRows, timeoutMs, scrollThrottleMs
     */
    getThumbnailConfig() {
        return this._thumbnailConfig;
    },

    /**
     * Loads thumbnail configuration from the backend.
     * Updates _thumbnailConfig with values from API.
     * Called during init; safe to call multiple times.
     * @returns {Promise<Object>} The loaded config
     */
    async loadThumbnailConfig() {
        try {
            const response = await this.apiGet('/config');
            this._thumbnailConfig = {
                concurrentRequests: response.thumbnail_concurrent_requests,
                extraRows: response.thumbnail_extra_rows,
                timeoutMs: response.thumbnail_timeout_ms,
                scrollThrottleMs: response.thumbnail_scroll_throttle_ms
            };
            console.log('Thumbnail config loaded:', this._thumbnailConfig);
        } catch (error) {
            console.warn('Failed to load thumbnail config, using defaults:', error);
        }
        return this._thumbnailConfig;
    },

    /* ----------------------------------------------------------------------
       Image Cache (for efficient incremental updates)
       ---------------------------------------------------------------------- */

    /**
     * Gets all images, using cache with delta updates for efficiency.
     * On first call, fetches all images and caches them.
     * On subsequent calls, fetches only changes since last sync.
     * @returns {Promise<Array<Object>>} Array of image objects
     */
    async getImages() {
        if (this.state.imageCache === null) {
            // First load - fetch all images
            return this._loadAllImages();
        } else {
            // Incremental update - fetch only changes
            return this._loadImagesDelta();
        }
    },

    /**
     * Forces a full reload of the image cache.
     * Use this when cache may be stale (e.g., after major changes).
     * @returns {Promise<Array<Object>>} Array of image objects
     */
    async reloadImages() {
        this.state.imageCache = null;
        this.state.imageCacheEpoch = null;
        return this._loadAllImages();
    },

    /**
     * Loads all images and initializes the cache.
     * @returns {Promise<Array<Object>>} Array of image objects
     * @private
     */
    async _loadAllImages() {
        const response = await this.apiGet('/images');

        // Build cache map from response
        this.state.imageCache = new Map();
        for (const img of response.images) {
            this.state.imageCache.set(img.id, img);
        }
        this.state.imageCacheEpoch = response.epoch;

        return Array.from(this.state.imageCache.values());
    },

    /**
     * Loads image changes since last sync and updates cache.
     * @returns {Promise<Array<Object>>} Array of all cached image objects
     * @private
     */
    async _loadImagesDelta() {
        const response = await this.apiGet(`/images?since=${encodeURIComponent(this.state.imageCacheEpoch)}`);

        // Apply updates to cache
        for (const img of response.updated) {
            this.state.imageCache.set(img.id, img);
        }

        // Remove deleted images from cache
        for (const id of response.deleted_ids) {
            this.state.imageCache.delete(id);
        }

        // Update epoch
        this.state.imageCacheEpoch = response.epoch;

        return Array.from(this.state.imageCache.values());
    },

    /**
     * Gets the current cached image count without fetching.
     * @returns {number} Number of cached images, or 0 if cache not loaded
     */
    getCachedImageCount() {
        return this.state.imageCache ? this.state.imageCache.size : 0;
    },

    /* ----------------------------------------------------------------------
       Mock API (for frontend development)
       ---------------------------------------------------------------------- */

    /**
     * Mock API handler for development without backend.
     * @param {string} endpoint - API endpoint
     * @param {Object} options - Fetch options
     * @returns {Promise<*>} Mock response data
     * @private
     */
    async _mockApi(endpoint, options) {
        // Simulate network delay
        await new Promise(r => setTimeout(r, 100));

        const method = options.method || 'GET';

        // Route to mock handlers
        if (endpoint === '/images' && method === 'GET') {
            return this._mockImages;
        }
        if (endpoint === '/folders' && method === 'GET') {
            return this._mockFolders;
        }
        if (endpoint === '/folders' && method === 'POST') {
            const data = JSON.parse(options.body);
            this._mockFolders.push({ path: data.path, count: 0 });
            return { success: true };
        }
        if (endpoint.startsWith('/folders/') && method === 'DELETE') {
            const path = decodeURIComponent(endpoint.slice(9));
            this._mockFolders = this._mockFolders.filter(f => f.path !== path);
            return { success: true };
        }
        if (endpoint === '/status' && method === 'GET') {
            // Mock status: simulate occasional updating state
            const isUpdating = Math.random() < 0.3; // 30% chance of updating
            return {
                status: isUpdating ? 'updating' : 'up_to_date',
                indexing_queue: isUpdating ? Math.floor(Math.random() * 50) : 0,
                embedding_queue: isUpdating ? Math.floor(Math.random() * 100) : 0
            };
        }
        if (endpoint === '/rescan' && method === 'POST') {
            return { success: true };
        }
        if (endpoint.startsWith('/duplicates') && method === 'GET') {
            // Parse level from query string
            const levelMatch = endpoint.match(/level=(\d)/);
            const level = levelMatch ? parseInt(levelMatch[1], 10) : 0;
            return this._getMockDuplicates(level);
        }
        if (endpoint.startsWith('/images/') && method === 'GET') {
            const id = endpoint.split('/')[2];
            return this._mockImages.find(img => img.id === id) || null;
        }
        if (endpoint.startsWith('/images/') && method === 'POST') {
            // Update image (description, rating)
            return { success: true };
        }
        if (endpoint.startsWith('/images/') && method === 'DELETE') {
            const id = endpoint.split('/')[2];
            this._mockImages = this._mockImages.filter(img => img.id !== id);
            return { success: true };
        }
        if (endpoint === '/stats' && method === 'GET') {
            return { totalImages: this._mockImages.length, totalFolders: this._mockFolders.length };
        }
        if (endpoint.match(/\/images\/[^/]+\/histogram$/) && method === 'GET') {
            // Return mock histogram data URLs (1x1 transparent PNGs)
            const transparentPng = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';
            return { r: transparentPng, g: transparentPng, b: transparentPng };
        }

        console.warn(`Mock API: unhandled ${method} ${endpoint}`);
        return null;
    },

    /**
     * Mock image data for development.
     * @type {Array<Object>}
     * @private
     */
    _mockImages: [
        { id: '1', basename: 'sunset.jpg', path: 'photos/sunset.jpg', width: 1920, height: 1080, size: 245000, timestamp: '2024-06-15T18:30:00', description: '', rating: '' },
        { id: '2', basename: 'mountain.jpg', path: 'photos/mountain.jpg', width: 2560, height: 1440, size: 380000, timestamp: '2024-06-14T10:15:00', description: '', rating: '' },
        { id: '3', basename: 'beach.jpg', path: 'photos/beach.jpg', width: 1920, height: 1080, size: 220000, timestamp: '2024-06-13T14:45:00', description: '', rating: '' },
        { id: '4', basename: 'forest.jpg', path: 'photos/forest.jpg', width: 1920, height: 1280, size: 310000, timestamp: '2024-06-12T09:20:00', description: '', rating: '' },
        { id: '5', basename: 'cityscape.jpg', path: 'photos/cityscape.jpg', width: 2048, height: 1365, size: 420000, timestamp: '2024-06-11T21:00:00', description: '', rating: '' },
        { id: '6', basename: 'waterfall.jpg', path: 'photos/waterfall.jpg', width: 1600, height: 1200, size: 195000, timestamp: '2024-06-10T14:30:00', description: '', rating: '' },
        { id: '7', basename: 'desert.jpg', path: 'photos/desert.jpg', width: 1920, height: 1080, size: 175000, timestamp: '2024-06-09T16:45:00', description: '', rating: '' },
        { id: '8', basename: 'lake.jpg', path: 'photos/lake.jpg', width: 2560, height: 1600, size: 445000, timestamp: '2024-06-08T07:15:00', description: '', rating: '' },
        { id: '9', basename: 'aurora.jpg', path: 'photos/aurora.jpg', width: 1920, height: 1080, size: 285000, timestamp: '2024-06-07T23:30:00', description: '', rating: '' },
        { id: '10', basename: 'flower.jpg', path: 'photos/flower.jpg', width: 1200, height: 1200, size: 125000, timestamp: '2024-06-06T11:00:00', description: '', rating: '' },
        { id: '11', basename: 'canyon.jpg', path: 'photos/canyon.jpg', width: 1920, height: 1280, size: 335000, timestamp: '2024-06-05T15:20:00', description: '', rating: '' },
        { id: '12', basename: 'snow.jpg', path: 'photos/snow.jpg', width: 1800, height: 1200, size: 210000, timestamp: '2024-06-04T08:45:00', description: '', rating: '' },
        { id: '13', basename: 'river.jpg', path: 'photos/river.jpg', width: 2048, height: 1152, size: 295000, timestamp: '2024-06-03T17:00:00', description: '', rating: '' },
        { id: '14', basename: 'clouds.jpg', path: 'photos/clouds.jpg', width: 1920, height: 1080, size: 165000, timestamp: '2024-06-02T12:30:00', description: '', rating: '' },
        { id: '15', basename: 'garden.jpg', path: 'photos/garden.jpg', width: 1600, height: 1067, size: 230000, timestamp: '2024-06-01T10:15:00', description: '', rating: '' },
        { id: '16', basename: 'lighthouse.jpg', path: 'photos/lighthouse.jpg', width: 1920, height: 1440, size: 275000, timestamp: '2024-05-31T19:45:00', description: '', rating: '' },
        { id: '17', basename: 'meadow.jpg', path: 'photos/meadow.jpg', width: 2560, height: 1440, size: 390000, timestamp: '2024-05-30T14:00:00', description: '', rating: '' },
        { id: '18', basename: 'ruins.jpg', path: 'photos/ruins.jpg', width: 1920, height: 1280, size: 320000, timestamp: '2024-05-29T16:30:00', description: '', rating: '' },
        { id: '19', basename: 'stars.jpg', path: 'photos/stars.jpg', width: 2048, height: 1365, size: 255000, timestamp: '2024-05-28T22:00:00', description: '', rating: '' },
        { id: '20', basename: 'village.jpg', path: 'photos/village.jpg', width: 1800, height: 1200, size: 285000, timestamp: '2024-05-27T09:30:00', description: '', rating: '' },
    ],

    /**
     * Mock folder data for development.
     * @type {Array<Object>}
     * @private
     */
    _mockFolders: [
        { path: 'photos', count: 59 }
    ],

    /**
     * Returns mock duplicate groups for a given similarity level.
     * @param {number} level - Similarity level (0=identical, 1=perceptual, 2=similar, 3=related)
     * @returns {Object} Mock response with groups array
     * @private
     */
    _getMockDuplicates(level) {
        // Helper to create image objects with duplicate-relevant fields
        const img = (id, basename, path, width, height, size, laplacian, lossless = false) => ({
            id, basename, path, width, height, size,
            laplacian_variance: laplacian,
            lossless,
            timestamp: '2024-06-15T12:00:00'
        });

        // Level 0: Identical (same checksum) - exact copies
        const identical = [
            {
                images: [
                    img('101', 'sunset.jpg', 'photos/sunset.jpg', 1920, 1080, 245000, 850, false),
                    img('102', 'sunset_copy.jpg', 'backup/sunset_copy.jpg', 1920, 1080, 245000, 850, false),
                ]
            },
            {
                images: [
                    img('103', 'beach.jpg', 'photos/beach.jpg', 1920, 1080, 220000, 720, false),
                    img('104', 'beach (1).jpg', 'downloads/beach (1).jpg', 1920, 1080, 220000, 720, false),
                    img('105', 'beach_backup.jpg', 'backup/beach_backup.jpg', 1920, 1080, 220000, 720, false),
                ]
            }
        ];

        // Level 1: Perceptual (same image, different encoding/size)
        const perceptual = [
            ...identical,
            {
                images: [
                    img('201', 'mountain_4k.png', 'photos/mountain_4k.png', 3840, 2160, 8500000, 920, true),
                    img('202', 'mountain_hd.jpg', 'photos/mountain_hd.jpg', 1920, 1080, 380000, 890, false),
                    img('203', 'mountain_thumb.jpg', 'thumbs/mountain_thumb.jpg', 640, 360, 45000, 650, false),
                ]
            },
            {
                images: [
                    img('204', 'flower_raw.png', 'raw/flower_raw.png', 4000, 4000, 12000000, 980, true),
                    img('205', 'flower.jpg', 'photos/flower.jpg', 1200, 1200, 125000, 870, false),
                ]
            },
            {
                images: [
                    img('206', 'cityscape_original.tiff', 'archive/cityscape_original.tiff', 4096, 2730, 15000000, 950, true),
                    img('207', 'cityscape.jpg', 'photos/cityscape.jpg', 2048, 1365, 420000, 920, false),
                    img('208', 'cityscape_web.jpg', 'web/cityscape_web.jpg', 1024, 683, 95000, 780, false),
                    img('209', 'cityscape_social.jpg', 'social/cityscape_social.jpg', 800, 533, 65000, 720, false),
                ]
            }
        ];

        // Level 2: Similar (shot sequences, minor variations)
        const similar = [
            ...perceptual,
            {
                images: [
                    img('301', 'portrait_001.jpg', 'session/portrait_001.jpg', 2560, 1707, 520000, 890, false),
                    img('302', 'portrait_002.jpg', 'session/portrait_002.jpg', 2560, 1707, 515000, 920, false),
                    img('303', 'portrait_003.jpg', 'session/portrait_003.jpg', 2560, 1707, 518000, 850, false),
                    img('304', 'portrait_004.jpg', 'session/portrait_004.jpg', 2560, 1707, 522000, 880, false),
                    img('305', 'portrait_005.jpg', 'session/portrait_005.jpg', 2560, 1707, 510000, 910, false),
                ]
            },
            {
                images: [
                    img('306', 'sunset_wide.jpg', 'photos/sunset_wide.jpg', 2560, 1080, 380000, 870, false),
                    img('307', 'sunset_cropped.jpg', 'photos/sunset_cropped.jpg', 1920, 1080, 290000, 860, false),
                ]
            },
            {
                images: [
                    img('308', 'product_angle1.jpg', 'products/product_angle1.jpg', 2000, 2000, 450000, 950, false),
                    img('309', 'product_angle2.jpg', 'products/product_angle2.jpg', 2000, 2000, 460000, 940, false),
                    img('310', 'product_angle3.jpg', 'products/product_angle3.jpg', 2000, 2000, 455000, 960, false),
                ]
            }
        ];

        // Level 3: Related (thematically similar)
        const related = [
            ...similar,
            {
                images: [
                    img('401', 'beach_hawaii.jpg', 'vacation/beach_hawaii.jpg', 1920, 1080, 245000, 850, false),
                    img('402', 'beach_florida.jpg', 'vacation/beach_florida.jpg', 2048, 1152, 280000, 870, false),
                    img('403', 'beach_caribbean.jpg', 'vacation/beach_caribbean.jpg', 1800, 1200, 260000, 830, false),
                    img('404', 'beach_california.jpg', 'vacation/beach_california.jpg', 1920, 1280, 290000, 880, false),
                ]
            },
            {
                images: [
                    img('405', 'cat_sleeping.jpg', 'pets/cat_sleeping.jpg', 1600, 1200, 185000, 780, false),
                    img('406', 'cat_yawning.jpg', 'pets/cat_yawning.jpg', 1600, 1200, 195000, 820, false),
                    img('407', 'cat_playing.jpg', 'pets/cat_playing.jpg', 1920, 1080, 210000, 750, false),
                ]
            },
            {
                images: [
                    img('408', 'coffee_latte.jpg', 'food/coffee_latte.jpg', 1200, 1200, 145000, 890, false),
                    img('409', 'coffee_cappuccino.jpg', 'food/coffee_cappuccino.jpg', 1200, 1200, 150000, 910, false),
                ]
            },
            {
                images: [
                    img('410', 'autumn_park.jpg', 'seasons/autumn_park.jpg', 2560, 1440, 420000, 920, false),
                    img('411', 'autumn_forest.jpg', 'seasons/autumn_forest.jpg', 2560, 1440, 450000, 940, false),
                    img('412', 'autumn_road.jpg', 'seasons/autumn_road.jpg', 1920, 1080, 310000, 880, false),
                    img('413', 'autumn_leaves.jpg', 'seasons/autumn_leaves.jpg', 1800, 1200, 280000, 850, false),
                    img('414', 'autumn_bench.jpg', 'seasons/autumn_bench.jpg', 2048, 1365, 360000, 900, false),
                    img('415', 'autumn_lake.jpg', 'seasons/autumn_lake.jpg', 2560, 1600, 480000, 930, false),
                ]
            }
        ];

        const levelGroups = [identical, perceptual, similar, related];
        const groups = levelGroups[level] || [];

        // Ensure any mock duplicates returned are also present in the /images list,
        // otherwise the Gallery screen cannot display them when filtering by IDs.
        for (const group of groups) {
            for (const imgObj of (group.images || [])) {
                if (!imgObj || !imgObj.id) continue;
                const exists = this._mockImages.some(i => String(i.id) === String(imgObj.id));
                if (!exists) {
                    this._mockImages.push({
                        id: imgObj.id,
                        basename: imgObj.basename,
                        path: imgObj.path,
                        width: imgObj.width,
                        height: imgObj.height,
                        size: imgObj.size,
                        timestamp: imgObj.timestamp,
                        description: '',
                        rating: ''
                    });
                }
            }
        }

        return { groups, status: 'done' };
    },

    /* ----------------------------------------------------------------------
       TOOLBAR MANAGEMENT

       Binds event listeners to toolbar buttons and manages button states.
       ---------------------------------------------------------------------- */

    /**
     * Initialises toolbar event listeners.
     * Called once during app initialization.
     * @private
     */
    _initToolbar() {
        // Theme toggle
        this._bindBtn('btn-theme', () => this.toggleTheme());

        // Navigation buttons
        this._bindBtn('btn-database', () => this.showDatabase());
        this._bindBtn('btn-duplicates', () => this.showDuplicates());
        this._bindBtn('btn-filter', () => this._handleFilterClick());
        this._bindBtn('btn-back-gallery', () => this.showGallery());

        // Gallery controls
        this._bindBtn('btn-thumb-smaller', () => this.setThumbnailSize(this.state.thumbnailSize - 50));
        this._bindBtn('btn-thumb-larger', () => this.setThumbnailSize(this.state.thumbnailSize + 50));
        this._bindBtn('btn-fullscreen', () => this._handleFullscreenClick());
        this._bindBtn('btn-reveal-folder', () => this._handleRevealFolderClick());
        this._bindBtn('btn-rotate-ccw', () => this._handleRotateClick('ccw'));
        this._bindBtn('btn-rotate-cw', () => this._handleRotateClick('cw'));
        this._bindBtn('btn-select-all', () => this.emit('selectAll'));
        this._bindBtn('btn-clear-selection', () => this.clearSelection());

        // Sort controls
        this._bindBtn('btn-sort-date', () => this.setSortBy('date'));
        this._bindBtn('btn-sort-rating', () => this.setSortBy('rating'));
        this._bindBtn('btn-sort-content', () => this.setSortBy('content'));
        this._bindBtn('btn-sort-direction', () => this.toggleSortDirection());

        // Duplicates controls
        this._bindBtn('btn-dup-thumb-smaller', () => this.setThumbnailSize(this.state.thumbnailSize - 50));
        this._bindBtn('btn-dup-thumb-larger', () => this.setThumbnailSize(this.state.thumbnailSize + 50));

        // Similarity slider
        const slider = document.getElementById('similarity-slider');
        if (slider) {
            slider.addEventListener('input', () => this._handleSimilarityChange(slider.value));
        }

        // Subscribe to state changes to update button states
        this.on('selectionChanged', () => this._updateToolbarStates());
        this.on('sortChanged', () => this._updateSortButtons());
        this.on('filterChanged', () => this._updateFilterButton());
        this.on('themeChanged', () => this._updateThemeButton());
    },

    /**
     * Initialises global keyboard shortcuts for navigation.
     * @private
     */
    _initGlobalKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Ignore if typing in an input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            // Ignore if no modifier key (we only handle Ctrl/Cmd shortcuts here)
            if (!e.ctrlKey && !e.metaKey) return;

            switch (e.key.toLowerCase()) {
                case 'g':
                    e.preventDefault();
                    this.showGallery();
                    break;
                case 'm':
                    e.preventDefault();
                    this.showDatabase();
                    break;
                case 'd':
                    e.preventDefault();
                    this.showDuplicates();
                    break;
                case 'f':
                    e.preventDefault();
                    this.showSearch();
                    break;
            }
        });
    },

    /**
     * Binds a click handler to a button by ID.
     * @param {string} id - Button element ID
     * @param {Function} handler - Click handler
     * @private
     */
    _bindBtn(id, handler) {
        const btn = document.getElementById(id);
        if (btn) {
            btn.addEventListener('click', handler);
        }
    },

    /**
     * Handles filter button click.
     * Opens search screen or clears filter if one is active.
     * @private
     */
    _handleFilterClick() {
        // Always open search screen - user can clear or refine filter there
        this.showSearch();
    },

    /**
     * Handles fullscreen button click.
     * Opens fullscreen view if exactly one image is selected.
     * @private
     */
    _handleFullscreenClick() {
        if (this.state.selectedImages.length === 1) {
            this.showFullscreen(this.state.selectedImages[0]);
        }
    },

    /**
     * Handles reveal folder button click.
     * Opens the containing folder for the selected image.
     * @private
     */
    async _handleRevealFolderClick() {
        if (this.state.selectedImages.length === 1) {
            const imageId = this.state.selectedImages[0];
            try {
                await this.apiPost(`/images/${imageId}/reveal`, {});
            } catch (error) {
                console.error('Failed to open folder:', error);
                this.showError('Failed to open containing folder.');
            }
        }
    },

    /**
     * Handles rotate button click.
     * Rotates all selected images in the specified direction.
     * @param {string} direction - 'cw' for clockwise, 'ccw' for counter-clockwise
     * @private
     */
    async _handleRotateClick(direction) {
        const selectedIds = [...this.state.selectedImages];
        if (selectedIds.length === 0) {
            return;
        }

        try {
            // Rotate all selected images in one batch request
            const result = await this.apiPost('/images/rotate', {
                image_ids: selectedIds,
                direction: direction
            });

            // Emit event for each successfully rotated image
            if (result && result.rotated) {
                for (const imageId of result.rotated) {
                    this.emit('imageRotated', imageId);
                }
            }

            // Report any failures
            if (result && result.results) {
                const failed = selectedIds.filter(id => !result.results[id]);
                if (failed.length > 0) {
                    console.error('Failed to rotate images:', failed);
                    this.showError(`Failed to rotate ${failed.length} image(s).`);
                }
            }
        } catch (error) {
            console.error('Failed to rotate images:', error);
            this.showError('Failed to rotate images.');
        }
    },

    /**
     * Handles similarity slider changes.
     * Updates label and emits event for duplicates screen.
     * @param {string} value - Slider value (0-3)
     * @private
     */
    _handleSimilarityChange(value) {
        const labels = ['Identical', 'Perceptual', 'Similar', 'Related'];
        const label = document.getElementById('similarity-label');
        if (label) {
            label.textContent = labels[value] || 'Identical';
        }
        this.emit('similarityChanged', parseInt(value, 10));
    },

    /**
     * Updates toolbar button states based on current selection.
     * @private
     */
    _updateToolbarStates() {
        const selCount = this.state.selectedImages.length;

        // Fullscreen button: enabled only when exactly one image selected
        const fullscreenBtn = document.getElementById('btn-fullscreen');
        if (fullscreenBtn) {
            fullscreenBtn.disabled = selCount !== 1;
        }

        // Reveal folder button: enabled only when exactly one image selected
        const revealBtn = document.getElementById('btn-reveal-folder');
        if (revealBtn) {
            revealBtn.disabled = selCount !== 1;
        }

        // Rotate buttons: enabled when at least one image selected
        const rotateCcwBtn = document.getElementById('btn-rotate-ccw');
        const rotateCwBtn = document.getElementById('btn-rotate-cw');
        if (rotateCcwBtn) {
            rotateCcwBtn.disabled = selCount === 0;
        }
        if (rotateCwBtn) {
            rotateCwBtn.disabled = selCount === 0;
        }
    },

    /**
     * Updates sort button active states.
     * @private
     */
    _updateSortButtons() {
        const sortBy = this.state.sortBy;
        const direction = this.state.sortDirection;

        // Update active states
        ['date', 'rating', 'content'].forEach(type => {
            const btn = document.getElementById(`btn-sort-${type}`);
            if (btn) {
                btn.classList.toggle('active', sortBy === type);
            }
        });

        // Update direction icon
        const dirBtn = document.getElementById('btn-sort-direction');
        if (dirBtn) {
            const icon = dirBtn.querySelector('.material-symbols-outlined');
            if (icon) {
                icon.textContent = direction === 'asc' ? 'arrow_upward' : 'arrow_downward';
            }
        }
    },

    /**
     * Updates filter button to show active state.
     * @private
     */
    _updateFilterButton() {
        const btn = document.getElementById('btn-filter');
        if (btn) {
            // Toggle active class to indicate filter is active (styling only)
            btn.classList.toggle('active', this.hasActiveFilter());
        }
    },

    /**
     * Updates theme button icon.
     * @private
     */
    _updateThemeButton() {
        const btn = document.getElementById('btn-theme');
        if (btn) {
            const icon = btn.querySelector('.material-symbols-outlined');
            if (icon) {
                icon.textContent = this.state.theme === 'light' ? 'dark_mode' : 'light_mode';
            }
        }
    },

    /* ----------------------------------------------------------------------
       STATUS & NOTIFICATIONS

       Loading indicators and error messages.
       ---------------------------------------------------------------------- */

    /**
     * Shows a loading indicator with optional message.
     * @param {string} [message='Loading…'] - Message to display
     */
    showLoading(message = 'Loading…') {
        let overlay = document.getElementById('loading-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'loading-overlay';
            overlay.className = 'loading-overlay';
            overlay.innerHTML = `
                <div class="loading-spinner"></div>
                <div class="loading-message"></div>
            `;
            document.body.appendChild(overlay);
        }
        overlay.querySelector('.loading-message').textContent = message;
        overlay.classList.add('visible');
    },

    /**
     * Hides the loading indicator.
     */
    hideLoading() {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            overlay.classList.remove('visible');
        }
    },

    /**
     * Shows an error message to the user.
     * @param {string} message - Error message to display
     * @param {number} [duration=4000] - How long to show the message in ms
     */
    showError(message, duration = 4000) {
        let toast = document.getElementById('error-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'error-toast';
            toast.className = 'error-toast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.classList.add('visible');

        // Auto-hide after duration
        clearTimeout(toast._hideTimeout);
        toast._hideTimeout = setTimeout(() => {
            toast.classList.remove('visible');
        }, duration);
    },

    /* ----------------------------------------------------------------------
       DIALOG SYSTEM

       Simple modal dialogs for confirmations and emoji picker.
       ---------------------------------------------------------------------- */

    /**
     * Shows a confirmation dialog.
     * @param {string} title - Dialog title
     * @param {string} message - Dialog message
     * @returns {Promise<boolean>} Resolves true if confirmed, false if cancelled
     */
    confirm(title, message) {
        return new Promise(resolve => {
            const dialog = document.getElementById('dialog-confirm');
            const titleEl = document.getElementById('dialog-confirm-title');
            const msgEl = document.getElementById('dialog-confirm-message');
            const okBtn = document.getElementById('dialog-confirm-ok');
            const cancelBtn = document.getElementById('dialog-confirm-cancel');

            titleEl.textContent = title;
            msgEl.textContent = message;

            const cleanup = (result) => {
                okBtn.removeEventListener('click', onOk);
                cancelBtn.removeEventListener('click', onCancel);
                dialog.removeEventListener('cancel', onCancel);
                dialog.close();
                resolve(result);
            };

            const onOk = () => cleanup(true);
            const onCancel = () => cleanup(false);

            okBtn.addEventListener('click', onOk);
            cancelBtn.addEventListener('click', onCancel);
            dialog.addEventListener('cancel', onCancel); // Escape key

            dialog.showModal();
        });
    },

    /**
     * Shows the emoji picker dialog.
     * @param {Function} onSelect - Callback when an emoji is selected
     */
    showEmojiPicker(onSelect) {
        const dialog = document.getElementById('dialog-emoji');
        const grid = document.getElementById('emoji-grid');
        const closeBtn = document.getElementById('dialog-emoji-close');

        // Populate grid if empty
        if (grid.children.length === 0) {
            this._populateEmojiGrid(grid);
        }

        // Handle emoji selection
        const handleClick = (e) => {
            if (e.target.classList.contains('emoji-btn')) {
                onSelect(e.target.textContent);
            }
        };

        const cleanup = () => {
            grid.removeEventListener('click', handleClick);
            closeBtn.removeEventListener('click', cleanup);
            dialog.removeEventListener('cancel', cleanup);
            dialog.close();
        };

        grid.addEventListener('click', handleClick);
        closeBtn.addEventListener('click', cleanup);
        dialog.addEventListener('cancel', cleanup);

        dialog.showModal();
    },

    /**
     * Populates the emoji grid with common rating emoji.
     * @param {HTMLElement} grid - The grid container element
     * @private
     */
    _populateEmojiGrid(grid) {
        const sections = [
            {
                title: 'Hearts',
                items: {
                    '❤️': 'Red heart',
                    '🧡': 'Orange heart',
                    '💛': 'Yellow heart',
                    '💚': 'Green heart',
                    '💙': 'Blue heart',
                    '💜': 'Purple heart'
                }
            },
            {
                title: 'Reactions',
                items: {
                    '👍': 'Thumbs up',
                    '👎': 'Thumbs down',
                    '👌': 'OK hand',
                    '🔥': 'Fire / hot',
                    '✅': 'Check mark / success',
                    '❌': 'Cross mark / failure',
                    '❓': 'Question mark / unknown'
                }
            },
            {
                title: 'Creative',
                items: {
                    '📸': 'Camera / photography',
                    '🎨': 'Artist palette / art',
                    '🏆': 'Trophy / winner',
                    '💎': 'Gem / diamond',
                    '🎉': 'Party popper / celebration'
                }
            },
            {
                title: 'Faces',
                items: {
                    '😀': 'Grinning face',
                    '😍': 'Smiling face with heart-eyes',
                    '🤩': 'Star-struck',
                    '😎': 'Smiling face with sunglasses',
                    '👶': 'Baby'
                }
            },
            {
                title: 'Nature',
                items: {
                    '🌅': 'Sunrise',
                    '🌄': 'Sunrise over mountains',
                    '🏔️': 'Snow-capped mountain',
                    '🌊': 'Ocean wave',
                    '☀️': 'Sunny',
                    '🌤️': 'Partly sunny',
                    '⛅': 'Sun behind cloud',
                    '☁️': 'Cloudy',
                    '🌧️': 'Rain',
                    '⛈️': 'Thunderstorm',
                    '🌩️': 'Lightning',
                    '❄️': 'Snowflake',
                    '🌈': 'Rainbow',
                    '🌙': 'Crescent moon',
                    '⭐': 'Star'
                }
            },
            {
                title: 'Sports',
                items: {
                    '🏈': 'American football',
                    '🏹': 'Archery',
                    '🏸': 'Badminton',
                    '🩰': 'Ballet',
                    '⚾': 'Baseball',
                    '🏀': 'Basketball',
                    '🎳': 'Bowling',
                    '🥊': 'Boxing',
                    '🏏': 'Cricket',
                    '🚴': 'Cycling',
                    '💃': 'Dancing',
                    '🏑': 'Field hockey',
                    '⚽': 'Football / soccer',
                    '⛳': 'Golf',
                    '🏒': 'Ice hockey',
                    '🥋': 'Martial arts',
                    '🚣': 'Rowing',
                    '🏉': 'Rugby',
                    '🏃': 'Running',
                    '⛷️': 'Skiing',
                    '🏂': 'Snowboarding',
                    '🏄': 'Surfing',
                    '🏊': 'Swimming',
                    '🏓': 'Table tennis',
                    '🎾': 'Tennis',
                    '🏐': 'Volleyball',
                    '🏋️': 'Weightlifting'
                }
            }
        ];

        grid.innerHTML = '';

        for (let i = 0; i < sections.length; i++) {
            const section = sections[i];

            if (i > 0) {
                const divider = document.createElement('div');
                divider.className = 'emoji-divider';
                divider.textContent = section.title;
                grid.appendChild(divider);
            }

            for (const [e, desc] of Object.entries(section.items)) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'emoji-btn';
                btn.textContent = e;
                btn.title = desc;
                grid.appendChild(btn);
            }
        }
    },

    /**
     * Closes any open dialog.
     * @param {string} dialogId - The dialog element ID
     */
    closeDialog(dialogId) {
        const dialog = document.getElementById(dialogId);
        if (dialog && dialog.open) {
            dialog.close();
        }
    },

    /* ----------------------------------------------------------------------
       UTILITY FUNCTIONS

       DOM helpers, timing utilities, URL builders, and formatters.
       ---------------------------------------------------------------------- */

    /**
     * Shorthand for getElementById.
     * @param {string} id - Element ID
     * @returns {HTMLElement|null}
     */
    $(id) {
        return document.getElementById(id);
    },

    /**
     * Creates a debounced version of a function.
     * @param {Function} fn - Function to debounce
     * @param {number} ms - Delay in milliseconds
     * @returns {Function} Debounced function
     */
    debounce(fn, ms) {
        let timeout;
        return (...args) => {
            clearTimeout(timeout);
            timeout = setTimeout(() => fn(...args), ms);
        };
    },

    /**
     * Creates a throttled version of a function.
     * @param {Function} fn - Function to throttle
     * @param {number} ms - Minimum interval in milliseconds
     * @returns {Function} Throttled function
     */
    throttle(fn, ms) {
        let last = 0;
        return (...args) => {
            const now = Date.now();
            if (now - last >= ms) {
                last = now;
                fn(...args);
            }
        };
    },

    /**
     * Builds a thumbnail URL for an image.
     * @param {string} imageId - The image ID
     * @param {number} [size] - Thumbnail size (defaults to current setting)
     * @returns {string} Thumbnail URL
     */
    thumbnailUrl(imageId, size) {
        size = size || this.state.thumbnailSize;
        if (this.mockMode) {
            // Return placeholder for mock mode
            return `https://picsum.photos/seed/${imageId}/${size}/${size}`;
        }
        return `${this.apiBase}/images/${imageId}/thumbnail?size=${size}`;
    },

    /**
     * Builds a full image URL.
     * @param {string} imageId - The image ID
     * @returns {string} Full image URL
     */
    imageUrl(imageId) {
        if (this.mockMode) {
            return `https://picsum.photos/seed/${imageId}/1920/1080`;
        }
        return `${this.apiBase}/images/${imageId}/full`;
    },

    /**
     * Formats a file size in bytes to a human-readable string.
     * @param {number} bytes - File size in bytes
     * @returns {string} Formatted size (e.g., "1.5 MB")
     */
    formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
        return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
    },

    /**
     * Formats a date string or timestamp to a localised string.
     * @param {string|number|Date} date - Date to format
     * @returns {string} Formatted date
     */
    formatDate(date) {
        if (!date) return 'Unknown';
        const d = new Date(date);
        if (isNaN(d.getTime())) return 'Invalid date';
        return d.toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
    },

    /**
     * Formats image dimensions.
     * @param {number} width - Width in pixels
     * @param {number} height - Height in pixels
     * @returns {string} Formatted dimensions (e.g., "1920 × 1080")
     */
    formatDimensions(width, height) {
        return `${width} × ${height}`;
    },

    /**
     * Escapes HTML special characters in a string.
     * @param {string} str - String to escape
     * @returns {string} Escaped string
     */
    escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    },

    /**
     * Creates an HTML element with optional attributes and children.
     * @param {string} tag - Element tag name
     * @param {Object} [attrs] - Attributes to set
     * @param {...(string|HTMLElement)} children - Child elements or text
     * @returns {HTMLElement}
     */
    createElement(tag, attrs = {}, ...children) {
        const el = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (key === 'className') {
                el.className = value;
            } else if (key.startsWith('data')) {
                el.dataset[key.slice(4).toLowerCase()] = value;
            } else {
                el.setAttribute(key, value);
            }
        }
        for (const child of children) {
            if (typeof child === 'string') {
                el.appendChild(document.createTextNode(child));
            } else if (child) {
                el.appendChild(child);
            }
        }
        return el;
    },

    /* ----------------------------------------------------------------------
       MODULE REGISTRATION & INITIALIZATION

       Allows screen modules to register themselves and handles app startup.
       ---------------------------------------------------------------------- */

    /**
     * Registers a screen module.
     * Modules should call this to register their lifecycle hooks.
     * @param {string} name - Module name (matches screen name)
     * @param {Object} module - Module object with onEnter/onLeave hooks
     * @param {Function} [module.onEnter] - Called when screen becomes active
     * @param {Function} [module.onLeave] - Called when leaving the screen
     * @param {Function} [module.init] - Called once during app initialization
     */
    registerModule(name, module) {
        this._modules[name] = module;
    },

    /**
     * Registers a callback to run when the app is ready.
     * If already ready, callback runs immediately.
     * @param {Function} callback - Function to call when ready
     */
    ready(callback) {
        if (this._isReady) {
            callback();
        } else {
            this._readyCallbacks.push(callback);
        }
    },

    /**
     * Initialises the application.
     * Called once when DOM is ready.
     * @private
     */
    _init() {
        // Load persisted state
        this._loadPersistedState();

        // Load thumbnail config from backend (async, uses defaults until loaded)
        if (!this.mockMode) {
            this.loadThumbnailConfig();
        }

        // Prime mock data so Gallery has a stable dataset from the outset.
        // Level 3 includes all lower levels in the current mock generator.
        if (this.mockMode) {
            this._getMockDuplicates(3);
        }

        // Apply initial theme
        document.getElementById('app').dataset.theme = this.state.theme;

        // Initialise toolbar
        this._initToolbar();

        // Initialise global keyboard shortcuts
        this._initGlobalKeyboardShortcuts();

        // Update initial toolbar states
        this._updateThemeButton();
        this._updateSortButtons();
        this._updateFilterButton();
        this._updateToolbarStates();

        // Initialise registered modules
        for (const [name, module] of Object.entries(this._modules)) {
            if (typeof module.init === 'function') {
                try {
                    module.init();
                } catch (error) {
                    console.error(`Error initializing module '${name}':`, error);
                }
            }
        }

        // Determine initial screen
        this._determineInitialScreen();

        // Mark as ready and run callbacks
        this._isReady = true;
        for (const callback of this._readyCallbacks) {
            try {
                callback();
            } catch (error) {
                console.error('Error in ready callback:', error);
            }
        }
        this._readyCallbacks = [];

        console.log('Imaginary initialised', this.mockMode ? '(mock mode)' : '');
    },

    /**
     * Determines and navigates to the initial screen.
     * Shows database screen if no images, otherwise gallery.
     * @private
     */
    async _determineInitialScreen() {
        try {
            const stats = await this.apiGet('/stats');
            if (stats.totalImages === 0) {
                this.navigateTo('database', { pushHistory: false });
            } else {
                this.navigateTo('gallery', { pushHistory: false });
            }
        } catch (error) {
            console.error('Error checking database:', error);
            // Default to gallery on error
            this.navigateTo('gallery', { pushHistory: false });
        }
    }
};

/* ==========================================================================
   DOM READY - Start the application
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
    App._init();
});
