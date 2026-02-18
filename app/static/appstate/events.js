/**
 * AppState Events Domain - Backend Event Polling
 * ===============================================
 *
 * Polls backend for events and dispatches them to relevant domains.
 * This enables the frontend to react to backend-initiated changes:
 * - New images added during folder scanning
 * - Face detection/reassessment completion
 * - Folder changes
 * - Processing completion
 *
 * Backend-initiated event types:
 * - `faces_reassessed` - Async face matching completed
 * - `folder_added` / `folder_removed` - Folder registration changes
 * - `processing_complete` - Full processing cycle finished
 * - `images_indexed` - Images ingested and embedded (before face detection/grouping)
 * - `image_ingested` - New image added to database
 * - `images_modified` - Images rotated/rescanned
 * - `nima_complete` - NIMA aesthetic scoring finished
 * - `error` - Backend error occurred
 *
 * Multi-client mutation event types:
 * - `faces_changed` - Face assignments, suppression, lock changes
 * - `people_changed` - People created, renamed, deleted, merged
 * - `images_changed` - Images rated, described, trashed
 * - `groups_changed` - Custom groups created, renamed, deleted, modified
 *
 * @fileoverview Backend event polling and dispatch.
 */

'use strict';

AppState.events = (function() {
    const { createSubscriberSystem } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {number|null} Polling interval timer */
    let _pollTimer = null;

    /** @type {boolean} Whether a poll is in progress */
    let _polling = false;

    /** @type {number} Poll interval in ms */
    let _intervalMs = 2000;

    /** @type {number} Cursor for multi-client polling — server_time from last response */
    let _lastServerTime = 0;

    // =========================================================================
    // EVENT HANDLERS
    // =========================================================================

    /**
     * Process a single event from the backend.
     * @param {Object} event - Event object with type and data
     */
    async function processEvent(event) {
        const { type, data } = event;
        console.log('[AppState.events.processEvent]', type, data);

        switch (type) {
            case 'faces_reassessed':
                // Backend completed async face matching
                // data: { person_id, matched_count, matched_face_ids }
                handleFacesReassessed(data);
                break;

            case 'processing_complete':
                // Backend finished processing all queues
                handleProcessingComplete(data);
                break;

            case 'folder_added':
                // New folder registered
                // data: { folder }
                handleFolderAdded(data);
                break;

            case 'folder_removed':
                // Folder unregistered
                // data: { folder }
                handleFolderRemoved(data);
                break;

            case 'image_ingested':
                // New image added to database
                // data: { id, path }
                handleImageIngested(data);
                break;

            case 'error':
                // Backend error
                // data: { message }
                handleError(data);
                break;

            case 'images_modified':
                // Images modified (rotation, rescan, etc.)
                // data: { image_ids: [...] }
                await handleImagesModified(data);
                break;

            case 'nima_complete':
                // NIMA aesthetic scoring finished — reload images so quality
                // sort picks up the new aesthetic_nima values
                // data: { scored_count }
                console.log('[AppState.events] NIMA scoring complete, reloading images');
                AppState.images.load();
                break;

            case 'images_indexed':
                // New images have been ingested and embedded — ready for display.
                // This fires well before processing_complete (which waits for
                // face detection, duplicate grouping, etc.) so the Gallery shows
                // newly imported images within seconds, not minutes.
                AppState.images.reload();
                if (AppState.folders?.load) {
                    AppState.folders.load();
                }
                if (AppState.folders?.loadStats) {
                    AppState.folders.loadStats();
                }
                break;

            case 'import_complete': {
                // Image import batch finished — files copied into catalogue dir.
                // A folder rescan is triggered server-side; processing_complete
                // will follow once the ingestion pipeline finishes.
                // data: { imported, skipped, catalogue_dir }
                console.log('[AppState.events] Import complete:', data);
                const imported = data?.imported || 0;
                const skipped = data?.skipped || 0;

                // Optimistically bump the catalogue folder's cached count so
                // the Database screen updates immediately.  The real count is
                // reconciled when processing_complete triggers folders.load().
                if (imported > 0 && data?.catalogue_dir) {
                    AppState.folders.adjustCount(data.catalogue_dir, imported);
                }

                const parts = [];
                if (imported > 0) parts.push(`${imported} new image${imported !== 1 ? 's' : ''} imported`);
                if (skipped > 0) parts.push(`${skipped} already present`);
                if (parts.length > 0) App.showInfo(`Import complete: ${parts.join(', ')}.`);
                break;
            }

            // -----------------------------------------------------------------
            // Multi-client mutation events
            // -----------------------------------------------------------------

            case 'faces_changed':
                // data: { updated?: [{id, person_id?, ...}], removed?: [id] }
                handleFacesChanged(data);
                break;

            case 'people_changed':
                // data: { upserted?: [{id, name, ...}], removed?: [id] }
                handlePeopleChanged(data);
                break;

            case 'images_changed':
                // data: { updated_ids?: [id], removed_ids?: [id] }
                await handleImagesChanged(data);
                break;

            case 'groups_changed':
                // data: { level, invalidate: true }
                await handleGroupsChanged(data);
                break;

            default:
                console.warn('[AppState.events.processEvent] Unknown event type:', type);
        }

        // Broadcast raw event for any listeners that want all events
        broadcast({ type: 'event', eventType: type, data });
    }

    /**
     * Reconcile people whose faces were updated in a bulk event.
     *
     * Collects person IDs from the event payload and calls
     * reconcilePerson() on each, updating face counts and preferred
     * faces incrementally.  This avoids the destructive
     * people.invalidate() which would wipe optimistic cache state.
     *
     * @param {Set<string>} personIds - Person IDs to reconcile
     */
    function _reconcileAffectedPeople(personIds) {
        if (!personIds?.size) return;
        if (AppState.people?._internal?.reconcileAll) {
            AppState.people._internal.reconcileAll(personIds);
        }
    }

    /**
     * Handle faces_reassessed event.
     * Auto-matched faces should be added to the person.
     *
     * Two event formats:
     * - Async (single person): { person_id, updated_faces: [{face_id, ...}] }
     * - Sync (multi person):   { updated_faces: [{face_id, person_id, person_name}, ...] }
     *
     * Async reassessment targets one person at a time so includes a top-level
     * person_id.  Sync reassessment (during scan) matches all unknowns at once
     * so each entry in updated_faces carries its own person_id.
     *
     * Uses incremental cache update (no full reload) for responsiveness.
     * The backend already persisted the matches.
     */
    function handleFacesReassessed(data) {
        const { person_id, updated_faces } = data || {};
        if (!updated_faces?.length) return;

        if (person_id) {
            // Async path: all faces matched to the same person.
            // autoAssign already reconciles people — no invalidation needed.
            const faceIds = updated_faces.map(f => f.face_id);
            console.log('[AppState.events] Faces reassessed:',
                faceIds.length, 'faces matched to', person_id);
            if (AppState.faces?.autoAssign) {
                AppState.faces.autoAssign(faceIds, person_id);
            }
        } else {
            // Sync path: faces matched to multiple people — use autoUpdate
            // which accepts per-face person_id assignments, then reconcile
            // affected people incrementally.
            console.log('[AppState.events] Faces reassessed (bulk):',
                updated_faces.length, 'faces matched');
            if (AppState.faces?.autoUpdate) {
                const updates = updated_faces.map(f => ({
                    id: f.face_id,
                    person_id: f.person_id,
                    person_name: f.person_name,
                }));
                AppState.faces.autoUpdate(updates);
            }
            const personIds = new Set();
            for (const f of updated_faces) {
                if (f.person_id) personIds.add(f.person_id);
            }
            _reconcileAffectedPeople(personIds);
        }
    }

    /**
     * Handle processing_complete event.
     * Backend finished processing all queues (indexing, embedding, face detection).
     *
     * Unlike the aggressive approach of reloading all data, we only:
     * 1. Update status (shows "up to date" instead of "updating")
     * 2. Reload folder stats (total counts changed)
     *
     * Full data reloads happen when user navigates to screens, avoiding
     * disruptive background reloads while user is actively working.
     */
    function handleProcessingComplete(data) {
        console.log('[AppState.events] Processing complete');

        // Update status (will show "up to date")
        if (AppState.status?.load) {
            AppState.status.load();
        }

        // Reload folders (per-folder counts) and global stats.
        // This reconciles any optimistic count adjustments (e.g. from
        // import_complete) with the real DB counts now that indexing
        // has finished.
        if (AppState.folders?.load) {
            AppState.folders.load();
        }
        if (AppState.folders?.loadStats) {
            AppState.folders.loadStats();
        }

        // Reload images so the Gallery picks up newly indexed photos.
        // Without this, Gallery shows empty after first-run indexing because
        // the cache was populated (empty) before any images were indexed.
        // The broadcast from load() sets Gallery.needsRefresh if it's not
        // the active screen, ensuring onEnter fetches fresh data.
        if (AppState.images?.reload) {
            AppState.images.reload();
        }

        // Invalidate duplicate group caches (levels 0-4) — recomputed during processing.
        // Level 5 (custom groups) is unaffected by processing.
        if (AppState.duplicates?.invalidate) {
            for (let level = 0; level <= 4; level++) {
                AppState.duplicates.invalidate(level);
            }
        }

        // Notify folders domain that processing is complete
        // This triggers databaseChanged broadcast for screens that care
        if (AppState.folders?.setStatus) {
            AppState.folders.setStatus({ status: 'up_to_date' });
        }

        broadcast({ type: 'processingComplete' });
    }

    /**
     * Handle folder_added event.
     */
    function handleFolderAdded(data) {
        console.log('[AppState.events] Folder added:', data?.folder);

        // Reload folders list
        if (AppState.folders?.load) {
            AppState.folders.load();
        }

        broadcast({ type: 'folderAdded', folder: data?.folder });
    }

    /**
     * Handle folder_removed event.
     */
    function handleFolderRemoved(data) {
        console.log('[AppState.events] Folder removed:', data?.folder);

        // Reload folders list
        if (AppState.folders?.load) {
            AppState.folders.load();
        }

        // Images from this folder were removed - invalidate cache
        // so next access triggers a reload (avoids disruptive background reload)
        if (AppState.images?.invalidate) {
            AppState.images.invalidate();
        }

        // Invalidate directory groups cache (level 4) — folder removal triggers re-sync
        if (AppState.duplicates?.invalidate) {
            AppState.duplicates.invalidate(4);
        }

        broadcast({ type: 'folderRemoved', folder: data?.folder });
    }

    /**
     * Handle image_ingested event.
     * Individual image added - could batch these for efficiency.
     */
    function handleImageIngested(data) {
        // Don't log every image - could be thousands during scan
        // Just mark images as needing refresh when processing completes
    }

    /**
     * Handle error event.
     */
    function handleError(data) {
        console.error('[AppState.events] Backend error:', data?.message);
        broadcast({ type: 'error', message: data?.message });
    }

    /**
     * Handle images_modified event.
     *
     * Called when backend has modified one or more images (rotation, rescan, etc.)
     * Orchestrates cache invalidation and data refresh across all affected domains.
     *
     * @param {Object} data - Event data
     * @param {string[]} data.image_ids - IDs of modified images
     */
    async function handleImagesModified(data) {
        const imageIds = data?.image_ids;
        if (!imageIds?.length) return;

        console.log('[AppState.events] Images modified:', imageIds.length, 'images');

        // 1. Cache-bust image thumbnails (sync)
        for (const imageId of imageIds) {
            ThumbnailLoader.bustCache(imageId);
        }

        // 2. Cache-bust face and person thumbnails BEFORE refresh
        // (face IDs and person associations don't change during rotation, only bboxes)
        // This ensures when refreshForImages() broadcasts and triggers re-render,
        // the cache-bust timestamps are already set
        FaceThumbnails.bustCacheForImages(imageIds);

        // 3. Refresh image metadata - wait for AppState to update
        await AppState.images.refreshByIds(imageIds);

        // 4. Refresh faces for these images (cascades to people)
        // This broadcasts and triggers re-render with cache-busted URLs
        await AppState.faces.refreshForImages(imageIds);

        // 5. Emit frontend event for Gallery to update rendered items
        // (VirtualGrid caches DOM elements - needs explicit removal for re-fetch)
        App.emit('imagesModified', imageIds);
    }

    // =========================================================================
    // MULTI-CLIENT EVENT HANDLERS
    // =========================================================================

    /**
     * Handle faces_changed event from another client.
     * Updates face cache incrementally and invalidates people (face counts).
     * @param {Object} data - {updated?: [{id, person_id?, ...}], removed?: [id]}
     */
    function handleFacesChanged(data) {
        // Collect affected person IDs BEFORE cache mutations so we can
        // reconcile people whose faces were removed (autoRemove deletes
        // the face objects, losing their person_id).
        const affectedPersonIds = new Set();
        if (data?.updated?.length) {
            for (const upd of data.updated) {
                if (upd.person_id) affectedPersonIds.add(upd.person_id);
                // Also capture the old person_id if the face is being reassigned
                const oldFace = AppState.faces._internal?.get(upd.id);
                if (oldFace?.person_id) affectedPersonIds.add(oldFace.person_id);
            }
        }
        if (data?.removed?.length) {
            for (const fid of data.removed) {
                const face = AppState.faces._internal?.get(fid);
                if (face?.person_id) affectedPersonIds.add(face.person_id);
            }
        }

        // Apply cache mutations
        if (data?.updated?.length) {
            AppState.faces.autoUpdate(data.updated);
        }
        if (data?.removed?.length) {
            AppState.faces.autoRemove(data.removed);
        }

        // Reconcile affected people incrementally instead of invalidating
        // the entire people cache (which would wipe optimistic updates).
        if (affectedPersonIds.size > 0 && AppState.people?._internal?.reconcileAll) {
            AppState.people._internal.reconcileAll(affectedPersonIds);
        }
    }

    /**
     * Handle people_changed event from another client.
     * @param {Object} data - {upserted?: [{id, name, ...}], removed?: [id]}
     */
    function handlePeopleChanged(data) {
        if (data?.upserted?.length) {
            AppState.people.autoUpsert(data.upserted);
        }
        if (data?.removed?.length) {
            AppState.people.autoRemove(data.removed);
        }
    }

    /**
     * Handle images_changed event from another client.
     * Removes trashed images from cache and refreshes updated ones.
     * Also reloads folder stats when images are removed so per-folder
     * counts update on the Database screen.
     * @param {Object} data - {updated_ids?: [id], removed_ids?: [id]}
     */
    async function handleImagesChanged(data) {
        if (data?.removed_ids?.length) {
            AppState.images.autoRemove(data.removed_ids);
            // Per-folder counts changed — refresh so Database screen updates
            AppState.folders.load();
            AppState.folders.loadStats();
        }
        if (data?.updated_ids?.length) {
            await AppState.images.refreshByIds(data.updated_ids);
        }
    }

    /**
     * Handle groups_changed event from another client.
     * Invalidates the duplicate group cache for the affected level and
     * forces a reload so the Duplicates screen picks up dissolved groups
     * (e.g. after another client pruned duplicates).
     * @param {Object} data - {level: number, invalidate: true}
     */
    async function handleGroupsChanged(data) {
        if (data?.level !== undefined && AppState.duplicates?.invalidate) {
            AppState.duplicates.invalidate(data.level);
            // Force reload so subscribers (Duplicates screen) see the update
            await AppState.duplicates.loadLevel(data.level, true);
        }
        broadcast({ type: 'groupsChanged', level: data?.level });
    }

    // =========================================================================
    // POLLING
    // =========================================================================

    /**
     * Poll for events from backend using cursor-based pagination.
     * Each poll sends the server_time from the previous response so only
     * new events are returned. Multiple browser tabs can poll independently
     * without draining events from each other.
     */
    async function poll() {
        if (_polling) return;
        _polling = true;

        // The initial poll (cursor = 0) returns ALL buffered events.
        // These are historical — the page already loads fresh state
        // during initialisation.  Processing them would replay stale
        // toasts and trigger redundant reloads.  We just advance the
        // cursor to "now" so subsequent polls only get new events.
        const isInitialPoll = _lastServerTime === 0;

        try {
            const response = await App.apiGet(`/events?since=${_lastServerTime}`);
            const data = response?.data;

            // Track connectivity for offline detection
            App.markOnline();

            // Update cursor for next poll
            if (data?.server_time) {
                _lastServerTime = data.server_time;
            }

            // Initial poll: just set cursor, skip event processing
            if (isInitialPoll) return;

            // If client has fallen behind the event buffer, do a full reload
            // instead of trying to process individual events
            if (data?.stale) {
                await handleStaleReload();
                return;
            }

            const events = data?.events || [];
            for (const event of events) {
                await processEvent(event);
            }
        } catch (err) {
            console.error('[AppState.events.poll] Error:', err);
        } finally {
            _polling = false;
        }
    }

    /**
     * Handle stale client — event buffer has wrapped and we missed events.
     * Performs a full reload of all AppState domains so the client converges
     * to the correct server state. Tries to be minimally disruptive: stays
     * on the same screen, preserves selection and scroll where possible.
     */
    async function handleStaleReload() {
        console.warn('[AppState.events] Client is stale, reloading all state');

        // Reload all data domains — order matters: images first (faces reference them)
        AppState.images.invalidate();
        await AppState.images.load();

        AppState.folders.load();
        AppState.people.load(true);

        // Only reload faces if they were already loaded (lazy domain)
        if (AppState.faces.isLoaded()) {
            AppState.faces.load(true);
        }

        // Invalidate all duplicate group caches (levels 0-5)
        for (let level = 0; level <= 5; level++) {
            AppState.duplicates.invalidate(level);
        }
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'events',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to events.
         * @param {Function} callback - Called with event
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Start polling for events.
         * Safe to call multiple times - only starts one timer.
         * @param {number} [intervalMs=2000] - Polling interval in ms
         */
        startPolling(intervalMs = 2000) {
            if (_pollTimer) return;

            _intervalMs = intervalMs;
            console.log('[AppState.events.startPolling] interval:', intervalMs);
            poll(); // Initial poll
            _pollTimer = setInterval(poll, intervalMs);
        },

        /**
         * Stop polling for events.
         * Resets the cursor so the next startPolling() begins from scratch.
         */
        stopPolling() {
            if (_pollTimer) {
                console.log('[AppState.events.stopPolling]');
                clearInterval(_pollTimer);
                _pollTimer = null;
            }
            _lastServerTime = 0;
        },

        /**
         * Check if currently polling.
         * @returns {boolean}
         */
        isPolling() {
            return _pollTimer !== null;
        },

        /**
         * Manually trigger a poll (e.g., after user action).
         * @returns {Promise<void>}
         */
        poll,
    };
})();
