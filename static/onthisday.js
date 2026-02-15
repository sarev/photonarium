/**
 * On This Day... - Nostalgic photo album overlay
 * ================================================
 *
 * Shows a scattered-photo album overlay when the app starts, if there are
 * photos taken on today's month/day across multiple years.  The aesthetic is
 * intentionally hardcoded (cream paper, sepia tint, coffee rings, ring binder)
 * and does NOT follow the light/dark theme toggle.
 *
 * Not a registered screen module - standalone object like Settings.
 *
 * Trigger: screensaver pattern.  After 8+ hours of user inactivity (tracked
 * via mousemove/keydown/etc.), the feature is "armed".  On the user's first
 * interaction, today's date is checked against the image library.  If matching
 * photos span 2+ years with 4+ total images, the album overlay fades in.
 * Shows at most once per calendar day (localStorage gate).  Can be disabled
 * via the on_this_day_enabled config option.
 *
 * @fileoverview "On this day..." nostalgic photo album overlay.
 */

'use strict';

// eslint-disable-next-line no-unused-vars -- referenced by core.js via typeof check
const OnThisDay = {

    /** @type {HTMLElement|null} Overlay element reference while visible */
    _overlay: null,

    /** @type {string[]|null} Image IDs currently shown in the album */
    _imageIds: null,

    /** @type {string} Which side the binder is on ('left' or 'right') */
    _binderSide: 'left',

    /** @type {Function|null} Escape keydown handler (for cleanup) */
    _escHandler: null,

    /** @type {Function|null} Backdrop click handler (for cleanup) */
    _backdropHandler: null,

    /** @type {number} Last user activity timestamp (ms since epoch) */
    _lastActivity: 0,

    /** @type {boolean} Whether images are loaded in AppState */
    _imagesReady: false,

    /** @type {boolean} Whether a trigger is deferred waiting for images to load */
    _pendingTrigger: false,

    /** @type {number} Inactivity threshold: 8 hours in milliseconds */
    _INACTIVITY_MS: 8 * 60 * 60 * 1000,

    // =========================================================================
    // LIFECYCLE
    // =========================================================================

    /**
     * Initialise the On This Day feature.
     *
     * Sets up activity tracking on common user-interaction events.  The last
     * activity timestamp is persisted to localStorage so a page reload after
     * a long absence can detect the gap.  Also subscribes to AppState.images
     * so we know when the image catalogue is available for querying.
     */
    init() {
        // Read last activity time from localStorage (default to now for first
        // run, so OTD doesn't trigger on the very first app launch)
        const stored = parseInt(localStorage.getItem('otd_lastActivity'), 10);
        this._lastActivity = (stored > 0) ? stored : Date.now();

        // Listen for user activity to detect returning after long inactivity
        const handler = () => this._onActivity();
        for (const evt of ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll']) {
            document.addEventListener(evt, handler, { passive: true, capture: true });
        }

        // Periodically persist lastActivity to localStorage (every 60 s)
        // so an app reload knows roughly when the user was last active
        setInterval(() => {
            localStorage.setItem('otd_lastActivity', String(this._lastActivity));
        }, 60000);

        // Also save on page unload for better accuracy after browser close
        window.addEventListener('beforeunload', () => {
            localStorage.setItem('otd_lastActivity', String(this._lastActivity));
        });

        // Subscribe to images becoming available (needed before we can query)
        const unsub = AppState.images.onChanged(() => {
            if (AppState.images.getCount() > 0) {
                unsub();
                this._imagesReady = true;
                // If a trigger fired before images were ready, run it now
                if (this._pendingTrigger) {
                    this._pendingTrigger = false;
                    this._tryShow();
                }
            }
        });
    },

    /**
     * Handle a user activity event.  Updates the last-activity timestamp
     * and triggers OTD when the user returns after prolonged inactivity
     * (screensaver pattern: long idle period -> first interaction fires).
     * @private
     */
    _onActivity() {
        const now = Date.now();
        const gap = now - this._lastActivity;
        this._lastActivity = now;

        // Don't trigger while the overlay is already showing
        if (this._overlay) return;

        if (gap >= this._INACTIVITY_MS) {
            if (this._imagesReady) {
                this._tryShow();
            } else {
                this._pendingTrigger = true;
            }
        }
    },

    /**
     * Attempt to show the On This Day overlay.
     * Checks config, date gate, and photo availability before showing.
     * @private
     */
    _tryShow() {
        // Check config (loaded async by App.loadThumbnailConfig)
        if (!App.isOnThisDayEnabled()) return;

        // Date gate: show at most once per calendar day
        const today = new Date();
        const dateKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
        if (localStorage.getItem('onThisDay_lastShown') === dateKey) return;

        const result = this._query(today.getMonth() + 1, today.getDate());
        if (!result) return;

        const { byYear, images } = result;
        if (Object.keys(byYear).length < 2 || images.length < 4) return;

        localStorage.setItem('onThisDay_lastShown', dateKey);

        const monthDay = today.toLocaleDateString('en-US', { month: 'long', day: 'numeric' });
        const years = Object.keys(byYear).map(Number).sort((a, b) => a - b);
        const yearRange = `${years[0]} \u2013 ${years[years.length - 1]}`;

        this._show(monthDay, yearRange, images);
    },

    // =========================================================================
    // QUERY
    // =========================================================================

    /**
     * Query AppState for images matching a given month/day.
     *
     * Groups by year, keeps top 3 per year ranked by aesthetic quality
     * (LAION, NIMA, sharpness), then caps to 12 total via round-robin
     * so every year gets fair representation.
     *
     * @param {number} month - 1-based month
     * @param {number} day   - Day of month
     * @returns {{byYear: Object<number, Array>, images: Array}|null}
     * @private
     */
    _query(month, day) {
        const all = AppState.images.getAll();
        if (!all.length) return null;

        const maxPerYear = 3;
        const maxTotal = 12;

        // Group matching images by year
        /** @type {Object<number, Array>} */
        const byYear = {};
        for (const img of all) {
            if (!img.timestamp) continue;
            const d = new Date(img.timestamp);
            if (d.getMonth() + 1 !== month || d.getDate() !== day) continue;
            const year = d.getFullYear();
            if (!byYear[year]) byYear[year] = [];
            byYear[year].push(img);
        }

        // Sort each year's images by quality, keep top N
        for (const year of Object.keys(byYear)) {
            byYear[year].sort((a, b) => {
                const la = a.aesthetic_laion ?? 0, lb = b.aesthetic_laion ?? 0;
                if (la !== lb) return lb - la;
                const na = a.aesthetic_nima ?? 0, nb = b.aesthetic_nima ?? 0;
                if (na !== nb) return nb - na;
                return (b.laplacian_var ?? 0) - (a.laplacian_var ?? 0);
            });
            byYear[year] = byYear[year].slice(0, maxPerYear);
        }

        // Round-robin cap to maxTotal images so every year gets representation
        const total = Object.values(byYear).reduce((s, imgs) => s + imgs.length, 0);
        let capped;
        if (total <= maxTotal) {
            capped = byYear;
        } else {
            capped = {};
            for (const y of Object.keys(byYear)) capped[y] = [];
            let count = 0;
            const maxRounds = Math.max(...Object.values(byYear).map(a => a.length));
            outer:
            for (let round = 0; round < maxRounds; round++) {
                for (const year of Object.keys(byYear).sort()) {
                    if (round < byYear[year].length) {
                        capped[year].push(byYear[year][round]);
                        count++;
                        if (count >= maxTotal) break outer;
                    }
                }
            }
            // Remove empty years
            for (const y of Object.keys(capped)) {
                if (!capped[y].length) delete capped[y];
            }
        }

        // Collect all images in chronological order (year ascending)
        const images = [];
        for (const year of Object.keys(capped).sort()) {
            images.push(...capped[year]);
        }

        return images.length ? { byYear: capped, images } : null;
    },

    // =========================================================================
    // DISPLAY
    // =========================================================================

    /**
     * Build and display the album overlay.
     *
     * @param {string} dateStr   - Formatted date (e.g. "February 15")
     * @param {string} yearRange - Year range string (e.g. "2018 - 2024")
     * @param {Array}  images    - Image objects to display
     * @private
     */
    _show(dateStr, yearRange, images) {
        this._imageIds = images.map(img => img.id);

        const overlay = document.getElementById('on-this-day-overlay');
        const album = document.getElementById('on-this-day-album');
        const subtitle = document.getElementById('on-this-day-subtitle');
        const photosContainer = document.getElementById('on-this-day-photos');

        subtitle.textContent = `${dateStr} \u00B7 ${yearRange}`;

        // Clear previous content (coffee rings, binder, photos)
        photosContainer.innerHTML = '';
        album.querySelectorAll('.otd-binder, .otd-coffee-ring').forEach(el => el.remove());

        // Create photo elements
        images.forEach((img, i) => {
            const year = new Date(img.timestamp).getFullYear();
            const aspect = img.width / Math.max(1, img.height);

            // Thumbnail dimensions (400px longest edge, matching API size)
            let thumbW, thumbH;
            if (img.width >= img.height) {
                thumbW = 400;
                thumbH = Math.round(400 / aspect);
            } else {
                thumbH = 400;
                thumbW = Math.round(400 * aspect);
            }

            const photo = document.createElement('div');
            photo.className = 'otd-photo';
            photo.style.setProperty('--aspect', aspect.toFixed(3));
            photo.style.setProperty('--delay', `${(i * 0.12).toFixed(2)}s`);

            const frame = document.createElement('div');
            frame.className = 'otd-photo-frame';

            const imgEl = document.createElement('img');
            imgEl.src = `/api/images/${img.id}/thumbnail?size=400`;
            imgEl.alt = '';
            imgEl.width = thumbW;
            imgEl.height = thumbH;
            imgEl.draggable = false;

            const yearLabel = document.createElement('div');
            yearLabel.className = 'otd-photo-year';
            yearLabel.textContent = year;

            frame.appendChild(imgEl);
            photo.appendChild(frame);
            photo.appendChild(yearLabel);
            photosContainer.appendChild(photo);
        });

        // Build binder first - scatter needs to know which side to avoid
        this._buildBinder(album);

        // Scatter photos using Monte-Carlo placement (overlay is visibility:hidden
        // at this point but still laid out, so getBoundingClientRect works)
        const items = photosContainer.querySelectorAll('.otd-photo');
        this._scatter(album, items);

        // Apply slight random tilts for a casual album feel
        items.forEach(el => {
            const tilt = (Math.random() - 0.5) * 6;  // -3 to +3 degrees
            el.style.setProperty('--tilt', `${tilt.toFixed(2)}deg`);
            el.style.transform = `rotate(${tilt.toFixed(2)}deg)`;
        });

        // Add coffee ring stains
        this._buildCoffeeRings(album);

        // Reveal the overlay (triggers CSS opacity transition)
        overlay.classList.add('visible');
        this._overlay = overlay;

        // Bind event handlers
        document.getElementById('on-this-day-close').onclick = () => this._dismiss();
        document.getElementById('on-this-day-dismiss').onclick = () => this._dismiss();
        document.getElementById('on-this-day-gallery').onclick = () => this._viewInGallery();

        this._escHandler = (e) => {
            if (e.key === 'Escape') this._dismiss();
        };
        document.addEventListener('keydown', this._escHandler);

        this._backdropHandler = (e) => {
            if (e.target === overlay) this._dismiss();
        };
        overlay.addEventListener('click', this._backdropHandler);
    },

    // =========================================================================
    // SCATTER ALGORITHM
    // =========================================================================

    /**
     * Monte-Carlo scatter placement for photos.
     *
     * Each image's longest edge starts at 33% of the relevant page dimension.
     * When the viewport and photo orientations differ, the minority orientation
     * is boosted by 1.4x so portrait shots in a landscape viewport (and vice
     * versa) aren't disproportionately tiny.
     *
     * For each image, up to 1000 random positions are tried.  A candidate is
     * valid when its bbox (padded with a tilt margin) sits fully on the page
     * and doesn't overlap any fixed bbox or already-placed photo.
     *
     * If fewer than the minimum threshold are placed, the size percentage
     * decreases by 1pp and the pass retries from scratch.
     *
     * @param {HTMLElement} album - The album container element
     * @param {NodeList}    items - Photo elements to position
     * @private
     */
    _scatter(album, items) {
        const n = items.length;
        if (!n) return;

        const aR = album.getBoundingClientRect();
        const hR = album.querySelector('.otd-header').getBoundingClientRect();
        const fR = album.querySelector('.otd-footer').getBoundingClientRect();
        const pgW = aR.width;
        const pgH = aR.height;
        const vpLandscape = pgW > pgH;

        const tm = 10;          // tilt margin on each side of bbox
        const pad = 12;         // photo-frame padding (6px x 2)
        const yearH = 22;       // year label + gap beneath photo
        const minPlaced = Math.min(5, n);

        // Fixed exclusion bboxes (coords relative to album top-left)
        const fixed = [
            { x: 0, y: 0, w: pgW, h: hR.bottom - aR.top + 6 },
            { x: 0, y: fR.top - aR.top - 6, w: pgW, h: pgH - (fR.top - aR.top) + 6 },
            this._binderSide === 'left'
                ? { x: 0, y: 0, w: 42, h: pgH }
                : { x: pgW - 42, y: 0, w: 42, h: pgH },
        ];

        // Pre-read aspect ratios
        const aspects = [];
        for (let i = 0; i < n; i++) {
            aspects.push(parseFloat(items[i].style.getPropertyValue('--aspect')) || 1);
        }

        /** AABB overlap test */
        function overlaps(ax, ay, aw, ah, bx, by, bw, bh) {
            return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
        }

        /**
         * Compute image pixel dimensions for a given size percentage.
         * Minority orientation gets a 1.4x boost for visual balance.
         */
        function imgSize(aspect, pct) {
            const landscape = aspect >= 1;
            let imgW, imgH;
            if (vpLandscape) {
                if (landscape) {
                    imgW = pgW * pct / 100;
                    imgH = imgW / aspect;
                } else {
                    imgH = pgH * 1.4 * pct / 100;
                    imgW = imgH * aspect;
                }
            } else {
                if (landscape) {
                    imgW = pgW * 1.4 * pct / 100;
                    imgH = imgW / aspect;
                } else {
                    imgH = pgH * pct / 100;
                    imgW = imgH * aspect;
                }
            }
            return { w: imgW, h: imgH };
        }

        // Outer loop: decrease pct until enough images fit
        let bestPositions = null;
        let bestPlacedIdx = null;

        for (let pct = 33; pct >= 8; pct -= 1) {
            const bboxes = fixed.slice();
            const positions = [];
            const placedIdx = [];

            for (let i = 0; i < n; i++) {
                const sz = imgSize(aspects[i], pct);
                const bw = sz.w + pad + tm * 2;
                const bh = sz.h + pad + yearH + tm * 2;

                // Skip if image bbox can't possibly fit on the page
                if (bw > pgW || bh > pgH) continue;

                for (let t = 0; t < 1000; t++) {
                    const tx = Math.random() * (pgW - bw);
                    const ty = Math.random() * (pgH - bh);

                    let hit = false;
                    for (let j = 0; j < bboxes.length; j++) {
                        const b = bboxes[j];
                        if (overlaps(tx, ty, bw, bh, b.x, b.y, b.w, b.h)) {
                            hit = true;
                            break;
                        }
                    }
                    if (!hit) {
                        bboxes.push({ x: tx, y: ty, w: bw, h: bh });
                        positions.push({ x: tx + tm, y: ty + tm, imgH: sz.h });
                        placedIdx.push(i);
                        break;
                    }
                }
            }

            // Keep the best result seen so far
            if (!bestPositions || placedIdx.length > bestPlacedIdx.length) {
                bestPositions = positions;
                bestPlacedIdx = placedIdx;
            }

            // Success: placed at least our minimum threshold
            if (placedIdx.length >= minPlaced) break;
        }

        // Apply positions to placed photos; hide any that didn't fit
        const placedSet = new Set(bestPlacedIdx);
        for (let k = 0; k < bestPlacedIdx.length; k++) {
            const idx = bestPlacedIdx[k];
            items[idx].style.left = `${bestPositions[k].x}px`;
            items[idx].style.top = `${bestPositions[k].y}px`;
            items[idx].style.setProperty('--photo-h', `${Math.round(bestPositions[k].imgH)}px`);
        }
        for (let i = 0; i < n; i++) {
            if (!placedSet.has(i)) items[i].style.display = 'none';
        }
    },

    // =========================================================================
    // DECORATIONS
    // =========================================================================

    /**
     * Build an SVG wire ring binder along a random edge of the album.
     * Draws a vertical spine with evenly-spaced wire loops that appear to
     * thread through punched holes in the paper.
     *
     * @param {HTMLElement} album - The album container element
     * @private
     */
    _buildBinder(album) {
        // Remove any previous binder
        album.querySelectorAll('.otd-binder').forEach(el => el.remove());

        this._binderSide = Math.random() < 0.5 ? 'left' : 'right';
        const side = this._binderSide;

        const binder = document.createElement('div');
        binder.className = 'otd-binder';
        binder.style[side] = '-16px';

        const albumRect = album.getBoundingClientRect();
        const svgW = 34;
        const ringSpacing = 44;
        const ringH = 12;          // half-height of each wire loop
        const margin = 30;         // top/bottom margin
        let ringCount = Math.floor((albumRect.height - margin * 2) / ringSpacing);
        if (ringCount < 3) ringCount = 3;
        const startY = (albumRect.height - (ringCount - 1) * ringSpacing) / 2;

        // Spine x-position and loop direction
        const spineX = side === 'left' ? svgW - 5 : 5;
        const loopDir = side === 'left' ? -1 : 1;
        const loopExtent = 22;     // how far the loop extends from spine

        let svg = '<svg xmlns="http://www.w3.org/2000/svg"'
            + ` width="${svgW}" height="${albumRect.height}"`
            + ` viewBox="0 0 ${svgW} ${albumRect.height}">`
            + '<defs>'
            // Metallic gradient for the wire
            + '<linearGradient id="otd-wire" x1="0%" y1="0%" x2="100%" y2="0%">'
            + '<stop offset="0%"   stop-color="#b0b0b0"/>'
            + '<stop offset="25%"  stop-color="#e0e0e0"/>'
            + '<stop offset="50%"  stop-color="#c8c8c8"/>'
            + '<stop offset="75%"  stop-color="#dcdcdc"/>'
            + '<stop offset="100%" stop-color="#a0a0a0"/>'
            + '</linearGradient>'
            // Subtle drop shadow for depth
            + '<filter id="otd-ws" x="-30%" y="-10%" width="160%" height="120%">'
            + `<feDropShadow dx="${loopDir * 1.5}" dy="1.5"`
            + ' stdDeviation="1" flood-color="rgba(0,0,0,0.22)"/>'
            + '</filter>'
            + '</defs>';

        // Spine line (thin vertical bar connecting all rings)
        svg += `<line x1="${spineX}" y1="${startY - ringH - 6}"`
            + ` x2="${spineX}" y2="${startY + (ringCount - 1) * ringSpacing + ringH + 6}"`
            + ' stroke="url(#otd-wire)" stroke-width="1.8"/>';

        // Wire loops + punch holes
        for (let b = 0; b < ringCount; b++) {
            const cy = startY + b * ringSpacing;
            const lx = spineX + loopDir * loopExtent;
            const cornerR = 5;

            // Punch hole (dark circle behind the wire)
            svg += `<circle cx="${spineX}" cy="${cy}" r="3"`
                + ' fill="#3a3028" opacity="0.18"/>';

            // Wire loop: squared D-shape with rounded outer corners
            let p;
            if (side === 'left') {
                p = `M ${spineX},${cy - ringH}`
                    + ` L ${lx + cornerR},${cy - ringH}`
                    + ` Q ${lx},${cy - ringH} ${lx},${cy - ringH + cornerR}`
                    + ` L ${lx},${cy + ringH - cornerR}`
                    + ` Q ${lx},${cy + ringH} ${lx + cornerR},${cy + ringH}`
                    + ` L ${spineX},${cy + ringH}`;
            } else {
                p = `M ${spineX},${cy - ringH}`
                    + ` L ${lx - cornerR},${cy - ringH}`
                    + ` Q ${lx},${cy - ringH} ${lx},${cy - ringH + cornerR}`
                    + ` L ${lx},${cy + ringH - cornerR}`
                    + ` Q ${lx},${cy + ringH} ${lx - cornerR},${cy + ringH}`
                    + ` L ${spineX},${cy + ringH}`;
            }
            svg += `<path d="${p}" stroke="url(#otd-wire)" stroke-width="2"`
                + ' fill="none" stroke-linecap="round" filter="url(#otd-ws)"/>';
        }

        svg += '</svg>';
        binder.innerHTML = svg;
        album.appendChild(binder);
    },

    /**
     * Add random coffee mug ring stains to the album page.
     * Each ring is a radial-gradient circle with variable opacity and
     * slight distortion via scaleX for realism.
     *
     * @param {HTMLElement} album - The album container element
     * @private
     */
    _buildCoffeeRings(album) {
        for (let r = 0; r < 1; r++) {
            const ring = document.createElement('div');
            ring.className = 'otd-coffee-ring';
            const size = 160 + Math.floor(Math.random() * 60);  // 160-220px (mug-sized)
            ring.style.width = `${size}px`;
            ring.style.height = `${size}px`;
            ring.style.left = `${(Math.random() * 100 - 5).toFixed(1)}%`;
            ring.style.top = `${(Math.random() * 100 - 5).toFixed(1)}%`;
            const alpha = (0.04 + Math.random() * 0.06).toFixed(3);
            ring.style.setProperty('--ring-alpha', alpha);
            const scaleX = (0.88 + Math.random() * 0.24).toFixed(2);
            const rotate = Math.floor(Math.random() * 360);
            ring.style.transform = `scaleX(${scaleX}) rotate(${rotate}deg)`;
            album.appendChild(ring);
        }
    },

    // =========================================================================
    // ACTIONS
    // =========================================================================

    /**
     * Dismiss the overlay and clean up event handlers.
     * @private
     */
    _dismiss() {
        if (!this._overlay) return;
        this._overlay.classList.remove('visible');
        if (this._escHandler) {
            document.removeEventListener('keydown', this._escHandler);
            this._escHandler = null;
        }
        if (this._backdropHandler) {
            this._overlay.removeEventListener('click', this._backdropHandler);
            this._backdropHandler = null;
        }
        this._overlay = null;
    },

    /**
     * Dismiss the overlay and navigate to the gallery with the album
     * images shown as a filtered set.
     * @private
     */
    _viewInGallery() {
        const ids = this._imageIds;
        this._dismiss();
        if (ids?.length) {
            AppState.filter.set({ imageIds: ids });
            App.navigateTo('gallery');
        }
    },
};
