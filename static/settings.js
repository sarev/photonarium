/**
 * @fileoverview In-app configuration editor for the Photonarium application.
 *
 * Provides a modal dialog that fetches the full config schema from the backend
 * and renders a generic settings form.  The frontend has zero knowledge of
 * individual settings — it renders whatever the schema describes.
 *
 * RESPONSIBILITIES:
 *
 * Schema-Driven Form:
 *   - Fetches GET /api/config/schema on open (sections, fields, constraints)
 *   - Renders sections, field labels, help text, and typed inputs
 *   - Fields with warning: true get a visual danger indicator
 *
 * Input Types:
 *   - "string"  → <input type="text">
 *   - "integer" / "number" → <input type="number" min/max/step>
 *   - "boolean" → <input type="checkbox">
 *   - "set"     → <textarea> (one value per line)
 *
 * Save Flow:
 *   - Client-side range validation from input min/max attributes
 *   - POST /api/config/save with collected values
 *   - On success: close dialog, info toast with restart message
 *   - On error: error toast with backend validation message
 *
 * @module settings
 * @requires core
 */

/**
 * Settings editor — standalone object (not a screen module).
 * @namespace
 */
const Settings = {

    /** @type {HTMLDialogElement|null} @private */
    _dialog: null,

    /** @type {Object|null} Last fetched schema (for value collection). @private */
    _schema: null,

    /**
     * Opens the settings dialog, fetching the schema from the backend.
     */
    async show() {
        this._dialog = App.$('dialog-settings');
        if (!this._dialog) return;

        try {
            const response = await App.apiGet('/config/schema');
            const schema = response.data;
            this._schema = schema;

            // Populate the config path link
            const pathEl = App.$('settings-config-path');
            const linkEl = App.$('settings-reveal-link');
            if (pathEl && schema.config_path) {
                pathEl.textContent = schema.config_path;
                pathEl.title = schema.config_path;
            }
            // Wire the "reveal in file manager" link
            if (linkEl) {
                linkEl.onclick = async (e) => {
                    e.preventDefault();
                    try {
                        await App.apiPost('/reveal', { target: 'config' });
                    } catch {
                        App.showError('Could not open config file location.');
                    }
                };
            }

            this._buildForm(schema);
            this._bindDialogEvents();
            this._dialog.showModal();
        } catch (error) {
            console.error('Failed to load settings schema:', error);
            App.showError('Could not load settings.');
        }
    },

    /**
     * Builds the form from the schema, populating the settings-content container.
     * @param {Object} schema - Schema from GET /api/config/schema
     * @private
     */
    _buildForm(schema) {
        const container = App.$('settings-content');
        if (!container) return;
        container.innerHTML = '';

        for (const section of schema.sections) {
            // Section heading
            const heading = document.createElement('h4');
            heading.className = 'settings-section-title';
            heading.textContent = section.title;
            container.appendChild(heading);

            // Fields
            for (const field of section.fields) {
                container.appendChild(this._createField(field));
            }
        }
    },

    /**
     * Creates a single field element from its schema definition.
     * @param {Object} field - Field schema with key, value, type, comment, constraints, warning
     * @returns {HTMLElement} The .settings-field container
     * @private
     */
    _createField(field) {
        const wrapper = document.createElement('div');
        wrapper.className = 'settings-field';
        if (field.warning) {
            wrapper.classList.add('settings-field-warning');
        }

        // Label row: field key + optional warning icon
        const label = document.createElement('label');
        label.className = 'settings-key';
        label.textContent = field.key;
        if (field.warning) {
            const icon = document.createElement('span');
            icon.className = 'material-symbols-outlined settings-warning-icon';
            icon.textContent = 'warning';
            icon.title = 'Changing this setting may require reconfiguration';
            label.appendChild(icon);
        }
        wrapper.appendChild(label);

        // Help text
        if (field.comment) {
            const help = document.createElement('p');
            help.className = 'settings-help';
            help.textContent = field.comment;
            wrapper.appendChild(help);
        }

        // Input element — type-dependent
        const input = this._createInput(field);
        wrapper.appendChild(input);

        return wrapper;
    },

    /**
     * Creates the appropriate input element for a field.
     * @param {Object} field - Field schema
     * @returns {HTMLElement} The input/textarea/checkbox wrapper
     * @private
     */
    _createInput(field) {
        const c = field.constraints || {};

        if (field.type === 'boolean') {
            // Checkbox with label wrapper for click area
            const row = document.createElement('div');
            row.className = 'settings-checkbox-row';
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.checked = !!field.value;
            cb.dataset.key = field.key;
            cb.dataset.type = 'boolean';
            const span = document.createElement('span');
            span.textContent = field.value ? 'Enabled' : 'Disabled';
            cb.addEventListener('change', () => {
                span.textContent = cb.checked ? 'Enabled' : 'Disabled';
            });
            row.appendChild(cb);
            row.appendChild(span);
            return row;
        }

        if (field.type === 'set') {
            // Textarea — one extension per line
            const ta = document.createElement('textarea');
            ta.className = 'dialog-input settings-textarea';
            ta.dataset.key = field.key;
            ta.dataset.type = 'set';
            ta.rows = 4;
            // Value is an array of strings
            ta.value = Array.isArray(field.value)
                ? field.value.join('\n')
                : String(field.value);
            return ta;
        }

        if (field.type === 'integer' || field.type === 'number') {
            const input = document.createElement('input');
            input.type = 'number';
            input.className = 'dialog-input settings-number';
            input.dataset.key = field.key;
            input.dataset.type = field.type;
            input.value = field.value;
            if (c.min !== undefined) input.min = c.min;
            if (c.max !== undefined) input.max = c.max;
            if (c.step !== undefined) input.step = c.step;
            return input;
        }

        // Default: text input for strings
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'dialog-input';
        input.dataset.key = field.key;
        input.dataset.type = 'string';
        input.value = field.value ?? '';
        return input;
    },

    /**
     * Binds Save/Cancel button handlers and Escape key.
     * @private
     */
    _bindDialogEvents() {
        const saveBtn = App.$('settings-save');
        const cancelBtn = App.$('settings-cancel');

        // Remove old listeners by cloning
        const newSave = saveBtn.cloneNode(true);
        saveBtn.parentNode.replaceChild(newSave, saveBtn);
        const newCancel = cancelBtn.cloneNode(true);
        cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);

        newSave.addEventListener('click', () => this._save());
        newCancel.addEventListener('click', () => this._close());

        // Close on backdrop click (native dialog behaviour fires 'cancel')
        this._dialog.oncancel = () => this._close();
    },

    /**
     * Validates inputs client-side using min/max attributes.
     * @returns {string|null} Error message or null if valid
     * @private
     */
    _validate() {
        if (!this._dialog) return null;

        const inputs = this._dialog.querySelectorAll('input[type="number"]');
        for (const input of inputs) {
            const val = parseFloat(input.value);
            const key = input.dataset.key;
            if (isNaN(val)) {
                return `${key}: please enter a valid number.`;
            }
            if (input.min !== '' && val < parseFloat(input.min)) {
                // Allow special_zero: check schema constraints
                const field = this._findField(key);
                if (field?.constraints?.special_zero && val === 0) continue;
                return `${key}: minimum value is ${input.min}.`;
            }
            if (input.max !== '' && val > parseFloat(input.max)) {
                return `${key}: maximum value is ${input.max}.`;
            }
        }
        return null;
    },

    /**
     * Finds a field definition in the cached schema by key.
     * @param {string} key - Field key
     * @returns {Object|null} Field schema object or null
     * @private
     */
    _findField(key) {
        if (!this._schema) return null;
        for (const section of this._schema.sections) {
            for (const field of section.fields) {
                if (field.key === key) return field;
            }
        }
        return null;
    },

    /**
     * Collects all input values into a {key: value} dict with proper types.
     * @returns {Object} Values dict keyed by field name
     * @private
     */
    _collectValues() {
        const values = {};
        if (!this._dialog) return values;

        // Checkboxes
        this._dialog.querySelectorAll('input[type="checkbox"][data-key]').forEach(cb => {
            values[cb.dataset.key] = cb.checked;
        });

        // Number inputs
        this._dialog.querySelectorAll('input[type="number"][data-key]').forEach(input => {
            const val = parseFloat(input.value);
            if (input.dataset.type === 'integer') {
                values[input.dataset.key] = Math.round(val);
            } else {
                values[input.dataset.key] = val;
            }
        });

        // Text inputs
        this._dialog.querySelectorAll('input[type="text"][data-key]').forEach(input => {
            values[input.dataset.key] = input.value;
        });

        // Textareas (set type — split by lines)
        this._dialog.querySelectorAll('textarea[data-key]').forEach(ta => {
            values[ta.dataset.key] = ta.value
                .split('\n')
                .map(line => line.trim())
                .filter(line => line.length > 0);
        });

        return values;
    },

    /**
     * Validates and saves the current form values.
     * @private
     */
    async _save() {
        // Client-side validation
        const error = this._validate();
        if (error) {
            App.showError(error);
            return;
        }

        const values = this._collectValues();

        try {
            const response = await App.apiPost('/config/save', { values });
            this._close();
            App.showInfo(
                response.message || 'Settings saved. Restart Photonarium for changes to take effect.',
            );
        } catch (err) {
            // Backend validation error — show the message
            const msg = err?.data?.error || err?.message || 'Failed to save settings.';
            App.showError(msg);
        }
    },

    /**
     * Closes the settings dialog.
     * @private
     */
    _close() {
        if (this._dialog) {
            this._dialog.close();
        }
    },
};
