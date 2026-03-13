/**
 * AppState Videos Domain - Video Browse & Search State
 * =====================================================
 *
 * Manages video-specific state for the Videos screen:
 * - Search results with per-scene scores (from video search)
 * - Browse mode (all videos)
 * - Selected video and its scene data
 * - Preferred scene management
 *
 * Memory only (not persisted).
 *
 * @fileoverview Videos domain for AppState.
 */

'use strict';

AppState.videos = (function() {
    const { createSubscriberSystem, transaction, queueTransaction } = AppState;
    const { subscribe, broadcast, notify } = createSubscriberSystem();

    // =========================================================================
    // STATE
    // =========================================================================

    /** @type {Array|null} Video search results (from POST /api/search/videos) */
    let _searchResults = null;

    /** @type {string|null} Active search query */
    let _query = null;

    /** @type {string|null} Currently selected video ID in the grid */
    let _selectedVideoId = null;

    /** @type {Array|null} All videos (browse mode) */
    let _allVideos = null;

    /** @type {Object<string, Array>} Cached scene data per video ID */
    const _sceneCache = {};

    // =========================================================================
    // PUBLIC API
    // =========================================================================

    return {
        /** @type {string} Domain name for transaction system */
        _name: 'videos',
        /** @type {Function} Notify function for transaction system */
        _notify: notify,

        /**
         * Subscribe to videos state changes.
         * @param {Function} callback - Called with event on changes
         * @returns {Function} Unsubscribe function
         */
        onChanged: subscribe,

        /**
         * Store video search results from the API.
         * @param {Array} results - Per-video result dicts with nested scenes
         * @param {string} query - The search query that produced these results
         */
        setSearchResults(results, query) {
            _searchResults = results;
            _query = query;
            _selectedVideoId = null;

            // Pre-populate scene cache from search results
            for (const vr of results) {
                if (vr.scenes) {
                    _sceneCache[vr.id] = vr.scenes;
                }
            }

            broadcast({ type: 'changed', property: 'searchResults' });
        },

        /**
         * Get current search results.
         * @returns {Array|null}
         */
        getSearchResults() {
            return _searchResults;
        },

        /**
         * Get the active search query.
         * @returns {string|null}
         */
        getQuery() {
            return _query;
        },

        /**
         * Whether we're in search mode (vs browse mode).
         * @returns {boolean}
         */
        isSearchMode() {
            return _searchResults !== null;
        },

        /**
         * Get the selected video ID.
         * @returns {string|null}
         */
        getSelectedVideoId() {
            return _selectedVideoId;
        },

        /**
         * Get the selected video's data.
         * @returns {Object|null}
         */
        getSelectedVideo() {
            if (!_selectedVideoId) return null;

            // Check search results first
            if (_searchResults) {
                return _searchResults.find(v => v.id === _selectedVideoId) || null;
            }

            // Browse mode: look up in AppState.images
            const img = AppState.images.getById(_selectedVideoId);
            return img || null;
        },

        /**
         * Select a video in the grid (triggers timeline render).
         * @param {string|null} videoId
         */
        selectVideo(videoId) {
            if (_selectedVideoId === videoId) return;
            _selectedVideoId = videoId;
            broadcast({ type: 'changed', property: 'selectedVideo' });
        },

        /**
         * Get cached scene data for a video.
         * @param {string} videoId
         * @returns {Array|null}
         */
        getScenes(videoId) {
            return _sceneCache[videoId] || null;
        },

        /**
         * Cache scene data for a video.
         * @param {string} videoId
         * @param {Array} scenes
         */
        setScenes(videoId, scenes) {
            _sceneCache[videoId] = scenes;
            if (_selectedVideoId === videoId) {
                broadcast({ type: 'changed', property: 'scenes' });
            }
        },

        /**
         * Invalidate cached scenes for a video so the next access
         * triggers a fresh load from the backend.  If the video is
         * currently selected, broadcasts a ``scenes`` change so the
         * timeline re-renders.
         *
         * @param {string} videoId
         */
        invalidateScenes(videoId) {
            if (_sceneCache[videoId]) {
                delete _sceneCache[videoId];
                if (_selectedVideoId === videoId) {
                    broadcast({ type: 'changed', property: 'scenes' });
                }
            }
        },

        /**
         * Load all videos for browse mode.
         * Filters AppState.images to videos only.
         */
        loadAll() {
            const all = AppState.images.getAll();
            _allVideos = all.filter(img => img.media_type === 'video');
            _searchResults = null;
            _query = null;
            broadcast({ type: 'changed', property: 'allVideos' });
        },

        /**
         * Get all videos (browse mode).
         * @returns {Array}
         */
        getAll() {
            if (_allVideos === null) {
                this.loadAll();
            }
            return _allVideos || [];
        },

        /**
         * Update the preferred scene for a video (two-phase optimistic).
         * @param {string} videoId
         * @param {string} sceneId
         * @returns {Promise<void>}
         */
        setPreferredScene(videoId, sceneId) {
            // PHASE 1: Synchronous optimistic update
            const img = AppState.images.getById(videoId);
            const prevSceneId = img?.preferred_scene_id;
            // Also update search result object (separate from images cache)
            const searchHit = _searchResults?.find(v => v.id === videoId);
            transaction(() => {
                if (img) {
                    img.preferred_scene_id = sceneId;
                }
                if (searchHit) {
                    searchHit.preferred_scene_id = sceneId;
                }
                broadcast({
                    type: 'changed', property: 'preferredScene',
                    imageId: videoId,
                });
            });

            // PHASE 2: Persist to backend (rollback on error)
            return queueTransaction(async () => {
                try {
                    await App.apiPut(`/images/${videoId}/preferred-scene`, {
                        scene_id: sceneId,
                    });
                } catch (err) {
                    console.error('[AppState.videos.setPreferredScene] Persist failed:', err);
                    transaction(() => {
                        if (img) {
                            img.preferred_scene_id = prevSceneId;
                        }
                        if (searchHit) {
                            searchHit.preferred_scene_id = prevSceneId;
                        }
                        broadcast({
                            type: 'changed', property: 'preferredScene',
                            imageId: videoId,
                        });
                    });
                    throw err;
                }
            });
        },

        /**
         * Update the STT language for a video (two-phase optimistic).
         *
         * Clears the scene cache for the video (transcriptions will be
         * re-fetched after the pipeline retranscribes) and persists the
         * change to the backend.
         *
         * @param {string} videoId
         * @param {string} language - ISO 639-1 code or '' for auto-detect
         * @returns {Promise<void>}
         */
        setSttLanguage(videoId, language) {
            // PHASE 1: Synchronous optimistic update
            const img = AppState.images.getById(videoId);
            const prevLanguage = img?.stt_language;
            // Snapshot current transcriptions for rollback
            const cachedScenes = _sceneCache[videoId];
            const prevTranscriptions = cachedScenes
                ? cachedScenes.map(s => ({ transcription: s.transcription, transcription_embedding: s.transcription_embedding }))
                : null;
            transaction(() => {
                if (img) {
                    img.stt_language = language;
                }
                // Null transcriptions in place — keeps timeline scenes visible
                // (the backend is nulling them too and will retranscribe)
                if (cachedScenes) {
                    for (const scene of cachedScenes) {
                        scene.transcription = null;
                        scene.transcription_embedding = null;
                    }
                }
                broadcast({
                    type: 'changed', property: 'sttLanguage',
                    imageId: videoId,
                });
            });

            // PHASE 2: Persist to backend (rollback on error)
            return queueTransaction(async () => {
                try {
                    await App.apiPut(`/images/${videoId}/stt-language`, {
                        language,
                    });
                } catch (err) {
                    console.error('[AppState.videos.setSttLanguage] Persist failed:', err);
                    transaction(() => {
                        if (img) {
                            img.stt_language = prevLanguage;
                        }
                        // Restore transcriptions
                        if (cachedScenes && prevTranscriptions) {
                            for (let i = 0; i < cachedScenes.length; i++) {
                                cachedScenes[i].transcription = prevTranscriptions[i].transcription;
                                cachedScenes[i].transcription_embedding = prevTranscriptions[i].transcription_embedding;
                            }
                        }
                        broadcast({
                            type: 'changed', property: 'sttLanguage',
                            imageId: videoId,
                        });
                    });
                    throw err;
                }
            });
        },

        /**
         * Update a scene's transcription text (two-phase optimistic).
         *
         * Immediately patches the cached scene object so the UI reflects
         * the edit, then persists to the backend.  Rolls back on error.
         *
         * @param {string} videoId - Parent video ID.
         * @param {string} sceneId - Scene UUID to update.
         * @param {string} transcription - New subtitle text ('' to clear).
         * @returns {Promise<void>}
         */
        updateSceneTranscription(videoId, sceneId, transcription) {
            // PHASE 1: Synchronous optimistic update
            const cachedScenes = _sceneCache[videoId];
            const scene = cachedScenes?.find(s => s.id === sceneId);
            const prevTranscription = scene?.transcription;
            if (scene) {
                transaction(() => {
                    scene.transcription = transcription;
                    broadcast({
                        type: 'changed', property: 'scenes',
                        imageId: videoId,
                    });
                });
            }

            // PHASE 2: Persist to backend (rollback on error)
            return queueTransaction(async () => {
                try {
                    await App.apiPut(`/scenes/${sceneId}/transcription`, {
                        transcription,
                    });
                } catch (err) {
                    console.error('[AppState.videos.updateSceneTranscription] Persist failed:', err);
                    if (scene) {
                        transaction(() => {
                            scene.transcription = prevTranscription;
                            broadcast({
                                type: 'changed', property: 'scenes',
                                imageId: videoId,
                            });
                        });
                    }
                    throw err;
                }
            });
        },

        /**
         * Queue videos for transcoding to browser-compatible MP4.
         * @param {string[]} videoIds - Video IDs to transcode.
         * @param {boolean} trashOriginal - Whether to trash the original after transcoding.
         * @returns {Promise<number>} Number of videos queued.
         */
        async requestTranscode(videoIds, trashOriginal = false) {
            try {
                const response = await App.apiPost('/videos/transcode', {
                    ids: videoIds,
                    trash_original: trashOriginal,
                });
                return response?.data?.queued || 0;
            } catch (err) {
                console.error('[AppState.videos.requestTranscode] Error:', err);
                throw err;
            }
        },

        /**
         * Exit search mode, preserving the selected video.
         *
         * Clears search results and query but keeps the selection so the
         * timeline stays visible.  Invalidates cached scenes for the
         * selected video so they are re-fetched without search scores
         * (removing heatmap overlays).
         */
        clearSearch() {
            _searchResults = null;
            _query = null;
            _allVideos = null;
            // Invalidate cached scenes so heatmap-scored data is replaced
            // by a clean fetch on the next render.
            if (_selectedVideoId && _sceneCache[_selectedVideoId]) {
                delete _sceneCache[_selectedVideoId];
            }
            broadcast({ type: 'changed', property: 'cleared' });
        },

        /**
         * Clear all videos state including selection.
         */
        clear() {
            _searchResults = null;
            _query = null;
            _selectedVideoId = null;
            _allVideos = null;
            // Clear all cached scenes
            for (const key of Object.keys(_sceneCache)) {
                delete _sceneCache[key];
            }
            broadcast({ type: 'changed', property: 'cleared' });
        },
    };
})();
