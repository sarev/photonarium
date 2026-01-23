/**
 * @fileoverview Database management screen module for the Imaginary application.
 *
 * This module handles the Database screen where users manage image source
 * folders and trigger database scans. It registers with the core App module
 * and is shown by default when the database is empty.
 *
 * RESPONSIBILITIES:
 *
 * Folder Management:
 *   - Displays list of currently registered image source folders
 *   - Add folder button opens native folder picker dialog
 *   - Each folder entry shows path and image count from that folder
 *   - Remove button on each folder (with confirmation dialog)
 *   - Removing a folder removes all its images from the database
 *
 * Database Scanning:
 *   - "Rescan All Folders" button triggers a full database rescan
 *   - Adding a new folder automatically triggers a scan of that folder
 *   - Scans are performed asynchronously on the backend
 *   - Detects new, modified, and deleted images
 *   - Modified images are detected by timestamp or file size changes
 *
 * Progress Reporting:
 *   - Shows progress bar during scan operations
 *   - Displays current status text (e.g., "Scanning folder X..." or "Processing image Y...")
 *   - Progress bar shows percentage completion
 *   - Polls backend for progress updates during scan
 *   - Hides progress bar when scan completes
 *
 * Database Status:
 *   - Displays total image count in database
 *   - Updates count after scan completion or folder removal
 *   - Shows last scan timestamp
 *
 * Startup Behavior:
 *   - If database is empty on app start, this screen is shown automatically
 *   - Prompts user to add at least one folder to begin
 *
 * Error Handling:
 *   - Displays error messages if folder cannot be added (e.g., doesn't exist)
 *   - Shows warning if scan encounters unreadable files
 *   - Handles backend connection errors gracefully
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Fetches current folder list and database stats from backend
 *   - onLeave(): Cancels any pending progress polling
 *
 * @module database
 * @requires core
 */

/**
 * Database management screen module.
 * @namespace
 */
const Database = {
    /**
     * DOM element references.
     * @type {Object}
     * @private
     */
    _els: {},

    /**
     * Active scan job id if a scan is in progress.
     * @type {string|null}
     * @private
     */
    _scanJobId: null,

    /**
     * Progress polling timer id.
     * @type {number|null}
     * @private
     */
    _pollTimer: null,

    /**
     * Initializes the database module.
     * Called once during app startup.
     */
    init() {
        this._els = {
            folderList: App.$('folder-list'),
            addFolderBtn: App.$('btn-add-folder'),
            rescanBtn: App.$('btn-rescan'),
            statusTotal: App.$('status-total'),
            scanProgress: App.$('scan-progress'),
            progressFill: App.$('progress-fill'),
            progressText: App.$('progress-text')
        };

        this._bindEvents();
    },

    /**
     * Called when entering the database screen.
     */
    onEnter() {
        this._refresh();

        // If a scan was already running, resume polling.
        if (this._scanJobId) {
            this._startPolling();
        }
    },

    /**
     * Called when leaving the database screen.
     */
    onLeave() {
        this._stopPolling();
    },

    /**
     * Binds event listeners for database screen controls.
     * @private
     */
    _bindEvents() {
        this._els.addFolderBtn.addEventListener('click', () => this._addFolder());
        this._els.rescanBtn.addEventListener('click', () => this._rescanAll());
    },

    /* ----------------------------------------------------------------------
       Folder management
       ---------------------------------------------------------------------- */

    /**
     * Opens a native folder picker.
     * Returns a display path (not a real absolute path in browsers).
     * @returns {Promise<{ path: string } | null>}
     * @private
     */
    async _pickFolder() {
        // Preferred: File System Access API (Chromium-based, secure contexts).
        if (window.showDirectoryPicker) {
            try {
                const handle = await window.showDirectoryPicker();
                if (!handle) {
                    return null;
                }

                // Browsers do not expose absolute paths; use a stable display name for now.
                return { path: handle.name };
            } catch (error) {
                // AbortError is user cancel.
                if (error && error.name === 'AbortError') {
                    return null;
                }
                console.warn('showDirectoryPicker failed, falling back:', error);
                // Continue to fallback.
            }
        }

        // Fallback: <input webkitdirectory> (works in Chromium, often Safari).
        return await new Promise((resolve) => {
            const input = document.createElement('input');
            input.type = 'file';
            input.multiple = true;

            // Non-standard but widely supported for folder picking.
            input.setAttribute('webkitdirectory', '');
            input.setAttribute('directory', '');

            input.addEventListener('change', () => {
                const files = input.files ? Array.from(input.files) : [];
                if (files.length === 0) {
                    resolve(null);
                    return;
                }

                // webkitRelativePath is like "FolderName/subdir/file.jpg"
                const rel = files[0].webkitRelativePath || '';
                const top = rel.split('/')[0].trim();

                resolve({ path: top || 'Selected folder' });
            }, { once: true });

            input.click();
        });
    },

    /**
     * Prompts for a folder path and adds it to the database.
     * Note: Until the Python backend exists, we mock this with a prompt.
     * @private
     */
    async _addFolder() {
        const picked = await this._pickFolder();
        if (!picked || !picked.path) {
            return;
        }

        try {
            const resp = await App.apiPost('/folders', { path: picked.path });
            if (resp && resp.success === false) {
                App.showError(resp.error || 'Could not add folder.');
                return;
            }

            await this._refresh();

            // Adding a folder triggers a scan of that folder.
            await this._startScan({ folder: picked.path });
        } catch (error) {
            console.error('Error adding folder:', error);
            App.showError('Could not add folder.');
        }
    },

    /**
     * Removes a folder (with confirmation).
     * @param {string} path - Folder path
     * @private
     */
    async _removeFolder(path) {
        const ok = await App.confirm('Remove folder?', `Remove "${path}" and all its images from the database?`);
        if (!ok) {
            return;
        }

        try {
            await App.apiDelete(`/folders/${encodeURIComponent(path)}`);
            await this._refresh();
        } catch (error) {
            console.error('Error removing folder:', error);
            App.showError('Could not remove folder.');
        }
    },

    /**
     * Renders the folder list.
     * @param {Array<{path: string, count: number}>} folders
     * @private
     */
    _renderFolders(folders) {
        const list = this._els.folderList;
        list.innerHTML = '';

        for (const folder of folders) {
            const li = document.createElement('li');
            li.className = 'folder-item';

            const pathEl = document.createElement('span');
            pathEl.className = 'folder-path';
            pathEl.textContent = folder.path;

            const countEl = document.createElement('span');
            countEl.className = 'folder-count';
            countEl.textContent = `${folder.count || 0} images`;

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'toolbar-btn folder-remove';
            removeBtn.title = 'Remove folder';
            removeBtn.innerHTML = '<span class="material-symbols-outlined">delete</span>';
            removeBtn.addEventListener('click', () => this._removeFolder(folder.path));

            li.appendChild(pathEl);
            li.appendChild(countEl);
            li.appendChild(removeBtn);

            list.appendChild(li);
        }

        // Disable rescan if there are no folders.
        this._els.rescanBtn.disabled = folders.length === 0;
    },

    /* ----------------------------------------------------------------------
       Status + refresh
       ---------------------------------------------------------------------- */

    /**
     * Refreshes folders and stats from the backend.
     * @private
     */
    async _refresh() {
        try {
            const [folders, stats] = await Promise.all([
                App.apiGet('/folders'),
                App.apiGet('/stats')
            ]);

            this._renderFolders(Array.isArray(folders) ? folders : []);
            this._els.statusTotal.textContent = stats && typeof stats.totalImages === 'number' ? String(stats.totalImages) : '0';
        } catch (error) {
            console.error('Error loading database status:', error);
            App.showError('Could not load database status.');
        }
    },

    /* ----------------------------------------------------------------------
       Scanning + progress
       ---------------------------------------------------------------------- */

    /**
     * Triggers a scan.
     * @param {Object} payload - Scan payload
     * @private
     */
    async _startScan(payload) {
        try {
            const resp = await App.apiPost('/scan', payload || {});
            if (!resp || !resp.jobId) {
                throw new Error('Scan did not return a jobId');
            }

            this._scanJobId = resp.jobId;
            this._showProgress(0, 'Queued...');
            this._startPolling();
        } catch (error) {
            console.error('Error starting scan:', error);
            App.showError('Could not start scan.');
        }
    },

    /**
     * Rescans all folders.
     * @private
     */
    async _rescanAll() {
        try {
            const folders = await App.apiGet('/folders');
            if (!Array.isArray(folders) || folders.length === 0) {
                App.showError('Add a folder first.');
                return;
            }

            await this._startScan({ folders: folders.map(f => f.path) });
        } catch (error) {
            console.error('Error initiating rescan:', error);
            App.showError('Could not start rescan.');
        }
    },

    /**
     * Starts polling scan progress.
     * @private
     */
    _startPolling() {
        if (!this._scanJobId) {
            return;
        }

        if (this._pollTimer) {
            return;
        }

        const poll = () => this._pollOnce();
        this._pollTimer = window.setInterval(poll, 750);
        poll();
    },

    /**
     * Stops polling scan progress.
     * @private
     */
    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    },

    /**
     * Polls the backend once for scan progress.
     * @private
     */
    async _pollOnce() {
        if (!this._scanJobId) {
            return;
        }

        try {
            const status = await App.apiGet(`/scan/${encodeURIComponent(this._scanJobId)}`);
            if (!status) {
                return;
            }

            const progress = typeof status.progress === 'number' ? status.progress : 0;
            const text = status.message || (status.status === 'complete' ? 'Scan complete' : 'Scanning...');

            this._showProgress(progress, text);

            if (status.status === 'complete' || status.status === 'error') {
                this._stopPolling();
                this._scanJobId = null;

                // Pull fresh counts after scan completes.
                await this._refresh();

                // Hide progress a moment later to avoid flicker.
                window.setTimeout(() => this._hideProgress(), 600);
            }
        } catch (error) {
            console.error('Error polling scan status:', error);
            // Keep polling; transient backend failures are expected.
        }
    },

    /**
     * Shows and updates the progress UI.
     * @param {number} progress - Progress percent 0..100
     * @param {string} text - Status text
     * @private
     */
    _showProgress(progress, text) {
        const pct = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));

        this._els.scanProgress.hidden = false;
        this._els.progressFill.style.width = `${pct}%`;
        this._els.progressText.textContent = text || 'Scanning...';
    },

    /**
     * Hides the progress UI.
     * @private
     */
    _hideProgress() {
        this._els.scanProgress.hidden = true;
        this._els.progressFill.style.width = '0%';
        this._els.progressText.textContent = '';
    }
};

App.registerModule('database', Database);
