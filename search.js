/**
 * @fileoverview Search and filter screen module for the Imaginary application.
 *
 * This module handles the Search screen where users create filters to narrow
 * down the gallery view. It registers with the core App module and
 * communicates filter state back to the Gallery module.
 *
 * RESPONSIBILITIES:
 *
 * Text Search:
 *   - Text input field for searching image descriptions
 *   - Searches use OpenCLIP semantic similarity, not just keyword matching
 *   - Matches against user-added descriptions
 *   - Results are ranked by semantic relevance when using content sort
 *
 * Date Range Filter:
 *   - Start date and end date picker inputs
 *   - Filters images by their "best guess" timestamp
 *   - If only start date set, shows images from that date onward
 *   - If only end date set, shows images up to that date
 *   - If both dates are the same, filters to that exact date
 *   - Date pickers use native browser date input
 *
 * Rating Filter:
 *   - Text input for entering emoji ratings to filter by
 *   - Emoji picker button opens emoji selection dialog
 *   - Multiple emoji can be entered to match images with any of those ratings
 *   - Matches images whose rating contains any of the specified emoji
 *
 * Emoji Picker:
 *   - Grid of common rating emoji (stars, hearts, thumbs, etc.)
 *   - Clicking an emoji adds it to the rating filter input
 *   - Picker dialog is shared with Gallery info panel (managed by core)
 *
 * Filter Application:
 *   - "Apply Filter" button activates the filter and returns to Gallery
 *   - Gallery receives filter criteria and updates its display
 *   - "Clear Filter" button resets all filter fields
 *   - Filter state is stored in App state for persistence during session
 *
 * Filter Indicator:
 *   - When a filter is active, the filter toolbar button shows active state
 *   - Clicking filter button when filter is active clears filter (from Gallery)
 *   - Filter criteria are preserved when navigating away and back to Search
 *
 * Validation:
 *   - Validates date range (start not after end)
 *   - Shows validation feedback for invalid inputs
 *
 * LIFECYCLE HOOKS:
 *   - onEnter(): Populates form fields from current filter state
 *   - onLeave(): Optionally auto-applies filter if fields have changed
 *
 * @module search
 * @requires core
 */
