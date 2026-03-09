/**
 * @fileoverview Setup wizard for first-run configuration and model downloading.
 *
 * Guides users through 5 steps:
 *   1. Hardware profile — selects performance-tuned presets
 *   2. Language & search — chooses English-only or multilingual CLIP model
 *   3. Review — summary of selected models and approximate sizes
 *   4. HuggingFace token — optional token for authenticated downloads
 *   5. Download — runs download_models.py with real-time output
 *
 * Appears automatically on first run (totalImages === 0 and wizard not yet
 * completed) and can be re-launched from the Settings dialog.
 *
 * @module wizard
 * @requires core
 */

/* global App */

/**
 * Setup wizard — standalone object (not a screen module).
 * @namespace
 */
const SetupWizard = {

    /** @type {HTMLDialogElement|null} @private */
    _dialog: null,

    /** @type {number} Current step index (0-based). @private */
    _currentStep: 0,

    /** @type {string[]} Step identifiers. @private */
    _steps: ['hardware', 'language', 'review', 'hf-token', 'download'],

    /** @type {Object} Accumulated config values from wizard selections. @private */
    _selections: {},

    /** @type {Array|null} Hardware presets from schema. @private */
    _presets: null,

    /** @type {Object|null} Language recommendations from schema. @private */
    _langRecs: null,

    /** @type {number|null} Download status polling timer. @private */
    _pollTimer: null,

    /** @type {number} Line cursor for incremental download output. @private */
    _linesSeen: 0,

    /** @type {string|null} Currently selected hardware preset id. @private */
    _selectedPreset: null,

    /** @type {string|null} Currently selected language option key. @private */
    _selectedLang: null,

    /** @type {boolean} Whether the download has started in the current session. @private */
    _downloadStarted: false,

    /** @type {string} HuggingFace token for authenticated downloads (session only). @private */
    _hfToken: '',

    /**
     * Opens the wizard dialog.  Fetches the config schema to get presets
     * and language recommendations, then renders step 1.
     */
    async show() {
        this._dialog = App.$('dialog-wizard');
        if (!this._dialog) return;

        this._currentStep = 0;
        this._selections = {};
        this._selectedPreset = null;
        this._selectedLang = null;
        this._downloadStarted = false;
        this._linesSeen = 0;
        this._hfToken = '';

        try {
            const response = await App.apiGet('/config/schema');
            const schema = response.data;
            this._presets = schema.presets || [];
            this._langRecs = schema.language_recommendations || {};

            this._renderStep();
            this._dialog.showModal();
        } catch (error) {
            console.error('Failed to load wizard schema:', error);
            App.showError('Could not load setup wizard.');
        }
    },

    /**
     * Closes the wizard dialog and cleans up polling timers.
     * @private
     */
    _close() {
        this._stopPolling();
        if (this._dialog) {
            this._dialog.close();
        }
    },

    // -----------------------------------------------------------------------
    // Step rendering
    // -----------------------------------------------------------------------

    /**
     * Renders the current step into the wizard body.
     * @private
     */
    _renderStep() {
        const body = this._dialog.querySelector('.wizard-body');
        const footer = this._dialog.querySelector('.wizard-footer');
        if (!body || !footer) return;

        body.innerHTML = '';
        footer.innerHTML = '';

        // Step indicator
        this._renderStepIndicator();

        const step = this._steps[this._currentStep];
        switch (step) {
            case 'hardware':
                this._renderHardwareStep(body);
                break;
            case 'language':
                this._renderLanguageStep(body);
                break;
            case 'review':
                this._renderReviewStep(body);
                break;
            case 'hf-token':
                this._renderHFTokenStep(body);
                break;
            case 'download':
                this._renderDownloadStep(body);
                break;
        }

        this._renderFooter(footer);
    },

    /**
     * Renders the step indicator dots.
     * @private
     */
    _renderStepIndicator() {
        const indicator = this._dialog.querySelector('.wizard-steps-indicator');
        if (!indicator) return;
        indicator.innerHTML = '';

        const labels = ['Hardware', 'Language', 'Review', 'Token', 'Download'];
        for (let i = 0; i < this._steps.length; i++) {
            const pill = document.createElement('span');
            pill.className = 'wizard-step-pill';
            if (i === this._currentStep) pill.classList.add('wizard-step-active');
            if (i < this._currentStep) pill.classList.add('wizard-step-done');
            pill.textContent = labels[i];
            indicator.appendChild(pill);
        }
    },

    /**
     * Renders the footer navigation buttons.
     * @param {HTMLElement} footer - The footer container.
     * @private
     */
    _renderFooter(footer) {
        const step = this._steps[this._currentStep];

        // Skip setup link on step 1
        if (step === 'hardware') {
            const skip = document.createElement('a');
            skip.href = '#';
            skip.className = 'wizard-skip-link';
            skip.textContent = 'Skip setup';
            skip.addEventListener('click', (e) => {
                e.preventDefault();
                this._close();
            });
            footer.appendChild(skip);
        }

        // Spacer
        const spacer = document.createElement('div');
        spacer.style.flex = '1';
        footer.appendChild(spacer);

        // Back button (not on step 1)
        if (this._currentStep > 0 && step !== 'download') {
            const back = document.createElement('button');
            back.className = 'action-btn';
            back.textContent = 'Back';
            back.addEventListener('click', () => {
                this._currentStep--;
                this._renderStep();
            });
            footer.appendChild(back);
        }

        // Step-specific forward buttons
        if (step === 'hardware' || step === 'language') {
            const next = document.createElement('button');
            next.className = 'action-btn primary';
            next.textContent = 'Next';
            // Disable until a selection is made
            const hasSelection = step === 'hardware'
                ? (this._selectedPreset !== null)
                : (this._selectedLang !== null);
            next.disabled = !hasSelection;
            next.id = 'wizard-next-btn';
            next.addEventListener('click', () => {
                this._currentStep++;
                this._renderStep();
            });
            footer.appendChild(next);
        } else if (step === 'review') {
            const next = document.createElement('button');
            next.className = 'action-btn primary';
            next.textContent = 'Save & Continue';
            next.addEventListener('click', () => this._saveAndContinue());
            footer.appendChild(next);
        } else if (step === 'hf-token') {
            const next = document.createElement('button');
            next.className = 'action-btn primary';
            next.textContent = 'Next';
            next.addEventListener('click', () => {
                // Store the token value (not persisted to config)
                const input = this._dialog.querySelector('[data-key="hf-token"]');
                if (input) this._hfToken = input.value.trim();
                this._currentStep++;
                this._renderStep();
            });
            footer.appendChild(next);
        }
        // Download step has its own buttons rendered in the step body
    },

    // -----------------------------------------------------------------------
    // Step 1: Hardware profile
    // -----------------------------------------------------------------------

    /**
     * Renders hardware preset radio cards.
     * @param {HTMLElement} body - The wizard body container.
     * @private
     */
    _renderHardwareStep(body) {
        const intro = document.createElement('p');
        intro.className = 'wizard-intro';
        intro.textContent = 'Select the hardware profile that best matches your system. '
            + 'This tunes batch sizes, thread counts, cache sizes, and model choices for optimal performance.';
        body.appendChild(intro);

        const cards = document.createElement('div');
        cards.className = 'wizard-cards';

        for (const preset of this._presets) {
            const card = this._createCard(
                preset.label,
                this._presetDescription(preset.id),
                preset.id === this._selectedPreset,
                () => {
                    this._selectedPreset = preset.id;
                    // Merge preset values into selections
                    Object.assign(this._selections, preset.values);
                    this._refreshCards(cards, 'preset');
                    this._enableNext();
                },
            );
            card.dataset.cardId = preset.id;
            card.dataset.cardType = 'preset';
            cards.appendChild(card);
        }

        // "I'll configure manually" option
        const manualCard = this._createCard(
            "I'll configure manually",
            'Skip hardware tuning — use default settings and adjust in Settings later.',
            this._selectedPreset === 'manual',
            () => {
                this._selectedPreset = 'manual';
                // Clear any preset values (keep only manual selections)
                this._selections = {};
                this._refreshCards(cards, 'preset');
                this._enableNext();
            },
        );
        manualCard.dataset.cardId = 'manual';
        manualCard.dataset.cardType = 'preset';
        cards.appendChild(manualCard);

        body.appendChild(cards);
    },

    /**
     * Returns a brief description for each hardware preset.
     * @param {string} presetId - Preset identifier.
     * @returns {string}
     * @private
     */
    _presetDescription(presetId) {
        const descriptions = {
            low: 'Minimal resource usage. Small models, low parallelism. Ideal for ARM devices, NAS boxes, or machines with limited RAM.',
            moderate: 'Balanced settings for general-purpose PCs. Good quality models without GPU requirements.',
            high_laptop: 'Higher parallelism and larger models. Suited for laptops with dedicated GPUs and 32 GB+ RAM.',
            high_desktop: 'Maximum parallelism and largest models. For powerful desktops with high-end GPUs and 64 GB+ RAM.',
        };
        return descriptions[presetId] || '';
    },

    // -----------------------------------------------------------------------
    // Step 2: Language & search models
    // -----------------------------------------------------------------------

    /**
     * Renders language/model selection radio cards.
     * @param {HTMLElement} body - The wizard body container.
     * @private
     */
    _renderLanguageStep(body) {
        const intro = document.createElement('p');
        intro.className = 'wizard-intro';
        intro.textContent = 'Choose your primary language for image search. '
            + 'This selects the CLIP model used for semantic search. '
            + 'Image captions are always generated in English regardless of this choice.';
        body.appendChild(intro);

        const cards = document.createElement('div');
        cards.className = 'wizard-cards';

        const langKeys = Object.keys(this._langRecs);
        for (const key of langKeys) {
            const rec = this._langRecs[key];
            const card = this._createCard(
                this._langLabel(key),
                rec.description,
                key === this._selectedLang,
                () => {
                    this._selectedLang = key;
                    // Override CLIP model from language recommendation
                    if (key === 'english') {
                        // For English, use the hardware preset's CLIP model if set
                        const preset = this._presets.find(p => p.id === this._selectedPreset);
                        if (preset) {
                            this._selections.openclip_model = preset.values.openclip_model;
                            this._selections.openclip_pretrained = preset.values.openclip_pretrained;
                        } else {
                            // Manual mode — use recommendation defaults
                            this._selections.openclip_model = rec.openclip_model;
                            this._selections.openclip_pretrained = rec.openclip_pretrained;
                        }
                    } else {
                        // Multilingual — always override with the specific NLLB model
                        this._selections.openclip_model = rec.openclip_model;
                        this._selections.openclip_pretrained = rec.openclip_pretrained;
                    }
                    this._refreshCards(cards, 'lang');
                    this._enableNext();
                },
            );
            card.dataset.cardId = key;
            card.dataset.cardType = 'lang';
            cards.appendChild(card);
        }

        body.appendChild(cards);

        // STT guidance note for multilingual
        const sttNote = document.createElement('p');
        sttNote.className = 'wizard-note';
        sttNote.textContent = 'For non-English audio transcription, the small Whisper model or above '
            + 'is strongly recommended. The wizard will set this based on your hardware profile.';
        body.appendChild(sttNote);
    },

    /**
     * Returns a human-readable label for a language recommendation key.
     * @param {string} key - Language key (english, multilingual, multilingual_hq).
     * @returns {string}
     * @private
     */
    _langLabel(key) {
        const labels = {
            english: 'English',
            multilingual: 'Multilingual',
            multilingual_hq: 'Multilingual (high quality)',
        };
        return labels[key] || key;
    },

    // -----------------------------------------------------------------------
    // Step 3: Review
    // -----------------------------------------------------------------------

    /**
     * Renders the model review summary table.
     * @param {HTMLElement} body - The wizard body container.
     * @private
     */
    _renderReviewStep(body) {
        const intro = document.createElement('p');
        intro.className = 'wizard-intro';
        intro.textContent = 'Review the models that will be configured. '
            + 'These will be downloaded in the next step. Existing cached models are skipped automatically.';
        body.appendChild(intro);

        const table = document.createElement('table');
        table.className = 'wizard-summary-table';

        // Header
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');
        for (const text of ['Model Type', 'Model Name', 'Approx. Size']) {
            const th = document.createElement('th');
            th.textContent = text;
            headerRow.appendChild(th);
        }
        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Body
        const tbody = document.createElement('tbody');
        const models = this._getModelSummary();
        for (const model of models) {
            const row = document.createElement('tr');
            for (const text of [model.type, model.name, model.size]) {
                const td = document.createElement('td');
                td.textContent = text;
                row.appendChild(td);
            }
            tbody.appendChild(row);
        }
        table.appendChild(tbody);
        body.appendChild(table);

        // Settings summary
        if (this._selectedPreset && this._selectedPreset !== 'manual') {
            const preset = this._presets.find(p => p.id === this._selectedPreset);
            if (preset) {
                const summary = document.createElement('p');
                summary.className = 'wizard-note';
                summary.textContent = `Hardware profile: ${preset.label}`;
                body.appendChild(summary);
            }
        }

        const note = document.createElement('p');
        note.className = 'wizard-note';
        note.textContent = 'Photonarium requires an internet connection for model downloads. '
            + 'Models are cached locally and only downloaded once.';
        body.appendChild(note);
    },

    /**
     * Builds a summary of models to be configured.
     * @returns {Array<{type: string, name: string, size: string}>}
     * @private
     */
    _getModelSummary() {
        const sel = this._selections;
        const clipModel = sel.openclip_model || 'ViT-B-32';
        const clipPretrained = sel.openclip_pretrained || 'openai';
        const captionModel = sel.caption_model || 'Salesforce/blip-image-captioning-large';
        const sttModel = sel.stt_model || 'base';

        return [
            {
                type: 'Image Search (CLIP)',
                name: `${clipModel} / ${clipPretrained}`,
                size: this._clipSize(clipModel),
            },
            {
                type: 'Image Captioning (BLIP)',
                name: captionModel.replace('Salesforce/', ''),
                size: this._captionSize(captionModel),
            },
            {
                type: 'Face Detection',
                name: 'MTCNN + InceptionResnetV1',
                size: '~110 MB',
            },
            {
                type: 'Speech-to-Text (Whisper)',
                name: sttModel,
                size: this._sttSize(sttModel),
            },
        ];
    },

    /**
     * Returns approximate CLIP model download size.
     * @param {string} model - CLIP model name.
     * @returns {string}
     * @private
     */
    _clipSize(model) {
        const sizes = {
            'ViT-B-32': '~400 MB',
            'ViT-B-16': '~400 MB',
            'ViT-L-14': '~900 MB',
            'nllb-clip-base-siglip': '~600 MB',
            'nllb-clip-large-siglip': '~1.2 GB',
        };
        return sizes[model] || '~400 MB';
    },

    /**
     * Returns approximate captioning model download size.
     * @param {string} model - Caption model name.
     * @returns {string}
     * @private
     */
    _captionSize(model) {
        const sizes = {
            'Salesforce/blip-image-captioning-base': '~1 GB',
            'Salesforce/blip-image-captioning-large': '~2 GB',
            'Salesforce/blip2-opt-2.7b': '~5 GB',
            'Salesforce/blip2-flan-t5-xl': '~8 GB',
        };
        return sizes[model] || '~2 GB';
    },

    /**
     * Returns approximate STT model download size.
     * @param {string} model - Whisper model size name.
     * @returns {string}
     * @private
     */
    _sttSize(model) {
        const sizes = {
            tiny: '~75 MB',
            base: '~140 MB',
            small: '~460 MB',
            medium: '~1.5 GB',
            'large-v3': '~3 GB',
        };
        return sizes[model] || '~140 MB';
    },

    // -----------------------------------------------------------------------
    // Step 4: HuggingFace token (optional)
    // -----------------------------------------------------------------------

    /**
     * Renders the optional HuggingFace token input step.
     * @param {HTMLElement} body - The wizard body container.
     * @private
     */
    _renderHFTokenStep(body) {
        const heading = document.createElement('h4');
        heading.textContent = 'HuggingFace Token (Optional)';
        body.appendChild(heading);

        const intro = document.createElement('p');
        intro.className = 'wizard-intro';
        intro.textContent = 'Many of Photonarium\u2019s AI models are hosted on HuggingFace. '
            + 'Providing an access token helps avoid download rate limits and is required '
            + 'for some gated models.';
        body.appendChild(intro);

        // Benefits list
        const benefits = document.createElement('ul');
        benefits.className = 'wizard-token-benefits';
        for (const text of [
            'Avoids some rate-limiting during downloads',
            'Required for gated models (some BLIP-2 variants)',
            'Faster, more reliable downloads',
        ]) {
            const li = document.createElement('li');
            li.textContent = text;
            benefits.appendChild(li);
        }
        body.appendChild(benefits);

        // How to get a token
        const howTo = document.createElement('p');
        howTo.className = 'wizard-token-howto';
        howTo.innerHTML = 'To create a token: sign up at '
            + '<a href="https://huggingface.co" target="_blank" rel="noopener">huggingface.co</a>'
            + ', go to <strong>Settings \u2192 Access Tokens</strong>, '
            + 'and create a token with <strong>Read</strong> permissions.';
        body.appendChild(howTo);

        const link = document.createElement('a');
        link.href = 'https://huggingface.co/settings/tokens';
        link.target = '_blank';
        link.rel = 'noopener';
        link.className = 'wizard-token-link';
        link.textContent = 'Open HuggingFace Token Settings \u2197';
        body.appendChild(link);

        // Input row (password input + show/hide toggle)
        const inputRow = document.createElement('div');
        inputRow.className = 'wizard-token-input-row';

        const input = document.createElement('input');
        input.type = 'password';
        input.className = 'wizard-token-input';
        input.dataset.key = 'hf-token';
        input.placeholder = 'hf_xxxxxxxxxxxxxxxxxxxxx';
        input.autocomplete = 'off';
        input.spellcheck = false;
        // Restore previously entered value when navigating back
        if (this._hfToken) input.value = this._hfToken;
        inputRow.appendChild(input);

        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.className = 'wizard-token-toggle';
        toggle.title = 'Show/hide token';
        toggle.innerHTML = '<span class="material-symbols-outlined">visibility</span>';
        toggle.addEventListener('click', () => {
            const isHidden = input.type === 'password';
            input.type = isHidden ? 'text' : 'password';
            toggle.innerHTML = '<span class="material-symbols-outlined">'
                + (isHidden ? 'visibility_off' : 'visibility') + '</span>';
        });
        inputRow.appendChild(toggle);

        body.appendChild(inputRow);

        // Reassurance note
        const note = document.createElement('p');
        note.className = 'wizard-token-note';
        note.textContent = 'Your token is only used for this download session and is not saved '
            + 'to your configuration.';
        body.appendChild(note);
    },

    // -----------------------------------------------------------------------
    // Step 5: Download
    // -----------------------------------------------------------------------

    /**
     * Renders the download step with start/abort/output controls.
     * @param {HTMLElement} body - The wizard body container.
     * @private
     */
    _renderDownloadStep(body) {
        const intro = document.createElement('p');
        intro.className = 'wizard-intro';
        intro.textContent = 'Download the required ML models. This may take several minutes '
            + 'depending on your internet connection and the models selected.';
        body.appendChild(intro);

        // Button row
        const btnRow = document.createElement('div');
        btnRow.className = 'wizard-download-actions';

        const startBtn = document.createElement('button');
        startBtn.className = 'action-btn primary';
        startBtn.textContent = 'Download Models';
        startBtn.id = 'wizard-download-start';
        startBtn.addEventListener('click', () => this._startDownload());
        btnRow.appendChild(startBtn);

        const abortBtn = document.createElement('button');
        abortBtn.className = 'action-btn danger';
        abortBtn.textContent = 'Abort';
        abortBtn.id = 'wizard-download-abort';
        abortBtn.style.display = 'none';
        abortBtn.addEventListener('click', () => this._abortDownload());
        btnRow.appendChild(abortBtn);

        body.appendChild(btnRow);

        // Output area
        const output = document.createElement('pre');
        output.className = 'wizard-output';
        output.id = 'wizard-download-output';
        body.appendChild(output);

        // Status message area
        const status = document.createElement('div');
        status.className = 'wizard-download-status';
        status.id = 'wizard-download-status';
        body.appendChild(status);

        // Footer buttons (managed here, not in _renderFooter)
        const footer = this._dialog.querySelector('.wizard-footer');
        if (footer) {
            footer.innerHTML = '';

            const skip = document.createElement('a');
            skip.href = '#';
            skip.className = 'wizard-skip-link';
            skip.textContent = 'Skip download';
            skip.title = 'You can download models later by running download_models.py';
            skip.addEventListener('click', (e) => {
                e.preventDefault();
                this._close();
            });
            footer.appendChild(skip);

            const spacer = document.createElement('div');
            spacer.style.flex = '1';
            footer.appendChild(spacer);

            const backBtn = document.createElement('button');
            backBtn.className = 'action-btn';
            backBtn.textContent = 'Back';
            backBtn.id = 'wizard-download-back';
            backBtn.addEventListener('click', () => {
                this._stopPolling();
                this._currentStep--;
                this._renderStep();
            });
            footer.appendChild(backBtn);
        }
    },

    /**
     * Starts the model download and begins polling for output.
     * @private
     */
    async _startDownload() {
        const startBtn = App.$('wizard-download-start');
        const abortBtn = App.$('wizard-download-abort');
        const backBtn = App.$('wizard-download-back');
        const output = App.$('wizard-download-output');

        if (startBtn) startBtn.style.display = 'none';
        if (abortBtn) abortBtn.style.display = '';
        if (backBtn) backBtn.disabled = true;
        if (output) output.textContent = '';

        this._linesSeen = 0;
        this._downloadStarted = true;

        try {
            const body = {};
            if (this._hfToken) body.hf_token = this._hfToken;
            await App.apiPost('/wizard/download', body);
            this._startPolling();
        } catch (error) {
            console.error('Failed to start download:', error);
            this._showDownloadStatus('Failed to start download.', 'error');
            if (startBtn) startBtn.style.display = '';
            if (abortBtn) abortBtn.style.display = 'none';
            if (backBtn) backBtn.disabled = false;
        }
    },

    /**
     * Aborts a running download.
     * @private
     */
    async _abortDownload() {
        this._stopPolling();
        try {
            await App.apiPost('/wizard/download-abort');
        } catch (error) {
            console.error('Failed to abort download:', error);
        }
        this._showDownloadStatus('Download aborted.', 'warning');
        this._showDownloadComplete(false);
    },

    /**
     * Starts polling the download status endpoint.
     * @private
     */
    _startPolling() {
        this._stopPolling();
        this._pollTimer = setInterval(() => this._pollDownloadStatus(), 1000);
    },

    /**
     * Stops polling the download status endpoint.
     * @private
     */
    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    },

    /**
     * Polls the download status and appends new output lines.
     * @private
     */
    async _pollDownloadStatus() {
        try {
            const response = await App.apiGet(`/wizard/download-status?since=${this._linesSeen}`);
            const data = response.data;
            const output = App.$('wizard-download-output');

            if (data.lines && data.lines.length > 0 && output) {
                output.textContent += data.lines.join('\n') + '\n';
                output.scrollTop = output.scrollHeight;
            }
            this._linesSeen = data.total_lines;

            // Check for completion
            if (data.state === 'completed') {
                this._stopPolling();
                this._showDownloadStatus('Models downloaded successfully.', 'success');
                this._showDownloadComplete(true);
            } else if (data.state === 'failed') {
                this._stopPolling();
                this._showDownloadStatus(
                    `Download failed (exit code ${data.return_code}). Check the output above for details.`,
                    'error',
                );
                this._showDownloadComplete(false);
            } else if (data.state === 'aborted') {
                this._stopPolling();
                this._showDownloadStatus('Download aborted.', 'warning');
                this._showDownloadComplete(false);
            }
        } catch (error) {
            console.error('Failed to poll download status:', error);
        }
    },

    /**
     * Shows a status message below the output area.
     * @param {string} message - Status message text.
     * @param {string} type - 'success', 'error', or 'warning'.
     * @private
     */
    _showDownloadStatus(message, type) {
        const status = App.$('wizard-download-status');
        if (!status) return;
        status.textContent = message;
        status.className = `wizard-download-status wizard-status-${type}`;
    },

    /**
     * Shows completion controls after download finishes or fails.
     * @param {boolean} success - Whether the download succeeded.
     * @private
     */
    _showDownloadComplete(success) {
        const abortBtn = App.$('wizard-download-abort');
        const backBtn = App.$('wizard-download-back');
        if (abortBtn) abortBtn.style.display = 'none';
        if (backBtn) backBtn.disabled = false;

        const footer = this._dialog.querySelector('.wizard-footer');
        if (!footer) return;

        // Remove existing finish/retry buttons
        footer.querySelectorAll('.wizard-finish-btn, .wizard-retry-btn').forEach(b => b.remove());

        if (!success) {
            const retryBtn = document.createElement('button');
            retryBtn.className = 'action-btn wizard-retry-btn';
            retryBtn.textContent = 'Retry';
            retryBtn.addEventListener('click', () => this._startDownload());
            footer.appendChild(retryBtn);
        }

        const finishBtn = document.createElement('button');
        finishBtn.className = 'action-btn primary wizard-finish-btn';
        finishBtn.textContent = success ? 'Finish' : 'Finish anyway';
        finishBtn.addEventListener('click', () => this._close());
        footer.appendChild(finishBtn);
    },

    // -----------------------------------------------------------------------
    // Config save (before download step)
    // -----------------------------------------------------------------------

    /**
     * Saves the wizard config selections and advances to the download step.
     * @private
     */
    async _saveAndContinue() {
        try {
            await App.apiPost('/wizard/save-config', { values: this._selections });
            this._currentStep++;
            this._renderStep();
        } catch (error) {
            console.error('Failed to save wizard config:', error);
            App.showError('Failed to save configuration. Please try again.');
        }
    },

    // -----------------------------------------------------------------------
    // UI helpers
    // -----------------------------------------------------------------------

    /**
     * Creates a selectable radio card element.
     * @param {string} title - Card title text.
     * @param {string} description - Card description text.
     * @param {boolean} selected - Whether the card is currently selected.
     * @param {Function} onClick - Click handler.
     * @returns {HTMLElement}
     * @private
     */
    _createCard(title, description, selected, onClick) {
        const card = document.createElement('div');
        card.className = 'wizard-card' + (selected ? ' wizard-card-selected' : '');
        card.tabIndex = 0;

        const titleEl = document.createElement('div');
        titleEl.className = 'wizard-card-title';
        titleEl.textContent = title;
        card.appendChild(titleEl);

        if (description) {
            const descEl = document.createElement('div');
            descEl.className = 'wizard-card-desc';
            descEl.textContent = description;
            card.appendChild(descEl);
        }

        card.addEventListener('click', onClick);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
            }
        });

        return card;
    },

    /**
     * Refreshes the selected state of all cards in a container.
     * @param {HTMLElement} container - Cards container.
     * @param {string} type - 'preset' or 'lang'.
     * @private
     */
    _refreshCards(container, type) {
        const selectedId = type === 'preset' ? this._selectedPreset : this._selectedLang;
        for (const card of container.querySelectorAll('.wizard-card')) {
            card.classList.toggle('wizard-card-selected', card.dataset.cardId === selectedId);
        }
    },

    /**
     * Enables the Next button (called after a selection is made).
     * @private
     */
    _enableNext() {
        const btn = App.$('wizard-next-btn');
        if (btn) btn.disabled = false;
    },
};
