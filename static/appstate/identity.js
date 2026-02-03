/**
 * AppState Identity Domain - Faces and People
 * =============================================
 *
 * Manages face detection results and person identities.
 * These domains are tightly coupled:
 * - People are emergent from faces (a person exists because faces are assigned)
 * - Face operations affect person state (counts, preferred face)
 * - Person operations affect face state (assignments, names)
 *
 * Key concepts:
 * - **Locked faces** (manually_tagged=true): Manually identified, used for
 *   similarity search, never auto-removed by threshold changes
 * - **Unlocked faces**: Auto-detected matches, can be auto-removed
 * - **Preferred face**: Representative face for a person's thumbnail
 *
 * @fileoverview Faces and people identity management.
 */

'use strict';

// =============================================================================
// FACES DOMAIN
// =============================================================================

AppState.faces = (function() {
    const { createSubscriberSystem, markDirty, transaction, queueTransaction } = AppState;
    const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * Face cache.
     * @type {Map<string, Object>|null}
     */
    let _cache = null;

    /**
     * Whether the cache is partial (unknown faces only).
     * When true, fetchForImage() must hit the API.
     * @type {boolean}
     */
    let _cacheIsPartial = false;

    /** @type {boolean} */
    let _loading = false;

    /** @type {Promise|null} */
    let _pendingLoad = null;

    // Derived view caches (invalidated on change)
    /** @type {Array|null} */
    let _unknownFaces = null;

    /** @type {Map<string, Array>|null} */
    let _facesByPerson = null;

    /** @type {Map<string, Array>|null} */
    let _facesByImage = null;

    /** Domain reference for transaction system */
    const domainRef = { _name: 'faces', _notify: notify };

    /**
     * Invalidate derived view caches.
     * @private
     */
    function invalidateDerived() {
        _unknownFaces = null;
        _facesByPerson = null;
        _facesByImage = null;
    }

    // =========================================================================
    // CACHE PRIMITIVES (_internal)
    // =========================================================================

    /**
     * Internal API for cache mutations.
     * These are single-item, synchronous operations.
     * They don't persist - callers handle persistence.
     */
    const _internal = {
        /**
         * Get face by ID.
         * @param {string} id - Face ID
         * @returns {Object|null}
         */
        get(id) {
            return _cache?.get(id) || null;
        },

        /**
         * Link a face to a person.
         * Does NOT set locked - caller controls that.
         * @param {string} faceId - Face ID
         * @param {string} personId - Person ID
         * @param {string} personName - Person name (denormalized)
         */
        linkToPerson(faceId, personId, personName) {
            const face = _cache?.get(faceId);
            if (face) {
                console.log('[AppState.faces._internal.linkToPerson]',
                    faceId, '->', personId, `(${personName})`);
                face.person_id = personId;
                face.person_name = personName;
                invalidateDerived();
                markDirty(domainRef);
            }
        },

        /**
         * Unlink a face from its person.
         * Does NOT change locked status.
         * @param {string} faceId - Face ID
         * @returns {string|null} Previous person_id
         */
        unlinkFromPerson(faceId) {
            const face = _cache?.get(faceId);
            if (face && face.person_id) {
                const oldPersonId = face.person_id;
                console.log('[AppState.faces._internal.unlinkFromPerson]',
                    faceId, 'was:', oldPersonId);
                face.person_id = null;
                face.person_name = null;
                invalidateDerived();
                markDirty(domainRef);
                return oldPersonId;
            }
            return null;
        },

        /**
         * Set the locked (manually_tagged) flag.
         * @param {string} faceId - Face ID
         * @param {boolean} locked - New locked state
         */
        setLocked(faceId, locked) {
            const face = _cache?.get(faceId);
            if (face && face.manually_tagged !== locked) {
                console.log('[AppState.faces._internal.setLocked]',
                    faceId, locked ? 'LOCKED' : 'UNLOCKED');
                face.manually_tagged = locked;
                markDirty(domainRef);
            }
        },

        /**
         * Set the suppressed flag.
         * @param {string} faceId - Face ID
         * @param {boolean} suppressed - New suppressed state
         */
        setSuppressed(faceId, suppressed) {
            const face = _cache?.get(faceId);
            if (face) {
                face.suppressed = suppressed;
                invalidateDerived();
                markDirty(domainRef);
            }
        },

        /**
         * Update denormalized person_name (for renames).
         * @param {string} faceId - Face ID
         * @param {string} newName - New person name
         */
        updateName(faceId, newName) {
            const face = _cache?.get(faceId);
            if (face && face.person_id) {
                face.person_name = newName;
                invalidateDerived();
                markDirty(domainRef);
            }
        },

        /**
         * Update arbitrary properties.
         * @param {string} id - Face ID
         * @param {Object} changes - Properties to merge
         */
        update(id, changes) {
            const face = _cache?.get(id);
            if (face) {
                Object.assign(face, changes);
                invalidateDerived();
                markDirty(domainRef);
            }
        },

        /**
         * Remove a face from cache.
         * @param {string} id - Face ID
         */
        remove(id) {
            if (_cache?.delete(id)) {
                invalidateDerived();
                markDirty(domainRef);
            }
        },

        // =====================================================================
        // BATCH HELPERS
        // =====================================================================

        /**
         * Assign faces to a person.
         * @param {string[]} faceIds - Face IDs to assign
         * @param {string} personId - Person to assign to
         * @param {Object} [options]
         * @param {boolean} [options.lock=false] - Lock the faces
         * @returns {Set<string>} Old person IDs that lost faces
         */
        assignToPersonBatch(faceIds, personId, { lock = false } = {}) {
            const person = AppState.people._internal.get(personId);
            if (!person) {
                console.warn('[AppState.faces._internal.assignToPersonBatch]',
                    'Person not found:', personId);
                return new Set();
            }

            const affectedPersonIds = new Set();

            for (const faceId of faceIds) {
                const face = this.get(faceId);
                if (!face || face.suppressed) continue;

                // Track old person
                if (face.person_id && face.person_id !== personId) {
                    affectedPersonIds.add(face.person_id);
                }

                this.linkToPerson(faceId, personId, person.name);
                if (lock) {
                    this.setLocked(faceId, true);
                }
            }

            console.log('[AppState.faces._internal.assignToPersonBatch]',
                faceIds.length, 'faces ->', personId,
                'affected old persons:', Array.from(affectedPersonIds));

            return affectedPersonIds;
        },

        /**
         * Unassign faces from their persons.
         * Also unlocks the faces.
         * @param {string[]} faceIds - Face IDs to unassign
         * @returns {Set<string>} Affected person IDs
         */
        unassignBatch(faceIds) {
            const affectedPersonIds = new Set();

            for (const faceId of faceIds) {
                const face = this.get(faceId);
                if (!face?.person_id) continue;

                affectedPersonIds.add(face.person_id);
                this.unlinkFromPerson(faceId);
                this.setLocked(faceId, false);
            }

            console.log('[AppState.faces._internal.unassignBatch]',
                faceIds.length, 'faces, affected persons:',
                Array.from(affectedPersonIds));

            return affectedPersonIds;
        },

        /**
         * Pick face with newest image_timestamp.
         * @param {string[]} faceIds - Face IDs to choose from
         * @returns {string} Face ID with newest timestamp
         */
        pickNewestFace(faceIds) {
            let newest = null;
            let newestTime = null;

            for (const faceId of faceIds) {
                const face = this.get(faceId);
                const ts = face?.image_timestamp || 0;
                if (newestTime === null || ts > newestTime) {
                    newestTime = ts;
                    newest = faceId;
                }
            }

            return newest || faceIds[0];
        },

        /**
         * Get faces for a person.
         * @param {string} personId - Person ID
         * @returns {Array} Faces for the person
         */
        getForPerson(personId) {
            if (!_cache) return [];

            // Build lookup if needed
            if (_facesByPerson === null) {
                _facesByPerson = new Map();
                for (const face of _cache.values()) {
                    if (face.person_id) {
                        const list = _facesByPerson.get(face.person_id) || [];
                        list.push(face);
                        _facesByPerson.set(face.person_id, list);
                    }
                }
            }

            return _facesByPerson.get(personId) || [];
        },

        /**
         * Get first face for a person.
         * @param {string} personId - Person ID
         * @param {Object} [options]
         * @param {string} [options.excludingImageId] - Image to exclude
         * @returns {Object|null}
         */
        getFirstForPerson(personId, options = {}) {
            const { excludingImageId = null } = options;
            const faces = this.getForPerson(personId);

            if (excludingImageId) {
                return faces.find(f => f.image_id !== excludingImageId) || null;
            }
            return faces[0] || null;
        }
    };

    // =========================================================================
    // PERSIST FUNCTIONS
    // =========================================================================

    /**
     * Persist identify operation.
     * Sequence: create person (if new) → assign faces → lock faces
     * @private
     */
    async function _persistIdentify(faceIds, personId, createdPerson, personName, preferredFaceId) {
        console.log('[AppState.faces._persistIdentify]',
            'personId:', personId, 'created:', createdPerson,
            'faces:', faceIds.length);

        let actualPersonId = personId;

        // Create person if new
        if (createdPerson) {
            try {
                await App.apiPost('/people', {
                    id: personId,
                    name: personName,
                    preferred_face_id: preferredFaceId
                });
            } catch (err) {
                // 409 CONFLICT means person already exists (race condition with another identify)
                // Find existing person and use their ID, then fix up the local cache
                if (err.message?.includes('409')) {
                    console.log('[AppState.faces._persistIdentify] Person exists (409), fixing cache');
                    await AppState.people.load(true);
                    const existing = AppState.people._internal.findByName(personName);
                    if (existing) {
                        actualPersonId = existing.id;
                        console.log('[AppState.faces._persistIdentify] Using existing person:', actualPersonId);

                        // Fix local cache: move faces from wrong person to correct person
                        transaction(() => {
                            // Update face cache to point to correct person
                            for (const faceId of faceIds) {
                                _internal.linkToPerson(faceId, actualPersonId, personName);
                            }
                            // Remove the incorrectly created person from cache
                            AppState.people._internal.remove(personId);
                            // Update face count on correct person
                            AppState.people._internal.reconcilePerson(actualPersonId);
                        });
                    } else {
                        throw err;  // Can't find person, re-throw
                    }
                } else {
                    throw err;
                }
            }
        }

        // Assign faces
        await App.apiPost('/faces/assign', {
            face_ids: faceIds,
            person_id: actualPersonId
        });

        // Lock faces
        await App.apiPatch('/faces', {
            face_ids: faceIds,
            locked: true
        });
    }

    /**
     * Persist unassign operation.
     * @private
     */
    async function _persistUnassign(faceIds) {
        console.log('[AppState.faces._persistUnassign]', faceIds.length, 'faces');
        await App.apiPost('/faces/unassign', { face_ids: faceIds });
    }

    /**
     * Persist suppress operation.
     * @private
     */
    async function _persistSuppress(faceIds) {
        console.log('[AppState.faces._persistSuppress]', faceIds.length, 'faces');
        await App.apiPost('/faces/suppress', { face_ids: faceIds });
    }

    /**
     * Persist lock/unlock operation.
     * @private
     */
    async function _persistSetLocked(faceIds, locked) {
        console.log('[AppState.faces._persistSetLocked]',
            faceIds.length, 'faces, locked:', locked);
        await App.apiPatch('/faces', { face_ids: faceIds, locked });
    }

    // =========================================================================
    // LOAD
    // =========================================================================

    /**
     * Load all faces from backend.
     * @param {boolean} [force=false] - Force reload even if cached
     * @returns {Promise<void>}
     */
    async function load(force = false) {
        if (!force && _cache !== null) return;
        if (_pendingLoad) return _pendingLoad;

        _loading = true;
        console.log('[AppState.faces.load] Starting...');

        _pendingLoad = (async () => {
            try {
                const response = await App.apiGet('/faces');
                _cache = new Map(response.data.map(f => [f.id, f]));
                _cacheIsPartial = false;  // Full cache loaded
                invalidateDerived();

                console.log('[AppState.faces.load] Loaded', _cache.size, 'faces');
                broadcast({ type: 'changed' });

            } catch (err) {
                console.error('[AppState.faces.load] Error:', err);
                broadcastError(err.message || 'Failed to load faces');
                throw err;
            } finally {
                _loading = false;
                _pendingLoad = null;
            }
        })();

        return _pendingLoad;
    }

    /** @type {Promise|null} Pending unknown-only load */
    let _pendingUnknownLoad = null;

    /**
     * Load only unknown faces from backend.
     * This is faster than load() as it skips known faces.
     * Used for initial render of faces screen.
     * @param {boolean} [force=false] - Force reload even if cached
     * @returns {Promise<void>}
     */
    async function loadUnknownOnly(force = false) {
        // If full cache exists, no need to load unknown only
        if (!force && _cache !== null) return;
        if (_pendingUnknownLoad) return _pendingUnknownLoad;
        if (_pendingLoad) return _pendingLoad;

        _loading = true;
        console.log('[AppState.faces.loadUnknownOnly] Starting...');

        _pendingUnknownLoad = (async () => {
            try {
                const response = await App.apiGet('/faces?unknown=true');
                _cache = new Map(response.data.map(f => [f.id, f]));
                _cacheIsPartial = true;  // Only unknown faces loaded
                invalidateDerived();

                console.log('[AppState.faces.loadUnknownOnly] Loaded', _cache.size, 'unknown faces');
                broadcast({ type: 'changed' });

            } catch (err) {
                console.error('[AppState.faces.loadUnknownOnly] Error:', err);
                broadcastError(err.message || 'Failed to load faces');
                throw err;
            } finally {
                _loading = false;
                _pendingUnknownLoad = null;
            }
        })();

        return _pendingUnknownLoad;
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        _name: 'faces',
        _notify: notify,
        _internal,

        onChanged: subscribe,
        onError: subscribeError,

        load,
        loadUnknownOnly,
        reload() { return load(true); },
        isCachePartial() { return _cacheIsPartial; },

        // --- Accessors ---

        /**
         * Get all faces.
         * @returns {Array}
         */
        getAll() {
            return _cache ? Array.from(_cache.values()) : [];
        },

        /**
         * Get face by ID.
         * @param {string} id - Face ID
         * @returns {Object|null}
         */
        getById(id) {
            return _cache?.get(id) || null;
        },

        /**
         * Get face count.
         * @returns {number}
         */
        getCount() {
            return _cache?.size || 0;
        },

        /**
         * Check if faces are loaded.
         * @returns {boolean}
         */
        isLoaded() {
            return _cache !== null;
        },

        /**
         * Check if faces are loading.
         * @returns {boolean}
         */
        isLoading() {
            return _loading;
        },

        /**
         * Get unknown faces (no person_id, not suppressed).
         * @returns {Array}
         */
        getUnknown() {
            if (!_cache) return [];
            if (_unknownFaces === null) {
                _unknownFaces = Array.from(_cache.values())
                    .filter(f => !f.person_id && !f.suppressed);
            }
            return _unknownFaces;
        },

        /**
         * Get faces for a person.
         * @param {string} personId - Person ID
         * @returns {Array}
         */
        getForPerson(personId) {
            return _internal.getForPerson(personId);
        },

        /**
         * Get faces for an image.
         * @param {string} imageId - Image ID
         * @returns {Array}
         */
        getForImage(imageId) {
            if (!_cache) return [];
            if (_facesByImage === null) {
                _facesByImage = new Map();
                for (const face of _cache.values()) {
                    const list = _facesByImage.get(face.image_id) || [];
                    list.push(face);
                    _facesByImage.set(face.image_id, list);
                }
            }
            return _facesByImage.get(imageId) || [];
        },

        /**
         * Fetch faces for an image from backend.
         * Uses cache if fully loaded, otherwise fetches from API.
         * ALWAYS adds fetched faces to cache to prevent mutation bugs.
         * @param {string} imageId - Image ID
         * @param {Object} [options]
         * @param {boolean} [options.fresh=false] - Bypass cache and fetch from API
         * @returns {Promise<Array>}
         */
        async fetchForImage(imageId, { fresh = false } = {}) {
            // If cache is complete and fresh not requested, use cache
            if (!fresh && _cache && !_cacheIsPartial) return this.getForImage(imageId);

            const faces = (await App.apiGet(`/images/${imageId}/faces`)).data;

            // ALWAYS add fetched faces to cache - this prevents bugs where
            // subsequent mutations fail because faces aren't in cache
            if (faces?.length) {
                if (!_cache) _cache = new Map();
                for (const face of faces) {
                    _cache.set(face.id, face);
                }
                _cacheIsPartial = true;
                invalidateDerived();
            }

            return faces;
        },

        /**
         * Fetch faces for a person from backend.
         * Also updates the cache with fetched faces (for setPreferredFace validation).
         * @param {string} personId - Person ID
         * @returns {Promise<Array>}
         */
        async fetchForPerson(personId) {
            const faces = (await App.apiGet(`/people/${personId}/faces`)).data;

            // Add fetched faces to cache (ensures setPreferredFace can validate them)
            if (faces?.length) {
                const wasEmpty = !_cache;
                if (!_cache) _cache = new Map();
                for (const face of faces) {
                    _cache.set(face.id, face);
                }
                // Mark cache as partial if we just created it (only has this person's faces)
                if (wasEmpty) {
                    _cacheIsPartial = true;
                }
                invalidateDerived();
            }

            return faces;
        },

        /**
         * Search faces using semantic query.
         * Calls backend search endpoint.
         * @param {string} query - Search query
         * @returns {Promise<Array>} Matching faces
         */
        async search(query) {
            console.log('[AppState.faces.search] query:', query);
            const url = query
                ? `/faces?search=${encodeURIComponent(query)}`
                : '/faces';
            const response = await App.apiGet(url);
            return response.data || [];
        },

        // =====================================================================
        // CACHE HELPERS
        // =====================================================================

        /**
         * Ensure faces are in cache before mutation operations.
         *
         * This is the ROOT FIX for the recurring bug where mutations fail
         * silently because faces aren't in the cache. Call this at the start
         * of identify(), unassign(), suppress(), etc.
         *
         * @param {string[]} faceIds - Face IDs that must be in cache
         * @returns {Promise<void>}
         */
        async ensureFacesInCache(faceIds) {
            if (!faceIds?.length) return;
            if (!_cache) _cache = new Map();

            // Find faces missing from cache
            const missingIds = faceIds.filter(id => !_cache.has(id));
            if (!missingIds.length) return;

            console.log('[AppState.faces.ensureFacesInCache]',
                missingIds.length, 'of', faceIds.length, 'faces missing from cache');

            // Fetch missing faces individually (batch endpoint doesn't exist)
            // Group by image to minimize API calls if we had that info,
            // but we don't, so fetch each face directly
            const fetched = [];
            for (const faceId of missingIds) {
                try {
                    const response = await App.apiGet(`/faces/${faceId}`);
                    if (response.data) {
                        fetched.push(response.data);
                    }
                } catch (err) {
                    console.warn('[AppState.faces.ensureFacesInCache] Failed to fetch face:', faceId, err);
                }
            }

            // Add to cache
            if (fetched.length) {
                for (const face of fetched) {
                    _cache.set(face.id, face);
                }
                _cacheIsPartial = true;
                invalidateDerived();
                console.log('[AppState.faces.ensureFacesInCache] Added', fetched.length, 'faces to cache');
            }
        },

        // =====================================================================
        // PUBLIC MUTATIONS
        // =====================================================================

        /**
         * Identify faces - manual identification.
         *
         * - Locks faces (manually_tagged = true)
         * - Creates person if name is new
         * - Sets preferred face if person has none
         * - Triggers backend similarity search
         *
         * @param {string|string[]} faceIds - Face ID(s) to identify
         * @param {string} personName - Name for the person
         * @param {Object} [options]
         * @param {string} [options.preferredFaceId] - Face to use as preferred
         * @returns {Promise<{personId: string}>}
         */
        async identify(faceIds, personName, options = {}) {
            if (!Array.isArray(faceIds)) faceIds = [faceIds];
            if (!faceIds?.length) return Promise.resolve();

            // Ensure faces are in cache before proceeding
            await this.ensureFacesInCache(faceIds);

            // Empty name = unassign
            const trimmedName = personName?.trim() || '';
            if (!trimmedName) {
                console.log('[AppState.faces.identify] Empty name, delegating to unassign');
                return this.unassign(faceIds);
            }

            // Ensure people cache is loaded for findByName to work
            await AppState.people.load();

            console.log('[AppState.faces.identify]', faceIds.length, 'faces as', trimmedName);

            const { preferredFaceId = null } = options;

            // Backup for rollback
            const backup = {
                faces: new Map(),
                people: new Map(),
                createdPersonId: null
            };

            for (const faceId of faceIds) {
                const face = _cache?.get(faceId);
                if (face) {
                    backup.faces.set(faceId, {
                        person_id: face.person_id,
                        person_name: face.person_name,
                        manually_tagged: face.manually_tagged
                    });
                }
            }

            let personId, createdPerson = false;

            // PHASE 1: Synchronous optimistic updates
            transaction(() => {
                // Find or create person
                let person = AppState.people._internal.findByName(trimmedName);
                if (!person) {
                    personId = crypto.randomUUID();
                    person = {
                        id: personId,
                        name: trimmedName,
                        face_count: 0,
                        preferred_face_id: null,
                        threshold: null
                    };
                    AppState.people._internal.add(person);
                    createdPerson = true;
                    backup.createdPersonId = personId;
                    console.log('[AppState.faces.identify] Created new person:', personId);
                } else {
                    personId = person.id;
                    console.log('[AppState.faces.identify] Found existing person:', personId);
                }

                // Backup person state
                backup.people.set(personId, { ...person });

                // Assign faces (locked)
                const affectedPersonIds = _internal.assignToPersonBatch(
                    faceIds, personId, { lock: true }
                );

                // Backup affected persons
                for (const pid of affectedPersonIds) {
                    const p = AppState.people._internal.get(pid);
                    if (p && !backup.people.has(pid)) {
                        backup.people.set(pid, { ...p });
                    }
                }

                // Set preferred if person has none
                if (!AppState.people._internal.get(personId).preferred_face_id) {
                    const prefId = preferredFaceId || _internal.pickNewestFace(faceIds);
                    AppState.people._internal.setPreferred(personId, prefId);
                }

                // Reconcile
                AppState.people._internal.reconcilePerson(personId);
                AppState.people._internal.reconcileAll(affectedPersonIds);
            });

            // PHASE 2: Async persist
            const finalPersonId = personId;
            const finalPreferredId = preferredFaceId || _internal.pickNewestFace(faceIds);

            return queueTransaction(async () => {
                try {
                    await _persistIdentify(
                        faceIds, finalPersonId, createdPerson,
                        trimmedName, finalPreferredId
                    );
                    console.log('[AppState.faces.identify] Persist complete');
                    return { personId: finalPersonId };

                } catch (err) {
                    console.error('[AppState.faces.identify] Persist failed, rolling back:', err);

                    transaction(() => {
                        // Restore faces
                        for (const [fid, data] of backup.faces) {
                            const face = _cache?.get(fid);
                            if (face) {
                                face.person_id = data.person_id;
                                face.person_name = data.person_name;
                                face.manually_tagged = data.manually_tagged;
                            }
                        }
                        invalidateDerived();
                        markDirty(domainRef);

                        // Remove created person
                        if (backup.createdPersonId) {
                            AppState.people._internal.remove(backup.createdPersonId);
                        }

                        // Restore other people
                        for (const [pid, data] of backup.people) {
                            if (pid !== backup.createdPersonId) {
                                const p = AppState.people._internal.get(pid);
                                if (p) Object.assign(p, data);
                            }
                        }
                    });

                    broadcastError(err.message || 'Failed to identify faces');
                    throw err;
                }
            });
        },

        /**
         * Auto-assign faces - backend-detected matches.
         *
         * Unlike identify():
         * - Does NOT lock faces
         * - Does NOT persist (backend already stored)
         * - Does NOT trigger further searches
         *
         * @param {string[]} faceIds - Face IDs to assign
         * @param {string} personId - Person to assign to
         */
        autoAssign(faceIds, personId) {
            if (!faceIds?.length) return;
            if (!AppState.people._internal.get(personId)) return;

            console.log('[AppState.faces.autoAssign]',
                faceIds.length, 'faces ->', personId, '(no lock, no persist)');

            transaction(() => {
                const affectedPersonIds = _internal.assignToPersonBatch(
                    faceIds, personId, { lock: false }
                );
                AppState.people._internal.reconcilePerson(personId);
                AppState.people._internal.reconcileAll(affectedPersonIds);
            });
        },

        /**
         * Unassign faces - return to unknown pool.
         * Unlocks the faces.
         *
         * @param {string|string[]} faceIds - Face ID(s) to unassign
         * @returns {Promise<void>}
         */
        async unassign(faceIds) {
            if (!Array.isArray(faceIds)) faceIds = [faceIds];
            if (!faceIds?.length) return Promise.resolve();

            // Ensure faces are in cache before proceeding
            await this.ensureFacesInCache(faceIds);

            console.log('[AppState.faces.unassign]', faceIds.length, 'faces');

            // Backup
            const backup = new Map();
            for (const faceId of faceIds) {
                const face = _cache?.get(faceId);
                if (face) {
                    backup.set(faceId, {
                        person_id: face.person_id,
                        person_name: face.person_name,
                        manually_tagged: face.manually_tagged
                    });
                }
            }

            let affectedPersonIds;

            // PHASE 1: Optimistic
            transaction(() => {
                affectedPersonIds = _internal.unassignBatch(faceIds);
                AppState.people._internal.reconcileAll(affectedPersonIds);
            });

            // PHASE 2: Persist
            return queueTransaction(async () => {
                try {
                    await _persistUnassign(faceIds);
                } catch (err) {
                    console.error('[AppState.faces.unassign] Persist failed, rolling back:', err);

                    transaction(() => {
                        for (const [fid, data] of backup) {
                            if (data.person_id) {
                                _internal.linkToPerson(fid, data.person_id, data.person_name);
                                _internal.setLocked(fid, data.manually_tagged);
                            }
                        }
                        for (const pid of affectedPersonIds) {
                            AppState.people._internal.reconcilePerson(pid);
                        }
                    });

                    broadcastError(err.message || 'Failed to unassign faces');
                    throw err;
                }
            });
        },

        /**
         * Suppress faces - mark as false positives.
         * Unassigns from person first if needed.
         *
         * @param {string|string[]} faceIds - Face ID(s) to suppress
         * @returns {Promise<void>}
         */
        async suppress(faceIds) {
            if (!Array.isArray(faceIds)) faceIds = [faceIds];
            if (!faceIds?.length) return Promise.resolve();

            // Ensure faces are in cache before proceeding
            await this.ensureFacesInCache(faceIds);

            console.log('[AppState.faces.suppress]', faceIds.length, 'faces');

            // Backup
            const backup = new Map();
            for (const faceId of faceIds) {
                const face = _cache?.get(faceId);
                if (face) {
                    backup.set(faceId, {
                        suppressed: face.suppressed,
                        person_id: face.person_id,
                        person_name: face.person_name,
                        manually_tagged: face.manually_tagged
                    });
                }
            }

            let affectedPersonIds;

            // PHASE 1: Optimistic
            transaction(() => {
                affectedPersonIds = _internal.unassignBatch(faceIds);
                for (const faceId of faceIds) {
                    _internal.setSuppressed(faceId, true);
                }
                AppState.people._internal.reconcileAll(affectedPersonIds);
            });

            // PHASE 2: Persist
            return queueTransaction(async () => {
                try {
                    await _persistSuppress(faceIds);
                } catch (err) {
                    console.error('[AppState.faces.suppress] Persist failed, rolling back:', err);

                    transaction(() => {
                        for (const [fid, data] of backup) {
                            const face = _cache?.get(fid);
                            if (face) {
                                face.suppressed = data.suppressed;
                                if (data.person_id) {
                                    face.person_id = data.person_id;
                                    face.person_name = data.person_name;
                                    face.manually_tagged = data.manually_tagged;
                                }
                            }
                        }
                        invalidateDerived();
                        markDirty(domainRef);

                        for (const pid of affectedPersonIds) {
                            AppState.people._internal.reconcilePerson(pid);
                        }
                    });

                    broadcastError(err.message || 'Failed to suppress faces');
                    throw err;
                }
            });
        },

        /**
         * Set locked status for faces.
         *
         * Locked faces (manually_tagged):
         * - Used as reference for similarity searches
         * - Never auto-removed by threshold changes
         *
         * Cannot unlock preferred faces.
         *
         * @param {string|string[]} faceIds - Face ID(s)
         * @param {boolean} locked - New locked state
         * @returns {Promise<void>}
         */
        async setLocked(faceIds, locked) {
            if (!Array.isArray(faceIds)) faceIds = [faceIds];
            if (!faceIds?.length) return Promise.resolve();

            // Ensure faces are in cache before proceeding
            await this.ensureFacesInCache(faceIds);

            console.log('[AppState.faces.setLocked]', faceIds.length, 'faces, locked:', locked);

            // INVARIANT: Cannot unlock preferred faces
            if (!locked) {
                for (const faceId of faceIds) {
                    const face = _internal.get(faceId);
                    if (face?.person_id) {
                        const person = AppState.people._internal.get(face.person_id);
                        if (person?.preferred_face_id === faceId) {
                            const msg = 'Cannot unlock the preferred face';
                            console.error('[AppState.faces.setLocked]', msg);
                            throw new Error(msg);
                        }
                    }
                }
            }

            // Backup
            const backup = new Map();
            for (const faceId of faceIds) {
                const face = _cache?.get(faceId);
                if (face) {
                    backup.set(faceId, face.manually_tagged);
                }
            }

            // PHASE 1: Optimistic
            transaction(() => {
                for (const faceId of faceIds) {
                    _internal.setLocked(faceId, locked);
                }
            });

            // PHASE 2: Persist
            return queueTransaction(async () => {
                try {
                    await _persistSetLocked(faceIds, locked);
                } catch (err) {
                    console.error('[AppState.faces.setLocked] Persist failed, rolling back:', err);

                    transaction(() => {
                        for (const [fid, wasLocked] of backup) {
                            _internal.setLocked(fid, wasLocked);
                        }
                    });

                    broadcastError(err.message || 'Failed to update lock status');
                    throw err;
                }
            });
        },

        /**
         * Apply threshold changes from backend.
         *
         * Called when backend re-evaluates faces after threshold change.
         * Assigned faces are unlocked (auto-detected).
         * Locked faces in unassigned list are filtered out (defensive).
         *
         * @param {string} personId - Person whose threshold changed
         * @param {string[]} assignedFaceIds - Faces to assign
         * @param {string[]} unassignedFaceIds - Faces to unassign
         */
        applyThresholdChanges(personId, assignedFaceIds, unassignedFaceIds) {
            if (!AppState.people._internal.get(personId)) return;

            console.log('[AppState.faces.applyThresholdChanges]',
                'person:', personId,
                'assign:', assignedFaceIds?.length || 0,
                'unassign:', unassignedFaceIds?.length || 0);

            transaction(() => {
                if (assignedFaceIds?.length) {
                    _internal.assignToPersonBatch(assignedFaceIds, personId, { lock: false });
                }

                if (unassignedFaceIds?.length) {
                    // Filter out locked faces (defensive)
                    const unlocked = unassignedFaceIds.filter(id => {
                        const face = _internal.get(id);
                        if (face?.manually_tagged) {
                            console.warn('[AppState.faces.applyThresholdChanges]',
                                'Backend tried to unassign locked face:', id);
                            return false;
                        }
                        return true;
                    });
                    _internal.unassignBatch(unlocked);
                }

                AppState.people._internal.reconcilePerson(personId);
            });
        },

        /**
         * Toggle manual tag (legacy method).
         * @param {string} faceId - Face ID
         * @returns {Promise<boolean>} New locked state
         */
        async toggleManualTag(faceId) {
            // Ensure face is in cache before reading current state
            await this.ensureFacesInCache([faceId]);
            const face = _cache?.get(faceId);
            const newValue = !(face?.manually_tagged || false);
            await this.setLocked([faceId], newValue);
            return newValue;
        },

        /**
         * Rotate face bounding boxes for an image (optimistic update).
         *
         * Called when user rotates an image to immediately update the overlay
         * without waiting for backend API. The backend will update the DB
         * independently; this just updates the client-side cache for instant UI.
         *
         * NOTE: This does NOT broadcast changes because:
         * 1. The subscriber would fetch fresh data from server (old bboxes)
         * 2. The caller must manually re-render the overlay from cache
         *
         * Bounding box transformation for 90° clockwise:
         *   new_x = 1 - old_y - old_h
         *   new_y = old_x
         *   new_w = old_h
         *   new_h = old_w
         *
         * For 270° (counter-clockwise):
         *   new_x = old_y
         *   new_y = 1 - old_x - old_w
         *   new_w = old_h
         *   new_h = old_w
         *
         * @param {string} imageId - Image ID whose faces to rotate
         * @param {number} degrees - Rotation degrees (90 or 270)
         * @returns {Array} Updated faces array for caller to render
         */
        rotateBoundingBoxes(imageId, degrees) {
            if (!_cache || (degrees !== 90 && degrees !== 270)) return [];

            const faces = this.getForImage(imageId);
            if (!faces.length) return [];

            console.log('[AppState.faces.rotateBoundingBoxes]',
                imageId, degrees + '°', faces.length, 'faces');

            for (const face of faces) {
                const { box_x, box_y, box_w, box_h } = face;

                if (degrees === 90) {
                    // 90° clockwise
                    face.box_x = 1 - box_y - box_h;
                    face.box_y = box_x;
                    face.box_w = box_h;
                    face.box_h = box_w;
                } else {
                    // 270° clockwise (same as 90° counter-clockwise)
                    face.box_x = box_y;
                    face.box_y = 1 - box_x - box_w;
                    face.box_w = box_h;
                    face.box_h = box_w;
                }
            }

            // Don't broadcast - caller must handle re-render manually
            // (broadcasting would cause subscription to fetch old data from server)
            return faces;
        },

        /**
         * Invalidate cache.
         */
        invalidate() {
            _cache = null;
            _cacheIsPartial = false;
            invalidateDerived();
        },

        // =====================================================================
        // TEST HOOKS (only for test harness)
        // =====================================================================

        /**
         * Test utilities for test harness.
         * @private
         */
        _test: {
            /**
             * Reset cache to empty state.
             */
            reset() {
                _cache = new Map();
                invalidateDerived();
            },

            /**
             * Add a face directly to cache.
             * @param {Object} face - Face object
             */
            addToCache(face) {
                if (!_cache) _cache = new Map();
                _cache.set(face.id, face);
                invalidateDerived();
            },

            /**
             * Get raw cache for inspection.
             * @returns {Map}
             */
            getCache() {
                return _cache;
            }
        },

        /**
         * Get face by ID (alias for getById).
         * @param {string} id - Face ID
         * @returns {Object|null}
         */
        get(id) {
            return _cache?.get(id) || null;
        }
    };
})();


// =============================================================================
// PEOPLE DOMAIN
// =============================================================================

AppState.people = (function() {
    const { createSubscriberSystem, markDirty, transaction, queueTransaction } = AppState;
    const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /**
     * People cache.
     * @type {Map<string, Object>|null}
     */
    let _cache = null;

    /** @type {number} Last cache update timestamp */
    let _cacheTime = 0;

    /** @type {Map<string, number>} Thumbnail cache-bust timestamps */
    let _thumbnailBust = new Map();

    /** @type {boolean} */
    let _loading = false;

    /** @type {Promise|null} */
    let _pendingLoad = null;

    /** Cache TTL in ms */
    const CACHE_TTL = 30000;

    /** Domain reference for transaction system */
    const domainRef = { _name: 'people', _notify: notify };

    // =========================================================================
    // CACHE PRIMITIVES (_internal)
    // =========================================================================

    /**
     * Internal API for cache mutations.
     */
    const _internal = {
        /**
         * Get person by ID.
         * @param {string} id - Person ID
         * @returns {Object|null}
         */
        get(id) {
            return _cache?.get(id) || null;
        },

        /**
         * Add a person to cache.
         * @param {Object} person - Person object
         */
        add(person) {
            // Initialize cache if needed (person creation can happen before load)
            if (!_cache) _cache = new Map();
            console.log('[AppState.people._internal.add]', person.id, person.name);
            _cache.set(person.id, person);
            markDirty(domainRef);
        },

        /**
         * Remove a person from cache.
         * @param {string} id - Person ID
         */
        remove(id) {
            if (_cache?.delete(id)) {
                console.log('[AppState.people._internal.remove]', id);
                markDirty(domainRef);
            }
        },

        /**
         * Update a person in cache.
         * @param {string} id - Person ID
         * @param {Object} changes - Properties to merge
         */
        update(id, changes) {
            const person = _cache?.get(id);
            if (person) {
                Object.assign(person, changes);
                markDirty(domainRef);
            }
        },

        /**
         * Find person by name (case-insensitive).
         * @param {string} name - Person name
         * @returns {Object|null}
         */
        findByName(name) {
            if (!_cache) return null;
            const lowerName = name.toLowerCase().trim();
            for (const person of _cache.values()) {
                if (person.name.toLowerCase() === lowerName) {
                    return person;
                }
            }
            return null;
        },

        /**
         * Set person name.
         * Also updates denormalized name on all faces.
         * @param {string} personId - Person ID
         * @param {string} name - New name
         */
        setName(personId, name) {
            const person = this.get(personId);
            if (!person) return;

            console.log('[AppState.people._internal.setName]',
                personId, person.name, '->', name);

            person.name = name;

            // Update denormalized name on faces
            const personFaces = AppState.faces._internal.getForPerson(personId);
            for (const face of personFaces) {
                AppState.faces._internal.updateName(face.id, name);
            }

            markDirty(domainRef);
        },

        /**
         * Set preferred face.
         * INVARIANT: Also locks the face.
         * @param {string} personId - Person ID
         * @param {string} faceId - Face ID
         */
        setPreferred(personId, faceId) {
            const person = this.get(personId);
            if (!person) return;

            console.log('[AppState.people._internal.setPreferred]',
                personId, '->', faceId);

            person.preferred_face_id = faceId;
            AppState.faces._internal.setLocked(faceId, true);
            this.bustThumbnail(personId);
            markDirty(domainRef);
        },

        /**
         * Set recognition threshold.
         * @param {string} personId - Person ID
         * @param {number|null} threshold - Threshold value
         */
        setThreshold(personId, threshold) {
            const person = this.get(personId);
            if (person) {
                person.threshold = threshold;
                markDirty(domainRef);
            }
        },

        /**
         * Bust thumbnail cache for a person.
         * @param {string} personId - Person ID
         */
        bustThumbnail(personId) {
            _thumbnailBust.set(personId, Date.now());
        },

        /**
         * Increment face count.
         * @param {string} id - Person ID
         */
        incrementFaceCount(id) {
            const person = this.get(id);
            if (person) {
                person.face_count = (person.face_count || 0) + 1;
                markDirty(domainRef);
            }
        },

        /**
         * Decrement face count.
         * @param {string} id - Person ID
         * @returns {number} New face count
         */
        decrementFaceCount(id) {
            const person = this.get(id);
            if (person) {
                person.face_count = Math.max(0, (person.face_count || 1) - 1);
                markDirty(domainRef);
                return person.face_count;
            }
            return 0;
        },

        // =====================================================================
        // RECONCILIATION
        // =====================================================================

        /**
         * Reconcile a person's state after face changes.
         *
         * - Recalculates face_count from actual faces (only if cache is complete)
         * - Auto-deletes person if no faces remain (only if cache is complete)
         * - Reassigns preferred face if it was removed
         *
         * IMPORTANT: When faces cache is partial (e.g., only current image's faces),
         * we can't accurately count faces or determine if a person should be deleted.
         *
         * @param {string} personId - Person ID
         */
        reconcilePerson(personId) {
            const person = this.get(personId);
            if (!person) return;

            const linkedFaces = AppState.faces._internal.getForPerson(personId);

            // Only recalculate face_count if we have the full faces cache
            // With partial cache, we'd incorrectly set count to 0 or a small number
            if (!AppState.faces.isCachePartial()) {
                person.face_count = linkedFaces.length;

                console.log('[AppState.people._internal.reconcilePerson]',
                    personId, 'face_count:', person.face_count);

                if (person.face_count === 0) {
                    console.log('[AppState.people._internal.reconcilePerson]',
                        'Auto-deleting empty person:', personId);
                    this.remove(personId);
                    return;
                }
            } else {
                console.log('[AppState.people._internal.reconcilePerson]',
                    personId, 'skipping face_count recalc (partial cache)');
            }

            // Check if preferred face was removed (safe even with partial cache)
            if (linkedFaces.length > 0) {
                const preferredStillExists = linkedFaces.some(
                    f => f.id === person.preferred_face_id
                );

                if (!preferredStillExists) {
                    // Pick newest from what we have
                    const newest = linkedFaces.reduce((a, b) =>
                        (a.image_timestamp || 0) > (b.image_timestamp || 0) ? a : b
                    );
                    console.log('[AppState.people._internal.reconcilePerson]',
                        'Reassigning preferred:', newest.id);
                    this.setPreferred(personId, newest.id);
                }
            }
        },

        /**
         * Reconcile multiple persons.
         * @param {Set<string>|string[]} personIds - Person IDs
         */
        reconcileAll(personIds) {
            for (const personId of personIds) {
                this.reconcilePerson(personId);
            }
        }
    };

    // =========================================================================
    // PERSIST FUNCTIONS
    // =========================================================================

    async function _persistRename(personId, name) {
        console.log('[AppState.people._persistRename]', personId, name);
        await App.apiPatch(`/people/${personId}`, { name });
    }

    async function _persistSetPreferred(personId, faceId) {
        console.log('[AppState.people._persistSetPreferred]', personId, faceId);
        await App.apiPatch(`/people/${personId}`, { preferred_face_id: faceId });
        await App.apiPatch('/faces', { face_ids: [faceId], locked: true });
    }

    async function _persistSetThreshold(personId, threshold) {
        console.log('[AppState.people._persistSetThreshold]', personId, threshold);
        return await App.apiPatch(`/people/${personId}`, { threshold });
    }

    /**
     * Persist merge operation.
     * SEQUENCE: assign faces → delete person
     */
    async function _persistMerge(faceIds, fromId, toId) {
        console.log('[AppState.people._persistMerge]',
            faceIds.length, 'faces from', fromId, 'to', toId);
        if (faceIds.length > 0) {
            await App.apiPost('/faces/assign', { face_ids: faceIds, person_id: toId });
        }
        await App.apiDelete(`/people/${fromId}`);
    }

    /**
     * Persist dissolve operation.
     * SEQUENCE: unassign faces → delete person
     */
    async function _persistDissolve(faceIds, personId) {
        console.log('[AppState.people._persistDissolve]',
            faceIds.length, 'faces from', personId);
        if (faceIds.length > 0) {
            await App.apiPost('/faces/unassign', { face_ids: faceIds });
        }
        await App.apiDelete(`/people/${personId}`);
    }

    // =========================================================================
    // LOAD
    // =========================================================================

    async function load(force = false) {
        if (!force && _cache !== null && (Date.now() - _cacheTime) < CACHE_TTL) {
            return;
        }
        if (_pendingLoad) return _pendingLoad;

        _loading = true;
        console.log('[AppState.people.load] Starting...');

        _pendingLoad = (async () => {
            try {
                const response = await App.apiGet('/people');
                _cache = new Map(response.data.map(p => [p.id, p]));
                _cacheTime = Date.now();

                console.log('[AppState.people.load] Loaded', _cache.size, 'people');
                broadcast({ type: 'changed' });

            } catch (err) {
                console.error('[AppState.people.load] Error:', err);
                broadcastError(err.message || 'Failed to load people');
                throw err;
            } finally {
                _loading = false;
                _pendingLoad = null;
            }
        })();

        return _pendingLoad;
    }

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        _name: 'people',
        _notify: notify,
        _internal,

        onChanged: subscribe,
        onError: subscribeError,

        load,
        invalidate() { _cacheTime = 0; },
        reload() { return load(true); },

        // --- Accessors ---

        /**
         * Get all people.
         * @returns {Array}
         */
        getAll() {
            return _cache ? Array.from(_cache.values()) : [];
        },

        /**
         * Get person by ID.
         * @param {string} id - Person ID
         * @returns {Object|null}
         */
        getById(id) {
            return _cache?.get(id) || null;
        },

        /**
         * Get person by name.
         * @param {string} name - Person name
         * @returns {Object|null}
         */
        getByName(name) {
            return _internal.findByName(name);
        },

        /**
         * Get people count.
         * @returns {number}
         */
        getCount() {
            return _cache?.size || 0;
        },

        /**
         * Check if people are loaded.
         * @returns {boolean}
         */
        isLoaded() {
            return _cache !== null;
        },

        /**
         * Check if people are loading.
         * @returns {boolean}
         */
        isLoading() {
            return _loading;
        },

        /**
         * Search people by name (fuzzy subsequence match).
         * @param {string} query - Search query
         * @returns {Array} Matching people, sorted
         */
        search(query) {
            if (!_cache) return [];

            const lowerQuery = query.toLowerCase().trim();
            if (!lowerQuery) {
                return Array.from(_cache.values())
                    .sort((a, b) =>
                        (b.face_count || 0) - (a.face_count || 0) ||
                        a.name.localeCompare(b.name)
                    );
            }

            function fuzzyMatch(q, target) {
                let qi = 0;
                for (let ti = 0; ti < target.length && qi < q.length; ti++) {
                    if (target[ti] === q[qi]) qi++;
                }
                return qi === q.length;
            }

            return Array.from(_cache.values())
                .filter(p => fuzzyMatch(lowerQuery, p.name.toLowerCase()))
                .sort((a, b) => {
                    const aPrefix = a.name.toLowerCase().startsWith(lowerQuery);
                    const bPrefix = b.name.toLowerCase().startsWith(lowerQuery);
                    if (aPrefix !== bPrefix) return bPrefix - aPrefix;

                    const aSubstr = a.name.toLowerCase().includes(lowerQuery);
                    const bSubstr = b.name.toLowerCase().includes(lowerQuery);
                    if (aSubstr !== bSubstr) return bSubstr - aSubstr;

                    return (b.face_count || 0) - (a.face_count || 0) ||
                        a.name.localeCompare(b.name);
                });
        },

        /**
         * Fetch person by ID from backend.
         * @param {string} id - Person ID
         * @returns {Promise<Object>}
         */
        async fetchById(id) {
            const response = await App.apiGet(`/people/${id}`);
            const person = response?.data;
            if (_cache && person) {
                _cache.set(person.id, person);
            }
            return person;
        },

        /**
         * Get thumbnail URL with cache busting.
         * @param {string} personId - Person ID
         * @param {number} [size=200] - Thumbnail size
         * @returns {string}
         */
        getThumbnailUrl(personId, size = 200) {
            const bust = _thumbnailBust.get(personId);
            const bustParam = bust ? `&_=${bust}` : '';
            return `/api/people/${personId}/thumbnail?size=${size}${bustParam}`;
        },

        /**
         * Bust thumbnail cache for a person.
         * @param {string} personId - Person ID
         */
        bustThumbnailCache(personId) {
            _thumbnailBust.set(personId, Date.now());
        },

        // =====================================================================
        // PUBLIC MUTATIONS
        // =====================================================================

        /**
         * Rename a person.
         *
         * Handles edge cases:
         * - Same name: no-op
         * - Empty new name: dissolve (unidentify all faces)
         * - Name collision: merge into existing person
         * - Otherwise: simple rename
         *
         * @param {string} personId - Person ID
         * @param {string} newName - New name
         * @returns {Promise<void>}
         */
        rename(personId, newName) {
            const person = _internal.get(personId);
            if (!person) throw new Error('Person not found');

            const oldName = person.name;
            const trimmedNew = newName?.trim() || '';

            // Case A: No-op
            if (trimmedNew === oldName) {
                console.log('[AppState.people.rename] No-op, same name');
                return Promise.resolve();
            }

            // Case B: Error
            if (!oldName) {
                throw new Error('Person has no name');
            }

            // Case C: Dissolve
            if (!trimmedNew) {
                console.log('[AppState.people.rename] Empty name, delegating to dissolve');
                return this.dissolve(personId);
            }

            // Case D: Merge
            const collision = _internal.findByName(trimmedNew);
            if (collision && collision.id !== personId) {
                console.log('[AppState.people.rename] Collision, delegating to merge');
                return this.merge(personId, collision.id);
            }

            // Case E: Simple rename
            console.log('[AppState.people.rename]', personId, oldName, '->', trimmedNew);

            const backup = { name: oldName };

            transaction(() => {
                _internal.setName(personId, trimmedNew);
            });

            return queueTransaction(async () => {
                try {
                    await _persistRename(personId, trimmedNew);
                } catch (err) {
                    console.error('[AppState.people.rename] Persist failed, rolling back:', err);
                    transaction(() => {
                        _internal.setName(personId, backup.name);
                    });
                    broadcastError(err.message || 'Failed to rename person');
                    throw err;
                }
            });
        },

        /**
         * Merge one person into another.
         *
         * All faces from source move to target.
         * Source person is deleted.
         * Faces keep their locked status.
         *
         * @param {string} fromId - Person to merge from (will be deleted)
         * @param {string} toId - Person to merge into
         * @returns {Promise<void>}
         */
        async merge(fromId, toId) {
            if (fromId === toId) return;

            const fromPerson = _internal.get(fromId);
            const toPerson = _internal.get(toId);
            if (!fromPerson || !toPerson) throw new Error('Person not found');

            console.log('[AppState.people.merge]', fromId, '->', toId);

            // Ensure faces are loaded for the source person
            let faces = AppState.faces.getForPerson(fromId);
            if (faces.length === 0 && fromPerson.face_count > 0) {
                // Faces not in cache - fetch them
                faces = await AppState.faces.fetchForPerson(fromId);
            }
            const faceIds = faces.map(f => f.id);

            // Backup
            const backup = {
                fromPerson: { ...fromPerson },
                toPerson: { ...toPerson },
                facePersonIds: new Map()
            };
            for (const faceId of faceIds) {
                backup.facePersonIds.set(faceId, fromId);
            }

            transaction(() => {
                for (const faceId of faceIds) {
                    AppState.faces._internal.linkToPerson(faceId, toId, toPerson.name);
                }
                toPerson.face_count += faceIds.length;
                _internal.remove(fromId);
            });

            return queueTransaction(async () => {
                try {
                    await _persistMerge(faceIds, fromId, toId);
                } catch (err) {
                    console.error('[AppState.people.merge] Persist failed, rolling back:', err);
                    transaction(() => {
                        _internal.add(backup.fromPerson);
                        for (const faceId of faceIds) {
                            AppState.faces._internal.linkToPerson(
                                faceId, fromId, backup.fromPerson.name
                            );
                        }
                        Object.assign(toPerson, backup.toPerson);
                    });
                    broadcastError(err.message || 'Failed to merge people');
                    throw err;
                }
            });
        },

        /**
         * Dissolve a person.
         *
         * Unidentifies all faces (returns to unknown pool).
         * Deletes the person.
         *
         * @param {string} personId - Person to dissolve
         * @returns {Promise<void>}
         */
        dissolve(personId) {
            const person = _internal.get(personId);
            if (!person) throw new Error('Person not found');

            console.log('[AppState.people.dissolve]', personId);

            const faceIds = AppState.faces.getForPerson(personId).map(f => f.id);

            // Backup
            const backup = {
                person: { ...person },
                faces: new Map()
            };
            for (const faceId of faceIds) {
                const face = AppState.faces._internal.get(faceId);
                if (face) {
                    backup.faces.set(faceId, {
                        person_id: face.person_id,
                        person_name: face.person_name,
                        manually_tagged: face.manually_tagged
                    });
                }
            }

            transaction(() => {
                for (const faceId of faceIds) {
                    AppState.faces._internal.unlinkFromPerson(faceId);
                    AppState.faces._internal.setLocked(faceId, false);
                }
                _internal.remove(personId);
            });

            return queueTransaction(async () => {
                try {
                    await _persistDissolve(faceIds, personId);
                } catch (err) {
                    console.error('[AppState.people.dissolve] Persist failed, rolling back:', err);
                    transaction(() => {
                        _internal.add(backup.person);
                        for (const [fid, data] of backup.faces) {
                            AppState.faces._internal.linkToPerson(
                                fid, data.person_id, data.person_name
                            );
                            AppState.faces._internal.setLocked(fid, data.manually_tagged);
                        }
                    });
                    broadcastError(err.message || 'Failed to dissolve person');
                    throw err;
                }
            });
        },

        /**
         * Set preferred face for a person.
         * Also locks the face (invariant).
         *
         * @param {string} personId - Person ID
         * @param {string} faceId - Face ID to set as preferred
         * @returns {Promise<void>}
         */
        setPreferredFace(personId, faceId) {
            const person = _internal.get(personId);
            if (!person) throw new Error('Person not found');

            const face = AppState.faces._internal.get(faceId);
            if (face?.person_id !== personId) {
                throw new Error('Face does not belong to this person');
            }

            console.log('[AppState.people.setPreferredFace]', personId, '->', faceId);

            const backup = {
                preferred_face_id: person.preferred_face_id,
                face_locked: face?.manually_tagged
            };

            transaction(() => {
                _internal.setPreferred(personId, faceId);
            });

            return queueTransaction(async () => {
                try {
                    await _persistSetPreferred(personId, faceId);
                } catch (err) {
                    console.error('[AppState.people.setPreferredFace] Persist failed:', err);
                    transaction(() => {
                        person.preferred_face_id = backup.preferred_face_id;
                        if (face) {
                            face.manually_tagged = backup.face_locked;
                        }
                        _internal.bustThumbnail(personId);
                        markDirty(domainRef);
                    });
                    broadcastError(err.message || 'Failed to set preferred face');
                    throw err;
                }
            });
        },

        /**
         * Set recognition threshold for a person.
         *
         * Backend re-evaluates faces and may return:
         * - assigned: faces that now match
         * - unassigned: faces that no longer match
         *
         * @param {string} personId - Person ID
         * @param {number|null} threshold - Threshold (0.6-0.99) or null for default
         * @returns {Promise<Object>}
         */
        setThreshold(personId, threshold) {
            const person = _internal.get(personId);
            if (!person) throw new Error('Person not found');

            console.log('[AppState.people.setThreshold]', personId, '->', threshold);

            const backup = person.threshold;

            transaction(() => {
                _internal.setThreshold(personId, threshold);
            });

            return queueTransaction(async () => {
                try {
                    const response = await _persistSetThreshold(personId, threshold);

                    // Handle threshold changes from backend
                    if (response?.data?.assigned || response?.data?.unassigned) {
                        console.log('[AppState.people.setThreshold] Applying backend changes');
                        AppState.faces.applyThresholdChanges(
                            personId,
                            response.data.assigned,
                            response.data.unassigned
                        );
                    }
                    return response;

                } catch (err) {
                    console.error('[AppState.people.setThreshold] Persist failed:', err);
                    transaction(() => {
                        _internal.setThreshold(personId, backup);
                    });
                    broadcastError(err.message || 'Failed to update threshold');
                    throw err;
                }
            });
        },

        /**
         * Create a new person (legacy method).
         * Usually people are created via faces.identify().
         *
         * @param {string} name - Person name
         * @returns {Promise<Object>} Created person
         */
        create(name) {
            const personId = crypto.randomUUID();
            const person = {
                id: personId,
                name,
                face_count: 0,
                preferred_face_id: null,
                threshold: null
            };

            console.log('[AppState.people.create]', personId, name);

            transaction(() => {
                _internal.add(person);
            });

            return queueTransaction(async () => {
                try {
                    await App.apiPost('/people', { id: personId, name });
                    return person;
                } catch (err) {
                    console.error('[AppState.people.create] Persist failed:', err);
                    transaction(() => {
                        _internal.remove(personId);
                    });
                    broadcastError(err.message || 'Failed to create person');
                    throw err;
                }
            });
        },

        /**
         * Delete a person (alias for dissolve).
         * @param {string} id - Person ID
         * @returns {Promise<void>}
         */
        delete(id) {
            return this.dissolve(id);
        },

        // =====================================================================
        // TEST HOOKS (only for test harness)
        // =====================================================================

        /**
         * Test utilities for test harness.
         * @private
         */
        _test: {
            /**
             * Reset cache to empty state.
             */
            reset() {
                _cache = new Map();
                _cacheTime = Date.now();
                _thumbnailBust.clear();
            },

            /**
             * Add a person directly to cache.
             * @param {Object} person - Person object
             */
            addToCache(person) {
                if (!_cache) _cache = new Map();
                _cache.set(person.id, person);
                _cacheTime = Date.now();
            },

            /**
             * Get raw cache for inspection.
             * @returns {Map}
             */
            getCache() {
                return _cache;
            }
        },

        /**
         * Get person by ID (alias for getById).
         * @param {string} id - Person ID
         * @returns {Object|null}
         */
        get(id) {
            return _cache?.get(id) || null;
        }
    };
})();
