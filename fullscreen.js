/**
 * @fileoverview Full-screen image viewer module for the Imaginary application.
 *
 * This module handles the full-screen image viewing experience, providing
 * zoom, pan, and navigation capabilities. It registers with the core App
 * module and takes over the entire viewport when active.
 *
 * RESPONSIBILITIES:
 *
 * Image Display:
 *   - Loads and displays the full-resolution image
 *   - Initially scales image to fit screen while preserving aspect ratio
 *   - Sets screen background to black for optimal viewing
 *   - Shows canonical filename overlay at bottom (fades out after delay)
 *
 * Zoom Controls:
 *   - Mouse scroll wheel zooms in/out centered on cursor position
 *   - Touch pinch gesture zooms in/out centered on pinch midpoint
 *   - Maintains zoom level and pan position during navigation
 *   - Double-tap/double-click toggles between fit-to-screen and 100% zoom
 *   - Respects minimum (fit-to-screen) and maximum zoom limits
 *
 * Pan Controls:
 *   - Click-and-drag to pan when zoomed in
 *   - Touch drag to pan when zoomed in
 *   - Constrains panning to keep image edges visible (no over-scroll)
 *   - Smooth momentum scrolling on touch devices
 *
 * Navigation:
 *   - Left/Right arrow keys navigate to previous/next image
 *   - Wraps from last image to first, and vice versa
 *   - Navigation respects current gallery sort order and filter
 *   - Resets zoom/pan state when navigating to new image
 *   - Swipe left/right on touch devices for navigation
 *
 * Exit Handling:
 *   - Escape key returns to Gallery view
 *   - Double-click/double-tap on image returns to Gallery view
 *   - Maintains gallery selection state (viewed image remains selected)
 *   - Restores gallery scroll position to show the viewed image
 *
 * Performance:
 *   - Preloads adjacent images for smooth navigation
 *   - Uses CSS transforms for zoom/pan (GPU accelerated)
 *   - Cancels pending image loads when navigating quickly
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(imageId): Loads specified image, hides toolbar, shows viewer
 *   - onLeave(): Resets zoom/pan state, shows toolbar, cleans up event listeners
 *
 * @module fullscreen
 * @requires core
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE

   Fullscreen module registration, state, and lifecycle hooks.
   ========================================================================== */

/**
 * Fullscreen viewer module.
 * @namespace
 */
const Fullscreen = {
    /**
     * Local state for the fullscreen viewer.
     * @type {Object}
     * @property {string|null} currentId - Currently displayed image ID
     * @property {Array<Object>} imageList - Reference to gallery's filtered/sorted images
     * @property {number} currentIndex - Index in imageList of current image
     * @property {number} zoom - Current zoom level (1 = fit to screen)
     * @property {number} panX - Current horizontal pan offset
     * @property {number} panY - Current vertical pan offset
     * @property {boolean} isPanning - Whether user is currently panning
     */
    state: {
        currentId: null,
        imageList: [],
        currentIndex: -1,
        zoom: 1,
        panX: 0,
        panY: 0,
        isPanning: false
    },

    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * Timeout for hiding the filename overlay.
     * @type {number|null}
     * @private
     */
    _filenameTimeout: null,

    /**
     * Bound event handler references for cleanup.
     * @type {Object}
     * @private
     */
    _handlers: {},

    /**
     * Initialises the fullscreen module.
     * Called once during app startup.
     */
    init() {
        // Cache DOM elements
        this._els = {
            screen: App.$('screen-fullscreen'),
            container: App.$('fullscreen-container'),
            image: App.$('fullscreen-image'),
            filename: App.$('fullscreen-filename')
        };
    },

    /**
     * Called when entering the fullscreen view.
     * @param {string} imageId - ID of the image to display
     */
    onEnter(imageId) {
        // Get the current image list from Gallery's filtered/sorted state
        this.state.imageList = Gallery.state.images;
        this.state.currentIndex = this.state.imageList.findIndex(img => img.id === imageId);

        // Reset zoom/pan state
        this._resetTransform();

        // Load and display the image
        this._loadImage(imageId);

        // Bind event listeners
        this._bindEvents();
    },

    /**
     * Called when leaving the fullscreen view.
     */
    onLeave() {
        // Unbind event listeners
        this._unbindEvents();

        // Clear filename timeout
        if (this._filenameTimeout) {
            clearTimeout(this._filenameTimeout);
            this._filenameTimeout = null;
        }

        // Clear image source to free memory
        this._els.image.src = '';
        this.state.currentId = null;
    },

    /**
     * Resets zoom and pan to default state.
     * @private
     */
    _resetTransform() {
        this.state.zoom = 1;
        this.state.panX = 0;
        this.state.panY = 0;
        this._applyTransform();
    },

    /**
     * Applies current zoom and pan as CSS transform.
     * @private
     */
    _applyTransform() {
        const { zoom, panX, panY } = this.state;
        this._els.image.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    },

    /**
     * Binds all event listeners for fullscreen interaction.
     * @private
     */
    _bindEvents() {
        // Store bound handlers for later removal
        this._handlers = {
            keydown: (e) => this._handleKeyDown(e),
            wheel: (e) => this._handleWheel(e),
            mousedown: (e) => this._handleMouseDown(e),
            mousemove: (e) => this._handleMouseMove(e),
            mouseup: (e) => this._handleMouseUp(e),
            dblclick: (e) => this._handleDoubleClick(e)
        };

        document.addEventListener('keydown', this._handlers.keydown);
        this._els.container.addEventListener('wheel', this._handlers.wheel, { passive: false });
        this._els.container.addEventListener('mousedown', this._handlers.mousedown);
        document.addEventListener('mousemove', this._handlers.mousemove);
        document.addEventListener('mouseup', this._handlers.mouseup);
        this._els.container.addEventListener('dblclick', this._handlers.dblclick);
    },

    /**
     * Unbinds all event listeners.
     * @private
     */
    _unbindEvents() {
        document.removeEventListener('keydown', this._handlers.keydown);
        this._els.container.removeEventListener('wheel', this._handlers.wheel);
        this._els.container.removeEventListener('mousedown', this._handlers.mousedown);
        document.removeEventListener('mousemove', this._handlers.mousemove);
        document.removeEventListener('mouseup', this._handlers.mouseup);
        this._els.container.removeEventListener('dblclick', this._handlers.dblclick);

        this._handlers = {};
    },

    /* ----------------------------------------------------------------------
       IMAGE DISPLAY

       Loading images and showing filename overlay.
       ---------------------------------------------------------------------- */

    /**
     * Filename overlay display duration in milliseconds.
     * @type {number}
     * @constant
     */
    FILENAME_DISPLAY_MS: 3000,

    /**
     * Loads and displays an image.
     * @param {string} imageId - ID of the image to load
     * @private
     */
    _loadImage(imageId) {
        this.state.currentId = imageId;

        // Find image data
        const img = this.state.imageList.find(i => i.id === imageId);
        if (!img) {
            console.error('Image not found:', imageId);
            return;
        }

        // Update index
        this.state.currentIndex = this.state.imageList.findIndex(i => i.id === imageId);

        // Load the full image
        this._els.image.src = App.imageUrl(imageId);

        // Show filename overlay with dimensions
        this._showFilename(img.basename, img.width, img.height);

        // Preload adjacent images
        this._preloadAdjacent();
    },

    /**
     * Shows the filename overlay and schedules it to hide.
     * @param {string} filename - Filename to display
     * @param {number} [width] - Image width in pixels
     * @param {number} [height] - Image height in pixels
     * @private
     */
    _showFilename(filename, width, height) {
        const el = this._els.filename;

        // Build display text with optional dimensions
        let text = filename;
        if (width && height) {
            text += ` (${width} × ${height})`;
        }
        el.textContent = text;
        el.classList.remove('hidden');

        // Clear any existing timeout
        if (this._filenameTimeout) {
            clearTimeout(this._filenameTimeout);
        }

        // Schedule hide
        this._filenameTimeout = setTimeout(() => {
            el.classList.add('hidden');
            this._filenameTimeout = null;
        }, this.FILENAME_DISPLAY_MS);
    },

    /**
     * Preloads the previous and next images for smooth navigation.
     * @private
     */
    _preloadAdjacent() {
        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        const prevIndex = (currentIndex - 1 + imageList.length) % imageList.length;
        const nextIndex = (currentIndex + 1) % imageList.length;

        // Preload by creating Image objects
        const preloadPrev = new Image();
        preloadPrev.src = App.imageUrl(imageList[prevIndex].id);

        const preloadNext = new Image();
        preloadNext.src = App.imageUrl(imageList[nextIndex].id);
    },

    /**
     * Gets the current image data object.
     * @returns {Object|null} Current image data or null
     * @private
     */
    _getCurrentImage() {
        return this.state.imageList[this.state.currentIndex] || null;
    },

    /* ----------------------------------------------------------------------
       ZOOM CONTROLS

       Scroll wheel zoom and double-click toggle.
       ---------------------------------------------------------------------- */

    /**
     * Minimum zoom level (fit to screen).
     * @type {number}
     * @constant
     */
    MIN_ZOOM: 1,

    /**
     * Maximum zoom level.
     * @type {number}
     * @constant
     */
    MAX_ZOOM: 8,

    /**
     * Zoom factor per scroll wheel tick.
     * @type {number}
     * @constant
     */
    ZOOM_STEP: 1.15,

    /**
     * Handles mouse wheel for zooming.
     * Zooms centered on cursor position.
     * Supports both scroll wheel and trackpad pinch gestures.
     * @param {WheelEvent} e
     * @private
     */
    _handleWheel(e) {
        e.preventDefault();

        let factor;

        // Trackpad pinch gestures have ctrlKey set and need proportional zoom
        if (e.ctrlKey) {
            // Pinch gesture - use proportional zoom based on deltaY
            // Smaller multiplier for smoother response
            factor = 1 - (e.deltaY * 0.01);
        } else {
            // Regular scroll wheel - use fixed step
            const delta = e.deltaY > 0 ? -1 : 1;
            factor = delta > 0 ? this.ZOOM_STEP : 1 / this.ZOOM_STEP;
        }

        this._zoomAtPoint(factor, e.clientX, e.clientY);
    },

    /**
     * Zooms in or out centered on a specific point.
     * @param {number} factor - Zoom multiplier (>1 zooms in, <1 zooms out)
     * @param {number} clientX - X coordinate of zoom center
     * @param {number} clientY - Y coordinate of zoom center
     * @private
     */
    _zoomAtPoint(factor, clientX, clientY) {
        const oldZoom = this.state.zoom;
        let newZoom = oldZoom * factor;

        // Clamp to limits
        newZoom = Math.max(this.MIN_ZOOM, Math.min(this.MAX_ZOOM, newZoom));

        if (newZoom === oldZoom) return;

        // Get container bounds
        const rect = this._els.container.getBoundingClientRect();
        const centerX = rect.width / 2;
        const centerY = rect.height / 2;

        // Calculate point relative to image center
        const pointX = clientX - rect.left - centerX;
        const pointY = clientY - rect.top - centerY;

        // Adjust pan to keep the point under cursor stationary
        const zoomRatio = newZoom / oldZoom;
        this.state.panX = pointX - (pointX - this.state.panX) * zoomRatio;
        this.state.panY = pointY - (pointY - this.state.panY) * zoomRatio;
        this.state.zoom = newZoom;

        // Constrain pan after zoom
        this._constrainPan();
        this._applyTransform();
    },

    /**
     * Handles double-click to toggle between fit and 100% zoom.
     * @param {MouseEvent} e
     * @private
     */
    _handleDoubleClick(e) {
        // If zoomed in, reset to fit
        if (this.state.zoom > 1.01) {
            this._resetTransform();
        } else {
            // Zoom to 100% (or 2x if image is smaller than screen) centered on click
            const targetZoom = Math.max(2, this._calculateNativeZoom());
            const factor = targetZoom / this.state.zoom;
            this._zoomAtPoint(factor, e.clientX, e.clientY);
        }
    },

    /**
     * Calculates the zoom level needed to show image at native resolution.
     * @returns {number} Zoom level for 1:1 pixel mapping
     * @private
     */
    _calculateNativeZoom() {
        const img = this._getCurrentImage();
        if (!img) return 2;

        const rect = this._els.container.getBoundingClientRect();
        const scaleX = img.width / rect.width;
        const scaleY = img.height / rect.height;

        // Native zoom is how much we need to scale up from fit-to-screen
        return Math.max(scaleX, scaleY);
    },

    /* ----------------------------------------------------------------------
       PAN CONTROLS

       Click-and-drag panning with constraints.
       ---------------------------------------------------------------------- */

    /**
     * Starting mouse position for pan operation.
     * @type {{x: number, y: number}|null}
     * @private
     */
    _panStart: null,

    /**
     * Handles mouse down to start panning.
     * @param {MouseEvent} e
     * @private
     */
    _handleMouseDown(e) {
        // Only pan with left button when zoomed in
        if (e.button !== 0 || this.state.zoom <= 1) return;

        e.preventDefault();
        this.state.isPanning = true;
        this._panStart = {
            x: e.clientX - this.state.panX,
            y: e.clientY - this.state.panY
        };

        // Change cursor to grabbing
        this._els.container.style.cursor = 'grabbing';
    },

    /**
     * Handles mouse move during panning.
     * @param {MouseEvent} e
     * @private
     */
    _handleMouseMove(e) {
        if (!this.state.isPanning || !this._panStart) return;

        this.state.panX = e.clientX - this._panStart.x;
        this.state.panY = e.clientY - this._panStart.y;

        this._constrainPan();
        this._applyTransform();
    },

    /**
     * Handles mouse up to end panning.
     * @param {MouseEvent} e
     * @private
     */
    _handleMouseUp(e) {
        if (!this.state.isPanning) return;

        this.state.isPanning = false;
        this._panStart = null;

        // Restore cursor
        this._els.container.style.cursor = this.state.zoom > 1 ? 'grab' : 'default';
    },

    /**
     * Constrains pan values to keep the image edges visible.
     * Prevents panning beyond the image boundaries.
     * @private
     */
    _constrainPan() {
        const { zoom } = this.state;

        // No panning needed at zoom level 1
        if (zoom <= 1) {
            this.state.panX = 0;
            this.state.panY = 0;
            return;
        }

        const rect = this._els.container.getBoundingClientRect();
        const img = this._els.image;

        // Calculate the scaled image dimensions
        // At zoom=1, image fits container. At zoom>1, it's larger.
        const imgWidth = img.naturalWidth || rect.width;
        const imgHeight = img.naturalHeight || rect.height;

        // Determine the base size (fit to container)
        const containerAspect = rect.width / rect.height;
        const imgAspect = imgWidth / imgHeight;

        let baseWidth, baseHeight;
        if (imgAspect > containerAspect) {
            // Image is wider - fits to width
            baseWidth = rect.width;
            baseHeight = rect.width / imgAspect;
        } else {
            // Image is taller - fits to height
            baseHeight = rect.height;
            baseWidth = rect.height * imgAspect;
        }

        // Scaled dimensions
        const scaledWidth = baseWidth * zoom;
        const scaledHeight = baseHeight * zoom;

        // Maximum pan is half the overflow
        const maxPanX = Math.max(0, (scaledWidth - rect.width) / 2);
        const maxPanY = Math.max(0, (scaledHeight - rect.height) / 2);

        // Clamp pan values
        this.state.panX = Math.max(-maxPanX, Math.min(maxPanX, this.state.panX));
        this.state.panY = Math.max(-maxPanY, Math.min(maxPanY, this.state.panY));
    },

    /* ----------------------------------------------------------------------
       NAVIGATION & EXIT

       Keyboard navigation, prev/next, and exit handling.
       ---------------------------------------------------------------------- */

    /**
     * Handles keyboard input for navigation and exit.
     * @param {KeyboardEvent} e
     * @private
     */
    _handleKeyDown(e) {
        switch (e.key) {
            case 'Escape':
                e.preventDefault();
                this._exit();
                break;
            case 'ArrowLeft':
                e.preventDefault();
                this._navigatePrev();
                break;
            case 'ArrowRight':
                e.preventDefault();
                this._navigateNext();
                break;
            case 'Home':
                e.preventDefault();
                this._navigateToIndex(0);
                break;
            case 'End':
                e.preventDefault();
                this._navigateToIndex(this.state.imageList.length - 1);
                break;
        }
    },

    /**
     * Navigates to the previous image.
     * Wraps to the last image if at the beginning.
     * @private
     */
    _navigatePrev() {
        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        const newIndex = (currentIndex - 1 + imageList.length) % imageList.length;
        this._navigateToIndex(newIndex);
    },

    /**
     * Navigates to the next image.
     * Wraps to the first image if at the end.
     * @private
     */
    _navigateNext() {
        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        const newIndex = (currentIndex + 1) % imageList.length;
        this._navigateToIndex(newIndex);
    },

    /**
     * Navigates to a specific image by index.
     * @param {number} index - Index in the image list
     * @private
     */
    _navigateToIndex(index) {
        const { imageList } = this.state;
        if (index < 0 || index >= imageList.length) return;

        const newImage = imageList[index];

        // Reset zoom/pan for new image
        this._resetTransform();

        // Load the new image
        this._loadImage(newImage.id);

        // Update gallery selection to match
        App.setSelectedImages([newImage.id]);
    },

    /**
     * Exits fullscreen view and returns to gallery.
     * @private
     */
    _exit() {
        App.exitFullscreen();
    }
};

// Register module with App
App.registerModule('fullscreen', Fullscreen);
