/**
 * Share - download or natively share the selected images/videos
 * ==============================================================
 *
 * The toolbar Share button (Gallery and Videos screens) opens a small dialog
 * summarising the selection (photo/video counts and total size) with an
 * Original/Reduced choice.  The size summary is the point of the dialog: it
 * stops an accidental multi-gigabyte pull over Wi-Fi before it starts.
 *
 * Download submits a hidden form targeting a hidden iframe, so the browser
 * streams the response (a single file, or a zip for multiple items) straight
 * to disk with its own download UI - no blob-in-RAM limit and no URL-length
 * cap on large selections.  The iframe also absorbs any error response
 * instead of letting a form navigation replace the SPA.
 *
 * The native Share... button uses the Web Share API to hand the files to the
 * OS share sheet (mail, messaging apps, etc.).  It only appears when the
 * browser supports sharing files AND the page is a secure context (https or
 * localhost) - over plain http on the LAN the API is unavailable, so the
 * button is hidden rather than left to fail.
 *
 * Not a registered screen module - standalone object like Settings.
 *
 * @fileoverview Share/download dialog for the current selection.
 */

'use strict';

// eslint-disable-next-line no-unused-vars -- referenced by core.js via typeof check
const Share = {

    /** @type {string[]} Image IDs captured when the dialog was opened */
    _ids: [],

    /** @type {{photos: number, videos: number, raws: number}} Selection composition */
    _counts: { photos: 0, videos: 0, raws: 0 },

    /** localStorage key remembering the last-used mode across sessions */
    _MODE_KEY: 'photonarium-share-mode',

    /**
     * Initialises dialog listeners and the hidden download sink iframe.
     * Called once from core.js during app initialisation.
     */
    init() {
        // Hidden iframe the download form targets: the attachment response
        // streams to disk, and any error response lands here instead of
        // navigating the SPA away
        const iframe = document.createElement('iframe');
        iframe.name = 'share-download-sink';
        iframe.setAttribute('aria-hidden', 'true');
        iframe.style.display = 'none';
        document.body.appendChild(iframe);

        App.$('dialog-share-cancel')?.addEventListener('click', () => {
            App.$('dialog-share')?.close();
        });
        App.$('dialog-share-download')?.addEventListener('click', () => this._download());
        App.$('dialog-share-native')?.addEventListener('click', () => this._nativeShare());

        // Remember the chosen mode and keep the caveat line current
        document.querySelectorAll('#dialog-share input[name="share-mode"]').forEach((radio) => {
            radio.addEventListener('change', () => {
                localStorage.setItem(this._MODE_KEY, this._mode());
                this._updateNote();
            });
        });

        // Enter with a radio focused (the dialog's initial focus) starts the
        // download, so repeat use is just click -> Enter.  Buttons keep their
        // native Enter behaviour
        App.$('dialog-share')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && e.target.matches('input[type="radio"]')) {
                e.preventDefault();
                this._download();
            }
        });
    },

    /**
     * Opens the share dialog for the current selection.
     * No-op when nothing is selected (the toolbar button should be disabled).
     */
    open() {
        const ids = AppState.selection.get('gallery');
        if (!ids.length) return;
        this._ids = [...ids];

        // Restore the last-used mode so repeat use is click -> Enter
        const saved = localStorage.getItem(this._MODE_KEY);
        const radio = document.querySelector(
            `#dialog-share input[name="share-mode"][value="${saved === 'reduced' ? 'reduced' : 'original'}"]`);
        if (radio) radio.checked = true;

        this._updateSummary();
        this._updateNote();

        // Native share needs Web Share API file support and a secure context
        const nativeBtn = App.$('dialog-share-native');
        if (nativeBtn) {
            nativeBtn.hidden = !(window.isSecureContext
                && typeof navigator.canShare === 'function'
                && navigator.canShare({ files: [new File([''], 'probe.txt')] }));
            nativeBtn.disabled = false;
        }

        App.$('dialog-share')?.showModal();
    },

    /**
     * Returns the currently selected mode ('original' or 'reduced').
     * @returns {string} The selected share mode
     * @private
     */
    _mode() {
        const checked = document.querySelector('#dialog-share input[name="share-mode"]:checked');
        return checked?.value === 'reduced' ? 'reduced' : 'original';
    },

    /**
     * Populates the summary line: counts per media type and total size.
     * Also caches the selection composition for the caveat line.
     * @private
     */
    _updateSummary() {
        let photos = 0, videos = 0, raws = 0, bytes = 0;
        for (const id of this._ids) {
            const img = AppState.images.getById(id);
            if (!img) continue;
            if (img.media_type === 'video') {
                videos++;
            } else {
                photos++;
                if (App.isRawFile(img.basename)) raws++;
            }
            bytes += img.size || 0;
        }
        this._counts = { photos, videos, raws };

        const parts = [];
        if (photos) parts.push(`${photos} photo${photos === 1 ? '' : 's'}`);
        if (videos) parts.push(`${videos} video${videos === 1 ? '' : 's'}`);
        const summary = App.$('dialog-share-summary');
        if (summary) {
            summary.textContent = `${parts.join(' and ')} — ${App.formatFileSize(bytes)}`;
        }
    },

    /**
     * Shows mode-specific caveats: reduced never touches videos (and strips
     * camera metadata from photos); original sends RAW files as-is.
     * @private
     */
    _updateNote() {
        const notes = [];
        if (this._mode() === 'reduced') {
            if (this._counts.videos) {
                notes.push('Videos are always sent at original size.');
            }
            if (this._counts.photos) {
                notes.push('Reduced photos leave out camera metadata, including location.');
            }
        } else if (this._counts.raws) {
            notes.push('RAW photos are sent as camera RAW files, which some devices cannot display.');
        }
        const note = App.$('dialog-share-note');
        if (note) {
            note.textContent = notes.join(' ');
            note.hidden = !notes.length;
        }
    },

    /**
     * Submits a hidden form POST so the browser streams the download itself.
     * @private
     */
    _download() {
        const form = document.createElement('form');
        form.method = 'POST';
        form.action = '/api/share/download';
        form.target = 'share-download-sink';

        const idsInput = document.createElement('input');
        idsInput.type = 'hidden';
        idsInput.name = 'ids';
        idsInput.value = this._ids.join(',');
        form.appendChild(idsInput);

        const modeInput = document.createElement('input');
        modeInput.type = 'hidden';
        modeInput.name = 'mode';
        modeInput.value = this._mode();
        form.appendChild(modeInput);

        document.body.appendChild(form);
        form.submit();
        form.remove();
        App.$('dialog-share')?.close();
    },

    /**
     * Fetches the selected files and hands them to the OS share sheet.
     *
     * Files are fetched one at a time (GET with a single id) so each keeps
     * its own filename; the browser holds them in memory, which is fine for
     * the phone-sized selections this path is aimed at.
     * @private
     */
    async _nativeShare() {
        const btn = App.$('dialog-share-native');
        if (!btn || btn.disabled) return;
        btn.disabled = true;
        const originalLabel = btn.textContent;
        btn.textContent = 'Preparing…';

        try {
            const mode = this._mode();
            const files = [];
            for (const id of this._ids) {
                const resp = await fetch(`/api/share/download?ids=${encodeURIComponent(id)}&mode=${mode}`);
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const blob = await resp.blob();
                const name = this._filenameFromResponse(resp)
                    || AppState.images.getById(id)?.basename
                    || 'photo.jpg';
                files.push(new File([blob], name, { type: blob.type }));
            }
            if (!navigator.canShare({ files })) {
                throw new Error('This device cannot share these files');
            }
            await navigator.share({ files });
            App.$('dialog-share')?.close();
        } catch (err) {
            // AbortError means the user dismissed the share sheet - not an error
            if (err.name !== 'AbortError') {
                console.error('Native share failed:', err);
                App.showError(`Sharing failed — try Download instead (${err.message})`);
            }
        } finally {
            btn.disabled = false;
            btn.textContent = originalLabel;
        }
    },

    /**
     * Extracts the filename from a Content-Disposition response header.
     * @param {Response} resp - The fetch response
     * @returns {string|null} The attachment filename, or null if absent
     * @private
     */
    _filenameFromResponse(resp) {
        const disposition = resp.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
        return match ? decodeURIComponent(match[1].replace(/"$/, '')) : null;
    },
};
