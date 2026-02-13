/**
 * Face tagging module for Photonarium.
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
 *   AppState.faces → (filter by search) → displayedFaces[] → VirtualGrid
 *   AppState.faces → (group by person) → knownPeople[] → static DOM cards
 *
 * STATE MANAGEMENT:
 *   All face/people data is stored in AppState (single source of truth).
 *   GUI reads from AppState, mutations go through AppState APIs which handle
 *   optimistic updates, cache management, and broadcasts.
 *
 * CACHES (see detailed docs in cache section below):
 *   - AppState.faces: Face data (managed by AppState)
 *   - AppState.people: Autocomplete suggestions (TTL-based, managed by AppState)
 *   - AppState.people._thumbnailBust: Forces browser to refetch changed person thumbnails
 *   - knownPeople: Computed on render from AppState.faces
 *   - displayedFaces: Computed on render, or search results (transient)
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

    /** @type {string|null} Image ID currently being rendered in overlay (prevents stale renders) */
    let currentOverlayImageId = null;

    /** @type {Array|null} Current faces displayed in overlay (for re-rendering on resize) */
    let currentOverlayFaces = null;

    /** @type {function|null} Bound resize handler for cleanup */
    let resizeHandler = null;

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
    // DATA ARCHITECTURE:
    //
    //   AppState.faces    - Single source of truth for all face data.
    //                       Each face has: id, image_id, person_id, person_name,
    //                       bbox, is_preferred, image_timestamp, etc.
    //
    //   displayedFaces[]  - Computed/transient array for VirtualGrid.
    //                       In normal mode: unknown faces from AppState.faces.
    //                       In search mode: search results (sorted by relevance).
    //
    //   knownPeople[]     - Computed on each render from AppState.faces.
    //                       Grouped by person: {id, name, faces[], preferredFace}
    //
    //   pickPreferredFaces[] - In pick-preferred mode only. All faces for one
    //                          person, loaded via AppState.faces.getForPerson().
    //
    // MUTATIONS:
    //   All mutations (identify, suppress, unassign) go through AppState APIs.
    //   AppState handles optimistic updates, backend calls, and broadcasts.
    //   GUI subscribes to AppState changes and re-renders automatically.

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

    // Debug logging for face identification flow
    const FACES_DEBUG = false;
    function facesLog(...args) {
        if (FACES_DEBUG) console.log('[FacesFlow]', ...args);
    }

    // Expose debug function to console
    window._facesDebug = {
        getState: () => ({
            displayedFacesCount: displayedFaces.length,
            appStateFacesCount: AppState.faces.getAll().length,
            appStateFacesUnknown: AppState.faces.getAll().filter(f => !f.person_id && !f.suppressed).length,
            appStateEpoch: window._appStateDebug?.getEpoch() || 'N/A',
        }),
        forceRefresh: () => {
            renderFacesGrid();
            console.log('[FacesFlow] Force refreshed from AppState');
        }
    };

    /** @type {number} Saved scroll position for unknown faces container */
    let savedScrollTop = 0;

    // Faces screen DOM references
    /** @type {HTMLElement} */
    let facesGrid;

    /** @type {HTMLElement} */
    let facesEmpty;


    // Persistent view containers (created once, toggled via hidden attribute)
    /** @type {HTMLElement} Normal view wrapper (people + divider + unknown) */
    let normalView = null;

    /** @type {HTMLElement} Pick-preferred view wrapper */
    let pickerView = null;

    /** @type {HTMLElement} Known people section container */
    let peopleSection = null;

    /** @type {HTMLElement} Unknown faces section container */
    let unknownSection = null;

    /** @type {HTMLElement} Unknown faces scroll container (inside unknownSection) */
    let unknownContainer = null;

    // Picker view persistent elements
    /** @type {HTMLElement} Picker header (contains name, count, threshold) */
    let pickerHeader = null;

    /** @type {HTMLElement} Picker grid container (for VirtualGrid) */
    let pickerGridContainer = null;

    // Picker header element references (for updating without rebuilding)
    /** @type {HTMLElement} Title element showing person name and face count */
    let pickerTitleEl = null;

    /** @type {HTMLInputElement} Threshold slider input */
    let pickerThresholdSlider = null;

    /** @type {HTMLElement} Threshold value display span */
    let pickerThresholdValue = null;

    /** @type {HTMLElement} Loading indicator in picker */
    let pickerLoadingEl = null;

    /** @type {Object|null} GridSelection for picker mode (separate from facesSelection) */
    let pickerSelection = null;

    /** @type {HTMLButtonElement} */
    let btnFacesThumbSmaller;

    /** @type {HTMLButtonElement} */
    let btnFacesThumbLarger;

    /** @type {HTMLButtonElement} */
    let btnFacesOnlyUnknowns;

    /** @type {HTMLButtonElement} */
    let btnFacesFocusPerson;

    /** @type {HTMLButtonElement} */
    let btnPickerHideLocked;

    /** @type {HTMLButtonElement} */
    let btnFacesSortDirection;

    /** @type {Object|null} GridSelection instance for faces screen */
    let facesSelection = null;

    /** @type {Array<Object>} Currently displayed unknown faces (for selection) */
    let displayedFaces = [];

    /**
     * Pending input state to restore after grid refresh.
     * VirtualGrid creates cards asynchronously (after thumbnails load),
     * so we can't restore immediately after render(). Instead, we store
     * the state here and restore it in onItemCreated when the card appears.
     * Cleared when: user focuses another input, navigates away, or timeout expires.
     * @type {Object|null}
     */
    let _pendingInputRestore = null;

    /**
     * Clear pending input restore state.
     * Called when user interacts elsewhere or navigates away.
     */
    function clearPendingInputRestore() {
        _pendingInputRestore = null;
    }

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

    /** @type {Function|null} Fullscreen event subscription cleanup function */
    let fullscreenUnsub = null;

    /**
     * @type {boolean} Suppress face overlay reload during identify operation.
     * DESIGN: Known deviation - avoids redundant DOM updates when identify() already
     * updates the overlay directly. Should ideally use debouncing instead, but this
     * works correctly. (see design-audit.md 2.1)
     */
    let suppressOverlayReload = false;

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

    /** @type {boolean} True after picker data fetch completes (distinguishes loading from empty) */
    let pickerDataLoaded = false;

    /** @type {number|null} Per-person recognition threshold (null = use global default) */
    let pickPreferredPersonThreshold = null;

    /** @type {string} Current semantic search query for filtering unknown faces */
    let unknownFacesSearchQuery = '';

    /** @type {boolean} Whether to show locked faces in pick-preferred mode (default: true) */
    let showLockedFaces = true;

    // =========================================================================
    // FACES REFRESH - Centralized refresh handling with state preservation
    // =========================================================================
    //
    // This object coordinates refresh operations across the three grid areas:
    // 1. People grid (known faces section) - static DOM, no VirtualGrid
    // 2. Unknown faces grid - VirtualGrid with selection
    // 3. Pick-preferred grid - VirtualGrid with selection (separate mode)
    //
    // Key responsibilities:
    // - Capture/restore input state (user typing in face label)
    // - Capture/restore scroll position
    // - Prune selection when items are removed by backend
    // - Track active mode to only refresh relevant grids

    const FacesRefresh = {
        // --- Input State Utilities ---
        // Used to preserve user input when a refresh happens mid-typing

        /**
         * Capture input state from a grid container.
         * Returns null if no input is focused.
         * Also saves to _pendingInputRestore for async restoration via onItemCreated.
         */
        captureInputState(gridContainer) {
            if (!gridContainer) {
                _pendingInputRestore = null;
                return null;
            }

            const activeInput = gridContainer.querySelector('input:focus, textarea:focus');
            if (!activeInput) {
                _pendingInputRestore = null;
                return null;
            }

            // Use data-id which is standard across all face cards
            const faceCard = activeInput.closest('[data-id]');
            if (!faceCard) {
                _pendingInputRestore = null;
                return null;
            }

            const state = {
                faceId: faceCard.dataset.id,
                inputSelector: activeInput.tagName.toLowerCase() +
                    (activeInput.className ? '.' + activeInput.className.split(' ').join('.') : ''),
                value: activeInput.value,
                selectionStart: activeInput.selectionStart,
                selectionEnd: activeInput.selectionEnd,
                container: gridContainer,  // Track which container this is for
            };

            // Save for async restoration when card is created
            _pendingInputRestore = state;

            // Clear pending state if user focuses a different input
            // (they've moved on, don't steal focus back)
            const onFocusElsewhere = (e) => {
                if (_pendingInputRestore !== state) {
                    // State already cleared or changed, remove listener
                    document.removeEventListener('focusin', onFocusElsewhere, true);
                    return;
                }
                // If focus went to an input/textarea that's NOT in the target card, clear
                const focusedEl = e.target;
                if (focusedEl.matches('input, textarea')) {
                    const focusedCard = focusedEl.closest('[data-id]');
                    if (!focusedCard || focusedCard.dataset.id !== state.faceId) {
                        // User focused a different input - abandon restore
                        _pendingInputRestore = null;
                        document.removeEventListener('focusin', onFocusElsewhere, true);
                    }
                }
            };
            document.addEventListener('focusin', onFocusElsewhere, true);

            return state;
        },

        /**
         * Restore input state after refresh.
         * Only restores if the face card already exists in the grid.
         * For VirtualGrid (async card creation), this may not find the card yet;
         * in that case, onItemCreated will handle restoration via _pendingInputRestore.
         */
        restoreInputState(gridContainer, state) {
            if (!gridContainer || !state) return;

            // Find the face card by data-id (if it already exists)
            const faceCard = gridContainer.querySelector(`[data-id="${state.faceId}"]`);
            if (!faceCard) return;  // Card not created yet (or face was removed)

            // Find the input
            const input = faceCard.querySelector(state.inputSelector);
            if (!input) return;

            // Restore value and selection
            input.value = state.value;
            input.focus();
            if (input.setSelectionRange) {
                input.setSelectionRange(state.selectionStart, state.selectionEnd);
            }

            // Clear pending state since we've restored
            if (_pendingInputRestore?.faceId === state.faceId) {
                _pendingInputRestore = null;
            }

            // Ensure visible
            faceCard.scrollIntoView({ block: 'nearest' });
        },

        /**
         * Check if a newly-created card needs input state restored.
         * Called from VirtualGrid's onItemCreated callback.
         * @param {string} id - Face ID of the created card
         * @param {HTMLElement} el - The card element
         * @param {HTMLElement} container - The grid container
         */
        maybeRestoreInput(id, el, container) {
            // Check if this is the card we're waiting for
            if (!_pendingInputRestore) return;
            if (_pendingInputRestore.faceId !== id) return;
            if (_pendingInputRestore.container !== container) return;

            const state = _pendingInputRestore;
            _pendingInputRestore = null;

            // Find the input in the new card
            const input = el.querySelector(state.inputSelector);
            if (!input) return;

            // Restore value and selection
            input.value = state.value;
            input.focus();
            if (input.setSelectionRange) {
                input.setSelectionRange(state.selectionStart, state.selectionEnd);
            }

            // Ensure visible
            el.scrollIntoView({ block: 'nearest' });

            // Reopen autocomplete if there was text (same as typing a character)
            if (state.value) {
                showCardAutocomplete(input, state.value, el);
            }
        },

        // --- Refresh Handlers ---
        // With persistent containers, scroll positions are preserved automatically.
        // We just need to capture/restore input state and prune selections.

        /**
         * Handle AppState.faces change event.
         * Routes to appropriate handler based on current view mode.
         */
        onFacesChanged() {
            if (viewMode === 'pick-preferred' && pickPreferredPersonId) {
                // Picker shows faces for a specific person
                // Use cache (already updated via optimistic update) instead of re-fetching
                // to avoid race condition where GET returns stale data before POST completes
                const inputState = this.captureInputState(pickerGridContainer);

                pickPreferredFaces = AppState.faces.getForPerson(pickPreferredPersonId);

                // Debug: Log manually_tagged values to diagnose lock state issues
                console.debug('[onFacesChanged] From cache:',
                    pickPreferredFaces.map(f => ({
                        id: f.id.slice(0, 8),
                        mt: f.manually_tagged,
                        type: typeof f.manually_tagged
                    })));
                if (pickerSelection) pickerSelection.pruneToValidIds();
                renderPickerContent();
                requestAnimationFrame(() => this.restoreInputState(pickerGridContainer, inputState));
            } else {
                // Normal mode - update both sections
                const inputState = this.captureInputState(unknownContainer);
                renderFacesGrid();
                requestAnimationFrame(() => this.restoreInputState(unknownContainer, inputState));
            }
        },

        /**
         * Handle AppState.people change event.
         * People grid needs refresh when persons change.
         */
        onPeopleChanged() {
            if (viewMode === 'pick-preferred' && pickPreferredPersonId) {
                // Update picker header if person data changed (e.g., name change)
                const people = AppState.people.getAll();
                const person = people?.find(p => p.id === pickPreferredPersonId);
                if (person && person.name !== pickPreferredPersonName) {
                    pickPreferredPersonName = person.name;
                    renderPickerContent();
                }
            } else {
                // Normal mode - just update people section
                updatePeopleSection();
            }
        },

        /**
         * Get the current search text for unknown faces.
         */
        getSearchText() {
            return unknownFacesSearchQuery;
        },

        /**
         * Set the search text for unknown faces.
         */
        setSearchText(text) {
            unknownFacesSearchQuery = text;
        },
    };

    // =========================================================================
    // PERSISTENT CONTAINER SETUP
    // =========================================================================

    /**
     * Ensure persistent view containers exist.
     *
     * Creates two wrapper divs inside facesGrid:
     * - normalView: contains people section, divider, unknown section
     * - pickerView: contains pick-preferred header and grid
     *
     * These are created once and never destroyed. Mode switching just toggles
     * the hidden attribute. This preserves scroll positions, selection state,
     * and VirtualGrid instances across mode transitions.
     */
    function ensurePersistentContainers() {
        if (!facesGrid) return;
        if (normalView && pickerView) return; // Already created

        // Clear any existing content
        facesGrid.innerHTML = '';

        // Create normal view wrapper
        normalView = document.createElement('div');
        normalView.id = 'faces-normal-view';
        normalView.className = 'faces-view';

        // Create people section (static DOM for known faces)
        peopleSection = document.createElement('div');
        peopleSection.className = 'faces-section known';
        peopleSection.setAttribute('tabindex', '0');
        // Focus this section when clicked (for keyboard event routing)
        peopleSection.addEventListener('click', (e) => {
            // Don't steal focus from inputs
            if (e.target.matches('input, textarea')) return;
            if (peopleSection.contains(document.activeElement) &&
                document.activeElement.matches('input, textarea')) return;
            if (document.activeElement !== peopleSection) {
                peopleSection.focus({ preventScroll: true });
            }
        });
        if (knownSectionHeight) {
            peopleSection.style.height = `${knownSectionHeight}px`;
        }
        normalView.appendChild(peopleSection);

        // Create divider (will be shown/hidden based on content)
        const divider = document.createElement('div');
        divider.className = 'faces-divider';
        divider.innerHTML = '<div class="faces-divider-handle"></div>';
        normalView.appendChild(divider);

        // Set up divider drag behavior
        setupDividerDrag(divider, peopleSection);

        // Create unknown section wrapper
        unknownSection = document.createElement('div');
        unknownSection.className = 'faces-section unknown';
        unknownSection.setAttribute('tabindex', '0');
        // Focus this section when clicked (for keyboard event routing)
        unknownSection.addEventListener('click', (e) => {
            // Don't steal focus from inputs (including during text selection where
            // mouseup lands elsewhere but an input inside this section has focus)
            if (e.target.matches('input, textarea')) return;
            if (unknownSection.contains(document.activeElement) &&
                document.activeElement.matches('input, textarea')) return;
            if (document.activeElement !== unknownSection) {
                unknownSection.focus({ preventScroll: true });
            }
        });

        // Create unknown section header with search
        const unknownHeader = document.createElement('div');
        unknownHeader.className = 'faces-section-header';
        unknownHeader.innerHTML = '<h3>Unknown Faces</h3>';

        // Add search input
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.className = 'faces-search-input';
        searchInput.placeholder = 'Search faces...';
        searchInput.title = "Semantic search: describe what you're looking for. Use -word to exclude. More terms = better results (e.g. 'happy smiling -glasses -sunglasses').";
        searchInput.value = unknownFacesSearchQuery;
        searchInput.addEventListener('blur', (e) => executeSearch(e.target));
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                executeSearch(e.target);
                e.target.blur();
            } else if (e.key === 'Escape') {
                e.target.value = '';
                unknownFacesSearchQuery = '';
                searchUnknownFaces('');
                e.target.blur();
            }
        });
        unknownHeader.appendChild(searchInput);
        unknownSection.appendChild(unknownHeader);

        // Create unknown faces scroll container
        unknownContainer = document.createElement('div');
        unknownContainer.className = 'faces-unknown-container';
        unknownSection.appendChild(unknownContainer);

        normalView.appendChild(unknownSection);
        facesGrid.appendChild(normalView);

        // Create picker view wrapper (hidden by default)
        pickerView = document.createElement('div');
        pickerView.id = 'faces-picker-view';
        pickerView.className = 'faces-view';
        pickerView.hidden = true;
        pickerView.setAttribute('tabindex', '0');
        // Focus this view when clicked (for keyboard event routing)
        pickerView.addEventListener('click', (e) => {
            // Don't steal focus from inputs (including during text selection)
            if (e.target.matches('input, textarea')) return;
            if (pickerView.contains(document.activeElement) &&
                document.activeElement.matches('input, textarea')) return;
            if (document.activeElement !== pickerView) {
                pickerView.focus({ preventScroll: true });
            }
        });

        // Handle Escape: clear selection first, then exit mode if already empty
        pickerView.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                // Don't handle if an input is focused (let it handle Escape)
                if (document.activeElement?.tagName === 'INPUT') return;

                // Check if selection is empty BEFORE GridSelection handles it
                const hasSelection = pickerSelection && pickerSelection.getSelected().length > 0;
                if (!hasSelection) {
                    // Selection already empty - exit pick-preferred mode
                    e.preventDefault();
                    e.stopPropagation();
                    exitPickPreferredMode();
                }
                // If there's a selection, let GridSelection clear it (don't prevent default)
            }
        });

        // Create picker header with full structure (persistent)
        pickerHeader = document.createElement('div');
        pickerHeader.className = 'faces-pick-preferred-header';

        const titleRow = document.createElement('div');
        titleRow.className = 'faces-pick-preferred-title-row';

        // Left side: name, count, rename button
        const titleLeft = document.createElement('div');
        titleLeft.className = 'faces-pick-preferred-title-left';

        pickerTitleEl = document.createElement('h3');
        pickerTitleEl.innerHTML = '<span class="face-count"></span>';

        const renameBtn = document.createElement('button');
        renameBtn.className = 'faces-rename-btn';
        renameBtn.title = 'Rename person';
        renameBtn.innerHTML = '<span class="material-symbols-outlined">edit</span>';
        renameBtn.addEventListener('click', handleRenamePersonClick);

        titleLeft.appendChild(pickerTitleEl);
        titleLeft.appendChild(renameBtn);
        titleRow.appendChild(titleLeft);

        // Right side: threshold slider
        const thresholdControl = document.createElement('div');
        thresholdControl.className = 'faces-threshold-control';

        const thresholdLabel = document.createElement('label');
        thresholdLabel.textContent = 'Match threshold:';
        thresholdLabel.htmlFor = 'threshold-slider';

        pickerThresholdSlider = document.createElement('input');
        pickerThresholdSlider.type = 'range';
        pickerThresholdSlider.id = 'threshold-slider';
        pickerThresholdSlider.className = 'faces-threshold-slider';
        pickerThresholdSlider.min = '60';
        pickerThresholdSlider.max = '99';
        pickerThresholdSlider.step = '1';
        pickerThresholdSlider.value = '80';

        pickerThresholdValue = document.createElement('span');
        pickerThresholdValue.className = 'faces-threshold-value';
        pickerThresholdValue.textContent = 'default';

        // Update display on input
        pickerThresholdSlider.addEventListener('input', () => {
            pickerThresholdValue.textContent = `${pickerThresholdSlider.value}%`;
        });

        // Save on change (mouse release)
        pickerThresholdSlider.addEventListener('change', () => handleThresholdChange(pickerThresholdSlider.value));

        // Hover preview tooltip - shows value at cursor position before clicking
        const hoverTooltip = document.createElement('div');
        hoverTooltip.className = 'faces-threshold-hover';

        pickerThresholdSlider.addEventListener('mouseenter', () => {
            hoverTooltip.style.opacity = '1';
        });

        pickerThresholdSlider.addEventListener('mouseleave', () => {
            hoverTooltip.style.opacity = '0';
        });

        pickerThresholdSlider.addEventListener('mousemove', (e) => {
            const rect = pickerThresholdSlider.getBoundingClientRect();
            const min = parseInt(pickerThresholdSlider.min, 10);
            const max = parseInt(pickerThresholdSlider.max, 10);
            // Calculate value at cursor position (account for thumb width ~16px)
            const thumbHalf = 8;
            const trackWidth = rect.width - thumbHalf * 2;
            const x = Math.max(0, Math.min(trackWidth, e.clientX - rect.left - thumbHalf));
            const ratio = x / trackWidth;
            const value = Math.round(min + ratio * (max - min));
            hoverTooltip.textContent = `${value}%`;
            // Position tooltip - above slider, but below if too close to top
            const tooltipHeight = 24;
            const margin = 8;
            let top = rect.top - tooltipHeight - margin;
            if (top < margin) {
                // Position below slider with extra offset to clear cursor
                top = rect.bottom + margin + 16;
            }
            hoverTooltip.style.left = `${e.clientX}px`;
            hoverTooltip.style.top = `${top}px`;
        });

        // Reset to default button
        const resetBtn = document.createElement('button');
        resetBtn.className = 'faces-threshold-reset';
        resetBtn.title = 'Reset to default';
        resetBtn.innerHTML = '<span class="material-symbols-outlined">restart_alt</span>';
        resetBtn.addEventListener('click', () => handleThresholdReset(pickerThresholdSlider, pickerThresholdValue));

        thresholdControl.appendChild(thresholdLabel);
        thresholdControl.appendChild(pickerThresholdSlider);
        thresholdControl.appendChild(pickerThresholdValue);
        thresholdControl.appendChild(resetBtn);
        // Append tooltip to #app so it inherits theme CSS variables
        App.$('app').appendChild(hoverTooltip);
        titleRow.appendChild(thresholdControl);

        pickerHeader.appendChild(titleRow);

        const hint = document.createElement('span');
        hint.className = 'hint';
        hint.textContent = 'Click a star to set as preferred face. Press Delete to unassign faces.';
        pickerHeader.appendChild(hint);

        pickerView.appendChild(pickerHeader);

        // Create loading indicator (sibling of grid container, not inside it)
        // VirtualGrid.render() clears container.innerHTML, so loading must be outside
        pickerLoadingEl = document.createElement('div');
        pickerLoadingEl.className = 'faces-loading-inline';
        pickerLoadingEl.innerHTML = '<div class="loading-spinner"></div><p>Loading faces...</p>';
        pickerLoadingEl.hidden = true;
        pickerView.appendChild(pickerLoadingEl);

        // Create picker grid container (persistent)
        pickerGridContainer = document.createElement('div');
        pickerGridContainer.className = 'faces-pick-preferred-container';
        pickerView.appendChild(pickerGridContainer);

        facesGrid.appendChild(pickerView);

        // Initialize VirtualGrid for unknown faces (created once, persists)
        initUnknownFacesGrid();

        // Initialize GridSelection for unknown faces
        initFacesSelection();

        // Initialize VirtualGrid for picker (created once, persists)
        initPickerGrid();

        // Initialize GridSelection for picker
        initPickerSelection();
    }

    /**
     * Set up divider drag behavior for resizing known section.
     */
    function setupDividerDrag(divider, section) {
        let startY = 0;
        let startHeight = 0;

        const onMouseMove = (e) => {
            const delta = e.clientY - startY;
            const maxHeight = Math.min(window.innerHeight * 0.5, window.innerHeight - 200);
            const newHeight = Math.max(100, Math.min(startHeight + delta, maxHeight));
            section.style.height = `${newHeight}px`;
        };

        const onMouseUp = () => {
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
            // Save the new height
            knownSectionHeight = parseInt(section.style.height, 10);
            try {
                localStorage.setItem('faces-known-height', String(knownSectionHeight));
            } catch (e) { /* ignore */ }
        };

        divider.addEventListener('mousedown', (e) => {
            e.preventDefault();
            startY = e.clientY;
            startHeight = section.offsetHeight;
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }

    /**
     * Initialize the VirtualGrid for unknown faces.
     * Called once when containers are created.
     */
    function initUnknownFacesGrid() {
        if (!unknownContainer) return;
        if (unknownFacesGrid) return; // Already exists

        unknownFacesGrid = VirtualGrid.create({
            container: unknownContainer,
            getItems: () => displayedFaces,
            getItemId: (face) => face.id,
            createItem: (face, index, blobUrl) => createUnknownFaceCard(face, blobUrl),
            getThumbnailId: (face) => face.id,
            getThumbnailUrl: (faceId) => FaceThumbnails.getUrl(faceId),
            itemSelector: '.face-card',
            getThumbSize: () => facesThumbnailSize,
            getItemHeight: (thumbSize, itemWidth) => itemWidth + 50,
            gap: 16,
            padding: 16,
            onItemCreated: (id, el) => {
                // Restore selection state
                if (facesSelection && facesSelection.isSelected(id)) {
                    el.classList.add('selected');
                }
                // Restore input state if this is the card we were typing in
                FacesRefresh.maybeRestoreInput(id, el, unknownContainer);
            }
        });
    }

    /**
     * Initialize GridSelection for unknown faces.
     * Called once when containers are created.
     */
    function initFacesSelection() {
        if (!unknownFacesGrid) return;
        if (facesSelection) return; // Already exists

        facesSelection = GridSelection.create({
            grid: unknownFacesGrid,
            getItems: () => displayedFaces,
            getItemId: (face) => face.id,
            itemSelector: '.face-card',
            selectedClass: 'selected',
            focusContainer: unknownSection,
            onSelectionChanged: handleFacesSelectionChanged,
            onItemActivated: (id) => {
                // Double-click or Enter on face - focus the name input
                const card = unknownContainer?.querySelector(`[data-id="${id}"]`);
                const input = card?.querySelector('.face-card-input');
                if (input) input.focus();
            },
            onDeleteRequested: (ids) => handleDeleteFaces(ids),
        });
    }

    /**
     * Initialize the VirtualGrid for pick-preferred mode.
     * Called once when containers are created.
     */
    function initPickerGrid() {
        if (!pickerGridContainer) return;
        if (pickPreferredGrid) return; // Already exists

        pickPreferredGrid = VirtualGrid.create({
            container: pickerGridContainer,
            getItems: () => {
                if (showLockedFaces) {
                    return pickPreferredFaces;
                }
                // Filter to only show unlocked faces
                const filtered = pickPreferredFaces.filter(f => !f.manually_tagged);
                // Debug: Log filter results
                console.debug('[getItems] showLockedFaces:', showLockedFaces,
                    'total:', pickPreferredFaces.length,
                    'filtered:', filtered.length,
                    'faces:', pickPreferredFaces.map(f => ({ id: f.id.slice(0, 8), mt: f.manually_tagged })));
                return filtered;
            },
            getItemId: (face) => face.id,
            createItem: (face, index, blobUrl) => createPickPreferredFaceCard(face, blobUrl),
            getThumbnailId: (face) => face.id,
            getThumbnailUrl: (faceId) => FaceThumbnails.getUrl(faceId),
            itemSelector: '.face-card',
            gap: 16,
            padding: 16,
            getThumbSize: () => facesThumbnailSize,
            getItemHeight: (thumbSize, itemWidth) => itemWidth + 50,
            onItemCreated: (id, el) => {
                // Restore selection state
                if (pickerSelection && pickerSelection.isSelected(id)) {
                    el.classList.add('selected');
                }
                // Restore input state if this is the card we were typing in
                FacesRefresh.maybeRestoreInput(id, el, pickerGridContainer);
            }
        });
    }

    /**
     * Initialize GridSelection for pick-preferred mode.
     * Called once when containers are created.
     */
    function initPickerSelection() {
        if (!pickPreferredGrid) return;
        if (pickerSelection) return; // Already exists

        pickerSelection = GridSelection.create({
            grid: pickPreferredGrid,
            getItems: () => showLockedFaces
                ? pickPreferredFaces
                : pickPreferredFaces.filter(f => !f.manually_tagged),
            getItemId: (face) => face.id,
            itemSelector: '.face-card',
            selectedClass: 'selected',
            focusContainer: pickerView,
            onSelectionChanged: handlePickPreferredSelectionChanged,
            onItemActivated: handlePickPreferredFaceActivated,
            onDeleteRequested: handlePickPreferredDeleteRequested,
            enableKeyboard: true,
            enableDragBox: true,
            enableLongPress: true
        });
    }

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
        btnFacesThumbSmaller = document.getElementById('btn-faces-thumb-smaller');
        btnFacesThumbLarger = document.getElementById('btn-faces-thumb-larger');
        btnFacesOnlyUnknowns = document.getElementById('btn-faces-only-unknowns');
        btnFacesFocusPerson = document.getElementById('btn-faces-focus-person');
        btnPickerHideLocked = document.getElementById('btn-picker-hide-locked');
        btnFacesSortDirection = document.getElementById('btn-faces-sort-direction');

        // Check if face detection is enabled
        loadFaceDetectionConfig();

        // Set up event listeners
        setupEventListeners();

        // Set up faces screen event listeners
        setupFacesScreenListeners();

        // Listen for screen changes
        App.on('screenChanged', handleScreenChange);

        // Listen for fullscreen overlay events via AppState
        AppState.nav.onChanged((event) => {
            if (event.property === 'fullscreenImageId') {
                const imageId = AppState.nav.getFullscreenImageId();
                if (imageId) {
                    // Add resize listener when fullscreen opens with tagging mode
                    if (isTaggingModeActive() && !resizeHandler) {
                        resizeHandler = handleWindowResize;
                        window.addEventListener('resize', resizeHandler);
                    }
                    handleFullscreenImageChange(imageId);
                }
            } else if (event.property === 'fullscreenClosing') {
                // Remove resize listener when fullscreen closes
                if (resizeHandler) {
                    window.removeEventListener('resize', resizeHandler);
                    resizeHandler = null;
                }
                clearFaceOverlay();
            }
        });
        // Transform changes (zoom/pan) still use App.emit (high-frequency UI updates)
        App.on('fullscreenTransformChanged', handleFullscreenTransformChange);

        // Listen for image changes (e.g., after scan completes, images deleted)
        // New images may have new faces; deleted images remove faces
        AppState.images.onChanged(() => {
            if (App.getScreen() !== 'faces') {
                needsRefresh = true;
            }
            // Note: When faces are detected for new images, AppState.faces.onChanged
            // will fire separately and handle the actual refresh
        });

        // Subscribe to AppState.faces for centralized state management
        // Subscribe to AppState.faces for reactive updates.
        // When faces change (identify, suppress, etc.), re-render the grid.
        AppState.faces.onChanged((event) => {
            facesLog('AppState.faces.onChanged received:', event?.type);

            // If fullscreen is open with tagging mode, reload face overlay
            if (Fullscreen.isOpen() && taggingMode) {
                // Skip reload if we're in the middle of an identify operation
                // (commitNameChange updates the UI directly, no need to re-render)
                if (suppressOverlayReload) {
                    facesLog('  -> Skipping fullscreen reload (suppressOverlayReload)');
                } else {
                    const imageId = Fullscreen.state.currentId;
                    if (imageId) {
                        facesLog('  -> Reloading fullscreen face overlay');
                        loadFacesForImage(imageId);
                    }
                }
            }

            // Skip if we're not on the faces screen
            if (App.getScreen() !== 'faces') {
                facesLog('  -> Skipping: not on faces screen');
                // Mark for re-render when we return to faces screen
                // (cache is already updated via synchronous optimistic updates)
                needsRerender = true;
                return;
            }
            // Skip if data isn't loaded yet
            if (!AppState.faces.isLoaded()) {
                facesLog('  -> Skipping: faces not loaded');
                return;
            }

            facesLog('  -> Rendering from AppState');

            // Render the grid
            requestAnimationFrame(() => {
                if (App.getScreen() === 'faces') {
                    // During initial load, always do full render
                    if (isLoading) {
                        renderFacesGrid();
                    } else {
                        FacesRefresh.onFacesChanged();
                    }
                    checkLoadingComplete();
                }
            });
        });

        // Subscribe to AppState.people for people grid updates
        // Handles: person added/removed, renamed, preferred face changed
        AppState.people.onChanged((event) => {
            // Skip if we're not on the faces screen
            if (App.getScreen() !== 'faces') {
                // Mark for re-render when we return (cache is already updated)
                needsRerender = true;
                return;
            }

            // Render the people section
            requestAnimationFrame(() => {
                if (App.getScreen() === 'faces') {
                    // During initial load, do full render (faces may have loaded from cache)
                    if (isLoading) {
                        renderFacesGrid();
                    } else {
                        FacesRefresh.onPeopleChanged();
                    }
                    checkLoadingComplete();
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

        // Hide locked faces button (for pick-preferred mode)
        if (btnPickerHideLocked) {
            btnPickerHideLocked.addEventListener('click', () => {
                showLockedFaces = !showLockedFaces;
                updateHideLockedButton();
                // Full re-render needed because item count changes
                renderPickerContent();
            });
        }

        // Sort direction button
        if (btnFacesSortDirection) {
            btnFacesSortDirection.addEventListener('click', () => {
                sortAscending = !sortAscending;
                updateSortDirectionIcon();
                renderFacesGrid();
            });
        }

        // Keyboard handler for known people section
        document.addEventListener('keydown', (e) => {
            // Only handle when on faces screen and not in pick-preferred mode
            if (App.getScreen() !== 'faces') return;
            if (viewMode === 'pick-preferred') return;

            // Don't intercept if focus is in an input field
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

            // Only handle keys when people section has focus
            if (document.activeElement !== peopleSection) return;

            const grid = peopleSection?.querySelector('.faces-section-grid');
            if (!grid) return;

            const cards = Array.from(grid.querySelectorAll('.person-card'));
            if (cards.length === 0) return;

            // Find currently selected card
            const selectedCard = grid.querySelector('.person-card.selected');
            const currentIndex = selectedCard ? cards.indexOf(selectedCard) : -1;

            // Calculate items per row for vertical navigation
            const getItemsPerRow = () => {
                if (cards.length < 2) return 1;
                const firstTop = cards[0].getBoundingClientRect().top;
                let count = 1;
                for (let i = 1; i < cards.length; i++) {
                    if (cards[i].getBoundingClientRect().top === firstTop) {
                        count++;
                    } else {
                        break;
                    }
                }
                return count;
            };

            let newIndex = currentIndex;

            switch (e.key) {
                case 'ArrowLeft':
                    e.preventDefault();
                    newIndex = currentIndex > 0 ? currentIndex - 1 : cards.length - 1;
                    break;
                case 'ArrowRight':
                    e.preventDefault();
                    newIndex = currentIndex < cards.length - 1 ? currentIndex + 1 : 0;
                    break;
                case 'ArrowUp': {
                    e.preventDefault();
                    const perRow = getItemsPerRow();
                    newIndex = currentIndex >= perRow ? currentIndex - perRow : currentIndex;
                    break;
                }
                case 'ArrowDown': {
                    e.preventDefault();
                    const perRow = getItemsPerRow();
                    newIndex = currentIndex + perRow < cards.length ? currentIndex + perRow : currentIndex;
                    break;
                }
                case 'Enter':
                    if (selectedCard) {
                        e.preventDefault();
                        enterPickPreferredMode(selectedCard.dataset.personId);
                    }
                    return;
                case 'Escape':
                    e.preventDefault();
                    // Deselect all
                    cards.forEach(c => c.classList.remove('selected'));
                    updateFocusButtonState();
                    return;
                default:
                    return;
            }

            // Update selection
            if (newIndex !== currentIndex && newIndex >= 0 && newIndex < cards.length) {
                cards.forEach(c => c.classList.remove('selected'));
                cards[newIndex].classList.add('selected');
                cards[newIndex].scrollIntoView({ block: 'nearest' });
                updateFocusButtonState();
            } else if (currentIndex === -1 && cards.length > 0) {
                // No selection, select first
                cards[0].classList.add('selected');
                cards[0].scrollIntoView({ block: 'nearest' });
                updateFocusButtonState();
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
     * Update the hide-locked button state.
     * Only visible in pick-preferred mode.
     */
    function updateHideLockedButton() {
        if (!btnPickerHideLocked) return;

        if (viewMode === 'pick-preferred') {
            btnPickerHideLocked.hidden = false;
            btnPickerHideLocked.classList.toggle('active', !showLockedFaces);
            btnPickerHideLocked.title = showLockedFaces
                ? 'Show unlocked only'
                : 'Show all faces';
        } else {
            btnPickerHideLocked.hidden = true;
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
     * - pickPreferredFaces loaded via AppState.faces.getForPerson()
     * - pickPreferredGrid replaces unknownFacesGrid
     *
     * WHY SEPARATE FACES ARRAY: The pick-preferred mode needs faces for a single
     * person, sorted by timestamp with is_preferred flag. This is a focused view
     * different from the general faces grid.
     *
     * @param {string} personId - Person ID to focus on
     */
    function enterPickPreferredMode(personId) {
        // Get name from local cache for immediate header display
        const localPerson = knownPeople.find(p => p.id === personId);
        if (!localPerson) return;

        // Check if switching to a different person
        const isSamePerson = pickPreferredPersonId === personId && viewMode === 'pick-preferred';

        // Set state before switching views
        viewMode = 'pick-preferred';
        pickPreferredPersonId = personId;
        pickPreferredPersonName = localPerson.name;
        pickPreferredPersonThreshold = null;
        pickPreferredFaces = []; // Start empty, will populate when data loads
        pickerDataLoaded = false; // Mark as loading until fetch completes

        // Toggle visibility: hide normal, show picker
        if (normalView) normalView.hidden = true;
        if (pickerView) pickerView.hidden = false;

        // Unbind normal mode selection (keyboard shouldn't affect hidden grid)
        if (facesSelection) facesSelection.unbind();
        if (unknownFacesGrid) unknownFacesGrid.unbind();

        // When switching to a different person, clear selection and reset scroll
        if (!isSamePerson) {
            if (pickerSelection) pickerSelection.clear();
            if (pickerGridContainer) pickerGridContainer.scrollTop = 0;
        }

        // Bind picker grid and selection
        if (pickPreferredGrid) pickPreferredGrid.bind();
        if (pickerSelection) pickerSelection.bind();

        // Render picker content (loading state initially)
        renderPickerContent();
        updateFocusButtonState();
        updateHideLockedButton();

        // Focus picker view for keyboard navigation
        if (pickerView) pickerView.focus({ preventScroll: true });

        // Use cached faces immediately (handles race condition with pending identify)
        // The cache is updated synchronously by identify() before API calls complete
        const cachedFaces = AppState.faces.getForPerson(personId);
        if (cachedFaces.length > 0) {
            pickPreferredFaces = cachedFaces;
            pickerDataLoaded = true;
            renderPickerContent();
        }

        // Fetch authoritative data from backend (for threshold and any server-side changes)
        Promise.all([
            AppState.people.fetchById(personId),       // For recognition_threshold
            AppState.faces.fetchForPerson(personId)    // All faces for this person
        ]).then(([personResult, faces]) => {
            // Only update if still in pick-preferred mode for this person
            if (viewMode !== 'pick-preferred' || pickPreferredPersonId !== personId) return;

            // Update with fetched data (may include faces not yet in cache, or fix any stale data)
            pickPreferredFaces = faces || [];
            pickPreferredPersonThreshold = personResult?.recognition_threshold ?? null;
            pickerDataLoaded = true; // Fetch complete

            // Re-render with actual data
            renderPickerContent();
        }).catch(error => {
            console.error('Failed to load person data:', error);
            pickerDataLoaded = true; // Mark loaded even on error to stop spinner
            // If we have cached data, keep showing it
            if (pickPreferredFaces.length === 0) {
                renderPickerContent(); // Show empty state
            }
        });
    }

    /**
     * Exit pick-preferred mode and return to normal all/unknowns view.
     *
     * Toggles visibility (normal grids keep their scroll positions and selection
     * state) and refreshes the people section to pick up any thumbnail changes.
     */
    function exitPickPreferredMode() {
        // Remember the person we were focused on (for reselecting after exit)
        const lastPersonId = pickPreferredPersonId;

        viewMode = 'all';
        pickPreferredPersonId = null;
        pickPreferredPersonName = null;
        pickPreferredFaces = [];
        pickPreferredPersonThreshold = null;
        pickerDataLoaded = false;
        showLockedFaces = true; // Reset to default when exiting

        // Toggle visibility: hide picker, show normal
        if (pickerView) pickerView.hidden = true;
        if (normalView) normalView.hidden = false;

        // Unbind picker grid and selection
        if (pickPreferredGrid) pickPreferredGrid.unbind();
        if (pickerSelection) pickerSelection.unbind();

        // Rebind normal grids and selection
        if (unknownFacesGrid) unknownFacesGrid.bind();
        if (facesSelection) facesSelection.bind();

        // Full re-render to rebuild knownPeople from updated faces cache
        // (picks up name changes, new/removed people, preferred face changes)
        renderFacesGrid();

        updateFocusButtonState();
        updateHideLockedButton();

        // Focus people section and reselect the person we were viewing
        if (peopleSection) {
            peopleSection.focus({ preventScroll: true });
            if (lastPersonId) {
                const personCard = peopleSection.querySelector(`.person-card[data-person-id="${lastPersonId}"]`);
                if (personCard) {
                    // Deselect all, then select the one we were viewing
                    peopleSection.querySelectorAll('.person-card.selected').forEach(c => c.classList.remove('selected'));
                    personCard.classList.add('selected');
                    personCard.scrollIntoView({ block: 'nearest' });
                    updateFocusButtonState();
                }
            }
        }
    }

    /**
     * Render the pick-preferred mode content inside pickerView.
     * The pickerView container is persistent; this just updates content.
     * Preserves scroll position, selection state, and input focus.
     */
    function renderPickerContent() {
        if (!pickerView || !pickerTitleEl) return;

        // Sync is_preferred on face objects from the person's preferred_face_id.
        // Cached faces (from getForPerson) don't have is_preferred — it's only
        // computed by backend SQL. The people cache always has preferred_face_id
        // (set synchronously by identify → setPreferred), so derive it here.
        if (pickPreferredPersonId && pickPreferredFaces.length > 0) {
            const person = AppState.people._internal.get(pickPreferredPersonId);
            const prefId = person?.preferred_face_id;
            for (const face of pickPreferredFaces) {
                face.is_preferred = (face.id === prefId);
            }
        }

        // Update title with name and count (reflect filtered count when hiding locked)
        const displayedFaceCount = showLockedFaces
            ? pickPreferredFaces.length
            : pickPreferredFaces.filter(f => !f.manually_tagged).length;
        const totalCount = pickPreferredFaces.length;
        const unlockedCount = pickPreferredFaces.filter(f => !f.manually_tagged).length;
        let countText = showLockedFaces
            ? (displayedFaceCount === 1 ? '1 image' : `${displayedFaceCount} images`)
            : `${displayedFaceCount} of ${totalCount} images`;
        if (unlockedCount > 0) {
            countText += `, ${unlockedCount} unlocked`;
        }
        pickerTitleEl.innerHTML = `${App.escapeHtml(pickPreferredPersonName || '')} <span class="face-count">(${countText})</span>`;

        // Update threshold slider/value (preserve focus if user is interacting)
        if (pickerThresholdSlider && document.activeElement !== pickerThresholdSlider) {
            const currentPercent = pickPreferredPersonThreshold !== null
                ? Math.round(pickPreferredPersonThreshold * 100)
                : 80;
            pickerThresholdSlider.value = String(currentPercent);
            if (pickerThresholdValue) {
                pickerThresholdValue.textContent = pickPreferredPersonThreshold !== null
                    ? `${currentPercent}%`
                    : 'default';
            }
        }

        // Show/hide loading state (only show spinner while fetch is in progress)
        const isLoading = !pickerDataLoaded;
        if (pickerLoadingEl) {
            pickerLoadingEl.hidden = !isLoading;
        }

        // Ensure grid and selection are initialized
        if (!pickPreferredGrid) {
            initPickerGrid();
        }
        if (!pickerSelection && pickPreferredGrid) {
            initPickerSelection();
        }

        // Don't render grid if still loading
        if (isLoading) return;

        // Prune any stale selections (e.g., faces that were unassigned)
        if (pickerSelection) {
            pickerSelection.pruneToValidIds();
        }

        // Render the grid (VirtualGrid preserves scroll position)
        if (pickPreferredGrid) {
            pickPreferredGrid.render();
            // Ensure grid is bound
            if (!pickPreferredGrid._bound) {
                pickPreferredGrid.bind();
            }
        }

        // Ensure selection is bound
        if (pickerSelection && !pickerSelection._bound) {
            pickerSelection.bind();
        }
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
        img.title = 'Double-click to open image ' + (face.image_basename || '');
        thumb.appendChild(img);

        // Unassign button (remove face from person)
        const unassignBtn = document.createElement('button');
        unassignBtn.className = 'face-card-unassign';
        unassignBtn.title = 'Unassign from person';
        unassignBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';
        unassignBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            // Selection model: if card is selected, apply to all selected; otherwise select it first
            if (pickerSelection && pickerSelection.isSelected(face.id)) {
                const selectedIds = pickerSelection.getSelected();
                await handlePickPreferredDeleteRequested(selectedIds);
            } else {
                // Not selected - clear selection, select this card, then unassign
                if (pickerSelection) {
                    pickerSelection.clear();
                    pickerSelection.select(face.id);
                }
                await handlePickPreferredDeleteRequested([face.id]);
            }
        });

        card.appendChild(thumb);
        card.appendChild(unassignBtn);

        // Ignore button (assign to "-" person) - but not when already viewing "-" person
        let ignoreBtn = null;
        if (pickPreferredPersonName !== '-') {
            ignoreBtn = document.createElement('button');
            ignoreBtn.className = 'face-card-ignore';
            ignoreBtn.title = 'Move to ignored list';
            ignoreBtn.innerHTML = '<span class="material-symbols-outlined">remove</span>';
            ignoreBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                e.preventDefault();
                // Selection model: if card is selected, apply to all selected; otherwise select it first
                if (pickerSelection && pickerSelection.isSelected(face.id)) {
                    const selectedIds = pickerSelection.getSelected();
                    await handleIgnoreFaces(selectedIds, pickerSelection);
                } else {
                    // Not selected - clear selection, select this card, then ignore
                    if (pickerSelection) {
                        pickerSelection.clear();
                        pickerSelection.select(face.id);
                    }
                    await handleIgnoreFaces([face.id], pickerSelection);
                }
            });
            card.appendChild(ignoreBtn);
        }

        // Quick Match button (centered) - for reassigning to different person
        const quickMatchBtn = createQuickMatchButton(face.id, card, pickerSelection);
        card.appendChild(quickMatchBtn);

        // Repel buttons if thumbnail is too small
        repelFaceCardButtons(facesThumbnailSize, ignoreBtn, quickMatchBtn, unassignBtn);

        // Add star overlay (outside thumb to avoid circular clip)
        const star = document.createElement('div');
        star.className = 'face-card-star' + (face.is_preferred ? ' preferred' : '');
        star.dataset.faceId = face.id;
        star.innerHTML = '<span class="material-symbols-outlined">star</span>';
        star.title = face.is_preferred ? 'Preferred face' : 'Set as preferred face';
        star.addEventListener('click', (e) => {
            e.stopPropagation();
            handleStarClick(face.id);
        });
        card.appendChild(star);

        // Add padlock overlay for manual tag status
        const padlock = document.createElement('div');
        // Coerce to boolean - handles 0, 1, null, undefined
        const isManuallyTagged = Boolean(face.manually_tagged);
        padlock.className = 'face-card-padlock' + (isManuallyTagged ? '' : ' unlocked');
        padlock.dataset.faceId = face.id;
        padlock.innerHTML = `<span class="material-symbols-outlined">${isManuallyTagged ? 'lock' : 'lock_open'}</span>`;
        padlock.title = isManuallyTagged
            ? 'Manually tagged - used for recognition'
            : 'Auto-tagged - not used for recognition';
        padlock.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            // Selection model: if card is selected, apply to all selected; otherwise select it first
            if (pickerSelection && pickerSelection.isSelected(face.id)) {
                const selectedIds = pickerSelection.getSelected();
                await handlePadlockClick(selectedIds);
            } else {
                // Not selected - clear selection, select this card, then toggle
                if (pickerSelection) {
                    pickerSelection.clear();
                    pickerSelection.select(face.id);
                }
                await handlePadlockClick([face.id]);
            }
        });
        card.appendChild(padlock);

        // Editable name input (allows reassigning misclassified faces)
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-card-input';
        input.value = pickPreferredPersonName || '';

        // Handle focus - pre-fetch cache for fast autocomplete
        input.addEventListener('focus', () => {
            // AppState.people.load() handles TTL internally
            AppState.people.load();
        });

        // Track text selection to prevent focus loss when releasing outside input
        input.addEventListener('mousedown', () => {
            const refocusOnMouseUp = (e) => {
                if (e.target !== input) {
                    setTimeout(() => {
                        if (document.activeElement !== input && card.isConnected) {
                            input.focus();
                        }
                    }, 0);
                }
            };
            document.addEventListener('mouseup', refocusOnMouseUp, { capture: true, once: true });
        });

        // Handle input for autocomplete
        input.addEventListener('input', () => {
            showCardAutocomplete(input, input.value, card);
        });

        // Handle blur to commit (if name changed)
        input.addEventListener('blur', () => {
            // Delay to allow autocomplete click
            setTimeout(() => {
                // Skip if card was removed from DOM (e.g., during grid refresh)
                // This prevents committing partial input when refresh destroys the card
                if (!card.isConnected) return;

                // Skip if input was refocused (user was just selecting text, not leaving)
                if (document.activeElement === input) return;

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
                const isPreferred = star.dataset.faceId === faceId;
                star.classList.toggle('preferred', isPreferred);
                star.title = isPreferred ? 'Preferred face' : 'Set as preferred face';
            });

            // Update padlock visual for the preferred face (it gets padlocked)
            const padlock = facesGrid.querySelector(`.face-card-padlock[data-face-id="${faceId}"]`);
            if (padlock) {
                updatePadlockIcon(padlock, true);
            }

            // Mark person thumbnail for cache busting when returning to grid
            AppState.people.bustThumbnailCache(pickPreferredPersonId);
        } catch (error) {
            console.error('Failed to set preferred face:', error);
            App.showError('Failed to set preferred face.');
        }
    }

    /**
     * Handle padlock click to toggle manually_tagged status.
     * Implements multi-select lock logic:
     * - If any unlocked: lock those
     * - If all locked: unlock all (except preferred faces)
     *
     * @param {string[]} faceIds - Face IDs to toggle
     */
    async function handlePadlockClick(faceIds) {
        if (!faceIds?.length) return;

        // Get face data and determine lock states
        // Use Map for O(1) lookup instead of O(n) array.find()
        const faceMap = new Map(pickPreferredFaces.map(f => [f.id, f]));
        const faces = faceIds.map(id => faceMap.get(id)).filter(Boolean);
        if (!faces.length) return;

        const unlockedFaces = faces.filter(f => !f.manually_tagged);
        const lockedFaces = faces.filter(f => f.manually_tagged);

        let toLock = [];
        let toUnlock = [];

        if (unlockedFaces.length > 0) {
            // Some are unlocked - lock those
            toLock = unlockedFaces.map(f => f.id);
        } else {
            // All are locked - unlock all (except preferred)
            toUnlock = lockedFaces
                .filter(f => !f.is_preferred)
                .map(f => f.id);

            if (toUnlock.length === 0 && lockedFaces.length > 0) {
                App.showError('Cannot unlock the preferred face.');
                return;
            }
        }

        try {
            if (toLock.length > 0) {
                await AppState.faces.setLocked(toLock, true);
                // Update padlock visuals immediately
                for (const faceId of toLock) {
                    const padlock = facesGrid.querySelector(`.face-card-padlock[data-face-id="${faceId}"]`);
                    if (padlock) updatePadlockIcon(padlock, true);
                    const face = pickPreferredFaces.find(f => f.id === faceId);
                    if (face) face.manually_tagged = true;
                }
            }
            if (toUnlock.length > 0) {
                await AppState.faces.setLocked(toUnlock, false);
                // Update padlock visuals immediately
                for (const faceId of toUnlock) {
                    const padlock = facesGrid.querySelector(`.face-card-padlock[data-face-id="${faceId}"]`);
                    if (padlock) updatePadlockIcon(padlock, false);
                    const face = pickPreferredFaces.find(f => f.id === faceId);
                    if (face) face.manually_tagged = false;
                }
            }
        } catch (error) {
            console.error('Failed to toggle manual tag:', error);
            App.showError('Failed to toggle manual tag.');
        }
    }

    /**
     * Update padlock icon and title based on manually_tagged value.
     * @param {HTMLElement} padlockElement - The padlock element to update
     * @param {boolean} isManuallyTagged - Whether the face is manually tagged
     */
    function updatePadlockIcon(padlockElement, isManuallyTagged) {
        padlockElement.classList.toggle('unlocked', !isManuallyTagged);
        const icon = padlockElement.querySelector('.material-symbols-outlined');
        if (icon) {
            icon.textContent = isManuallyTagged ? 'lock' : 'lock_open';
        }
        padlockElement.title = isManuallyTagged
            ? 'Manually tagged - used for recognition'
            : 'Auto-tagged - not used for recognition';
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
     * Delegates to AppState.faces.identify() which handles optimistic updates.
     * The AppState subscription will refresh the pick-preferred view.
     *
     * @param {string} typedFaceId - Face ID where user typed the new name
     * @param {string} name - New person name to assign to
     * @param {HTMLElement} card - Card element (for resetting input on no-op)
     */
    async function commitPickPreferredFaceName(typedFaceId, name, card) {
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
        let faceIds = pickerSelection ? pickerSelection.getSelected() : [];

        // If the typed face isn't in the selection, or no selection, just use the typed face
        if (faceIds.length === 0 || !faceIds.includes(typedFaceId)) {
            faceIds = [typedFaceId];
        }

        // Clear selection immediately for better UX
        if (pickerSelection) {
            pickerSelection.clear();
        }

        try {
            // Delegate to AppState - it handles optimistic updates and broadcasts
            // The subscription will refresh the pick-preferred view
            await AppState.faces.identify(faceIds, name, {
                preferredFaceId: typedFaceId
            });

            // Check if all faces moved out - if so, delete the empty person and exit
            const remainingFaces = AppState.faces.getForPerson(pickPreferredPersonId);
            if (remainingFaces.length === 0) {
                await AppState.people.delete(pickPreferredPersonId);
                exitPickPreferredMode();
            }
        } catch (error) {
            console.error('Failed to reassign face:', error);
            App.showError(`Failed to reassign ${faceIds.length > 1 ? 'faces' : 'face'}.`);
        }
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
     * Delegates to AppState.faces.identify() which handles optimistic updates.
     * The AppState subscription will trigger re-render automatically.
     *
     * @param {Array<string>} faceIds - Face IDs to identify
     * @param {string} name - Name of the person (existing or new)
     * @param {Object} [options] - Optional settings
     * @param {string} [options.preferredFaceId] - Face ID to set as preferred (for new persons)
     * @returns {Promise<boolean>} True if API call succeeded
     */
    async function identifyFacesAsPerson(faceIds, name, options = {}) {
        facesLog('identifyFacesAsPerson:', { faceIds, name, options });

        if (!faceIds || faceIds.length === 0 || !name) {
            facesLog('  -> Early return: invalid args');
            return false;
        }

        // Clear selection immediately for better UX
        if (facesSelection) {
            facesSelection.clear();
        }

        try {
            // Delegate to AppState - it handles optimistic updates and broadcasts
            // The subscription will trigger renderFacesGrid() automatically
            await AppState.faces.identify(faceIds, name, {
                preferredFaceId: options.preferredFaceId
            });
            facesLog('  -> Success');
            return true;
        } catch (error) {
            facesLog('  -> Error:', error);
            console.error('Failed to identify faces:', error);
            App.showError(`Failed to identify ${faceIds.length > 1 ? 'faces' : 'face'}.`);
            return false;
        }
    }

    /**
     * Handle selection change in pick-preferred mode.
     */
    function handlePickPreferredSelectionChanged(selectedIds) {
        // Nothing special to do here
    }

    /**
     * Opens fullscreen viewer with selection sync for faces screen.
     * Subscribes to fullscreen events to update face selection as user navigates.
     * @param {string} imageId - Image ID to open
     * @param {Array<Object>} faces - Array of face objects for navigation context
     * @param {Object} selection - GridSelection instance to update
     * @param {Object} grid - VirtualGrid instance for scrolling
     */
    function openFullscreenWithSync(imageId, faces, selection, grid) {
        // Clear any existing subscription
        if (fullscreenUnsub) {
            fullscreenUnsub();
            fullscreenUnsub = null;
        }

        // Subscribe to fullscreen navigation events
        fullscreenUnsub = AppState.nav.onChanged((event) => {
            if (event.property === 'fullscreenImageId') {
                // Fullscreen navigated to a new image - find matching face and select
                const newImageId = AppState.nav.getFullscreenImageId();
                if (newImageId && selection) {
                    const face = faces.find(f => f.image_id === newImageId);
                    if (face) {
                        selection.select(face.id);
                    }
                }
            } else if (event.property === 'fullscreenClosing') {
                // Fullscreen is closing - scroll to the last viewed face
                if (event.imageId && grid) {
                    const face = faces.find(f => f.image_id === event.imageId);
                    if (face) {
                        grid.scrollToId(face.id);
                    }
                }
                // Unsubscribe
                if (fullscreenUnsub) {
                    fullscreenUnsub();
                    fullscreenUnsub = null;
                }
            }
        });

        // Clear multi-selection and select only the target face
        const targetFace = faces.find(f => f.image_id === imageId);
        if (targetFace && selection) {
            selection.select(targetFace.id);
        }

        // Build image list and open fullscreen
        const imageList = faces
            .filter(f => f.image_id)
            .map(f => ({ id: f.image_id }));
        App.showFullscreen(imageId, { imageList });
        setTaggingMode(true);
    }

    /**
     * Handle face activation in pick-preferred mode (Enter/double-click).
     * Opens fullscreen view for the corresponding image.
     */
    function handlePickPreferredFaceActivated(faceId) {
        const face = pickPreferredFaces.find(f => f.id === faceId);
        if (face && face.image_id) {
            openFullscreenWithSync(face.image_id, pickPreferredFaces, pickerSelection, pickPreferredGrid);
        }
    }

    /**
     * Handle rename button click in pick-preferred mode.
     * Shows modal dialog with autocomplete for renaming/merging.
     */
    async function handleRenamePersonClick() {
        if (!pickPreferredPersonId || !pickPreferredPersonName) return;

        // Pre-fetch people for autocomplete
        AppState.people.load();

        const dialog = document.getElementById('dialog-prompt');

        const newName = await App.prompt(
            'Rename Person',
            `Enter new name for "${pickPreferredPersonName}":`,
            {
                defaultValue: pickPreferredPersonName,
                onInput: (inputEl, autocompleteEl, value) => {
                    // Build autocomplete dropdown
                    autocompleteEl.innerHTML = '';
                    const q = value.trim();
                    if (!q) {
                        autocompleteEl.style.display = 'none';
                        return;
                    }

                    let matches = AppState.people.search(q);
                    // Exclude current person
                    matches = matches.filter(p => p.id !== pickPreferredPersonId);
                    if (matches.length === 0) {
                        autocompleteEl.style.display = 'none';
                        return;
                    }

                    // Position autocomplete below input (fixed positioning for dialog top-layer)
                    const rect = inputEl.getBoundingClientRect();
                    autocompleteEl.style.top = `${rect.bottom + 4}px`;
                    autocompleteEl.style.left = `${rect.left}px`;
                    autocompleteEl.style.width = `${rect.width}px`;

                    // Build items first, then show (to avoid :empty CSS rule)
                    const maxResults = 5;
                    for (let i = 0; i < Math.min(matches.length, maxResults); i++) {
                        const person = matches[i];
                        const item = document.createElement('div');
                        item.className = 'autocomplete-item';

                        const img = document.createElement('img');
                        img.src = AppState.people.getThumbnailUrl(person.id);
                        img.alt = '';
                        item.appendChild(img);

                        const nameSpan = document.createElement('span');
                        nameSpan.textContent = person.name;
                        item.appendChild(nameSpan);

                        const countSpan = document.createElement('span');
                        countSpan.className = 'face-count';
                        countSpan.textContent = `(${person.face_count})`;
                        item.appendChild(countSpan);

                        item.addEventListener('click', () => {
                            // Use dialog's selectValue to close with this name
                            if (dialog._selectValue) {
                                dialog._selectValue(person.name);
                            }
                        });

                        autocompleteEl.appendChild(item);
                    }

                    // Show after items added (so :empty rule doesn't hide it)
                    autocompleteEl.style.display = 'block';
                },
                onSelect: true  // Enable _selectValue on dialog
            }
        );

        // null means cancelled
        if (newName === null) return;

        // Commit the rename (handles merge, dissolve, simple rename)
        await commitPickerRenameFromModal(newName.trim());
    }

    /**
     * Commit rename from modal dialog.
     * Handles merge, dissolve, and simple rename cases.
     *
     * Cases:
     * - Same name → no-op
     * - Name exists → merge faces into existing person, delete original
     * - Empty name → unidentify all faces, delete person
     * - New name → simple rename (preserves locked/preferred state)
     */
    async function commitPickerRenameFromModal(trimmedName) {
        // Case B: Same name - no-op
        if (trimmedName.toLowerCase() === pickPreferredPersonName.toLowerCase()) {
            return;
        }

        try {
            await AppState.people.load();
            const existingPeople = AppState.people.getAll();
            const collision = existingPeople.find(p =>
                p.name.toLowerCase() === trimmedName.toLowerCase() && p.id !== pickPreferredPersonId
            );

            if (collision) {
                // Case C: Merge - move all faces to existing person, delete original
                const confirmed = await App.confirm(
                    'Merge People',
                    `Merge "${pickPreferredPersonName}" into "${collision.name}"? All faces will be moved to "${collision.name}".`
                );
                if (!confirmed) return;

                await AppState.people.merge(pickPreferredPersonId, collision.id);
                exitPickPreferredMode();

            } else if (trimmedName === '') {
                // Case D: Dissolve - unidentify all faces, delete person
                const confirmed = await App.confirm(
                    'Remove Person',
                    `Remove "${pickPreferredPersonName}"? All faces will return to the unknown pool.`
                );
                if (!confirmed) return;

                await AppState.people.dissolve(pickPreferredPersonId);
                exitPickPreferredMode();

            } else {
                // Case E: Simple rename - preserves locked/preferred state
                await AppState.people.rename(pickPreferredPersonId, trimmedName);

                // Update local picker state for immediate display
                pickPreferredPersonName = trimmedName;

                // Update header display
                const faceCount = pickPreferredFaces.length;
                const countText = faceCount === 1 ? '1 image' : `${faceCount} images`;
                pickerTitleEl.innerHTML = `${App.escapeHtml(trimmedName)} <span class="face-count">(${countText})</span>`;
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

                        // Debug: Log manually_tagged values to diagnose lock state issues
                        console.debug('[handleThresholdChange] Fetched faces:',
                            pickPreferredFaces.map(f => ({
                                id: f.id.slice(0, 8),
                                mt: f.manually_tagged,
                                type: typeof f.manually_tagged
                            })));

                        // Clear any pending reload flag (we're handling the update ourselves)
                        reloadPending = false;

                        // Clear picker selection (threshold changes only happen in picker mode)
                        if (pickerSelection) {
                            pickerSelection.clear();
                        }

                        // Update header (account for showLockedFaces filter)
                        const titleH3 = facesGrid.querySelector('.faces-pick-preferred-header h3');
                        if (titleH3) {
                            const displayedFaceCount = showLockedFaces
                                ? pickPreferredFaces.length
                                : pickPreferredFaces.filter(f => !f.manually_tagged).length;
                            const totalCount = pickPreferredFaces.length;
                            const countText = showLockedFaces
                                ? (displayedFaceCount === 1 ? '1 image' : `${displayedFaceCount} images`)
                                : `${displayedFaceCount} of ${totalCount} images`;
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
     * Delegates to AppState.faces.unassign() which handles optimistic updates.
     * The AppState subscription will refresh the pick-preferred view.
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

        // Clear selection immediately for better UX
        if (pickerSelection) {
            pickerSelection.clear();
        }

        try {
            // Delegate to AppState - it handles optimistic updates and broadcasts
            // The subscription will refresh the pick-preferred view
            await AppState.faces.unassign(faceIds);

            // Check if all faces were removed - if so, delete the person and exit
            // (reconcilePerson() can't handle this with partial cache, so we do it explicitly)
            const remainingFaces = AppState.faces.getForPerson(pickPreferredPersonId);
            if (remainingFaces.length === 0) {
                await AppState.people.delete(pickPreferredPersonId);
                exitPickPreferredMode();
            }
        } catch (error) {
            console.error('Failed to unassign faces:', error);
            App.showError('Failed to unassign faces');
        }
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
            focusContainer: unknownSection,
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
            // Open fullscreen WITHOUT selection sync for unknown faces.
            // Multiple faces can share the same image_id (different people in same photo),
            // so syncing by image_id would select the wrong face when returning.
            // The user's original selection is preserved instead.
            const imageList = displayedFaces
                .filter(f => f.image_id)
                .map(f => ({ id: f.image_id }));
            App.showFullscreen(face.image_id, { imageList });
            setTaggingMode(true);
        }
    }

    /**
     * Suppress a single face (mark as false positive) without confirmation.
     * Delegates to AppState which handles optimistic updates.
     * @param {string} faceId - Face ID to suppress
     */
    function suppressSingleFace(faceId) {
        // Delegate to AppState - it handles optimistic updates and broadcasts
        // The subscription will trigger renderFacesGrid() automatically
        AppState.faces.suppress(faceId).catch(error => {
            console.error(`Failed to suppress face ${faceId}:`, error);
        });
    }

    /**
     * Handle delete request for selected faces.
     * Suppresses faces (marks as false positives) rather than deleting images.
     * Delegates to AppState which handles optimistic updates.
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

        // Clear selection immediately for better UX
        if (facesSelection) {
            facesSelection.clear();
        }

        // Delegate to AppState - it handles optimistic updates and broadcasts
        // The subscription will trigger renderFacesGrid() automatically
        AppState.faces.suppress(faceIds).catch(error => {
            console.error('Failed to suppress faces:', error);
            App.showError('Failed to suppress faces.');
        });
    }

    /**
     * Handle ignore request for faces.
     * Assigns faces to the "-" (ignored) person.
     * Works for both unknown faces and picker faces (reassignment).
     * @param {Array<string>} faceIds - Face IDs to ignore
     * @param {GridSelection} [selection] - Selection to clear (facesSelection or pickerSelection)
     */
    async function handleIgnoreFaces(faceIds, selection) {
        if (!faceIds || faceIds.length === 0) return;

        // Only confirm if moving multiple faces
        if (faceIds.length > 1) {
            const message = `Move ${faceIds.length} faces to the ignored list? They will no longer appear in the unknown faces.`;
            const confirmed = await App.confirm('Ignore Faces', message);
            if (!confirmed) return;
        }

        // Clear selection immediately for better UX
        if (selection) {
            selection.clear();
        }

        try {
            // Assign to the "-" (ignored) person
            await AppState.faces.identify(faceIds, '-');

            // If in picker mode, check if all faces were removed - if so, delete person and exit
            if (pickPreferredPersonId) {
                const remainingFaces = AppState.faces.getForPerson(pickPreferredPersonId);
                if (remainingFaces.length === 0) {
                    await AppState.people.delete(pickPreferredPersonId);
                    exitPickPreferredMode();
                }
            }
        } catch (error) {
            console.error('Failed to ignore faces:', error);
            App.showError('Failed to ignore faces.');
        }
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
     *   - Data is retained in AppState (no reload needed on return)
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
                    // loadAllFaces() will create containers after clearing
                    loadAllFaces();
                    // Focus will be set after load completes
                } else {
                    // Ensure persistent containers exist (for re-entering without refresh)
                    ensurePersistentContainers();

                    // Ensure people cache is loaded for autocomplete
                    AppState.people.load();

                    // Handle pick-preferred mode
                    if (viewMode === 'pick-preferred') {
                        if (pickPreferredGrid) pickPreferredGrid.bind();
                        if (pickerSelection) pickerSelection.bind();
                        if (needsRerender) {
                            needsRerender = false;
                            FacesRefresh.onFacesChanged();
                        }
                        // Focus picker view
                        if (pickerView) pickerView.focus({ preventScroll: true });
                        return;
                    }

                    // Normal mode: Rebind grids and selection
                    if (needsRerender) {
                        // Data changed while away - re-render both people and unknown sections
                        needsRerender = false;
                        renderFacesGrid();
                    } else {
                        // Just rebind existing grids
                        if (unknownFacesGrid) {
                            unknownFacesGrid.bind();
                        }
                        if (facesSelection) {
                            facesSelection.bind();
                        }
                    }

                    // Focus appropriate section
                    if (knownPeople.length > 0 && peopleSection) {
                        peopleSection.focus({ preventScroll: true });
                    } else if (unknownSection) {
                        unknownSection.focus({ preventScroll: true });
                    }
                }
            },

            onLeave() {
                // Unbind all grids and selections (stops event handlers on hidden screen)
                if (viewMode === 'pick-preferred') {
                    if (pickPreferredGrid) pickPreferredGrid.unbind();
                    if (pickerSelection) pickerSelection.unbind();
                } else {
                    if (unknownFacesGrid) unknownFacesGrid.unbind();
                    if (facesSelection) facesSelection.unbind();
                }
                // Clear search state
                unknownFacesSearchQuery = '';
                // Clear pending reload flag
                reloadPending = false;
                // Clear loading flag — if we navigated away mid-load, the
                // subscription handlers skipped checkLoadingComplete() because
                // we weren't on the faces screen.  Without this reset,
                // loadAllFaces() would bail on re-entry (isLoading guard)
                // and the screen would be permanently blank.  Also force a
                // full refresh on re-entry since loadAllFaces() destroyed the
                // grids/containers but never finished rebuilding them.
                if (isLoading) {
                    AppState.loading.hide('faces');
                    isLoading = false;
                    needsRefresh = true;
                }
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

        // Manage resize listener
        if (taggingMode && Fullscreen.isOpen()) {
            // Ensure people cache is loaded for face identification operations
            AppState.people.load();

            // Add resize listener for bbox repositioning
            if (!resizeHandler) {
                resizeHandler = handleWindowResize;
                window.addEventListener('resize', resizeHandler);
            }

            const imageId = Fullscreen.state.currentId;
            if (imageId) {
                loadFacesForImage(imageId, { fresh: true });
            }
        } else if (!taggingMode) {
            // Remove resize listener
            if (resizeHandler) {
                window.removeEventListener('resize', resizeHandler);
                resizeHandler = null;
            }
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
     * - displayedFaces is set to search results only (sorted by relevance)
     *
     * DEFAULT MODE (query empty):
     * - Reload faces from AppState via renderFacesGrid()
     *
     * @param {string} query - Search query (empty string resets to default)
     */
    async function searchUnknownFaces(query) {
        // Preserve scroll position for restoration after render
        const unknownContainer = facesGrid?.querySelector('.faces-unknown-container');
        const scrollTopBefore = unknownContainer ? unknownContainer.scrollTop : 0;

        try {
            if (!query) {
                // No search - render from AppState (normal mode)
                renderFacesGrid();
            } else {
                // Search mode - fetch and display search results
                showFacesLoading('Searching faces…');

                // Use AppState.faces.search() for semantic face search
                const faces = await AppState.faces.search(query);
                displayedFaces = faces;

                // Re-render just the unknown faces grid with search results
                if (unknownFacesGrid) {
                    unknownFacesGrid.render();
                }

                // Update count in header
                const header = unknownSection?.querySelector('.faces-section-header h3');
                if (header) {
                    header.textContent = `Unknown Faces (${displayedFaces.length})`;
                }
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
     *
     * Shows loading banner, triggers AppState loads, then returns.
     * The subscription handlers (onChanged) will render grids and hide
     * the loading banner when all required domains are ready.
     */
    function loadAllFaces() {
        if (isLoading) return;
        isLoading = true;

        // Save scroll position before reload
        const container = facesGrid?.querySelector('.faces-unknown-container');
        const scrollTopBefore = container ? container.scrollTop : 0;
        savedScrollTop = scrollTopBefore;

        // Unbind and destroy grids and selections BEFORE clearing DOM
        // (otherwise event listeners are orphaned when container is removed)
        if (facesSelection) {
            facesSelection.unbind();
            facesSelection = null;
        }
        if (pickerSelection) {
            pickerSelection.unbind();
            pickerSelection = null;
        }
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

        // Clear grid and reset persistent container references
        if (facesGrid) facesGrid.innerHTML = '';
        normalView = null;
        pickerView = null;
        peopleSection = null;
        unknownSection = null;
        unknownContainer = null;
        pickerHeader = null;
        pickerGridContainer = null;
        pickerTitleEl = null;
        pickerThresholdSlider = null;
        pickerThresholdValue = null;
        pickerLoadingEl = null;
        if (facesEmpty) facesEmpty.hidden = true;

        // Check if data is already cached - if so, render immediately
        // But if needsRefresh was true, force reload to get fresh data
        const facesLoaded = AppState.faces.isLoaded();
        const peopleLoaded = AppState.people.isLoaded();
        const wasRefreshNeeded = needsRefresh;

        if (facesLoaded && peopleLoaded && !wasRefreshNeeded) {
            // Data already cached - render immediately, no loading banner needed
            needsRefresh = false;
            needsRerender = false;
            isLoading = false;
            renderFacesGrid();

            // Restore scroll position
            const newContainer = facesGrid?.querySelector('.faces-unknown-container');
            if (newContainer && savedScrollTop > 0) {
                newContainer.scrollTop = savedScrollTop;
                savedScrollTop = 0;
            }

            // Bind selection
            if (facesSelection) {
                facesSelection.bind();
            }

            // Re-run search if active
            if (unknownFacesSearchQuery) {
                searchUnknownFaces(unknownFacesSearchQuery);
            }

            // Focus appropriate section for keyboard navigation
            if (viewMode === 'pick-preferred' && pickerView) {
                pickerView.focus({ preventScroll: true });
            } else if (knownPeople.length > 0 && peopleSection) {
                peopleSection.focus({ preventScroll: true });
            } else if (unknownSection) {
                unknownSection.focus({ preventScroll: true });
            }
            return;
        }

        // Show loading banner - data needs to be fetched
        showFacesLoading('Loading faces…');

        // Trigger loads - subscription handlers will render when data arrives
        // and hide loading banner when both domains are ready
        // Use loadUnknownOnly() for faster initial load - only fetches unknown faces
        // Known people section uses AppState.people data directly
        // Force reload if needsRefresh was true (otherwise loadUnknownOnly returns early
        // when cache exists, broadcast never fires, and nothing renders)
        AppState.faces.loadUnknownOnly(wasRefreshNeeded);
        AppState.people.load(wasRefreshNeeded);

        // Mark refresh complete - actual rendering happens in subscription handlers
        needsRefresh = false;
        needsRerender = false;
    }

    /**
     * Mark faces as needing refresh on next screen enter.
     * Called when database changes or faces are modified externally.
     */
    function markFacesNeedsRefresh() {
        needsRefresh = true;
    }

    /**
     * Render/update the faces grid with known and unknown sections.
     *
     * ARCHITECTURE:
     * Uses persistent containers (normalView) that are never destroyed.
     * - People section: Updated in-place with new person cards
     * - Unknown section: VirtualGrid.render() refreshes with new data
     *
     * This preserves scroll positions and selection state across updates.
     */
    function renderFacesGrid() {
        facesLog('renderFacesGrid START');

        if (!facesGrid) {
            facesLog('  -> Early return: no facesGrid');
            return;
        }

        // Ensure persistent containers exist
        ensurePersistentContainers();

        // Clear pending flags
        reloadPending = false;

        // Get unknown faces from AppState (uses loadUnknownOnly for fast initial load)
        const unknownFaces = AppState.faces.getUnknown();
        facesLog('  Unknown faces count:', unknownFaces.length);

        // Update displayedFaces - this is what VirtualGrid and GridSelection use
        displayedFaces = unknownFaces;
        facesLog('  displayedFaces set to', displayedFaces.length, 'faces');

        // Get people from AppState (already has face_count from backend)
        // Filter out people with 0 faces (shouldn't exist, but defensive)
        const allPeople = AppState.people.getAll();
        knownPeople = allPeople
            .filter(p => p.face_count > 0)
            .sort((a, b) => {
                const cmp = a.name.localeCompare(b.name);
                return sortAscending ? cmp : -cmp;
            });
        facesLog('  Known people count:', knownPeople.length);

        // Check for empty state
        if (unknownFaces.length === 0 && knownPeople.length === 0) {
            displayedFaces = [];
            if (facesEmpty) facesEmpty.hidden = false;
            if (normalView) normalView.hidden = true;
            return;
        }
        if (facesEmpty) facesEmpty.hidden = true;
        if (normalView) normalView.hidden = (viewMode === 'pick-preferred');

        // Update people section content (preserve scroll position)
        updatePeopleSection();

        // Update unknown section - VirtualGrid.render() preserves scroll
        updateUnknownSection();

        // Re-apply semantic search if active (identification removed a face but
        // the search query should remain in effect with its sort order)
        if (unknownFacesSearchQuery) {
            searchUnknownFaces(unknownFacesSearchQuery);
        }

        // Prune selection to remove any IDs that no longer exist
        if (facesSelection) {
            facesSelection.pruneToValidIds();
        }
    }

    /**
     * Update the people section content without destroying the container.
     * Preserves scroll position.
     */
    function updatePeopleSection() {
        if (!peopleSection) return;

        // Save scroll position
        const scrollTop = peopleSection.scrollTop;

        // Find or create header and grid
        let header = peopleSection.querySelector('.faces-section-header');
        let grid = peopleSection.querySelector('.faces-section-grid');

        if (!header) {
            header = document.createElement('div');
            header.className = 'faces-section-header';
            header.innerHTML = '<h3 class="faces-section-title known">Known</h3><span class="faces-section-count"></span>';
            peopleSection.insertBefore(header, peopleSection.firstChild);
        }

        if (!grid) {
            grid = document.createElement('div');
            grid.className = 'faces-section-grid';
            peopleSection.appendChild(grid);
        }

        // Update count
        const countEl = header.querySelector('.faces-section-count');
        if (countEl) {
            countEl.textContent = `(${knownPeople.length})`;
        }

        // Show/hide based on content and mode
        const showPeople = knownPeople.length > 0 && !showOnlyUnknowns;
        peopleSection.hidden = !showPeople;

        // Update divider visibility
        const divider = normalView?.querySelector('.faces-divider');
        const hasUnknowns = displayedFaces.length > 0;
        if (divider) {
            divider.hidden = !showPeople || !hasUnknowns;
        }

        // When there are no unknown faces (and thus no divider), let the people
        // section fill the full available height. When unknowns reappear (e.g.
        // from backend processing), restore the constrained layout.
        if (showPeople) {
            if (!hasUnknowns) {
                // No unknowns — unconstrain so the section fills available space
                peopleSection.style.maxHeight = 'none';
                peopleSection.style.height = '';
            } else {
                // Unknowns present — re-apply CSS cap and saved divider height
                peopleSection.style.maxHeight = '';
                if (knownSectionHeight) {
                    peopleSection.style.height = `${knownSectionHeight}px`;
                }
            }
        }

        if (!showPeople) return;

        // Rebuild person cards
        grid.innerHTML = '';
        for (const person of knownPeople) {
            const card = createPersonCard(person);
            grid.appendChild(card);
        }

        // Restore scroll position
        peopleSection.scrollTop = scrollTop;
    }

    /**
     * Update the unknown faces section.
     * VirtualGrid.render() automatically preserves scroll position.
     */
    function updateUnknownSection() {
        if (!unknownSection || !unknownContainer) return;

        // Update header count
        const header = unknownSection.querySelector('.faces-section-header h3');
        if (header) {
            header.textContent = `Unknown Faces (${displayedFaces.length})`;
        }

        // Show/hide section
        unknownSection.hidden = displayedFaces.length === 0;

        if (displayedFaces.length === 0) return;

        // Ensure VirtualGrid exists and render
        if (!unknownFacesGrid) {
            initUnknownFacesGrid();
        }

        if (unknownFacesGrid) {
            unknownFacesGrid.render();
            // Ensure bound for keyboard navigation
            if (viewMode !== 'pick-preferred') {
                unknownFacesGrid.bind();
            }
        }

        // Ensure selection is initialized and bound
        if (!facesSelection) {
            initFacesSelection();
        }
        if (facesSelection && viewMode !== 'pick-preferred') {
            facesSelection.bind();
        }
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
        searchInput.title = "Semantic search: describe what you're looking for. Use -word to exclude. More terms = better results (e.g. 'happy smiling -glasses -sunglasses').";
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
            getThumbnailUrl: (faceId) => FaceThumbnails.getUrl(faceId),
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
            // Don't start card drag when selecting text in input field
            if (e.target.matches('input, textarea')) {
                e.preventDefault();
                return;
            }

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
        img.title = 'Double-click to open image ' + (face.image_basename || '');
        thumb.appendChild(img);

        // Suppress button (mark as false positive)
        const suppressBtn = document.createElement('button');
        suppressBtn.className = 'face-card-suppress';
        suppressBtn.title = 'Mark as false positive (not a face)';
        suppressBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';
        suppressBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            // If this face is part of a selection, suppress all selected faces
            if (facesSelection && facesSelection.isSelected(face.id)) {
                const selectedIds = facesSelection.getSelected();
                await handleFacesDeleteRequested(selectedIds);
            } else {
                // Not selected - clear selection, select this card, then suppress
                if (facesSelection) {
                    facesSelection.clear();
                    facesSelection.select(face.id);
                }
                await handleFacesDeleteRequested([face.id]);
            }
        });

        // Ignore button (assign to "-" person)
        const ignoreBtn = document.createElement('button');
        ignoreBtn.className = 'face-card-ignore';
        ignoreBtn.title = 'Move to ignored list';
        ignoreBtn.innerHTML = '<span class="material-symbols-outlined">remove</span>';
        ignoreBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            // Selection model: if card is selected, apply to all selected; otherwise select it first
            if (facesSelection && facesSelection.isSelected(face.id)) {
                const selectedIds = facesSelection.getSelected();
                await handleIgnoreFaces(selectedIds, facesSelection);
            } else {
                // Not selected - clear selection, select this card, then ignore
                if (facesSelection) {
                    facesSelection.clear();
                    facesSelection.select(face.id);
                }
                await handleIgnoreFaces([face.id], facesSelection);
            }
        });

        // Quick Match button (centered)
        const quickMatchBtn = createQuickMatchButton(face.id, card, facesSelection);

        card.appendChild(thumb);
        card.appendChild(suppressBtn);
        card.appendChild(ignoreBtn);
        card.appendChild(quickMatchBtn);

        // Repel buttons if thumbnail is too small
        repelFaceCardButtons(facesThumbnailSize, ignoreBtn, quickMatchBtn, suppressBtn);

        // Create editable name input
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-card-input';
        input.placeholder = 'Enter name...';
        input.value = '';

        // Handle focus - pre-fetch cache for fast autocomplete
        input.addEventListener('focus', () => {
            // AppState.people.load() handles TTL internally
            AppState.people.load();
            // Disable card dragging while editing text (allows text selection)
            card.draggable = false;
        });

        // Track text selection to prevent focus loss when releasing outside input
        // On mousedown in input, add a one-time document mouseup handler that refocuses
        input.addEventListener('mousedown', () => {
            const refocusOnMouseUp = (e) => {
                // If released outside the input, refocus to prevent blur
                if (e.target !== input) {
                    // Use setTimeout to let any pending events settle, then refocus
                    setTimeout(() => {
                        if (document.activeElement !== input && card.isConnected) {
                            input.focus();
                        }
                    }, 0);
                }
            };
            // Add handler with capture to run before other handlers, once to auto-cleanup
            document.addEventListener('mouseup', refocusOnMouseUp, { capture: true, once: true });
        });

        // Handle input for autocomplete
        input.addEventListener('input', () => {
            showCardAutocomplete(input, input.value, card);
        });

        // Handle blur to commit (applies to all selected faces)
        input.addEventListener('blur', () => {
            // Re-enable card dragging after editing
            card.draggable = true;
            // Delay to allow autocomplete click
            setTimeout(() => {
                // Skip if card was removed from DOM (e.g., during grid refresh)
                // This prevents committing partial input when refresh destroys the card
                if (!card.isConnected) return;

                // Skip if input was refocused (user was just selecting text, not leaving)
                if (document.activeElement === input) return;

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
        img.src = AppState.people.getThumbnailUrl(person.id);
        img.alt = person.name;
        img.loading = 'lazy';
        thumb.appendChild(img);

        card.appendChild(thumb);

        // Add face count badge if multiple faces (outside thumb to avoid circle clipping)
        const faceCount = person.face_count ?? person.faces?.length ?? 0;
        if (faceCount > 1) {
            const badge = document.createElement('div');
            badge.className = 'face-card-badge';
            badge.innerHTML = `<span class="material-symbols-outlined">star</span>`;
            badge.title = `${faceCount} faces`;
            card.appendChild(badge);
        }

        // Add filter badge (left side) - click to filter gallery by this person
        const filterBadge = document.createElement('div');
        filterBadge.className = 'face-card-filter-badge';
        filterBadge.innerHTML = `<span class="material-symbols-outlined">filter_alt</span>`;
        filterBadge.title = `Show all images with ${person.name}`;
        filterBadge.addEventListener('click', (e) => {
            e.stopPropagation(); // Don't trigger card selection
            // Set filter to show only this person's images and navigate to gallery
            App.setFilter({
                people: [{ id: person.id, name: person.name }]
            });
            App.navigateTo('gallery');
        });
        card.appendChild(filterBadge);

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

        // Make person card draggable (for merging people)
        card.draggable = true;
        card.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('application/x-person-id', person.id);
            e.dataTransfer.setData('application/x-person-name', person.name);
            e.dataTransfer.effectAllowed = 'move';
        });

        // Drop target for unknown faces AND other person cards (merge)
        card.addEventListener('dragover', (e) => {
            // Check if dragging faces
            if (e.dataTransfer.types.includes('application/x-face-ids')) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                card.classList.add('drop-target');
            }
            // Check if dragging another person card (for merge)
            else if (e.dataTransfer.types.includes('application/x-person-id')) {
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

            // Handle face drop (identify faces as this person)
            const faceData = e.dataTransfer.getData('application/x-face-ids');
            if (faceData) {
                facesLog('DROP faces on person card:', person.name, 'data=', faceData);
                try {
                    const faceIds = JSON.parse(faceData);
                    facesLog('  -> Parsed faceIds:', faceIds);
                    if (faceIds && faceIds.length > 0) {
                        facesLog('  -> Calling identifyFacesAsPerson');
                        await identifyFacesAsPerson(faceIds, person.name);
                        facesLog('  -> identifyFacesAsPerson returned');
                    }
                } catch (err) {
                    facesLog('  -> DROP ERROR:', err);
                    console.error('Drop failed:', err);
                    App.showError('Failed to identify faces');
                }
                return;
            }

            // Handle person drop (merge people)
            const draggedPersonId = e.dataTransfer.getData('application/x-person-id');
            const draggedPersonName = e.dataTransfer.getData('application/x-person-name');
            if (draggedPersonId && draggedPersonId !== person.id) {
                facesLog('DROP person on person card:', draggedPersonName, '->', person.name);
                try {
                    // Show merge confirmation dialog
                    const confirmed = await App.confirm(
                        'Merge People',
                        `Merge "${draggedPersonName}" into "${person.name}"? All faces will be moved to "${person.name}".`
                    );
                    if (confirmed) {
                        await AppState.people.merge(draggedPersonId, person.id);
                        facesLog('  -> Merge complete');
                    }
                } catch (err) {
                    facesLog('  -> MERGE ERROR:', err);
                    console.error('Merge failed:', err);
                    App.showError('Failed to merge people');
                }
            }
        });

        return card;
    }


    /**
     * Show autocomplete dropdown for a name input.
     * Reusable for face cards and picker rename input.
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     * @param {HTMLElement} container - Parent element to append autocomplete to
     * @param {Object} options - {excludePersonId: string, className: string}
     */
    function showNameAutocomplete(input, query, container, options = {}) {
        const { excludePersonId = null, className = 'face-card-autocomplete' } = options;

        // Trigger background refresh if cache is stale (don't await - use current data)
        AppState.people.load();

        // Remove existing autocomplete
        const existing = container.querySelector('.' + className.split(' ')[0]);
        if (existing) existing.remove();

        // Use AppState for fuzzy search with proper sorting
        const q = query.trim();
        if (!q) return;

        let matches = AppState.people.search(q);

        // Optionally exclude current person (for rename)
        if (excludePersonId) {
            matches = matches.filter(p => p.id !== excludePersonId);
        }

        if (matches.length === 0) return;

        // Create autocomplete dropdown
        const autocomplete = document.createElement('div');
        autocomplete.className = className;

        const maxResults = 5;
        for (let i = 0; i < Math.min(matches.length, maxResults); i++) {
            const person = matches[i];
            const item = document.createElement('div');
            item.className = className.split(' ')[0] + '-item';

            const img = document.createElement('img');
            img.src = AppState.people.getThumbnailUrl(person.id);
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

        container.appendChild(autocomplete);

        // Post-render: adjust position if the autocomplete extends off-viewport.
        // Uses rAF so layout is finalized and getBoundingClientRect is accurate.
        requestAnimationFrame(() => {
            if (!autocomplete.isConnected) return;
            const rect = autocomplete.getBoundingClientRect();

            // Bottom overflow: flip upward (above the card instead of below)
            if (rect.bottom > window.innerHeight) {
                autocomplete.style.top = 'auto';
                autocomplete.style.bottom = '100%';
                autocomplete.style.marginBottom = '2px';
            }
        });
    }

    /**
     * Show autocomplete for face card input.
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     * @param {HTMLElement} card - Parent card element
     */
    function showCardAutocomplete(input, query, card) {
        showNameAutocomplete(input, query, card, { className: 'face-card-autocomplete' });
    }

    /**
     * Show the global loading overlay with a custom message.
     * @param {string} message - Message to display
     */
    function showFacesLoading(message) {
        AppState.loading.show('faces', message);
    }

    /**
     * Hide the global loading overlay if faces is the owner.
     */
    function hideFacesLoading() {
        AppState.loading.hide('faces');
        isLoading = false;
    }

    /**
     * Check if all required domains are loaded and hide loading banner if so.
     * Called by subscription handlers after rendering.
     */
    function checkLoadingComplete() {
        if (!isLoading) return;
        if (AppState.faces.isLoaded() && AppState.people.isLoaded()) {
            hideFacesLoading();
            isLoading = false;

            // Restore scroll position after both domains loaded
            const container = facesGrid?.querySelector('.faces-unknown-container');
            if (container && savedScrollTop > 0) {
                container.scrollTop = savedScrollTop;
                savedScrollTop = 0;
            }

            // Bind selection after grid is rendered
            if (facesSelection) {
                facesSelection.bind();
            }

            // If there's an active search query, re-run it to filter results
            if (unknownFacesSearchQuery) {
                searchUnknownFaces(unknownFacesSearchQuery);
            }

            // Focus appropriate section for keyboard navigation
            if (viewMode === 'pick-preferred' && pickerView) {
                pickerView.focus({ preventScroll: true });
            } else if (knownPeople.length > 0 && peopleSection) {
                peopleSection.focus({ preventScroll: true });
            } else if (unknownSection) {
                unknownSection.focus({ preventScroll: true });
            }
        }
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
     * Clear face overlay when changing screens (fullscreen handles its own faces via events).
     * @param {string} screen - New screen name
     */
    function handleScreenChange(screen) {
        // Clear pending input restore - user navigated away
        clearPendingInputRestore();

        // Clear face overlay when changing screens
        // (fullscreen overlay handles its own faces via fullscreenImageChanged event)
        if (!Fullscreen.isOpen()) {
            clearFaceOverlay();
        }
    }

    /**
     * Handle fullscreen image change.
     * @param {string} imageId - New image ID
     */
    function handleFullscreenImageChange(imageId) {
        if (isTaggingModeActive() && imageId) {
            // Clear old bboxes immediately before loading new ones
            clearFaceOverlay(false);
            loadFacesForImage(imageId, { fresh: true });
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

    /**
     * Handle window resize - recalculate bbox positions.
     * Debounced to avoid excessive re-renders during resize.
     */
    let resizeDebounceTimer = null;
    function handleWindowResize() {
        if (!faceOverlay || !isTaggingModeActive() || !currentOverlayFaces) return;

        // Debounce resize handling
        if (resizeDebounceTimer) {
            clearTimeout(resizeDebounceTimer);
        }
        resizeDebounceTimer = setTimeout(() => {
            resizeDebounceTimer = null;
            if (currentOverlayFaces && currentOverlayImageId) {
                renderFaces(currentOverlayFaces, currentOverlayImageId);
            }
        }, 100);
    }

    // =========================================================================
    // FACE LOADING AND RENDERING
    // =========================================================================

    /**
     * Load faces for an image and render overlays.
     * Uses cache by default (for optimistic updates), fetches fresh on navigation.
     * @param {string} imageId - Image ID
     * @param {Object} [options]
     * @param {boolean} [options.fresh=false] - Bypass cache and fetch from backend
     */
    async function loadFacesForImage(imageId, { fresh = false } = {}) {
        if (!faceOverlay) return;

        // Track which image we're loading faces for
        currentOverlayImageId = imageId;

        try {
            // Use cache when available (has optimistic updates from user actions)
            // Only fetch fresh when explicitly requested (e.g., initial load, navigation)
            let faces;
            if (!fresh) {
                faces = AppState.faces.getForImage(imageId);
            }

            // Fetch from backend if cache miss or fresh requested
            if (!faces || faces.length === 0 || fresh) {
                faces = await AppState.faces.fetchForImage(imageId, { fresh });
            }

            // Skip if we've navigated away during the async call
            if (currentOverlayImageId !== imageId) return;

            renderFaces(faces || [], imageId);
        } catch (error) {
            console.error('Failed to load faces:', error);
            if (currentOverlayImageId === imageId) {
                clearFaceOverlay();
            }
        }
    }

    /**
     * Clear the face overlay.
     * @param {boolean} [resetTracking=true] - Whether to reset the image tracking variable
     */
    function clearFaceOverlay(resetTracking = true) {
        if (faceOverlay) {
            faceOverlay.innerHTML = '';
        }
        if (resetTracking) {
            currentOverlayImageId = null;
            currentOverlayFaces = null;
        }
        focusedInput = null;
        closeAutocomplete();
    }

    /**
     * Capture input state from the face overlay.
     * Used to preserve user's typing when overlay refreshes.
     * @returns {Object|null} Input state or null if no input focused
     */
    function captureOverlayInputState() {
        if (!faceOverlay) return null;

        const activeInput = faceOverlay.querySelector('input:focus');
        if (!activeInput) return null;

        const faceBox = activeInput.closest('[data-face-id]');
        if (!faceBox) return null;

        return {
            faceId: faceBox.dataset.faceId,
            value: activeInput.value,
            selectionStart: activeInput.selectionStart,
            selectionEnd: activeInput.selectionEnd,
        };
    }

    /**
     * Restore input state after overlay refresh.
     * @param {Object} state - State from captureOverlayInputState
     */
    function restoreOverlayInputState(state) {
        if (!state || !faceOverlay) return;

        // Find the face box by data-face-id (if it still exists)
        const faceBox = faceOverlay.querySelector(`[data-face-id="${state.faceId}"]`);
        if (!faceBox) return;  // Face was removed

        const input = faceBox.querySelector('input');
        if (!input) return;

        // Restore value and selection
        input.value = state.value;
        input.focus({ preventScroll: true });
        if (input.setSelectionRange) {
            input.setSelectionRange(state.selectionStart, state.selectionEnd);
        }
    }

    /**
     * Render faces on the overlay.
     * @param {Array<Object>} faces - Array of face objects
     * @param {string} forImageId - Image ID these faces belong to (for stale check)
     * @param {Object} [savedInputState] - Input state to restore (passed through recursive calls)
     */
    function renderFaces(faces, forImageId, savedInputState = null) {
        if (!faceOverlay || !fullscreenImage || !fullscreenContainer) {
            return;
        }

        // Skip if we've navigated to a different image
        if (forImageId && currentOverlayImageId !== forImageId) {
            return;
        }

        // Capture input state before clearing (for restore after render)
        // Use passed-in state if this is a recursive call after image load
        const inputState = savedInputState || captureOverlayInputState();

        // Clear overlay content but preserve tracking variable (we're about to render)
        clearFaceOverlay(false);

        // Store faces for re-rendering on resize
        currentOverlayFaces = faces;

        // Wait for image to be loaded to get dimensions
        if (!fullscreenImage.complete) {
            fullscreenImage.addEventListener('load', () => {
                // Check again after load - user may have navigated away
                if (forImageId && currentOverlayImageId !== forImageId) return;
                // Pass through input state to the recursive call
                renderFaces(faces, forImageId, inputState);
            }, { once: true });
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

        // Post-render: flip labels that extend past the viewport bottom.
        // Uses rAF so the browser has laid out the elements and getBoundingClientRect
        // returns accurate values. Only applies at zoom=1 (panned/zoomed state makes
        // viewport checks unreliable and the user can scroll to see labels).
        if (zoom === 1) {
            requestAnimationFrame(() => {
                const belowLabels = faceOverlay.querySelectorAll('.face-label.below');
                for (const label of belowLabels) {
                    const rect = label.getBoundingClientRect();
                    if (rect.bottom > window.innerHeight) {
                        label.classList.remove('below');
                        label.classList.add('above');
                    }
                }
            });
        }

        // Restore input state after render (if we had a focused input)
        if (inputState) {
            requestAnimationFrame(() => restoreOverlayInputState(inputState));
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
            // Ignored faces (named '-') get an extra class for different styling
            // Note: translucency is handled purely via CSS colors, not element opacity,
            // so child elements (like the action button) aren't affected
            if (face.person_name === '-') {
                box.classList.add('ignored');
            }
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

        // Create action button (unidentify for known faces, suppress for unknown)
        const actionBtn = document.createElement('button');
        actionBtn.className = 'face-delete-btn';
        actionBtn.innerHTML = '<span class="material-symbols-outlined">close</span>';

        if (face.person_id) {
            // Known face: green button to unidentify
            actionBtn.classList.add('unidentify');
            actionBtn.title = 'Remove identification (return to unknown)';
            actionBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                // Suppress overlay reload - we update the DOM directly
                suppressOverlayReload = true;

                // Optimistic UI: update box to unknown styling
                box.classList.remove('known', 'ignored');
                box.classList.add('unknown');

                // Update action button to suppress style
                actionBtn.classList.remove('unidentify');
                actionBtn.title = 'Remove face detection (not a real face)';

                // Update label to show input field
                const label = box.querySelector('.face-label');
                if (label) {
                    showNameInput(label, { ...face, person_id: null, person_name: null });
                }

                try {
                    await AppState.faces.unassign([face.id]);
                } catch (error) {
                    console.error('Failed to unidentify face:', error);
                    App.showError('Failed to unidentify face');
                    // Reload to restore correct state on error
                    const imageId = Fullscreen.state.currentId;
                    if (imageId) loadFacesForImage(imageId);
                } finally {
                    suppressOverlayReload = false;
                }
            });
        } else {
            // Unknown face: red button to suppress
            actionBtn.title = 'Remove face detection (not a real face)';
            actionBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                suppressFace(face.id, box);
            });
        }

        box.appendChild(actionBtn);

        // Create ignore button (assign to "-" person) - only for non-ignored faces
        const isIgnored = face.person_id && face.person_name === '-';
        let ignoreBtn = null;
        if (!isIgnored) {
            ignoreBtn = document.createElement('button');
            ignoreBtn.className = 'face-ignore-btn';
            ignoreBtn.innerHTML = '<span class="material-symbols-outlined">remove</span>';
            ignoreBtn.title = 'Move to ignored list';
            ignoreBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                // Suppress overlay reload - we update the DOM directly
                suppressOverlayReload = true;

                // Optimistic UI: update box to ignored styling
                box.classList.remove('unknown', 'known', 'focused');
                box.classList.add('known', 'ignored');

                // Remove the ignore button (no longer needed on ignored face)
                ignoreBtn.remove();

                // Update action button to unidentify style
                const existingActionBtn = box.querySelector('.face-delete-btn');
                if (existingActionBtn) {
                    existingActionBtn.classList.add('unidentify');
                    existingActionBtn.title = 'Remove identification (return to unknown)';
                }

                // Update label to show "-" (handle both known faces with span and unknown with input)
                const label = box.querySelector('.face-label');
                if (label) {
                    // Clear any existing content (input or name span)
                    label.innerHTML = '';
                    // Create name span showing "-"
                    const nameSpan = document.createElement('span');
                    nameSpan.className = 'face-name';
                    nameSpan.textContent = '-';
                    nameSpan.addEventListener('click', () => {
                        showNameInput(label, { ...face, person_id: 'ignored', person_name: '-' });
                    });
                    label.appendChild(nameSpan);
                }

                // Close any open autocomplete
                closeAutocomplete();

                try {
                    await AppState.faces.identify([face.id], '-');
                } catch (error) {
                    console.error('Failed to ignore face:', error);
                    App.showError('Failed to ignore face');
                    // Reload to restore correct state on error
                    const imageId = Fullscreen.state.currentId;
                    if (imageId) loadFacesForImage(imageId);
                } finally {
                    suppressOverlayReload = false;
                }
            });
            box.appendChild(ignoreBtn);
        }

        // Quick Match button (centered) - shows on all face types
        const quickMatchBtn = createQuickMatchButtonForOverlay(face.id, box, face);
        box.appendChild(quickMatchBtn);

        // Repel buttons if they would overlap on small bboxes
        // Layout: ignore (left, -10px), quickmatch (center), action (right, -10px)
        // Each button is 20px wide. Quickmatch is centered via CSS transform.
        // Overlap happens when width < 48px (need 10+4+20+4+10 from center to edges)
        const MIN_BUTTON_GAP = 4;
        const BUTTON_SIZE = 20;
        const BUTTON_OFFSET = 10;  // How far buttons extend beyond box edge
        // Minimum: half-button + gap + half-center-button on each side = 10+4+10 = 24 per side = 48 total
        const minWidthNeeded = (BUTTON_OFFSET + MIN_BUTTON_GAP + BUTTON_SIZE / 2) * 2;

        if (width < minWidthNeeded) {
            // Calculate how much each outer button needs to move outward
            const overflow = minWidthNeeded - width;
            const outerOffset = overflow / 2;

            // Move outer buttons further out, center button stays centered
            if (ignoreBtn) {
                ignoreBtn.style.left = `${-BUTTON_OFFSET - outerOffset}px`;
            }
            actionBtn.style.right = `${-BUTTON_OFFSET - outerOffset}px`;
            // Quick match stays centered (CSS handles it)
        }

        // Clamp buttons inward when face box is near the image edge so
        // buttons don't extend outside the visible overlay area
        const outerOffset = (width < minWidthNeeded) ? (minWidthNeeded - width) / 2 : 0;
        const btnEdge = 2; // minimum px from image edge

        // Top edge: all three buttons sit at top: -10px by default
        if (top < BUTTON_OFFSET + btnEdge) {
            const clampedTop = Math.max(btnEdge, top) - top + btnEdge;
            if (ignoreBtn) ignoreBtn.style.top = `${clampedTop}px`;
            actionBtn.style.top = `${clampedTop}px`;
            quickMatchBtn.style.top = `${clampedTop}px`;
        }

        // Left edge: ignore button extends left by BUTTON_OFFSET (+ outerOffset)
        if (ignoreBtn) {
            const btnLeft = left - BUTTON_OFFSET - outerOffset;
            if (btnLeft < 0) {
                ignoreBtn.style.left = `${-left + btnEdge}px`;
            }
        }

        // Right edge: action button extends right by BUTTON_OFFSET (+ outerOffset)
        const btnRight = left + width + BUTTON_OFFSET + outerOffset;
        if (btnRight > imgWidth) {
            actionBtn.style.right = `${-(imgWidth - left - width) + btnEdge}px`;
        }

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
            // Add focused class first - needed for ignored faces where label is
            // display:none until focused (can't focus elements inside hidden containers)
            box.classList.add('focused');

            const nameSpan = label.querySelector('.face-name');
            if (nameSpan) {
                nameSpan.click();
                // Focus will happen after showNameInput creates the input
                setTimeout(() => {
                    const input = label.querySelector('.face-input');
                    if (input) input.focus({ preventScroll: true });
                }, 0);
            } else {
                // For unknown faces, just focus the existing input
                const input = label.querySelector('.face-input');
                if (input) input.focus({ preventScroll: true });
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
                // Skip if label was removed from DOM (e.g., during overlay refresh)
                // This prevents committing partial input when refresh destroys the element
                if (!label.isConnected) return;

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
    async function showAutocomplete(input, query) {
        // Ensure people cache is fresh before searching
        // load() handles TTL internally - returns immediately if cache is valid
        await AppState.people.load();

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
            img.src = AppState.people.getThumbnailUrl(person.id);
            img.alt = '';
            img.onerror = () => { img.style.display = 'none'; };
            item.appendChild(img);

            // Add name
            const nameSpan = document.createElement('span');
            nameSpan.className = 'name';
            nameSpan.textContent = person.name;
            item.appendChild(nameSpan);

            // Handle mousedown - fires BEFORE blur, so we can update input value first
            item.addEventListener('mousedown', (e) => {
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

        // Post-render: adjust position if the autocomplete extends off-viewport.
        // Uses rAF so layout is finalized and getBoundingClientRect is accurate.
        requestAnimationFrame(() => {
            if (!activeAutocomplete) return;
            const rect = activeAutocomplete.getBoundingClientRect();

            // Bottom overflow: flip upward (above the label instead of below)
            if (rect.bottom > window.innerHeight) {
                activeAutocomplete.style.top = 'auto';
                activeAutocomplete.style.bottom = '100%';
                activeAutocomplete.style.marginTop = '0';
                activeAutocomplete.style.marginBottom = '2px';
            }

            // Right overflow: shift left by the overflow amount
            const reRect = activeAutocomplete.getBoundingClientRect();
            if (reRect.right > window.innerWidth) {
                const shift = reRect.right - window.innerWidth;
                activeAutocomplete.style.left = `${-shift}px`;
                activeAutocomplete.style.right = 'auto';
            }

            // Left overflow: shift right by the overflow amount
            const finalRect = activeAutocomplete.getBoundingClientRect();
            if (finalRect.left < 0) {
                const currentLeft = parseFloat(activeAutocomplete.style.left) || 0;
                activeAutocomplete.style.left = `${currentLeft - finalRect.left}px`;
                activeAutocomplete.style.right = 'auto';
            }
        });
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
        if (!Fullscreen.isOpen()) return;

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
        // Escape to cancel editing (stopPropagation prevents fullscreen from closing)
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
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
        // Suppress overlay reload during identify - we update the DOM directly
        suppressOverlayReload = true;

        const faceBox = label.closest('.face-box');
        const originalPersonId = face.person_id;
        const originalPersonName = face.person_name;
        const originalClasses = faceBox ? [...faceBox.classList] : [];

        if (name) {
            // =========================================================
            // OPTIMISTIC UI: Update DOM immediately, before API call
            // =========================================================
            face.person_id = 'pending';
            face.person_name = name;

            if (faceBox) {
                faceBox.classList.remove('unknown', 'ignored');
                faceBox.classList.add('known');
            }

            // Show name span instead of input
            label.innerHTML = '';
            const nameSpan = document.createElement('span');
            nameSpan.className = 'face-name';
            nameSpan.textContent = name;
            nameSpan.addEventListener('click', () => {
                showNameInput(label, face);
            });
            label.appendChild(nameSpan);

            // =========================================================
            // API CALL: Fire and handle result/error
            // AppState.faces.identify() does synchronous optimistic updates
            // and broadcasts immediately, so UI updates before this returns.
            // =========================================================
            callIdentifyBatchApi([faceId], name, faceId)
                .then(result => {
                    // AppState handles cache updates; nothing to do here on success
                })
                .catch(error => {
                    console.error('Failed to update face:', error);
                    App.showError('Failed to update face.');

                    // ROLLBACK: Restore original state
                    face.person_id = originalPersonId;
                    face.person_name = originalPersonName;

                    if (faceBox) {
                        faceBox.className = '';
                        for (const cls of originalClasses) {
                            faceBox.classList.add(cls);
                        }
                    }

                    // Restore input field
                    showNameInput(label, face);
                })
                .finally(() => {
                    suppressOverlayReload = false;
                });

        } else if (face.person_id) {
            // =========================================================
            // UNIDENTIFY: Update DOM, let AppState handle cache
            // =========================================================
            if (faceBox) {
                faceBox.classList.remove('known');
                faceBox.classList.add('unknown');

                // Also update the action button from green (unidentify) to red (suppress)
                const actionBtn = faceBox.querySelector('.face-delete-btn');
                if (actionBtn) {
                    actionBtn.classList.remove('unidentify');
                }
            }

            // AppState.faces.unassign() expects an array of face IDs
            AppState.faces.unassign([faceId])
                .then(() => {
                    // Success - AppState handled cache updates
                })
                .catch(error => {
                    console.error('Failed to unidentify face:', error);
                    App.showError('Failed to update face.');

                    // ROLLBACK DOM (AppState already rolled back cache)
                    if (faceBox) {
                        faceBox.className = '';
                        for (const cls of originalClasses) {
                            faceBox.classList.add(cls);
                        }
                        // Restore action button to green (unidentify) since face was known
                        const actionBtn = faceBox.querySelector('.face-delete-btn');
                        if (actionBtn) {
                            actionBtn.classList.add('unidentify');
                        }
                    }
                })
                .finally(() => {
                    suppressOverlayReload = false;
                });
        } else {
            // No name and no existing person - nothing to do
            suppressOverlayReload = false;
        }
    }

    /**
     * Suppress a face (mark as false positive) from fullscreen tagging mode.
     * Called when user clicks the X button on a face bounding box.
     * Delegates to AppState.faces.suppress() which handles cache updates.
     * The AppState subscription will trigger faces screen refresh if needed.
     *
     * @param {string} faceId - Face ID to suppress
     * @param {HTMLElement} faceBox - Face box DOM element (removed from overlay)
     */
    async function suppressFace(faceId, faceBox) {
        // Suppress overlay reload during suppress - we've already removed the box
        suppressOverlayReload = true;

        // Remove from fullscreen overlay immediately (optimistic UI)
        faceBox.remove();

        try {
            // Delegate to AppState - it handles cache updates and broadcasts
            await AppState.faces.suppress(faceId);
        } catch (error) {
            console.error('Failed to suppress face:', error);
            App.showError('Failed to remove face.');
            // Note: We don't restore the box on error - user can reload if needed
        } finally {
            suppressOverlayReload = false;
        }
    }

    // =========================================================================
    // QUICK MATCH CARD
    // =========================================================================

    /** @type {HTMLElement|null} Currently open quick match card */
    let quickMatchCard = null;

    /** @type {HTMLElement|null} Backdrop behind quick match card */
    let quickMatchBackdrop = null;

    /** @type {string|null} Face ID for currently open quick match card */
    let quickMatchFaceId = null;

    /** @type {function|null} Bound keydown handler for Escape */
    let quickMatchKeyHandler = null;

    /**
     * Show the Quick Match card for one or more faces.
     * Fetches top matches and displays them in a popup anchored to the face thumbnail.
     *
     * @param {string[]} faceIds - Face IDs to assign (first one used for matching)
     * @param {HTMLElement} anchor - Element to anchor the card to
     * @param {Object} options - Additional options
     * @param {function} [options.onAssign] - Callback when a match is selected (faceIds, personId, personName)
     */
    async function showQuickMatch(faceIds, anchor, options = {}) {
        // Close any existing card first
        hideQuickMatch();

        // Use first face for matching
        const primaryFaceId = faceIds[0];
        quickMatchFaceId = primaryFaceId;

        // Create backdrop immediately (before async call) to prevent race conditions
        const backdrop = document.createElement('div');
        backdrop.className = 'quick-match-backdrop';
        backdrop.addEventListener('click', () => {
            hideQuickMatch();
        });
        quickMatchBackdrop = backdrop;
        // Append to #app so it inherits theme CSS variables
        App.$('app').appendChild(backdrop);

        // Show backdrop immediately
        requestAnimationFrame(() => {
            backdrop.classList.add('visible');
        });

        // Set up Escape key handler early
        quickMatchKeyHandler = (e) => {
            if (e.key === 'Escape') {
                e.stopPropagation();
                e.preventDefault();
                hideQuickMatch();
            }
        };
        document.addEventListener('keydown', quickMatchKeyHandler, { capture: true });

        // Build and show the card immediately with source face + loading state,
        // then fill in matches when the API responds
        const card = document.createElement('div');
        card.className = 'quick-match-card';
        quickMatchCard = card;

        // Source face at top
        const sourceDiv = document.createElement('div');
        sourceDiv.className = 'quick-match-source';
        sourceDiv.title = 'Click to dismiss';

        const sourceImg = document.createElement('img');
        sourceImg.src = FaceThumbnails.getUrl(primaryFaceId);
        sourceImg.alt = 'This face';
        sourceDiv.appendChild(sourceImg);

        const sourceLabel = document.createElement('span');
        sourceLabel.className = 'quick-match-source-label';
        sourceLabel.textContent = faceIds.length > 1
            ? `Find match for ${faceIds.length} faces`
            : 'Find match for this face';
        sourceDiv.appendChild(sourceLabel);

        sourceDiv.addEventListener('click', (e) => {
            e.stopPropagation();
            hideQuickMatch();
        });

        card.appendChild(sourceDiv);

        // Placeholder while matches load — reuse empty style with ellipsis animation
        const loadingEl = document.createElement('div');
        loadingEl.className = 'quick-match-empty';
        loadingEl.textContent = 'Searching\u2026';
        card.appendChild(loadingEl);

        // Show card immediately (matches will appear when ready)
        App.$('app').appendChild(card);
        positionQuickMatchCard(card, anchor);
        requestAnimationFrame(() => {
            card.classList.add('visible');
        });

        // Fetch matches from backend (card is already visible)
        let matches = [];
        try {
            const response = await App.apiGet(`/faces/${primaryFaceId}/matches?limit=5`);
            matches = response.data || [];
        } catch (error) {
            console.error('Failed to fetch face matches:', error);
        }

        // Check if we were dismissed during the fetch (race condition)
        if (quickMatchFaceId !== primaryFaceId) {
            return;
        }

        // Replace loading placeholder with results
        loadingEl.remove();

        if (matches.length > 0) {
            const matchesDiv = document.createElement('div');
            matchesDiv.className = 'quick-match-matches';

            for (const match of matches) {
                const item = document.createElement('div');
                item.className = 'quick-match-item';

                const img = document.createElement('img');
                // Use person's preferred face thumbnail, not the matched face
                img.src = `/api/people/${match.person_id}/thumbnail`;
                img.alt = match.person_name;
                item.appendChild(img);

                const name = document.createElement('span');
                name.className = 'name';
                name.textContent = match.person_name;
                item.appendChild(name);

                item.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    hideQuickMatch();

                    // Assign all faces to this person
                    if (options.onAssign) {
                        options.onAssign(faceIds, match.person_id, match.person_name);
                    } else {
                        // Default: use AppState.faces.identify
                        try {
                            await AppState.faces.identify(faceIds, match.person_name);
                        } catch (error) {
                            console.error('Failed to assign faces:', error);
                            App.showError('Failed to assign faces');
                        }
                    }
                });

                matchesDiv.appendChild(item);
            }

            card.appendChild(matchesDiv);
        } else {
            // No matches
            const empty = document.createElement('div');
            empty.className = 'quick-match-empty';
            empty.textContent = 'No similar faces found';
            card.appendChild(empty);
        }

        // Reposition now that card content has changed size
        positionQuickMatchCard(card, anchor);
    }

    /**
     * Position the Quick Match card centered over the anchor, respecting viewport bounds.
     * @param {HTMLElement} card - The card element
     * @param {HTMLElement} anchor - The anchor element
     */
    function positionQuickMatchCard(card, anchor) {
        const anchorRect = anchor.getBoundingClientRect();
        const cardRect = card.getBoundingClientRect();

        // Target: centered horizontally over anchor, above it vertically
        let left = anchorRect.left + (anchorRect.width / 2) - (cardRect.width / 2);
        let top = anchorRect.top - cardRect.height - 8;

        // If card would go above viewport, position below anchor instead
        if (top < 8) {
            top = anchorRect.bottom + 8;
        }

        // Keep within horizontal viewport bounds
        const padding = 8;
        if (left < padding) {
            left = padding;
        } else if (left + cardRect.width > window.innerWidth - padding) {
            left = window.innerWidth - cardRect.width - padding;
        }

        // Keep within vertical viewport bounds
        if (top + cardRect.height > window.innerHeight - padding) {
            top = window.innerHeight - cardRect.height - padding;
        }

        card.style.left = `${left}px`;
        card.style.top = `${top}px`;
    }

    /**
     * Repel face card buttons if they would overlap on small thumbnails.
     * Moves outer buttons further out while keeping center button centered.
     *
     * @param {number} cardWidth - Width of the card/thumbnail in pixels
     * @param {HTMLElement|null} leftBtn - Left button (ignore), or null
     * @param {HTMLElement} centerBtn - Center button (quickmatch)
     * @param {HTMLElement} rightBtn - Right button (suppress/unassign)
     */
    function repelFaceCardButtons(cardWidth, leftBtn, centerBtn, rightBtn) {
        const BUTTON_SIZE = 22;      // Face card buttons are 22px
        const BUTTON_INSET = 4;      // 0.25rem = 4px from edge
        const MIN_GAP = 4;           // Minimum gap between buttons

        // Calculate minimum width needed:
        // Left side: inset + button + gap + half of center button
        // Right side: same
        // Total: 2 * (4 + 22 + 4 + 11) = 2 * 41 = 82px for 3 buttons
        // For 2 buttons: inset + button + gap + half-center on right side only matters
        const numButtons = leftBtn ? 3 : 2;
        const halfCenter = BUTTON_SIZE / 2;
        const minWidthNeeded = numButtons === 3
            ? 2 * (BUTTON_INSET + BUTTON_SIZE + MIN_GAP + halfCenter)
            : BUTTON_INSET + BUTTON_SIZE + MIN_GAP + halfCenter + halfCenter + MIN_GAP + BUTTON_SIZE + BUTTON_INSET;

        if (cardWidth < minWidthNeeded) {
            const overflow = minWidthNeeded - cardWidth;
            const outerOffset = overflow / 2;

            // Move outer buttons further out (negative position moves them outside card)
            if (leftBtn) {
                leftBtn.style.left = `${BUTTON_INSET - outerOffset}px`;
            }
            rightBtn.style.right = `${BUTTON_INSET - outerOffset}px`;
            // Center button stays centered (CSS handles it)
        }
    }

    /**
     * Hide and remove the Quick Match card and backdrop.
     */
    function hideQuickMatch() {
        if (!quickMatchCard) return;

        const card = quickMatchCard;
        const backdrop = quickMatchBackdrop;
        quickMatchCard = null;
        quickMatchBackdrop = null;
        quickMatchFaceId = null;

        // Remove keydown handler
        if (quickMatchKeyHandler) {
            document.removeEventListener('keydown', quickMatchKeyHandler, { capture: true });
            quickMatchKeyHandler = null;
        }

        // Animate out
        card.classList.remove('visible');
        card.classList.add('closing');
        if (backdrop) {
            backdrop.classList.remove('visible');
            backdrop.classList.add('closing');
        }

        setTimeout(() => {
            card.remove();
            if (backdrop) backdrop.remove();
        }, 100);
    }

    /**
     * Create a Quick Match button for a face card.
     * @param {string} faceId - Face ID
     * @param {HTMLElement} card - Card element (used as anchor)
     * @param {Object} selection - GridSelection instance (optional)
     * @returns {HTMLElement}
     */
    function createQuickMatchButton(faceId, card, selection) {
        const btn = document.createElement('button');
        btn.className = 'face-card-quickmatch';
        btn.title = 'Find matching person';
        // Use auto_awesome icon with magic unicode fallback
        btn.innerHTML = '<span class="material-symbols-outlined">auto_awesome</span>';

        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();

            // Selection model: if this face is selected, apply to all selected.
            // The clicked face must be first — showQuickMatch uses faceIds[0]
            // as the primary face for matching.
            let faceIds;
            if (selection && selection.isSelected(faceId)) {
                const selected = selection.getSelected();
                faceIds = [faceId, ...selected.filter(id => id !== faceId)];
            } else {
                faceIds = [faceId];
            }

            await showQuickMatch(faceIds, card.querySelector('.face-card-thumb') || card);
        });

        return btn;
    }

    /**
     * Create a Quick Match button for a fullscreen overlay face box.
     * @param {string} faceId - Face ID
     * @param {HTMLElement} box - Face box element (used as anchor)
     * @param {Object} face - Face object
     * @returns {HTMLElement}
     */
    function createQuickMatchButtonForOverlay(faceId, box, face) {
        const btn = document.createElement('button');
        btn.className = 'face-quickmatch-btn';
        btn.title = 'Find matching person';
        btn.innerHTML = '<span class="material-symbols-outlined">auto_awesome</span>';

        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            e.preventDefault();
            await showQuickMatch([faceId], box, {
                onAssign: async (faceIds, personId, personName) => {
                    // Update the face box label (only one face in fullscreen overlay)
                    const label = box.querySelector('.face-label');
                    if (label) {
                        // Clear and show new name
                        label.innerHTML = '';
                        const nameSpan = document.createElement('span');
                        nameSpan.className = 'face-name';
                        nameSpan.textContent = personName;
                        nameSpan.addEventListener('click', () => {
                            showNameInput(label, { ...face, person_id: personId, person_name: personName });
                        });
                        label.appendChild(nameSpan);
                    }

                    // Update box styling
                    box.classList.remove('unknown', 'ignored');
                    box.classList.add('known');

                    // Call API
                    suppressOverlayReload = true;
                    try {
                        await AppState.faces.identify(faceIds, personName);
                    } catch (error) {
                        console.error('Failed to assign face:', error);
                        App.showError('Failed to assign face');
                        // Reload overlay on error
                        const imageId = Fullscreen.state.currentId;
                        if (imageId) loadFacesForImage(imageId);
                    } finally {
                        suppressOverlayReload = false;
                    }
                }
            });
        });

        return btn;
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
        // Render faces on overlay from provided data (for optimistic updates)
        renderFaceOverlay: renderFaces,
    };

})();
