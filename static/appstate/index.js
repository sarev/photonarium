/**
 * AppState Index - Domain Load Order
 * ====================================
 *
 * This file documents the load order for AppState domain modules.
 * Each module attaches itself to the global AppState object created by core.js.
 *
 * **Note**: In the HTML, include scripts in this order:
 *
 * ```html
 * <script src="appstate/core.js"></script>
 * <script src="appstate/view.js"></script>
 * <script src="appstate/nav.js"></script>
 * <script src="appstate/filter.js"></script>
 * <script src="appstate/selection.js"></script>
 * <script src="appstate/status.js"></script>
 * <script src="appstate/search.js"></script>
 * <script src="appstate/folders.js"></script>
 * <script src="appstate/duplicates.js"></script>
 * <script src="appstate/identity.js"></script>
 * <script src="appstate/images.js"></script>
 * <script src="appstate/index.js"></script>
 * ```
 *
 * ## Domain Overview
 *
 * | Domain | File | Description |
 * |--------|------|-------------|
 * | Core | core.js | Transaction system, subscriber system, storage helpers |
 * | view | view.js | Theme, thumbnail size, sort settings (localStorage) |
 * | nav | nav.js | Screen navigation, history, scroll positions |
 * | filter | filter.js | Filter criteria (text, date, rating, people, semantic) |
 * | selection | selection.js | Per-context selection with shift-click anchoring |
 * | status | status.js | Backend processing status with polling |
 * | search | search.js | Semantic search execution and results |
 * | folders | folders.js | Folder management and stats |
 * | duplicates | duplicates.js | Duplicate groups by similarity level |
 * | faces | identity.js | Face detection results and identification |
 * | people | identity.js | Named people (emergent from faces) |
 * | images | images.js | Image metadata and display list |
 *
 * ## Dependencies
 *
 * ```
 * core.js       (foundation - no dependencies)
 *    │
 *    ├── view.js, nav.js, filter.js, selection.js
 *    │   (independent domains)
 *    │
 *    ├── status.js, search.js, folders.js, duplicates.js
 *    │   (use createSubscriberSystem from core)
 *    │
 *    ├── identity.js
 *    │   (faces + people together - tightly coupled)
 *    │
 *    └── images.js
 *        (references duplicates._internal for cascade delete)
 * ```
 *
 * ## Architecture Notes
 *
 * **faces + people in identity.js**: These domains are tightly coupled because:
 * - Faces belong to people
 * - Identifying a face may create a new person
 * - Unassigning all faces from a person auto-deletes that person
 * - Renaming a person to an existing name triggers merge
 * - Renaming a person to empty triggers dissolve
 *
 * Keeping them in one file avoids circular dependencies and makes the
 * bidirectional operations clear.
 *
 * **images.js depends on duplicates._internal**: When an image is deleted,
 * it needs to remove that image from cached duplicate groups. This is a
 * one-way dependency (images → duplicates).
 *
 * @fileoverview AppState domain load order and architecture documentation.
 */

'use strict';

// Verify all domains are loaded
(function() {
    const required = [
        'createSubscriberSystem', 'transaction', 'queueTransaction', 'markDirty',
        'view', 'nav', 'filter', 'selection', 'status', 'search',
        'folders', 'duplicates', 'faces', 'people', 'images', 'loading', 'events',
    ];

    const missing = required.filter(name => !AppState[name]);
    if (missing.length > 0) {
        console.error('[AppState] Missing domains or functions:', missing.join(', '));
    } else {
        console.log('[AppState] All domains loaded successfully');
    }

    // Expose version info
    AppState.version = '2.0.0';
    AppState.domains = [
        'view', 'nav', 'filter', 'selection', 'status', 'search',
        'folders', 'duplicates', 'faces', 'people', 'images', 'loading', 'events',
    ];
})();
