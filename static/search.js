/**
 * @fileoverview Search and filter screen module for the Imaginary application.
 *
 * This module handles the Search screen where users create filters to narrow
 * down the gallery view. It registers with the core App module and
 * communicates filter state back to the Gallery module.
 *
 * RESPONSIBILITIES:
 *
 * Text Search:
 *   - Text input field for searching image descriptions
 *   - Searches use OpenCLIP semantic similarity, not just keyword matching
 *   - Matches against user-added descriptions
 *   - Results are ranked by semantic relevance when using content sort
 *
 * Date Range Filter:
 *   - Start date and end date picker inputs
 *   - Filters images by their "best guess" timestamp
 *   - If only start date set, shows images from that date onward
 *   - If only end date set, shows images up to that date
 *   - If both dates are the same, filters to that exact date
 *   - Date pickers use native browser date input
 *
 * Rating Filter:
 *   - Text input for entering emoji ratings to filter by
 *   - Emoji picker button opens emoji selection dialog
 *   - Multiple emoji can be entered to match images with any of those ratings
 *   - Matches images whose rating contains any of the specified emoji
 *
 * Emoji Picker:
 *   - Grid of common rating emoji (stars, hearts, thumbs, etc.)
 *   - Clicking an emoji adds it to the rating filter input
 *   - Picker dialog is shared with Gallery info panel (managed by core)
 *
 * Filter Application:
 *   - "Apply Filter" button activates the filter and returns to Gallery
 *   - Gallery receives filter criteria and updates its display
 *   - "Clear Filter" button resets all filter fields
 *   - Filter state is stored in App state for persistence during session
 *
 * Filter Indicator:
 *   - When a filter is active, the filter toolbar button shows active state
 *   - Clicking filter button when filter is active clears filter (from Gallery)
 *   - Filter criteria are preserved when navigating away and back to Search
 *
 * Validation:
 *   - Validates date range (start not after end)
 *   - Shows validation feedback for invalid inputs
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Populates form fields from current filter state
 *   - onLeave(): Optionally auto-applies filter if fields have changed
 *
 * @module search
 * @requires core
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE

   Search module registration, state, and lifecycle hooks.
   ========================================================================== */

/**
 * Search/filter screen module.
 * @namespace
 */
const Search = {
    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * Selected people for the filter.
     * @type {Array<Object>}
     * @private
     */
    _selectedPeople: [],

    /**
     * Get all people sorted by name.
     * Delegates to AppState.people with sorting applied.
     * @returns {Array<Object>} Sorted people list
     * @private
     */
    _getAllPeopleSorted() {
        return AppState.people.getAll().sort((a, b) =>
            a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
        );
    },

    /**
     * Whether face detection is enabled.
     * @type {boolean}
     * @private
     */
    _faceDetectionEnabled: true,

    /**
     * Initialises the search module.
     * Called once during app startup.
     */
    init() {
        // Cache DOM elements
        this._els = {
            textInput: App.$('filter-text'),
            similaritySlider: App.$('filter-similarity'),
            similarityValue: App.$('similarity-value'),
            dateStart: App.$('filter-date-start'),
            dateEnd: App.$('filter-date-end'),
            ratingInput: App.$('filter-rating'),
            emojiBtn: App.$('btn-emoji-picker'),
            applyBtn: App.$('btn-apply-filter'),
            clearBtn: App.$('btn-clear-filter'),
            // People filter elements
            peopleGroup: App.$('filter-people-group'),
            peopleChips: App.$('filter-people-chips'),
            peoplePickerBtn: App.$('btn-people-picker'),
            // People picker dialog elements
            peopleDialog: App.$('dialog-people-picker'),
            peopleSearch: App.$('people-picker-search'),
            peopleAvailable: App.$('people-picker-available'),
            peopleSelected: App.$('people-picker-selected'),
            peopleDoneBtn: App.$('dialog-people-done'),
            peopleCancelBtn: App.$('dialog-people-cancel')
        };

        // Check face detection status
        this._loadFaceDetectionStatus();

        // Bind button events
        this._bindEvents();

        // Subscribe to AppState.people for instant dialog updates
        AppState.people.onChanged(() => {
            // Re-render people picker if dialog is open
            if (this._els.peopleDialog?.open) {
                this._renderPeopleAvailable();
            }
        });
    },

    /**
     * Called when entering the search screen.
     * Populates form fields from current filter state.
     */
    onEnter() {
        this._populateForm();
        // Focus the text input for quick typing
        this._els.textInput.focus();

        // Bind Escape key to return to gallery
        this._escapeHandler = (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                App.showGallery();
            }
        };
        document.addEventListener('keydown', this._escapeHandler);
    },

    /**
     * Called when leaving the search screen.
     */
    onLeave() {
        // Remove Escape key handler
        if (this._escapeHandler) {
            document.removeEventListener('keydown', this._escapeHandler);
            this._escapeHandler = null;
        }
    },

    /**
     * Binds event listeners for form controls.
     * @private
     */
    _bindEvents() {
        // Apply filter button
        this._els.applyBtn.addEventListener('click', () => this._applyFilter());

        // Clear filter button
        this._els.clearBtn.addEventListener('click', () => this._clearFilter());

        // Similarity slider - update displayed value and sync with gallery slider
        this._els.similaritySlider.addEventListener('input', () => {
            const value = this._els.similaritySlider.value;
            this._els.similarityValue.textContent = value + '%';

            // Sync gallery toolbar slider if it exists
            const gallerySimilaritySlider = App.$('gallery-similarity-slider');
            const gallerySimilarityValue = App.$('gallery-similarity-value');
            if (gallerySimilaritySlider) {
                gallerySimilaritySlider.value = value;
            }
            if (gallerySimilarityValue) {
                gallerySimilarityValue.textContent = value + '%';
            }
        });

        // Add hover tooltip to similarity slider
        App.addSliderHoverTooltip(this._els.similaritySlider);

        // Emoji picker button
        this._els.emojiBtn.addEventListener('click', () => {
            App.showEmojiPicker((emoji) => {
                this._els.ratingInput.value += emoji;
                this._els.ratingInput.focus();
            });
        });

        // Allow Enter key to apply filter from text input
        this._els.textInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this._applyFilter();
            }
        });

        // People filter events
        if (this._els.peopleChips) {
            this._els.peopleChips.addEventListener('click', () => this._openPeoplePicker());
        }
        if (this._els.peoplePickerBtn) {
            this._els.peoplePickerBtn.addEventListener('click', () => this._openPeoplePicker());
        }

        // People picker dialog events
        if (this._els.peopleDoneBtn) {
            this._els.peopleDoneBtn.addEventListener('click', () => this._closePeoplePicker(true));
        }
        if (this._els.peopleCancelBtn) {
            this._els.peopleCancelBtn.addEventListener('click', () => this._closePeoplePicker(false));
        }
        if (this._els.peopleSearch) {
            this._els.peopleSearch.addEventListener('input', () => this._filterPeopleList());
        }
        // Keyboard handling for people picker dialog
        if (this._els.peopleDialog) {
            this._els.peopleDialog.addEventListener('keydown', (e) => {
                // Stop all key events from reaching the underlying page
                e.stopPropagation();

                if (e.key === 'Escape') {
                    e.preventDefault();
                    this._closePeoplePicker(false);
                } else if (e.key === 'Enter' && e.target !== this._els.peopleSearch) {
                    // Enter confirms (but not when typing in search)
                    e.preventDefault();
                    this._closePeoplePicker(true);
                }
            });
        }

        // Drag-and-drop for people picker panels
        this._setupPanelDropHandlers(this._els.peopleAvailable, 'available');
        this._setupPanelDropHandlers(this._els.peopleSelected, 'selected');
    },

    /**
     * Sets up drag-and-drop handlers for a people picker panel.
     * @param {HTMLElement} panel - The panel element
     * @param {string} panelType - 'available' or 'selected'
     * @private
     */
    _setupPanelDropHandlers(panel, panelType) {
        if (!panel) return;

        panel.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            panel.classList.add('drag-over');
        });

        panel.addEventListener('dragleave', (e) => {
            // Only remove class if leaving the panel entirely
            if (!panel.contains(e.relatedTarget)) {
                panel.classList.remove('drag-over');
            }
        });

        panel.addEventListener('drop', (e) => {
            e.preventDefault();
            panel.classList.remove('drag-over');

            const sourcePanel = e.dataTransfer.getData('text/panel');
            if (sourcePanel === panelType) return; // Dropped on same panel

            try {
                const person = JSON.parse(e.dataTransfer.getData('application/json'));
                if (panelType === 'selected') {
                    // Dropping on selected panel = add
                    if (!this._selectedPeople.some(p => p.id === person.id)) {
                        this._selectedPeople.push(person);
                    }
                } else {
                    // Dropping on available panel = remove
                    this._selectedPeople = this._selectedPeople.filter(p => p.id !== person.id);
                }
                this._renderPeopleAvailable();
                this._renderPeopleSelected();
            } catch (error) {
                console.error('Failed to parse dropped person data:', error);
            }
        });
    },

    /**
     * Loads face detection enabled status from the backend.
     * @private
     */
    async _loadFaceDetectionStatus() {
        try {
            // Use AppState.status - load if not already loaded
            let status = AppState.status.get();
            if (!status) {
                status = await AppState.status.load();
            }
            this._faceDetectionEnabled = status?.face_detection_enabled !== false;
        } catch (error) {
            // Default to enabled if can't reach backend
            this._faceDetectionEnabled = true;
        }
        this._updatePeopleFieldState();
    },

    /**
     * Updates the people field state based on face detection status.
     * @private
     */
    _updatePeopleFieldState() {
        if (this._els.peopleGroup) {
            if (this._faceDetectionEnabled) {
                this._els.peopleGroup.classList.remove('disabled');
                if (this._els.peoplePickerBtn) {
                    this._els.peoplePickerBtn.disabled = false;
                    this._els.peoplePickerBtn.title = 'Select people';
                }
            } else {
                this._els.peopleGroup.classList.add('disabled');
                if (this._els.peoplePickerBtn) {
                    this._els.peoplePickerBtn.disabled = true;
                    this._els.peoplePickerBtn.title = 'Face detection is disabled in settings';
                }
            }
        }
    },

    /* ----------------------------------------------------------------------
       PEOPLE PICKER DIALOG

       Open, close, and manage the people picker dialog.
       ---------------------------------------------------------------------- */

    /**
     * Opens the people picker dialog.
     * Shows immediately with cached data, loads fresh data in background.
     * @private
     */
    _openPeoplePicker() {
        if (!this._faceDetectionEnabled) return;
        if (!this._els.peopleDialog) return;

        // Clear search
        if (this._els.peopleSearch) {
            this._els.peopleSearch.value = '';
        }

        // Show dialog immediately with whatever cached data we have
        this._renderPeopleAvailable();
        this._renderPeopleSelected();
        this._els.peopleDialog.showModal();

        // Load fresh data in background - explicit re-render on completion
        // (The subscription also handles this, but this ensures it works even
        // if the subscription hasn't fired yet or if the cache was empty)
        AppState.people.load().then(() => {
            if (this._els.peopleDialog?.open) {
                this._renderPeopleAvailable();
            }
        }).catch(() => {
            // Ignore errors - we already showed what we have
        });
    },

    /**
     * Closes the people picker dialog.
     * @param {boolean} saveSelection - Whether to save the selection
     * @private
     */
    _closePeoplePicker(saveSelection) {
        if (!this._els.peopleDialog) return;

        if (saveSelection) {
            // Selection is already stored in _selectedPeople
            this._renderPeopleChips();
        }

        this._els.peopleDialog.close();
    },

    // _loadAllPeople() removed - now uses AppState.people.load()

    /**
     * Filters the available people list by search query.
     * @private
     */
    _filterPeopleList() {
        this._renderPeopleAvailable();
    },

    /**
     * Fuzzy/subsequence match - each char in query appears in order in target.
     * @param {string} query - Search query (lowercase)
     * @param {string} target - Target string (lowercase)
     * @returns {boolean}
     * @private
     */
    _fuzzyMatch(query, target) {
        if (!query) return true;
        let qi = 0;
        for (let ti = 0; ti < target.length && qi < query.length; ti++) {
            if (target[ti] === query[qi]) {
                qi++;
            }
        }
        return qi === query.length;
    },

    /**
     * Renders the available people panel in the picker.
     * @private
     */
    _renderPeopleAvailable() {
        if (!this._els.peopleAvailable) return;

        const query = (this._els.peopleSearch?.value || '').toLowerCase().trim();
        const selectedIds = new Set(this._selectedPeople.map(p => p.id));

        // Filter people by search query (fuzzy match), excluding already selected
        let filtered = this._getAllPeopleSorted().filter(p => !selectedIds.has(p.id));
        if (query) {
            filtered = filtered.filter(p => this._fuzzyMatch(query, p.name.toLowerCase()));
        }

        this._els.peopleAvailable.innerHTML = '';

        if (filtered.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'people-picker-empty';
            empty.textContent = query ? 'No matching people' : 'No people available';
            this._els.peopleAvailable.appendChild(empty);
            return;
        }

        for (const person of filtered) {
            const item = this._createPersonItem(person, 'available');
            this._els.peopleAvailable.appendChild(item);
        }
    },

    /**
     * Renders the selected people panel in the picker.
     * @private
     */
    _renderPeopleSelected() {
        if (!this._els.peopleSelected) return;

        this._els.peopleSelected.innerHTML = '';

        if (this._selectedPeople.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'people-picker-empty';
            empty.textContent = 'Click or drag people from the left to add them';
            this._els.peopleSelected.appendChild(empty);
            return;
        }

        for (const person of this._selectedPeople) {
            const item = this._createPersonItem(person, 'selected');
            this._els.peopleSelected.appendChild(item);
        }
    },

    /**
     * Creates a person item element for the picker.
     * @param {Object} person - Person object
     * @param {string} panel - 'available' or 'selected'
     * @returns {HTMLElement}
     * @private
     */
    _createPersonItem(person, panel) {
        const item = document.createElement('div');
        item.className = 'people-picker-item';
        item.dataset.personId = person.id;
        item.draggable = true;

        const img = document.createElement('img');
        img.src = AppState.people.getThumbnailUrl(person.id);
        img.alt = person.name;
        img.loading = 'lazy';
        item.appendChild(img);

        const name = document.createElement('span');
        name.textContent = person.name;
        item.appendChild(name);

        // Click to move between panels
        item.addEventListener('click', () => {
            if (panel === 'available') {
                // Add to selected
                this._selectedPeople.push(person);
            } else {
                // Remove from selected
                this._selectedPeople = this._selectedPeople.filter(p => p.id !== person.id);
            }
            this._renderPeopleAvailable();
            this._renderPeopleSelected();
        });

        // Drag-and-drop support
        item.addEventListener('dragstart', (e) => {
            e.dataTransfer.setData('application/json', JSON.stringify(person));
            e.dataTransfer.setData('text/panel', panel);
            e.dataTransfer.effectAllowed = 'move';
        });

        return item;
    },

    /**
     * Renders the people chips in the filter field.
     * @private
     */
    _renderPeopleChips() {
        if (!this._els.peopleChips) return;

        this._els.peopleChips.innerHTML = '';

        if (this._selectedPeople.length === 0) {
            const placeholder = document.createElement('span');
            placeholder.className = 'people-placeholder';
            placeholder.textContent = 'Click to add people...';
            this._els.peopleChips.appendChild(placeholder);
            return;
        }

        for (const person of this._selectedPeople) {
            const chip = document.createElement('span');
            chip.className = 'people-chip';

            const nameSpan = document.createElement('span');
            nameSpan.textContent = person.name;
            chip.appendChild(nameSpan);

            const removeBtn = document.createElement('button');
            removeBtn.className = 'people-chip-remove';
            removeBtn.textContent = '×';
            removeBtn.title = 'Remove';
            removeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this._selectedPeople = this._selectedPeople.filter(p => p.id !== person.id);
                this._renderPeopleChips();
            });
            chip.appendChild(removeBtn);

            this._els.peopleChips.appendChild(chip);
        }
    },

    /* ----------------------------------------------------------------------
       FORM POPULATION & READING

       Populating form from filter state and reading form values.
       ---------------------------------------------------------------------- */

    /**
     * Populates form fields from the current filter state.
     * @private
     */
    _populateForm() {
        const filter = App.getFilter();

        if (filter) {
            this._els.textInput.value = filter.text || '';
            this._els.dateStart.value = filter.dateStart || '';
            this._els.dateEnd.value = filter.dateEnd || '';
            this._els.ratingInput.value = filter.rating || '';

            // Sync similarity slider from filter threshold if available
            if (filter.threshold) {
                const pct = Math.round(filter.threshold * 100);
                this._els.similaritySlider.value = pct;
                this._els.similarityValue.textContent = pct + '%';
            }

            // Populate people filter
            if (filter.people && filter.people.length > 0) {
                this._selectedPeople = [...filter.people];
            } else {
                this._selectedPeople = [];
            }
            this._renderPeopleChips();
        } else {
            this._clearForm();
        }
    },

    /**
     * Clears all form fields.
     * @private
     */
    _clearForm() {
        this._els.textInput.value = '';
        this._els.similaritySlider.value = 20;
        this._els.similarityValue.textContent = '20%';
        this._els.dateStart.value = '';
        this._els.dateEnd.value = '';
        this._els.ratingInput.value = '';
        this._selectedPeople = [];
        this._renderPeopleChips();
    },

    /**
     * Reads form values and returns a filter object.
     * Returns null if all fields are empty.
     * @returns {Object|null} Filter object or null
     * @private
     */
    _readForm() {
        const text = this._els.textInput.value.trim();
        const dateStart = this._els.dateStart.value;
        const dateEnd = this._els.dateEnd.value;
        const rating = this._els.ratingInput.value.trim();
        const people = this._selectedPeople.length > 0 ? [...this._selectedPeople] : null;

        // Return null if all fields are empty
        if (!text && !dateStart && !dateEnd && !rating && !people) {
            return null;
        }

        return {
            text: text || null,
            dateStart: dateStart || null,
            dateEnd: dateEnd || null,
            rating: rating || null,
            people: people
        };
    },

    /**
     * Checks if the form has any values entered.
     * @returns {boolean} True if any field has a value
     * @private
     */
    _hasFormValues() {
        return !!(
            this._els.textInput.value.trim() ||
            this._els.dateStart.value ||
            this._els.dateEnd.value ||
            this._els.ratingInput.value.trim() ||
            this._selectedPeople.length > 0
        );
    },

    /* ----------------------------------------------------------------------
       FILTER ACTIONS

       Apply, clear, and validate filter.
       ---------------------------------------------------------------------- */

    /**
     * Validates the current form values.
     * @returns {{valid: boolean, message: string|null}} Validation result
     * @private
     */
    _validate() {
        const dateStart = this._els.dateStart.value;
        const dateEnd = this._els.dateEnd.value;

        // Check date range validity
        if (dateStart && dateEnd) {
            const start = new Date(dateStart);
            const end = new Date(dateEnd);
            if (start > end) {
                return {
                    valid: false,
                    message: 'Start date cannot be after end date'
                };
            }
        }

        return { valid: true, message: null };
    },

    /**
     * Shows a validation error to the user.
     * @param {string} message - Error message to display
     * @private
     */
    _showError(message) {
        App.showError(message);
    },

    /**
     * Applies the current filter and returns to gallery.
     * Navigates immediately and shows loading overlay while semantic search runs.
     * @private
     */
    async _applyFilter() {
        // Validate
        const validation = this._validate();
        if (!validation.valid) {
            this._showError(validation.message);
            return;
        }

        // Read form values and threshold BEFORE navigating away
        const filter = this._readForm();
        const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

        // Navigate to gallery immediately for responsive UX
        App.showGallery();

        // If there's a text query, perform semantic search with loading indicator
        if (filter && filter.text) {
            AppState.loading.show('search', 'Searching…');
            try {
                const response = await AppState.search.execute(filter.text, threshold, 10000);

                if (response && response.results) {
                    // Store matching image IDs, scores, and threshold in the filter
                    filter.type = 'semantic';
                    filter.threshold = threshold;
                    filter.imageIds = response.results.map(r => r.id);
                    filter.scores = {};
                    response.results.forEach(r => {
                        filter.scores[r.id] = r.score;
                    });
                }
            } catch (error) {
                console.error('Semantic search failed:', error);
                App.showError('Search failed. Please try again.');
            } finally {
                AppState.loading.hide('search');
            }
        }

        // Set filter - gallery subscribes to filterChanged event
        App.setFilter(filter);
    },

    /**
     * Clears the filter and optionally returns to gallery.
     * @param {boolean} [navigateToGallery=true] - Whether to navigate after clearing
     * @private
     */
    _clearFilter(navigateToGallery = true) {
        // Clear form fields
        this._clearForm();

        // Clear app filter state
        // Gallery subscribes to filterChanged event and will reload automatically
        App.clearFilter();

        // Navigate to gallery if requested
        if (navigateToGallery) {
            App.showGallery();
        }
    }
};

// Register module with App
App.registerModule('search', Search);
