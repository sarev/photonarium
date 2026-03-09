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

    /** @type {Function|null} Unsubscribe for fullscreen nav tracking. @private */
    _fullscreenUnsub: null,

    /** @type {HTMLElement|null} Scene preview popup singleton. @private */
    _previewPopup: null,

    /** @type {HTMLImageElement|null} Preview popup image element. @private */
    _previewImg: null,

    /** @type {HTMLElement|null} Preview popup score badge. @private */
    _previewScore: null,

    /** @type {number|null} Long-press timer for touch preview. @private */
    _previewTimer: null,

    /** @type {boolean} Flag to suppress click after long-press preview. @private */
    _previewShown: false,

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
            languageSelect: App.$('vid-stt-language'),
            languageControl: document.querySelector('.vid-language-control'),
            languageSeparator: document.querySelector('.vid-language-separator'),
        };

        // Populate language dropdown from config
        this._populateLanguageDropdown();

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
                this._updateContentSortButton(ids);
                this._updateLanguageDropdown(ids);

                // Single selection: show timeline for the selected video
                if (ids.length === 1) {
                    AppState.videos.selectVideo(ids[0]);
                    this._loadScenesIfNeeded(ids[0]);
                } else {
                    AppState.videos.selectVideo(null);
                }
            },
            onItemActivated: (id) => {
                this._openFullscreen(id);
            },
            onDeleteRequested: (ids) => {
                this._deleteVideos(ids);
            },
        });

        // Language dropdown change handler
        if (this._els.languageSelect) {
            this._els.languageSelect.addEventListener('change', () => {
                const videoId = AppState.videos.getSelectedVideoId();
                if (videoId) {
                    AppState.videos.setSttLanguage(videoId, this._els.languageSelect.value);
                }
            });
        }

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
        App.on('fullscreenSelected', () => {
            if (AppState.nav.getScreen() === 'videos') {
                const sel = App.getSelectedImages();
                if (sel.length === 1) this._openFullscreen(sel[0]);
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
            } else if (event.property === 'selectedVideo') {
                this._renderTimeline();
                this._updateLanguageDropdown();
            } else if (event.property === 'scenes') {
                // Scenes invalidated (e.g. after retranscription) — reload
                // from backend if the cache is now empty for the selected video
                const selId = AppState.videos.getSelectedVideoId();
                if (selId && !AppState.videos.getScenes(selId)) {
                    this._loadScenesIfNeeded(selId);
                }
                this._renderTimeline();
                this._updateLanguageDropdown();
            } else if (event.property === 'sttLanguage') {
                this._updateLanguageDropdown();
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
            } else if (event.property === 'sortBy') {
                const { by } = AppState.view.getSort();
                if (by === 'content' && !AppState.videos.isSearchMode()) {
                    this._loadContentSimilarities();
                    return;
                }
                this._refreshGrid();
            } else if (event.property === 'sortDirection') {
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

        const videos = this._getVideoList();

        // 1. Fullscreen return — if the user navigated through videos in
        //    fullscreen, select the last-viewed video (mirrors gallery.js
        //    pattern at line 456).
        const lastViewedId = AppState.nav.consumeLastViewedImageId();
        if (lastViewedId && videos?.some(v => v.id === lastViewedId)) {
            AppState.videos.selectVideo(lastViewedId);
            this._loadScenesIfNeeded(lastViewedId);
            if (this._selection) this._selection.select(lastViewedId);
            this._renderTimeline();
            this._grid.scrollToId(lastViewedId, 'instant');
            return;
        }

        // 2. Cross-screen selection sync — if the user selected a video on
        //    Gallery and switched to Videos, pick it up from the shared
        //    'gallery' selection context.
        const gallerySelection = AppState.selection.get('gallery');
        if (gallerySelection.length > 0 && videos?.length > 0) {
            const videoIds = new Set(videos.map(v => v.id));
            const match = gallerySelection.find(id => videoIds.has(id));
            if (match) {
                AppState.videos.selectVideo(match);
                this._loadScenesIfNeeded(match);
                if (this._selection) this._selection.select(match);
                this._renderTimeline();
                this._grid.scrollToId(match, 'instant');
                return;
            }
        }

        // 3. Fallback — keep existing selection or auto-select first video.
        const selectedId = AppState.videos.getSelectedVideoId();
        if (selectedId) {
            this._loadScenesIfNeeded(selectedId);
        } else {
            if (videos?.length > 0) {
                const firstId = videos[0].id;
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
        this._hidePreview();
        if (this._selection) this._selection.unbind();
        if (this._grid) this._grid.unbind();
        if (this._fullscreenUnsub) {
            this._fullscreenUnsub();
            this._fullscreenUnsub = null;
        }
    },

    // =========================================================================
    // FULLSCREEN
    // =========================================================================

    /**
     * Open the fullscreen viewer scoped to the current video list.
     * Passes the video list as the navigation context so prev/next only
     * iterates videos visible on this screen. Subscribes to fullscreen
     * nav events to track selection changes and handle close — mirroring
     * the Gallery pattern (gallery.js _openFullscreen).
     * @param {string} id - Video ID to open
     * @private
     */
    _openFullscreen(id) {
        // Clean up any lingering subscription
        if (this._fullscreenUnsub) {
            this._fullscreenUnsub();
            this._fullscreenUnsub = null;
        }

        // Subscribe to fullscreen navigation events
        this._fullscreenUnsub = AppState.nav.onChanged((event) => {
            if (event.property === 'fullscreenImageId') {
                // Fullscreen navigated to a new video — update selection
                const newId = AppState.nav.getFullscreenImageId();
                if (newId && this._selection) {
                    this._selection.select(newId);
                    // Update video-specific state (timeline, scenes)
                    AppState.videos.selectVideo(newId);
                    this._loadScenesIfNeeded(newId);
                    this._renderTimeline();
                }
            } else if (event.property === 'fullscreenClosing') {
                // Consume lastViewedImageId so onEnter doesn't re-apply
                AppState.nav.consumeLastViewedImageId();

                // Scroll to the last-viewed video
                if (event.imageId && this._grid) {
                    this._grid.scrollToId(event.imageId, 'instant');
                }

                // Unsubscribe
                if (this._fullscreenUnsub) {
                    this._fullscreenUnsub();
                    this._fullscreenUnsub = null;
                }
            }
        });

        // Build fullscreen options with video-scoped navigation list
        const isSearch = AppState.videos.isSearchMode();
        const videos = this._getVideoList();
        const video = videos.find(v => v.id === id);
        const bestScene = isSearch ? video?.best_scene_id : null;
        const seekTo = bestScene
            ? this._getSceneStartTime(id, bestScene)
            : 0;

        // Select only the target video before opening
        if (this._selection) {
            this._selection.select(id);
        }

        App.showFullscreen(id, { imageList: videos, seekTo });
    },

    // =========================================================================
    // SORTING
    // =========================================================================

    /**
     * Enable/disable the "Sort by content similarity" button based on
     * whether exactly one video is selected.
     * @param {string[]} selection - Currently selected video IDs
     * @private
     */
    _updateContentSortButton(selection) {
        const btn = document.getElementById('btn-vid-sort-content');
        if (btn) btn.disabled = selection.length !== 1;
    },

    // =========================================================================
    // LANGUAGE DROPDOWN
    // =========================================================================

    /**
     * Language code to display name map (ISO 639-1 → English name).
     * Covers all Whisper-supported languages; only codes present in
     * App.config.stt_languages are shown in the dropdown.
     * @private
     */
    _LANGUAGE_NAMES: {
        af: 'Afrikaans', am: 'Amharic', ar: 'Arabic', as: 'Assamese',
        az: 'Azerbaijani', ba: 'Bashkir', be: 'Belarusian', bg: 'Bulgarian',
        bn: 'Bengali', bo: 'Tibetan', br: 'Breton', bs: 'Bosnian',
        ca: 'Catalan', cs: 'Czech', cy: 'Welsh', da: 'Danish',
        de: 'German', el: 'Greek', en: 'English', es: 'Spanish',
        et: 'Estonian', eu: 'Basque', fa: 'Persian', fi: 'Finnish',
        fo: 'Faroese', fr: 'French', gl: 'Galician', gu: 'Gujarati',
        ha: 'Hausa', haw: 'Hawaiian', he: 'Hebrew', hi: 'Hindi',
        hr: 'Croatian', ht: 'Haitian Creole', hu: 'Hungarian', hy: 'Armenian',
        id: 'Indonesian', is: 'Icelandic', it: 'Italian', ja: 'Japanese',
        jw: 'Javanese', ka: 'Georgian', kk: 'Kazakh', km: 'Khmer',
        kn: 'Kannada', ko: 'Korean', la: 'Latin', lb: 'Luxembourgish',
        ln: 'Lingala', lo: 'Lao', lt: 'Lithuanian', lv: 'Latvian',
        mg: 'Malagasy', mi: 'Maori', mk: 'Macedonian', ml: 'Malayalam',
        mn: 'Mongolian', mr: 'Marathi', ms: 'Malay', mt: 'Maltese',
        my: 'Myanmar', ne: 'Nepali', nl: 'Dutch', nn: 'Nynorsk',
        no: 'Norwegian', oc: 'Occitan', pa: 'Punjabi', pl: 'Polish',
        ps: 'Pashto', pt: 'Portuguese', ro: 'Romanian', ru: 'Russian',
        sa: 'Sanskrit', sd: 'Sindhi', si: 'Sinhala', sk: 'Slovak',
        sl: 'Slovenian', sn: 'Shona', so: 'Somali', sq: 'Albanian',
        sr: 'Serbian', su: 'Sundanese', sv: 'Swedish', sw: 'Swahili',
        ta: 'Tamil', te: 'Telugu', tg: 'Tajik', th: 'Thai',
        tk: 'Turkmen', tl: 'Tagalog', tr: 'Turkish', tt: 'Tatar',
        uk: 'Ukrainian', ur: 'Urdu', uz: 'Uzbek', vi: 'Vietnamese',
        yo: 'Yoruba', yue: 'Cantonese', zh: 'Chinese',
    },

    /**
     * Populate the language dropdown from App.config.stt_languages.
     * Called once during init.
     * @private
     */
    /** @type {boolean} Whether the language dropdown has been populated. @private */
    _languagePopulated: false,

    _populateLanguageDropdown() {
        const select = this._els.languageSelect;
        if (!select || this._languagePopulated) return;

        const codes = App.getSttLanguages();
        // Config may not have loaded yet — will retry from _updateLanguageDropdown
        if (!codes.length) return;
        this._languagePopulated = true;

        // Always prepend auto-detect option
        const auto = document.createElement('option');
        auto.value = '';
        auto.textContent = 'Auto-detect';
        select.appendChild(auto);

        for (const code of codes) {
            const opt = document.createElement('option');
            opt.value = code;
            opt.textContent = this._LANGUAGE_NAMES[code] || code;
            select.appendChild(opt);
        }
    },

    /**
     * Show/hide the language dropdown and sync its value to the selected
     * video.  Visible only when: exactly 1 video is selected, STT is
     * enabled, scenes are loaded, and at least one scene has a transcription.
     *
     * @param {string[]} [selection] - Currently selected video IDs.
     *     Falls back to reading the selected video from AppState if omitted.
     * @private
     */
    _updateLanguageDropdown(selection) {
        const control = this._els.languageControl;
        const separator = this._els.languageSeparator;
        const select = this._els.languageSelect;
        if (!control || !select) return;

        // Lazily populate if config wasn't ready at init time
        this._populateLanguageDropdown();

        // Use provided selection, or derive from AppState.videos
        const ids = selection
            || (AppState.videos.getSelectedVideoId() ? [AppState.videos.getSelectedVideoId()] : []);
        let visible = false;

        if (ids.length === 1 && App.isSttEnabled()) {
            const videoId = ids[0];
            const scenes = AppState.videos.getScenes(videoId);
            const hasTranscription = scenes && scenes.some(s => s.transcription);
            // Show only if scenes are loaded and at least one has a transcription
            if (hasTranscription) {
                visible = true;
                const img = AppState.images.getById(videoId);
                const lang = img?.stt_language || '';

                // If the video has a language not in the config list, add it
                // temporarily so the dropdown reflects the actual value
                if (lang && !select.querySelector(`option[value="${CSS.escape(lang)}"]`)) {
                    const opt = document.createElement('option');
                    opt.value = lang;
                    opt.textContent = (this._LANGUAGE_NAMES[lang] || lang) + ' *';
                    opt.dataset.temporary = 'true';
                    select.appendChild(opt);
                }

                select.value = lang;
            }
        }

        // Remove any temporary options when hiding
        if (!visible) {
            select.querySelectorAll('option[data-temporary]').forEach(o => o.remove());
        }

        control.hidden = !visible;
        if (separator) separator.hidden = !visible;
    },

    /**
     * Load content similarity data for the selected video.
     * Mirrors Gallery._loadContentSimilarities().
     * @private
     */
    async _loadContentSimilarities() {
        const selected = App.getSelectedImages();
        if (selected.length === 0) {
            App.showError('Select a video first to sort by visual similarity.');
            App.setSortBy('date');
            return;
        }

        const referenceId = selected[0];

        try {
            await AppState.images.loadSimilarities(referenceId);
            this._refreshGrid();
        } catch (error) {
            console.error('Failed to load content similarities:', error);
            if (error.message && error.message.includes('404')) {
                App.showError('This video is still being processed. Please wait.');
            } else {
                App.showError('Could not load similarity data.');
            }
            App.setSortBy('date');
        }
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
                    if (isSearch) {
                        cmp = (a.combined_score || 0) - (b.combined_score || 0);
                    } else {
                        cmp = AppState.images.getSimilarity(a.id) - AppState.images.getSimilarity(b.id);
                    }
                    break;
                case 'rating':
                    cmp = (a.rating || '').localeCompare(b.rating || '');
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
        this._hidePreview();
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
                overlay.style.backgroundColor = this._scoreToColour(scene.normalised_score);
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

            // Scene preview popup — hover (desktop) and long-press (touch)
            sceneEl.addEventListener('mouseenter', (e) => {
                this._showPreview(e, scene, wrapper);
            });
            sceneEl.addEventListener('mousemove', (e) => {
                this._movePreview(e, wrapper);
            });
            sceneEl.addEventListener('mouseleave', () => {
                this._hidePreview();
            });

            // Long-press for touch: show preview after 400ms hold
            sceneEl.addEventListener('pointerdown', (e) => {
                if (e.pointerType !== 'touch') return;
                this._previewShown = false;
                this._previewTimer = setTimeout(() => {
                    this._previewShown = true;
                    this._showPreview(e, scene, wrapper);
                }, 400);
            });
            const cancelTouch = () => {
                if (this._previewTimer) {
                    clearTimeout(this._previewTimer);
                    this._previewTimer = null;
                }
                this._hidePreview();
            };
            sceneEl.addEventListener('pointerup', cancelTouch);
            sceneEl.addEventListener('pointerleave', cancelTouch);
            sceneEl.addEventListener('pointercancel', cancelTouch);

            // Click handlers
            sceneEl.addEventListener('click', (e) => {
                // Suppress click after long-press preview
                if (this._previewShown) {
                    this._previewShown = false;
                    e.stopPropagation();
                    return;
                }
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
            this._hidePreview();
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

        // Click on minimap background — jump viewport centre to click position
        minimap.addEventListener('mousedown', (e) => {
            if (e.target === viewport || dragging) return;
            const rect = minimap.getBoundingClientRect();
            const clickPct = (e.clientX - rect.left) / rect.width;
            // Centre the viewport around the click
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
            const color = this._scoreToColour(scene.normalised_score ?? 0);
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
    // SCENE PREVIEW POPUP
    // =========================================================================

    /**
     * Lazily create the scene preview popup element (body-level singleton).
     * @private
     */
    _ensurePreviewPopup() {
        if (this._previewPopup) return;
        const popup = App.createElement('div', { className: 'scene-preview-popup hidden' });
        const img = App.createElement('img', { className: 'scene-preview-img' });
        const score = App.createElement('span', { className: 'scene-preview-score' });
        const subtitle = App.createElement('span', { className: 'scene-preview-subtitle' });
        popup.appendChild(img);
        popup.appendChild(score);
        popup.appendChild(subtitle);
        document.body.appendChild(popup);
        this._previewPopup = popup;
        this._previewImg = img;
        this._previewScore = score;
        this._previewSubtitle = subtitle;
    },

    /**
     * Show the scene preview popup above the timeline, horizontally
     * centred on the pointer and clamped to viewport edges.
     * @param {MouseEvent|PointerEvent} e - Event with clientX/clientY
     * @param {Object} scene - Scene data (scene_id, normalised_score, etc.)
     * @param {HTMLElement} wrapper - The .timeline-track-wrapper element
     * @private
     */
    _showPreview(e, scene, wrapper) {
        this._ensurePreviewPopup();
        const popup = this._previewPopup;
        const img = this._previewImg;
        const scoreEl = this._previewScore;

        const thumbId = scene.scene_id || scene.id;
        img.src = `/api/scenes/${thumbId}/thumbnail?size=400`;

        const isSearch = AppState.videos.isSearchMode();
        if (isSearch && scene.normalised_score != null) {
            scoreEl.textContent = Math.round(scene.normalised_score * 100) + '%';
            scoreEl.style.display = '';
        } else {
            scoreEl.style.display = 'none';
        }

        const subEl = this._previewSubtitle;
        if (scene.transcription) {
            subEl.textContent = scene.transcription;
            subEl.style.display = '';
        } else {
            subEl.style.display = 'none';
        }

        popup.classList.remove('hidden');

        // Position after the image loads so offsetWidth/offsetHeight are correct.
        // Also position immediately with estimated size for responsiveness.
        const position = () => {
            const wrapperRect = wrapper.getBoundingClientRect();
            const gap = 6;
            const popupW = popup.offsetWidth || 240;
            const popupH = popup.offsetHeight || 160;
            let top = wrapperRect.top - popupH - gap;
            let left = e.clientX - popupW / 2;

            // Clamp to viewport
            left = Math.max(8, Math.min(left, window.innerWidth - popupW - 8));
            top = Math.max(8, top);

            popup.style.top = top + 'px';
            popup.style.left = left + 'px';
        };

        position();
        // Re-position once the image has loaded (corrects width)
        img.addEventListener('load', position, { once: true });
    },

    /**
     * Update horizontal position of the preview popup to track the pointer.
     * @param {MouseEvent} e - mousemove event
     * @param {HTMLElement} wrapper - The .timeline-track-wrapper element
     * @private
     */
    _movePreview(e, wrapper) {
        const popup = this._previewPopup;
        if (!popup || popup.classList.contains('hidden')) return;

        const wrapperRect = wrapper.getBoundingClientRect();
        const gap = 6;
        const popupW = popup.offsetWidth || 240;
        const popupH = popup.offsetHeight || 160;

        let left = e.clientX - popupW / 2;
        left = Math.max(8, Math.min(left, window.innerWidth - popupW - 8));
        popup.style.left = left + 'px';

        let top = wrapperRect.top - popupH - gap;
        top = Math.max(8, top);
        popup.style.top = top + 'px';
    },

    /**
     * Hide the scene preview popup.
     * @private
     */
    _hidePreview() {
        if (this._previewPopup) {
            this._previewPopup.classList.add('hidden');
        }
        if (this._previewTimer) {
            clearTimeout(this._previewTimer);
            this._previewTimer = null;
        }
    },

    // =========================================================================
    // HELPERS
    // =========================================================================

    /**
     * Convert a normalised score (0-1) to a heatmap colour.
     * 0 = transparent, 0-0.5 = blue->yellow, 0.5-1.0 = yellow->red.
     * @param {number} score
     * @returns {string} CSS colour with opacity
     * @private
     */
    _scoreToColour(score) {
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
