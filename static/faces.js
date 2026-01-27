/**
 * Face tagging module for Imaginary.
 *
 * Handles face detection overlay rendering in fullscreen view,
 * name input fields with autocomplete, and face identification.
 *
 * @module faces
 */

/* global App */

(function () {
    'use strict';

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {boolean} Whether face tagging mode is active */
    let taggingMode = false;

    /** @type {boolean} Whether face detection is enabled in config */
    let faceDetectionEnabled = true;

    /** @type {Array<Object>} Cached list of all people for autocomplete */
    let peopleCache = [];

    /** @type {number} Timestamp of last people cache update */
    let peopleCacheTime = 0;

    /** @type {number} Cache TTL in milliseconds */
    const PEOPLE_CACHE_TTL = 30000;

    /** @type {HTMLElement|null} Currently focused face input */
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

    /** @type {number} Thumbnail size for faces screen (pixels) */
    let facesThumbnailSize = 100;

    /** @type {boolean} Show only unknown faces */
    let showOnlyUnknowns = false;

    /** @type {boolean} Sort direction (true = ascending) */
    let sortAscending = true;

    /** @type {Array<Object>} All faces loaded from API */
    let allFaces = [];

    /** @type {boolean} Whether faces screen is currently loading */
    let isLoading = false;

    /** @type {boolean} Whether faces need to be reloaded on next enter */
    let needsRefresh = true;

    /** @type {number} Saved scroll position for restoration */
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

    /** @type {HTMLInputElement} */
    let facesOnlyUnknowns;

    /** @type {HTMLButtonElement} */
    let btnFacesSortDirection;

    /** @type {Object|null} GridSelection instance for faces screen */
    let facesSelection = null;

    /** @type {Array<Object>} Currently displayed faces (after filtering/sorting) */
    let displayedFaces = [];

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
        facesOnlyUnknowns = document.getElementById('faces-only-unknowns');
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

        // Initialize GridSelection for faces screen
        initFacesSelection();

        // Register the faces screen module
        registerFacesModule();
    }

    /**
     * Load face detection config from backend.
     */
    async function loadFaceDetectionConfig() {
        try {
            const response = await App.api('/status');
            if (response && response.face_detection_enabled !== undefined) {
                faceDetectionEnabled = response.face_detection_enabled;
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

        // Only unknowns checkbox
        if (facesOnlyUnknowns) {
            facesOnlyUnknowns.addEventListener('change', () => {
                showOnlyUnknowns = facesOnlyUnknowns.checked;
                renderFacesGrid();
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
    }

    /**
     * Initialize GridSelection for faces screen.
     * Creates a simple grid adapter since we don't use VirtualGrid.
     */
    function initFacesSelection() {
        if (!facesGrid || typeof GridSelection === 'undefined') return;

        // Create a simple adapter that provides the methods GridSelection needs
        const gridAdapter = {
            _config: {
                container: facesGrid
            },
            _innerContainer: facesGrid,

            /**
             * Update visual class on an item.
             */
            setItemClass(id, className, value) {
                const item = facesGrid.querySelector(`.face-card[data-id="${id}"]`);
                if (item) {
                    item.classList.toggle(className, value);
                }
            },

            /**
             * Scroll to an item by index.
             */
            scrollTo(index) {
                if (index < 0 || index >= displayedFaces.length) return;
                const id = displayedFaces[index].id;
                const item = facesGrid.querySelector(`.face-card[data-id="${id}"]`);
                if (item) {
                    item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
                }
            },

            /**
             * Get approximate items per row (based on grid layout).
             */
            getItemsPerRow() {
                const firstCard = facesGrid.querySelector('.face-card');
                if (!firstCard) return 1;
                const gridWidth = facesGrid.clientWidth;
                const cardWidth = firstCard.offsetWidth + 8; // 8px gap
                return Math.max(1, Math.floor(gridWidth / cardWidth));
            },

            /**
             * Get approximate visible rows.
             */
            getVisibleRows() {
                const firstCard = facesGrid.querySelector('.face-card');
                if (!firstCard) return 1;
                const gridHeight = facesGrid.clientHeight;
                const cardHeight = firstCard.offsetHeight + 8; // 8px gap
                return Math.max(1, Math.floor(gridHeight / cardHeight));
            }
        };

        facesSelection = GridSelection.create({
            grid: gridAdapter,
            getItems: () => displayedFaces,
            getItemId: (face) => face.id,
            itemSelector: '.face-card',
            selectedClass: 'selected',
            onSelectionChanged: handleFacesSelectionChanged,
            onItemActivated: handleFaceActivated,
            onDeleteRequested: null, // Could add face suppression here
            enableKeyboard: true,
            enableDragBox: true,
            enableLongPress: true
        });
    }

    /**
     * Handle selection change in faces grid.
     * @param {Array<string>} selectedIds - Selected face IDs
     */
    function handleFacesSelectionChanged(selectedIds) {
        // Could update UI to show selection count, enable/disable buttons, etc.
        // For now, just let the selection state be managed by GridSelection
    }

    /**
     * Handle face activation (Enter key or double-click).
     * Navigate to fullscreen with tagging mode.
     * @param {string} faceId - Activated face ID
     */
    function handleFaceActivated(faceId) {
        // Don't activate if user is interacting with an input field
        // (e.g., double-clicking to select text in the name input)
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
     * Register the faces screen module with App.
     */
    function registerFacesModule() {
        App.registerModule('faces', {
            onEnter() {
                if (needsRefresh) {
                    loadAllFaces(); // Selection is bound after load completes
                } else {
                    // Restore scroll position
                    if (facesGrid) {
                        facesGrid.scrollTop = savedScrollTop;
                    }
                    // Bind selection handlers (grid already has content)
                    if (facesSelection) {
                        facesSelection.bind();
                    }
                }
            },
            onLeave() {
                // Save scroll position
                if (facesGrid) {
                    savedScrollTop = facesGrid.scrollTop;
                }
                // Unbind selection handlers
                if (facesSelection) {
                    facesSelection.unbind();
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
     * Load all faces from the API.
     */
    async function loadAllFaces() {
        if (isLoading) return;
        isLoading = true;

        // Unbind selection during reload
        if (facesSelection) {
            facesSelection.unbind();
        }

        // Show loading state
        if (facesGrid) facesGrid.innerHTML = '';
        if (facesEmpty) facesEmpty.hidden = true;
        if (facesLoading) facesLoading.hidden = false;

        try {
            // Load all faces in a single API call
            allFaces = await App.api('/faces') || [];
            needsRefresh = false;
            renderFacesGrid();

            // Bind selection after grid is rendered
            if (facesSelection) {
                facesSelection.bind();
            }
        } catch (error) {
            console.error('Failed to load faces:', error);
            App.showError('Failed to load faces.');
            if (facesEmpty) facesEmpty.hidden = false;
        } finally {
            isLoading = false;
            if (facesLoading) facesLoading.hidden = true;
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
     * Render the faces grid with sections.
     */
    function renderFacesGrid() {
        if (!facesGrid) return;

        // Clear selection when re-rendering
        if (facesSelection) {
            facesSelection.clear();
        }

        // Clear grid
        facesGrid.innerHTML = '';

        // Filter faces
        let faces = [...allFaces];
        if (showOnlyUnknowns) {
            faces = faces.filter(f => !f.person_id);
        }

        // Sort faces - known faces by name, unknown faces by image timestamp
        faces.sort((a, b) => {
            // Known faces first
            if (a.person_name && !b.person_name) return -1;
            if (!a.person_name && b.person_name) return 1;

            // Both known - sort by name
            if (a.person_name && b.person_name) {
                const cmp = a.person_name.localeCompare(b.person_name);
                return sortAscending ? cmp : -cmp;
            }

            // Both unknown - sort by face ID (proxy for creation time)
            const cmp = (a.id || '').localeCompare(b.id || '');
            return sortAscending ? cmp : -cmp;
        });

        // Update displayedFaces for GridSelection (unknown faces only for selection)
        displayedFaces = faces.filter(f => !f.person_id);

        // Check for empty state
        if (faces.length === 0) {
            displayedFaces = [];
            if (facesEmpty) facesEmpty.hidden = false;
            return;
        }
        if (facesEmpty) facesEmpty.hidden = true;

        // Group faces by known/unknown
        const knownFaces = faces.filter(f => f.person_id);
        const unknownFaces = faces.filter(f => !f.person_id);

        // Render known faces section
        if (knownFaces.length > 0 && !showOnlyUnknowns) {
            const section = createFacesSection('Known', knownFaces.length, 'known');
            const grid = section.querySelector('.faces-section-grid');

            // Group by person for known faces
            const byPerson = new Map();
            for (const face of knownFaces) {
                if (!byPerson.has(face.person_id)) {
                    byPerson.set(face.person_id, []);
                }
                byPerson.get(face.person_id).push(face);
            }

            // Get unique people and sort by name
            const people = Array.from(byPerson.entries()).map(([personId, personFaces]) => ({
                id: personId,
                name: personFaces[0].person_name,
                faces: personFaces,
                preferredFace: personFaces.find(f => f.is_preferred) || personFaces[0],
            }));

            people.sort((a, b) => {
                const cmp = a.name.localeCompare(b.name);
                return sortAscending ? cmp : -cmp;
            });

            for (const person of people) {
                const card = createPersonCard(person);
                grid.appendChild(card);
            }

            facesGrid.appendChild(section);
        }

        // Render unknown faces section
        if (unknownFaces.length > 0) {
            const section = createFacesSection('Unknown', unknownFaces.length, 'unknown');
            const grid = section.querySelector('.faces-section-grid');

            for (const face of unknownFaces) {
                const card = createFaceCard(face);
                grid.appendChild(card);
            }

            facesGrid.appendChild(section);
        }
    }

    /**
     * Create a faces section element.
     * @param {string} title - Section title
     * @param {number} count - Number of faces
     * @param {string} type - 'known' or 'unknown'
     * @returns {HTMLElement}
     */
    function createFacesSection(title, count, type) {
        const section = document.createElement('div');
        section.className = `faces-section ${type}`;

        const header = document.createElement('div');
        header.className = 'faces-section-header';

        const titleEl = document.createElement('h3');
        titleEl.className = `faces-section-title ${type}`;
        titleEl.textContent = title;
        header.appendChild(titleEl);

        const countEl = document.createElement('span');
        countEl.className = 'faces-section-count';
        countEl.textContent = `(${count})`;
        header.appendChild(countEl);

        section.appendChild(header);

        const grid = document.createElement('div');
        grid.className = 'faces-section-grid';
        section.appendChild(grid);

        return section;
    }

    /**
     * Create a person card (for known faces - shows representative face).
     * @param {Object} person - Person object with faces array
     * @returns {HTMLElement}
     */
    function createPersonCard(person) {
        const card = document.createElement('div');
        card.className = 'face-card';
        card.dataset.personId = person.id;

        const thumb = document.createElement('div');
        thumb.className = 'face-card-thumb';

        const img = document.createElement('img');
        img.src = `/api/people/${person.id}/thumbnail`;
        img.alt = person.name;
        img.loading = 'lazy';
        thumb.appendChild(img);

        // Add preferred indicator if multiple faces
        if (person.faces.length > 1) {
            const preferred = document.createElement('div');
            preferred.className = 'face-card-preferred';
            preferred.innerHTML = '<span class="material-symbols-outlined">star</span>';
            preferred.title = `${person.faces.length} faces`;
            thumb.appendChild(preferred);
        }

        card.appendChild(thumb);

        const name = document.createElement('div');
        name.className = 'face-card-name';
        name.textContent = person.name;
        card.appendChild(name);

        // Double-click to navigate to gallery filtered by this person
        card.addEventListener('dblclick', () => {
            App.setFilter({ people: [{ id: person.id, name: person.name }] });
            App.navigateTo('gallery');
        });

        return card;
    }

    /**
     * Create a face card (for unknown faces - editable name).
     * @param {Object} face - Face object
     * @returns {HTMLElement}
     */
    function createFaceCard(face) {
        const card = document.createElement('div');
        card.className = 'face-card';
        card.dataset.id = face.id; // Use data-id for GridSelection compatibility

        const thumb = document.createElement('div');
        thumb.className = 'face-card-thumb';

        const img = document.createElement('img');
        img.src = `/api/faces/${face.id}/thumbnail`;
        img.alt = 'Unknown face';
        img.loading = 'lazy';
        thumb.appendChild(img);

        card.appendChild(thumb);

        // Create editable name input
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'face-card-input';
        input.placeholder = 'Enter name...';
        input.value = '';
        input.dataset.faceId = face.id;

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

        // Note: Double-click is handled by GridSelection's onItemActivated

        return card;
    }

    /**
     * Show autocomplete for face card input.
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     * @param {HTMLElement} card - Parent card element
     */
    async function showCardAutocomplete(input, query, card) {
        // Refresh people cache if stale
        if (Date.now() - peopleCacheTime > PEOPLE_CACHE_TTL) {
            await refreshPeopleCache();
        }

        // Remove existing autocomplete
        const existing = card.querySelector('.face-card-autocomplete');
        if (existing) existing.remove();

        // Filter people by query
        const q = query.toLowerCase().trim();
        if (!q) return;

        const matches = peopleCache.filter(p => p.name.toLowerCase().includes(q));
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
            img.src = `/api/people/${person.id}/thumbnail`;
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
     * Commit a name change for selected faces.
     * If multiple faces are selected, applies the name to all of them.
     * The face where the user typed becomes the "preferred" face for that person.
     *
     * @param {string} typedFaceId - Face ID where user typed the name
     * @param {string} name - Name to assign
     * @param {HTMLElement} card - Card element where user typed
     */
    async function commitSelectedFacesName(typedFaceId, name, card) {
        if (!name) return;

        // Get selected faces, or just the typed face if none selected
        let faceIds = facesSelection ? facesSelection.getSelected() : [];

        // If the typed face isn't in the selection, or no selection, just use the typed face
        if (faceIds.length === 0 || !faceIds.includes(typedFaceId)) {
            faceIds = [typedFaceId];
        }

        try {
            // Identify all selected faces with the same name
            // The first one (typed face) should be marked as preferred
            const results = await Promise.all(faceIds.map((faceId, index) =>
                App.api(`/faces/${faceId}/identify`, {
                    method: 'POST',
                    body: JSON.stringify({
                        name,
                        // The face where user typed becomes preferred (unless one already exists)
                        set_preferred: faceId === typedFaceId
                    }),
                })
            ));

            // Check if any succeeded
            const anySuccess = results.some(r => r && r.success);
            if (anySuccess) {
                // Invalidate cache and reload
                peopleCacheTime = 0;
                loadAllFaces();
            }
        } catch (error) {
            console.error('Failed to identify faces:', error);
            App.showError(`Failed to identify ${faceIds.length > 1 ? 'faces' : 'face'}.`);
        }
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
            const result = await App.api(`/faces/${faceId}/identify`, {
                method: 'POST',
                body: JSON.stringify({ name }),
            });

            if (result && result.success) {
                // Invalidate cache and reload
                peopleCacheTime = 0;
                loadAllFaces();
            }
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
            const faces = await App.api(`/images/${imageId}/faces`);
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

        // Handle focus
        input.addEventListener('focus', () => {
            focusedInput = input;
            const faceBox = label.closest('.face-box');
            if (faceBox) {
                faceBox.classList.add('focused');
            }
        });

        // Handle blur (commit changes)
        input.addEventListener('blur', () => {
            const faceBox = label.closest('.face-box');
            if (faceBox) {
                faceBox.classList.remove('focused');
            }
            focusedInput = null;
            closeAutocomplete();

            // Commit the change
            const newName = input.value.trim();
            const originalName = input.dataset.originalName;

            if (newName !== originalName) {
                commitNameChange(face.id, newName, label, face);
            }
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
     * @param {HTMLInputElement} input - Input element
     * @param {string} query - Search query
     */
    async function showAutocomplete(input, query) {
        // Refresh people cache if stale
        if (Date.now() - peopleCacheTime > PEOPLE_CACHE_TTL) {
            await refreshPeopleCache();
        }

        // Filter people by query
        const q = query.toLowerCase().trim();
        const matches = q
            ? peopleCache.filter(p => p.name.toLowerCase().includes(q))
            : peopleCache;

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

            // Add thumbnail
            const img = document.createElement('img');
            img.src = `/api/people/${person.id}/thumbnail`;
            img.alt = '';
            img.onerror = () => { img.style.display = 'none'; };
            item.appendChild(img);

            // Add name
            const nameSpan = document.createElement('span');
            nameSpan.className = 'name';
            nameSpan.textContent = person.name;
            item.appendChild(nameSpan);

            // Handle click
            item.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                input.value = person.name;
                closeAutocomplete();
                input.focus();
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
     */
    async function refreshPeopleCache() {
        try {
            const people = await App.api('/people');
            peopleCache = people || [];
            peopleCacheTime = Date.now();
        } catch (error) {
            console.error('Failed to refresh people cache:', error);
        }
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
                // Identify face with name
                const result = await App.api(`/faces/${faceId}/identify`, {
                    method: 'POST',
                    body: JSON.stringify({ name }),
                });

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

                    // Invalidate people cache
                    peopleCacheTime = 0;
                }
            } else if (face.person_id) {
                // Unidentify face
                await App.api(`/faces/${faceId}/unidentify`, {
                    method: 'POST',
                });

                face.person_id = null;
                face.person_name = null;

                // Update box class
                const faceBox = label.closest('.face-box');
                if (faceBox) {
                    faceBox.classList.remove('known');
                    faceBox.classList.add('unknown');
                }

                // Invalidate people cache
                peopleCacheTime = 0;
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
     * Suppress a face (mark as false positive).
     * @param {string} faceId - Face ID
     * @param {HTMLElement} faceBox - Face box element
     */
    async function suppressFace(faceId, faceBox) {
        try {
            await App.api(`/faces/${faceId}/suppress`, {
                method: 'POST',
            });

            // Remove face box from overlay
            faceBox.remove();

            // Invalidate people cache
            peopleCacheTime = 0;
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
