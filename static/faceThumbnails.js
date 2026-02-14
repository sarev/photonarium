'use strict';

/**
 * Face Thumbnail URL Manager
 *
 * Manages cache-busting for face thumbnail URLs. When images are modified
 * (rotation, rescan, etc.), face thumbnails are regenerated on the backend.
 * This utility ensures the frontend fetches fresh versions instead of cached ones.
 *
 * @fileoverview Face thumbnail cache-busting utility.
 */
const FaceThumbnails = {
    /**
     * Cache-bust timestamps. Map of faceId -> timestamp.
     * @type {Map<string, number>}
     * @private
     */
    _cacheBust: new Map(),

    /**
     * Mark a face thumbnail as needing cache-bust.
     * @param {string} faceId
     */
    bustCache(faceId) {
        if (!faceId) return;
        this._cacheBust.set(faceId, Date.now());
    },

    /**
     * Mark all face thumbnails for given images as needing cache-bust.
     * Also busts person thumbnails if the face is a person's preferred face.
     * Called when images are modified (rotation, rescan, etc.)
     * @param {string[]} imageIds
     */
    bustCacheForImages(imageIds) {
        if (!imageIds?.length) return;
        for (const imageId of imageIds) {
            const faces = AppState.faces.getForImage(imageId);
            for (const face of faces) {
                this.bustCache(face.id);

                // If this face is a person's preferred face, bust the person's thumbnail too
                if (face.person_id) {
                    const person = AppState.people.getById(face.person_id);
                    if (person?.preferred_face_id === face.id) {
                        AppState.people.bustThumbnailCache(face.person_id);
                    }
                }
            }
        }
    },

    /**
     * Get URL for a face thumbnail with cache-bust parameter if needed.
     * Uses session cache-bust if set, otherwise falls back to face's
     * updated_at from AppState (survives page reload).
     * @param {string} faceId
     * @returns {string}
     */
    getUrl(faceId) {
        // Session cache-bust takes priority
        let ts = this._cacheBust.get(faceId);

        // Fall back to face's updated_at (survives page reload)
        if (!ts && typeof AppState !== 'undefined' && AppState.faces?.getById) {
            const face = AppState.faces.getById(faceId);
            if (face?.updated_at) {
                ts = face.updated_at;
            }
        }

        const base = `/api/faces/${faceId}/thumbnail`;
        return ts ? `${base}?t=${ts}` : base;
    },

    /**
     * Clear all cache-bust entries.
     * Called on full page reload or when cache is known fresh.
     */
    clear() {
        this._cacheBust.clear();
    },
};
