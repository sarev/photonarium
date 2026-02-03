/**
 * @fileoverview Full-screen image viewer overlay for the Imaginary application.
 *
 * This module provides a modal overlay for full-screen image viewing with
 * zoom, pan, and navigation capabilities. It floats over whatever screen
 * is currently active - the underlying screen continues running normally.
 *
 * ARCHITECTURE:
 *   - True modal overlay (not part of screen navigation system)
 *   - Opens with fade-in transition, closes with fade-out
 *   - Underlying screen remains visible and active (just covered)
 *   - No state saving/restoring needed - screens maintain their own state
 *
 * RESPONSIBILITIES:
 *
 * Image Display:
 *   - Loads and displays the full-resolution image
 *   - Initially scales image to fit screen while preserving aspect ratio
 *   - Shows overlays (close button and filename) that fade after 3 seconds
 *   - Overlays reappear on any user interaction (mouse, keyboard, zoom, pan)
 *
 * Zoom Controls:
 *   - Mouse scroll wheel zooms in/out centered on cursor position
 *   - Touch pinch gesture zooms in/out centered on pinch midpoint
 *   - Double-tap/double-click toggles between fit-to-screen and 100% zoom
 *
 * Pan Controls:
 *   - Click-and-drag to pan when zoomed in
 *   - Touch drag to pan when zoomed in
 *   - Constrains panning to keep image edges visible
 *
 * Navigation:
 *   - Left/Right arrow keys navigate to previous/next image
 *   - Wraps from last image to first, and vice versa
 *   - Swipe left/right on touch devices for navigation
 *
 * API:
 *   - open(imageId): Shows overlay with the specified image
 *   - close(): Hides overlay with fade-out transition
 *   - isOpen(): Returns whether overlay is currently visible
 *
 * @module fullscreen
 * @requires core
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE

   Fullscreen module registration, state, and lifecycle hooks.
   ========================================================================== */

/**
 * Fullscreen viewer overlay module.
 * @namespace
 */
const Fullscreen = {
    /**
     * Local state for the fullscreen viewer.
     * @type {Object}
     * @property {boolean} isOpen - Whether the overlay is currently visible
     * @property {string|null} currentId - Currently displayed image ID
     * @property {Array<Object>} imageList - Reference to gallery's filtered/sorted images
     * @property {number} currentIndex - Index in imageList of current image
     * @property {number} zoom - Current zoom level (1 = fit to screen)
     * @property {number} panX - Current horizontal pan offset
     * @property {number} panY - Current vertical pan offset
     * @property {boolean} isPanning - Whether user is currently panning
     */
    state: {
        isOpen: false,
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
     * Timeout for hiding overlays (close button and filename).
     * @type {number|null}
     * @private
     */
    _overlayTimeout: null,

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
            overlay: App.$('fullscreen-overlay'),
            container: App.$('fullscreen-container'),
            image: App.$('fullscreen-image'),
            filename: App.$('fullscreen-filename'),
            closeBtn: App.$('fullscreen-close'),
            taggingBtn: App.$('fullscreen-tagging'),
            prevBtn: App.$('fullscreen-prev'),
            nextBtn: App.$('fullscreen-next')
        };

        // Bind button clicks (permanent, not per-session)
        this._els.closeBtn.addEventListener('click', () => {
            this.close();
        });
        this._els.taggingBtn.addEventListener('click', () => {
            // Toggle tagging mode via Faces module
            if (typeof Faces !== 'undefined' && Faces.toggleTaggingMode) {
                Faces.toggleTaggingMode();
                this._updateTaggingButton();
            }
        });
        this._els.prevBtn.addEventListener('click', () => {
            this._navigatePrev();
        });
        this._els.nextBtn.addEventListener('click', () => {
            this._navigateNext();
        });
    },

    /**
     * Updates the tagging button active state.
     * @private
     */
    _updateTaggingButton() {
        if (!this._els.taggingBtn) return;
        const isActive = typeof Faces !== 'undefined' && Faces.isTaggingModeActive?.();
        this._els.taggingBtn.classList.toggle('active', isActive);
    },

    /**
     * Opens the fullscreen overlay with the specified image.
     * @param {string} imageId - ID of the image to display
     * @param {Object} [options] - Optional settings
     * @param {Array<Object>} [options.imageList] - Custom image list for navigation context
     */
    open(imageId, options = {}) {
        if (this.state.isOpen) {
            // Already open - just navigate to the new image
            this._navigateToImage(imageId);
            return;
        }

        // Use provided image list if available, otherwise fall back to AppState
        if (options.imageList && options.imageList.length > 0) {
            this.state.imageList = options.imageList;
            this.state.currentIndex = this.state.imageList.findIndex(img => img.id === imageId);
        } else {
            // Get the current display list from AppState (sorted/filtered per current settings)
            this.state.imageList = AppState.images.getDisplayList();
            this.state.currentIndex = this.state.imageList.findIndex(img => img.id === imageId);
        }

        // If still not found, fetch the single image by ID
        if (this.state.currentIndex < 0) {
            this._loadSingleImage(imageId);
            return;
        }

        // Reset zoom/pan state
        this._resetTransform();

        // Load and display the image
        this._loadImage(imageId);

        // Bind event listeners
        this._bindEvents();

        // Show the overlay
        this._show();
    },

    /**
     * Navigate to a specific image (when already open).
     * @param {string} imageId - ID of the image to navigate to
     * @private
     */
    _navigateToImage(imageId) {
        const index = this.state.imageList.findIndex(img => img.id === imageId);
        if (index >= 0) {
            this.state.currentIndex = index;
            this._resetTransform();
            this._loadImage(imageId);
        }
    },

    /**
     * Load a single image by ID when it's not in the gallery's image list.
     * Used when navigating from Faces screen to an image not currently loaded.
     * @param {string} imageId - Image ID to load
     * @private
     */
    async _loadSingleImage(imageId) {
        try {
            const image = await AppState.images.fetchById(imageId);
            if (image) {
                // Create a single-image list
                this.state.imageList = [image];
                this.state.currentIndex = 0;

                // Reset zoom/pan state
                this._resetTransform();

                // Load and display the image
                this._loadImage(imageId);

                // Bind event listeners
                this._bindEvents();

                // Show the overlay
                this._show();
            } else {
                console.error('Image not found:', imageId);
            }
        } catch (error) {
            console.error('Failed to load image:', imageId, error);
        }
    },

    /**
     * Closes the fullscreen overlay.
     */
    close() {
        if (!this.state.isOpen) return;

        // Unbind event listeners
        this._unbindEvents();

        // Clear overlay timeout
        if (this._overlayTimeout) {
            clearTimeout(this._overlayTimeout);
            this._overlayTimeout = null;
        }

        // Hide the overlay
        this._hide();

        // Clear image source to free memory (after transition)
        setTimeout(() => {
            if (!this.state.isOpen) {
                this._els.image.src = '';
                this.state.currentId = null;
            }
        }, 300);
    },

    /**
     * Returns whether the overlay is currently open.
     * @returns {boolean}
     */
    isOpen() {
        return this.state.isOpen;
    },

    /**
     * Shows the overlay with fade-in transition.
     * @private
     */
    _show() {
        this.state.isOpen = true;
        // AppState already notified by _loadImage() which runs before this
        // Use requestAnimationFrame to ensure the class change triggers transition
        requestAnimationFrame(() => {
            this._els.overlay.classList.add('visible');
            // Focus overlay so keyboard events (arrows, escape) go here, not the underlying grid
            this._els.overlay.focus();
        });
    },

    /**
     * Hides the overlay with fade-out transition.
     * @private
     */
    _hide() {
        this.state.isOpen = false;
        // Broadcast closing event (for GUI sync) then clear AppState
        AppState.nav.closeFullscreen();
        this._els.overlay.classList.remove('visible');
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

        // Notify face overlay to update its transform
        App.emit('fullscreenTransformChanged', zoom, panX, panY);
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
            dblclick: (e) => this._handleDoubleClick(e),
            touchstart: (e) => this._handleTouchStart(e),
            touchmove: (e) => this._handleTouchMove(e),
            touchend: (e) => this._handleTouchEnd(e)
        };

        document.addEventListener('keydown', this._handlers.keydown);
        this._els.container.addEventListener('wheel', this._handlers.wheel, { passive: false });
        this._els.container.addEventListener('mousedown', this._handlers.mousedown);
        document.addEventListener('mousemove', this._handlers.mousemove);
        document.addEventListener('mouseup', this._handlers.mouseup);
        this._els.container.addEventListener('dblclick', this._handlers.dblclick);
        this._els.container.addEventListener('touchstart', this._handlers.touchstart, { passive: true });
        this._els.container.addEventListener('touchmove', this._handlers.touchmove, { passive: false });
        this._els.container.addEventListener('touchend', this._handlers.touchend, { passive: true });
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
        this._els.container.removeEventListener('touchstart', this._handlers.touchstart);
        this._els.container.removeEventListener('touchmove', this._handlers.touchmove);
        this._els.container.removeEventListener('touchend', this._handlers.touchend);

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
     * @param {number} [knownIndex] - If provided, use this index instead of searching.
     *                                This is needed when imageList has duplicate image IDs
     *                                (e.g., multiple faces on the same photo).
     * @private
     */
    _loadImage(imageId, knownIndex) {
        this.state.currentId = imageId;

        // Find image data
        const img = this.state.imageList.find(i => i.id === imageId);
        if (!img) {
            console.error('Image not found:', imageId);
            return;
        }

        // Update index - use provided index if available (avoids findIndex returning
        // wrong index when there are duplicate image IDs in the list)
        this.state.currentIndex = knownIndex !== undefined
            ? knownIndex
            : this.state.imageList.findIndex(i => i.id === imageId);

        // Load the full image
        this._els.image.src = App.imageUrl(imageId);

        // Show filename overlay with dimensions
        // If imageList entry lacks metadata (e.g., opened from faces screen),
        // try to get it from AppState.images cache
        const metadata = img.basename ? img : (AppState.images.getById(imageId) || img);
        this._showFilename(metadata.basename, metadata.width, metadata.height);

        // Preload adjacent images
        this._preloadAdjacent();

        // Notify AppState (triggers face overlay load, selection sync, etc.)
        AppState.nav.setFullscreenImageId(imageId);
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

        // Show all overlays
        this._showOverlays();
    },

    /**
     * Shows all overlays (close button, nav buttons, tagging button, filename) and schedules them to hide.
     * Called on user interaction to keep overlays visible while active.
     * @private
     */
    _showOverlays() {
        // Show overlays
        this._els.filename.classList.remove('hidden');
        this._els.closeBtn.classList.remove('hidden');
        this._els.taggingBtn.classList.remove('hidden');
        this._els.prevBtn.classList.remove('hidden');
        this._els.nextBtn.classList.remove('hidden');

        // Update tagging button state
        this._updateTaggingButton();

        // Clear any existing timeout
        if (this._overlayTimeout) {
            clearTimeout(this._overlayTimeout);
        }

        // Schedule hide
        this._overlayTimeout = setTimeout(() => {
            this._els.filename.classList.add('hidden');
            this._els.closeBtn.classList.add('hidden');
            this._els.taggingBtn.classList.add('hidden');
            this._els.prevBtn.classList.add('hidden');
            this._els.nextBtn.classList.add('hidden');
            this._overlayTimeout = null;
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

        // Note: Adjacent face preloading disabled - was causing SQLite contention
        // that made fullscreen navigation unresponsive. Faces are loaded on-demand
        // when navigating (minimal latency since face data is small).
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

        // Show overlays on zoom interaction
        this._showOverlays();

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
        // Don't toggle zoom if clicking on face tagging elements
        if (e.target.closest('.face-box, .face-label, .face-input, .face-delete-btn')) return;

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
        // Show overlays on click
        this._showOverlays();

        // Only pan with left button when zoomed in
        if (e.button !== 0 || this.state.zoom <= 1) return;

        // Don't start panning if clicking on face tagging elements
        // (allow default behavior so inputs can receive focus, buttons can be clicked)
        if (e.target.closest('.face-box, .face-label, .face-input, .face-delete-btn')) return;

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
     * Handles mouse move - shows overlays and handles panning.
     * @param {MouseEvent} e
     * @private
     */
    _handleMouseMove(e) {
        // Show overlays on mouse movement
        this._showOverlays();

        // Handle panning if active
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
       TOUCH SWIPE GESTURES

       Swipe left/right for navigation, swipe up to close.
       Visual feedback: image follows finger, then animates to complete or snap back.
       ---------------------------------------------------------------------- */

    /**
     * Minimum swipe distance in pixels to trigger navigation.
     * @type {number}
     * @constant
     */
    SWIPE_THRESHOLD: 50,

    /**
     * Starting touch position for swipe detection.
     * @type {{x: number, y: number, time: number}|null}
     * @private
     */
    _touchStart: null,

    /**
     * Whether a swipe gesture is actively in progress.
     * @type {boolean}
     * @private
     */
    _isSwiping: false,

    /**
     * Handles touch start - records starting position.
     * @param {TouchEvent} e
     * @private
     */
    _handleTouchStart(e) {
        // Only track single-finger touches for swipe
        if (e.touches.length !== 1) {
            this._touchStart = null;
            return;
        }

        this._touchStart = {
            x: e.touches[0].clientX,
            y: e.touches[0].clientY,
            time: Date.now()
        };
        this._isSwiping = false;
    },

    /**
     * Handles touch move - translates image to follow finger.
     * @param {TouchEvent} e
     * @private
     */
    _handleTouchMove(e) {
        // Only process single-finger swipes when not zoomed
        if (!this._touchStart || e.touches.length !== 1 || this.state.zoom > 1) return;

        const dx = e.touches[0].clientX - this._touchStart.x;
        const dy = e.touches[0].clientY - this._touchStart.y;
        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);

        // Determine swipe direction once movement exceeds threshold
        if (!this._isSwiping && (absDx > 10 || absDy > 10)) {
            // Horizontal swipe
            if (absDx > absDy) {
                this._isSwiping = true;
                this._swipeDirection = 'horizontal';
                this._hideFaceOverlay();
            }
            // Upward vertical swipe
            else if (dy < -10) {
                this._isSwiping = true;
                this._swipeDirection = 'vertical';
                this._hideFaceOverlay();
            }
        }

        if (!this._isSwiping) return;

        e.preventDefault();

        // Apply transform to follow finger
        if (this._swipeDirection === 'horizontal') {
            this._els.image.style.transition = 'none';
            this._els.image.style.transform = `translateX(${dx}px)`;
        } else if (this._swipeDirection === 'vertical' && dy < 0) {
            // Only allow upward movement for close gesture
            const opacity = Math.max(0.3, 1 + dy / 300);
            this._els.image.style.transition = 'none';
            this._els.image.style.transform = `translateY(${dy}px)`;
            this._els.image.style.opacity = opacity;
        }
    },

    /**
     * Handles touch end - completes or cancels swipe with animation.
     * @param {TouchEvent} e
     * @private
     */
    _handleTouchEnd(e) {
        if (!this._touchStart) return;

        const touch = e.changedTouches[0];
        const dx = touch.clientX - this._touchStart.x;
        const dy = touch.clientY - this._touchStart.y;
        const elapsed = Date.now() - this._touchStart.time;

        const wasSwiping = this._isSwiping;
        const direction = this._swipeDirection;

        this._touchStart = null;
        this._isSwiping = false;
        this._swipeDirection = null;

        // If zoomed or wasn't swiping, just reset
        if (this.state.zoom > 1 || !wasSwiping) {
            this._resetSwipeTransform();
            return;
        }

        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);
        const velocity = direction === 'horizontal' ? absDx / elapsed : absDy / elapsed;

        // Check if swipe should complete (past threshold or fast enough)
        const shouldComplete = direction === 'horizontal'
            ? (absDx > this.SWIPE_THRESHOLD || velocity > 0.3)
            : (dy < -this.SWIPE_THRESHOLD || velocity > 0.3);

        if (shouldComplete && direction === 'horizontal') {
            // Animate off-screen then navigate
            const targetX = dx < 0 ? -window.innerWidth : window.innerWidth;
            this._els.image.style.transition = 'transform 0.2s ease-out';
            this._els.image.style.transform = `translateX(${targetX}px)`;

            setTimeout(() => {
                if (dx < 0) {
                    this._navigateNext();
                } else {
                    this._navigatePrev();
                }
                this._resetSwipeTransform();
                this._showFaceOverlay();
            }, 200);
        } else if (shouldComplete && direction === 'vertical' && dy < 0) {
            // Animate up and fade out then close
            this._els.image.style.transition = 'transform 0.2s ease-out, opacity 0.2s ease-out';
            this._els.image.style.transform = `translateY(${-window.innerHeight}px)`;
            this._els.image.style.opacity = '0';

            setTimeout(() => {
                this._resetSwipeTransform();
                this.close();
            }, 200);
        } else {
            // Snap back
            this._els.image.style.transition = 'transform 0.2s ease-out, opacity 0.2s ease-out';
            this._els.image.style.transform = 'translateX(0) translateY(0)';
            this._els.image.style.opacity = '1';

            setTimeout(() => {
                this._resetSwipeTransform();
                this._showFaceOverlay();
            }, 200);
        }
    },

    /**
     * Resets swipe transform styles on the image.
     * @private
     */
    _resetSwipeTransform() {
        this._els.image.style.transition = '';
        this._els.image.style.transform = '';
        this._els.image.style.opacity = '';
        this._applyTransform(); // Restore zoom/pan transform
    },

    /**
     * Hides the face overlay during swipe animation.
     * @private
     */
    _hideFaceOverlay() {
        const overlay = document.getElementById('face-overlay');
        if (overlay) overlay.style.visibility = 'hidden';
    },

    /**
     * Shows the face overlay after swipe animation.
     * @private
     */
    _showFaceOverlay() {
        const overlay = document.getElementById('face-overlay');
        if (overlay) overlay.style.visibility = '';
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
        // Show overlays on any key interaction
        this._showOverlays();

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

        // Load the new image (pass index to handle duplicate image IDs correctly)
        this._loadImage(newImage.id, index);

        // Update gallery selection to match
        App.setSelectedImages([newImage.id]);
    },

    /**
     * Exits fullscreen view and returns to gallery.
     * @private
     */
    _exit() {
        this.close();
    }
};

// Initialize module when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Fullscreen.init());
} else {
    Fullscreen.init();
}
