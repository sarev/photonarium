/**
 * @fileoverview Videos screen module for the Photonarium application.
 *
 * The Videos screen is the primary home for browsing and managing videos.
 * It features a top grid of video thumbnails (using VirtualGrid for
 * efficient rendering at scale) and a bottom timeline panel showing
 * scene keyframes with optional heatmap overlay for search results.
 *
 * LAYOUT:
 *   - Top panel: Video thumbnail grid (VirtualGrid with 16:9 aspect ratio)
 *   - Bottom panel: Scene timeline strip with proportional scene widths
 *
 * MODES:
 *   - Browse mode: Shows all videos, no heatmap overlay
 *   - Search mode: Shows filtered videos with per-scene score heatmap
 *
 * @module videos
 * @requires core
 * @requires thumbnails
 */

/* ==========================================================================
   MODULE SETUP & LIFECYCLE
   ========================================================================== */

'use strict';

/**
 * Videos screen module.
 * @namespace
 */
const Videos = {
    /** @type {Object} DOM element references. @private */
    _els: {},

    /** @type {VirtualGrid|null} Grid instance for video thumbnails. @private */
    _grid: null,

    /** @type {GridSelection|null} Selection handler. @private */
    _selection: null,

    /** @type {Function[]} Unsubscribe callbacks. @private */
    _unsubs: [],

    /**
     * Initialise the videos module.
     */
    init() {
        this._els = {
            gridSection: App.$('videos-grid-section'),
            grid: App.$('videos-grid'),
            timelineSection: App.$('videos-timeline-section'),
            timeline: App.$('videos-timeline'),
            timelineEmpty: App.$('videos-timeline-empty'),
        };

        // Create VirtualGrid with 16:9 aspect ratio cells
        this._grid = VirtualGrid.create({
            container: this._els.grid,
            getItems: () => this._getVideoList(),
            getItemId: (video) => video.id,
            createItem: (video, _index, blobUrl) => this._createVideoCard(video, blobUrl),
            getThumbnailId: (video) => video.id,
            getThumbSize: () => AppState.view.getVideoThumbnailSize(),
            getThumbnailUrl: (thumbId) => {
                // Use preferred scene thumbnail for videos
                const videos = this._getVideoList();
                const video = videos.find(v => v.id === thumbId);
                if (video?.preferred_scene_id) {
                    return `/api/scenes/${video.preferred_scene_id}/thumbnail`;
                }
                return App.thumbnailUrl(thumbId);
            },
            itemSelector: '.video-card',
            getItemHeight: (_thumbSize, itemWidth) => {
                // 16:9 thumbnail + label area (matching gallery pattern)
                const thumbnailHeight = Math.round(itemWidth * 9 / 16);
                const labelHeight = 24;
                return thumbnailHeight + labelHeight + 16; // 16 = padding
            },
            onItemCreated: (id, el) => {
                // Sync selection state when item is added to DOM
                if (this._selection && this._selection.isSelected(id)) {
                    el.classList.add('selected');
                }
            },
        });

        // Create GridSelection for click/ctrl/shift/drag selection
        this._selection = GridSelection.create({
            grid: this._grid,
            getItems: () => this._getVideoList(),
            getItemId: (video) => video.id,
            itemSelector: '.video-card',
            onSelectionChanged: (ids) => {
                // Feed selection into App so toolbar button states update
                App.setSelectedImages(ids);

                // Single selection: show timeline for the selected video
                if (ids.length === 1) {
                    AppState.videos.selectVideo(ids[0]);
                    this._loadScenesIfNeeded(ids[0]);
                } else if (ids.length === 0) {
                    AppState.videos.selectVideo(null);
                }
            },
            onItemActivated: (id) => {
                // Double-click: open in fullscreen
                const isSearch = AppState.videos.isSearchMode();
                const videos = this._getVideoList();
                const video = videos.find(v => v.id === id);
                const bestScene = isSearch ? video?.best_scene_id : null;
                const seekTo = bestScene
                    ? this._getSceneStartTime(id, bestScene)
                    : 0;
                App.showFullscreen(id, { seekTo });
            },
            onDeleteRequested: (ids) => {
                this._deleteVideos(ids);
            },
        });

        // Respond to toolbar events (select all, trash)
        App.on('selectAll', () => {
            if (AppState.nav.getScreen() === 'videos' && this._selection) {
                this._selection.selectAll();
            }
        });
        App.on('trashSelected', () => {
            if (AppState.nav.getScreen() === 'videos') {
                this._deleteVideos(App.getSelectedImages());
            }
        });
        App.on('clearSelection', () => {
            if (AppState.nav.getScreen() === 'videos' && this._selection) {
                this._selection.clear();
            }
        });

        // Subscribe to AppState.videos changes
        this._unsubs.push(AppState.videos.onChanged((event) => {
            if (event.property === 'searchResults' || event.property === 'allVideos'
                    || event.property === 'cleared') {
                // Only render if the Videos screen is active — rendering
                // into a hidden (zero-size) container produces empty layout
                if (AppState.nav.getScreen() === 'videos') {
                    this._refreshGrid();
                    // 'cleared' resets selectedVideoId to null — re-render
                    // the timeline so heatmap overlays are removed
                    if (event.property === 'cleared') {
                        this._renderTimeline();
                    }
                } else {
                    this._needsRefresh = true;
                }
            } else if (event.property === 'selectedVideo' || event.property === 'scenes') {
                this._renderTimeline();
            } else if (event.property === 'preferredScene') {
                this._renderTimeline();
                // Re-render grid to update preferred scene thumbnails
                this._refreshGrid();
            }
        }));

        // Subscribe to images changes (for browse mode updates)
        this._unsubs.push(AppState.images.onChanged((event) => {
            if (event.property === 'loaded' || event.type === 'delta') {
                if (!AppState.videos.isSearchMode()) {
                    AppState.videos.loadAll();
                }
            }
        }));

        // When the filter is cleared, exit search mode and return to browse
        this._unsubs.push(AppState.filter.onChanged(() => {
            if (!AppState.filter.isActive() && AppState.videos.isSearchMode()) {
                AppState.videos.clear();
                if (AppState.nav.getScreen() === 'videos') {
                    AppState.videos.loadAll();
                }
            }
        }));

        // Respond to video thumbnail size and sort changes
        this._unsubs.push(AppState.view.onChanged((event) => {
            if (AppState.nav.getScreen() !== 'videos') {
                if (event.property === 'sortBy' || event.property === 'sortDirection') {
                    this._needsRefresh = true;
                }
                return;
            }
            if (event.property === 'videoThumbnailSize' && this._grid) {
                this._grid.render();
            } else if (event.property === 'sortBy' || event.property === 'sortDirection') {
                this._refreshGrid();
            }
        }));
    },

    /**
     * Called when entering the videos screen.
     */
    onEnter() {
        // If there's an active text filter from a non-video search (e.g.
        // "all" mode), automatically execute a video search so the user
        // sees heatmaps and score badges rather than an unfiltered list.
        if (!AppState.videos.isSearchMode() && AppState.filter.isActive()) {
            const filter = AppState.filter.get();
            if (filter?.text && filter.searchMode !== 'videos') {
                this._applyFilterToVideos(filter);
                return;
            }
        }

        // In browse mode (no search), load all videos
        if (!AppState.videos.isSearchMode()) {
            AppState.videos.loadAll();
        }

        // Always do a full refresh on enter — the grid may have been
        // rendered while the screen was hidden (zero-size container),
        // which produces an empty layout.
        this._refreshGrid();
        this._renderTimeline();
    },

    /**
     * Execute a video search using an existing filter's text query.
     * Called when the user navigates to Videos while a non-video text
     * filter is active (e.g. from an "all" mode search on the Gallery).
     * @param {Object} filter - The active filter object (must have .text)
     * @private
     */
    async _applyFilterToVideos(filter) {
        const threshold = filter.threshold || 0.25;
        AppState.loading.show('video-search', 'Searching videos\u2026');
        try {
            const response = await App.apiPost('/search/videos', {
                query: filter.text,
                threshold,
                limit: 200,
            });
            if (response?.data?.results) {
                AppState.videos.setSearchResults(response.data.results, filter.text);
            }
        } catch (error) {
            console.error('Video search failed:', error);
            // Fall back to showing all videos without search
            AppState.videos.loadAll();
        } finally {
            AppState.loading.hide('video-search');
        }
        this._refreshGrid();
        this._renderTimeline();
    },

    /**
     * Called when leaving the videos screen.
     */
    onLeave() {
        if (this._selection) this._selection.unbind();
        if (this._grid) this._grid.unbind();
    },

    // =========================================================================
    // GRID
    // =========================================================================

    /**
     * Get the current video list, sorted according to view settings.
     * In search mode with content sort, uses match score; otherwise
     * applies the same sort field/direction as Gallery.
     * @returns {Array}
     * @private
     */
    _getVideoList() {
        const isSearch = AppState.videos.isSearchMode();
        const list = (isSearch
            ? AppState.videos.getSearchResults()
            : AppState.videos.getAll()) || [];

        const { by, direction } = AppState.view.getSort();
        const dir = direction === 'asc' ? 1 : -1;

        const sorted = [...list].sort((a, b) => {
            let cmp = 0;
            switch (by) {
                case 'content':
                    // In search mode, sort by match score; browse mode has no scores
                    cmp = (a.combined_score || 0) - (b.combined_score || 0);
                    break;
                case 'rating':
                    cmp = (a.rating || '').localeCompare(b.rating || '');
                    break;
                case 'quality':
                    cmp = (a.aesthetic_nima || 0) - (b.aesthetic_nima || 0);
                    break;
                case 'date':
                default:
                    cmp = (a.timestamp || '').localeCompare(b.timestamp || '');
                    break;
            }
            return cmp * dir;
        });

        return sorted;
    },

    /**
     * Refresh the VirtualGrid (re-render with current data).
     * Follows the same bind/unbind pattern as Gallery._loadImages().
     * @private
     */
    _refreshGrid() {
        if (!this._grid) return;

        const videos = this._getVideoList();

        if (!videos || videos.length === 0) {
            // Empty state: unbind grid/selection to clean up listeners + blobs
            if (this._selection) this._selection.unbind();
            if (this._grid) this._grid.unbind();
            ThumbnailLoader.clear();
            const container = this._els.grid;
            if (container) {
                const isSearch = AppState.videos.isSearchMode();
                const msg = isSearch
                    ? 'No matching videos found'
                    : AppState.filter.isActive()
                        ? 'No videos match the current filter'
                        : 'No videos in library';
                container.innerHTML = `<div class="empty-state">${App.icon('videocam_off', '\u{1F4F7}')}<p>${msg}</p></div>`;
            }
            this._setTimelineVisible(false);
            return;
        }

        this._setTimelineVisible(true);

        // Clear thumbnail loader and re-render the virtual grid
        ThumbnailLoader.clear();
        this._grid.render();
        // render() calls bind() on the grid internally — don't double-bind

        // Bind selection handlers
        if (this._selection) this._selection.bind();

        this._needsRefresh = false;
    },

    /**
     * Create a video card element for the VirtualGrid.
     * Called by VirtualGrid when a thumbnail blob URL is ready.
     * @param {Object} video - Video data (search result or image record)
     * @param {string} blobUrl - Blob URL for the thumbnail image
     * @returns {HTMLElement}
     * @private
     */
    _createVideoCard(video, blobUrl) {
        const isSearch = AppState.videos.isSearchMode();
        const card = App.createElement('div', { className: 'video-card loaded', dataId: video.id });

        // Thumbnail image (blob URL managed by VirtualGrid/ThumbnailLoader)
        const img = App.createElement('img', {
            src: blobUrl,
            alt: video.basename || '',
            title: video.path || video.basename || '',
        });
        card.appendChild(img);

        // Play overlay (hover-reveal)
        const playOverlay = App.createElement('div', { className: 'video-play-overlay' });
        playOverlay.innerHTML = App.icon('play_arrow', '\u25B6');
        card.appendChild(playOverlay);

        // Duration badge
        const duration = video.duration || 0;
        if (duration > 0) {
            const badge = App.createElement('span', { className: 'video-duration-badge' });
            const mins = Math.floor(duration / 60);
            const secs = Math.floor(duration % 60);
            badge.textContent = mins + ':' + String(secs).padStart(2, '0');
            card.appendChild(badge);
        }

        // Score badge (search mode only)
        if (isSearch && video.normalised_score != null) {
            const scoreBadge = App.createElement('span', { className: 'video-score-badge' });
            scoreBadge.textContent = Math.round(video.normalised_score * 100) + '%';
            card.appendChild(scoreBadge);
        }

        // Basename label
        const label = App.createElement('span', { className: 'video-card-label' }, video.basename || '');
        card.appendChild(label);

        return card;
    },

    /**
     * Load scenes for a video if not already cached.
     * @param {string} videoId
     * @private
     */
    async _loadScenesIfNeeded(videoId) {
        if (AppState.videos.getScenes(videoId)) return;

        try {
            const query = AppState.videos.getQuery();
            const url = `/images/${videoId}/scenes` + (query ? `?query=${encodeURIComponent(query)}` : '');
            const response = await App.apiGet(url);
            if (response?.data) {
                AppState.videos.setScenes(videoId, response.data);
            }
        } catch (err) {
            console.error('Failed to load scenes:', err);
        }
    },

    // =========================================================================
    // TIMELINE RENDERING
    // =========================================================================

    /**
     * Render the scene timeline for the selected video.
     * @private
     */
    _renderTimeline() {
        const container = this._els.timeline;
        if (!container) return;

        const videoId = AppState.videos.getSelectedVideoId();
        if (!videoId) {
            container.innerHTML = '';
            if (this._els.timelineEmpty) {
                this._els.timelineEmpty.hidden = false;
            }
            return;
        }

        if (this._els.timelineEmpty) {
            this._els.timelineEmpty.hidden = true;
        }

        const scenes = AppState.videos.getScenes(videoId);
        if (!scenes || scenes.length === 0) {
            container.innerHTML = '<div class="empty-state">Loading scenes...</div>';
            return;
        }

        const isSearch = AppState.videos.isSearchMode();
        const video = AppState.videos.getSelectedVideo();
        const preferredSceneId = video?.preferred_scene_id;

        container.innerHTML = '';

        // Wrapper with scroll indicators
        const wrapper = App.createElement('div', { className: 'timeline-track-wrapper' });
        const indicatorLeft = App.createElement('div', { className: 'scroll-indicator left' });
        const indicatorRight = App.createElement('div', { className: 'scroll-indicator right' });
        wrapper.appendChild(indicatorLeft);
        wrapper.appendChild(indicatorRight);

        // Timeline track (horizontal strip, scrollable when scenes overflow)
        const track = App.createElement('div', { className: 'timeline-track' });

        // Calculate total duration for proportional widths.
        // Each scene gets a proportional share but is clamped to a minimum
        // pixel width (80px via CSS min-width). When scenes would be
        // narrower than that, the track overflows and scrolls.
        const totalDuration = scenes.reduce((sum, s) => sum + (s.end_time - s.start_time), 0);

        for (const scene of scenes) {
            const sceneDuration = scene.end_time - scene.start_time;
            const widthPct = totalDuration > 0 ? (sceneDuration / totalDuration * 100) : (100 / scenes.length);

            const sceneEl = App.createElement('div', { className: 'timeline-scene' });
            sceneEl.style.width = widthPct + '%';
            sceneEl.dataset.sceneId = scene.scene_id || scene.id;

            // Scene thumbnail — tiled horizontally so portrait frames
            // repeat to fill wide scenes instead of being cropped
            const thumbId = scene.scene_id || scene.id;
            const thumbDiv = App.createElement('div', { className: 'timeline-scene-thumb' });
            const thumbUrl = `/api/scenes/${thumbId}/thumbnail?size=200`;
            thumbDiv.style.backgroundImage = `url(${thumbUrl})`;
            sceneEl.appendChild(thumbDiv);

            // Heatmap overlay (search mode only)
            if (isSearch && scene.normalised_score != null) {
                const overlay = App.createElement('div', { className: 'timeline-heatmap' });
                overlay.style.backgroundColor = this._scoreToColor(scene.normalised_score);
                sceneEl.appendChild(overlay);
            }

            // Timecode label
            const timecode = App.createElement('span', { className: 'timeline-timecode' });
            timecode.textContent = this._formatTime(scene.start_time);
            sceneEl.appendChild(timecode);

            // Preferred scene star (same icon/style as face preferred picker)
            const isPreferred = thumbId === preferredSceneId;
            const star = App.createElement('button', {
                className: 'timeline-star' + (isPreferred ? ' preferred' : ''),
            });
            star.innerHTML = App.icon('star', '\u2605');
            star.title = 'Set as preferred scene';
            star.addEventListener('click', (e) => {
                e.stopPropagation();
                AppState.videos.setPreferredScene(videoId, thumbId);
            });
            sceneEl.appendChild(star);

            // Click handlers
            sceneEl.addEventListener('click', () => {
                // Highlight selected scene
                track.querySelectorAll('.timeline-scene.selected').forEach(
                    s => s.classList.remove('selected'),
                );
                sceneEl.classList.add('selected');
            });

            sceneEl.addEventListener('dblclick', () => {
                App.showFullscreen(videoId, { seekTo: scene.start_time });
            });

            track.appendChild(sceneEl);
        }

        wrapper.appendChild(track);
        container.appendChild(wrapper);

        // Update scroll indicators on scroll and after layout
        const updateIndicators = () => {
            const scrollLeft = track.scrollLeft;
            const maxScroll = track.scrollWidth - track.clientWidth;
            indicatorLeft.classList.toggle('visible', scrollLeft > 4);
            indicatorRight.classList.toggle('visible', maxScroll - scrollLeft > 4);
        };
        track.addEventListener('scroll', updateIndicators, { passive: true });
        // Initial check after layout settles
        requestAnimationFrame(updateIndicators);

        // Transcription text below timeline
        const hasTranscriptions = scenes.some(s => s.transcription);
        if (hasTranscriptions) {
            const transcripts = App.createElement('div', { className: 'timeline-transcripts' });
            for (const scene of scenes) {
                if (!scene.transcription) continue;
                const entry = App.createElement('div', { className: 'timeline-transcript-entry' });
                const time = App.createElement('span', { className: 'timeline-transcript-time' });
                time.textContent = this._formatTime(scene.start_time);
                const text = App.createElement('span', { className: 'timeline-transcript-text' });
                text.textContent = scene.transcription;
                entry.appendChild(time);
                entry.appendChild(text);
                transcripts.appendChild(entry);
            }
            container.appendChild(transcripts);
        }
    },

    /**
     * Show or hide the timeline section and divider.
     * When hidden, the grid section fills the full screen.
     * @param {boolean} visible
     * @private
     */
    _setTimelineVisible(visible) {
        if (this._els.timelineSection) {
            this._els.timelineSection.hidden = !visible;
        }
        // The divider is a sibling between grid and timeline sections
        const divider = this._els.gridSection?.nextElementSibling;
        if (divider?.classList.contains('videos-divider')) {
            divider.hidden = !visible;
        }
    },

    // =========================================================================
    // DELETE
    // =========================================================================

    /**
     * Move selected videos to trash.
     * Delegates to the same backend as Gallery image deletion.
     * @param {string[]} ids - Video image IDs to delete
     * @private
     */
    async _deleteVideos(ids) {
        if (!ids || ids.length === 0) return;

        if (!AppState.status.isTrashEnabled()) {
            App.showError(
                'Cannot delete: trash directory is misconfigured. '
                + 'Check that it does not overlap an indexed folder.',
            );
            return;
        }

        const count = ids.length;
        const noun = count === 1 ? 'video' : 'videos';
        const confirmed = await App.confirm(
            'Move to Trash',
            `Move ${count === 1 ? 'this video' : count + ' videos'} to the trash?`,
            { okText: 'Move to Trash' },
        );
        if (!confirmed) return;

        App.showInfo(`Moving ${count} ${noun} to \u2018trash\u2019\u2026`);

        try {
            await App.apiPost('/images/trash', { ids });
            AppState.images.removeMany(ids);
            if (this._selection) this._selection.clear();
            AppState.videos.selectVideo(null);
            if (!AppState.videos.isSearchMode()) {
                AppState.videos.loadAll();
            }
        } catch (err) {
            App.showError(`Failed to move ${noun} to trash: ${err.message}`);
        }
    },

    // =========================================================================
    // HELPERS
    // =========================================================================

    /**
     * Convert a normalised score (0-1) to a heatmap colour.
     * 0 = transparent, 0-0.5 = blue->yellow, 0.5-1.0 = yellow->red.
     * @param {number} score
     * @returns {string} CSS color with opacity
     * @private
     */
    _scoreToColor(score) {
        if (score <= 0) return 'transparent';

        let r, g, b;
        if (score <= 0.5) {
            // Blue (0,100,255) -> Yellow (255,220,0)
            const t = score / 0.5;
            r = Math.round(t * 255);
            g = Math.round(100 + t * 120);
            b = Math.round(255 * (1 - t));
        } else {
            // Yellow (255,220,0) -> Red (255,50,0)
            const t = (score - 0.5) / 0.5;
            r = 255;
            g = Math.round(220 * (1 - t) + 50 * t);
            b = 0;
        }
        return `rgba(${r}, ${g}, ${b}, 0.5)`;
    },

    /**
     * Format seconds as m:ss timecode.
     * @param {number} seconds
     * @returns {string}
     * @private
     */
    _formatTime(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return m + ':' + String(s).padStart(2, '0');
    },

    /**
     * Get the start time of a scene by ID.
     * @param {string} videoId
     * @param {string} sceneId
     * @returns {number} Start time in seconds, or 0 if not found
     * @private
     */
    _getSceneStartTime(videoId, sceneId) {
        const scenes = AppState.videos.getScenes(videoId);
        if (!scenes) return 0;
        const scene = scenes.find(s => (s.scene_id || s.id) === sceneId);
        return scene ? scene.start_time : 0;
    },
};

// Register with the app
App.registerModule('videos', Videos);
