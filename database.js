/**
 * @fileoverview Database management screen module for the Imaginary application.
 *
 * This module handles the Database screen where users manage image source
 * folders and monitor database processing status. It registers with the core
 * App module and is shown by default when the database is empty.
 *
 * RESPONSIBILITIES:
 *
 * Folder Management:
 *   - Displays list of currently registered image source folders
 *   - Add folder button opens native folder picker dialog
 *   - Each folder entry shows path and image count from that folder
 *   - Remove button on each folder (with confirmation dialog)
 *   - Removing a folder marks its images as deleted in the database
 *
 * Processing Status:
 *   - Shows current database status: "Up to date" or "Updating"
 *   - When updating, displays queue counts:
 *     - Indexing: N remaining (ingestion queue)
 *     - Embedding: N remaining (embedding queue)
 *   - Polls backend for status updates while on this screen
 *   - Status updates automatically as background threads process
 *
 * Database Statistics:
 *   - Displays total image count in database
 *   - Updates count as processing completes
 *
 * Startup Behavior:
 *   - If database is empty on app start, this screen is shown automatically
 *   - Prompts user to add at least one folder to begin
 *
 * Error Handling:
 *   - Displays error messages if folder cannot be added (e.g., doesn't exist)
 *   - Handles backend connection errors gracefully
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Fetches folder list, stats, and starts status polling
 *   - onLeave(): Stops status polling
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
     * Status polling timer id.
     * @type {number|null}
     * @private
     */
    _pollTimer: null,

    /**
     * Last known processing status to detect changes.
     * @type {string|null}
     * @private
     */
    _lastStatus: null,

    /**
     * History of indexing queue samples for ETA calculation.
     * Each entry is {count, timestamp}.
     * @type {Array<{count: number, timestamp: number}>}
     * @private
     */
    _indexingHistory: [],

    /**
     * History of embedding queue samples for ETA calculation.
     * Each entry is {count, timestamp}.
     * @type {Array<{count: number, timestamp: number}>}
     * @private
     */
    _embeddingHistory: [],

    /**
     * Maximum number of samples to keep for ETA calculation.
     * @type {number}
     * @private
     */
    _maxHistorySamples: 10,

    /**
     * Initialises the database module.
     * Called once during app startup.
     */
    init() {
        this._els = {
            folderList: App.$('folder-list'),
            addFolderBtn: App.$('btn-add-folder'),
            rescanBtn: App.$('btn-rescan'),
            statusTotal: App.$('status-total'),
            processingStatus: App.$('processing-status'),
            statusIndicator: App.$('status-indicator'),
            statusText: App.$('status-text'),
            queueCounts: App.$('queue-counts'),
            indexingCount: App.$('indexing-count'),
            indexingEta: App.$('indexing-eta'),
            embeddingCount: App.$('embedding-count'),
            embeddingEta: App.$('embedding-eta')
        };

        this._bindEvents();
    },

    /**
     * Called when entering the database screen.
     */
    onEnter() {
        this._refresh();
        this._startPolling();
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
     * Opens a native folder picker dialog via the backend.
     * The backend uses tkinter to show an OS-native folder selection dialog.
     * @returns {Promise<{ path: string } | null>}
     * @private
     */
    async _pickFolder() {
        try {
            const result = await App.apiPost('/pick-folder', {});
            if (result && result.path) {
                return { path: result.path };
            }
            return null;
        } catch (error) {
            console.error('Error opening folder picker:', error);
            App.showError('Could not open folder picker.');
            return null;
        }
    },

    /**
     * Prompts for a folder path and adds it to the database.
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
            // No need to start scan - backend automatically queues new folder contents
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

    /**
     * Triggers a full rescan of all registered folders.
     * @private
     */
    async _rescanAll() {
        try {
            const resp = await App.apiPost('/rescan');
            if (resp && resp.success === false) {
                App.showError(resp.error || 'Could not start rescan.');
                return;
            }
            // Status polling will pick up the new queue items
        } catch (error) {
            console.error('Error initiating rescan:', error);
            App.showError('Could not start rescan.');
        }
    },

    /* ----------------------------------------------------------------------
       Processing status polling
       ---------------------------------------------------------------------- */

    /**
     * Starts polling for processing status.
     * @private
     */
    _startPolling() {
        if (this._pollTimer) {
            return;
        }

        const poll = () => this._pollStatus();
        this._pollTimer = window.setInterval(poll, 1000);
        poll(); // Immediate first poll
    },

    /**
     * Stops polling for processing status.
     * @private
     */
    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    },

    /**
     * Polls the backend for current processing status.
     * @private
     */
    async _pollStatus() {
        try {
            const status = await App.apiGet('/status');
            this._updateStatusDisplay(status);

            // If status changed to 'up_to_date', refresh the stats
            if (status.status === 'up_to_date' && this._lastStatus === 'updating') {
                await this._refresh();
                App.emit('databaseChanged');
            }

            this._lastStatus = status.status;
        } catch (error) {
            console.error('Error polling status:', error);
            // Don't show error toast for polling failures
        }
    },

    /**
     * Updates the status display based on backend response.
     * @param {Object} status - Status object from backend
     * @param {string} status.status - 'up_to_date' or 'updating'
     * @param {number} status.indexing_queue - Items in ingestion queue
     * @param {number} status.embedding_queue - Items in embedding queue
     * @param {number} status.total_images - Total images in database
     * @private
     */
    _updateStatusDisplay(status) {
        if (!status) {
            return;
        }

        const isUpdating = status.status === 'updating';
        const indexing = status.indexing_queue || 0;
        const embedding = status.embedding_queue || 0;

        // Update indicator class
        this._els.statusIndicator.className = 'status-indicator ' + (isUpdating ? 'updating' : 'up-to-date');

        // Update status text
        this._els.statusText.textContent = isUpdating ? 'Updating' : 'Up to date';

        // Update total images count (always, so it updates during indexing)
        if (typeof status.total_images === 'number') {
            this._els.statusTotal.textContent = String(status.total_images);
        }

        // Show/hide queue counts
        if (isUpdating && (indexing > 0 || embedding > 0)) {
            this._els.queueCounts.hidden = false;
            this._els.indexingCount.textContent = indexing;
            this._els.embeddingCount.textContent = embedding;

            // Update ETAs
            this._updateIndexingEta(indexing);
            this._updateEmbeddingEta(embedding);
        } else {
            this._els.queueCounts.hidden = true;
            // Clear history when not updating
            this._indexingHistory = [];
            this._embeddingHistory = [];
            this._els.indexingEta.textContent = '';
            this._els.embeddingEta.textContent = '';
        }
    },

    /**
     * Updates the indexing ETA based on processing rate.
     * @param {number} currentCount - Current indexing queue size
     * @private
     */
    _updateIndexingEta(currentCount) {
        const now = Date.now();

        // Add current sample to history
        this._indexingHistory.push({ count: currentCount, timestamp: now });

        // Keep only recent samples
        if (this._indexingHistory.length > this._maxHistorySamples) {
            this._indexingHistory.shift();
        }

        // Need at least 2 samples to calculate rate
        if (this._indexingHistory.length < 2) {
            this._els.indexingEta.textContent = '';
            return;
        }

        // Calculate processing rate from oldest to newest sample
        const oldest = this._indexingHistory[0];
        const newest = this._indexingHistory[this._indexingHistory.length - 1];
        const countDiff = oldest.count - newest.count;
        const timeDiff = (newest.timestamp - oldest.timestamp) / 1000; // seconds

        // If no progress or queue growing, can't estimate
        if (countDiff <= 0 || timeDiff <= 0) {
            this._els.indexingEta.textContent = '';
            return;
        }

        // Calculate rate (images per second) and ETA
        const rate = countDiff / timeDiff;
        const etaSeconds = currentCount / rate;

        // Format ETA
        this._els.indexingEta.textContent = ' (' + this._formatEta(etaSeconds) + ')';
    },

    /**
     * Formats seconds into a human-readable ETA string.
     * @param {number} seconds - Estimated seconds remaining
     * @returns {string} Formatted ETA string
     * @private
     */
    _formatEta(seconds) {
        if (seconds < 60) {
            return '< 1 min';
        } else if (seconds < 3600) {
            const mins = Math.ceil(seconds / 60);
            return mins === 1 ? '~1 min' : `~${mins} mins`;
        } else {
            const hours = Math.floor(seconds / 3600);
            const mins = Math.ceil((seconds % 3600) / 60);
            if (mins === 0) {
                return hours === 1 ? '~1 hour' : `~${hours} hours`;
            }
            return hours === 1 ? `~1 hour ${mins} mins` : `~${hours} hours ${mins} mins`;
        }
    },

    /**
     * Updates the embedding ETA based on processing rate.
     * @param {number} currentCount - Current embedding queue size
     * @private
     */
    _updateEmbeddingEta(currentCount) {
        const now = Date.now();

        // Add current sample to history
        this._embeddingHistory.push({ count: currentCount, timestamp: now });

        // Keep only recent samples
        if (this._embeddingHistory.length > this._maxHistorySamples) {
            this._embeddingHistory.shift();
        }

        // Need at least 2 samples to calculate rate
        if (this._embeddingHistory.length < 2) {
            this._els.embeddingEta.textContent = '';
            return;
        }

        // Calculate processing rate from oldest to newest sample
        const oldest = this._embeddingHistory[0];
        const newest = this._embeddingHistory[this._embeddingHistory.length - 1];
        const countDiff = oldest.count - newest.count;
        const timeDiff = (newest.timestamp - oldest.timestamp) / 1000; // seconds

        // If no progress or queue growing, can't estimate
        if (countDiff <= 0 || timeDiff <= 0) {
            this._els.embeddingEta.textContent = '';
            return;
        }

        // Calculate rate (images per second) and ETA
        const rate = countDiff / timeDiff;
        const etaSeconds = currentCount / rate;

        // Format ETA
        this._els.embeddingEta.textContent = ' (' + this._formatEta(etaSeconds) + ')';
    }
};

App.registerModule('database', Database);
