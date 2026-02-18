/**
 * @fileoverview Database management screen module for the Photonarium application.
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
     * Last known processing status to detect changes.
     * @type {string|null}
     * @private
     */
    _lastStatus: null,

    /**
     * AppState subscription cleanup functions.
     * @type {Array<Function>}
     * @private
     */
    _unsubs: [],

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
     * History of face detection queue samples for ETA calculation.
     * Each entry is {count, timestamp}.
     * @type {Array<{count: number, timestamp: number}>}
     * @private
     */
    _faceHistory: [],

    /**
     * History of NIMA scoring queue samples for ETA calculation.
     * Each entry is {count, timestamp}.
     * @type {Array<{count: number, timestamp: number}>}
     * @private
     */
    _nimaHistory: [],

    /**
     * Maximum number of samples to keep for ETA calculation.
     * @type {number}
     * @private
     */
    _maxHistorySamples: 10,

    /**
     * Cached catalogue_dir from /api/config (empty string if disabled).
     * @type {string}
     * @private
     */
    _catalogueDir: '',

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
            statusPeople: App.$('status-people'),
            statusFaces: App.$('status-faces'),
            statusTrashed: App.$('status-trashed'),
            processingStatus: App.$('processing-status'),
            statusIndicator: App.$('status-indicator'),
            statusText: App.$('status-text'),
            queueCounts: App.$('queue-counts'),
            indexingCount: App.$('indexing-count'),
            indexingEta: App.$('indexing-eta'),
            embeddingCount: App.$('embedding-count'),
            embeddingEta: App.$('embedding-eta'),
            faceQueueRow: App.$('face-queue-row'),
            faceCount: App.$('face-count'),
            faceEta: App.$('face-eta'),
            nimaQueueRow: App.$('nima-queue-row'),
            nimaCount: App.$('nima-count'),
            nimaEta: App.$('nima-eta'),
            trashQueueRow: App.$('trash-queue-row'),
            trashQueueCount: App.$('trash-queue-count'),
            importQueueRow: App.$('import-queue-row'),
            importQueueCount: App.$('import-queue-count'),
            importSkippedCount: App.$('import-skipped-count'),
            // Phase 4 status elements
            duplicatesRow: App.$('duplicates-row'),
            duplicatesStatus: App.$('duplicates-status'),
            faceGroupingRow: App.$('face-grouping-row'),
            faceGroupingStatus: App.$('face-grouping-status'),
            faceReassessRow: App.$('face-reassess-row'),
            faceReassessStatus: App.$('face-reassess-status'),
            faceEmbeddingsRow: App.$('face-embeddings-row'),
            faceEmbeddingsStatus: App.$('face-embeddings-status'),
            revealConfigBtn: App.$('btn-reveal-config'),
            trashedLink: App.$('status-trashed-link'),
            // Import section elements
            importSection: App.$('import-section'),
            importDropZone: App.$('import-drop-zone'),
            importFolderBtn: App.$('btn-import-folder'),
            importFilesBtn: App.$('btn-import-files'),
            importDirBtn: App.$('btn-import-dir'),
            importFileInput: App.$('import-file-input'),
            importDirInput: App.$('import-dir-input'),
            importStatusText: App.$('import-status-text'),
        };

        // Fetch catalogue_dir from config to decide import visibility
        this._loadCatalogueDir();

        this._bindEvents();

        // Subscribe to AppState.folders changes for auto-rendering
        this._unsubs.push(AppState.folders.onChanged((event) => {
            if (App.getScreen() === 'database') {
                // Re-render folder list when folders change
                if (event.type === 'changed' && !event.property) {
                    this._renderFolders(AppState.folders.getAll());
                }
                // Handle stats update
                if (event.type === 'changed' && event.property === 'stats') {
                    const stats = AppState.folders.getStats();
                    if (stats) {
                        if (typeof stats.totalImages === 'number') {
                            this._els.statusTotal.textContent = String(stats.totalImages);
                        }
                        if (typeof stats.totalPeople === 'number') {
                            this._els.statusPeople.textContent = String(stats.totalPeople);
                        }
                        if (typeof stats.totalFaces === 'number') {
                            this._els.statusFaces.textContent = String(stats.totalFaces);
                        }
                        if (typeof stats.totalTrashed === 'number') {
                            this._els.statusTrashed.textContent = String(stats.totalTrashed);
                            this._updateTrashedLink(stats.totalTrashed);
                        }
                    }
                }
                // Handle databaseChanged event (processing completed)
                if (event.type === 'databaseChanged') {
                    AppState.folders.loadStats();
                    App.emit('databaseChanged');
                }
            }
        }));

        // Subscribe to AppState.status changes for status display
        this._unsubs.push(AppState.status.onChanged(() => {
            if (App.getScreen() === 'database') {
                const status = AppState.status.get();
                if (status) {
                    // Update AppState.folders with status for databaseChanged detection
                    AppState.folders.setStatus(status);
                    // Update local display
                    this._updateStatusDisplay(status);
                    this._lastStatus = status.status;
                }
            }
        }));
    },

    /**
     * Called when entering the database screen.
     */
    onEnter() {
        this._refresh();
        // Start status polling via AppState
        AppState.status.startPolling(1000);

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
     * Called when leaving the database screen.
     */
    onLeave() {
        // Stop status polling via AppState
        AppState.status.stopPolling();

        // Remove Escape key handler
        if (this._escapeHandler) {
            document.removeEventListener('keydown', this._escapeHandler);
            this._escapeHandler = null;
        }
    },

    /**
     * Binds event listeners for database screen controls.
     * @private
     */
    _bindEvents() {
        this._els.addFolderBtn.addEventListener('click', () => this._addFolder());
        this._els.rescanBtn.addEventListener('click', () => this._rescanAll());
        this._els.revealConfigBtn.addEventListener('click', () => Settings.show());

        // "Trashed" stat link — opens the trash directory in the file manager
        if (this._els.trashedLink) {
            this._els.trashedLink.addEventListener('click', async (e) => {
                e.preventDefault();
                if (this._els.trashedLink.classList.contains('disabled')) return;
                try {
                    await App.apiPost('/reveal', { target: 'trash' });
                } catch {
                    App.showError('Could not open trash folder.');
                }
            });
        }

        // --- Import section event bindings ---

        // Drag-and-drop on the import drop zone (desktop only)
        const zone = this._els.importDropZone;
        if (zone) {
            zone.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'copy';
                zone.classList.add('drag-over');
            });
            zone.addEventListener('dragleave', () => {
                zone.classList.remove('drag-over');
            });
            zone.addEventListener('drop', (e) => {
                e.preventDefault();
                zone.classList.remove('drag-over');
                this._handleDrop(e.dataTransfer);
            });
        }

        // "Pick Folder" in import section — uses native folder picker,
        // then offers choice between Add Folder and Import
        if (this._els.importFolderBtn) {
            this._els.importFolderBtn.addEventListener('click', async () => {
                const picked = await this._pickFolder();
                if (!picked || !picked.path) return;
                this._showAddChoice(picked.path);
            });
        }

        // "Pick Photos" — opens file picker, uploads via preflight dedup
        if (this._els.importFilesBtn && this._els.importFileInput) {
            this._els.importFilesBtn.addEventListener('click', () => {
                this._els.importFileInput.click();
            });
            this._els.importFileInput.addEventListener('change', () => {
                const files = this._els.importFileInput.files;
                if (files && files.length > 0) {
                    this._uploadFiles(files);
                }
                // Reset so the same files can be re-selected
                this._els.importFileInput.value = '';
            });
        }

        // "Pick Folder" on mobile (webkitdirectory) — uploads folder contents
        if (this._els.importDirBtn && this._els.importDirInput) {
            // Show this button only if webkitdirectory is supported AND on mobile
            if ('webkitdirectory' in this._els.importDirInput && window.innerWidth <= 768) {
                this._els.importDirBtn.hidden = false;
            }
            this._els.importDirBtn.addEventListener('click', () => {
                this._els.importDirInput.click();
            });
            this._els.importDirInput.addEventListener('change', () => {
                const files = this._els.importDirInput.files;
                if (files && files.length > 0) {
                    this._uploadFiles(files);
                }
                this._els.importDirInput.value = '';
            });
        }
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
            const response = await App.apiPost('/pick-folder', {});
            const result = response.data;
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
     * If a catalogue directory is configured, shows a choice dialog
     * (Add Folder vs Import). Otherwise adds the folder directly.
     * @private
     */
    async _addFolder() {
        const picked = await this._pickFolder();
        if (!picked || !picked.path) {
            return;
        }

        // If catalogue is configured, offer the choice
        if (this._catalogueDir) {
            this._showAddChoice(picked.path);
            return;
        }

        try {
            await AppState.folders.add(picked.path);
            // Folder list re-renders via onChanged subscription
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
            await AppState.folders.remove(path);
            // Folder list re-renders via onChanged subscription
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

        // Normalise catalogue path for comparison (lowercase + forward slashes on Windows)
        const normPath = (p) => p.replace(/\\/g, '/').toLowerCase();
        const catalogueNorm = this._catalogueDir ? normPath(this._catalogueDir) : '';

        for (const folder of folders) {
            const li = document.createElement('li');
            li.className = 'folder-item';

            const pathEl = document.createElement('span');
            pathEl.className = 'folder-path';
            pathEl.textContent = folder.path;

            const isCatalogue = catalogueNorm && normPath(folder.path) === catalogueNorm;

            const countEl = document.createElement('span');
            countEl.className = 'folder-count';
            countEl.textContent = `${folder.count || 0} images`;

            // Show a "catalogue" badge after the image count
            if (isCatalogue) {
                const badge = document.createElement('span');
                badge.className = 'folder-catalogue-badge';
                badge.textContent = 'catalogue';
                badge.title = 'Managed import directory \u2014 images are copied here when you use Import';
                countEl.appendChild(badge);
            }

            const removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'toolbar-btn folder-remove';
            if (isCatalogue) {
                removeBtn.disabled = true;
                removeBtn.title = 'Catalogue directory cannot be removed';
            } else {
                removeBtn.title = "Remove folder from Photonarium (doesn't affect the folder/files on disk!)";
            }
            removeBtn.innerHTML = App.icon('delete', '\u{1F5D1}');
            if (!isCatalogue) {
                removeBtn.addEventListener('click', () => this._removeFolder(folder.path));
            }

            const infoEl = document.createElement('div');
            infoEl.className = 'folder-info';
            infoEl.appendChild(pathEl);
            infoEl.appendChild(countEl);

            li.appendChild(infoEl);
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
            await Promise.all([
                AppState.folders.load(),
                AppState.folders.loadStats(),
            ]);
            this._renderFolders(AppState.folders.getAll());
            // Stats will be updated via subscription
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
            await AppState.folders.rescan();
            // Status polling will pick up the new queue items
        } catch (error) {
            console.error('Error initiating rescan:', error);
            App.showError('Could not start rescan.');
        }
    },

    /**
     * Enables or disables the "Trashed" reveal link based on the count.
     * @param {number} count - Current number of trashed images
     * @private
     */
    _updateTrashedLink(count) {
        if (!this._els.trashedLink) return;
        this._els.trashedLink.classList.toggle('disabled', count === 0);
    },

    /**
     * Updates the status display based on backend response.
     * @param {Object} status - Status object from backend
     * @param {string} status.status - 'up_to_date' or 'updating'
     * @param {number} status.indexing_queue - Items in ingestion queue
     * @param {number} status.embedding_queue - Items in embedding queue
     * @param {number} status.face_queue - Items in face detection queue
     * @param {number} status.total_images - Total images in database
     * @param {Object} [status.duplicates] - Duplicate detection status (if computing)
     * @param {Object} [status.face_grouping] - Face grouping status (if computing)
     * @param {Object} [status.face_reassess] - Face reassessment status (if computing)
     * @param {Object} [status.face_embeddings] - Face CLIP embedding status (if computing)
     * @private
     */
    _updateStatusDisplay(status) {
        if (!status) {
            return;
        }

        const isUpdating = status.status === 'updating';
        const indexing = status.indexing_queue || 0;
        const embedding = status.embedding_queue || 0;
        const faces = status.face_queue || 0;
        const nima = status.nima_queue || 0;

        // Phase 4 statuses (only present when active)
        const duplicates = status.duplicates;
        const faceGrouping = status.face_grouping;
        const faceReassess = status.face_reassess;
        const faceEmbeddings = status.face_embeddings;

        // Update indicator class
        this._els.statusIndicator.className = 'status-indicator ' + (isUpdating ? 'updating' : 'up-to-date');

        // Update status text
        this._els.statusText.textContent = isUpdating ? 'Updating' : 'Up to date';

        // Update counts (always, so they update during processing)
        if (typeof status.total_images === 'number') {
            this._els.statusTotal.textContent = String(status.total_images);
        }
        if (typeof status.total_people === 'number') {
            this._els.statusPeople.textContent = String(status.total_people);
        }
        if (typeof status.total_faces === 'number') {
            this._els.statusFaces.textContent = String(status.total_faces);
        }
        if (typeof status.trashed_count === 'number') {
            this._els.statusTrashed.textContent = String(status.trashed_count);
            this._updateTrashedLink(status.trashed_count);
        }

        // Determine if any processing is active
        const trashQueue = status.trash_queue || 0;
        const importQueue = status.import_queue || 0;
        const importActive = importQueue > 0 || status.import_progress != null;
        const hasQueueWork = indexing > 0 || embedding > 0 || faces > 0 || nima > 0 || trashQueue > 0 || importActive;
        const hasPhase4Work = duplicates || faceGrouping || faceEmbeddings;
        const hasAnyWork = hasQueueWork || hasPhase4Work;

        if (isUpdating && hasAnyWork) {
            this._els.queueCounts.hidden = false;

            // Show/hide indexing row (always visible structure, but show count only when active)
            if (indexing > 0) {
                this._els.indexingCount.textContent = indexing;
                this._els.indexingCount.parentElement.hidden = false;
                this._updateIndexingEta(indexing);
            } else {
                this._els.indexingCount.parentElement.hidden = true;
                this._indexingHistory = [];
                this._els.indexingEta.textContent = '';
            }

            // Show/hide embedding row
            if (embedding > 0) {
                this._els.embeddingCount.textContent = embedding;
                this._els.embeddingCount.parentElement.hidden = false;
                this._updateEmbeddingEta(embedding);
            } else {
                this._els.embeddingCount.parentElement.hidden = true;
                this._embeddingHistory = [];
                this._els.embeddingEta.textContent = '';
            }

            // Show/hide face detection row
            if (this._els.faceQueueRow && this._els.faceCount) {
                if (faces > 0) {
                    this._els.faceQueueRow.hidden = false;
                    this._els.faceCount.textContent = faces;
                    this._updateFaceEta(faces);
                } else {
                    this._els.faceQueueRow.hidden = true;
                    this._faceHistory = [];
                    if (this._els.faceEta) this._els.faceEta.textContent = '';
                }
            }

            // Show/hide NIMA aesthetic scoring row
            if (this._els.nimaQueueRow && this._els.nimaCount) {
                if (nima > 0) {
                    this._els.nimaQueueRow.hidden = false;
                    this._els.nimaCount.textContent = nima;
                    this._updateNimaEta(nima);
                } else {
                    this._els.nimaQueueRow.hidden = true;
                    this._nimaHistory = [];
                    if (this._els.nimaEta) this._els.nimaEta.textContent = '';
                }
            }

            // Show/hide trash queue row
            if (this._els.trashQueueRow && this._els.trashQueueCount) {
                if (trashQueue > 0) {
                    this._els.trashQueueRow.hidden = false;
                    this._els.trashQueueCount.textContent = trashQueue;
                } else {
                    this._els.trashQueueRow.hidden = true;
                }
            }

            // Show/hide import queue row.  The ImportWorker dequeues items
            // before copying, so importQueue (qsize) can be 0 while files
            // are still being processed.  Use import_progress to track
            // actual completion.
            if (this._els.importQueueRow && this._els.importQueueCount) {
                const ip = status.import_progress;
                if (ip) {
                    const remaining = (ip.total || 0) - (ip.done || 0);
                    this._els.importQueueRow.hidden = false;
                    this._els.importQueueCount.textContent = remaining;
                    if (this._els.importSkippedCount) {
                        this._els.importSkippedCount.textContent = ip.skipped || 0;
                    }
                } else {
                    this._els.importQueueRow.hidden = true;
                }
            }

            // Show/hide duplicate detection row
            if (this._els.duplicatesRow) {
                if (duplicates) {
                    this._els.duplicatesRow.hidden = false;
                    const levelNames = ['identical', 'near-identical', 'similar', 'related'];
                    const levelName = levelNames[duplicates.level] || `level ${duplicates.level}`;
                    this._els.duplicatesStatus.textContent = levelName;
                } else {
                    this._els.duplicatesRow.hidden = true;
                }
            }

            // Show/hide face grouping row
            if (this._els.faceGroupingRow) {
                if (faceGrouping) {
                    this._els.faceGroupingRow.hidden = false;
                    this._els.faceGroupingStatus.textContent = 'computing';
                } else {
                    this._els.faceGroupingRow.hidden = true;
                }
            }

            // Show/hide face reassessment row
            if (this._els.faceReassessRow) {
                if (faceReassess) {
                    this._els.faceReassessRow.hidden = false;
                    this._els.faceReassessStatus.textContent = 'computing';
                } else {
                    this._els.faceReassessRow.hidden = true;
                }
            }

            // Show/hide face embeddings row
            if (this._els.faceEmbeddingsRow) {
                if (faceEmbeddings) {
                    this._els.faceEmbeddingsRow.hidden = false;
                    const current = faceEmbeddings.current || 0;
                    const total = faceEmbeddings.total || 0;
                    this._els.faceEmbeddingsStatus.textContent = `${current}/${total}`;
                } else {
                    this._els.faceEmbeddingsRow.hidden = true;
                }
            }
        } else {
            this._els.queueCounts.hidden = true;
            // Clear history when not updating
            this._indexingHistory = [];
            this._embeddingHistory = [];
            this._faceHistory = [];
            this._nimaHistory = [];
            this._els.indexingEta.textContent = '';
            this._els.embeddingEta.textContent = '';
            if (this._els.faceEta) this._els.faceEta.textContent = '';
            if (this._els.nimaEta) this._els.nimaEta.textContent = '';
            // Hide all rows
            this._els.indexingCount.parentElement.hidden = false; // Reset to default structure
            this._els.embeddingCount.parentElement.hidden = false;
            if (this._els.faceQueueRow) this._els.faceQueueRow.hidden = true;
            if (this._els.nimaQueueRow) this._els.nimaQueueRow.hidden = true;
            if (this._els.trashQueueRow) this._els.trashQueueRow.hidden = true;
            if (this._els.importQueueRow) this._els.importQueueRow.hidden = true;
            if (this._els.duplicatesRow) this._els.duplicatesRow.hidden = true;
            if (this._els.faceGroupingRow) this._els.faceGroupingRow.hidden = true;
            if (this._els.faceReassessRow) this._els.faceReassessRow.hidden = true;
            if (this._els.faceEmbeddingsRow) this._els.faceEmbeddingsRow.hidden = true;
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
    },

    /**
     * Updates the face detection ETA based on processing rate.
     * @param {number} currentCount - Current face detection queue size
     * @private
     */
    _updateFaceEta(currentCount) {
        if (!this._els.faceEta) return;

        const now = Date.now();

        // Add current sample to history
        this._faceHistory.push({ count: currentCount, timestamp: now });

        // Keep only recent samples
        if (this._faceHistory.length > this._maxHistorySamples) {
            this._faceHistory.shift();
        }

        // Need at least 2 samples to calculate rate
        if (this._faceHistory.length < 2) {
            this._els.faceEta.textContent = '';
            return;
        }

        // Calculate processing rate from oldest to newest sample
        const oldest = this._faceHistory[0];
        const newest = this._faceHistory[this._faceHistory.length - 1];
        const countDiff = oldest.count - newest.count;
        const timeDiff = (newest.timestamp - oldest.timestamp) / 1000; // seconds

        // If no progress or queue growing, can't estimate
        if (countDiff <= 0 || timeDiff <= 0) {
            this._els.faceEta.textContent = '';
            return;
        }

        // Calculate rate (images per second) and ETA
        const rate = countDiff / timeDiff;
        const etaSeconds = currentCount / rate;

        // Format ETA
        this._els.faceEta.textContent = ' (' + this._formatEta(etaSeconds) + ')';
    },

    /**
     * Updates the NIMA aesthetic scoring ETA based on processing rate.
     * @param {number} currentCount - Current NIMA queue size
     * @private
     */
    _updateNimaEta(currentCount) {
        if (!this._els.nimaEta) return;

        const now = Date.now();

        // Add current sample to history
        this._nimaHistory.push({ count: currentCount, timestamp: now });

        // Keep only recent samples
        if (this._nimaHistory.length > this._maxHistorySamples) {
            this._nimaHistory.shift();
        }

        // Need at least 2 samples to calculate rate
        if (this._nimaHistory.length < 2) {
            this._els.nimaEta.textContent = '';
            return;
        }

        // Calculate processing rate from oldest to newest sample
        const oldest = this._nimaHistory[0];
        const newest = this._nimaHistory[this._nimaHistory.length - 1];
        const countDiff = oldest.count - newest.count;
        const timeDiff = (newest.timestamp - oldest.timestamp) / 1000; // seconds

        // If no progress or queue growing, can't estimate
        if (countDiff <= 0 || timeDiff <= 0) {
            this._els.nimaEta.textContent = '';
            return;
        }

        // Calculate rate (images per second) and ETA
        const rate = countDiff / timeDiff;
        const etaSeconds = currentCount / rate;

        // Format ETA
        this._els.nimaEta.textContent = ' (' + this._formatEta(etaSeconds) + ')';
    },

    /* ----------------------------------------------------------------------
       Import / Catalogue
       ---------------------------------------------------------------------- */

    /**
     * Fetches the catalogue_dir from /api/config and shows/hides the
     * import section accordingly. Called once during init().
     * @private
     */
    async _loadCatalogueDir() {
        try {
            const response = await App.apiGet('/config');
            this._catalogueDir = response?.data?.catalogue_dir || '';
            if (this._catalogueDir && this._els.importSection) {
                this._els.importSection.hidden = false;
            }

            // Set the file picker's accept filter from the backend's
            // image_extensions list so RAW formats are included too.
            const exts = response?.data?.image_extensions;
            if (exts?.length && this._els.importFileInput) {
                // Combine image/* (for standard types) with explicit extensions
                // (for RAW formats that browsers don't recognise as images)
                this._els.importFileInput.accept =
                    ['image/*', ...exts].join(',');
            }
        } catch {
            // Config load failures are non-fatal — import section stays hidden
        }
    },

    /**
     * Handles files/folders dropped onto the import drop zone.
     * For file drops: uploads via preflight dedup.
     * For folder drops (desktop DataTransferItem entries): shows choice dialog.
     * @param {DataTransfer} dataTransfer - The drop event's dataTransfer
     * @private
     */
    async _handleDrop(dataTransfer) {
        const files = dataTransfer.files;
        if (!files || files.length === 0) return;

        // Check if any of the dropped items are directories via webkitGetAsEntry
        let hasDirectories = false;
        if (dataTransfer.items) {
            for (const item of dataTransfer.items) {
                const entry = item.webkitGetAsEntry?.();
                if (entry && entry.isDirectory) {
                    hasDirectories = true;
                    break;
                }
            }
        }

        if (hasDirectories) {
            // Cannot get real filesystem paths from the browser drop API, so
            // we upload the files just like "Pick Photos" would.
            // Collect all files from the DataTransfer (browser flattens dirs).
            this._uploadFiles(files);
        } else {
            // Pure file drop — upload directly
            this._uploadFiles(files);
        }
    },

    /**
     * Shows the Add Folder vs Import choice dialog.
     * @param {string} path - The folder path selected by the user
     * @private
     */
    _showAddChoice(path) {
        const dialog = App.$('dialog-add-choice');
        if (!dialog) return;

        const refBtn = App.$('add-choice-reference');
        const importBtn = App.$('add-choice-import');
        const cancelBtn = App.$('add-choice-cancel');

        // Clean up old listeners by cloning buttons
        const newRefBtn = refBtn.cloneNode(true);
        const newImportBtn = importBtn.cloneNode(true);
        const newCancelBtn = cancelBtn.cloneNode(true);
        refBtn.replaceWith(newRefBtn);
        importBtn.replaceWith(newImportBtn);
        cancelBtn.replaceWith(newCancelBtn);

        newRefBtn.addEventListener('click', async () => {
            dialog.close();
            try {
                await AppState.folders.add(path);
            } catch (error) {
                console.error('Error adding folder:', error);
                App.showError('Could not add folder.');
            }
        });

        newImportBtn.addEventListener('click', async () => {
            dialog.close();
            await this._importFromPath(path);
        });

        newCancelBtn.addEventListener('click', () => {
            dialog.close();
        });

        dialog.showModal();
    },

    /**
     * Imports images from a local filesystem path (desktop only).
     * Sends the path to the backend which handles the actual file copying.
     * @param {string} path - Path to file or directory on disk
     * @private
     */
    async _importFromPath(path) {
        try {
            const response = await App.apiPost('/import', { paths: [path] });
            const queued = response?.data?.queued || 0;
            if (queued > 0) {
                App.showInfo(`Import started: ${queued} image${queued !== 1 ? 's' : ''} queued.`);
            } else {
                App.showInfo('No new images found to import.');
            }
        } catch (error) {
            console.error('Error starting import:', error);
            App.showError('Could not start import.');
        }
    },

    /**
     * Uploads files via the preflight dedup + multipart upload flow.
     *
     * 1. Compute SHA-256 checksums for all selected files (client-side)
     * 2. Send checksums to /api/import/preflight to find which are new
     * 3. Upload only the new files via /api/import/upload
     *
     * Uses crypto.subtle.digest() which is hardware-accelerated (~500MB/s).
     *
     * @param {FileList} fileList - Files from input element or drop event
     * @private
     */
    async _uploadFiles(fileList) {
        const files = [...fileList];
        if (files.length === 0) return;

        // Filter to image files only (by MIME type or extension)
        const imageFiles = files.filter(f => {
            if (f.type && f.type.startsWith('image/')) return true;
            // Fallback: check extension for camera RAW files that lack MIME types
            const ext = '.' + f.name.split('.').pop().toLowerCase();
            return ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
                '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.srf', '.dng', '.raf',
                '.rw2', '.orf', '.pef', '.srw', '.x3f', '.3fr', '.iiq',
                '.rwl', '.kdc', '.dcr', '.erf'].includes(ext);
        });

        if (imageFiles.length === 0) {
            App.showInfo('No image files found in selection.');
            return;
        }

        try {
            // Preflight dedup: send file name+size pairs to the backend which
            // checks them against import_name+size (catches previously imported
            // files even if renamed on disk due to collision) and basename+size
            // (catches files already in watched folders).  This is instant (no
            // file reading, no crypto), works on plain HTTP.  The backend's
            // SHA-256 dedup in ImportWorker catches any remaining edge cases.
            this._setImportStatus(`Checking ${imageFiles.length} file${imageFiles.length !== 1 ? 's' : ''} for duplicates...`);

            const fileMeta = imageFiles.map(f => ({ name: f.name, size: f.size }));
            const preflight = await App.apiPost('/import/preflight', { files: fileMeta });
            const known = preflight?.data?.known || [];
            const newFiles = imageFiles.filter((_, i) => !known[i]);
            const skippedCount = imageFiles.length - newFiles.length;

            if (newFiles.length === 0) {
                this._setImportStatus(null);
                const n = imageFiles.length;
                App.showInfo(`${n} image${n !== 1 ? 's' : ''} checked, all already in your library.`);
                return;
            }

            const total = imageFiles.length;
            this._setImportStatus(
                `Uploading ${newFiles.length} of ${total} image${total !== 1 ? 's' : ''}...`,
            );

            // Upload files via multipart with a generous timeout so the UI
            // doesn't hang indefinitely if the server response is lost.
            const formData = new FormData();
            for (const file of newFiles) {
                formData.append('files', file);
            }
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 120_000);
            try {
                const uploadResponse = await fetch('/api/import/upload', {
                    method: 'POST',
                    body: formData,
                    signal: controller.signal,
                });
                clearTimeout(timeoutId);
                if (!uploadResponse.ok) {
                    throw new Error(`Upload failed: ${uploadResponse.status}`);
                }
            } catch (uploadErr) {
                clearTimeout(timeoutId);
                if (uploadErr.name === 'AbortError') {
                    // Timeout — the server likely received the files but the
                    // response was lost.  Show a non-fatal message instead of
                    // an error since the import may still proceed server-side.
                    this._setImportStatus(null);
                    App.showInfo('Upload timed out, but the server may still be processing your images.');
                    return;
                }
                throw uploadErr;
            }

            this._setImportStatus(null);
            const parts = [`${total} checked`];
            parts.push(`${newFiles.length} importing`);
            if (skippedCount > 0) parts.push(`${skippedCount} already present`);
            App.showInfo(parts.join(', ') + '.');

        } catch (error) {
            this._setImportStatus(null);
            console.error('Error uploading files:', error);
            App.showError('Could not upload files for import.');
        }
    },

    /**
     * Updates or hides the import status text below the drop zone.
     * @param {string|null} message - Status message, or null to hide
     * @private
     */
    _setImportStatus(message) {
        const el = this._els.importStatusText;
        if (!el) return;
        if (message) {
            el.textContent = message;
            el.hidden = false;
        } else {
            el.hidden = true;
            el.textContent = '';
        }
    },
};

App.registerModule('database', Database);
