/**
 * @fileoverview Search and filter screen module for the Photonarium application.
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
     * IDs of people auto-added by name detection from the text input.
     * Tracked separately from manually-picked people so that when a longer
     * name match subsumes a shorter auto-added one, the shorter chip can
     * be swapped out without affecting manual picks.
     * Persisted in the filter object across screen navigations.
     * @type {Set<string>}
     * @private
     */
    _autoAddedPeopleIds: new Set(),

    /**
     * When editing an existing smart group, holds the group's hash.
     * Null when creating a new smart group.
     * @type {string|null}
     * @private
     */
    _editingSmartGroupHash: null,

    /**
     * Name of the smart group being edited, for comparison.
     * @type {string|null}
     * @private
     */
    _editingSmartGroupName: null,

    /**
     * Selected metadata criteria for the filter.
     * Keys are metadata field names, values are filter strings.
     * @type {Object<string, string>}
     * @private
     */
    _selectedMetadata: {},

    /**
     * Cache for metadata autocomplete values, keyed by metadata key name.
     * Populated lazily from /api/metadata-values when the user types.
     * @type {Object<string, string[]>}
     * @private
     */
    _metadataValuesCache: {},

    /**
     * Get all people sorted by name.
     * Delegates to AppState.people with sorting applied.
     * @returns {Array<Object>} Sorted people list
     * @private
     */
    _getAllPeopleSorted() {
        // Spread to avoid mutating AppState's internal array (sort is in-place)
        return [...AppState.people.getAll()].sort((a, b) =>
            a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
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
            textWarning: App.$('filter-text-warning'),
            similaritySlider: App.$('filter-similarity'),
            similarityValue: App.$('similarity-value'),
            dateLabel: App.$('filter-date-label'),
            dateStart: App.$('filter-date-start'),
            dateEnd: App.$('filter-date-end'),
            ratingInput: App.$('filter-rating'),
            emojiBtn: App.$('btn-emoji-picker'),
            applyBtn: App.$('btn-apply-filter'),
            clearBtn: App.$('btn-clear-filter-action'),
            saveSmartGroupBtn: App.$('btn-save-smart-group'),
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
            peopleCancelBtn: App.$('dialog-people-cancel'),
            // Metadata filter elements
            metadataChips: App.$('filter-metadata-chips'),
            metadataPickerBtn: App.$('btn-metadata-picker'),
        };

        // Check face detection status
        this._loadFaceDetectionStatus();

        // Bind button events
        this._bindEvents();

        // Subscribe to AppState.people for deletions and dialog updates
        AppState.people.onChanged(() => {
            this._pruneDeletedPeople();

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

        // Update the Save/Update button text based on editing state
        this._updateSmartGroupButton();

        // Pre-load people cache so name extraction can work without delay
        AppState.people.load();

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

        // Clear smart group editing state so returning later starts fresh
        this._editingSmartGroupHash = null;
        this._editingSmartGroupName = null;
        this._updateSmartGroupButton();
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

        // Save as Smart Group button
        if (this._els.saveSmartGroupBtn) {
            this._els.saveSmartGroupBtn.addEventListener('click', () => this._saveAsSmartGroup());
        }

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

        // Easter egg: clicking the "Date Range" label triggers On This Day
        if (this._els.dateLabel) {
            this._els.dateLabel.addEventListener('click', () => {
                if (typeof OnThisDay !== 'undefined' && OnThisDay.tryShowNow) {
                    OnThisDay.tryShowNow();
                }
            });
        }

        // Allow Enter key to apply filter from text input
        this._els.textInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this._applyFilter();
            }
        });

        // Auto-detect known person names typed into the description field.
        // While typing, require a trailing separator (space/punctuation) so
        // partial names like "ste" don't prematurely match "Ste".
        const debouncedExtract = App.debounce(
            () => this._extractPeopleFromText({ trailingRequired: true }),
            400,
        );
        this._els.textInput.addEventListener('input', debouncedExtract);
        // On blur the user has finished typing, so accept end-of-string.
        this._els.textInput.addEventListener('blur',
            () => this._extractPeopleFromText({ trailingRequired: false }),
        );

        // People filter events
        if (this._els.peopleChips) {
            this._els.peopleChips.addEventListener('click', () => this._openPeoplePicker());
        }
        if (this._els.peoplePickerBtn) {
            this._els.peoplePickerBtn.addEventListener('click', () => this._openPeoplePicker());
        }

        // Metadata filter events
        if (this._els.metadataChips) {
            this._els.metadataChips.addEventListener('click', (e) => {
                // Only open picker if not clicking a remove button
                if (!e.target.closest('.metadata-chip-remove')) {
                    this._openMetadataPicker();
                }
            });
        }
        if (this._els.metadataPickerBtn) {
            this._els.metadataPickerBtn.addEventListener('click', () => this._openMetadataPicker());
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

    /**
     * Removes people from the selection, auto-added set, and active filter
     * when they no longer exist in AppState (deleted by another client).
     * @private
     */
    _pruneDeletedPeople() {
        if (!this._selectedPeople.length) return;

        const before = this._selectedPeople.length;
        this._selectedPeople = this._selectedPeople.filter(p => {
            if (AppState.people.get(p.id)) return true;
            // Person was deleted — also clean up auto-added tracking
            this._autoAddedPeopleIds.delete(p.id);
            return false;
        });

        if (this._selectedPeople.length === before) return;

        // Re-render chips (safe even when not on Search screen — DOM is inert)
        this._renderPeopleChips();

        // Also prune the active filter so _populateForm() doesn't restore stale
        // people when the user re-enters the Search screen
        const filter = App.getFilter();
        if (filter?.people?.length) {
            const validIds = new Set(this._selectedPeople.map(p => p.id));
            filter.people = filter.people.filter(p => validIds.has(p.id));
            if (filter.autoAddedPeopleIds) {
                filter.autoAddedPeopleIds = filter.autoAddedPeopleIds
                    .filter(id => validIds.has(id));
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
            // Sync: drop auto-added IDs for people the user removed via picker
            const selectedIds = new Set(this._selectedPeople.map(p => p.id));
            for (const autoId of this._autoAddedPeopleIds) {
                if (!selectedIds.has(autoId)) {
                    this._autoAddedPeopleIds.delete(autoId);
                }
            }
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
                this._autoAddedPeopleIds.delete(person.id);
                this._selectedPeople = this._selectedPeople.filter(p => p.id !== person.id);
                this._renderPeopleChips();
            });
            chip.appendChild(removeBtn);

            this._els.peopleChips.appendChild(chip);
        }
    },

    /* ----------------------------------------------------------------------
       AUTO-DETECT PEOPLE FROM TEXT

       Scans the description input for known person names and automatically
       adds matching people as chips, drawing the user's attention to the
       People Picker feature organically.
       ---------------------------------------------------------------------- */

    /**
     * Escapes special regex metacharacters in a string.
     * @param {string} str - Raw string to escape
     * @returns {string} Regex-safe string
     * @private
     */
    _escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    },

    /**
     * Scans the description text input for known person names and adds any
     * matches as people-filter chips.
     *
     * Matching is case-insensitive and word-boundary-aware (leading `\b`).
     * Names are tested longest-first so "Mary Jane" matches before "Mary".
     * People already selected are skipped.
     *
     * @param {Object} [options]
     * @param {boolean} [options.trailingRequired=true] - Require a trailing separator
     *   (space, comma, etc.) after the name — prevents premature matches while typing
     * @private
     */
    _extractPeopleFromText({ trailingRequired = true } = {}) {
        if (!this._faceDetectionEnabled || !AppState.people.isLoaded()) {
            if (this._els.textWarning) this._els.textWarning.hidden = true;
            return;
        }

        let text = this._els.textInput.value;
        if (!text.trim()) {
            // Text is empty — remove any auto-added people and hide warning
            if (this._autoAddedPeopleIds.size > 0) {
                this._selectedPeople = this._selectedPeople.filter(
                    p => !this._autoAddedPeopleIds.has(p.id),
                );
                this._autoAddedPeopleIds.clear();
                this._renderPeopleChips();
            }
            if (this._els.textWarning) this._els.textWarning.hidden = true;
            return;
        }

        // Sort longest-first so multi-word names match before their prefixes
        const allPeople = [...AppState.people.getAll()].sort(
            (a, b) => b.name.length - a.name.length,
        );

        // Phase 1: Greedy longest-first matching with consumed-region tracking.
        // A matched character range cannot overlap a previously matched range,
        // so "Mary Jane" consumes those characters before "Mary" or "Jane"
        // can claim them independently.
        const consumed = [];       // Array of [start, end] character ranges
        const claimedIds = new Set();  // Person IDs the current text supports

        for (const person of allPeople) {
            const escaped = this._escapeRegex(person.name);
            const pattern = trailingRequired
                ? `\\b${escaped}\\b(?=[\\s,;.!?])`
                : `\\b${escaped}\\b`;
            const regex = new RegExp(pattern, 'gi');

            let m;
            while ((m = regex.exec(text)) !== null) {
                const start = m.index;
                const end = start + m[0].length;
                const overlaps = consumed.some(([cs, ce]) => start < ce && end > cs);
                if (!overlaps) {
                    consumed.push([start, end]);
                    claimedIds.add(person.id);
                    break;  // one match per person is sufficient
                }
            }
        }

        // Phase 2: Reconcile auto-added people with current text claims.
        const selectedIds = new Set(this._selectedPeople.map(p => p.id));
        let changed = false;

        // Remove auto-added people the text no longer claims (e.g. "Mary"
        // was auto-added, but now "Mary Jane" consumes those characters)
        for (const autoId of [...this._autoAddedPeopleIds]) {
            if (!claimedIds.has(autoId)) {
                this._selectedPeople = this._selectedPeople.filter(p => p.id !== autoId);
                this._autoAddedPeopleIds.delete(autoId);
                changed = true;
            }
        }

        // Add newly claimed people not already in the selection
        for (const person of allPeople) {
            if (claimedIds.has(person.id) && !selectedIds.has(person.id)) {
                this._selectedPeople.push({ id: person.id, name: person.name });
                this._autoAddedPeopleIds.add(person.id);
                changed = true;
            }
        }

        if (changed) {
            this._renderPeopleChips();
        }

        // Show a warning if the description text, after stripping matched
        // people names, contains no letters — the semantic search will likely
        // return nothing since it only sees whitespace/punctuation.
        this._updateTextWarning(text, consumed);
    },

    /**
     * Shows or hides the warning icon next to the text input.
     *
     * The warning appears when the description text is non-empty and, after
     * stripping all matched people-name regions, contains no letters — meaning
     * the semantic search would be fed only punctuation/whitespace.
     *
     * @param {string} text - The raw description input value
     * @param {Array<[number, number]>} consumed - Character ranges matched as names
     * @private
     */
    _updateTextWarning(text, consumed) {
        if (!this._els.textWarning) return;

        let show = false;
        if (text.trim() && consumed.length > 0) {
            // Build the text with matched name regions removed
            const sorted = [...consumed].sort((a, b) => b[0] - a[0]);
            let remainder = text;
            for (const [start, end] of sorted) {
                remainder = remainder.slice(0, start) + remainder.slice(end);
            }
            // If no letters remain, the search text is effectively empty
            show = !/[a-zA-Z]/.test(remainder);
        }

        this._els.textWarning.hidden = !show;
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

            // Populate people filter (and restore auto-added tracking)
            if (filter.people && filter.people.length > 0) {
                this._selectedPeople = [...filter.people];
            } else {
                this._selectedPeople = [];
            }
            this._autoAddedPeopleIds = new Set(filter.autoAddedPeopleIds || []);
            this._renderPeopleChips();

            // Populate metadata filter
            if (filter.metadata && Object.keys(filter.metadata).length > 0) {
                this._selectedMetadata = { ...filter.metadata };
            } else {
                this._selectedMetadata = {};
            }
            this._renderMetadataChips();
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
        if (this._els.textWarning) this._els.textWarning.hidden = true;
        this._els.similaritySlider.value = 20;
        this._els.similarityValue.textContent = '20%';
        this._els.dateStart.value = '';
        this._els.dateEnd.value = '';
        this._els.ratingInput.value = '';
        this._selectedPeople = [];
        this._autoAddedPeopleIds = new Set();
        this._renderPeopleChips();
        this._selectedMetadata = {};
        this._renderMetadataChips();
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
        const metadata = Object.keys(this._selectedMetadata).length > 0
            ? { ...this._selectedMetadata } : null;

        // Return null if all fields are empty
        if (!text && !dateStart && !dateEnd && !rating && !people && !metadata) {
            return null;
        }

        return {
            text: text || null,
            dateStart: dateStart || null,
            dateEnd: dateEnd || null,
            rating: rating || null,
            people: people,
            metadata: metadata,
            // Persist auto-added tracking so it survives screen navigation
            autoAddedPeopleIds: this._autoAddedPeopleIds.size > 0
                ? [...this._autoAddedPeopleIds] : null,
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
            this._selectedPeople.length > 0 ||
            Object.keys(this._selectedMetadata).length > 0
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
                    message: 'Start date cannot be after end date',
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

        // Final extraction pass: pick up any remaining person names the user
        // finished typing but didn't trigger via input/blur (e.g. end-of-string).
        this._extractPeopleFromText({ trailingRequired: false });

        // Read form values and threshold BEFORE navigating away.
        // The original text is preserved in the filter for display when the
        // user returns to the Search screen.
        const filter = this._readForm();
        const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

        // Build a CLIP-friendly query by stripping selected people's names
        // so the embedding focuses on the descriptive content, not proper nouns.
        let searchText = filter?.text || '';
        if (searchText && this._selectedPeople.length > 0) {
            // Strip longest names first so "Mary Jane" is removed before "Mary"
            const sorted = [...this._selectedPeople].sort(
                (a, b) => b.name.length - a.name.length,
            );
            for (const person of sorted) {
                const escaped = this._escapeRegex(person.name);
                searchText = searchText.replace(
                    new RegExp('\\b' + escaped + '\\b', 'gi'), '',
                );
            }
            searchText = searchText.replace(/\s{2,}/g, ' ').trim();
        }

        // Navigate to gallery immediately for responsive UX
        App.showGallery();

        // If there's a text query, perform semantic search with loading indicator
        if (filter && searchText) {
            AppState.loading.show('search', 'Searching…');
            try {
                const response = await AppState.search.execute(searchText, threshold, 10000);

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

        // If there are metadata criteria, search for matching images
        if (filter && filter.metadata) {
            try {
                const response = await App.apiPost('/metadata-search', {
                    criteria: filter.metadata,
                });
                if (response?.data?.image_ids) {
                    filter.metadataImageIds = new Set(response.data.image_ids);
                }
            } catch (error) {
                console.error('Metadata search failed:', error);
                // Continue without metadata filter rather than blocking
            }
        }

        // Set filter - gallery subscribes to filterChanged event
        App.setFilter(filter);
    },

    /**
     * Updates the smart group button text based on editing state.
     * @private
     */
    _updateSmartGroupButton() {
        const btn = this._els.saveSmartGroupBtn;
        if (!btn) return;

        // Find the last text node (after the icon span) and update it
        const textNodes = [...btn.childNodes].filter(n => n.nodeType === Node.TEXT_NODE);
        const textNode = textNodes[textNodes.length - 1];
        if (this._editingSmartGroupHash) {
            if (textNode) textNode.textContent = ' Update Smart Group';
            btn.title = 'Update the filter criteria for this Smart Group';
        } else {
            if (textNode) textNode.textContent = ' Save as Smart Group';
            btn.title = 'Save current filter criteria as a Smart Group';
        }
    },

    /**
     * Saves the current filter criteria as a smart group, or updates an
     * existing one if in editing mode.
     * @private
     */
    async _saveAsSmartGroup() {
        // Validate form
        const validation = this._validate();
        if (!validation.valid) {
            this._showError(validation.message);
            return;
        }

        // Final extraction pass for people names
        this._extractPeopleFromText({ trailingRequired: false });

        // Read form values
        const form = this._readForm();
        if (!form) {
            this._showError('Enter at least one filter criterion before saving.');
            return;
        }

        // Build clean filter JSON — only persist the user-visible criteria,
        // not computed fields (imageIds, scores, metadataImageIds, type)
        const filterJson = {};
        if (form.text) filterJson.text = form.text;
        if (form.dateStart) filterJson.dateStart = form.dateStart;
        if (form.dateEnd) filterJson.dateEnd = form.dateEnd;
        if (form.rating) filterJson.rating = form.rating;
        if (form.people) filterJson.people = form.people.map(p => ({ id: p.id, name: p.name }));
        if (form.metadata) filterJson.metadata = form.metadata;

        // Include threshold from slider
        const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;
        if (form.text) filterJson.threshold = threshold;

        if (this._editingSmartGroupHash) {
            // Update existing smart group
            const groupHash = this._editingSmartGroupHash;
            try {
                await AppState.duplicates.updateGroupFilter(
                    groupHash,
                    this._editingSmartGroupName,
                    filterJson,
                );
                App.showInfo('Smart Group updated');
            } catch (err) {
                this._showError('Failed to update Smart Group');
                return;
            }
            // Re-evaluate preview in the background (filter changed, old preview may not match)
            AppState.duplicates.evaluateAndSetPreview(groupHash, filterJson);
        } else {
            // Create new smart group — prompt for name
            const name = await App.prompt('Smart Group', 'Enter a name for the Smart Group:');
            if (!name) return;

            let groupHash;
            try {
                groupHash = await AppState.duplicates.createGroup(name, [], filterJson);
                App.showInfo('Smart Group created');
            } catch (err) {
                this._showError('Failed to create Smart Group');
                return;
            }
            // Evaluate preview in the background
            if (groupHash) {
                AppState.duplicates.evaluateAndSetPreview(groupHash, filterJson);
            }
        }
    },

    /**
     * Loads a smart group's filter criteria into the search form for editing.
     * Called from the Groups screen when the edit badge is clicked.
     *
     * @param {Object} group - The smart group object (must have filter_json)
     */
    loadSmartGroupForEditing(group) {
        if (!group?.filter_json) return;

        // Parse filter_json (may be string from backend or already an object)
        const filter = typeof group.filter_json === 'string'
            ? JSON.parse(group.filter_json)
            : { ...group.filter_json };

        // Set editing state
        this._editingSmartGroupHash = group.group_hash;
        this._editingSmartGroupName = group.name;

        // Set the filter silently so _populateForm() will read it on enter
        App.setFilter(filter);

        // Navigate to Search screen — onEnter() will populate form and update button
        App.showSearch();
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
    },

    /* ----------------------------------------------------------------------
       METADATA FILTER

       Metadata chip rendering, writable modal with autocomplete, and
       the public setMetadataFilters() method for filter-from-example.
       ---------------------------------------------------------------------- */

    /**
     * Sets metadata filters from external code (e.g. Gallery filter-from-example).
     * @param {Object} metadata - {key: value} pairs to set as filter criteria
     */
    setMetadataFilters(metadata) {
        this._selectedMetadata = { ...metadata };
        this._renderMetadataChips();
    },

    /**
     * Renders metadata filter chips from the current selection.
     * @private
     */
    _renderMetadataChips() {
        const container = this._els.metadataChips;
        if (!container) return;

        container.innerHTML = '';

        const keys = Object.keys(this._selectedMetadata);
        if (keys.length === 0) {
            const placeholder = document.createElement('span');
            placeholder.className = 'metadata-placeholder';
            placeholder.textContent = 'Click to add metadata filters...';
            container.appendChild(placeholder);
            return;
        }

        for (const key of keys) {
            const value = this._selectedMetadata[key];
            const chip = document.createElement('span');
            chip.className = 'metadata-chip';

            const keySpan = document.createElement('span');
            keySpan.className = 'metadata-chip-key';
            keySpan.textContent = key + ':';
            chip.appendChild(keySpan);

            const valueSpan = document.createElement('span');
            valueSpan.textContent = ' ' + value;
            chip.appendChild(valueSpan);

            const removeBtn = document.createElement('button');
            removeBtn.className = 'metadata-chip-remove';
            removeBtn.textContent = '\u00D7';
            removeBtn.title = 'Remove filter';
            removeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                delete this._selectedMetadata[key];
                this._renderMetadataChips();
            });
            chip.appendChild(removeBtn);

            container.appendChild(chip);
        }
    },

    /**
     * Opens the metadata picker dialog in writable mode.
     * Shows all known metadata keys with input fields for filter values.
     * @private
     */
    async _openMetadataPicker() {
        const dialog = App.$('dialog-metadata');
        const title = App.$('dialog-metadata-title');
        const body = App.$('dialog-metadata-body');
        const actions = App.$('dialog-metadata-actions');
        if (!dialog || !body) return;

        title.textContent = 'Metadata Filter';
        body.innerHTML = '<p class="metadata-empty">Loading available fields\u2026</p>';

        // Fetch available keys from the backend
        let keys = [];
        try {
            const response = await App.apiGet('/metadata-keys');
            keys = response?.data?.keys || [];
        } catch (e) {
            console.error('Failed to load metadata keys:', e);
            body.innerHTML = '<p class="metadata-empty">Failed to load metadata fields.</p>';
        }

        if (keys.length === 0) {
            body.innerHTML = '<p class="metadata-empty">No metadata available. Run a scan with images that have EXIF data.</p>';
        } else {
            this._renderWritableMetadata(body, keys);
        }

        // Actions: Cancel + Done
        actions.innerHTML = '';
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'action-btn';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.addEventListener('click', () => dialog.close());

        const doneBtn = document.createElement('button');
        doneBtn.className = 'action-btn primary';
        doneBtn.textContent = 'Done';
        doneBtn.addEventListener('click', () => {
            // Read values from input fields
            this._selectedMetadata = {};
            const inputs = body.querySelectorAll('.metadata-input');
            inputs.forEach(input => {
                const value = input.value.trim();
                if (value) {
                    this._selectedMetadata[input.dataset.key] = value;
                }
            });
            this._renderMetadataChips();
            dialog.close();
        });

        actions.appendChild(cancelBtn);
        actions.appendChild(doneBtn);

        // Handle Escape/Enter keys within dialog
        const keyHandler = (e) => {
            e.stopPropagation();
            if (e.key === 'Escape') {
                e.preventDefault();
                dialog.close();
            } else if (e.key === 'Enter' && !e.target.matches('.metadata-input')) {
                e.preventDefault();
                doneBtn.click();
            }
        };
        dialog.addEventListener('keydown', keyHandler);
        dialog.addEventListener('close', () => {
            dialog.removeEventListener('keydown', keyHandler);
        }, { once: true });

        dialog.showModal();
    },

    /**
     * Renders writable metadata rows with input fields and autocomplete.
     * @param {HTMLElement} container - Body element to render into
     * @param {string[]} keys - Available metadata key names
     * @private
     */
    _renderWritableMetadata(container, keys) {
        container.innerHTML = '';

        // Preferred display order for common keys
        const keyOrder = [
            'Camera', 'Lens', 'Focal Length', 'Aperture', 'Shutter Speed',
            'ISO', 'Exposure Comp', 'Exposure Program', 'Metering', 'Flash',
            'White Balance', 'Color Space', 'Software', 'Artist', 'Copyright',
            'GPS',
        ];

        // Sort: ordered first, then extras alphabetically. Skip 'Date Taken'.
        const ordered = keyOrder.filter(k => keys.includes(k));
        const extras = keys.filter(k => !keyOrder.includes(k) && k !== 'Date Taken').sort();
        const sortedKeys = [...ordered, ...extras];

        for (const key of sortedKeys) {
            const row = document.createElement('div');
            row.className = 'metadata-row metadata-row-writable';

            const keyEl = document.createElement('span');
            keyEl.className = 'metadata-key';
            keyEl.textContent = key;
            row.appendChild(keyEl);

            const inputContainer = document.createElement('div');
            inputContainer.className = 'metadata-input-container';

            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'metadata-input';
            input.dataset.key = key;
            input.placeholder = `e.g. ${this._getPlaceholder(key)}`;
            input.autocomplete = 'off';
            // Pre-fill from current selection
            if (this._selectedMetadata[key]) {
                input.value = this._selectedMetadata[key];
            }

            const dropdown = document.createElement('div');
            dropdown.className = 'metadata-autocomplete';

            inputContainer.appendChild(input);
            inputContainer.appendChild(dropdown);
            row.appendChild(inputContainer);
            container.appendChild(row);

            // Autocomplete behaviour
            this._bindAutocomplete(input, dropdown, key);
        }
    },

    /**
     * Returns a placeholder hint for a metadata key.
     * @param {string} key - Metadata key name
     * @returns {string} Placeholder text
     * @private
     */
    _getPlaceholder(key) {
        const hints = {
            'Camera': 'Nikon',
            'Lens': '24-70mm',
            'Focal Length': '50mm',
            'Aperture': 'f/2.8',
            'Shutter Speed': '1/250s',
            'ISO': '400',
            'Exposure Comp': '+0.7',
            'Exposure Program': 'Aperture Priority',
            'Metering': 'Matrix',
            'Flash': 'Fired',
            'White Balance': 'Auto',
            'Color Space': 'sRGB',
            'Software': 'Lightroom',
            'Artist': 'Name',
            'Copyright': '2024',
        };
        return hints[key] || 'value';
    },

    /**
     * Binds autocomplete behaviour to a metadata input field.
     * Fetches values lazily and uses subsequence matching to filter.
     * @param {HTMLInputElement} input
     * @param {HTMLElement} dropdown
     * @param {string} key - Metadata key name
     * @private
     */
    _bindAutocomplete(input, dropdown, key) {
        let highlightedIndex = -1;

        const updateDropdown = async () => {
            const query = input.value.trim();
            if (!query) {
                dropdown.classList.remove('visible');
                return;
            }

            // Lazy-fetch values for this key
            if (!this._metadataValuesCache[key]) {
                try {
                    const resp = await App.apiGet(`/metadata-values?key=${encodeURIComponent(key)}`);
                    this._metadataValuesCache[key] = resp?.data?.values || [];
                } catch (e) {
                    this._metadataValuesCache[key] = [];
                }
            }

            const values = this._metadataValuesCache[key];
            // Subsequence matching: each character in query must appear in order
            const matches = values.filter(v => this._fuzzyMatch(query, v));

            if (matches.length === 0) {
                dropdown.classList.remove('visible');
                return;
            }

            dropdown.innerHTML = '';
            highlightedIndex = -1;

            matches.slice(0, 20).forEach((value, i) => {
                const item = document.createElement('div');
                item.className = 'metadata-autocomplete-item';
                item.innerHTML = this._highlightMatch(query, value);
                item.addEventListener('mousedown', (e) => {
                    e.preventDefault();  // Prevent blur
                    input.value = value;
                    dropdown.classList.remove('visible');
                });
                dropdown.appendChild(item);
            });

            dropdown.classList.add('visible');
        };

        input.addEventListener('input', updateDropdown);
        input.addEventListener('focus', updateDropdown);
        input.addEventListener('blur', () => {
            // Small delay to allow mousedown on items
            setTimeout(() => dropdown.classList.remove('visible'), 150);
        });

        // Keyboard navigation
        input.addEventListener('keydown', (e) => {
            const items = dropdown.querySelectorAll('.metadata-autocomplete-item');
            if (!items.length || !dropdown.classList.contains('visible')) return;

            if (e.key === 'ArrowDown') {
                e.preventDefault();
                highlightedIndex = Math.min(highlightedIndex + 1, items.length - 1);
                items.forEach((it, i) => it.classList.toggle('highlighted', i === highlightedIndex));
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                highlightedIndex = Math.max(highlightedIndex - 1, 0);
                items.forEach((it, i) => it.classList.toggle('highlighted', i === highlightedIndex));
            } else if (e.key === 'Enter' && highlightedIndex >= 0) {
                e.preventDefault();
                e.stopPropagation();
                input.value = items[highlightedIndex].textContent;
                dropdown.classList.remove('visible');
            }
        });
    },

    /**
     * Subsequence (fuzzy) match: each character in query appears in order in value.
     * Case-insensitive.
     * @param {string} query
     * @param {string} value
     * @returns {boolean}
     * @private
     */
    _fuzzyMatch(query, value) {
        const q = query.toLowerCase();
        const v = value.toLowerCase();
        let qi = 0;
        for (let vi = 0; vi < v.length && qi < q.length; vi++) {
            if (v[vi] === q[qi]) qi++;
        }
        return qi === q.length;
    },

    /**
     * Highlights matching characters in a value for subsequence display.
     * Wraps matching chars in <mark> tags.
     * @param {string} query
     * @param {string} value
     * @returns {string} HTML with highlighted matches
     * @private
     */
    _highlightMatch(query, value) {
        const q = query.toLowerCase();
        const v = value.toLowerCase();
        let qi = 0;
        let html = '';
        for (let vi = 0; vi < value.length; vi++) {
            if (qi < q.length && v[vi] === q[qi]) {
                html += `<mark>${App.escapeHtml(value[vi])}</mark>`;
                qi++;
            } else {
                html += App.escapeHtml(value[vi]);
            }
        }
        return html;
    },
};

// Register module with App
App.registerModule('search', Search);
