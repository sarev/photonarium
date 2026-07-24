/**
 * AppState Images Domain - Image Metadata
 * =========================================
 *
 * Manages image metadata with:
 * - Delta sync with backend (epoch-based updates)
 * - Display list (sorted and filtered view)
 * - Similarity data for content sorting
 * - People names for people sorting
 *
 * The display list is the single source of truth for what the gallery shows.
 * It's lazily recomputed when images, sort, or filter changes.
 *
 * @fileoverview Image metadata and display list domain.
 */

'use strict';

AppState.images = (function() {
    const { createSubscriberSystem, markDirty, transaction, queueTransaction } = AppState;
    const { subscribe, subscribeError, broadcast, notify, broadcastError } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {Map<string, Object>|null} Image cache */
    let _cache = null;

    /** @type {string|null} Backend epoch for delta updates */
    let _cacheEpoch = null;

    /** @type {boolean} */
    let _loading = false;

    /** @type {Promise|null} */
    let _pendingLoad = null;

    // Display list state
    /** @type {Array} Sorted and filtered images */
    let _displayList = [];

    /** @type {boolean} Whether display list needs recomputation */
    let _displayListDirty = true;

    // Sort data (loaded on demand)
    /** @type {{referenceId: string, scores: Map}|null} */
    let _similarities = null;

    /** @type {Object|null} imageId → comma-separated people names */
    let _peopleNames = null;

    /** @type {Map<string, Object>|null} imageId → quality score breakdown (debug) */
    let _qualityBreakdown = null;

    /** Domain reference for transaction system */
    const domainRef = { _name: 'images', _notify: notify };

    // =========================================================================
    // DISPLAY LIST HELPERS
    // =========================================================================

    /**
     * Mark display list as needing recomputation.
     * @private
     */
    function _markDisplayListDirty() {
        _displayListDirty = true;
        _qualityBreakdown = null;
    }

    /**
     * Ensure display list is computed.
     * @private
     */
    function _ensureDisplayList() {
        if (!_displayListDirty) return;

        if (!_cache) {
            _displayList = [];
        } else {
            const all = Array.from(_cache.values());
            _displayList = _filterImages(_sortImages(all));
        }
        _displayListDirty = false;
    }

    /**
     * Parse an image basename into its enhancement family key and depth.
     *
     * A derived image is named ``<stem>__enhanced_<depth>.<ext>`` (kept flat —
     * see imagedb.derived_image_name), so its source and all its derivatives
     * share ``stem``, and ``depth`` (0 for the source) orders them by recency.
     * Used as the sort tie-break so a derived image groups with — and sorts by
     * age relative to — its source when they share a timestamp.
     * @param {string} basename
     * @returns {{ stem: string, depth: number }}
     * @private
     */
    function _enhancedFamilyKey(basename) {
        const stem = (basename || '').replace(/\.[^.]*$/, '');  // drop extension
        const m = stem.match(/^(.*)__enhanced_(\d+)$/);
        return m ? { stem: m[1], depth: parseInt(m[2], 10) } : { stem, depth: 0 };
    }

    /**
     * Sort images based on view settings.
     * @param {Array} images
     * @returns {Array}
     * @private
     */
    function _sortImages(images) {
        const { by, direction } = AppState.view.getSort();
        const sorted = [...images];

        // Quality sort: absolute aesthetic + percentile-ranked technical factors
        // (works for both group views and the full gallery).
        // Higher scores = better quality.  'desc' (default) = best first.
        if (by === 'quality') {
            const dir = direction === 'desc' ? 1 : -1;
            const scores = _computeQualityScores(sorted);
            sorted.sort((a, b) => {
                const qa = scores.get(a.id) || 0;
                const qb = scores.get(b.id) || 0;
                if (qa !== qb) return (qb - qa) * dir;
                // Deterministic tiebreak: pixels, sharpness, file size, ID
                const pa = a.width * a.height, pb = b.width * b.height;
                if (pa !== pb) return (pb - pa) * dir;
                const sa = a.laplacian_var || 0, sb = b.laplacian_var || 0;
                if (sa !== sb) return (sb - sa) * dir;
                if (a.size !== b.size) return (b.size - a.size) * dir;
                return a.id < b.id ? -1 : 1;
            });
            return sorted;
        }

        sorted.sort((a, b) => {
            let cmp = 0;
            if (by === 'date') {
                cmp = new Date(a.timestamp) - new Date(b.timestamp);
            } else if (by === 'rating') {
                cmp = (a.rating || '').localeCompare(b.rating || '');
            } else if (by === 'content') {
                const simA = _similarities?.scores.get(a.id) || 0;
                const simB = _similarities?.scores.get(b.id) || 0;
                cmp = simA - simB;
            } else if (by === 'people') {
                const namesA = _peopleNames?.[a.id] || '';
                const namesB = _peopleNames?.[b.id] || '';
                cmp = namesA.localeCompare(namesB, undefined, { sensitivity: 'base' });
            }
            // Deterministic tie-break for equal sort keys.  Images sharing a
            // key — most importantly a derived "…__enhanced_N" image, which
            // inherits its source's timestamp — would otherwise fall back to
            // arbitrary insertion order and drift to the far end of the run.
            // Break ties by enhancement family (stem) then depth: this keeps a
            // derived image adjacent to its source, ordered oldest→newest
            // (source before its derivatives).  Folded into cmp *before* the
            // direction flip, so it reverses with the sort — newest-first shows
            // the derived above its source, oldest-first shows it below.
            if (cmp === 0) {
                const fa = _enhancedFamilyKey(a.basename);
                const fb = _enhancedFamilyKey(b.basename);
                cmp = fa.stem.localeCompare(fb.stem, undefined, { numeric: true, sensitivity: 'base' })
                    || (fa.depth - fb.depth);
            }
            return direction === 'asc' ? cmp : -cmp;
        });

        return sorted;
    }

    /**
     * Compute composite quality scores for a set of images. Works on any
     * image set (group or full gallery). The aesthetic component uses
     * absolute scores (LAION and NIMA divided by 10 to normalise to 0-1),
     * while sharpness, pixel count, and bits-per-pixel use percentile
     * ranking within the set.  Weights and NIMA/LAION blend ratio come
     * from the backend configuration (``/api/config``).
     *
     * @param {Array} images - Array of image objects with aesthetic_laion, aesthetic_nima, laplacian_var, width, height, size
     * @returns {Map<string, number>} Map of image ID to quality score [0..1]
     * @private
     */
    function _computeQualityScores(images) {
        const n = images.length;
        if (n === 0) return new Map();

        // Read quality config (weights + NIMA/LAION blend alpha)
        const qc = App.getQualityConfig();
        const wA = qc.weightAesthetic;
        const wS = qc.weightSharpness;
        const wP = qc.weightPixels;
        const wB = qc.weightBpp;
        const alpha = qc.alpha;

        // Aesthetic: blend NIMA and LAION raw scores, then percentile-rank.
        // Both models produce scores in narrow bands (NIMA: ~3.5-6.5,
        // LAION: ~0-7) so dividing by 10 wastes most of the [0,1] range.
        // Percentile ranking uses the full range and adapts to the actual
        // score distribution in the current image set.
        const hasNima = images.some(i => i.aesthetic_nima != null);
        const A = _percentileRanks(images.map(i => {
            const laion = i.aesthetic_laion ?? 0;
            if (!hasNima || i.aesthetic_nima == null) return laion;
            const nima = i.aesthetic_nima;
            return alpha * nima + (1 - alpha) * laion;
        }));

        // Other components — percentile-ranked
        const S = _percentileRanks(images.map(i => Math.log1p(i.laplacian_var || 0)));
        const P = _percentileRanks(images.map(i => i.width * i.height));
        const B = _percentileRanks(images.map(i => 8 * i.size / Math.max(1, i.width * i.height)));

        // Combine with configurable weights and store breakdown for debugging
        const scores = new Map();
        _qualityBreakdown = new Map();
        for (let i = 0; i < n; i++) {
            const total = wA * A[i] + wS * S[i] + wP * P[i] + wB * B[i];
            scores.set(images[i].id, total);
            _qualityBreakdown.set(images[i].id, {
                total,
                aesthetic: A[i],
                sharpness: S[i],
                pixels: P[i],
                bpp: B[i],
                rawLaion: images[i].aesthetic_laion,
                rawNima: images[i].aesthetic_nima,
            });
        }
        return scores;
    }

    /**
     * Convert raw values to percentile ranks [0..1] using average-rank for ties.
     * @param {Array<number>} values - Raw values to rank
     * @returns {Array<number>} Percentile ranks, same length as input
     * @private
     */
    function _percentileRanks(values) {
        const n = values.length;
        if (n === 1) return [0.5];

        // Create (value, originalIndex) pairs, sort ascending
        const indexed = values.map((v, i) => [v, i]);
        indexed.sort((a, b) => a[0] - b[0]);

        // Assign average ranks for ties, normalised to [0..1]
        const ranks = new Array(n);
        let i = 0;
        while (i < n) {
            let j = i;
            while (j < n && indexed[j][0] === indexed[i][0]) j++;
            const avgRank = (i + j - 1) / 2;  // 0-based average rank
            for (let k = i; k < j; k++) {
                ranks[indexed[k][1]] = avgRank / (n - 1);  // Normalise to [0..1]
            }
            i = j;
        }
        return ranks;
    }

    /**
     * Filter images based on filter settings.
     * @param {Array} images
     * @returns {Array}
     * @private
     */
    function _filterImages(images) {
        const currentFilter = AppState.filter.get();
        if (!currentFilter) return images;

        // Duplicates filter
        if (currentFilter.type === 'duplicates' && Array.isArray(currentFilter.imageIds)) {
            const idSet = new Set(currentFilter.imageIds.map(String));
            return images.filter(img => idSet.has(String(img.id)));
        }

        // Pre-compute filter values outside the loop (avoids per-image object creation)
        const isSemantic = currentFilter.type === 'semantic';
        const idSet = Array.isArray(currentFilter.imageIds) ? new Set(currentFilter.imageIds.map(String)) : null;
        const scores = isSemantic ? (currentFilter.scores || {}) : null;
        const textLower = (!isSemantic && currentFilter.text) ? currentFilter.text.toLowerCase() : null;
        const dateFrom = currentFilter.dateFrom || null;
        const dateTo = currentFilter.dateTo || null;
        const isDateRange = !!currentFilter.dateRange;
        const hasDateFilter = dateFrom !== null || dateTo !== null;
        const filterEmoji = currentFilter.rating ? [...currentFilter.rating] : null;
        const filterRatingExact = !!currentFilter.ratingExact;
        const peopleImageIds = (currentFilter.people && currentFilter.peopleImageIds) || null;
        const metadataImageIds = currentFilter.metadataImageIds || null;
        const searchMode = currentFilter.searchMode || 'all';

        // Single pass through all images
        const filtered = images.filter(img => {
            // Media type filtering based on search mode
            if (searchMode === 'images' && img.media_type === 'video') return false;
            if (searchMode === 'videos' && img.media_type !== 'video') return false;
            if (idSet && !idSet.has(String(img.id))) return false;
            if (textLower) {
                const desc = (img.description || '').toLowerCase();
                if (!desc.includes(textLower)) return false;
            }
            if (hasDateFilter) {
                if (!AppState.filter.matchDate(img.timestamp, dateFrom, dateTo, isDateRange)) return false;
            }
            if (filterEmoji) {
                if (filterRatingExact) {
                    // Exact: the rating must equal the entered string verbatim,
                    // so '⭐' matches only one-star, not '⭐⭐'/'⭐⭐⭐'.
                    if ((img.rating || '') !== currentFilter.rating) return false;
                } else if (!filterEmoji.some(e => img.rating && img.rating.includes(e))) {
                    return false;
                }
            }
            if (peopleImageIds && !peopleImageIds.has(String(img.id))) return false;
            if (metadataImageIds && !metadataImageIds.has(String(img.id))) return false;
            return true;
        });

        // Semantic results sorted by similarity score
        if (scores) {
            filtered.sort((a, b) => (scores[b.id] || 0) - (scores[a.id] || 0));
        }

        return filtered;
    }

    // Subscribe to view and filter changes
    AppState.view.onChanged((event) => {
        if (event.property === 'sortBy' || event.property === 'sortDirection') {
            _markDisplayListDirty();
            broadcast({ type: 'changed', property: 'displayList' });
        }
    });

    AppState.filter.onChanged(() => {
        _markDisplayListDirty();
        broadcast({ type: 'changed', property: 'displayList' });
    });

    // =========================================================================
    // INTERNAL API
    // =========================================================================

    const _internal = {
        /**
         * Update an image in cache.
         * @param {string} id - Image ID
         * @param {Object} changes - Properties to merge
         */
        update(id, changes) {
            const image = _cache?.get(id);
            if (image) {
                Object.assign(image, changes);
                _markDisplayListDirty();
                markDirty(domainRef);
            }
        },

        /**
         * Remove an image from cache.
         * @param {string} id - Image ID
         */
        remove(id) {
            if (_cache?.delete(id)) {
                _markDisplayListDirty();
                markDirty(domainRef);
            }
        },

        /**
         * Get image by ID.
         * @param {string} id - Image ID
         * @returns {Object|null}
         */
        get(id) {
            return _cache?.get(id) || null;
        },
    };

    // =========================================================================
    // FACE CLEANUP HELPER
    // =========================================================================

    /**
     * Handle face/person cleanup when deleting an image.
     * @param {string} imageId - Image being deleted
     * @private
     */
    function handleFaceCleanup(imageId) {
        const imageFaces = AppState.faces.getForImage(imageId);
        if (!imageFaces || imageFaces.length === 0) return;

        const personUpdates = new Map();

        for (const face of imageFaces) {
            if (face.person_id) {
                const existing = personUpdates.get(face.person_id) || {
                    decrement: 0,
                    wasPreferred: false,
                };
                existing.decrement++;

                const person = AppState.people._internal.get(face.person_id);
                if (person?.preferred_face_id === face.id) {
                    existing.wasPreferred = true;
                }
                personUpdates.set(face.person_id, existing);
            }
            AppState.faces._internal.remove(face.id);
        }

        for (const [personId, updates] of personUpdates) {
            for (let i = 0; i < updates.decrement; i++) {
                const newCount = AppState.people._internal.decrementFaceCount(personId);
                if (newCount === 0) {
                    AppState.people._internal.remove(personId);
                    break;
                }
            }

            const person = AppState.people._internal.get(personId);
            if (person && updates.wasPreferred) {
                const remainingFace = AppState.faces._internal.getFirstForPerson(
                    personId, { excludingImageId: imageId },
                );
                if (remainingFace) {
                    AppState.people._internal.update(personId, {
                        preferred_face_id: remainingFace.id,
                    });
                    AppState.people._internal.bustThumbnail(personId);
                    AppState.faces._internal.update(remainingFace.id, {
                        manually_tagged: true,
                    });
                }
            }
        }
    }

    // =========================================================================
    // LOAD
    // =========================================================================

    /**
     * Load images (full or delta based on cache state).
     * @param {boolean} [forceFullReload=false] - Force full reload
     * @returns {Promise<void>}
     */
    async function load(forceFullReload = false) {
        if (_pendingLoad) return _pendingLoad;

        _loading = true;

        _pendingLoad = (async () => {
            try {
                if (_cache === null || forceFullReload) {
                    // Full load
                    const response = await App.apiGet('/images');
                    const data = response.data;
                    _cache = new Map(data.images.map(img => [img.id, img]));
                    _cacheEpoch = data.epoch;
                } else {
                    // Delta load
                    const response = await App.apiGet(`/images?since=${_cacheEpoch}`);
                    const data = response.data;

                    if (data.updated) {
                        for (const img of data.updated) {
                            _cache.set(img.id, img);
                        }
                    }
                    if (data.deleted_ids) {
                        for (const id of data.deleted_ids) {
                            _cache.delete(id);
                        }
                    }
                    _cacheEpoch = data.epoch;
                }

                _markDisplayListDirty();
                broadcast({ type: 'changed', reload: forceFullReload });

            } catch (err) {
                console.error('[AppState.images.load] Error:', err);
                broadcastError(err.message || 'Failed to load images');
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
        _name: 'images',
        _notify: notify,
        _internal,

        onChanged: subscribe,
        onError: subscribeError,

        load,
        reload() { return load(true); },

        // --- Accessors ---

        /**
         * Get all images.
         * @returns {Array}
         */
        getAll() {
            return _cache ? Array.from(_cache.values()) : [];
        },

        /**
         * Get image by ID.
         * @param {string} id - Image ID
         * @returns {Object|null}
         */
        getById(id) {
            return _cache?.get(id) || null;
        },

        /**
         * Get image count.
         * @returns {number}
         */
        getCount() {
            return _cache?.size || 0;
        },

        /**
         * Check if images are loaded.
         * @returns {boolean}
         */
        isLoaded() {
            return _cache !== null;
        },

        /**
         * Check if images are loading.
         * @returns {boolean}
         */
        isLoading() {
            return _loading;
        },

        // --- Display List ---

        /**
         * Get the display list (sorted and filtered images).
         *
         * This is the single source of truth for gallery display.
         * Lazily recomputed when dependencies change.
         *
         * @returns {Array} DO NOT MUTATE
         */
        getDisplayList() {
            _ensureDisplayList();
            return _displayList;
        },

        /**
         * Get quality score breakdown for a given image (available after quality sort).
         * @param {string} id - Image ID
         * @returns {Object|null} Breakdown with total, aesthetic, sharpness, pixels, bpp, rawLaion, rawNima
         */
        getQualityBreakdown(id) {
            _ensureDisplayList();
            return _qualityBreakdown?.get(id) || null;
        },

        // --- Mutations ---

        /**
         * Update one or more images.
         * @param {Object|Array} updates - {id, ...changes} or array of them
         * @returns {Promise<void>}
         */
        update(updates) {
            if (!App.requireOnline()) return;
            if (!Array.isArray(updates)) updates = [updates];

            console.log('[AppState.images.update]', updates.length, 'images');

            // PHASE 1: Synchronous optimistic updates
            const backup = new Map();
            transaction(() => {
                for (const upd of updates) {
                    const image = _cache?.get(upd.id);
                    if (image) {
                        backup.set(upd.id, { ...image });
                        _internal.update(upd.id, upd);
                    }
                }
            });

            // PHASE 2: Persist to backend
            return queueTransaction(async () => {
                try {
                    for (const upd of updates) {
                        const { id, ...changes } = upd;
                        await App.apiPost(`/images/${id}`, changes);
                    }
                } catch (err) {
                    console.error('[AppState.images.update] Persist failed:', err);
                    transaction(() => {
                        for (const [id, img] of backup) {
                            _cache.set(id, img);
                            markDirty(domainRef);
                        }
                    });
                    broadcastError(err.message || 'Failed to update images');
                    throw err;
                }
            });
        },

        /**
         * Delete one or more images by moving them to the trash directory.
         * Handles cascade: faces → people → duplicates → images.
         *
         * @param {string|Array} ids - Image ID(s)
         * @returns {Promise<void>}
         */
        delete(ids) {
            if (!App.requireOnline()) return;
            if (!Array.isArray(ids)) ids = [ids];

            console.log('[AppState.images.delete]', ids.length, 'images');

            return queueTransaction(async () => {
                const backup = new Map();
                for (const id of ids) {
                    const img = _cache?.get(id);
                    if (img) backup.set(id, img);
                }

                // Handle cascade cleanup
                for (const id of ids) {
                    handleFaceCleanup(id);
                    AppState.duplicates._internal.removeImage(id);
                    _internal.remove(id);
                }

                try {
                    await App.apiPost('/images/trash', { image_ids: ids });
                    // Refresh folder counts so Database screen reflects the change
                    AppState.folders.load();
                    AppState.folders.loadStats();
                } catch (err) {
                    console.error('[AppState.images.delete] Persist failed:', err);
                    broadcastError(err.message || 'Failed to move images to trash');
                    // Cascade rollback is complex - reload instead
                    AppState.faces.reload();
                    AppState.people.reload();
                    load(true);
                    throw err;
                }
            });
        },

        /**
         * Rotate one or more images.
         * @param {string|Array} ids - Image ID(s)
         * @param {number} degrees - Rotation angle in degrees (clockwise positive).
         *                           Common values: 90 (right), 180, 270 (left).
         * @returns {Promise<void>}
         */
        rotate(ids, degrees) {
            if (!App.requireOnline()) return;
            if (!Array.isArray(ids)) ids = [ids];

            console.log('[AppState.images.rotate]', ids.length, 'images by', degrees + '°');

            return queueTransaction(async () => {
                try {
                    await App.apiPost('/images/rotate', { image_ids: ids, degrees });

                    // Update cached dimensions for 90/270 rotations
                    for (const id of ids) {
                        const image = _cache?.get(id);
                        if (image && (degrees === 90 || degrees === 270)) {
                            const temp = image.width;
                            image.width = image.height;
                            image.height = temp;
                        }
                    }
                    broadcast({ type: 'changed' });
                } catch (err) {
                    console.error('[AppState.images.rotate] Error:', err);
                    broadcastError(err.message || 'Failed to rotate images');
                    throw err;
                }
            });
        },

        /**
         * Fetch single image by ID.
         * Uses cache if available; falls back to API for full details.
         * @param {string} id - Image ID
         * @returns {Promise<Object>}
         */
        async fetchById(id) {
            const cached = _cache?.get(id);
            if (cached) return cached;
            const response = await App.apiGet(`/images/${id}`);
            const image = response.data;
            if (_cache && image) {
                _cache.set(image.id, image);
            }
            return image;
        },

        /**
         * Fetch EXIF metadata for a single image (lazy-loaded).
         * Returns parsed exif_data object, or null if none available.
         * @param {string} id - Image ID
         * @returns {Promise<Object|null>}
         */
        async fetchExifData(id) {
            const response = await App.apiGet(`/images/${id}/exif`);
            return response.data?.exif_data || null;
        },

        // --- Similarity Data ---

        /**
         * Load similarity scores for content sorting.
         * @param {string} referenceId - Reference image ID
         * @returns {Promise<Object>}
         */
        async loadSimilarities(referenceId) {
            const response = await App.apiGet(`/similar/${referenceId}`);
            _similarities = {
                referenceId,
                scores: new Map(response.data.results.map(r => [r.id, r.similarity])),
            };
            _markDisplayListDirty();
            broadcast({ type: 'changed', property: 'similarities' });
            return response;
        },

        /**
         * Get similarity score for an image.
         * @param {string} imageId - Image ID
         * @returns {number}
         */
        getSimilarity(imageId) {
            return _similarities?.scores.get(imageId) || 0;
        },

        /**
         * Get reference image ID for similarity sort.
         * @returns {string|null}
         */
        getSimilarityReferenceId() {
            return _similarities?.referenceId || null;
        },

        /**
         * Clear similarity data.
         */
        clearSimilarities() {
            _similarities = null;
            _markDisplayListDirty();
        },

        // --- People Names ---

        /**
         * Load people names for people sorting.
         * @returns {Promise<Object>}
         */
        async loadPeopleNames() {
            const response = await App.apiGet('/images/people-names');
            _peopleNames = response.data;
            _markDisplayListDirty();
            broadcast({ type: 'changed', property: 'peopleNames' });
            return response.data;
        },

        /**
         * Get people names for an image.
         * @param {string} imageId - Image ID
         * @returns {string}
         */
        getPeopleNames(imageId) {
            return _peopleNames?.[imageId] || '';
        },

        /**
         * Check if people names are loaded.
         * @returns {boolean}
         */
        hasPeopleNames() {
            return _peopleNames !== null;
        },

        /**
         * Clear people names data.
         */
        clearPeopleNames() {
            _peopleNames = null;
            _markDisplayListDirty();
        },

        // --- Filter by People ---

        /**
         * Get image IDs filtered by people.
         * @param {string[]} peopleIds - Person IDs
         * @returns {Promise<Set<string>>}
         */
        async getFilteredByPeople(peopleIds) {
            const response = await App.apiGet(
                `/images?people=${encodeURIComponent(peopleIds.join(','))}`,
            );
            const images = response.data.images || [];
            return new Set(images.map(img => String(img.id)));
        },

        /**
         * Refresh metadata for specific images.
         * Uses delta sync to efficiently fetch only changed images.
         * Called by images_modified event handler.
         * @param {string[]} ids - Image IDs to refresh (for logging only)
         */
        async refreshByIds(ids) {
            if (!ids?.length) return;

            // Delta sync will fetch all images changed since last epoch,
            // which includes the rotated images (they have updated updated_at)
            await load();
        },

        /**
         * Remove images from cache in response to a multi-client event.
         * Backend already trashed/deleted; this cleans up the local cache
         * and cascades to faces, people, and duplicate group caches.
         *
         * @param {string[]} ids - IDs of removed images
         */
        autoRemove(ids) {
            if (!ids?.length || !_cache) return;
            transaction(() => {
                for (const id of ids) {
                    handleFaceCleanup(id);
                    AppState.duplicates._internal.removeImage(id);
                    _internal.remove(id);
                }
            });
        },

        /**
         * Invalidate cache.
         */
        invalidate() {
            _cache = null;
            _cacheEpoch = null;
        },
    };
})();
