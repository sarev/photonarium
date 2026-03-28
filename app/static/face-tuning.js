/**
 * Face detection tuning module for Photonarium.
 *
 * Provides a live preview overlay in the fullscreen viewer where users can
 * adjust face detection parameters (min confidence, min face size) and see
 * the results immediately on the current image.  Detection runs on CPU via
 * a dedicated preview endpoint, so it never contends with the pipeline's
 * GPU models.
 *
 * The confidence slider refilters cached results client-side (instant).
 * The face-size slider triggers a backend re-detection on release.
 *
 * "Apply" persists the chosen settings to the config file.
 * "Cancel" discards changes and restores the previous overlay state.
 *
 * @module FaceTuning
 */
(function () {
    'use strict';

    // =========================================================================
    // STATE
    // =========================================================================

    /** Whether tuning mode is currently active. */
    let _active = false;

    /** Image ID currently being tuned. */
    let _currentImageId = null;

    /** Cached detection results from the last backend call (all faces above
     *  the server-side confidence floor).  The confidence slider refilters
     *  this array client-side without a round-trip. */
    let _cachedDetections = [];

    /** Original config values, captured on enter for Cancel restoration. */
    let _originalConfidence = 0.95;
    let _originalFaceSize = 40;

    /** Whether a backend detection request is in flight. */
    let _detecting = false;

    // =========================================================================
    // DOM REFERENCES (populated in init)
    // =========================================================================

    let _overlay = null;
    let _panel = null;
    let _countEl = null;
    let _confidenceSlider = null;
    let _confidenceValue = null;
    let _faceSizeSlider = null;
    let _faceSizeValue = null;
    let _detectBtn = null;
    let _applyBtn = null;
    let _cancelBtn = null;
    let _toggleBtn = null;

    // Fullscreen DOM references (for overlay positioning)
    let _fsContainer = null;
    let _fsImage = null;

    // =========================================================================
    // CONSTANTS
    // =========================================================================

    /** Confidence band width for the "borderline" zone above threshold.
     *  Faces within this margin of the threshold are orange; above are green. */
    const BAND_WIDTH = 0.02;

    /** How far below threshold to still show a bbox (avoids clutter). */
    const BELOW_CUTOFF = 0.10;

    // =========================================================================
    // ENTER / EXIT
    // =========================================================================

    /**
     * Enter face detection tuning mode.
     * Hides the normal face tagging overlay, shows the tuning panel, and
     * runs an initial detection with the current config values.
     */
    async function enter() {
        if (_active) return;
        if (!Fullscreen.isOpen()) return;

        _active = true;
        _currentImageId = Fullscreen.state.currentId;
        if (!_currentImageId) {
            _active = false;
            return;
        }

        // Seed sliders from current config
        const conf = App.config || {};
        _originalConfidence = conf.face_detection_min_confidence ?? 0.95;
        _originalFaceSize = conf.face_detection_min_size ?? 40;

        _confidenceSlider.value = _originalConfidence;
        _confidenceValue.textContent = Number(_originalConfidence).toFixed(2);
        _faceSizeSlider.value = _originalFaceSize;
        _faceSizeValue.textContent = `${_originalFaceSize}px`;

        // Hide normal face overlay while tuning
        const faceOverlay = document.getElementById('face-overlay');
        if (faceOverlay) faceOverlay.hidden = true;

        // Show tuning UI
        _overlay.hidden = false;
        _panel.hidden = false;
        _toggleBtn?.classList.add('active');

        // Run initial detection
        await _detect();
    }

    /**
     * Exit tuning mode without saving.
     * Restores the normal face overlay and clears the tuning preview.
     */
    function exit() {
        if (!_active) return;
        _active = false;
        _currentImageId = null;
        _cachedDetections = [];

        // Clear and hide tuning UI
        _overlay.innerHTML = '';
        _overlay.hidden = true;
        _panel.hidden = true;
        _toggleBtn?.classList.remove('active');
        _countEl.textContent = '';

        // Restore normal face overlay
        const faceOverlay = document.getElementById('face-overlay');
        if (faceOverlay && typeof Faces !== 'undefined' && Faces.isTaggingModeActive?.()) {
            faceOverlay.hidden = false;
        }
    }

    /** @returns {boolean} Whether tuning mode is currently active. */
    function isActive() {
        return _active;
    }

    // =========================================================================
    // DETECTION
    // =========================================================================

    /**
     * Run face detection on the current image via the preview endpoint.
     * Updates the cached detections and re-renders the overlay.
     */
    async function _detect() {
        if (!_active || !_currentImageId) return;
        if (_detecting) return;

        _detecting = true;
        _detectBtn.disabled = true;
        _detectBtn.textContent = 'Detecting\u2026';

        try {
            const resp = await App.apiPost('/faces/detect-preview', {
                image_id: _currentImageId,
                min_face_size: parseInt(_faceSizeSlider.value, 10),
            });

            // Bail if we left tuning mode or changed image during the request
            if (!_active || _currentImageId !== Fullscreen.state.currentId) return;

            _cachedDetections = resp.data || [];
            _render();
        } catch (err) {
            console.error('[FaceTuning] Detection failed:', err);
        } finally {
            _detecting = false;
            _detectBtn.disabled = false;
            _detectBtn.textContent = 'Detect';
        }
    }

    // =========================================================================
    // RENDERING
    // =========================================================================

    /**
     * Render the cached detections onto the tuning overlay, filtered and
     * colour-coded by the current confidence slider value.
     */
    function _render() {
        _overlay.innerHTML = '';

        if (!_fsImage || !_fsContainer || !_fsImage.complete) return;

        const threshold = parseFloat(_confidenceSlider.value);

        // Filter: show faces above threshold, borderline above, borderline
        // below, but skip anything far below to avoid clutter.
        const visible = _cachedDetections.filter(
            f => f.confidence >= threshold - BELOW_CUTOFF,
        );

        // Position the overlay to match the fullscreen image
        _positionOverlay();

        let aboveCount = 0;

        for (const face of visible) {
            const box = document.createElement('div');
            box.className = 'face-tuning-box';

            // Colour coding relative to threshold
            const diff = face.confidence - threshold;
            if (diff >= BAND_WIDTH) {
                box.classList.add('confidence-above');
                aboveCount++;
            } else if (diff >= 0) {
                box.classList.add('confidence-borderline');
                aboveCount++;
            } else {
                box.classList.add('confidence-below');
            }

            // Position from normalised coordinates
            box.style.left = `${face.box_x * 100}%`;
            box.style.top = `${face.box_y * 100}%`;
            box.style.width = `${face.box_w * 100}%`;
            box.style.height = `${face.box_h * 100}%`;

            // Confidence label
            const label = document.createElement('span');
            label.className = 'tuning-label';
            label.textContent = face.confidence.toFixed(2);
            box.appendChild(label);

            _overlay.appendChild(box);
        }

        // Update count display
        _countEl.textContent = `${aboveCount} face${aboveCount !== 1 ? 's' : ''}`;
    }

    /**
     * Position and size the tuning overlay to match the fullscreen image.
     * Mirrors the logic in faces.js renderFaces().
     */
    function _positionOverlay() {
        const containerRect = _fsContainer.getBoundingClientRect();
        const imgNaturalWidth = _fsImage.naturalWidth || containerRect.width;
        const imgNaturalHeight = _fsImage.naturalHeight || containerRect.height;

        const containerAspect = containerRect.width / containerRect.height;
        const imgAspect = imgNaturalWidth / imgNaturalHeight;

        let baseWidth, baseHeight;
        if (imgAspect > containerAspect) {
            baseWidth = containerRect.width;
            baseHeight = containerRect.width / imgAspect;
        } else {
            baseHeight = containerRect.height;
            baseWidth = containerRect.height * imgAspect;
        }

        const offsetX = (containerRect.width - baseWidth) / 2;
        const offsetY = (containerRect.height - baseHeight) / 2;

        _overlay.style.position = 'absolute';
        _overlay.style.left = `${offsetX}px`;
        _overlay.style.top = `${offsetY}px`;
        _overlay.style.width = `${baseWidth}px`;
        _overlay.style.height = `${baseHeight}px`;
        _overlay.style.transformOrigin = 'center';

        // Sync with fullscreen zoom/pan
        const { zoom = 1, panX = 0, panY = 0 } = Fullscreen.state || {};
        _overlay.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    }

    // =========================================================================
    // APPLY / CANCEL
    // =========================================================================

    /**
     * Save the current slider values to the config and exit tuning mode.
     */
    async function _apply() {
        const confidence = parseFloat(_confidenceSlider.value);
        const faceSize = parseInt(_faceSizeSlider.value, 10);

        _applyBtn.disabled = true;
        try {
            await App.apiPost('/config/save', {
                values: {
                    face_detection_min_confidence: confidence,
                    face_detection_min_size: faceSize,
                },
            });

            // Update the cached config so future enters see the new values
            if (App.config) {
                App.config.face_detection_min_confidence = confidence;
                App.config.face_detection_min_size = faceSize;
            }

            App.toast?.(`Face detection settings saved (confidence ${confidence.toFixed(2)}, min size ${faceSize}px)`);
        } catch (err) {
            console.error('[FaceTuning] Failed to save config:', err);
            App.toast?.('Failed to save settings');
        } finally {
            _applyBtn.disabled = false;
        }

        exit();
    }

    // =========================================================================
    // EVENT HANDLERS
    // =========================================================================

    /**
     * Handle confidence slider input (live refilter, no backend call).
     */
    function _onConfidenceInput() {
        _confidenceValue.textContent = Number(_confidenceSlider.value).toFixed(2);
        if (_active) _render();
    }

    /**
     * Handle face-size slider input (update display only).
     */
    function _onFaceSizeInput() {
        _faceSizeValue.textContent = `${_faceSizeSlider.value}px`;
    }

    /**
     * Handle face-size slider release (trigger backend re-detection).
     */
    function _onFaceSizeCommit() {
        if (_active) _detect();
    }

    /**
     * Handle fullscreen transform changes (zoom/pan) while tuning.
     */
    function _onTransformChanged() {
        if (_active && _overlay && !_overlay.hidden) {
            const { zoom = 1, panX = 0, panY = 0 } = Fullscreen.state || {};
            _overlay.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
        }
    }

    /**
     * Handle window resize — reposition overlay.
     */
    let _resizeTimer = null;
    function _onResize() {
        if (!_active) return;
        if (_resizeTimer) clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(() => {
            _resizeTimer = null;
            if (_active) _render();
        }, 100);
    }

    // =========================================================================
    // INITIALISATION
    // =========================================================================

    function init() {
        // Cache DOM references
        _overlay = document.getElementById('face-tuning-overlay');
        _panel = document.getElementById('face-tuning-panel');
        _countEl = document.getElementById('face-tuning-count');
        _confidenceSlider = document.getElementById('tuning-confidence');
        _confidenceValue = document.getElementById('tuning-confidence-value');
        _faceSizeSlider = document.getElementById('tuning-face-size');
        _faceSizeValue = document.getElementById('tuning-face-size-value');
        _detectBtn = document.getElementById('tuning-detect');
        _applyBtn = document.getElementById('tuning-apply');
        _cancelBtn = document.getElementById('tuning-cancel');
        _toggleBtn = document.getElementById('fullscreen-tune-faces');

        _fsContainer = document.getElementById('fullscreen-container');
        _fsImage = document.getElementById('fullscreen-image');

        if (!_overlay || !_panel) return;

        // Slider events
        _confidenceSlider.addEventListener('input', _onConfidenceInput);
        _faceSizeSlider.addEventListener('input', _onFaceSizeInput);
        _faceSizeSlider.addEventListener('mouseup', _onFaceSizeCommit);
        _faceSizeSlider.addEventListener('touchend', _onFaceSizeCommit);

        // Button events
        _detectBtn.addEventListener('click', _detect);
        _applyBtn.addEventListener('click', _apply);
        _cancelBtn.addEventListener('click', exit);
        _toggleBtn?.addEventListener('click', () => {
            if (_active) {
                exit();
            } else {
                enter();
            }
        });

        // Track fullscreen zoom/pan
        App.on('fullscreenTransformChanged', _onTransformChanged);

        // Exit tuning when navigating to a different image
        AppState.nav.onChanged((event) => {
            if (!_active) return;
            if (event.property === 'fullscreenImageId') {
                exit();
            } else if (event.property === 'fullscreenClosing') {
                exit();
            }
        });

        // Resize handling
        window.addEventListener('resize', _onResize);
    }

    // Initialise when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Export public API
    window.FaceTuning = {
        enter,
        exit,
        isActive,
    };

})();
