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
            console.log('[TIMELINE DEBUG] videos changed:', event.property, 'screen:', AppState.nav.getScreen(), 'selectedId:', AppState.videos.getSelectedVideoId());
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
            console.log('[TIMELINE DEBUG] filter changed, active:', AppState.filter.isActive(), 'isSearch:', AppState.videos.isSearchMode(), 'screen:', AppState.nav.getScreen());
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
        console.log('[TIMELINE DEBUG] onEnter, isSearch:', AppState.videos.isSearchMode(), 'filterActive:', AppState.filter.isActive(), 'selectedId:', AppState.videos.getSelectedVideoId());
        // If there's an active text filter from a non-video search (e.g.
        // "all" mode), automatically execute a video search so the user
        // sees heatmaps and score badges rather than an unfiltered list.
        if (!AppState.videos.isSearchMode() && AppState.filter.isActive()) {
            const filter = AppState.filter.get();
            if (filter?.text && filter.searchMode !== 'videos') {
                console.log('[TIMELINE DEBUG] onEnter: applying filter to videos');
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

        // If a video is already selected, ensure its scenes are loaded
        // (selection callback won't fire since the selection hasn't changed)
        const selectedId = AppState.videos.getSelectedVideoId();
        if (selectedId) {
            console.log('[TIMELINE DEBUG] onEnter: loading scenes for', selectedId);
            this._loadScenesIfNeeded(selectedId);
        } else {
            // Auto-select the first video if none is selected (e.g. after
            // arriving from search screen where results were set off-screen,
            // or after clear() reset selectedVideoId while GridSelection
            // retained its previous selection state)
            const videos = this._getVideoList();
            if (videos?.length > 0) {
                const firstId = videos[0].id;
                console.log('[TIMELINE DEBUG] onEnter: auto-selecting first video', firstId);
                AppState.videos.selectVideo(firstId);
                this._loadScenesIfNeeded(firstId);
                if (this._selection) {
                    this._selection.select(firstId);
                }
            }
        }
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
        console.log('[TIMELINE DEBUG] _renderTimeline, videoId:', videoId);
        if (!videoId) {
            container.innerHTML = '';
            if (this._els.timelineEmpty) {
                this._els.timelineEmpty.hidden = false;
            }
            console.log('[TIMELINE DEBUG] _renderTimeline: no videoId, cleared');
            return;
        }

        if (this._els.timelineEmpty) {
            this._els.timelineEmpty.hidden = true;
        }

        const scenes = AppState.videos.getScenes(videoId);
        if (!scenes || scenes.length === 0) {
            container.innerHTML = '<div class="empty-state">Loading scenes...</div>';
            console.log('[TIMELINE DEBUG] _renderTimeline: no scenes for', videoId, '(showing loading)');
            return;
        }
        console.log('[TIMELINE DEBUG] _renderTimeline: rendering', scenes.length, 'scenes for', videoId);

        const isSearch = AppState.videos.isSearchMode();
        const video = AppState.videos.getSelectedVideo();
        const preferredSceneId = video?.preferred_scene_id;

        // Preserve scroll position across re-renders (e.g. preferred scene change)
        const prevTrack = container.querySelector('.timeline-track');
        const savedScrollLeft = prevTrack ? prevTrack.scrollLeft : 0;

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
                App.showFullscreen(videoId, { seekTo: scene.start_time, autoplay: true });
            });

            track.appendChild(sceneEl);
        }

        wrapper.appendChild(track);
        container.appendChild(wrapper);

        // Drag-to-scroll on the timeline track
        this._initTrackDrag(track);

        // Minimap below the track
        const minimap = this._buildMinimap(scenes, track, isSearch, totalDuration);
        container.appendChild(minimap);

        // Update scroll indicators on scroll and after layout
        const updateIndicators = () => {
            const scrollLeft = track.scrollLeft;
            const maxScroll = track.scrollWidth - track.clientWidth;
            indicatorLeft.classList.toggle('visible', scrollLeft > 4);
            indicatorRight.classList.toggle('visible', maxScroll - scrollLeft > 4);
        };

        // Sync minimap viewport on scroll (RAF-throttled)
        let rafPending = false;
        track.addEventListener('scroll', () => {
            updateIndicators();
            if (!rafPending) {
                rafPending = true;
                requestAnimationFrame(() => {
                    rafPending = false;
                    this._syncMinimapViewport(minimap, track);
                });
            }
        }, { passive: true });

        // Initial layout check — show/hide minimap, restore scroll position
        requestAnimationFrame(() => {
            if (savedScrollLeft > 0) {
                track.scrollLeft = savedScrollLeft;
            }
            updateIndicators();
            const overflows = track.scrollWidth > track.clientWidth + 4;
            minimap.classList.toggle('hidden', !overflows);
            this._syncMinimapViewport(minimap, track);
        });

        // Re-check on resize (window resize or divider drag)
        const ro = new ResizeObserver(() => {
            const overflows = track.scrollWidth > track.clientWidth + 4;
            minimap.classList.toggle('hidden', !overflows);
            this._syncMinimapViewport(minimap, track);
            updateIndicators();
        });
        ro.observe(track);

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
    // TIMELINE MINIMAP & DRAG-TO-SCROLL
    // =========================================================================

    /**
     * Enable click-and-drag horizontal scrolling on the timeline track.
     * Suppresses click events after a real drag to avoid selecting scenes.
     * @param {HTMLElement} track
     * @private
     */
    _initTrackDrag(track) {
        let startX = 0;
        let startScrollLeft = 0;
        let isDragging = false;

        // Prevent native drag on scene thumbnails (background-image divs)
        // which can swallow click/dblclick events in some browsers
        track.addEventListener('dragstart', (e) => e.preventDefault());

        track.addEventListener('mousedown', (e) => {
            // Always reset drag state so the capture click handler
            // doesn't suppress clicks from a previous drag
            isDragging = false;
            // Don't hijack clicks on interactive elements (stars, buttons)
            if (e.target.closest('button, a, input')) return;
            startX = e.clientX;
            startScrollLeft = track.scrollLeft;

            const onMove = (/** @type {MouseEvent} */ ev) => {
                const dx = ev.clientX - startX;
                if (!isDragging && Math.abs(dx) > 3) {
                    isDragging = true;
                    track.classList.add('dragging');
                }
                if (isDragging) {
                    track.scrollLeft = startScrollLeft - dx;
                }
            };
            const onUp = () => {
                track.classList.remove('dragging');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });

        // Suppress click after a real drag (threshold exceeded).
        // Uses the shared isDragging flag which is reset on next mousedown.
        track.addEventListener('click', (e) => {
            if (isDragging) {
                e.stopPropagation();
            }
        }, { capture: true });
    },

    /**
     * Build the minimap bar element for the timeline.
     * Shows time ticks, a draggable viewport indicator, and an optional
     * heatmap gradient when in search mode.
     * @param {Array} scenes - Scene list
     * @param {HTMLElement} track - The timeline track element
     * @param {boolean} isSearch - Whether search mode is active
     * @param {number} totalDuration - Total video duration in seconds
     * @returns {HTMLElement} The minimap container
     * @private
     */
    _buildMinimap(scenes, track, isSearch, totalDuration) {
        const minimap = App.createElement('div', { className: 'timeline-minimap' });

        // Heatmap gradient layer (search mode only)
        if (isSearch) {
            const heatmap = App.createElement('div', { className: 'timeline-minimap-heatmap' });
            heatmap.style.background = this._buildHeatmapGradient(scenes, totalDuration);
            minimap.appendChild(heatmap);
        }

        // Time ticks
        const ticks = this._computeTickIntervals(totalDuration);
        for (const tickTime of ticks) {
            const pct = (tickTime / totalDuration) * 100;
            const tickEl = App.createElement('div', { className: 'timeline-minimap-tick' });
            tickEl.style.left = pct + '%';
            const label = App.createElement('span', { className: 'timeline-minimap-tick-label' });
            label.textContent = this._formatTime(tickTime);
            tickEl.appendChild(label);
            minimap.appendChild(tickEl);
        }

        // Draggable viewport indicator
        const viewport = App.createElement('div', { className: 'timeline-minimap-viewport' });
        minimap.appendChild(viewport);
        minimap._viewport = viewport;

        this._initMinimapDrag(minimap, viewport, track);

        return minimap;
    },

    /**
     * Wire drag interactions on the minimap: viewport drag and
     * background click-to-jump.
     * @param {HTMLElement} minimap
     * @param {HTMLElement} viewport
     * @param {HTMLElement} track
     * @private
     */
    _initMinimapDrag(minimap, viewport, track) {
        let dragging = false;
        let startX = 0;
        let startScrollLeft = 0;

        // Viewport drag — proportionally scrolls the track
        viewport.addEventListener('mousedown', (e) => {
            dragging = true;
            startX = e.clientX;
            startScrollLeft = track.scrollLeft;
            viewport.classList.add('dragging');
            e.preventDefault();
            e.stopPropagation();

            const onMove = (/** @type {MouseEvent} */ ev) => {
                if (!dragging) return;
                const minimapWidth = minimap.clientWidth;
                const dx = ev.clientX - startX;
                const scrollDelta = (dx / minimapWidth) * track.scrollWidth;
                track.scrollLeft = startScrollLeft + scrollDelta;
            };
            const onUp = () => {
                dragging = false;
                viewport.classList.remove('dragging');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });

        // Click on minimap background — jump viewport center to click position
        minimap.addEventListener('mousedown', (e) => {
            if (e.target === viewport || dragging) return;
            const rect = minimap.getBoundingClientRect();
            const clickPct = (e.clientX - rect.left) / rect.width;
            // Center the viewport around the click
            const targetScroll = clickPct * track.scrollWidth - track.clientWidth / 2;
            track.scrollLeft = Math.max(0, targetScroll);

            // Start dragging from this position
            dragging = true;
            startX = e.clientX;
            startScrollLeft = track.scrollLeft;
            viewport.classList.add('dragging');
            e.preventDefault();

            const onMove = (/** @type {MouseEvent} */ ev) => {
                if (!dragging) return;
                const minimapWidth = minimap.clientWidth;
                const dx = ev.clientX - startX;
                const scrollDelta = (dx / minimapWidth) * track.scrollWidth;
                track.scrollLeft = startScrollLeft + scrollDelta;
            };
            const onUp = () => {
                dragging = false;
                viewport.classList.remove('dragging');
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
            };
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        });
    },

    /**
     * Update the minimap viewport position and width to reflect the
     * current scroll state of the track.
     * @param {HTMLElement} minimap
     * @param {HTMLElement} track
     * @private
     */
    _syncMinimapViewport(minimap, track) {
        const viewport = minimap._viewport;
        if (!viewport) return;
        const sw = track.scrollWidth;
        if (sw <= 0) return;
        viewport.style.width = (track.clientWidth / sw * 100) + '%';
        viewport.style.left = (track.scrollLeft / sw * 100) + '%';
    },

    /**
     * Build a CSS linear-gradient string representing the search score
     * heatmap across the full video duration.
     * @param {Array} scenes - Scene list with normalised_score
     * @param {number} totalDuration - Total video duration in seconds
     * @returns {string} CSS gradient value
     * @private
     */
    _buildHeatmapGradient(scenes, totalDuration) {
        if (!totalDuration || scenes.length === 0) return 'transparent';
        const stops = [];
        for (const scene of scenes) {
            const midTime = (scene.start_time + scene.end_time) / 2;
            const pct = (midTime / totalDuration * 100).toFixed(2);
            const color = this._scoreToColor(scene.normalised_score ?? 0);
            stops.push(`${color} ${pct}%`);
        }
        return `linear-gradient(to right, ${stops.join(', ')})`;
    },

    /**
     * Compute "nice" tick intervals for the minimap time axis.
     * Aims for roughly 10 ticks across the duration. Returns an array
     * of tick positions in seconds (excludes 0 and end).
     * @param {number} totalDuration - Total duration in seconds
     * @returns {number[]}
     * @private
     */
    _computeTickIntervals(totalDuration) {
        if (totalDuration <= 0) return [];
        const intervals = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
        const targetTicks = 10;
        let best = intervals[0];
        for (const iv of intervals) {
            if (totalDuration / iv >= targetTicks * 0.4) best = iv;
        }
        const ticks = [];
        for (let t = best; t < totalDuration; t += best) {
            ticks.push(t);
        }
        return ticks;
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
