/**
 * Face tagging module for Imaginary.
 *
 * This module handles two distinct UI contexts:
 *
 * 1. FULLSCREEN TAGGING MODE - Overlay on fullscreen image viewer
 *    - Renders bounding boxes over detected faces
 *    - Inline name input with autocomplete for identification
 *    - Suppress button (X) to mark false positives
 *    - Activated via toolbar toggle button
 *
 * 2. FACES SCREEN - Dedicated screen for face/people management
 *    - Two sections: Known people (identified) and Unknown faces
 *    - Known section: Static grid of person cards (one per person, shows preferred face)
 *    - Unknown section: VirtualGrid of unidentified face thumbnails
 *    - Supports batch operations via multi-select
 *
 * VIEW MODES (Faces Screen):
 *   'all'            - Default. Shows known people section + unknown faces section
 *   'unknowns'       - Hides known section, only shows unknown faces (toolbar toggle)
 *   'pick-preferred' - Focus mode for one person. Shows all their faces with star
 *                      icons to select preferred. Entered by double-clicking a
 *                      known person or via focus button. Delete key here unassigns
 *                      faces (returns to unknown pool) rather than suppressing.
 *
 * DATA FLOW:
 *   API /faces → allFaces[] → (filter by search) → displayedFaces[] → VirtualGrid
 *   API /faces → (group by person) → knownPeople[] → static DOM cards
 *
 * CACHES (see detailed docs in cache section below):
 *   - AppState.people: Autocomplete suggestions (TTL-based, managed by AppState)
 *   - thumbnailCacheBust: Forces browser to refetch changed person thumbnails
 *   - knownPeople: Denormalized people+faces for known section
 *   - allFaces/displayedFaces: Face data with client-side filtering
 *
 * REFRESH FLAGS (coordinate updates without full reloads):
 *   - needsRefresh: Full API reload needed on next screen enter
 *   - needsRerender: Just re-render grid (local data already updated)
 *   - reloadPending: Deferred reload waiting for selection to clear
 *
 * @module faces
 */

/* global App */

(function () {
    'use strict';

    // =========================================================================
    // STATE - FULLSCREEN TAGGING MODE
    // =========================================================================

    /** @type {boolean} Whether face tagging mode is active (toolbar toggle) */
    let taggingMode = false;

    /** @type {boolean} Whether face detection is enabled in config */
    let faceDetectionEnabled = true;

    // -------------------------------------------------------------------------
    // -------------------------------------------------------------------------
    // AUTOCOMPLETE STATE - Tracks active dropdown
    // -------------------------------------------------------------------------
    // People data is managed by AppState.people.
    // Use AppState.people.search() for autocomplete queries.

    /** @type {HTMLElement|null} Currently focused face input field */
    let focusedInput = null;

    /** @type {HTMLElement|null} Currently open autocomplete dropdown */
    let activeAutocomplete = null;

    /** @type {number} Selected index in autocomplete list */
    let autocompleteSelectedIndex = -1;

    // =========================================================================
    // DOM REFERENCES
    // =========================================================================

    /** @type {HTMLElement} */
    let faceOverlay;

    /** @type {HTMLElement} */
    let fullscreenContainer;

    /** @type {HTMLElement} */
    let fullscreenImage;

    /** @type {HTMLButtonElement} */
    let btnFaceTagging;

    /** @type {HTMLButtonElement} */
    let btnFaces;

    // =========================================================================
    // FACES SCREEN STATE
    // =========================================================================
    //
    // DATA ARRAYS AND THEIR RELATIONSHIPS:
    //
    //   allFaces[]        - Raw API response. All faces (known + unknown).
    //                       Each face has: id, image_id, person_id, person_name,
    //                       bbox, is_preferred, image_timestamp, etc.
    //
    //   displayedFaces[]  - Filtered/sorted subset for current view.
    //                       In 'all'/'unknowns' mode: unknown faces only.
    //                       Fed to unknownFacesGrid for virtual rendering.
    //
    //   knownPeople[]     - Grouped by person for known section. Each entry:
    //                       {id, name, faces[], preferredFace}
    //                       Built from allFaces where person_id is set.
    //                       Rendered as static DOM (not virtualized - small count).
    //
    //   pickPreferredFaces[] - In pick-preferred mode only. All faces for one
    //                          person, loaded separately via /people/:id/faces.
    //
    // WHY SEPARATE ARRAYS: The known section needs person-grouped data while
    // unknown section needs flat face list. Keeping both avoids repeated
    // grouping operations. displayedFaces is separate from allFaces to support
    // client-side filtering (search) without re-fetching.

    /** @type {number} Thumbnail size for faces screen (pixels) */
    let facesThumbnailSize = 100;

    /** @type {boolean} Show only unknown faces (hides known section) */
    let showOnlyUnknowns = false;

    /** @type {boolean} Sort direction for unknown faces (true = oldest first) */
    let sortAscending = true;

    /** @type {number|null} Known section height in pixels (null = auto/default) */
    let knownSectionHeight = null;

    // Load persisted known section height from localStorage
    try {
        const saved = localStorage.getItem('faces-known-height');
        if (saved) knownSectionHeight = parseInt(saved, 10);
    } catch (e) { /* ignore */ }

    /** @type {Array<Object>} All faces from API (source of truth for this session) */
    let allFaces = [];

    /** @type {boolean} Whether faces screen is currently loading from API */
    let isLoading = false;

    // -------------------------------------------------------------------------
    // REFRESH FLAGS - Coordinate screen updates without unnecessary API calls
    // -------------------------------------------------------------------------
    // These flags work together to handle updates from various sources:
    // - User actions in fullscreen tagging mode
    // - Background face reassessment completing
    // - User actions within faces screen itself
    //
    // Priority: needsRefresh (full reload) > needsRerender (just redraw grid)
    // The reloadPending flag defers reload until user finishes their selection.

    /** @type {boolean} Full API reload needed on next screen enter */
    let needsRefresh = true;

    /** @type {boolean} Grid re-render needed (data already updated locally) */
    let needsRerender = false;

    /** @type {boolean} Suppress AppState subscription re-render during optimistic updates */
    let suppressBroadcastRender = false;

    /** @type {number} Saved scroll position for unknown faces container */
    let savedScrollTop = 0;

    // Faces screen DOM references
    /** @type {HTMLElement} */
    let facesGrid;

    /** @type {HTMLElement} */
    let facesEmpty;

    /** @type {HTMLElement} */
    let facesLoading;

    /** @type {HTMLButtonElement} */
    let btnFacesThumbSmaller;

    /** @type {HTMLButtonElement} */
    let btnFacesThumbLarger;

    /** @type {HTMLButtonElement} */
    let btnFacesOnlyUnknowns;

    /** @type {HTMLButtonElement} */
    let btnFacesFocusPerson;

    /** @type {HTMLButtonElement} */
    let btnFacesSortDirection;

    /** @type {Object|null} GridSelection instance for faces screen */
    let facesSelection = null;

    /** @type {Array<Object>} Currently displayed unknown faces (for selection) */
    let displayedFaces = [];

    /**
     * Whether a full reload is pending (deferred because user had active selection).
     * Checked by handleFacesSelectionChanged to trigger reload when selection clears.
     *
     * IMPORTANT: Any operation that handles its own update/reload should set this
     * to false BEFORE clearing selection, to prevent handleFacesSelectionChanged
     * from triggering a duplicate/interfering reload.
     * @type {boolean}
     */
    let reloadPending = false;

    /** @type {Object|null} VirtualGrid instance for unknown faces section */
    let unknownFacesGrid = null;

    /** @type {Array<Object>} Known people with faces, for static known section */
    let knownPeople = [];

    // -------------------------------------------------------------------------
    // VIEW MODE STATE
    // -------------------------------------------------------------------------
    // The faces screen has three view modes with different UI and behavior:
    //
    //   'all'            Default. Known section (static) + Unknown section (VirtualGrid).
    //                    Selection applies to unknown faces only.
    //                    Delete = suppress (mark as false positive).
    //
    //   'unknowns'       Only unknown faces (hides known section).
    //                    Same behavior as 'all', just filtered view.
    //
    //   'pick-preferred' Focus on single person. Replaces both sections with
    //                    VirtualGrid of that person's faces. Star icon on each
    //                    face to set as preferred thumbnail. Delete = unassign
    //                    (return face to unknown pool, not suppress).
    //
    // Transitions:
    //   'all' ↔ 'unknowns'       : Toggle button in toolbar
    //   'all' → 'pick-preferred' : Double-click known person OR focus button
    //   'pick-preferred' → 'all' : Focus button OR Escape key

    /** @type {string} Current view mode */
    let viewMode = 'all';

    /** @type {string|null} Person ID in pick-preferred mode (null otherwise) */
    let pickPreferredPersonId = null;

    /** @type {string|null} Person name in pick-preferred mode (for header display) */
    let pickPreferredPersonName = null;

    /** @type {Object|null} VirtualGrid for pick-preferred mode (separate from unknownFacesGrid) */
    let pickPreferredGrid = null;

    /** @type {Array<Object>} All faces for focused person in pick-preferred mode */
    let pickPreferredFaces = [];

    /** @type {number|null} Per-person recognition threshold (null = use global default) */
    let pickPreferredPersonThreshold = null;

    // -------------------------------------------------------------------------
    // THUMBNAIL CACHE BUSTING
    // -------------------------------------------------------------------------
    // Problem: Browser caches /api/people/:id/thumbnail URLs. When the preferred
    // face changes (star click, suppression, reassignment), the cached thumbnail
    // becomes stale.
    //
    // Solution: Map of personId → timestamp. When rendering person thumbnails,
    // append ?t=timestamp to force refetch. The timestamp is set when:
    //   - User clicks star to change preferred face
    //   - User suppresses a face that was the preferred face
    //   - User reassigns the preferred face to another person
    //
    // Used by: renderKnownFacesSection(), showCardAutocomplete(), fullscreen
    // autocomplete rendering. Cleared on page reload (acceptable - browser
    // cache will eventually expire anyway).

    /** @type {Map<string, number>} Person ID → cache bust timestamp */
    let thumbnailCacheBust = new Map();

    /** @type {string} Current semantic search query for filtering unknown faces */
    let unknownFacesSearchQuery = '';

    // =========================================================================
    // INITIALIZATION
    // =========================================================================

    /**
     * Initialize the faces module.
     * Called when DOM is ready.
     */
    function init() {
        // Get DOM references - Tagging overlay
        faceOverlay = document.getElementById('face-overlay');
        fullscreenContainer = document.getElementById('fullscreen-container');
        fullscreenImage = document.getElementById('fullscreen-image');
        btnFaceTagging = document.getElementById('btn-face-tagging');
        btnFaces = document.getElementById('btn-faces');

        // Get DOM references - Faces screen
        facesGrid = document.getElementById('faces-grid');
        facesEmpty = document.getElementById('faces-empty');
        facesLoading = document.getElementById('faces-loading');
        btnFacesThumbSmaller = document.getElementById('btn-faces-thumb-smaller');
        btnFacesThumbLarger = document.getElementById('btn-faces-thumb-larger');
        btnFacesOnlyUnknowns = document.getElementById('btn-faces-only-unknowns');
        btnFacesFocusPerson = document.getElementById('btn-faces-focus-person');
        btnFacesSortDirection = document.getElementById('btn-faces-sort-direction');

        // Check if face detection is enabled
        loadFaceDetectionConfig();

        // Set up event listeners
        setupEventListeners();

        // Set up faces screen event listeners
        setupFacesScreenListeners();

        // Listen for screen changes
        App.on('screenChanged', handleScreenChange);

        // Listen for image changes in fullscreen
        App.on('fullscreenImageChanged', handleFullscreenImageChange);

        // Listen for transform changes (zoom/pan) in fullscreen
        App.on('fullscreenTransformChanged', handleFullscreenTransformChange);

        // Listen for database changes (e.g., after scan completes)
        App.on('databaseChanged', () => {
            needsRefresh = true;
        });

        // Subscribe to AppState.faces for centralized state management
        // Note: We handle our own optimistic updates in faces.js, so this subscription
        // is mainly for external changes (e.g., from fullscreen tagging mode).
        AppState.faces.onChanged((event) => {
            // Skip if we're not on the faces screen
            if (App.getScreen() !== 'faces') {
                // Mark for refresh when we return to faces screen
                needsRefresh = true;
                return;
            }
            // Skip if data isn't loaded yet
            if (!AppState.faces.isLoaded()) return;

            // Skip re-render if we're in an optimistic update (we already rendered)
            // This prevents a race condition where the subscription fires after our
            // optimistic render, potentially replacing DOM elements mid-initialization
            if (suppressBroadcastRender) {
                return;
            }

            // For external changes, sync and re-render
            // Our own optimistic updates already handle local state, but this catches
            // changes from other sources (fullscreen mode, background reassessment, etc.)
            allFaces = AppState.faces.getAll();
            needsRerender = true;
            // Trigger re-render on next animation frame to batch multiple changes
            requestAnimationFrame(() => {
                if (needsRerender && App.getScreen() === 'faces') {
                    needsRerender = false;
                    renderFacesGrid();
                }
            });
        });

        // Register the faces screen module
        // Note: GridSelection is initialized in renderFacesGrid after VirtualGrid is set up
        registerFacesModule();
    }

    /**
     * Load face detection config from backend.
     */
    async function loadFaceDetectionConfig() {
        try {
            // Use AppState.status - load if not already loaded
            let status = AppState.status.get();
            if (!status) {
                status = await AppState.status.load();
            }
            if (status && status.face_detection_enabled !== undefined) {
                faceDetectionEnabled = status.face_detection_enabled;
                updateButtonStates();
            }
        } catch (error) {
            console.warn('Failed to load face detection config:', error);
        }
    }

    /**
     * Set up DOM event listeners.
     */
    function setupEventListeners() {
        // Face tagging toggle button
        if (btnFaceTagging) {
            btnFaceTagging.addEventListener('click', toggleTaggingMode);
        }

        // Faces screen button
        if (btnFaces) {
            btnFaces.addEventListener('click', () => {
                App.navigateTo('faces');
            });
        }

        // Close autocomplete when clicking outside
        document.addEventListener('click', (e) => {
            if (activeAutocomplete && !activeAutocomplete.contains(e.target)) {
                closeAutocomplete();
            }
        });

        // Handle keyboard navigation in fullscreen
        document.addEventListener('keydown', handleKeyDown);
    }

    /**
     * Update button states based on face detection enabled status.
     */
    function updateButtonStates() {
        if (btnFaceTagging) {
            btnFaceTagging.disabled = !faceDetectionEnabled;
            btnFaceTagging.title = faceDetectionEnabled
                ? 'Toggle face tagging mode'
                : 'Face detection is disabled in settings';
        }

        if (btnFaces) {
            btnFaces.disabled = !faceDetectionEnabled;
            btnFaces.title = faceDetectionEnabled
                ? 'Browse faces'
                : 'Face detection is disabled in settings';
        }
    }

    /**
     * Set up event listeners for faces screen controls.
     */
    function setupFacesScreenListeners() {
        // Thumbnail size buttons
        if (btnFacesThumbSmaller) {
            btnFacesThumbSmaller.addEventListener('click', () => {
                setFacesThumbnailSize(facesThumbnailSize - 20);
            });
        }

        if (btnFacesThumbLarger) {
            btnFacesThumbLarger.addEventListener('click', () => {
                setFacesThumbnailSize(facesThumbnailSize + 20);
            });
        }

        // Only unknowns toggle button
        if (btnFacesOnlyUnknowns) {
            btnFacesOnlyUnknowns.addEventListener('click', () => {
                if (viewMode === 'pick-preferred') {
                    // Exit pick-preferred mode first
                    exitPickPreferredMode();
                }
                showOnlyUnknowns = !showOnlyUnknowns;
                updateOnlyUnknownsButton();
                renderFacesGrid();
            });
        }

        // Focus person button (for pick-preferred mode)
        if (btnFacesFocusPerson) {
            btnFacesFocusPerson.addEventListener('click', handleFocusButtonClick);
        }

        // Sort direction button
        if (btnFacesSortDirection) {
            btnFacesSortDirection.addEventListener('click', () => {
                sortAscending = !sortAscending;
                updateSortDirectionIcon();
                renderFacesGrid();
            });
        }

        // Keyboard handler for known section (Enter to enter pick-preferred)
        document.addEventListener('keydown', (e) => {
            // Only handle when on faces screen and not in pick-preferred mode
            if (App.getScreen() !== 'faces') return;
            if (viewMode === 'pick-preferred') return;

            // Don't intercept if focus is in an input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            if (e.key === 'Enter') {
                const selectedPersonId = getSelectedKnownPersonId();
                if (selectedPersonId) {
                    e.preventDefault();
                    enterPickPreferredMode(selectedPersonId);
                }
            }
        });
    }

    /**
     * Update the only-unknowns button active state.
     */
    function updateOnlyUnknownsButton() {
        if (btnFacesOnlyUnknowns) {
            btnFacesOnlyUnknowns.classList.toggle('active', showOnlyUnknowns);
        }
    }

    /**
     * Update the focus button state based on current selection.
     */
    function updateFocusButtonState() {
        if (!btnFacesFocusPerson) return;

        if (viewMode === 'pick-preferred') {
            // In pick-preferred mode, button is active and enabled (to exit)
            btnFacesFocusPerson.classList.add('active');
            btnFacesFocusPerson.disabled = false;
            btnFacesFocusPerson.title = 'Exit focus mode';
        } else {
            // Normal mode: disabled unless a known person is selected
            btnFacesFocusPerson.classList.remove('active');
            btnFacesFocusPerson.title = 'Focus on one person';

            // Check if exactly one person card is selected (not face card)
            const selectedPersonId = getSelectedKnownPersonId();
            btnFacesFocusPerson.disabled = !selectedPersonId;
        }
    }

    /**
     * Get the person ID if a known person card is selected.
     * Returns null if no known person is selected.
     */
    function getSelectedKnownPersonId() {
        // Look for selected person cards in the known section
        const selectedPersonCard = facesGrid?.querySelector('.face-card.person-card.selected');
        return selectedPersonCard?.dataset.personId || null;
    }

    /**
     * Handle focus button click.
     */
    function handleFocusButtonClick() {
        if (viewMode === 'pick-preferred') {
            // Exit pick-preferred mode
            exitPickPreferredMode();
        } else {
            // Enter pick-preferred mode for selected person
            const selectedPersonId = getSelectedKnownPersonId();
            if (selectedPersonId) {
                enterPickPreferredMode(selectedPersonId);
            }
        }
    }

    /**
     * Enter pick-preferred mode for a person.
     *
     * WHAT THIS MODE DOES:
     * - Replaces known/unknown sections with single grid of person's faces
     * - Each face card has a star icon (★/☆) to set as preferred thumbnail
     * - Delete key unassigns faces (returns to unknown) instead of suppressing
     * - Triggers reassessment to find similar unknown faces
     *
     * STATE CHANGES:
     * - viewMode → 'pick-preferred'
     * - pickPreferredPersonId/Name set for header display
     * - pickPreferredFaces loaded from API (separate from allFaces)
     * - pickPreferredGrid replaces unknownFacesGrid
     *
     * WHY SEPARATE FACES ARRAY: The /people/:id/faces endpoint returns faces
     * sorted by timestamp with is_preferred flag. This is different from
     * allFaces which contains all faces grouped by person.
     *
     * @param {string} personId - Person ID to focus on
     */
    function enterPickPreferredMode(personId) {
        // Get name from local cache for immediate header display
        const localPerson = knownPeople.find(p => p.id === personId);
        if (!localPerson) return;

        // Set state before async load (enables UI update)
        viewMode = 'pick-preferred';
        pickPreferredPersonId = personId;
        pickPreferredPersonName = localPerson.name;
        pickPreferredPersonThreshold = null;
        pickPreferredFaces = []; // Start empty, will populate when data loads

        // Render immediately with loading state
        renderPickPreferredMode();
        updateFocusButtonState();

        // Load person details and faces in background
        Promise.all([
            AppState.people.fetchById(personId),       // For recognition_threshold
            AppState.faces.fetchForPerson(personId)    // All faces for this person
        ]).then(([personResult, faces]) => {
            // Only update if still in pick-preferred mode for this person
            if (viewMode !== 'pick-preferred' || pickPreferredPersonId !== personId) return;

            pickPreferredFaces = faces || [];
            pickPreferredPersonThreshold = personResult?.recognition_threshold ?? null;

            // Re-render with actual data
            renderPickPreferredMode();
        }).catch(error => {
            console.error('Failed to load person data:', error);
            // Still in loading state but with empty data - renderPickPreferredMode handles this
        });
    }

    /**
     * Exit pick-preferred mode and return to normal all/unknowns view.
     *
     * Cleans up pick-preferred state and restores the standard two-section
     * layout. Does NOT reload from API - uses existing allFaces/knownPeople.
     */
    function exitPickPreferredMode() {
        viewMode = 'all';
        pickPreferredPersonId = null;
        pickPreferredPersonName = null;
        pickPreferredFaces = [];
        pickPreferredPersonThreshold = null;

        // Clean up pick-preferred grid
        if (pickPreferredGrid) {
            pickPreferredGrid.unbind();
            pickPreferredGrid.destroy();
            pickPreferredGrid = null;
        }

        // Re-render normal faces grid
        renderFacesGrid();
        updateFocusButtonState();
    }

    /**
     * Render the pick-preferred mode view.
     */
    function renderPickPreferredMode() {
        if (!facesGrid) return;

        // Clear current grid and unbind
        if (unknownFacesGrid) {
            unknownFacesGrid.unbind();
            unknownFacesGrid.destroy();
            unknownFacesGrid = null;
        }
        if (facesSelection) {
            facesSelection.unbind();
            facesSelection = null;
        }

        facesGrid.innerHTML = '';

        // Create header
        const header = document.createElement('div');
        header.className = 'faces-pick-preferred-header';
        const faceCount = pickPreferredFaces.length;
        const countText = faceCount === 1 ? '1 image' : `${faceCount} images`;

        const titleRow = document.createElement('div');
        titleRow.className = 'faces-pick-preferred-title-row';

        // Left side: name, count, rename button
        const titleLeft = document.createElement('div');
        titleLeft.className = 'faces-pick-preferred-title-left';

        const title = document.createElement('h3');
        title.innerHTML = `${App.escapeHtml(pickPreferredPersonName)} <span class="face-count">(${countText})</span>`;

        const renameBtn = document.createElement('button');
        renameBtn.className = 'faces-rename-btn';
        renameBtn.title = 'Rename person';
        renameBtn.innerHTML = '<span class="material-symbols-outlined">edit</span>';
        renameBtn.addEventListener('click', handleRenamePersonClick);

        titleLeft.appendChild(title);
        titleLeft.appendChild(renameBtn);
        titleRow.appendChild(titleLeft);

        // Right side: threshold slider
        const thresholdControl = document.createElement('div');
        thresholdControl.className = 'faces-threshold-control';

        const thresholdLabel = document.createElement('label');
        thresholdLabel.textContent = 'Match threshold:';
        thresholdLabel.htmlFor = 'threshold-slider';

        const thresholdSlider = document.createElement('input');
        thresholdSlider.type = 'range';
        thresholdSlider.id = 'threshold-slider';
        thresholdSlider.className = 'faces-threshold-slider';
        thresholdSlider.min = '60';
        thresholdSlider.max = '99';
        thresholdSlider.step = '1';
        // Convert threshold (0.0-1.0) to percentage (60-99)
        const currentPercent = pickPreferredPersonThreshold !== null
            ? Math.round(pickPreferredPersonThreshold * 100)
            : 80;  // Default display value
        thresholdSlider.value = String(currentPercent);

        const thresholdValue = document.createElement('span');
        thresholdValue.className = 'faces-threshold-value';
        thresholdValue.textContent = pickPreferredPersonThreshold !== null
            ? `${currentPercent}%`
            : 'default';

        // Update display on input
        thresholdSlider.addEventListener('input', () => {
            thresholdValue.textContent = `${thresholdSlider.value}%`;
        });

        // Save on change (mouse release)
        thresholdSlider.addEventListener('change', () => handleThresholdChange(thresholdSlider.value));

        // Reset to default button
        const resetBtn = document.createElement('button');
        resetBtn.className = 'faces-threshold-reset';
        resetBtn.title = 'Reset to default';
        resetBtn.innerHTML = '<span class="material-symbols-outlined">restart_alt</span>';
        resetBtn.addEventListener('click', () => handleThresholdReset(thresholdSlider, thresholdValue));

        thresholdControl.appendChild(thresholdLabel);
        thresholdControl.appendChild(thresholdSlider);
        thresholdControl.appendChild(thresholdValue);
        thresholdControl.appendChild(resetBtn);
        titleRow.appendChild(thresholdControl);

        header.appendChild(titleRow);

        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.textContent = 'Click a star to set as preferred face. Press Delete to unassign faces.';
        header.appendChild(hint);

        facesGrid.appendChild(header);

        // Create container for the grid
        const container = document.createElement('div');
        container.className = 'faces-pick-preferred-container';
        facesGrid.appendChild(container);

        // Show loading state if no faces yet
        if (pickPreferredFaces.length === 0) {
            const loading = document.createElement('div');
            loading.className = 'faces-loading-inline';
            loading.innerHTML = '<div class="loading-spinner"></div><p>Loading faces…</p>';
            container.appendChild(loading);
            // Don't create the grid yet - will be created when data loads
            return;
        }

        // Set displayed faces for selection
        displayedFaces = pickPreferredFaces;

        // Create VirtualGrid for pick-preferred mode
        pickPreferredGrid = VirtualGrid.create({
            container: container,
            getItems: () => pickPreferredFaces,
            getItemId: (face) => face.id,
            createItem: (face, index, blobUrl) => createPickPreferredFaceCard(face, blobUrl),
            getThumbnailId: (face) => face.id,
            getThumbnailUrl: (faceId) => `/api/faces/${faceId}/thumbnail`,
            itemSelector: '.face-card',
            gap: 16,
            padding: 16,
            getThumbSize: () => facesThumbnailSize,
            getItemHeight: (thumbSize, itemWidth) => itemWidth + 50,
            onItemCreated: (id, el) => {
                if (facesSelection && facesSelection.isSelected(id)) {
                    el.classList.add('selected');
                }
            }
        });

        pickPreferredGrid.render();
        pickPreferredGrid.bind();

        // Initialize selection for pick-preferred mode
        facesSelection = GridSelection.create({
            grid: pickPreferredGrid,
            getItems: () => pickPreferredFaces,
            getItemId: (face) => face.id,
            itemSelector: '.face-card',
            selectedClass: 'selected',
            onSelectionChanged: handlePickPreferredSelectionChanged,
            onItemActivated: handlePickPreferredFaceActivated,
            onDeleteRequested: handlePickPreferredDeleteRequested,
            enableKeyboard: true,
            enableDragBox: true,
            enableLongPress: true
        });

        facesSelection.bind();
    }

    /**
     * Create a face card for pick-preferred mode with star overlay and editable name.
     * @param {Object} face - Face object
     * @param {string} blobUrl - Blob URL for the thumbnail
     * @returns {HTMLElement}
     */
    function createPickPreferredFaceCard(face, blobUrl) {
        const card = document.createElement('div');
        card.className = 'face-card';
        card.dataset.id = face.id;

        const thumb = document.createElement('div');
        thumb.className = 'face-card-thumb';

        const img = document.createElement('img');
        img.src = blobUrl;
        img.alt = pickPreferredPersonName || 'Face';
        thumb.appendChild(img);

        card.appendChild(thumb);

        // Add star overlay (outside thumb to avoid circular clip)
        const star = document.createElement('div');
        star.className = 'face-card-star' + (face.is_preferred ? ' preferred' : '');
        star.dataset.faceId = face.id;
        star.innerHTML = '<span class="material-symbols-outlined">star</span>';
        star.addEventListener('click', (e) => {
            e.stopPropagation();
            handleStarClick(face.id);
        });
        card.appendChild(star);

        // Editable name input (allows reassigning misclassified faces)
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-card-input';
        input.value = pickPreferredPersonName || '';
        input.dataset.faceId = face.id;

        // Handle focus - pre-fetch cache for fast autocomplete
        input.addEventListener('focus', () => {
            // AppState.people.load() handles TTL internally
            AppState.people.load();
        });

        // Handle input for autocomplete
        input.addEventListener('input', () => {
            showCardAutocomplete(input, input.value, card);
        });

        // Handle blur to commit (if name changed)
        input.addEventListener('blur', () => {
            // Delay to allow autocomplete click
            setTimeout(() => {
                const autocomplete = card.querySelector('.face-card-autocomplete');
                if (autocomplete) {
                    autocomplete.remove();
                }
                commitPickPreferredFaceName(face.id, input.value.trim(), card);
            }, 200);
        });

        // Handle keyboard
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.blur();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                input.value = pickPreferredPersonName || '';  // Reset to current name
                input.blur();
            }
        });

        card.appendChild(input);

        return card;
    }

    /**
     * Handle star click to set preferred face.
     * @param {string} faceId - Face ID to set as preferred
     */
    async function handleStarClick(faceId) {
        if (!pickPreferredPersonId) return;

        try {
            // Use AppState.people.setPreferredFace()
            await AppState.people.setPreferredFace(pickPreferredPersonId, faceId);

            // Update local state
            for (const face of pickPreferredFaces) {
                face.is_preferred = (face.id === faceId);
            }

            // Update star visuals
            const allStars = facesGrid.querySelectorAll('.face-card-star');
            allStars.forEach(star => {
                star.classList.toggle('preferred', star.dataset.faceId === faceId);
            });

            // Mark person thumbnail for cache busting when returning to grid
            thumbnailCacheBust.set(pickPreferredPersonId, Date.now());
        } catch (error) {
            console.error('Failed to set preferred face:', error);
            App.showError('Failed to set preferred face.');
        }
    }

    /**
     * Commit a name change for a face in pick-preferred mode.
     *
     * SCENARIO: User is viewing Person A's faces and types a different name
     * (Person B) in one of the face card inputs. This reassigns the face(s)
     * from Person A to Person B.
     *
     * BATCH BEHAVIOR: If faces are selected, reassigns all selected faces.
     * Otherwise just reassigns the single face where user typed.
     *
     * PREFERRED FACE HANDLING:
     * - Backend auto-selects new preferred for source person (Person A)
     * - If the typed face was Person A's preferred, local state marks
     *   first remaining face as preferred (approximation - may differ from
     *   backend's "newest" selection, but will be corrected on next refresh)
     * - Cache busted so thumbnail updates
     *
     * @param {string} typedFaceId - Face ID where user typed the new name
     * @param {string} name - New person name to assign to
     * @param {HTMLElement} card - Card element (for resetting input on no-op)
     */
    function commitPickPreferredFaceName(typedFaceId, name, card) {
        // No-op if name unchanged
        if (!name || name.toLowerCase() === (pickPreferredPersonName || '').toLowerCase()) {
            const input = card.querySelector('.face-card-input');
            if (input) {
                input.value = pickPreferredPersonName || '';
            }
            return;
        }

        // Close autocomplete for better UX
        closeAllAutocompletes();

        // Get selected faces, or just the typed face if none selected
        let faceIds = facesSelection ? facesSelection.getSelected() : [];

        // If the typed face isn't in the selection, or no selection, just use the typed face
        if (faceIds.length === 0 || !faceIds.includes(typedFaceId)) {
            faceIds = [typedFaceId];
        }

        const faceIdSet = new Set(faceIds);

        // Suppress subscription-triggered re-renders during optimistic update
        suppressBroadcastRender = true;

        // Optimistic update: Find or create destination person
        const destPerson = AppState.people.getByName(name);
        const destPersonId = destPerson?.id || `temp-${Date.now()}`;

        // Update face objects in allFaces
        for (const face of allFaces) {
            if (faceIdSet.has(face.id)) {
                face.person_id = destPersonId;
                face.person_name = name;
            }
        }

        // Clear any pending reload flag (we're handling the update ourselves)
        reloadPending = false;

        // Clear selection
        if (facesSelection) {
            facesSelection.clear();
        }

        // Remove reassigned faces from local state
        pickPreferredFaces = pickPreferredFaces.filter(f => !faceIdSet.has(f.id));

        // Check if we reassigned the preferred face
        const reassignedPreferred = pickPreferredFaces.some(f => f.is_preferred) === false
            && pickPreferredFaces.length > 0;
        if (reassignedPreferred) {
            // Mark first remaining face as preferred in local state
            pickPreferredFaces[0].is_preferred = true;
            // Bust cache for current person's thumbnail
            thumbnailCacheBust.set(pickPreferredPersonId, Date.now());
        }

        // If all faces removed, exit pick-preferred mode and reload
        if (pickPreferredFaces.length === 0) {
            exitPickPreferredMode();
            AppState.people.invalidate();
            loadAllFaces();  // Reload to get updated face assignments
        } else {
            // Re-render pick-preferred view
            displayedFaces = pickPreferredFaces;
            pickPreferredGrid.render();

            // Update the count in header
            const titleH3 = facesGrid.querySelector('.faces-pick-preferred-header h3');
            if (titleH3) {
                const faceCount = pickPreferredFaces.length;
                const countText = faceCount === 1 ? '1 face' : `${faceCount} faces`;
                titleH3.innerHTML = `${App.escapeHtml(pickPreferredPersonName)} <span class="face-count">(${countText})</span>`;
            }
        }

        // Fire API in background
        callIdentifyBatchApi(faceIds, name, typedFaceId)
            .then(result => {
                // Re-enable subscription renders now that API completed
                suppressBroadcastRender = false;

                if (result && result.success) {
                    // Update temp ID with real ID if it was a new person
                    if (destPersonId.startsWith('temp-') && result.data?.person?.id) {
                        const realId = result.data.person.id;
                        for (const face of allFaces) {
                            if (face.person_id === destPersonId) {
                                face.person_id = realId;
                            }
                        }
                    }
                    // Bust cache for destination person
                    if (result.data?.person?.id) {
                        thumbnailCacheBust.set(result.data.person.id, Date.now());
                    }
                    // Invalidate people cache (new person may have been created)
                    AppState.people.invalidate();
                    // Note: Backend may trigger async reassessment to find similar faces.
                    // This runs in the background - users see newly matched faces on next load.
                } else {
                    throw new Error(result?.error || 'Unknown error');
                }
            })
            .catch(error => {
                // Re-enable subscription renders
                suppressBroadcastRender = false;

                console.error('Failed to reassign face:', error);
                App.showError(`Failed to reassign ${faceIds.length > 1 ? 'faces' : 'face'}.`);
                // Reload to get correct state
                needsRefresh = true;
                loadAllFaces();
            });
    }

    /**
     * Identify faces via AppState (centralized state management).
     * Returns a response format compatible with legacy callers.
     *
     * @param {Array<string>} faceIds - Face IDs to identify
     * @param {string} name - Name of the person (existing or new)
     * @param {string} [preferredFaceId] - Face ID to set as preferred (for new persons)
     * @returns {Promise<{success: boolean, data?: Object, error?: string}>}
     */
    async function callIdentifyBatchApi(faceIds, name, preferredFaceId = null) {
        try {
            const result = await AppState.faces.identify(faceIds, name, {
                preferredFaceId
            });
            // Return format compatible with legacy callers
            return {
                success: true,
                data: {
                    person: {
                        id: result.personId,
                        name: name
                    }
                }
            };
        } catch (error) {
            return {
                success: false,
                error: error.message || 'Failed to identify faces'
            };
        }
    }

    /**
     * Identify multiple faces as a specific person (normal mode).
     * Used by typing a name in unknown faces and drag-and-drop onto person.
     *
     * Uses optimistic updates - immediately updates local state and fires the
     * API call in the background. No loading spinner needed.
     *
     * @param {Array<string>} faceIds - Face IDs to identify
     * @param {string} name - Name of the person (existing or new)
     * @param {Object} [options] - Optional settings
     * @param {string} [options.preferredFaceId] - Face ID to set as preferred (for new persons)
     * @returns {Promise<boolean>} True if API call succeeded (for callers that need to know)
     */
    async function identifyFacesAsPerson(faceIds, name, options = {}) {
        if (!faceIds || faceIds.length === 0 || !name) return false;

        // Clear selection immediately
        if (facesSelection) {
            facesSelection.clear();
        }

        // Suppress subscription-triggered re-renders during optimistic update
        // (prevents race condition where AppState broadcast fires mid-render)
        suppressBroadcastRender = true;

        // Optimistic update: Find or create person, update face objects locally
        const existingPerson = AppState.people.getByName(name);
        const personId = existingPerson?.id || `temp-${Date.now()}`;  // Temp ID for new person

        // Update local face objects (AppState.faces.identify does this too, but we do it
        // here for immediate UI feedback before the async call completes)
        const faceIdSet = new Set(faceIds);
        for (const face of allFaces) {
            if (faceIdSet.has(face.id)) {
                face.person_id = personId;
                face.person_name = name;
            }
        }

        // Re-render grid immediately with updated faces
        renderFacesGrid();

        // Fire API call in background (AppState handles cache updates on completion)
        // Don't await - let it complete asynchronously
        callIdentifyBatchApi(faceIds, name, options.preferredFaceId)
            .then(result => {
                // Re-enable subscription renders now that API completed
                suppressBroadcastRender = false;

                if (result && result.success) {
                    // Update temp ID with real ID if it was a new person
                    if (personId.startsWith('temp-') && result.data?.person?.id) {
                        const realId = result.data.person.id;
                        for (const face of allFaces) {
                            if (face.person_id === personId) {
                                face.person_id = realId;
                            }
                        }
                        // Invalidate people cache to pick up new person
                        AppState.people.invalidate();
                        // Re-render to show correct person card
                        renderFacesGrid();
                    }
                    // Note: Backend may trigger async reassessment to find similar faces.
                    // This runs in the background - users see newly matched faces on next load.
                } else {
                    // API failed - revert optimistic update
                    console.error('Identify API failed:', result?.error);
                    // Reload to get correct state
                    needsRefresh = true;
                    loadAllFaces();
                    App.showError(`Failed to identify ${faceIds.length > 1 ? 'faces' : 'face'}.`);
                }
            })
            .catch(error => {
                // Re-enable subscription renders
                suppressBroadcastRender = false;

                console.error('Failed to identify faces:', error);
                // Reload to get correct state
                needsRefresh = true;
                loadAllFaces();
                App.showError(`Failed to identify ${faceIds.length > 1 ? 'faces' : 'face'}.`);
            });

        return true;  // Optimistically return success
    }

    /**
     * Handle selection change in pick-preferred mode.
     */
    function handlePickPreferredSelectionChanged(selectedIds) {
        // Nothing special to do here
    }

    /**
     * Handle face activation in pick-preferred mode (Enter/double-click).
     * Opens fullscreen view for the corresponding image.
     */
    function handlePickPreferredFaceActivated(faceId) {
        const face = pickPreferredFaces.find(f => f.id === faceId);
        if (face && face.image_id) {
            App.showFullscreen(face.image_id);
            setTaggingMode(true);
        }
    }

    /**
     * Handle rename button click in pick-preferred mode.
     */
    async function handleRenamePersonClick() {
        if (!pickPreferredPersonId || !pickPreferredPersonName) return;

        const newName = await App.prompt('Rename Person', 'Enter new name:', pickPreferredPersonName);
        if (!newName || newName.trim() === '' || newName.trim() === pickPreferredPersonName) return;

        const trimmedName = newName.trim();

        try {
            // Check for collision with existing person via AppState.people
            await AppState.people.load();
            const existingPeople = AppState.people.getAll();
            const collision = existingPeople.find(p =>
                p.name.toLowerCase() === trimmedName.toLowerCase() && p.id !== pickPreferredPersonId
            );

            if (collision) {
                App.showError(`A person named "${trimmedName}" already exists.`);
                return;
            }

            // Update the person name via AppState.people.rename()
            await AppState.people.rename(pickPreferredPersonId, trimmedName);

            // Update local state
            pickPreferredPersonName = trimmedName;

            // Update faces in pickPreferredFaces
            for (const face of pickPreferredFaces) {
                face.person_name = trimmedName;
            }

            // Update faces in allFaces (so grid shows correct name after exiting)
            for (const face of allFaces) {
                if (face.person_id === pickPreferredPersonId) {
                    face.person_name = trimmedName;
                }
            }

            // Update header display
            const titleH3 = facesGrid.querySelector('.faces-pick-preferred-header h3');
            if (titleH3) {
                const faceCount = pickPreferredFaces.length;
                const countText = faceCount === 1 ? '1 image' : `${faceCount} images`;
                titleH3.innerHTML = `${App.escapeHtml(trimmedName)} <span class="face-count">(${countText})</span>`;
            }
        } catch (error) {
            console.error('Failed to rename person:', error);
            App.showError('Failed to rename person.');
        }
    }

    /**
     * Handle threshold slider change.
     * @param {string} percentValue - Threshold as percentage string (60-99)
     */
    async function handleThresholdChange(percentValue) {
        if (!pickPreferredPersonId) return;

        const threshold = parseInt(percentValue, 10) / 100;

        try {
            // Use AppState.people.setThreshold() - returns {success, data}
            const result = await AppState.people.setThreshold(pickPreferredPersonId, threshold);

            if (result && result.success) {
                pickPreferredPersonThreshold = threshold;

                const data = result.data || {};

                // Check if person was deleted (all faces ejected)
                if (data.deleted) {
                    App.showError(`All faces ejected - person deleted`);
                    exitPickPreferredMode();
                    AppState.people.invalidate();
                    loadAllFaces();
                    return;
                }

                // If faces changed (ejected or potentially added), reload the view
                if (data.faces_changed) {
                    const ejectedCount = (data.ejected_face_ids || []).length;
                    if (ejectedCount > 0) {
                        const msg = ejectedCount === 1
                            ? '1 face no longer meets threshold'
                            : `${ejectedCount} faces no longer meet threshold`;
                        App.showError(msg);
                    }

                    // Reload faces for this person via AppState
                    try {
                        const faces = await AppState.faces.fetchForPerson(pickPreferredPersonId);
                        pickPreferredFaces = faces || [];
                        displayedFaces = pickPreferredFaces;

                        // Clear any pending reload flag (we're handling the update ourselves)
                        reloadPending = false;

                        // Clear selection
                        if (facesSelection) {
                            facesSelection.clear();
                        }

                        // Update header
                        const titleH3 = facesGrid.querySelector('.faces-pick-preferred-header h3');
                        if (titleH3) {
                            const faceCount = pickPreferredFaces.length;
                            const countText = faceCount === 1 ? '1 image' : `${faceCount} images`;
                            titleH3.innerHTML = `${App.escapeHtml(pickPreferredPersonName)} <span class="face-count">(${countText})</span>`;
                        }

                        // Re-render grid
                        if (pickPreferredGrid) {
                            pickPreferredGrid.render();
                        }
                    } catch (e) {
                        console.error('Failed to reload faces:', e);
                    }

                    // Invalidate caches
                    AppState.people.invalidate();
                }

                // Note: Backend may trigger async reassessment after threshold change.
                // This runs in the background - users see changes on next load or re-entry.
            }
        } catch (error) {
            console.error('Failed to update threshold:', error);
            App.showError('Failed to update threshold.');
        }
    }


    /**
     * Handle threshold reset button click.
     * @param {HTMLInputElement} slider - The slider element
     * @param {HTMLElement} valueDisplay - The value display element
     */
    async function handleThresholdReset(slider, valueDisplay) {
        if (!pickPreferredPersonId) return;

        try {
            // Use AppState.people.setThreshold() with null to reset
            const result = await AppState.people.setThreshold(pickPreferredPersonId, null);

            if (result && result.success) {
                pickPreferredPersonThreshold = null;
                slider.value = '80';  // Reset slider to default position
                valueDisplay.textContent = 'default';
            }
        } catch (error) {
            console.error('Failed to reset threshold:', error);
            App.showError('Failed to reset threshold.');
        }
    }

    /**
     * Handle delete request in pick-preferred mode.
     *
     * KEY DIFFERENCE FROM NORMAL MODE:
     * In normal mode, delete = suppress (mark as false positive, hide forever).
     * In pick-preferred mode, delete = unassign (return to unknown pool).
     *
     * This allows correcting misidentifications without losing the face data.
     * The unassigned faces will reappear in the unknown section and can be
     * re-identified to a different person.
     *
     * EDGE CASE: If all faces are unassigned, exits pick-preferred mode and
     * triggers full reload (person may have been deleted by backend).
     *
     * @param {Array<string>} faceIds - Selected face IDs to unassign
     */
    async function handlePickPreferredDeleteRequested(faceIds) {
        if (!faceIds || faceIds.length === 0) return;

        const count = faceIds.length;
        const message = count === 1
            ? 'Remove this face from the person? It will return to the unknown pool.'
            : `Remove ${count} faces from the person? They will return to the unknown pool.`;

        const confirmed = await App.confirm('Unassign Faces', message);
        if (!confirmed) return;

        // Suppress subscription-triggered re-renders during optimistic update
        suppressBroadcastRender = true;

        // Optimistic update - update local state immediately
        const faceIdSet = new Set(faceIds);

        // Update faces in allFaces (return to unknown pool)
        for (const face of allFaces) {
            if (faceIdSet.has(face.id)) {
                face.person_id = null;
                face.person_name = null;
            }
        }

        // Update local pick-preferred state
        pickPreferredFaces = pickPreferredFaces.filter(f => !faceIdSet.has(f.id));

        // Prevent duplicate reload from selection change handler
        reloadPending = false;
        if (facesSelection) {
            facesSelection.clear();
        }

        if (pickPreferredFaces.length === 0) {
            // No faces left - exit mode and reload (person may be deleted by backend)
            exitPickPreferredMode();
            AppState.people.invalidate();
            loadAllFaces();  // Reload to get faces back in unknown pool
        } else {
            // Re-render with remaining faces
            displayedFaces = pickPreferredFaces;
            pickPreferredGrid.render();
        }

        // Fire API in background
        AppState.faces.unassign(faceIds)
            .then(() => {
                suppressBroadcastRender = false;
            })
            .catch(error => {
                suppressBroadcastRender = false;
                console.error('Failed to unassign faces:', error);
                App.showError('Failed to unassign faces');
                // On failure, reload to get correct state
                needsRefresh = true;
                loadAllFaces();
            });
    }

    /**
     * Initialize GridSelection for faces screen.
     * Works with the VirtualGrid for unknown faces.
     */
    function initFacesSelection() {
        if (!unknownFacesGrid || typeof GridSelection === 'undefined') return;

        facesSelection = GridSelection.create({
            grid: unknownFacesGrid,
            getItems: () => displayedFaces,
            getItemId: (face) => face.id,
            itemSelector: '.face-card',
            selectedClass: 'selected',
            onSelectionChanged: handleFacesSelectionChanged,
            onItemActivated: handleFaceActivated,
            onDeleteRequested: handleFacesDeleteRequested,
            enableKeyboard: true,
            enableDragBox: true,
            enableLongPress: true
        });
    }

    /**
     * Handle selection change in faces grid.
     *
     * KEY BEHAVIOR: Triggers deferred reload when selection clears.
     *
     * When background reassessment finds matches while user has an active
     * selection, we can't reload immediately (would disrupt their work).
     * Instead, reloadPending is set. When selection clears, this handler
     * triggers the deferred reload.
     *
     * IMPORTANT: Operations that handle their own reload (batch identify,
     * suppress, etc.) must set reloadPending=false BEFORE clearing selection,
     * otherwise this handler will trigger a duplicate reload.
     *
     * @param {Array<string>} selectedIds - Selected face IDs
     */
    function handleFacesSelectionChanged(selectedIds) {
        // Only trigger deferred reload in normal mode - pick-preferred handles its own
        if (selectedIds.length === 0 && reloadPending && viewMode === 'all') {
            reloadPending = false;
            AppState.people.invalidate();
            loadAllFaces();
        }
    }

    /**
     * Handle face activation (Enter key or double-click on unknown face).
     *
     * Opens fullscreen viewer on the face's source image with tagging mode
     * enabled. This allows quick navigation to see the face in context.
     *
     * Note: Known person cards have separate double-click handling that
     * enters pick-preferred mode instead.
     *
     * @param {string} faceId - Activated face ID
     */
    function handleFaceActivated(faceId) {
        // Ignore if user is editing a name input (double-click selects text)
        const activeEl = document.activeElement;
        if (activeEl && (activeEl.classList.contains('face-card-input') ||
            activeEl.closest('.face-card-input'))) {
            return;
        }

        const face = displayedFaces.find(f => f.id === faceId);
        if (face && face.image_id) {
            App.showFullscreen(face.image_id);
            setTaggingMode(true);
        }
    }

    /**
     * Remove unknown faces from display WITHOUT full API reload.
     *
     * PURPOSE: Provides smooth UX for suppress/identify operations by updating
     * local state and re-rendering, preserving scroll position and search query.
     *
     * WHEN TO USE: After successfully suppressing or identifying unknown faces.
     * These operations remove faces from the unknown section, and we have all
     * the info needed to update locally without hitting the API again.
     *
     * WHEN NOT TO USE: Known face operations that might affect person state
     * (deletion, preferred face changes) - use needsRefresh=true instead.
     *
     * ORDER OF OPERATIONS (critical for avoiding race conditions):
     * 1. Set reloadPending=false BEFORE clearing selection
     *    (prevents handleFacesSelectionChanged from triggering duplicate reload)
     * 2. Clear selection
     * 3. Invalidate AppState.people (face counts may have changed)
     * 4. Update allFaces and displayedFaces arrays
     * 5. Update header count
     * 6. Re-render grid (or set needsRerender if container hidden)
     *
     * @param {Array<string>|string} faceIds - Face ID(s) to remove
     */
    function removeUnknownFacesLocally(faceIds) {
        const ids = new Set(Array.isArray(faceIds) ? faceIds : [faceIds]);

        // CRITICAL: Set flag BEFORE clearing selection to prevent race
        reloadPending = false;

        if (facesSelection) {
            facesSelection.clear();
        }

        // Invalidate people cache (face counts changed)
        AppState.people.invalidate();

        // Update local data arrays
        allFaces = allFaces.filter(f => !ids.has(f.id));
        displayedFaces = displayedFaces.filter(f => !ids.has(f.id));

        // Update section header count
        const countEl = facesGrid?.querySelector('.faces-section.unknown .faces-section-count');
        if (countEl) {
            countEl.textContent = `(${displayedFaces.length})`;
        }

        // Re-render VirtualGrid (preserves scroll and search)
        // But only if container is visible - otherwise mark for re-render on return
        if (unknownFacesGrid) {
            const container = facesGrid?.querySelector('.faces-unknown-container');
            if (container && container.offsetWidth > 0) {
                unknownFacesGrid.render();
            } else {
                // Local data is already updated - just need re-render when visible
                needsRerender = true;
            }
        }
    }

    /**
     * Suppress a single face (mark as false positive) without confirmation.
     * Uses optimistic update - removes from UI immediately, API call is background.
     * @param {string} faceId - Face ID to suppress
     */
    function suppressSingleFace(faceId) {
        // Suppress subscription-triggered re-renders during optimistic update
        suppressBroadcastRender = true;

        // Optimistic update - remove from UI immediately
        removeUnknownFacesLocally(faceId);

        // Fire API in background
        AppState.faces.suppress(faceId)
            .then(() => {
                suppressBroadcastRender = false;
            })
            .catch(error => {
                suppressBroadcastRender = false;
                console.error(`Failed to suppress face ${faceId}:`, error);
                // On failure, reload to get correct state
                needsRefresh = true;
                loadAllFaces();
            });
    }

    /**
     * Handle delete request for selected faces.
     * Suppresses faces (marks as false positives) rather than deleting images.
     * Uses optimistic update - removes from UI immediately after confirmation.
     * @param {Array<string>} faceIds - Selected face IDs
     */
    async function handleFacesDeleteRequested(faceIds) {
        if (!faceIds || faceIds.length === 0) return;

        const count = faceIds.length;
        const message = count === 1
            ? 'Mark this face as a false positive? It will be hidden but the image will not be deleted.'
            : `Mark ${count} faces as false positives? They will be hidden but the images will not be deleted.`;

        const confirmed = await App.confirm('Suppress Faces', message);
        if (!confirmed) return;

        // Suppress subscription-triggered re-renders during optimistic update
        suppressBroadcastRender = true;

        // Optimistic update - remove from UI immediately
        removeUnknownFacesLocally(faceIds);

        // Fire API in background
        AppState.faces.suppress(faceIds)
            .then(() => {
                suppressBroadcastRender = false;
            })
            .catch(error => {
                suppressBroadcastRender = false;
                console.error('Failed to suppress faces:', error);
                // On failure, reload to get correct state
                needsRefresh = true;
                loadAllFaces();
                App.showError('Failed to suppress faces.');
            });
    }

    /**
     * Register the faces screen module with App.
     *
     * LIFECYCLE HOOKS:
     *
     * onEnter: Called when navigating TO faces screen.
     *   - If needsRefresh: Full API reload (data changed externally)
     *   - If needsRerender: Just re-render grid (local data already updated)
     *   - Otherwise: Restore scroll position, rebind event handlers
     *
     * onLeave: Called when navigating AWAY from faces screen.
     *   - Saves scroll position for restoration on return
     *   - Unbinds VirtualGrid (stops scroll listeners, thumbnail loading)
     *   - Unbinds GridSelection (stops keyboard/mouse handlers)
     *   - Does NOT destroy data (allFaces, knownPeople retained)
     *
     * WHY BIND/UNBIND: VirtualGrid has scroll listeners that continue firing
     * if not unbound. When screen is hidden (e.g., fullscreen overlay),
     * scroll events with zero container dimensions cause issues.
     * GridSelection has keyboard handlers that could interfere with other screens.
     */
    function registerFacesModule() {
        App.registerModule('faces', {
            onEnter() {
                if (needsRefresh) {
                    // External change requires full reload from API
                    loadAllFaces();
                } else {
                    // Ensure people cache is loaded for autocomplete
                    // (fire-and-forget - will complete before user types)
                    AppState.people.load();

                    // Handle pick-preferred mode
                    if (viewMode === 'pick-preferred' && pickPreferredGrid) {
                        // Restore scroll position
                        const container = pickPreferredGrid._config?.container || facesGrid;
                        if (container) {
                            container.scrollTop = savedScrollTop;
                        }
                        // Rebind grid and selection
                        pickPreferredGrid.bind();
                        if (facesSelection) {
                            facesSelection.bind();
                        }
                        return;
                    }

                    // Normal mode: Restore scroll position to unknown container
                    const unknownContainer = facesGrid?.querySelector('.faces-unknown-container');
                    if (unknownContainer) {
                        unknownContainer.scrollTop = savedScrollTop;
                    } else if (facesGrid) {
                        facesGrid.scrollTop = savedScrollTop;
                    }

                    // Rebind VirtualGrid (scroll listeners, thumbnail loading)
                    if (unknownFacesGrid) {
                        unknownFacesGrid.bind();
                        // Handle deferred re-render (data changed while hidden)
                        if (needsRerender) {
                            unknownFacesGrid.render();
                            needsRerender = false;
                        }
                    }

                    // Rebind selection (keyboard/mouse handlers)
                    if (facesSelection) {
                        facesSelection.bind();
                    }
                }
            },

            onLeave() {
                // Handle pick-preferred mode
                if (viewMode === 'pick-preferred' && pickPreferredGrid) {
                    // Save scroll position
                    const container = pickPreferredGrid._config?.container || facesGrid;
                    if (container) {
                        savedScrollTop = container.scrollTop;
                    }
                    // Unbind grid and selection
                    pickPreferredGrid.unbind();
                    if (facesSelection) {
                        facesSelection.unbind();
                    }
                    return;
                }

                // Normal mode: Preserve scroll position for return
                const unknownContainer = facesGrid?.querySelector('.faces-unknown-container');
                if (unknownContainer) {
                    savedScrollTop = unknownContainer.scrollTop;
                } else if (facesGrid) {
                    savedScrollTop = facesGrid.scrollTop;
                }

                // Unbind VirtualGrid (critical - stops scroll events on hidden container)
                if (unknownFacesGrid) {
                    unknownFacesGrid.unbind();
                }
                // Unbind selection handlers
                if (facesSelection) {
                    facesSelection.unbind();
                }
                // Clear search state
                unknownFacesSearchQuery = '';
                // Clear pending reload flag
                reloadPending = false;
            },
            markNeedsRefresh() {
                needsRefresh = true;
            }
        });
    }

    /**
     * Set the thumbnail size for faces screen.
     * @param {number} size - Size in pixels
     */
    function setFacesThumbnailSize(size) {
        facesThumbnailSize = Math.max(60, Math.min(200, size));
        if (facesGrid) {
            facesGrid.style.setProperty('--thumb-size', `${facesThumbnailSize}px`);
        }
        updateThumbnailSizeButtons();

        // Refresh VirtualGrid to recalculate layout
        if (typeof ThumbnailLoader !== 'undefined') {
            ThumbnailLoader.clear();
        }
        if (unknownFacesGrid) {
            unknownFacesGrid.refresh();
        }
        if (pickPreferredGrid) {
            pickPreferredGrid.refresh();
        }
    }

    /**
     * Update thumbnail size button states.
     */
    function updateThumbnailSizeButtons() {
        if (btnFacesThumbSmaller) {
            btnFacesThumbSmaller.disabled = facesThumbnailSize <= 60;
        }
        if (btnFacesThumbLarger) {
            btnFacesThumbLarger.disabled = facesThumbnailSize >= 200;
        }
    }

    /**
     * Update sort direction icon.
     */
    function updateSortDirectionIcon() {
        if (btnFacesSortDirection) {
            const icon = btnFacesSortDirection.querySelector('.material-symbols-outlined');
            if (icon) {
                icon.textContent = sortAscending ? 'arrow_downward' : 'arrow_upward';
            }
        }
    }

    // =========================================================================
    // TAGGING MODE
    // =========================================================================

    /**
     * Toggle face tagging mode on/off.
     */
    function toggleTaggingMode() {
        setTaggingMode(!taggingMode);
    }

    /**
     * Set face tagging mode to a specific state.
     * @param {boolean} enabled - Whether to enable tagging mode
     */
    function setTaggingMode(enabled) {
        if (!faceDetectionEnabled) return;

        taggingMode = enabled;

        // Update button state
        if (btnFaceTagging) {
            btnFaceTagging.classList.toggle('active', taggingMode);
        }

        // Update overlay visibility
        if (faceOverlay) {
            faceOverlay.hidden = !taggingMode;
        }

        // Refresh faces if enabling
        if (taggingMode && App.getScreen() === 'fullscreen') {
            const imageId = App.getCurrentImageId();
            if (imageId) {
                loadFacesForImage(imageId);
            }
        } else if (!taggingMode) {
            // Clear overlay when disabling
            clearFaceOverlay();
        }
    }

    /**
     * Check if tagging mode is currently active.
     * @returns {boolean}
     */
    function isTaggingModeActive() {
        return taggingMode && faceDetectionEnabled;
    }

    // =========================================================================
    // FACES SCREEN - LOADING AND RENDERING
    // =========================================================================

    /**
     * Execute search with current input value.
     * Called on blur or Enter key.
     * @param {HTMLInputElement} input - The search input element
     */
    function executeSearch(input) {
        const query = input.value.trim();
        if (query !== unknownFacesSearchQuery) {
            unknownFacesSearchQuery = query;
            searchUnknownFaces(query);
        }
    }

    /**
     * Search unknown faces by semantic similarity (OpenCLIP embeddings).
     *
     * SEARCH MODE (query non-empty):
     * - Backend returns ONLY unknown faces matching the query, sorted by similarity
     * - Known faces are preserved from current allFaces (not refetched)
     * - displayedFaces is set to search results only
     *
     * DEFAULT MODE (query empty):
     * - Backend returns all faces with group-based sorting
     * - displayedFaces filtered to unknown faces only
     *
     * STATE PRESERVATION:
     * - Saves/restores scroll position around API call
     * - Restores search input value (in case DOM recreated during render)
     * - Uses unknownFacesSearchQuery to track current search state
     *
     * @param {string} query - Search query (empty string resets to default)
     */
    async function searchUnknownFaces(query) {
        // Preserve scroll position for restoration after render
        const unknownContainer = facesGrid?.querySelector('.faces-unknown-container');
        const scrollTopBefore = unknownContainer ? unknownContainer.scrollTop : 0;

        try {
            showFacesLoading(query ? 'Searching faces…' : 'Loading faces…');

            // Use AppState.faces.search() for semantic face search
            const faces = await AppState.faces.search(query);

            if (query) {
                // Search mode: merge results with existing known faces
                const knownFaces = allFaces.filter(f => f.person_id);
                allFaces = [...knownFaces, ...faces];
                displayedFaces = faces;
            } else {
                // Default mode: full replacement
                allFaces = faces;
                displayedFaces = faces.filter(f => !f.person_id);
            }

            // Re-render just the unknown faces grid
            if (unknownFacesGrid) {
                unknownFacesGrid.render();
            }

            // Update count in header
            const countEl = facesGrid?.querySelector('.faces-section-count');
            if (countEl) {
                countEl.textContent = `(${displayedFaces.length})`;
            }

            // Restore search input value (in case DOM was recreated)
            const searchInput = facesGrid?.querySelector('.faces-search-input');
            if (searchInput && searchInput.value !== unknownFacesSearchQuery) {
                searchInput.value = unknownFacesSearchQuery;
            }

            // Restore scroll position after render
            const newUnknownContainer = facesGrid?.querySelector('.faces-unknown-container');
            if (newUnknownContainer && scrollTopBefore > 0) {
                newUnknownContainer.scrollTop = scrollTopBefore;
            }
        } catch (error) {
            console.error('Failed to search faces:', error);
            App.showError('Failed to search faces.');
        } finally {
            hideFacesLoading();
        }
    }

    /**
     * Load all faces from the API.
     */
    async function loadAllFaces() {
        if (isLoading) return;
        isLoading = true;

        // Save scroll position before reload
        const unknownContainer = facesGrid?.querySelector('.faces-unknown-container');
        const scrollTopBefore = unknownContainer ? unknownContainer.scrollTop : 0;

        // Unbind selection during reload
        if (facesSelection) {
            facesSelection.unbind();
        }

        // Unbind and destroy existing VirtualGrid BEFORE clearing DOM
        // (otherwise scroll listeners are orphaned when container is removed)
        if (unknownFacesGrid) {
            unknownFacesGrid.unbind();
            unknownFacesGrid.destroy();
            unknownFacesGrid = null;
        }
        if (pickPreferredGrid) {
            pickPreferredGrid.unbind();
            pickPreferredGrid.destroy();
            pickPreferredGrid = null;
        }

        // Show loading state
        if (facesGrid) facesGrid.innerHTML = '';
        if (facesEmpty) facesEmpty.hidden = true;
        if (facesLoading) facesLoading.hidden = false;

        try {
            // Load faces and people in parallel using AppState (cached)
            await Promise.all([
                AppState.faces.load(),
                AppState.people.load()
            ]);
            allFaces = AppState.faces.getAll();
            needsRefresh = false;
            needsRerender = false;
            renderFacesGrid();

            // Restore scroll position after render
            const newUnknownContainer = facesGrid?.querySelector('.faces-unknown-container');
            if (newUnknownContainer && scrollTopBefore > 0) {
                newUnknownContainer.scrollTop = scrollTopBefore;
            }

            // Bind selection after grid is rendered
            if (facesSelection) {
                facesSelection.bind();
            }

            // If there's an active search query, re-run it to filter results
            if (unknownFacesSearchQuery) {
                // Don't await - let it run async to avoid blocking
                searchUnknownFaces(unknownFacesSearchQuery);
            }
        } catch (error) {
            console.error('Failed to load faces:', error);
            App.showError('Failed to load faces.');
            if (facesEmpty) facesEmpty.hidden = false;
        } finally {
            isLoading = false;
            hideFacesLoading();
        }
    }

    /**
     * Mark faces as needing refresh on next screen enter.
     * Called when database changes or faces are modified externally.
     */
    function markFacesNeedsRefresh() {
        needsRefresh = true;
    }

    /**
     * Render the faces grid with known and unknown sections.
     *
     * ARCHITECTURE:
     * - Known section: Static DOM cards (small count, no virtualization needed).
     *   One card per person showing their preferred face thumbnail.
     * - Unknown section: VirtualGrid (can have thousands of faces).
     *   Face cards with name input for identification.
     *
     * DATA FLOW:
     *   allFaces → (filter showOnlyUnknowns) → split known/unknown
     *   knownFaces → buildKnownPeopleList() → knownPeople[] → static DOM
     *   unknownFaces → displayedFaces[] → VirtualGrid → lazy-rendered cards
     *
     * SIDE EFFECTS:
     * - Clears reloadPending and selection
     * - Rebuilds knownPeople[] and invalidates AppState.people cache
     * - Creates new VirtualGrid instance (destroys previous)
     * - Initializes GridSelection for unknown faces
     */
    function renderFacesGrid() {
        if (!facesGrid) return;

        // Clear pending flags - we're doing a complete render
        reloadPending = false;

        // Clear selection (will be re-initialized after grid setup)
        if (facesSelection) {
            facesSelection.clear();
        }

        // Save scroll positions before destroying
        let savedUnknownScroll = 0;
        let savedKnownScroll = 0;
        if (unknownFacesGrid) {
            const container = facesGrid.querySelector('.faces-unknown-container');
            if (container) {
                savedUnknownScroll = container.scrollTop;
            }
        }
        const knownSection = facesGrid.querySelector('.faces-section.known');
        if (knownSection) {
            savedKnownScroll = knownSection.scrollTop;
        }

        // Destroy existing VirtualGrid to avoid orphaned event listeners
        if (unknownFacesGrid) {
            unknownFacesGrid.unbind();
            unknownFacesGrid.destroy();
            unknownFacesGrid = null;
        }

        facesGrid.innerHTML = '';

        // Apply view filter
        let faces = [...allFaces];
        if (showOnlyUnknowns) {
            faces = faces.filter(f => !f.person_id);
        }

        // Partition into known (has person_id) and unknown
        const knownFaces = faces.filter(f => f.person_id);
        const unknownFaces = faces.filter(f => !f.person_id);
        // Note: unknownFaces already sorted by backend (group_size DESC, group_id, timestamp)
        // This clusters similar faces together with largest groups first

        // Update displayedFaces - this is what VirtualGrid and GridSelection use
        displayedFaces = unknownFaces;

        // Build known people list for static section
        knownPeople = buildKnownPeopleList(knownFaces);

        // Invalidate people cache - it will be refreshed on next autocomplete access
        // (AppState.people now manages the cache)
        AppState.people.invalidate();

        // Check for empty state
        if (faces.length === 0) {
            displayedFaces = [];
            knownPeople = [];
            if (facesEmpty) facesEmpty.hidden = false;
            return;
        }
        if (facesEmpty) facesEmpty.hidden = true;

        // Render known faces section (static DOM - one card per person)
        if (knownPeople.length > 0 && !showOnlyUnknowns) {
            const section = createKnownFacesSection(knownPeople);
            // Apply stored height if available
            if (knownSectionHeight) {
                section.style.height = `${knownSectionHeight}px`;
            }
            facesGrid.appendChild(section);

            // Add divider between known and unknown sections (only if both exist)
            if (unknownFaces.length > 0) {
                const divider = createFacesDivider(section);
                facesGrid.appendChild(divider);
            }
        }

        // Render unknown faces section using VirtualGrid
        if (unknownFaces.length > 0) {
            const section = createUnknownFacesSection(unknownFaces.length);
            facesGrid.appendChild(section);

            // Initialize VirtualGrid in the unknown section container
            const unknownContainer = section.querySelector('.faces-unknown-container');
            if (unknownContainer) {
                initUnknownFacesGridInContainer(unknownContainer);
            }
        }

        // Initialize and bind selection after grid is set up
        initFacesSelection();
        if (facesSelection) {
            facesSelection.bind();
        }

        // Restore scroll positions after grid is set up
        // Use requestAnimationFrame to ensure DOM layout is complete
        requestAnimationFrame(() => {
            if (savedUnknownScroll > 0) {
                const container = facesGrid.querySelector('.faces-unknown-container');
                if (container) {
                    // Setting scrollTop triggers scroll event which updates VirtualGrid
                    container.scrollTop = savedUnknownScroll;
                }
            }
            if (savedKnownScroll > 0) {
                const newKnownSection = facesGrid.querySelector('.faces-section.known');
                if (newKnownSection) {
                    newKnownSection.scrollTop = savedKnownScroll;
                }
            }
        });
    }

    /**
     * Build list of known people from faces.
     * @param {Array<Object>} knownFaces - Faces with person_id
     * @returns {Array<Object>} People with faces array
     */
    function buildKnownPeopleList(knownFaces) {
        const byPerson = new Map();
        for (const face of knownFaces) {
            if (!byPerson.has(face.person_id)) {
                byPerson.set(face.person_id, []);
            }
            byPerson.get(face.person_id).push(face);
        }

        const people = Array.from(byPerson.entries()).map(([personId, personFaces]) => ({
            id: personId,
            name: personFaces[0].person_name,
            faces: personFaces,
            preferredFace: personFaces.find(f => f.is_preferred) || personFaces[0],
        }));

        // Sort by name
        people.sort((a, b) => {
            const cmp = a.name.localeCompare(b.name);
            return sortAscending ? cmp : -cmp;
        });

        return people;
    }

    /**
     * Create the known faces section (static DOM).
     * @param {Array<Object>} people - List of people with faces
     * @returns {HTMLElement}
     */
    function createKnownFacesSection(people) {
        const section = document.createElement('div');
        section.className = 'faces-section known';

        const header = document.createElement('div');
        header.className = 'faces-section-header';

        const titleEl = document.createElement('h3');
        titleEl.className = 'faces-section-title known';
        titleEl.textContent = 'Known';
        header.appendChild(titleEl);

        const countEl = document.createElement('span');
        countEl.className = 'faces-section-count';
        countEl.textContent = `(${people.length})`;
        header.appendChild(countEl);

        section.appendChild(header);

        const grid = document.createElement('div');
        grid.className = 'faces-section-grid';

        for (const person of people) {
            const card = createPersonCard(person);
            grid.appendChild(card);
        }

        section.appendChild(grid);
        return section;
    }

    /**
     * Create a draggable divider between known and unknown sections.
     * @param {HTMLElement} knownSection - The known faces section to resize
     * @returns {HTMLElement}
     */
    function createFacesDivider(knownSection) {
        const divider = document.createElement('div');
        divider.className = 'faces-divider';

        let isDragging = false;
        let startY = 0;
        let startHeight = 0;

        const onMouseMove = (e) => {
            if (!isDragging) return;
            e.preventDefault();

            const deltaY = e.clientY - startY;
            const newHeight = Math.max(100, Math.min(startHeight + deltaY, window.innerHeight * 0.7));

            knownSection.style.height = `${newHeight}px`;
        };

        const onMouseUp = (e) => {
            if (!isDragging) return;
            isDragging = false;
            divider.classList.remove('dragging');
            document.body.style.cursor = '';

            // Persist the new height
            const rect = knownSection.getBoundingClientRect();
            knownSectionHeight = Math.round(rect.height);
            try {
                localStorage.setItem('faces-known-height', String(knownSectionHeight));
            } catch (e) { /* ignore */ }

            // Refresh VirtualGrid after resize
            if (unknownFacesGrid) {
                unknownFacesGrid.refresh();
            }

            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };

        divider.addEventListener('mousedown', (e) => {
            e.preventDefault();
            isDragging = true;
            startY = e.clientY;
            startHeight = knownSection.getBoundingClientRect().height;
            divider.classList.add('dragging');
            document.body.style.cursor = 'ns-resize';

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });

        // Double-click to reset to auto height
        divider.addEventListener('dblclick', (e) => {
            e.preventDefault();
            knownSection.style.height = '';
            knownSectionHeight = null;
            try {
                localStorage.removeItem('faces-known-height');
            } catch (e) { /* ignore */ }

            if (unknownFacesGrid) {
                unknownFacesGrid.refresh();
            }
        });

        return divider;
    }

    /**
     * Create the unknown faces section with VirtualGrid container.
     * @param {number} count - Number of unknown faces
     * @returns {HTMLElement}
     */
    function createUnknownFacesSection(count) {
        const section = document.createElement('div');
        section.className = 'faces-section unknown';

        const header = document.createElement('div');
        header.className = 'faces-section-header';

        const titleEl = document.createElement('h3');
        titleEl.className = 'faces-section-title unknown';
        titleEl.textContent = 'Unknown';
        header.appendChild(titleEl);

        const countEl = document.createElement('span');
        countEl.className = 'faces-section-count';
        countEl.textContent = `(${count})`;
        header.appendChild(countEl);

        // Semantic search input - searches on blur or Enter (not as you type)
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'faces-search-input';
        searchInput.placeholder = 'Search faces...';
        searchInput.value = unknownFacesSearchQuery;

        // Execute search when input loses focus
        searchInput.addEventListener('blur', (e) => {
            executeSearch(e.target);
        });

        // Handle special keys
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                // Blur to trigger search
                e.target.blur();
            } else if (e.key === 'Escape') {
                // Clear and reset
                e.target.value = '';
                unknownFacesSearchQuery = '';
                searchUnknownFaces('');
                e.target.blur();
            }
        });
        header.appendChild(searchInput);

        section.appendChild(header);

        // Container for VirtualGrid
        const container = document.createElement('div');
        container.className = 'faces-unknown-container';
        section.appendChild(container);

        return section;
    }

    /**
     * Initialize VirtualGrid in a specific container.
     * @param {HTMLElement} container - Container element for the grid
     */
    function initUnknownFacesGridInContainer(container) {
        if (typeof VirtualGrid === 'undefined') return;

        unknownFacesGrid = VirtualGrid.create({
            container: container,
            getItems: () => displayedFaces,
            getItemId: (face) => face.id,
            createItem: (face, index, blobUrl) => createUnknownFaceCard(face, blobUrl),
            getThumbnailId: (face) => face.id,
            getThumbnailUrl: (faceId) => `/api/faces/${faceId}/thumbnail`,
            itemSelector: '.face-card',
            gap: 16,
            padding: 0,  // Section already has padding
            getThumbSize: () => facesThumbnailSize,
            getItemHeight: (thumbSize, itemWidth) => {
                // Face card: thumbnail (square) + input height + padding
                return itemWidth + 50;
            },
            onItemCreated: (id, el) => {
                // Sync selection state when item is created
                if (facesSelection && facesSelection.isSelected(id)) {
                    el.classList.add('selected');
                }
            }
        });

        // Render the grid
        unknownFacesGrid.render();
        unknownFacesGrid.bind();
    }

    /**
     * Create an unknown face card for VirtualGrid (with blob URL).
     * @param {Object} face - Face object
     * @param {string} blobUrl - Blob URL for the thumbnail
     * @returns {HTMLElement}
     */
    function createUnknownFaceCard(face, blobUrl) {
        const card = document.createElement('div');
        card.className = 'face-card';
        card.dataset.id = face.id;
        card.draggable = true;

        // Drag start - include this face and all other selected faces
        card.addEventListener('dragstart', (e) => {
            // If this card isn't selected, select only this one
            let faceIds;
            if (facesSelection && facesSelection.isSelected(face.id)) {
                faceIds = facesSelection.getSelected();
            } else {
                faceIds = [face.id];
            }

            e.dataTransfer.setData('application/x-face-ids', JSON.stringify(faceIds));
            e.dataTransfer.effectAllowed = 'move';

            // Mark all dragged cards
            setTimeout(() => {
                faceIds.forEach(id => {
                    const el = facesGrid?.querySelector(`.face-card[data-id="${id}"]`);
                    if (el) el.classList.add('dragging');
                });
            }, 0);
        });

        card.addEventListener('dragend', () => {
            // Remove dragging class from all cards
            facesGrid?.querySelectorAll('.face-card.dragging').forEach(el => {
                el.classList.remove('dragging');
            });
        });

        const thumb = document.createElement('div');
        thumb.className = 'face-card-thumb';

        const img = document.createElement('img');
        img.src = blobUrl;
        img.alt = 'Unknown face';
        thumb.appendChild(img);

        // Suppress button (mark as false positive)
        const suppressBtn = document.createElement('button');
        suppressBtn.className = 'face-card-suppress';
        suppressBtn.title = 'Mark as false positive (not a face)';
        suppressBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';
        suppressBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            await suppressSingleFace(face.id);
        });

        card.appendChild(thumb);
        card.appendChild(suppressBtn);

        // Create editable name input
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-card-input';
        input.placeholder = 'Enter name...';
        input.value = '';
        input.dataset.faceId = face.id;

        // Handle focus - pre-fetch cache for fast autocomplete
        input.addEventListener('focus', () => {
            // AppState.people.load() handles TTL internally
            AppState.people.load();
        });

        // Handle input for autocomplete
        input.addEventListener('input', () => {
            showCardAutocomplete(input, input.value, card);
        });

        // Handle blur to commit (applies to all selected faces)
        input.addEventListener('blur', () => {
            // Delay to allow autocomplete click
            setTimeout(() => {
                const autocomplete = card.querySelector('.face-card-autocomplete');
                if (autocomplete) {
                    autocomplete.remove();
                }
                commitSelectedFacesName(face.id, input.value.trim(), card);
            }, 200);
        });

        // Handle keyboard
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                input.blur();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                input.value = '';
                input.blur();
            }
        });

        card.appendChild(input);

        return card;
    }


    /**
     * Create a person card (for known faces - shows representative face).
     * @param {Object} person - Person object with faces array
     * @returns {HTMLElement}
     */
    function createPersonCard(person) {
        const card = document.createElement('div');
        card.className = 'face-card person-card';
        card.dataset.personId = person.id;
        card.dataset.id = `person-${person.id}`;  // For consistency

        const thumb = document.createElement('div');
        thumb.className = 'face-card-thumb';

        const img = document.createElement('img');
        // Use cache bust timestamp if available (after preferred face changed in this session),
        // or fall back to preferredFace.id from the faces data (handles page reload)
        const bustTime = thumbnailCacheBust.get(person.id);
        const cacheKey = bustTime || (person.preferredFace && person.preferredFace.id) || '';
        img.src = cacheKey
            ? `/api/people/${person.id}/thumbnail?t=${cacheKey}`
            : `/api/people/${person.id}/thumbnail`;
        img.alt = person.name;
        img.loading = 'lazy';
        thumb.appendChild(img);

        card.appendChild(thumb);

        // Add face count badge if multiple faces (outside thumb to avoid circle clipping)
        if (person.faces.length > 1) {
            const badge = document.createElement('div');
            badge.className = 'face-card-badge';
            badge.innerHTML = `<span class="material-symbols-outlined">star</span>`;
            badge.title = `${person.faces.length} faces`;
            card.appendChild(badge);
        }

        const name = document.createElement('div');
        name.className = 'face-card-name';
        name.textContent = person.name;
        card.appendChild(name);

        // Single click to select/deselect for focus button
        card.addEventListener('click', (e) => {
            // Deselect all other person cards
            const allPersonCards = facesGrid.querySelectorAll('.person-card.selected');
            allPersonCards.forEach(c => {
                if (c !== card) c.classList.remove('selected');
            });
            // Toggle selection on this card
            card.classList.toggle('selected');
            updateFocusButtonState();
        });

        // Double-click to enter pick-preferred mode
        card.addEventListener('dblclick', (e) => {
            e.preventDefault();
            enterPickPreferredMode(person.id);
        });

        // Drop target for unknown faces
        card.addEventListener('dragover', (e) => {
            // Check if dragging faces
            if (e.dataTransfer.types.includes('application/x-face-ids')) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                card.classList.add('drop-target');
            }
        });

        card.addEventListener('dragleave', (e) => {
            // Only remove if actually leaving the card (not entering a child)
            if (!card.contains(e.relatedTarget)) {
                card.classList.remove('drop-target');
            }
        });

        card.addEventListener('drop', async (e) => {
            e.preventDefault();
            card.classList.remove('drop-target');

            const data = e.dataTransfer.getData('application/x-face-ids');
            if (!data) return;

            try {
                const faceIds = JSON.parse(data);
                if (!faceIds || faceIds.length === 0) return;

                // Identify all dropped faces as this person
                await identifyFacesAsPerson(faceIds, person.name);
            } catch (err) {
                console.error('Drop failed:', err);
                App.showError('Failed to identify faces');
            }
        });

        return card;
    }


    /**
     * Show autocomplete for face card input.
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     * @param {HTMLElement} card - Parent card element
     */
    function showCardAutocomplete(input, query, card) {
        // Trigger background refresh if cache is stale (don't await - use current data)
        // AppState.people.load() handles TTL internally
        AppState.people.load();

        // Remove existing autocomplete
        const existing = card.querySelector('.face-card-autocomplete');
        if (existing) existing.remove();

        // Use AppState for fuzzy search with proper sorting
        const q = query.trim();
        if (!q) return;

        const matches = AppState.people.search(q);
        if (matches.length === 0) return;

        // Create autocomplete dropdown
        const autocomplete = document.createElement('div');
        autocomplete.className = 'face-card-autocomplete';

        const maxResults = 5;
        for (let i = 0; i < Math.min(matches.length, maxResults); i++) {
            const person = matches[i];
            const item = document.createElement('div');
            item.className = 'face-card-autocomplete-item';

            const img = document.createElement('img');
            // Use cache bust timestamp if available (in case preferred face changed)
            const bustTime = thumbnailCacheBust.get(person.id);
            img.src = bustTime
                ? `/api/people/${person.id}/thumbnail?t=${bustTime}`
                : `/api/people/${person.id}/thumbnail`;
            img.alt = '';
            item.appendChild(img);

            const nameSpan = document.createElement('span');
            nameSpan.textContent = person.name;
            item.appendChild(nameSpan);

            item.addEventListener('mousedown', (e) => {
                e.preventDefault();
                input.value = person.name;
                autocomplete.remove();
                input.blur();
            });

            autocomplete.appendChild(item);
        }

        // Position relative to card
        card.style.position = 'relative';
        card.appendChild(autocomplete);
    }

    /**
     * Show the loading overlay with a custom message.
     * @param {string} message - Message to display
     */
    function showFacesLoading(message) {
        if (facesLoading) {
            const p = facesLoading.querySelector('p');
            if (p) p.textContent = message;
            facesLoading.hidden = false;
        }
        if (facesGrid) facesGrid.style.opacity = '0.5';
    }

    /**
     * Hide the loading overlay and reset to default message.
     */
    function hideFacesLoading() {
        if (facesLoading) {
            facesLoading.hidden = true;
            const p = facesLoading.querySelector('p');
            if (p) p.textContent = 'Loading faces…';
        }
        if (facesGrid) facesGrid.style.opacity = '';
    }

    /**
     * Close all autocomplete dropdowns and clear any pending timers.
     */
    function closeAllAutocompletes() {
        const autocompletes = document.querySelectorAll('.face-card-autocomplete');
        autocompletes.forEach(ac => ac.remove());
    }

    /**
     * Commit a name change for selected faces.
     * If multiple faces are selected, applies the name to all of them.
     * The face where the user typed becomes the "preferred" face for that person.
     * Uses batch API for efficiency and triggers async re-assessment of unknown faces.
     *
     * @param {string} typedFaceId - Face ID where user typed the name
     * @param {string} name - Name to assign
     * @param {HTMLElement} card - Card element where user typed
     */
    async function commitSelectedFacesName(typedFaceId, name, card) {
        if (!name) return;

        // Close autocomplete for better UX
        closeAllAutocompletes();

        // Get selected faces, or just the typed face if none selected
        let faceIds = facesSelection ? facesSelection.getSelected() : [];

        // If the typed face isn't in the selection, or no selection, just use the typed face
        if (faceIds.length === 0 || !faceIds.includes(typedFaceId)) {
            faceIds = [typedFaceId];
        }

        // Use shared identification function
        await identifyFacesAsPerson(faceIds, name, { preferredFaceId: typedFaceId });
    }


    /**
     * Commit a name change for a single face card (legacy/internal use).
     * @param {string} faceId - Face ID
     * @param {string} name - Name to assign
     * @param {HTMLElement} card - Card element
     */
    async function commitFaceCardName(faceId, name, card) {
        if (!name) return;

        try {
            await AppState.faces.identify([faceId], name);
            // Local state updated via AppState.faces.onChanged subscription
            // Invalidate people cache and reload for full consistency
            AppState.people.invalidate();
            loadAllFaces();
        } catch (error) {
            console.error('Failed to identify face:', error);
            App.showError('Failed to identify face.');
        }
    }

    // =========================================================================
    // SCREEN CHANGE HANDLERS
    // =========================================================================

    /**
     * Handle screen change events.
     * @param {string} screen - New screen name
     */
    function handleScreenChange(screen) {
        if (screen === 'fullscreen' && isTaggingModeActive()) {
            const imageId = App.getCurrentImageId();
            if (imageId) {
                loadFacesForImage(imageId);
            }
        } else {
            clearFaceOverlay();
        }
    }

    /**
     * Handle fullscreen image change.
     * @param {string} imageId - New image ID
     */
    function handleFullscreenImageChange(imageId) {
        if (isTaggingModeActive() && imageId) {
            loadFacesForImage(imageId);
        }
    }

    /**
     * Handle fullscreen transform change (zoom/pan).
     * Updates face overlay to match image transform.
     * @param {number} zoom - Zoom level
     * @param {number} panX - Horizontal pan
     * @param {number} panY - Vertical pan
     */
    function handleFullscreenTransformChange(zoom, panX, panY) {
        if (!faceOverlay || !isTaggingModeActive()) return;

        // Apply same transform as image
        faceOverlay.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;
    }

    // =========================================================================
    // FACE LOADING AND RENDERING
    // =========================================================================

    /**
     * Load faces for an image from the API.
     * @param {string} imageId - Image ID
     */
    async function loadFacesForImage(imageId) {
        if (!faceOverlay) return;

        try {
            const faces = await AppState.faces.fetchForImage(imageId);
            renderFaces(faces || []);
        } catch (error) {
            console.error('Failed to load faces:', error);
            clearFaceOverlay();
        }
    }

    /**
     * Clear the face overlay.
     */
    function clearFaceOverlay() {
        if (faceOverlay) {
            faceOverlay.innerHTML = '';
        }
        focusedInput = null;
        closeAutocomplete();
    }

    /**
     * Render faces on the overlay.
     * @param {Array<Object>} faces - Array of face objects
     */
    function renderFaces(faces) {
        if (!faceOverlay || !fullscreenImage || !fullscreenContainer) {
            return;
        }

        clearFaceOverlay();

        // Wait for image to be loaded to get dimensions
        if (!fullscreenImage.complete) {
            fullscreenImage.addEventListener('load', () => renderFaces(faces), { once: true });
            return;
        }

        // Calculate the base (untransformed) image dimensions and position
        // This mirrors the logic in fullscreen.js _constrainPan()
        const containerRect = fullscreenContainer.getBoundingClientRect();
        const imgNaturalWidth = fullscreenImage.naturalWidth || containerRect.width;
        const imgNaturalHeight = fullscreenImage.naturalHeight || containerRect.height;

        const containerAspect = containerRect.width / containerRect.height;
        const imgAspect = imgNaturalWidth / imgNaturalHeight;

        let baseWidth, baseHeight;
        if (imgAspect > containerAspect) {
            // Image is wider - fits to width
            baseWidth = containerRect.width;
            baseHeight = containerRect.width / imgAspect;
        } else {
            // Image is taller - fits to height
            baseHeight = containerRect.height;
            baseWidth = containerRect.height * imgAspect;
        }

        // Position the overlay centered in the container (same as image)
        const offsetX = (containerRect.width - baseWidth) / 2;
        const offsetY = (containerRect.height - baseHeight) / 2;

        // Size and position the overlay to match the untransformed image
        faceOverlay.style.position = 'absolute';
        faceOverlay.style.left = `${offsetX}px`;
        faceOverlay.style.top = `${offsetY}px`;
        faceOverlay.style.width = `${baseWidth}px`;
        faceOverlay.style.height = `${baseHeight}px`;
        faceOverlay.style.transformOrigin = 'center';

        // Apply the current transform (get from Fullscreen state)
        const { zoom = 1, panX = 0, panY = 0 } = (typeof Fullscreen !== 'undefined' && Fullscreen.state) || {};
        faceOverlay.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`;

        // Render face boxes - positions relative to overlay (which matches image)
        for (const face of faces) {
            const faceBox = createFaceBox(face, baseWidth, baseHeight);
            faceOverlay.appendChild(faceBox);
        }
    }

    /**
     * Create a face bounding box element.
     * @param {Object} face - Face object from API
     * @param {number} imgWidth - Base image display width
     * @param {number} imgHeight - Base image display height
     * @returns {HTMLElement}
     */
    function createFaceBox(face, imgWidth, imgHeight) {
        const box = document.createElement('div');
        box.className = 'face-box';
        box.dataset.faceId = face.id;

        // Set known/unknown class
        if (face.person_id) {
            box.classList.add('known');
        } else {
            box.classList.add('unknown');
        }

        // Calculate pixel positions from normalized coordinates
        // Positions are relative to the overlay (which matches the image)
        const left = face.box_x * imgWidth;
        const top = face.box_y * imgHeight;
        const width = face.box_w * imgWidth;
        const height = face.box_h * imgHeight;

        box.style.left = `${left}px`;
        box.style.top = `${top}px`;
        box.style.width = `${width}px`;
        box.style.height = `${height}px`;

        // Create delete button
        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'face-delete-btn';
        deleteBtn.title = 'Remove face detection (not a real face)';
        deleteBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';
        deleteBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            suppressFace(face.id, box);
        });
        box.appendChild(deleteBtn);

        // Create label
        const label = createFaceLabel(face, top, imgHeight);
        box.appendChild(label);

        // Click on face box focuses the label input
        box.addEventListener('click', (e) => {
            // Don't handle if clicking on the label itself or delete button
            if (e.target.closest('.face-label') || e.target.closest('.face-delete-btn')) {
                return;
            }

            // For known faces, click the name span to show input (same as clicking label)
            const nameSpan = label.querySelector('.face-name');
            if (nameSpan) {
                nameSpan.click();
                // Focus will happen after showNameInput creates the input
                setTimeout(() => {
                    const input = label.querySelector('.face-input');
                    if (input) input.focus();
                }, 0);
            } else {
                // For unknown faces, just focus the existing input
                const input = label.querySelector('.face-input');
                if (input) input.focus();
            }
        });

        return box;
    }

    /**
     * Create a face label element.
     * @param {Object} face - Face object from API
     * @param {number} boxTop - Top position of face box
     * @param {number} imgHeight - Image height
     * @returns {HTMLElement}
     */
    function createFaceLabel(face, boxTop, imgHeight) {
        const label = document.createElement('div');
        label.className = 'face-label';

        // Position label below or above based on box position
        const spaceBelow = imgHeight - boxTop - (face.box_h * imgHeight);
        if (spaceBelow > 60) {
            label.classList.add('below');
        } else {
            label.classList.add('above');
        }

        if (face.person_id && face.person_name) {
            // Known face - show name that can be clicked to edit
            const nameSpan = document.createElement('span');
            nameSpan.className = 'face-name';
            nameSpan.textContent = face.person_name;
            nameSpan.addEventListener('click', () => {
                showNameInput(label, face);
            });
            label.appendChild(nameSpan);
        } else {
            // Unknown face - show input field
            showNameInput(label, face);
        }

        return label;
    }

    /**
     * Show name input field in a label.
     * @param {HTMLElement} label - Label element
     * @param {Object} face - Face object
     */
    function showNameInput(label, face) {
        // Clear existing content
        label.innerHTML = '';

        // Create input
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-input';
        input.placeholder = 'Enter name...';
        input.value = face.person_name || '';
        input.dataset.faceId = face.id;
        input.dataset.originalName = face.person_name || '';

        // Handle focus - pre-fetch people cache for fast autocomplete
        input.addEventListener('focus', () => {
            focusedInput = input;
            const faceBox = label.closest('.face-box');
            if (faceBox) {
                faceBox.classList.add('focused');
            }
            // Pre-fetch cache if stale (don't await - let it load in background)
            // AppState.people.load() handles TTL internally
            AppState.people.load();
        });

        // Handle blur (commit changes)
        input.addEventListener('blur', () => {
            const faceBox = label.closest('.face-box');
            if (faceBox) {
                faceBox.classList.remove('focused');
            }
            focusedInput = null;

            // Delay to allow autocomplete click to update input value first
            setTimeout(() => {
                closeAutocomplete();

                // Commit the change
                const newName = input.value.trim();
                const originalName = input.dataset.originalName;

                if (newName !== originalName) {
                    commitNameChange(face.id, newName, label, face);
                }
            }, 200);
        });

        // Handle input for autocomplete
        input.addEventListener('input', () => {
            showAutocomplete(input, input.value);
        });

        // Handle keyboard in input
        input.addEventListener('keydown', (e) => {
            handleInputKeyDown(e, input, face, label);
        });

        label.appendChild(input);

        // Focus the input if this is a new unknown face
        if (!face.person_id) {
            // Delay focus to ensure DOM is ready
            setTimeout(() => input.focus(), 50);
        }
    }

    // =========================================================================
    // AUTOCOMPLETE
    // =========================================================================

    /**
     * Show autocomplete dropdown for name input.
     * Uses AppState.people.search() for fuzzy matching and sorting.
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     */
    function showAutocomplete(input, query) {
        // Use AppState for fuzzy search with proper sorting
        const matches = AppState.people.search(query);

        // Close if no matches
        if (matches.length === 0) {
            closeAutocomplete();
            return;
        }

        // Create or update autocomplete
        if (!activeAutocomplete) {
            activeAutocomplete = document.createElement('div');
            activeAutocomplete.className = 'face-autocomplete';
            input.parentElement.appendChild(activeAutocomplete);
        }

        // Limit displayed results
        const maxResults = 5;
        const displayedMatches = matches.slice(0, maxResults);

        activeAutocomplete.innerHTML = '';
        autocompleteSelectedIndex = -1;

        for (let i = 0; i < displayedMatches.length; i++) {
            const person = displayedMatches[i];
            const item = document.createElement('div');
            item.className = 'face-autocomplete-item';
            item.dataset.index = i;
            item.dataset.personId = person.id;
            item.dataset.name = person.name;

            // Add thumbnail (with cache busting if preferred face changed)
            const img = document.createElement('img');
            const bustTime = thumbnailCacheBust.get(person.id);
            img.src = bustTime
                ? `/api/people/${person.id}/thumbnail?t=${bustTime}`
                : `/api/people/${person.id}/thumbnail`;
            img.alt = '';
            img.onerror = () => { img.style.display = 'none'; };
            item.appendChild(img);

            // Add name
            const nameSpan = document.createElement('span');
            nameSpan.className = 'name';
            nameSpan.textContent = person.name;
            item.appendChild(nameSpan);

            // Handle click - set name and blur to commit
            item.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                input.value = person.name;
                closeAutocomplete();
                input.blur();
            });

            activeAutocomplete.appendChild(item);
        }

        // Show "..." if more results
        if (matches.length > maxResults) {
            const more = document.createElement('div');
            more.className = 'face-autocomplete-more';
            more.textContent = `...${matches.length - maxResults} more`;
            activeAutocomplete.appendChild(more);
        }
    }

    /**
     * Close the autocomplete dropdown.
     */
    function closeAutocomplete() {
        if (activeAutocomplete) {
            activeAutocomplete.remove();
            activeAutocomplete = null;
        }
        autocompleteSelectedIndex = -1;
    }

    /**
     * Refresh the people cache from API.
     * @deprecated Use AppState.people.reload() directly
     */
    async function refreshPeopleCache() {
        await AppState.people.reload();
    }

    // =========================================================================
    // KEYBOARD HANDLING
    // =========================================================================

    /**
     * Handle global keydown events.
     * @param {KeyboardEvent} e
     */
    function handleKeyDown(e) {
        if (!isTaggingModeActive()) return;
        if (App.getScreen() !== 'fullscreen') return;

        // Tab to cycle through unknown face inputs
        if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
            const unknownInputs = getUnknownFaceInputs();
            if (unknownInputs.length > 0) {
                e.preventDefault();

                const currentIndex = focusedInput
                    ? unknownInputs.indexOf(focusedInput)
                    : -1;

                const nextIndex = e.shiftKey
                    ? (currentIndex <= 0 ? unknownInputs.length - 1 : currentIndex - 1)
                    : (currentIndex + 1) % unknownInputs.length;

                unknownInputs[nextIndex].focus();
            }
        }
    }

    /**
     * Handle keydown events in name input.
     * @param {KeyboardEvent} e
     * @param {HTMLInputElement} input
     * @param {Object} face
     * @param {HTMLElement} label
     */
    function handleInputKeyDown(e, input, face, label) {
        // Escape to cancel editing
        if (e.key === 'Escape') {
            e.preventDefault();
            input.value = input.dataset.originalName || '';
            input.blur();
            closeAutocomplete();
            return;
        }

        // Enter to commit
        if (e.key === 'Enter') {
            e.preventDefault();

            // If autocomplete is open and item is selected, use that
            if (activeAutocomplete && autocompleteSelectedIndex >= 0) {
                const selected = activeAutocomplete.querySelector('.selected');
                if (selected) {
                    input.value = selected.dataset.name;
                }
            }

            input.blur();
            return;
        }

        // Arrow keys for autocomplete navigation
        if (activeAutocomplete) {
            const items = activeAutocomplete.querySelectorAll('.face-autocomplete-item');

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                autocompleteSelectedIndex = Math.min(autocompleteSelectedIndex + 1, items.length - 1);
                updateAutocompleteSelection(items);
                return;
            }

            if (e.key === 'ArrowUp') {
                e.preventDefault();
                autocompleteSelectedIndex = Math.max(autocompleteSelectedIndex - 1, -1);
                updateAutocompleteSelection(items);
                return;
            }
        }
    }

    /**
     * Update visual selection in autocomplete.
     * @param {NodeList} items
     */
    function updateAutocompleteSelection(items) {
        items.forEach((item, i) => {
            item.classList.toggle('selected', i === autocompleteSelectedIndex);
        });
    }

    /**
     * Get all input fields for unknown faces.
     * @returns {HTMLInputElement[]}
     */
    function getUnknownFaceInputs() {
        if (!faceOverlay) return [];
        return Array.from(faceOverlay.querySelectorAll('.face-box.unknown .face-input'));
    }

    // =========================================================================
    // API CALLS
    // =========================================================================

    /**
     * Commit a name change for a face.
     * @param {string} faceId - Face ID
     * @param {string} name - New name (empty to unidentify)
     * @param {HTMLElement} label - Label element
     * @param {Object} face - Original face object
     */
    async function commitNameChange(faceId, name, label, face) {
        try {
            if (name) {
                // Use shared API helper
                const result = await callIdentifyBatchApi([faceId], name, faceId);

                if (result && result.success) {
                    // Update face object and re-render label
                    face.person_id = result.data.person.id;
                    face.person_name = result.data.person.name;

                    // Update box class
                    const faceBox = label.closest('.face-box');
                    if (faceBox) {
                        faceBox.classList.remove('unknown');
                        faceBox.classList.add('known');
                    }

                    // Show name span instead of input
                    label.innerHTML = '';
                    const nameSpan = document.createElement('span');
                    nameSpan.className = 'face-name';
                    nameSpan.textContent = face.person_name;
                    nameSpan.addEventListener('click', () => {
                        showNameInput(label, face);
                    });
                    label.appendChild(nameSpan);

                    // Invalidate people cache and mark faces screen for refresh
                    AppState.people.invalidate();
                    needsRefresh = true;
                }
            } else if (face.person_id) {
                // Unidentify face using AppState
                await AppState.faces.unassign(faceId);

                face.person_id = null;
                face.person_name = null;

                // Update box class
                const faceBox = label.closest('.face-box');
                if (faceBox) {
                    faceBox.classList.remove('known');
                    faceBox.classList.add('unknown');
                }

                // Invalidate people cache and mark faces screen for refresh
                AppState.people.invalidate();
                needsRefresh = true;
            }
        } catch (error) {
            console.error('Failed to update face:', error);
            App.showError('Failed to update face.');
            // Revert input to original value
            const input = label.querySelector('.face-input');
            if (input) {
                input.value = input.dataset.originalName || '';
            }
        }
    }

    /**
     * Suppress a face (mark as false positive) from fullscreen tagging mode.
     *
     * CONTEXT: Called when user clicks the X button on a face bounding box
     * in fullscreen view. The face box is removed immediately (optimistic UI)
     * and then marked as suppressed in the database (won't appear in future
     * face lists).
     *
     * BACKEND BEHAVIOR (app.py suppress_face_endpoint):
     * - If this was a person's preferred face, auto-selects new preferred
     * - If this was the person's only face, deletes the person
     *
     * KNOWN VS UNKNOWN FACE HANDLING:
     * - Known face: Set needsRefresh (person state may have changed), bust
     *   thumbnail cache (preferred may have changed), update local arrays.
     * - Unknown face: Use removeUnknownFacesLocally() which preserves scroll
     *   position and search state, only re-renders if container visible.
     *
     * WHY DIFFERENT: Known faces require full refresh because person may be
     * deleted or preferred changed. Unknown faces can update locally since
     * no person-level state is affected.
     *
     * @param {string} faceId - Face ID to suppress
     * @param {HTMLElement} faceBox - Face box DOM element (removed from overlay)
     */
    async function suppressFace(faceId, faceBox) {
        // Determine if this was a known or unknown face BEFORE suppressing
        const face = allFaces.find(f => f.id === faceId);
        const wasKnownFace = face && face.person_id;
        const personId = wasKnownFace ? face.person_id : null;

        // Remove from fullscreen overlay immediately (optimistic UI)
        faceBox.remove();

        try {
            await AppState.faces.suppress(faceId);
            // Local state updated via AppState.faces.onChanged subscription

            if (wasKnownFace) {
                // Known face - need full refresh (person may be deleted/changed)
                needsRefresh = true;
                AppState.people.invalidate();

                // Bust cache in case this was the preferred face
                thumbnailCacheBust.set(personId, Date.now());

                // Update local arrays for partial consistency
                allFaces = allFaces.filter(f => f.id !== faceId);
                displayedFaces = displayedFaces.filter(f => f.id !== faceId);
            } else {
                // Unknown face - can update locally (preserves scroll/search)
                removeUnknownFacesLocally(faceId);
            }
        } catch (error) {
            console.error('Failed to suppress face:', error);
            App.showError('Failed to remove face.');
        }
    }

    // =========================================================================
    // MODULE REGISTRATION
    // =========================================================================

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Export public API
    window.Faces = {
        isTaggingModeActive,
        setTaggingMode,
        toggleTaggingMode,
        loadFacesForImage,
        clearFaceOverlay,
        refreshPeopleCache,
        loadAllFaces,
        renderFacesGrid,
        markNeedsRefresh: markFacesNeedsRefresh,
    };

})();
