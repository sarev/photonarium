/**
 * @fileoverview Full-screen image viewer overlay for the Photonarium application.
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
 *   - Mouse scroll wheel zooms in/out centred on cursor position
 *   - Touch pinch gesture zooms in/out centred on pinch midpoint
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
     * Curated quick-rating palette for the bottom-left widget.
     *
     * Each option renders as monochrome line-art SVG (see _ratingSvg) but is
     * *stored* as a plain emoji string in the free-form `images.rating` field,
     * so the Gallery sidebar and Search (substring match) keep working with no
     * schema or query changes. Selecting an option REPLACES any existing
     * rating (including custom emoji strings the user typed elsewhere) — this
     * is a deliberate simplification: the widget offers a fast, fixed set, not
     * the full emoji vocabulary.
     *
     * Stars are modelled as a run of 1–3 star emoji ('⭐', '⭐⭐', '⭐⭐⭐'),
     * which substring search treats naturally; the optional "Exact match"
     * search toggle distinguishes one star from two or three.
     * @type {Array<{value: string, kind: string, stars?: number, svg?: string, label: string}>}
     */
    RATING_OPTIONS: [
        { value: '⭐', kind: 'star', stars: 1, label: '1 star' },
        { value: '⭐⭐', kind: 'star', stars: 2, label: '2 stars' },
        { value: '⭐⭐⭐', kind: 'star', stars: 3, label: '3 stars' },
        { value: '\u{1F641}', kind: 'icon', svg: 'face-sad', label: 'Unhappy' },
        { value: '\u{1F610}', kind: 'icon', svg: 'face-neutral', label: 'Neutral' },
        { value: '\u{1F642}', kind: 'icon', svg: 'face-happy', label: 'Happy' },
        { value: '\u{1F44E}', kind: 'icon', svg: 'thumb-down', label: 'Thumb down' },
        { value: '❤️', kind: 'icon', svg: 'heart', label: 'Love' },
        { value: '\u{1F44D}', kind: 'icon', svg: 'thumb-up', label: 'Thumb up' },
    ],

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
        isPanning: false,
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
     * setTimeout ID for the next slideshow advance.
     * @type {number|null}
     * @private
     */
    _slideshowTimer: null,

    /**
     * Whether a slideshow is currently running.
     * @type {boolean}
     * @private
     */
    _slideshowActive: false,

    /**
     * Whether the current slideshow is in shuffle mode.
     * @type {boolean}
     * @private
     */
    _slideshowShuffled: false,

    /**
     * Pre-shuffled index order for shuffle mode (null for linear).
     * @type {number[]|null}
     * @private
     */
    _slideshowOrder: null,

    /**
     * Current position within _slideshowOrder (or imageList for linear).
     * @type {number}
     * @private
     */
    _slideshowPosition: -1,

    /**
     * Last significant mouse position during slideshow (pixel coordinates).
     * Null until the first mouse event establishes a baseline.
     * Subsequent moves must exceed MOUSE_DEADZONE_PX from this point to count.
     * @type {{x: number, y: number}|null}
     * @private
     */
    _lastMousePos: null,

    /**
     * Minimum pixel distance from the last significant mouse position before
     * a movement is treated as intentional during slideshow.
     * @type {number}
     * @constant
     */
    MOUSE_DEADZONE_PX: 16,

    /**
     * Duration of each half of the slideshow cross-fade (fade-out + fade-in).
     * @type {number}
     * @constant
     */
    CROSSFADE_MS: 400,

    /**
     * Timer ID for an in-progress cross-fade transition.
     * @type {number|null}
     * @private
     */
    _crossFadeTimer: null,

    /**
     * Preloaded Image for the next slideshow advance.  Created during the
     * hold period by _scheduleSlideshowAdvance() so the browser has the
     * full image cached before the cross-fade begins.
     * @type {{ id: string, img: HTMLImageElement }|null}
     * @private
     */
    _slideshowPreload: null,

    /**
     * Unsubscribe function for AppState.images.onChanged subscription.
     * Active while fullscreen is open so we can prune trashed images
     * from the navigation list (including deletions by other clients).
     * @type {Function|null}
     * @private
     */
    _unsubImages: null,

    /**
     * Whether overlays are currently visible (avoids redundant DOM work).
     * @type {boolean}
     * @private
     */
    _overlaysVisible: false,

    /**
     * Cached container rect to avoid forced reflows during zoom/pan.
     * Invalidated on open, close, and window resize.
     * @type {DOMRect|null}
     * @private
     */
    _cachedContainerRect: null,

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
            toolbar: App.$('fullscreen-toolbar'),
            closeBtn: App.$('fullscreen-close'),
            taggingBtn: App.$('fullscreen-tagging'),
            ignoreBtn: App.$('fullscreen-ignore'),
            enhanceBtn: App.$('fullscreen-enhance'),
            rotateLeftBtn: App.$('fullscreen-rotate-left'),
            rotateRightBtn: App.$('fullscreen-rotate-right'),
            prevBtn: App.$('fullscreen-prev'),
            nextBtn: App.$('fullscreen-next'),
            slideshowBtn: App.$('fullscreen-slideshow'),
            shuffleBtn: App.$('fullscreen-shuffle'),
            video: App.$('fullscreen-video'),
            rating: App.$('fullscreen-rating'),
            ratingTrigger: App.$('fullscreen-rating-trigger'),
            ratingPopup: App.$('fullscreen-rating-popup'),
            ratingCurrent: App.$('fullscreen-rating-trigger')?.querySelector('.fs-rating-current'),
        };

        // Build the quick-rating palette once (its contents never change)
        this._buildRatingGrid();

        // Clear the "dismissed" state when the pointer leaves the rating widget,
        // so hovering again reopens the popup.  It is force-closed on select,
        // navigation and exit (see _dismissRatingPopup).
        this._els.rating?.addEventListener('mouseleave', () => {
            this._els.rating.classList.remove('fs-rating-dismissed');
        });

        // Bind button clicks (permanent, not per-session)
        this._els.closeBtn.addEventListener('click', () => {
            this.close();
        });

        // Slideshow: clicking the active mode stops; clicking the other switches
        this._els.slideshowBtn.addEventListener('click', () => {
            if (this._slideshowActive && !this._slideshowShuffled) {
                this.stopSlideshow();
            } else {
                this.startSlideshow(false);
            }
        });
        this._els.shuffleBtn.addEventListener('click', () => {
            if (this._slideshowActive && this._slideshowShuffled) {
                this.stopSlideshow();
            } else {
                this.startSlideshow(true);
            }
        });
        this._els.taggingBtn.addEventListener('click', () => {
            this._toggleFaceTagging();
        });
        this._els.ignoreBtn.addEventListener('click', () => {
            this._ignoreUnknownFaces();
        });
        this._els.enhanceBtn?.addEventListener('click', () => {
            this._openEnhanceDialog();
        });
        // Enhance dialog: cancel, delegated capability selection (→ preview),
        // and confirm (→ commit the full-resolution run).  Bound once.
        App.$('dialog-enhance-cancel')?.addEventListener('click', () => {
            App.$('dialog-enhance')?.close();
        });
        // Closing the dialog (Cancel, Esc, or after confirm) must cancel any
        // in-progress crop drag so a late pointerup can't fire a stray preview.
        App.$('dialog-enhance')?.addEventListener('close', () => {
            this._cancelEnhanceDrag?.();
        });
        App.$('enhance-options')?.addEventListener('click', (e) => {
            const btn = e.target.closest('.enhance-option');
            if (btn) this._previewEnhance(btn.dataset.recipe);
        });
        App.$('dialog-enhance-confirm')?.addEventListener('click', () => {
            this._submitEnhance(this._enhanceRecipe);
        });
        // Strength slider: live client-side blend by setting the overlay opacity.
        App.$('enhance-strength')?.addEventListener('input', (e) => {
            const pct = Number(e.target.value);
            const afterImg = App.$('enhance-preview-after');
            if (afterImg) afterImg.style.opacity = String(pct / 100);
            const label = App.$('enhance-strength-value');
            if (label) label.textContent = `${pct}%`;
        });
        this._els.rotateLeftBtn.addEventListener('click', () => {
            this._rotateImage(270);
        });
        this._els.rotateRightBtn.addEventListener('click', () => {
            this._rotateImage(90);
        });
        this._els.prevBtn.addEventListener('click', () => {
            this._navigatePrev();
        });
        this._els.nextBtn.addEventListener('click', () => {
            this._navigateNext();
        });
    },

    /* ------------------------------------------------------------------
       QUICK RATING WIDGET (bottom-left)

       A fixed palette of star runs + sentiment icons, rendered as
       monochrome SVG line-art (offline, theme-consistent, no emoji-font
       reliance) but persisted as plain emoji into the free-form rating
       field via AppState. See RATING_OPTIONS for the model.
       ------------------------------------------------------------------ */

    /**
     * Returns the inner SVG markup for a named rating icon. Stroke follows
     * currentColor so the three visual states (rest / hover / selected) are
     * driven purely by CSS, exactly as in the design concept.
     * @param {string} name - Icon name: 'star', 'face-sad', 'face-neutral',
     *   'face-happy', 'thumb-down', 'thumb-up', 'heart'
     * @returns {string} SVG element markup
     * @private
     */
    _ratingSvg(name) {
        // Mouth path varies per sentiment face; head + eyes are shared.
        const FACE_MOUTHS = {
            'face-sad': 'M8.5 16 Q12 12.5 15.5 16',
            'face-neutral': 'M8.5 15 L15.5 15',
            'face-happy': 'M8.5 14.5 Q12 18 15.5 14.5',
        };
        if (name in FACE_MOUTHS) {
            return '<svg class="fs-rating-icon face" viewBox="0 0 24 24" aria-hidden="true">'
                + '<circle class="head" cx="12" cy="12" r="9.5"/>'
                + '<circle class="eye" cx="9" cy="10" r="1.1"/><circle class="eye" cx="15" cy="10" r="1.1"/>'
                + `<path class="mouth" d="${FACE_MOUTHS[name]}"/></svg>`;
        }
        const PATHS = {
            star: 'M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z',
            'thumb-down': 'M15 3H6c-.83 0-1.54.5-1.84 1.22L1.14 11.27c-.09.23-.14.47-.14.73v2c0 1.1.9 2 2 2h6.31l-.95 4.57-.03.32c0 .41.17.79.44 1.06L9.83 23l6.59-6.59c.36-.36.58-.86.58-1.41V5c0-1.1-.9-2-2-2zm4 0v12h4V3h-4z',
            'thumb-up': 'M1 21h4V9H1v12zM23 10c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z',
            heart: 'M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z',
        };
        return `<svg class="fs-rating-icon solid" viewBox="0 0 24 24" aria-hidden="true"><path d="${PATHS[name]}"/></svg>`;
    },

    /**
     * Builds the quick-rating palette grid once during init. Star cells get a
     * shared `.star` class so CSS `:has()` can render the run cascade; each
     * cell carries its option index for the click handler.
     * @private
     */
    _buildRatingGrid() {
        const popup = this._els.ratingPopup;
        if (!popup) return;

        popup.innerHTML = this.RATING_OPTIONS.map((opt, i) => {
            const svg = this._ratingSvg(opt.kind === 'star' ? 'star' : opt.svg);
            const starCls = opt.kind === 'star' ? ' star' : '';
            return `<button type="button" class="fs-rating-cell${starCls}" data-index="${i}"`
                + ` title="${App.escapeHtml(opt.label)}" aria-label="${App.escapeHtml(opt.label)}"`
                + ` aria-pressed="false">${svg}</button>`;
        }).join('');

        // Single delegated click handler for the whole palette
        popup.addEventListener('click', (e) => {
            const cell = e.target.closest('.fs-rating-cell');
            if (!cell) return;
            const opt = this.RATING_OPTIONS[Number(cell.dataset.index)];
            if (opt) this._setRating(opt.value);
            // Close the popup after a selection (don't leave it hanging open).
            this._dismissRatingPopup();
        });
    },

    /**
     * Force-closes the rating popup — on select, navigation or exit — even
     * while the pointer is still over it.  The class is cleared on the next
     * mouseleave so hovering reopens it.  Also drops focus from any rating cell
     * so :focus-within does not hold the popup open.
     * @private
     */
    _dismissRatingPopup() {
        const el = this._els.rating;
        if (!el) return;
        el.classList.add('fs-rating-dismissed');
        if (el.contains(document.activeElement)) {
            document.activeElement.blur();
        }
    },

    /**
     * Persists a rating selection. Clicking the already-current rating clears
     * it; otherwise the chosen value REPLACES whatever was stored. Writes go
     * through AppState (single source of truth), which optimistically updates
     * the cache and notifies subscribers — including the Gallery sidebar.
     * @param {string} value - The emoji string for the chosen option
     * @private
     */
    async _setRating(value) {
        const imageId = this.state.currentId;
        if (!imageId) return;

        const current = AppState.images.getById(imageId)?.rating || '';
        const next = (current === value) ? '' : value;

        // Optimistically paint the widget before the async round-trip
        this._updateRatingWidget(next);

        try {
            await AppState.images.update({ id: imageId, rating: next });
        } catch (error) {
            console.error('Failed to save rating:', error);
            App.showError('Failed to save rating.');
            // AppState rolls back on error; repaint from the cache
            this._updateRatingWidget(AppState.images.getById(imageId)?.rating || '');
        }
    },

    /**
     * Refreshes the rating trigger and palette selection to reflect a value.
     *
     * The trigger shows: the matching monochrome SVG(s) when the rating is one
     * of the curated values; the raw rating text for a custom string (so the
     * user can see they've rated it, even though the palette can't represent
     * it); or a muted "Rating…" prompt when empty. The palette highlights the
     * matching cell, lighting the star run up to N.
     * @param {string} [rating] - Rating value; defaults to the current image's
     * @private
     */
    _updateRatingWidget(rating) {
        if (!this._els.rating) return;
        if (rating === undefined) {
            rating = AppState.images.getById(this.state.currentId)?.rating || '';
        }

        const matched = this.RATING_OPTIONS.find(o => o.value === rating);

        // Trigger content
        if (this._els.ratingCurrent) {
            if (!rating) {
                this._els.ratingCurrent.className = 'fs-rating-current empty';
                this._els.ratingCurrent.textContent = 'Rating…';
            } else if (matched && matched.kind === 'star') {
                this._els.ratingCurrent.className = 'fs-rating-current';
                this._els.ratingCurrent.innerHTML = this._ratingSvg('star').repeat(matched.stars);
            } else if (matched) {
                this._els.ratingCurrent.className = 'fs-rating-current';
                this._els.ratingCurrent.innerHTML = this._ratingSvg(matched.svg);
            } else {
                // Custom string we can't render as line-art — show it verbatim
                this._els.ratingCurrent.className = 'fs-rating-current custom';
                this._els.ratingCurrent.textContent = rating;
            }
        }

        // Palette selection state
        const cells = this._els.ratingPopup?.querySelectorAll('.fs-rating-cell');
        cells?.forEach((cell) => {
            const opt = this.RATING_OPTIONS[Number(cell.dataset.index)];
            const on = opt.kind === 'star'
                ? (!!matched && matched.kind === 'star' && opt.stars <= matched.stars)
                : (opt.value === rating);
            cell.classList.toggle('selected', on);
            cell.setAttribute('aria-pressed', String(on));
        });

        if (this._els.ratingTrigger) {
            this._els.ratingTrigger.setAttribute('title', rating ? `Rated ${rating}` : 'Rate this image');
        }
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
     * Updates fullscreen rotate button states based on the current image.
     * RAW files cannot be rotated, so the buttons are disabled with a tooltip.
     * @param {Object} [metadata] - Image metadata with basename property
     * @private
     */
    _updateRotateButtons(metadata) {
        const isRaw = metadata && App.isRawFile(metadata.basename);
        if (this._els.rotateLeftBtn) {
            this._els.rotateLeftBtn.disabled = isRaw;
            this._els.rotateLeftBtn.title = isRaw ? 'Cannot rotate RAW files' : 'Rotate left (Ctrl+L)';
        }
        if (this._els.rotateRightBtn) {
            this._els.rotateRightBtn.disabled = isRaw;
            this._els.rotateRightBtn.title = isRaw ? 'Cannot rotate RAW files' : 'Rotate right (Ctrl+R)';
        }
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

        // Store seek-to time and autoplay flag for video playback
        this._pendingSeekTo = options.seekTo ?? null;
        this._pendingAutoplay = options.autoplay ?? false;

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

        // Fresh open starts with the rating popup closed (clear any stale
        // dismissed state from a previous session).
        this._els.rating?.classList.remove('fs-rating-dismissed');

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

        // Stop slideshow if running
        this.stopSlideshow();

        // Unbind event listeners
        this._unbindEvents();

        // Clear overlay timeout and visibility flag
        if (this._overlayTimeout) {
            clearTimeout(this._overlayTimeout);
            this._overlayTimeout = null;
        }
        this._overlaysVisible = false;

        // Hide the overlay
        this._hide();

        // Pause and hide video if playing
        if (this._els.video) {
            this._els.video.pause();
            this._els.video.removeAttribute('src');
            this._els.video.hidden = true;
        }

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
     * Returns the container rect, using a cache to avoid forced reflows
     * during rapid zoom/pan interactions.
     * @returns {DOMRect}
     * @private
     */
    _getContainerRect() {
        if (!this._cachedContainerRect) {
            this._cachedContainerRect = this._els.container.getBoundingClientRect();
        }
        return this._cachedContainerRect;
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
            touchend: (e) => this._handleTouchEnd(e),
            resize: () => { this._cachedContainerRect = null; },
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
        window.addEventListener('resize', this._handlers.resize);

        // Subscribe to image changes so we can prune trashed images from
        // the navigation list — including deletions by other clients that
        // arrive via event polling.
        this._unsubImages = AppState.images.onChanged(() => this._onImagesChanged());

        // Populate container rect cache now that we're open
        this._cachedContainerRect = null;
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
        window.removeEventListener('resize', this._handlers.resize);

        if (this._unsubImages) {
            this._unsubImages();
            this._unsubImages = null;
        }

        this._handlers = {};
        this._cachedContainerRect = null;
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

        const metadata = img.basename ? img : (AppState.images.getById(imageId) || img);
        const isVideo = metadata.media_type === 'video';

        if (isVideo) {
            // Video: hide image, show and load video element
            this._els.image.hidden = true;
            this._els.image.src = '';
            this._els.video.hidden = false;
            this._removeCaptionTrack(this._els.video);
            this._els.video.src = ThumbnailLoader.getFullImageUrl(imageId);

            // Seek to requested time and/or autoplay on load
            const seekTo = this._pendingSeekTo;
            const autoplay = this._pendingAutoplay;
            this._pendingSeekTo = null;
            this._pendingAutoplay = false;
            if (seekTo != null && seekTo > 0) {
                this._els.video.addEventListener('loadedmetadata', () => {
                    this._els.video.currentTime = seekTo;
                    if (autoplay) this._els.video.play();
                }, { once: true });
            } else if (autoplay) {
                this._els.video.addEventListener('loadedmetadata', () => {
                    this._els.video.play();
                }, { once: true });
            }
            this._els.video.load();

            // Add WebVTT captions track for scene transcriptions
            this._addCaptionTrack(this._els.video, imageId);

            // Show filename with duration instead of dimensions
            this._showFilename(metadata.basename, null, null, metadata.duration);

            // Disable image-only toolbar buttons for videos
            this._els.taggingBtn.disabled = true;
            this._els.ignoreBtn.disabled = true;
            this._els.rotateLeftBtn.disabled = true;
            this._els.rotateRightBtn.disabled = true;
        } else {
            // Image: hide video, show image
            if (this._els.video) {
                this._els.video.pause();
                this._els.video.removeAttribute('src');
                this._els.video.hidden = true;
                this._removeCaptionTrack(this._els.video);
            }
            this._els.image.hidden = false;

            // Load the full image (with cache-bust if image was recently modified)
            this._els.image.src = ThumbnailLoader.getFullImageUrl(imageId);
            this._els.image.alt = img.basename || '';

            // Show filename overlay with dimensions
            this._showFilename(metadata.basename, metadata.width, metadata.height);

            // Re-enable toolbar buttons for images
            this._els.taggingBtn.disabled = false;
            this._els.ignoreBtn.disabled = false;

            // Disable rotate buttons for RAW files (which cannot be modified)
            this._updateRotateButtons(metadata);

            // Preload adjacent images
            this._preloadAdjacent();
        }

        // Reflect this image's current rating in the quick-rating widget
        this._updateRatingWidget(metadata.rating || '');

        // Notify AppState (triggers face overlay load, selection sync, etc.)
        AppState.nav.setFullscreenImageId(imageId);
    },

    /**
     * Add a WebVTT captions track to a video element for scene transcriptions.
     * @param {HTMLVideoElement} video - The video element
     * @param {string} imageId - Image/video UUID for the subtitles API
     * @private
     */
    _addCaptionTrack(video, imageId) {
        this._removeCaptionTrack(video);
        const track = document.createElement('track');
        track.kind = 'captions';
        track.src = `/api/images/${imageId}/subtitles.vtt`;
        track.srclang = App.config?.stt_language || 'en';
        track.label = 'Transcription';
        track.default = true;
        video.appendChild(track);
        track.addEventListener('load', () => { track.track.mode = 'showing'; });
    },

    /**
     * Remove any existing caption tracks from a video element.
     * @param {HTMLVideoElement} video - The video element
     * @private
     */
    _removeCaptionTrack(video) {
        // Disable all text tracks first to immediately clear displayed cues,
        // then remove the <track> element.  Without this, stale subtitles
        // linger on screen while the next video loads.
        for (const t of video.textTracks) {
            t.mode = 'disabled';
        }
        const existing = video.querySelector('track');
        if (existing) existing.remove();
    },

    /**
     * Shows the filename overlay and schedules it to hide.
     * @param {string} filename - Filename to display
     * @param {number} [width] - Image width in pixels
     * @param {number} [height] - Image height in pixels
     * @private
     */
    _showFilename(filename, width, height, duration) {
        const el = this._els.filename;

        // Build display text with optional dimensions or duration
        let text = filename;
        if (duration != null) {
            const mins = Math.floor(duration / 60);
            const secs = Math.floor(duration % 60);
            text += ` (${mins}:${String(secs).padStart(2, '0')})`;
        } else if (width && height) {
            text += ` (${width} × ${height})`;
        }
        el.textContent = text;

        // Show all overlays
        this._showOverlays();
    },

    /**
     * Shows all overlays (toolbar, nav buttons, filename) and schedules them to hide.
     * Called on user interaction to keep overlays visible while active.
     * @private
     */
    _showOverlays() {
        // During slideshow auto-advance, suppress overlay display so the
        // toolbar/filename don't flash on every image transition.  User
        // interactions (mouse move, key press, etc.) still show overlays.
        if (this._slideshowAdvancing) return;

        // Only do DOM work if overlays aren't already visible
        if (!this._overlaysVisible) {
            this._els.filename.classList.remove('hidden');
            this._els.toolbar.classList.remove('hidden');
            this._els.prevBtn.classList.remove('hidden');
            this._els.nextBtn.classList.remove('hidden');
            this._els.rating?.classList.remove('hidden');
            this._updateTaggingButton();
            this._overlaysVisible = true;
        }

        // Reset the hide timer (lightweight — just timer management)
        if (this._overlayTimeout) {
            clearTimeout(this._overlayTimeout);
        }
        this._overlayTimeout = setTimeout(() => {
            this._els.filename.classList.add('hidden');
            this._els.toolbar.classList.add('hidden');
            this._els.prevBtn.classList.add('hidden');
            this._els.nextBtn.classList.add('hidden');
            this._els.rating?.classList.add('hidden');
            this._overlaysVisible = false;
            this._overlayTimeout = null;
        }, this.FILENAME_DISPLAY_MS);

        // Reset slideshow advance timer on user interaction (natural pause-on-interact)
        if (this._slideshowActive) {
            this._scheduleSlideshowAdvance();
        }
    },

    /**
     * Preloads the previous and next images for smooth navigation.
     * @private
     */
    _preloadAdjacent() {
        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        const nextIndex = (currentIndex + 1) % imageList.length;

        // Preload the next image only — forward navigation is far more
        // common than backward, and each preload consumes a browser
        // connection that competes with the current image display.
        // Skip video items — new Image() does not work for video preloading.
        const nextImg = imageList[nextIndex];
        if (nextImg.media_type !== 'video') {
            const preloadNext = new Image();
            preloadNext.src = ThumbnailLoader.getFullImageUrl(nextImg.id);
        }

        // During shuffle slideshow, the next advance target may differ from
        // the index-adjacent images — preload it too so the cross-fade is smooth.
        if (this._slideshowActive && this._slideshowShuffled) {
            const shuffleNext = this._getNextSlideshowTarget();
            const shuffleImg = imageList[shuffleNext];
            if (shuffleNext !== nextIndex && shuffleImg.media_type !== 'video') {
                const preloadShuffle = new Image();
                preloadShuffle.src = ThumbnailLoader.getFullImageUrl(shuffleImg.id);
            }
        }

        // Preload face data for adjacent images into AppState cache so
        // bboxes appear instantly on navigation (no API round-trip).
        // Only when tagging mode is active — no point fetching face data
        // that won't be displayed.  Deferred so the current image and its
        // face overlay load first — firing immediately would saturate the
        // browser's per-origin connection limit (~6) and delay display.
        // (Previously disabled due to SQLite contention — resolved by
        // single-writer SafeConnection.)
        if (typeof Faces !== 'undefined' && Faces.isTaggingModeActive?.()) {
            setTimeout(() => {
                AppState.faces.fetchForImage(nextImg.id);
            }, 500);
        }
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
       ENHANCE DIALOG

       Lists the enhancement capabilities the backend currently offers (only
       those whose model weights are downloaded) and submits the chosen one.
       Enhancement runs in the background; the result is ingested as a new
       version of the original and announced via the enhance_complete event.
       ---------------------------------------------------------------------- */

    /**
     * Open the Enhance dialog for the current image and populate it with the
     * capabilities the backend can currently offer.
     * @private
     */
    async _openEnhanceDialog() {
        const img = this._getCurrentImage();
        if (!img) return;
        if (img.media_type === 'video') {
            App.showInfo('Enhancement applies to photos, not videos.');
            return;
        }

        const dialog = App.$('dialog-enhance');
        const optionsEl = App.$('enhance-options');
        const emptyEl = App.$('enhance-empty');
        if (!dialog || !optionsEl) return;

        this._enhanceImageId = img.id;
        this._enhanceRecipe = null;
        // Crop state for the pannable preview: native source dimensions, the
        // square crop side, and the chosen top-left (null → centred on the first
        // preview).  The panned region persists across capability switches.
        this._enhanceImgW = img.width || 0;
        this._enhanceImgH = img.height || 0;
        this._enhanceCropSide = Math.min(256, this._enhanceImgW || 256, this._enhanceImgH || 256);
        this._enhanceCrop = null;
        this._setupEnhanceCropDrag();
        // Load the full-resolution original into both panes' base layers so the
        // user can pan over it entirely client-side (no round-trip per drag).
        const fullUrl = ThumbnailLoader.getFullImageUrl(img.id);
        const beforeBaseImg = App.$('enhance-preview-before');
        const afterBaseImg = App.$('enhance-preview-after-base');
        if (beforeBaseImg) beforeBaseImg.src = fullUrl;
        if (afterBaseImg) afterBaseImg.src = fullUrl;
        // Catalogue metadata should carry dimensions, but fall back to the
        // loaded image's natural size if they're missing or zero — otherwise the
        // Before pane would size to 0px and panning would be disabled.
        if ((!this._enhanceImgW || !this._enhanceImgH) && beforeBaseImg) {
            beforeBaseImg.addEventListener('load', () => {
                this._enhanceImgW = beforeBaseImg.naturalWidth || this._enhanceImgW;
                this._enhanceImgH = beforeBaseImg.naturalHeight || this._enhanceImgH;
                this._enhanceCropSide = Math.min(256, this._enhanceImgW || 256, this._enhanceImgH || 256);
                if (this._enhanceCrop) this._positionCropView(this._enhanceCrop.left, this._enhanceCrop.top);
            }, { once: true });
        }
        optionsEl.innerHTML = '<div class="enhance-loading">Loading…</div>';
        if (emptyEl) emptyEl.hidden = true;
        // Reset preview + confirm to their initial (pick-a-capability) state.
        const previewEl = App.$('enhance-preview');
        if (previewEl) previewEl.hidden = true;
        const confirmBtn = App.$('dialog-enhance-confirm');
        if (confirmBtn) confirmBtn.hidden = true;
        dialog.showModal();

        let caps = [];
        try {
            const resp = await App.apiGet('/enhance/capabilities');
            caps = resp?.data?.capabilities || [];
        } catch (err) {
            console.error('[Enhance] failed to load capabilities', err);
        }
        this._enhanceCaps = caps;

        if (!caps.length) {
            optionsEl.innerHTML = '';
            if (emptyEl) emptyEl.hidden = false;
            return;
        }

        optionsEl.innerHTML = caps.map((c) => (
            `<button type="button" class="enhance-option" data-recipe="${App.escapeHtml(c.key)}">`
            + `<span class="enhance-option-label">${App.escapeHtml(c.label)}</span>`
            + `<span class="enhance-option-desc">${App.escapeHtml(c.description)}</span>`
            + '</button>'
        )).join('');
    },

    /**
     * Render a before/after preview for a capability on a fast centre-crop,
     * then reveal the "Save as new version" confirm button.
     * @param {string} recipe - Capability key (e.g. 'denoise').
     * @private
     */
    async _previewEnhance(recipe) {
        const imageId = this._enhanceImageId;
        if (!imageId || !recipe) return;
        this._enhanceRecipe = recipe;

        const previewEl = App.$('enhance-preview');
        const afterImg = App.$('enhance-preview-after');
        const spinner = App.$('enhance-preview-spinner');
        const confirmBtn = App.$('dialog-enhance-confirm');
        const strengthRow = App.$('enhance-strength-row');
        const strengthInput = App.$('enhance-strength');
        const strengthValue = App.$('enhance-strength-value');

        // Show a strength slider only for capabilities that support it
        // (restoration — denoise/deblur), reset to 100% on each selection.
        const cap = (this._enhanceCaps || []).find((c) => c.key === recipe);
        const supportsStrength = !!cap?.strength;
        if (strengthRow) strengthRow.hidden = !supportsStrength;
        if (supportsStrength && strengthInput) {
            strengthInput.value = '100';
            if (strengthValue) strengthValue.textContent = '100%';
        }
        if (afterImg) afterImg.style.opacity = '1';

        // Mark the chosen option and show the preview area with a spinner.
        App.$('enhance-options')?.querySelectorAll('.enhance-option').forEach((b) => {
            b.classList.toggle('selected', b.dataset.recipe === recipe);
        });
        // Resolve the crop: the user's panned region, or a centred crop the
        // first time.  Position both panes' base layers to show it, then request
        // just the enhanced overlay for that exact crop.
        const side = this._enhanceCropSide;
        if (!this._enhanceCrop) {
            this._enhanceCrop = {
                left: Math.floor((this._enhanceImgW - side) / 2),
                top: Math.floor((this._enhanceImgH - side) / 2),
            };
        }
        const { left, top } = this._enhanceCrop;
        afterImg?.removeAttribute('src');
        if (previewEl) previewEl.hidden = false;
        if (spinner) spinner.hidden = false;
        if (confirmBtn) confirmBtn.hidden = false;
        // Position after un-hiding so the viewport has a measurable width.
        this._positionCropView(left, top);

        // Guard against a slow preview being overtaken by a newer selection.
        const token = (this._enhancePreviewToken || 0) + 1;
        this._enhancePreviewToken = token;
        try {
            const resp = await App.apiPost('/enhance/preview', {
                image_id: imageId, recipe, crop_left: left, crop_top: top,
            });
            if (token !== this._enhancePreviewToken) return; // superseded
            const after = resp?.data?.after || '';
            if (afterImg) afterImg.src = after;
        } catch (err) {
            if (token !== this._enhancePreviewToken) return;
            console.error('[Enhance] preview failed', err);
            // Most often this image just isn't a good fit for the chosen model
            // (the backend rejects unstable output rather than show garbage).
            App.showError('This image couldn’t be enhanced that way.');
            if (previewEl) previewEl.hidden = true;
            if (confirmBtn) confirmBtn.hidden = true;
        } finally {
            if (token === this._enhancePreviewToken && spinner) spinner.hidden = true;
        }
    },

    /**
     * Submit the full-resolution enhancement and close the dialog. The backend
     * returns immediately; completion arrives via the enhance_complete event
     * (a toast), and the new version via delta sync.
     * @param {string} recipe - Capability key (e.g. 'denoise').
     * @private
     */
    async _submitEnhance(recipe) {
        const imageId = this._enhanceImageId;
        if (!imageId || !recipe) return;
        // Strength only applies to capabilities that offer the slider.
        const cap = (this._enhanceCaps || []).find((c) => c.key === recipe);
        const strengthInput = App.$('enhance-strength');
        const strength = (cap?.strength && strengthInput)
            ? Number(strengthInput.value) / 100
            : 1.0;
        App.$('dialog-enhance')?.close();
        try {
            await App.apiPost('/enhance', { image_id: imageId, recipe, strength });
            App.showInfo('Enhancing… you’ll be notified when it’s ready.');
        } catch (err) {
            console.error('[Enhance] submit failed', err);
            App.showError('Could not start enhancement.');
        }
    },

    /**
     * Size and translate both panes' base original layers so the square viewport
     * shows the crop at (left, top). The original is scaled by viewport/side and
     * shifted by the crop's top-left — so panning is a pure transform with no
     * server round-trip, and the enhanced overlay (which fills the viewport)
     * stays aligned over the same region.
     * @param {number} left - Crop left edge in source pixels.
     * @param {number} top - Crop top edge in source pixels.
     * @private
     */
    _positionCropView(left, top) {
        const view = App.$('enhance-before-view');
        const side = this._enhanceCropSide;
        if (!view || !side) return;
        const factor = (view.clientWidth || 1) / side;
        const w = `${this._enhanceImgW * factor}px`;
        const h = `${this._enhanceImgH * factor}px`;
        const transform = `translate(${-left * factor}px, ${-top * factor}px)`;
        for (const el of [App.$('enhance-preview-before'), App.$('enhance-preview-after-base')]) {
            if (!el) continue;
            el.style.width = w;
            el.style.height = h;
            el.style.transform = transform;
        }
    },

    /**
     * Wire pointer-drag panning on the preview viewports (bound once). Dragging
     * either pane moves the crop over the original live; on release, if the crop
     * moved, the enhanced preview regenerates for the new region.
     * @private
     */
    _setupEnhanceCropDrag() {
        if (this._enhanceDragBound) return;
        this._enhanceDragBound = true;
        const views = ['enhance-before-view', 'enhance-after-view']
            .map((id) => App.$(id)).filter(Boolean);

        let dragging = false;
        let startX = 0; let startY = 0; let startLeft = 0; let startTop = 0; let factor = 1;

        const onMove = (e) => {
            if (!dragging) return;
            const side = this._enhanceCropSide;
            // Dragging right reveals content to the left, so the crop moves the
            // opposite way to the pointer.  Clamp inside the image.
            const left = Math.max(0, Math.min(this._enhanceImgW - side, startLeft - (e.clientX - startX) / factor));
            const top = Math.max(0, Math.min(this._enhanceImgH - side, startTop - (e.clientY - startY) / factor));
            this._enhancePendingCrop = { left: Math.round(left), top: Math.round(top) };
            this._positionCropView(left, top);
        };

        // Tear down the live drag (drop the window listeners, clear the drag
        // flag and visual state).  Shared by the normal release path and the
        // dialog-close cancel path below.
        const stopDrag = () => {
            dragging = false;
            views.forEach((v) => v.classList.remove('dragging'));
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
        };

        const onUp = () => {
            if (!dragging) return;
            stopDrag();
            const next = this._enhancePendingCrop;
            const cur = this._enhanceCrop;
            if (next && (!cur || next.left !== cur.left || next.top !== cur.top)) {
                this._enhanceCrop = next;
                if (this._enhanceRecipe) this._previewEnhance(this._enhanceRecipe);
            }
        };

        // Abort an in-progress drag without regenerating the preview.  Called
        // when the dialog closes mid-drag so a pointerup landing after the close
        // can't fire a stray preview request against a now-hidden dialog.
        this._cancelEnhanceDrag = () => {
            if (!dragging) return;
            stopDrag();
            this._enhancePendingCrop = null;
        };

        for (const view of views) {
            view.addEventListener('pointerdown', (e) => {
                // Nothing to pan until a capability is chosen, and no room if the
                // image isn't larger than the crop in either axis.
                const side = this._enhanceCropSide;
                if (!this._enhanceRecipe) return;
                if (this._enhanceImgW <= side && this._enhanceImgH <= side) return;
                dragging = true;
                factor = (view.clientWidth || 1) / side;
                startX = e.clientX;
                startY = e.clientY;
                startLeft = this._enhanceCrop?.left ?? Math.floor((this._enhanceImgW - side) / 2);
                startTop = this._enhanceCrop?.top ?? Math.floor((this._enhanceImgH - side) / 2);
                this._enhancePendingCrop = { left: startLeft, top: startTop };
                views.forEach((v) => v.classList.add('dragging'));
                window.addEventListener('pointermove', onMove);
                window.addEventListener('pointerup', onUp);
                e.preventDefault();
            });
        }
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
     * Zooms centred on cursor position.
     * Supports both scroll wheel and trackpad pinch gestures.
     * @param {WheelEvent} e
     * @private
     */
    _handleWheel(e) {
        e.preventDefault();

        // No zoom for videos
        if (this._els.video && !this._els.video.hidden) return;

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
     * Zooms in or out centred on a specific point.
     * @param {number} factor - Zoom multiplier (>1 zooms in, <1 zooms out)
     * @param {number} clientX - X coordinate of zoom centre
     * @param {number} clientY - Y coordinate of zoom centre
     * @private
     */
    _zoomAtPoint(factor, clientX, clientY) {
        const oldZoom = this.state.zoom;
        let newZoom = oldZoom * factor;

        // Clamp to limits
        newZoom = Math.max(this.MIN_ZOOM, Math.min(this.MAX_ZOOM, newZoom));

        if (newZoom === oldZoom) return;

        // Get container bounds (cached to avoid reflow)
        const rect = this._getContainerRect();
        const centreX = rect.width / 2;
        const centreY = rect.height / 2;

        // Calculate point relative to image centre
        const pointX = clientX - rect.left - centreX;
        const pointY = clientY - rect.top - centreY;

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
            // Zoom to 100% (or 2x if image is smaller than screen) centred on click
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

        const rect = this._getContainerRect();
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
        // (allow default behaviour so inputs can receive focus, buttons can be clicked)
        if (e.target.closest('.face-box, .face-label, .face-input, .face-delete-btn')) return;

        e.preventDefault();
        this.state.isPanning = true;
        this._panStart = {
            x: e.clientX - this.state.panX,
            y: e.clientY - this.state.panY,
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
        // During slideshow, ignore small mouse movements (trackpad jitter,
        // bumped mouse) so the slideshow isn't paused by idle noise.  Only
        // movements exceeding MOUSE_DEADZONE_PX from the last significant
        // position count as intentional.
        if (this._slideshowActive && !this.state.isPanning) {
            if (!this._lastMousePos) {
                // First movement after slideshow start — establish baseline
                this._lastMousePos = { x: e.clientX, y: e.clientY };
                return;
            }
            const dx = e.clientX - this._lastMousePos.x;
            const dy = e.clientY - this._lastMousePos.y;
            if (dx * dx + dy * dy < this.MOUSE_DEADZONE_PX * this.MOUSE_DEADZONE_PX) {
                return;
            }
            this._lastMousePos = { x: e.clientX, y: e.clientY };
        }

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

        const rect = this._getContainerRect();
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
        // Only track single-finger touches
        if (e.touches.length !== 1) {
            this._touchStart = null;
            return;
        }

        // When zoomed in, start touch-pan (mirrors mouse drag-to-pan)
        if (this.state.zoom > 1) {
            this.state.isPanning = true;
            this._panStart = {
                x: e.touches[0].clientX - this.state.panX,
                y: e.touches[0].clientY - this.state.panY,
            };
            return;
        }

        this._touchStart = {
            x: e.touches[0].clientX,
            y: e.touches[0].clientY,
            time: Date.now(),
        };
        this._isSwiping = false;
    },

    /**
     * Handles touch move - translates image to follow finger.
     * @param {TouchEvent} e
     * @private
     */
    _handleTouchMove(e) {
        // Touch-pan while zoomed in (mirrors mouse drag-to-pan)
        if (this.state.isPanning && this._panStart && e.touches.length === 1) {
            e.preventDefault();
            this.state.panX = e.touches[0].clientX - this._panStart.x;
            this.state.panY = e.touches[0].clientY - this._panStart.y;
            this._constrainPan();
            this._applyTransform();
            return;
        }

        // Only process single-finger swipes when not zoomed
        if (!this._touchStart || e.touches.length !== 1) return;

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
            } else if (dy < -10) {
                // Upward vertical swipe
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
        // End touch-pan when zoomed (mirrors mouse up)
        if (this.state.isPanning) {
            this.state.isPanning = false;
            this._panStart = null;
            return;
        }

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

        // Check for Ctrl/Cmd modifier shortcuts
        const ctrlOrCmd = e.ctrlKey || e.metaKey;

        if (ctrlOrCmd) {
            switch (e.key.toLowerCase()) {
                case 'f':
                    // Ctrl+F: Toggle face tagging mode
                    e.preventDefault();
                    this._toggleFaceTagging();
                    return;
                case 'i':
                    // Ctrl+I: Ignore all unknown faces in this image
                    e.preventDefault();
                    this._ignoreUnknownFaces();
                    return;
                case 'r':
                    // Ctrl+R: Rotate image right (90° clockwise)
                    e.preventDefault();
                    this._rotateImage(90);
                    return;
                case 'l':
                    // Ctrl+L: Rotate image left (270°)
                    e.preventDefault();
                    this._rotateImage(270);
                    return;
                case 'backspace':
                case 'delete':
                    // Ctrl+Backspace/Delete: Delete image and advance
                    e.preventDefault();
                    this._deleteAndAdvance();
                    return;
            }
        }

        switch (e.key) {
            case 'Escape':
                e.preventDefault();
                // During slideshow, Escape stops slideshow AND exits fullscreen
                this._exit();
                break;
            case ' ':
                // Space: toggle slideshow pause/resume (or start linear)
                e.preventDefault();
                if (this._slideshowActive) {
                    this.stopSlideshow();
                } else {
                    this.startSlideshow(false);
                }
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

        // If a cross-fade is in progress, cancel it so we navigate immediately
        // and re-show overlays (the earlier _showOverlays call was suppressed)
        if (this._crossFadeTimer) {
            this._cancelCrossFade();
            this._showOverlays();
        }

        const newIndex = (currentIndex - 1 + imageList.length) % imageList.length;
        this._navigateToIndex(newIndex);

        // Sync slideshow position to match manual navigation
        if (this._slideshowActive) {
            this._syncSlideshowPosition(newIndex);
        }
    },

    /**
     * Navigates to the next image.
     * Wraps to the first image if at the end.
     * @private
     */
    _navigateNext() {
        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        if (this._crossFadeTimer) {
            this._cancelCrossFade();
            this._showOverlays();
        }

        const newIndex = (currentIndex + 1) % imageList.length;
        this._navigateToIndex(newIndex);

        // Sync slideshow position to match manual navigation
        if (this._slideshowActive) {
            this._syncSlideshowPosition(newIndex);
        }
    },

    /**
     * Navigates to a specific image by index.
     * @param {number} index - Index in the image list
     * @private
     */
    _navigateToIndex(index) {
        const { imageList } = this.state;
        if (index < 0 || index >= imageList.length) return;

        // Navigating between images closes the rating popup.
        this._dismissRatingPopup();

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
    },

    /* ----------------------------------------------------------------------
       KEYBOARD SHORTCUT ACTIONS

       Actions triggered by Ctrl/Cmd keyboard shortcuts.
       ---------------------------------------------------------------------- */

    /**
     * Toggles face tagging mode.
     * Ctrl+F shortcut.
     * @private
     */
    _toggleFaceTagging() {
        if (typeof Faces !== 'undefined' && Faces.toggleTaggingMode) {
            Faces.toggleTaggingMode();
            this._updateTaggingButton();
        }
    },

    /**
     * Ignores all unknown faces in the current image.
     * Identifies them as '-' (the ignored person).
     * Ctrl+I shortcut.
     * @private
     */
    async _ignoreUnknownFaces() {
        const imageId = this.state.currentId;
        if (!imageId) return;

        // Get faces for this image from cache
        let faces = AppState.faces.getForImage(imageId);

        if (!faces.length) {
            // Cache might be empty/partial - fetch fresh (fetchForImage
            // automatically populates the cache with the response)
            try {
                const fetched = await AppState.faces.fetchForImage(imageId, { fresh: true });
                if (fetched?.length) {
                    faces = fetched;
                }
            } catch (err) {
                console.error('[Fullscreen._ignoreUnknownFaces] Failed to fetch faces:', err);
                return;
            }
        }

        if (!faces?.length) return;

        // Filter to unknown faces only (no person_id, not suppressed)
        const unknownFaceIds = faces
            .filter(f => !f.person_id && !f.suppressed)
            .map(f => f.id);

        if (!unknownFaceIds.length) return;

        console.log('[Fullscreen._ignoreUnknownFaces]', unknownFaceIds.length, 'faces');

        try {
            // Identify all unknown faces as '-' (ignored)
            await AppState.faces.identify(unknownFaceIds, '-');
        } catch (error) {
            console.error('Failed to ignore faces:', error);
            App.showError('Failed to ignore faces');
        }
    },

    /**
     * Rotates the current image.
     * Backend rotates both the image file and face bounding boxes atomically.
     * Ctrl+R (90° right) and Ctrl+L (270° left) shortcuts.
     * @param {number} degrees - 90 for right, 270 for left
     * @private
     */
    async _rotateImage(degrees) {
        const imageId = this.state.currentId;
        if (!imageId) return;

        // RAW files cannot be rotated — show error and bail
        const img = this._getCurrentImage();
        if (img && App.isRawFile(img.basename)) {
            App.showError('RAW files cannot be rotated.');
            return;
        }

        console.log('[Fullscreen._rotateImage]', imageId, degrees + '°');

        try {
            // Rotate via AppState (handles dimension swap, cache bust, API call)
            // Backend rotates both the image and face bounding boxes atomically
            await AppState.images.rotate(imageId, degrees);

            // Bust cache and reload the full image (it's been rotated on disk)
            // Note: Gallery thumbnail update happens via images_modified event from backend
            ThumbnailLoader.bustCache(imageId);
            this._els.image.src = ThumbnailLoader.getFullImageUrl(imageId);

            // Update filename display with new dimensions
            const img = this._getCurrentImage();
            if (img) {
                this._showFilename(img.basename, img.width, img.height);
            }

            // Update face bounding boxes in cache and reload overlay
            // Backend has already rotated the bboxes in the database
            if (typeof Faces !== 'undefined' && Faces.isTaggingModeActive?.()) {
                // Update cache with rotated bboxes
                AppState.faces.rotateBoundingBoxes(imageId, degrees);
                // Re-render overlay from updated cache
                const faces = AppState.faces.getForImage(imageId);
                if (faces.length) {
                    // Wait for image to load before rendering overlay
                    // (overlay needs correct image dimensions).
                    // Staleness check: user may have navigated away while
                    // the rotated image was loading.
                    this._els.image.addEventListener('load', () => {
                        if (this.state.currentId !== imageId) return;
                        Faces.renderFaceOverlay(faces, imageId);
                    }, { once: true });
                }
            }
        } catch (error) {
            console.error('Failed to rotate image:', error);
            App.showError('Failed to rotate image');
        }
    },

    /**
     * Deletes the current image and advances to the next one.
     * If this is the last image, closes fullscreen.
     * Ctrl+Backspace/Delete shortcut.
     * @private
     */
    async _deleteAndAdvance() {
        const imageId = this.state.currentId;
        const { imageList, currentIndex } = this.state;
        if (!imageId || imageList.length === 0) return;

        // Guard: trash must be enabled
        if (!AppState.status.isTrashEnabled()) {
            App.showError(
                'Cannot delete: trash directory is misconfigured. '
                + 'Check that it does not overlap an indexed folder.',
            );
            return;
        }

        console.log('[Fullscreen._deleteAndAdvance]', imageId);

        // Store the next image info before deletion
        const wasLastImage = imageList.length === 1;
        const nextIndex = currentIndex < imageList.length - 1
            ? currentIndex
            : currentIndex - 1;

        // Remove from local list first for immediate UI update
        this.state.imageList = imageList.filter(img => img.id !== imageId);

        if (wasLastImage) {
            // No more images, close fullscreen
            this.close();
        } else {
            // Navigate to next image (or previous if we were at the end)
            const newIndex = Math.max(0, Math.min(nextIndex, this.state.imageList.length - 1));
            const nextImage = this.state.imageList[newIndex];
            if (nextImage) {
                this._resetTransform();
                this._loadImage(nextImage.id, newIndex);
            }
        }

        try {
            // Move to trash via AppState (handles cache update, faces cleanup, API call)
            await AppState.images.delete(imageId);
        } catch (error) {
            console.error('Failed to move image to trash:', error);
            App.showError('Failed to move image to trash');
            // Restore by reloading the display list
            this.state.imageList = AppState.images.getDisplayList();
        }
    },

    /* ----------------------------------------------------------------------
       IMAGE LIST SYNC

       Reacts to AppState.images changes (including deletions by other
       clients) to keep the navigation list in sync with reality.
       ---------------------------------------------------------------------- */

    /**
     * Handles AppState.images changes while fullscreen is open.
     *
     * Prunes images that no longer exist in AppState (trashed locally or
     * by another client).  If the currently-displayed image was removed,
     * closes fullscreen immediately.  If a slideshow is active and the
     * list shrank, resyncs slideshow state so indices stay valid.
     * @private
     */
    _onImagesChanged() {
        if (!this.state.isOpen) return;

        const currentId = this.state.currentId;

        // If the current image was removed from the database, exit
        if (currentId && !AppState.images.getById(currentId)) {
            this.close();
            return;
        }

        // Keep the rating widget in sync with edits from elsewhere (e.g. the
        // Gallery sidebar) while fullscreen is open.
        this._updateRatingWidget();

        // Prune any removed images from the navigation list
        const prevLength = this.state.imageList.length;
        this.state.imageList = this.state.imageList.filter(
            img => AppState.images.getById(img.id),
        );

        // Nothing was pruned — no work to do
        if (this.state.imageList.length === prevLength) return;

        // List is now empty — close
        if (this.state.imageList.length === 0) {
            this.close();
            return;
        }

        // Re-locate current image in the pruned list
        this.state.currentIndex = Math.max(0,
            this.state.imageList.findIndex(img => img.id === currentId),
        );

        // Resync slideshow state if active (indices may have shifted)
        if (this._slideshowActive) {
            this._resyncSlideshow();
        }
    },

    /**
     * Rebuilds slideshow position and order after the imageList was pruned.
     *
     * For linear mode, resets position to currentIndex.  For shuffle mode,
     * generates a fresh shuffle order with the current image at position 0
     * so it isn't revisited immediately.  Cancels any in-progress cross-fade
     * (its target index may now be invalid) and reschedules the advance.
     * @private
     */
    _resyncSlideshow() {
        const { imageList, currentIndex } = this.state;

        // Can't run a slideshow with 0 or 1 images
        if (imageList.length <= 1) {
            this.stopSlideshow();
            return;
        }

        // Cancel any in-progress cross-fade — its target index may be stale
        this._cancelCrossFade();

        if (this._slideshowShuffled) {
            // Rebuild shuffle order for the pruned list
            const order = Array.from({ length: imageList.length }, (_, i) => i);
            for (let i = order.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [order[i], order[j]] = [order[j], order[i]];
            }
            // Place current image at position 0 so it isn't revisited next
            const curPos = order.indexOf(currentIndex);
            if (curPos > 0) {
                [order[0], order[curPos]] = [order[curPos], order[0]];
            }
            this._slideshowOrder = order;
            this._slideshowPosition = 0;
        } else {
            this._slideshowPosition = currentIndex;
        }

        // Reschedule advance (also re-preloads the correct next image)
        this._scheduleSlideshowAdvance();
    },

    /* ----------------------------------------------------------------------
       SLIDESHOW

       Auto-advancing image display with optional shuffle.
       ---------------------------------------------------------------------- */

    /**
     * Starts (or switches) the slideshow.
     * If face tagging mode is active, it is disabled first.
     * @param {boolean} shuffle - True for shuffled order, false for linear
     */
    startSlideshow(shuffle = false) {
        // Disable face tagging during slideshow
        if (typeof Faces !== 'undefined' && Faces.isTaggingModeActive?.()) {
            Faces.setTaggingMode(false);
            this._updateTaggingButton();
        }

        // If already running, stop first (handles mode switch)
        if (this._slideshowActive) {
            this.stopSlideshow();
        }

        const { imageList, currentIndex } = this.state;
        if (imageList.length <= 1) return;

        this._slideshowActive = true;
        this._slideshowShuffled = shuffle;
        this._lastMousePos = null; // Reset deadzone baseline

        if (shuffle) {
            // Fisher-Yates shuffle of indices [0..length-1]
            const order = Array.from({ length: imageList.length }, (_, i) => i);
            for (let i = order.length - 1; i > 0; i--) {
                const j = Math.floor(Math.random() * (i + 1));
                [order[i], order[j]] = [order[j], order[i]];
            }
            this._slideshowOrder = order;
            // Find current image's position in the shuffled order
            this._slideshowPosition = order.indexOf(currentIndex);
            if (this._slideshowPosition < 0) this._slideshowPosition = 0;
        } else {
            this._slideshowOrder = null;
            this._slideshowPosition = currentIndex;
        }

        // Highlight the active button and ensure the toolbar is visible for
        // a full 3 seconds so the user can see the highlighted state.  Without
        // this, the overlays may disappear almost immediately if the mouse last
        // moved a couple of seconds ago — making it look like nothing happened.
        this._els.slideshowBtn.classList.toggle('slideshow-active', !shuffle);
        this._els.shuffleBtn.classList.toggle('slideshow-active', shuffle);
        this._showOverlays();

        // Schedule the first advance
        this._scheduleSlideshowAdvance();
    },

    /**
     * Stops the slideshow and resets state.
     */
    stopSlideshow() {
        if (this._slideshowTimer) {
            clearTimeout(this._slideshowTimer);
            this._slideshowTimer = null;
        }
        this._cancelCrossFade();

        this._slideshowActive = false;
        this._slideshowShuffled = false;
        this._slideshowOrder = null;
        this._slideshowPosition = -1;
        this._slideshowPreload = null;
        this._lastMousePos = null;

        // Remove active highlight from both buttons
        this._els.slideshowBtn?.classList.remove('slideshow-active');
        this._els.shuffleBtn?.classList.remove('slideshow-active');

        // Show overlays so user sees the toolbar again
        this._showOverlays();
    },

    /**
     * Returns the imageList index the slideshow will advance to next,
     * without mutating position state.  Used for preloading.
     * @returns {number} Index into imageList
     * @private
     */
    _getNextSlideshowTarget() {
        const nextPos = (this._slideshowPosition + 1) % this.state.imageList.length;
        return this._slideshowShuffled
            ? this._slideshowOrder[nextPos]
            : nextPos;
    },

    /**
     * Schedules the next slideshow advance after the configured interval.
     * Also preloads the next image so it's browser-cached before the
     * cross-fade fires — eliminates flash-of-black on slow loads.
     * Clears any existing timer first so re-calling this resets the countdown.
     * @private
     */
    _scheduleSlideshowAdvance() {
        if (this._slideshowTimer) {
            clearTimeout(this._slideshowTimer);
        }
        const ms = (App.getSlideshowInterval() || 5) * 1000;

        // Preload the actual next slideshow image during the hold period.
        // In shuffle mode this may differ from the index-adjacent images
        // that _preloadAdjacent() handles for manual arrow-key navigation.
        const { imageList } = this.state;
        if (imageList.length > 1) {
            const targetIndex = this._getNextSlideshowTarget();
            const nextImage = imageList[targetIndex];
            if (nextImage && nextImage.media_type !== 'video') {
                const img = new Image();
                img.src = ThumbnailLoader.getFullImageUrl(nextImage.id);
                this._slideshowPreload = { id: nextImage.id, img };
            }
        }

        this._slideshowTimer = setTimeout(() => this._slideshowAdvance(), ms);
    },

    /**
     * Advances to the next image with a cross-fade transition.
     *
     * Waits for the preloaded image to be browser-cached, then fades out
     * the current image, navigates while invisible (instant from cache),
     * and fades in.  The next advance is scheduled after the fade-in
     * completes so the full image is visible for the entire configured
     * interval.
     * @private
     */
    _slideshowAdvance() {
        const { imageList } = this.state;
        if (!this._slideshowActive || imageList.length <= 1) return;

        // Move to next position (wrap with modulo)
        this._slideshowPosition = (this._slideshowPosition + 1) % imageList.length;

        // Resolve the actual image index
        const targetIndex = this._slideshowShuffled
            ? this._slideshowOrder[this._slideshowPosition]
            : this._slideshowPosition;

        const targetImage = imageList[targetIndex];
        if (!targetImage) return;

        const targetMeta = AppState.images.getById(targetImage.id) || targetImage;
        const isVideo = targetMeta.media_type === 'video';

        if (isVideo) {
            // Video: navigate directly, play for video duration then advance
            this._slideshowAdvancing = true;
            this._navigateToIndex(targetIndex);

            // Wait for video to start playing, then schedule advance after it ends
            const video = this._els.video;
            const maxDuration = 30; // Cap slideshow video playback at 30s
            const onVideoReady = () => {
                if (!this._slideshowActive) return;
                const playDuration = Math.min(video.duration || maxDuration, maxDuration);
                this._slideshowAdvancing = false;
                this._crossFadeTimer = setTimeout(() => {
                    if (this._slideshowActive) {
                        video.pause();
                        this._slideshowAdvance();
                    }
                }, playDuration * 1000);
            };

            if (video && !video.hidden) {
                video.addEventListener('canplay', () => {
                    if (this._slideshowActive) {
                        video.play().catch(() => {});
                    }
                    onVideoReady();
                }, { once: true });
                // Fallback timeout in case canplay never fires
                setTimeout(() => {
                    if (this._slideshowActive && this._slideshowAdvancing) {
                        this._slideshowAdvancing = false;
                        this._scheduleSlideshowAdvance();
                    }
                }, 5000);
            } else {
                // Video element not ready — just advance
                this._scheduleSlideshowAdvance();
            }
            return;
        }

        // Use the image preloaded during the hold period if it matches,
        // otherwise create a fresh preload (e.g. after manual navigation
        // changed the slideshow position).
        let preloaded;
        if (this._slideshowPreload?.id === targetImage.id) {
            preloaded = this._slideshowPreload.img;
        } else {
            preloaded = new Image();
            preloaded.src = ThumbnailLoader.getFullImageUrl(targetImage.id);
        }
        this._slideshowPreload = null;

        // Begin the cross-fade once the next image is cached.
        const doCrossFade = () => {
            if (!this._slideshowActive) return;

            // Suppress overlay display for the duration of the cross-fade
            this._slideshowAdvancing = true;
            const img = this._els.image;
            const ms = this.CROSSFADE_MS;

            // Phase 1: fade out current image
            img.style.transition = `opacity ${ms}ms ease`;
            img.style.opacity = '0';

            this._crossFadeTimer = setTimeout(() => {
                // Bail if slideshow was stopped during fade-out
                if (!this._slideshowActive) {
                    this._clearCrossFadeStyles();
                    return;
                }

                // Phase 2: navigate while invisible (image is cached, loads instantly)
                this._navigateToIndex(targetIndex);

                // Phase 3: fade in — wait one frame for the new src to take effect
                requestAnimationFrame(() => {
                    img.style.opacity = '1';

                    this._crossFadeTimer = setTimeout(() => {
                        this._crossFadeTimer = null;
                        this._clearCrossFadeStyles();

                        if (this._slideshowActive) {
                            this._scheduleSlideshowAdvance();
                        }
                    }, ms);
                });
            }, ms);
        };

        // If the image is already cached (common — preloaded during the
        // full hold duration), start the cross-fade immediately.
        if (preloaded.complete && preloaded.naturalWidth > 0) {
            doCrossFade();
        } else {
            // Still loading — wait for it, then cross-fade.
            preloaded.onload = doCrossFade;
            preloaded.onerror = doCrossFade; // degrade gracefully
        }
    },

    /**
     * Removes inline cross-fade styles from the image and clears the
     * advancing flag so normal overlay behaviour resumes.
     * @private
     */
    _clearCrossFadeStyles() {
        this._els.image.style.transition = '';
        this._els.image.style.opacity = '';
        this._slideshowAdvancing = false;
    },

    /**
     * Cancels an in-progress cross-fade transition, restoring the image to
     * full opacity immediately.  Safe to call when no fade is in progress.
     * @private
     */
    _cancelCrossFade() {
        if (!this._crossFadeTimer) return;
        clearTimeout(this._crossFadeTimer);
        this._crossFadeTimer = null;
        this._clearCrossFadeStyles();
    },

    /**
     * Syncs slideshow position to match a manually navigated image index.
     * Called when user presses arrow keys or swipes during a slideshow.
     * @param {number} imageIndex - The image list index that was navigated to
     * @private
     */
    _syncSlideshowPosition(imageIndex) {
        if (this._slideshowShuffled && this._slideshowOrder) {
            // Find this image index in the shuffled order
            const pos = this._slideshowOrder.indexOf(imageIndex);
            if (pos >= 0) {
                this._slideshowPosition = pos;
            }
        } else {
            this._slideshowPosition = imageIndex;
        }
    },
};

// Initialise module when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => Fullscreen.init());
} else {
    Fullscreen.init();
}
