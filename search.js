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
     * Initializes the search module.
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
            clearBtn: App.$('btn-clear-filter')
        };

        // Bind button events
        this._bindEvents();
    },

    /**
     * Called when entering the search screen.
     * Populates form fields from current filter state.
     */
    onEnter() {
        this._populateForm();
        // Focus the text input for quick typing
        this._els.textInput.focus();
    },

    /**
     * Called when leaving the search screen.
     */
    onLeave() {
        // Nothing to clean up
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

        // Similarity slider - update displayed value
        this._els.similaritySlider.addEventListener('input', () => {
            this._els.similarityValue.textContent = this._els.similaritySlider.value + '%';
        });

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

        // Return null if all fields are empty
        if (!text && !dateStart && !dateEnd && !rating) {
            return null;
        }

        return {
            text: text || null,
            dateStart: dateStart || null,
            dateEnd: dateEnd || null,
            rating: rating || null
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
            this._els.ratingInput.value.trim()
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
        // For now, use a simple alert. Could be replaced with inline error display.
        alert(message);
    },

    /**
     * Applies the current filter and returns to gallery.
     * @private
     */
    async _applyFilter() {
        // Validate
        const validation = this._validate();
        if (!validation.valid) {
            this._showError(validation.message);
            return;
        }

        // Read form values
        const filter = this._readForm();

        // If there's a text query, perform semantic search
        if (filter && filter.text) {
            try {
                // Get similarity threshold from slider (convert percentage to decimal)
                const threshold = parseInt(this._els.similaritySlider.value, 10) / 100;

                const response = await App.apiPost('/search', {
                    query: filter.text,
                    threshold: threshold,
                    limit: 500
                });

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
                this._showError('Search failed. Please try again.');
                return;
            }
        }

        // Set filter - gallery subscribes to filterChanged event
        App.setFilter(filter);

        // Navigate to gallery
        App.showGallery();
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
