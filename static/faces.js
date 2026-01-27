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
    // INITIALIZATION
    // =========================================================================

    /**
     * Initialize the faces module.
     * Called when DOM is ready.
     */
    function init() {
        // Get DOM references
        faceOverlay = document.getElementById('face-overlay');
        fullscreenContainer = document.getElementById('fullscreen-container');
        fullscreenImage = document.getElementById('fullscreen-image');
        btnFaceTagging = document.getElementById('btn-face-tagging');
        btnFaces = document.getElementById('btn-faces');

        // Check if face detection is enabled
        loadFaceDetectionConfig();

        // Set up event listeners
        setupEventListeners();

        // Listen for screen changes
        App.on('screenChanged', handleScreenChange);

        // Listen for image changes in fullscreen
        App.on('fullscreenImageChanged', handleFullscreenImageChange);
    }

    /**
     * Load face detection config from backend.
     */
    async function loadFaceDetectionConfig() {
        try {
            const response = await App.api('/api/status');
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
                // TODO: Navigate to faces screen (Phase 4)
                console.log('Faces screen not implemented yet');
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

    // =========================================================================
    // TAGGING MODE
    // =========================================================================

    /**
     * Toggle face tagging mode on/off.
     */
    function toggleTaggingMode() {
        if (!faceDetectionEnabled) return;

        taggingMode = !taggingMode;

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
        } else {
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
            const faces = await App.api(`/api/images/${imageId}/faces`);
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
        if (!faceOverlay || !fullscreenImage) {
            return;
        }

        clearFaceOverlay();

        // Wait for image to be loaded to get dimensions
        if (!fullscreenImage.complete) {
            fullscreenImage.addEventListener('load', () => renderFaces(faces), { once: true });
            return;
        }

        // Get image dimensions and position
        const imgRect = fullscreenImage.getBoundingClientRect();
        const containerRect = fullscreenContainer.getBoundingClientRect();

        // Calculate image offset within container
        const offsetX = imgRect.left - containerRect.left;
        const offsetY = imgRect.top - containerRect.top;

        for (const face of faces) {
            const faceBox = createFaceBox(face, imgRect, offsetX, offsetY);
            faceOverlay.appendChild(faceBox);
        }
    }

    /**
     * Create a face bounding box element.
     * @param {Object} face - Face object from API
     * @param {DOMRect} imgRect - Image bounding rectangle
     * @param {number} offsetX - X offset of image in container
     * @param {number} offsetY - Y offset of image in container
     * @returns {HTMLElement}
     */
    function createFaceBox(face, imgRect, offsetX, offsetY) {
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
        const left = offsetX + (face.box_x * imgRect.width);
        const top = offsetY + (face.box_y * imgRect.height);
        const width = face.box_w * imgRect.width;
        const height = face.box_h * imgRect.height;

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
        const label = createFaceLabel(face, top, imgRect.height);
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
            const people = await App.api('/api/people');
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
                const result = await App.api(`/api/faces/${faceId}/identify`, {
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
                await App.api(`/api/faces/${faceId}/unidentify`, {
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
            await App.api(`/api/faces/${faceId}/suppress`, {
                method: 'POST',
            });

            // Remove face box from overlay
            faceBox.remove();

            // Invalidate people cache
            peopleCacheTime = 0;
        } catch (error) {
            console.error('Failed to suppress face:', error);
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
        toggleTaggingMode,
        loadFacesForImage,
        clearFaceOverlay,
        refreshPeopleCache,
    };

})();
