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
 * @module core
 */

/* ==========================================================================
   APPLICATION STATE MANAGEMENT

   Maintains application state including current screen, theme, UI preferences,
   and provides a pub/sub event system for cross-module communication.
   ========================================================================== */

/**
 * Camera RAW file extensions that cannot be rendered natively by browsers.
 * Used to disable rotation controls and show appropriate UI for RAW images.
 * Matches the RAW_EXTENSIONS frozenset in rawimage.py.
 * @type {Set<string>}
 */
const RAW_EXTENSIONS = new Set([
    '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.srf', '.dng', '.raf',
    '.rw2', '.orf', '.pef', '.srw', '.x3f', '.3fr', '.iiq', '.rwl',
    '.kdc', '.dcr', '.erf',
]);

/**
 * Global application object.
 * All modules interact through this object.
 * @namespace
 */
const App = {
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
        scrollPositions: {},
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
     * @deprecated Use AppState.nav.getScreen() instead
     */
    getScreen() {
        return AppState.nav.getScreen();
    },

    /**
     * Gets the current image ID being viewed in fullscreen.
     * @returns {string|null} The current image ID or null if not in fullscreen
     * @deprecated Use AppState.nav.getFullscreenImageId() instead
     */
    getCurrentImageId() {
        return AppState.nav.getFullscreenImageId();
    },

    /**
     * Gets the current theme.
     * @returns {string} The current theme ('light' or 'dark')
     * @deprecated Use AppState.view.getTheme() instead
     */
    getTheme() {
        return AppState.view.getTheme();
    },

    /**
     * Sets the theme and persists to localStorage.
     * Updates the data-theme attribute on the app container.
     * @param {string} theme - The theme to set ('light' or 'dark')
     * @fires App#themeChanged
     * @deprecated Use AppState.view.setTheme() instead
     */
    setTheme(theme) {
        AppState.view.setTheme(theme);
        // Event bridging handled in _initAppStateBridge()
    },

    /**
     * Toggles between light and dark theme.
     * @deprecated Use AppState.view.toggleTheme() instead
     */
    toggleTheme() {
        AppState.view.toggleTheme();
    },

    /**
     * Gets the current thumbnail size in pixels.
     * @returns {number} The thumbnail size
     * @deprecated Use AppState.view.getThumbnailSize() instead
     */
    getThumbnailSize() {
        return AppState.view.getThumbnailSize();
    },

    /**
     * Sets the thumbnail size and persists to localStorage.
     * Clamps value between minimum and maximum allowed sizes.
     * @param {number} size - The thumbnail size in pixels
     * @fires App#thumbnailSizeChanged
     * @deprecated Use AppState.view.setThumbnailSize() instead
     */
    setThumbnailSize(size) {
        AppState.view.setThumbnailSize(size);
        // Event bridging handled in _initAppStateBridge()
    },

    /**
     * Gets the current sort configuration.
     * @returns {{by: string, direction: string}} Sort configuration
     * @deprecated Use AppState.view.getSort() instead
     */
    getSort() {
        return AppState.view.getSort();
    },

    /**
     * Sets the sort field.
     * @param {string} sortBy - The field to sort by ('date', 'rating', 'content')
     * @fires App#sortChanged
     * @deprecated Use AppState.view.setSortBy() instead
     */
    setSortBy(sortBy) {
        AppState.view.setSortBy(sortBy);
        // Event bridging handled in _initAppStateBridge()
    },

    /**
     * Sets the sort direction.
     * @param {string} direction - The sort direction ('asc' or 'desc')
     * @fires App#sortChanged
     * @deprecated Use AppState.view.setSortDirection() instead
     */
    setSortDirection(direction) {
        AppState.view.setSortDirection(direction);
        // Event bridging handled in _initAppStateBridge()
    },

    /**
     * Toggles the sort direction between ascending and descending.
     * @deprecated Use AppState.view.toggleSortDirection() instead
     */
    toggleSortDirection() {
        AppState.view.toggleSortDirection();
    },

    /**
     * Gets the current filter criteria.
     * @returns {Object|null} The filter object or null if no filter active
     * @deprecated Use AppState.filter.get() instead
     */
    getFilter() {
        return AppState.filter.get();
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
     * @deprecated Use AppState.filter.set() instead
     */
    setFilter(filter, options = {}) {
        AppState.filter.set(filter, options);
    },

    /**
     * Checks if a filter is currently active.
     * @returns {boolean} True if a filter is active
     * @deprecated Use AppState.filter.isActive() instead
     */
    hasActiveFilter() {
        return AppState.filter.isActive();
    },

    /**
     * Clears the current filter.
     * @fires App#filterChanged
     * @deprecated Use AppState.filter.clear() instead
     */
    clearFilter() {
        AppState.filter.clear();
    },

    /**
     * Gets the array of selected image IDs.
     * @returns {Array<string>} Array of selected image IDs
     * @deprecated Use AppState.selection.get('gallery') instead
     */
    getSelectedImages() {
        return AppState.selection.get('gallery');
    },

    /**
     * Sets the selected images.
     * @param {Array<string>} imageIds - Array of image IDs to select
     * @fires App#selectionChanged
     * @deprecated Use AppState.selection.set('gallery', ids) instead
     */
    setSelectedImages(imageIds) {
        AppState.selection.set('gallery', imageIds);
    },

    /**
     * Adds an image to the selection.
     * @param {string} imageId - The image ID to add
     * @fires App#selectionChanged
     * @deprecated Use AppState.selection.add('gallery', id) instead
     */
    addToSelection(imageId) {
        AppState.selection.add('gallery', imageId);
    },

    /**
     * Removes an image from the selection.
     * @param {string} imageId - The image ID to remove
     * @fires App#selectionChanged
     * @deprecated Use AppState.selection.remove('gallery', id) instead
     */
    removeFromSelection(imageId) {
        AppState.selection.remove('gallery', imageId);
    },

    /**
     * Toggles an image's selection state.
     * @param {string} imageId - The image ID to toggle
     * @fires App#selectionChanged
     * @deprecated Use AppState.selection.toggle('gallery', id) instead
     */
    toggleSelection(imageId) {
        AppState.selection.toggle('gallery', imageId);
    },

    /**
     * Clears all selected images.
     * @fires App#selectionChanged
     * @deprecated Use AppState.selection.clear('gallery') instead
     */
    clearSelection() {
        AppState.selection.clear('gallery');
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
       State Persistence & AppState Bridge
       ---------------------------------------------------------------------- */

    /**
     * Syncs App.state with AppState for backward compatibility.
     * View settings are now managed by AppState.view but we keep App.state
     * in sync for any code that still reads from it directly.
     * @private
     */
    _syncStateFromAppState() {
        // Sync view settings from AppState.view to App.state
        this.state.theme = AppState.view.getTheme();
        this.state.thumbnailSize = AppState.view.getThumbnailSize();
        this.state.sortBy = AppState.view.getSortBy();
        this.state.sortDirection = AppState.view.getSortDirection();
        // Sync selection from AppState.selection to App.state
        this.state.selectedImages = AppState.selection.get('gallery');
        // Sync filter from AppState.filter to App.state
        this.state.filter = AppState.filter.get();
        // Sync nav from AppState.nav to App.state
        this.state.screen = AppState.nav.getScreen();
    },

    /**
     * Sets up event bridging from AppState to App events.
     * This allows gradual migration - existing code using App.on('themeChanged')
     * will continue to work while new code uses AppState.view.onChanged().
     * @private
     */
    _initAppStateBridge() {
        // Bridge AppState.view changes to App events
        AppState.view.onChanged((event) => {
            // Keep App.state in sync
            this._syncStateFromAppState();

            // Emit corresponding App events
            switch (event.property) {
                case 'theme':
                    this.emit('themeChanged', AppState.view.getTheme());
                    break;
                case 'thumbnailSize':
                    this.emit('thumbnailSizeChanged', AppState.view.getThumbnailSize());
                    break;
                case 'sortBy':
                case 'sortDirection':
                    this.emit('sortChanged', AppState.view.getSort());
                    break;
            }
        });

        // Bridge AppState.selection changes to App events
        AppState.selection.onChanged((event) => {
            // Only bridge 'gallery' context to maintain backward compatibility
            if (event.context === 'gallery') {
                // Keep App.state.selectedImages in sync
                this.state.selectedImages = AppState.selection.get('gallery');
                // Emit the legacy event
                this.emit('selectionChanged', this.state.selectedImages);
            }
        });

        // Bridge AppState.filter changes to App events
        AppState.filter.onChanged(() => {
            // Keep App.state.filter in sync
            this.state.filter = AppState.filter.get();
            // Emit the legacy event
            this.emit('filterChanged', this.state.filter);
        });
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
    SCREENS: ['gallery', 'database', 'search', 'duplicates', 'faces'],

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

        // Don't navigate if already on this screen
        const currentScreen = AppState.nav.getScreen();
        if (screen === currentScreen) {
            return;
        }

        const previousScreen = currentScreen;

        // Call onLeave hook for current screen
        this._callScreenHook(previousScreen, 'onLeave');

        // Save scroll position for scrollable screens
        this._saveScrollPosition(previousScreen);

        // Push to history if enabled
        if (pushHistory && previousScreen) {
            this._navigationHistory.push({
                screen: previousScreen,
                data: null
            });
        }

        // Update state (both AppState and legacy state for backward compatibility)
        AppState.nav.setScreen(screen, { addToHistory: false }); // We manage history ourselves
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
            'search': 'btn-filter',
            'faces': 'btn-faces'
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
                AppState.nav.setScrollPosition(screen, scrollable.scrollTop);
                this.state.scrollPositions[screen] = scrollable.scrollTop; // Keep legacy sync
            }
        }
    },

    /**
     * Restores a previously saved scroll position for a screen.
     * @param {string} screen - The screen name
     * @private
     */
    _restoreScrollPosition(screen) {
        const savedPosition = AppState.nav.getScrollPosition(screen);
        if (savedPosition > 0) {
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
     * Opens the fullscreen overlay for a specific image.
     * The underlying screen remains visible underneath.
     * @param {string} imageId - The ID of the image to view
     * @param {Object} [options] - Optional settings
     * @param {Array<Object>} [options.imageList] - Custom image list for navigation context
     */
    showFullscreen(imageId, options) {
        Fullscreen.open(imageId, options);
    },

    /**
     * Closes the fullscreen overlay.
     */
    hideFullscreen() {
        Fullscreen.close();
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
     * Navigates to faces screen.
     * Convenience method.
     */
    showFaces() {
        this.navigateTo('faces');
    },

    /**
     * Closes the fullscreen overlay.
     * Alias for hideFullscreen() for backward compatibility.
     */
    exitFullscreen() {
        Fullscreen.close();
    },

    /* ----------------------------------------------------------------------
       API COMMUNICATION

       Simple fetch wrappers for backend calls.
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
        const url = this.apiBase + endpoint;
        const method = options.method || 'GET';
        const headers = method === 'GET' ? {} : { 'Content-Type': 'application/json' };

        // Log API request (skip noisy polling endpoints)
        const isPolling = endpoint === '/events' || endpoint === '/status';
        if (!isPolling) {
            console.log(`[API] ${method} ${endpoint}`);
        }

        const response = await fetch(url, {
            headers,
            ...options
        });

        if (!response.ok) {
            console.error(`[API] ${method} ${endpoint} failed: ${response.status}`);
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

    /**
     * PATCH request helper.
     * @param {string} endpoint - API endpoint
     * @param {Object} data - Request body
     * @returns {Promise<*>} Response data
     */
    async apiPatch(endpoint, data) {
        return this.api(endpoint, {
            method: 'PATCH',
            body: JSON.stringify(data)
        });
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
            const data = response.data;
            this._thumbnailConfig = {
                concurrentRequests: data.thumbnail_concurrent_requests,
                extraRows: data.thumbnail_extra_rows,
                timeoutMs: data.thumbnail_timeout_ms,
                scrollThrottleMs: data.thumbnail_scroll_throttle_ms
            };
            // Quality scoring weights (used by AppState.images for Quality sort)
            this._qualityConfig = {
                weightAesthetic: data.quality_weight_aesthetic ?? 0.60,
                weightSharpness: data.quality_weight_sharpness ?? 0.20,
                weightPixels: data.quality_weight_pixels ?? 0.15,
                weightBpp: data.quality_weight_bpp ?? 0.05,
                alpha: data.quality_alpha ?? 0.60,
                nimaEnabled: data.nima_enabled ?? false,
            };
        } catch (error) {
            console.warn('Failed to load thumbnail config, using defaults:', error);
        }
        return this._thumbnailConfig;
    },

    /**
     * Gets the quality scoring configuration.
     * Returns cached config (defaults until loadThumbnailConfig completes).
     * @returns {Object} Quality config with weightAesthetic, weightSharpness, weightPixels, weightBpp, alpha, nimaEnabled
     */
    getQualityConfig() {
        return this._qualityConfig || {
            weightAesthetic: 0.60,
            weightSharpness: 0.20,
            weightPixels: 0.15,
            weightBpp: 0.05,
            alpha: 0.60,
            nimaEnabled: false,
        };
    },

    /* ----------------------------------------------------------------------
       Image Cache (delegated to AppState.images)
       ---------------------------------------------------------------------- */

    /**
     * Gets all images, using cache with delta updates for efficiency.
     * On first call, fetches all images and caches them.
     * On subsequent calls, fetches only changes since last sync.
     * @returns {Promise<Array<Object>>} Array of image objects
     */
    async getImages() {
        await AppState.images.load();
        return AppState.images.getAll();
    },

    /**
     * Forces a full reload of the image cache.
     * Use this when cache may be stale (e.g., after major changes).
     * @returns {Promise<Array<Object>>} Array of image objects
     */
    async reloadImages() {
        await AppState.images.reload();
        return AppState.images.getAll();
    },

    /**
     * Gets the current cached image count without fetching.
     * @returns {number} Number of cached images, or 0 if cache not loaded
     */
    getCachedImageCount() {
        return AppState.images.getCount();
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
        this._bindBtn('btn-clear-filter', () => this._handleClearFilterClick());
        this._bindBtn('btn-back-gallery', () => this.showGallery());

        // Gallery controls
        this._bindBtn('btn-thumb-smaller', () => this.setThumbnailSize(AppState.view.getThumbnailSize() - 50));
        this._bindBtn('btn-thumb-larger', () => this.setThumbnailSize(AppState.view.getThumbnailSize() + 50));
        this._bindBtn('btn-fullscreen', () => this._handleFullscreenClick());
        this._bindBtn('btn-reveal-folder', () => this._handleRevealFolderClick());
        this._bindBtn('btn-rotate-ccw', () => this._handleRotateClick(270));
        this._bindBtn('btn-rotate-cw', () => this._handleRotateClick(90));
        this._bindBtn('btn-select-all', () => this.emit('selectAll'));
        this._bindBtn('btn-clear-selection', () => this.clearSelection());

        // Sort controls
        this._bindBtn('btn-sort-date', () => this.setSortBy('date'));
        this._bindBtn('btn-sort-rating', () => this.setSortBy('rating'));
        this._bindBtn('btn-sort-content', () => this.setSortBy('content'));
        this._bindBtn('btn-sort-people', () => this.setSortBy('people'));
        this._bindBtn('btn-sort-quality', () => this.setSortBy('quality'));
        this._bindBtn('btn-sort-direction', () => this.toggleSortDirection());

        // Duplicates controls
        this._bindBtn('btn-dup-thumb-smaller', () => this.setThumbnailSize(AppState.view.getThumbnailSize() - 50));
        this._bindBtn('btn-dup-thumb-larger', () => this.setThumbnailSize(AppState.view.getThumbnailSize() + 50));

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

            // Ignore navigation shortcuts while fullscreen is open
            // (don't change screen underneath the fullscreen overlay)
            if (AppState.nav.isFullscreenOpen()) return;

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
                case 's':
                    e.preventDefault();
                    this.showSearch();
                    break;
                case 'f':
                    e.preventDefault();
                    this.showFaces();
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
     * Rotates all selected images by the specified angle.
     * @param {number} degrees - Rotation angle (90 for right, 270 for left)
     * @private
     */
    async _handleRotateClick(degrees) {
        const selectedIds = [...this.state.selectedImages];
        if (selectedIds.length === 0) {
            return;
        }

        // Defense-in-depth: filter out RAW files (toolbar should already be
        // disabled, but protect against programmatic calls)
        const rotatableIds = selectedIds.filter(id => {
            const img = AppState.images.getById(id);
            return !img || !App.isRawFile(img.basename);
        });
        if (rotatableIds.length === 0) {
            this.showError('RAW files cannot be rotated.');
            return;
        }

        try {
            // Rotate all rotatable images in one batch request
            // Note: Backend emits images_modified event for gallery thumbnail updates
            const result = await this.apiPost('/images/rotate', {
                image_ids: rotatableIds,
                degrees: degrees
            });

            // Report any failures
            if (result && result.results) {
                const failed = rotatableIds.filter(id => !result.results[id]);
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

        // Rotate buttons: disabled when nothing selected OR any selected image
        // is a RAW file (RAW files cannot be rotated — they are read-only sensor data)
        const rotateCcwBtn = document.getElementById('btn-rotate-ccw');
        const rotateCwBtn = document.getElementById('btn-rotate-cw');
        const anySelectedIsRaw = selCount > 0 && this.state.selectedImages.some(id => {
            const img = AppState.images.getById(id);
            return img && App.isRawFile(img.basename);
        });
        const rotateDisabled = selCount === 0 || anySelectedIsRaw;
        const rotateTitle = anySelectedIsRaw
            ? 'Cannot rotate RAW files'
            : '';
        if (rotateCcwBtn) {
            rotateCcwBtn.disabled = rotateDisabled;
            rotateCcwBtn.title = rotateTitle || rotateCcwBtn.getAttribute('data-default-title') || 'Rotate left';
        }
        if (rotateCwBtn) {
            rotateCwBtn.disabled = rotateDisabled;
            rotateCwBtn.title = rotateTitle || rotateCwBtn.getAttribute('data-default-title') || 'Rotate right';
        }
    },

    /**
     * Updates sort button active states.
     * @private
     */
    _updateSortButtons() {
        const sortBy = AppState.view.getSortBy();
        const direction = AppState.view.getSortDirection();

        // Update active states
        ['date', 'rating', 'content', 'people', 'quality'].forEach(type => {
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
     * Updates filter button to show active state and clear button enabled state.
     * @private
     */
    _updateFilterButton() {
        const hasFilter = this.hasActiveFilter();

        const btn = document.getElementById('btn-filter');
        if (btn) {
            // Toggle active class to indicate filter is active (styling only)
            btn.classList.toggle('active', hasFilter);
        }

        const clearBtn = document.getElementById('btn-clear-filter');
        if (clearBtn) {
            // Enable/disable clear filter button based on filter state
            clearBtn.disabled = !hasFilter;
        }
    },

    /**
     * Handles clear filter button click.
     * Clears the filter without changing screens.
     * @private
     */
    _handleClearFilterClick() {
        if (this.hasActiveFilter()) {
            this.clearFilter();
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
                icon.textContent = AppState.view.getTheme() === 'light' ? 'dark_mode' : 'light_mode';
            }
        }
    },

    /* ----------------------------------------------------------------------
       STATUS & NOTIFICATIONS

       Loading indicators and error messages.
       ---------------------------------------------------------------------- */

    /**
     * Shows the global loading overlay with optional message.
     * Uses AppState.loading with 'app' as the owner.
     * @param {string} [message='Loading…'] - Message to display
     */
    showLoading(message = 'Loading…') {
        AppState.loading.show('app', message);
    },

    /**
     * Hides the global loading overlay if 'app' is the owner.
     */
    hideLoading() {
        AppState.loading.hide('app');
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
            // Append to #app so it inherits theme CSS variables
            (document.getElementById('app') || document.body).appendChild(toast);
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
     * @param {Object} [options] - Optional configuration
     * @param {boolean} [options.danger=false] - If true, OK button uses danger style (red)
     * @param {string} [options.okText] - Custom OK button label (e.g. "Delete")
     * @returns {Promise<boolean>} Resolves true if confirmed, false if cancelled
     */
    confirm(title, message, options = {}) {
        return new Promise(resolve => {
            const dialog = document.getElementById('dialog-confirm');
            const titleEl = document.getElementById('dialog-confirm-title');
            const msgEl = document.getElementById('dialog-confirm-message');
            const okBtn = document.getElementById('dialog-confirm-ok');
            const cancelBtn = document.getElementById('dialog-confirm-cancel');

            titleEl.textContent = title;
            msgEl.textContent = message;

            // Apply danger styling and custom OK text if requested
            const isDanger = options.danger === true;
            okBtn.classList.toggle('danger', isDanger);
            okBtn.classList.toggle('primary', !isDanger);
            okBtn.textContent = options.okText || 'OK';

            let onKeyDown; // Declared here so cleanup can reference it

            const cleanup = (result) => {
                okBtn.removeEventListener('click', onOk);
                cancelBtn.removeEventListener('click', onCancel);
                dialog.removeEventListener('cancel', onCancel);
                dialog.removeEventListener('keydown', onKeyDown);
                // Reset button state for next use
                okBtn.classList.remove('danger');
                okBtn.classList.add('primary');
                okBtn.textContent = 'OK';
                dialog.close();
                resolve(result);
            };

            const onOk = () => cleanup(true);
            const onCancel = () => cleanup(false);

            onKeyDown = (e) => {
                // Stop all key events from reaching the underlying page
                e.stopPropagation();

                if (e.key === 'Escape') {
                    e.preventDefault();
                    onCancel();
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    // Enter confirms whichever button is focused, or OK by default
                    if (document.activeElement === cancelBtn) {
                        onCancel();
                    } else {
                        onOk();
                    }
                } else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                    e.preventDefault();
                    // Toggle focus between buttons
                    if (document.activeElement === okBtn) {
                        cancelBtn.focus();
                    } else {
                        okBtn.focus();
                    }
                }
            };

            okBtn.addEventListener('click', onOk);
            cancelBtn.addEventListener('click', onCancel);
            dialog.addEventListener('cancel', onCancel); // Escape key (native)
            dialog.addEventListener('keydown', onKeyDown);

            dialog.showModal();
            cancelBtn.focus(); // Default to Cancel for safety
        });
    },

    /**
     * Shows a prompt dialog for text input.
     * @param {string} title - Dialog title
     * @param {string} message - Dialog message
     * @param {string|Object} [defaultValueOrOptions=''] - Default input value, or options object
     * @param {string} [defaultValueOrOptions.defaultValue=''] - Default input value
     * @param {Function} [defaultValueOrOptions.onInput] - Callback for input events: (inputEl, autocompleteEl, value) => void
     * @param {Function} [defaultValueOrOptions.onSelect] - Callback when value is selected (e.g., from autocomplete)
     * @returns {Promise<string|null>} Resolves with the entered value, or null if cancelled
     */
    prompt(title, message, defaultValueOrOptions = '') {
        // Support both old signature (string) and new signature (options object)
        const options = typeof defaultValueOrOptions === 'string'
            ? { defaultValue: defaultValueOrOptions }
            : defaultValueOrOptions;
        const { defaultValue = '', onInput, onSelect } = options;

        return new Promise(resolve => {
            const dialog = document.getElementById('dialog-prompt');
            const titleEl = document.getElementById('dialog-prompt-title');
            const msgEl = document.getElementById('dialog-prompt-message');
            const inputEl = document.getElementById('dialog-prompt-input');
            const autocompleteEl = document.getElementById('dialog-prompt-autocomplete');
            const okBtn = document.getElementById('dialog-prompt-ok');
            const cancelBtn = document.getElementById('dialog-prompt-cancel');

            titleEl.textContent = title;
            msgEl.textContent = message;
            inputEl.value = defaultValue;
            if (autocompleteEl) autocompleteEl.innerHTML = '';

            let onDialogKeyDown; // Declared here so cleanup can reference it
            let onInputHandler;

            const cleanup = (result) => {
                okBtn.removeEventListener('click', onOk);
                cancelBtn.removeEventListener('click', onCancel);
                inputEl.removeEventListener('keydown', onKeydown);
                if (onInputHandler) inputEl.removeEventListener('input', onInputHandler);
                dialog.removeEventListener('cancel', onCancel);
                dialog.removeEventListener('keydown', onDialogKeyDown);
                if (autocompleteEl) {
                    autocompleteEl.innerHTML = '';
                    autocompleteEl.style.display = 'none';
                }
                dialog.close();
                resolve(result);
            };

            const onOk = () => cleanup(inputEl.value);
            const onCancel = () => cleanup(null);
            const onKeydown = (e) => {
                // Stop all key events from reaching the underlying page
                e.stopPropagation();

                if (e.key === 'Enter') {
                    e.preventDefault();
                    onOk();
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    onCancel();
                }
            };
            onDialogKeyDown = (e) => {
                // Stop all key events from reaching the underlying page
                e.stopPropagation();

                // Escape anywhere in dialog cancels
                if (e.key === 'Escape') {
                    e.preventDefault();
                    onCancel();
                    return;
                }

                // Arrow keys toggle focus between buttons (only when a button has focus)
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                    if (document.activeElement === okBtn || document.activeElement === cancelBtn) {
                        e.preventDefault();
                        if (document.activeElement === okBtn) {
                            cancelBtn.focus();
                        } else {
                            okBtn.focus();
                        }
                    }
                }
            };

            // Wire up onInput callback for autocomplete support
            if (onInput) {
                onInputHandler = () => {
                    onInput(inputEl, autocompleteEl, inputEl.value);
                };
                inputEl.addEventListener('input', onInputHandler);
            }

            // Allow external code to programmatically select a value and close
            if (onSelect) {
                // Expose a select function via the dialog element
                dialog._selectValue = (value) => {
                    inputEl.value = value;
                    cleanup(value);
                };
            }

            okBtn.addEventListener('click', onOk);
            cancelBtn.addEventListener('click', onCancel);
            inputEl.addEventListener('keydown', onKeydown);
            dialog.addEventListener('cancel', onCancel); // Escape key
            dialog.addEventListener('keydown', onDialogKeyDown);

            dialog.showModal();
            inputEl.select(); // Select text for easy replacement

            // Trigger initial onInput if there's a default value
            if (onInput && defaultValue) {
                onInput(inputEl, autocompleteEl, defaultValue);
            }
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

        const handleKeyDown = (e) => {
            // Stop all key events from reaching the underlying page
            e.stopPropagation();

            if (e.key === 'Escape') {
                e.preventDefault();
                cleanup();
            }
        };

        const cleanup = () => {
            grid.removeEventListener('click', handleClick);
            closeBtn.removeEventListener('click', cleanup);
            dialog.removeEventListener('cancel', cleanup);
            dialog.removeEventListener('keydown', handleKeyDown);
            dialog.close();
        };

        grid.addEventListener('click', handleClick);
        closeBtn.addEventListener('click', cleanup);
        dialog.addEventListener('cancel', cleanup);
        dialog.addEventListener('keydown', handleKeyDown);

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
        size = size || AppState.view.getThumbnailSize();
        return `${this.apiBase}/images/${imageId}/thumbnail?size=${size}`;
    },

    /**
     * Builds a full image URL.
     * @param {string} imageId - The image ID
     * @returns {string} Full image URL
     */
    imageUrl(imageId) {
        return `${this.apiBase}/images/${imageId}/full`;
    },

    /**
     * Formats a file size in bytes to a human-readable string.
     * @param {number} bytes - File size in bytes
     * @returns {string} Formatted size (e.g., "1.5 MB")
     */
    formatFileSize(bytes) {
        if (bytes == null || isNaN(bytes)) return 'Unknown';
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
     * Check whether a filename has a camera RAW extension.
     * Used to disable rotation controls and adjust UI for RAW images.
     * @param {string} basename - Filename (e.g. "IMG_1234.CR2")
     * @returns {boolean} True if the file is a RAW image
     */
    isRawFile(basename) {
        if (!basename) return false;
        const dot = basename.lastIndexOf('.');
        if (dot < 0) return false;
        return RAW_EXTENSIONS.has(basename.slice(dot).toLowerCase());
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

    /**
     * Adds a hover tooltip to a range slider showing the value at cursor position.
     * @param {HTMLInputElement} slider - The range input element
     * @param {Object} [options] - Configuration options
     * @param {string} [options.suffix='%'] - Suffix to append to value (e.g., '%')
     * @param {Function} [options.formatValue] - Custom formatter function(value) => string
     */
    addSliderHoverTooltip(slider, options = {}) {
        const { suffix = '%', formatValue } = options;

        const tooltip = document.createElement('div');
        tooltip.className = 'slider-hover-tooltip';
        // Append to #app so it inherits theme CSS variables
        (document.getElementById('app') || document.body).appendChild(tooltip);

        slider.addEventListener('mouseenter', () => {
            tooltip.style.opacity = '1';
        });

        slider.addEventListener('mouseleave', () => {
            tooltip.style.opacity = '0';
        });

        slider.addEventListener('mousemove', (e) => {
            const rect = slider.getBoundingClientRect();
            const min = parseFloat(slider.min);
            const max = parseFloat(slider.max);
            // Account for thumb width (~16px)
            const thumbHalf = 8;
            const trackWidth = rect.width - thumbHalf * 2;
            const x = Math.max(0, Math.min(trackWidth, e.clientX - rect.left - thumbHalf));
            const ratio = x / trackWidth;
            const value = Math.round(min + ratio * (max - min));

            // Format the display value
            const displayValue = formatValue ? formatValue(value) : `${value}${suffix}`;
            tooltip.textContent = displayValue;

            // Position tooltip - above slider, but below if too close to top
            const tooltipHeight = 24;
            const margin = 8;
            let top = rect.top - tooltipHeight - margin;
            if (top < margin) {
                // Position below slider with extra offset to clear cursor
                top = rect.bottom + margin + 16;
            }
            tooltip.style.left = `${e.clientX}px`;
            tooltip.style.top = `${top}px`;
        });
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
        // Initialize AppState.view (applies theme to DOM, loads from localStorage)
        AppState.view.init();

        // Set up event bridge from AppState to App events (backward compatibility)
        this._initAppStateBridge();

        // Sync App.state from AppState for code that still reads directly
        this._syncStateFromAppState();

        // Load thumbnail config from backend (async, uses defaults until loaded)
        this.loadThumbnailConfig();

        // Start event polling (receives backend notifications about new images, faces, etc.)
        if (AppState.events?.startPolling) {
            AppState.events.startPolling(2000);
        }

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
    },

    /**
     * Determines and navigates to the initial screen.
     * Shows gallery immediately, redirects to database if empty.
     * @private
     */
    _determineInitialScreen() {
        // Show gallery immediately - don't wait for stats
        this.navigateTo('gallery', { pushHistory: false });

        // Hide loading splash right away
        this._hideLoadingSplash();

        // Check stats in background and redirect if database is empty
        this.apiGet('/stats').then(response => {
            const stats = response.data;
            if (stats.totalImages === 0 && this.getScreen() === 'gallery') {
                this.navigateTo('database', { pushHistory: false });
            }
        }).catch(error => {
            console.error('Error checking database:', error);
        });
    },

    /**
     * Hides the loading splash and shows the app.
     * @private
     */
    _hideLoadingSplash() {
        const splash = document.getElementById('app-loading');
        const app = document.getElementById('app');

        if (splash) {
            splash.hidden = true;
        }
        if (app) {
            app.classList.add('ready');
        }
    }
};

/* ==========================================================================
   DOM READY - Start the application
   ========================================================================== */

document.addEventListener('DOMContentLoaded', () => {
    App._init();
});
