#!/usr/bin/env python3
"""
Tutorial Generator for Photonarium
=================================

Automates screenshot capture and HTML tutorial generation using Playwright.
Starts the real Photonarium backend against the tools/mktutorial/ data
directory, drives the browser through each tutorial step, captures
screenshots, and generates static HTML pages.

Prerequisites:
    pip install playwright pillow
    playwright install chromium

Usage (from the project root):
    # One-time setup: generate DB, thumbnails, models, setup screenshots
    python tools/mktutorial/tutorial.py --setup

    # Generate tutorial screenshots and HTML slideshow
    python tools/mktutorial/tutorial.py

Creates a 'generated' folder next to the project root with:
    - Copy of the demo database, config, and thumbnails (for the server)
    - Screenshots captured by Playwright
    - Static HTML tutorial pages with a table of contents
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright is required. Install it with:')
    print('  pip install playwright')
    print('  playwright install chromium')
    sys.exit(1)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent  # tools/mktutorial/
PROJECT_DIR = SCRIPT_DIR.parent.parent  # project root
TUTORIALS_DIR = PROJECT_DIR / 'generated'
SCREENSHOTS_DIR = TUTORIALS_DIR / 'screenshots'
MANUAL_DIR = TUTORIALS_DIR / 'manual'
SETUP_CACHE_DIR = SCRIPT_DIR / 'setup-cache'

# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------

SERVER_PORT = 5111  # non-standard port to avoid clashing
SERVER_URL = f'http://localhost:{SERVER_PORT}'
SERVER_STARTUP_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Viewport and timing
# ---------------------------------------------------------------------------

VIEWPORT = {'width': 1500, 'height': 900}
SETTLE_MS = 400  # ms to wait after actions for animations

# Debug: constrain which sections to run (None = run all).
# Set via --from-section / --to-section CLI flags.
START_FROM_SECTION = None
STOP_AFTER_SECTION = None

# ---------------------------------------------------------------------------
# Tutorial script — section titles and step text loaded from script.json
# ---------------------------------------------------------------------------

_SCRIPT_PATH = SCRIPT_DIR / 'script.json'
with open(_SCRIPT_PATH, encoding='utf-8') as _f:
    _SCRIPT = json.load(_f)
_STEP_TEXT = _SCRIPT['steps']

# ---------------------------------------------------------------------------
# Tutorial step registry — auto-numbered sections and steps
# ---------------------------------------------------------------------------

SECTIONS = []
STEPS = []

_current_section = -1  # auto-incremented by section()
_step_counter = 0  # reset to 0 by section(), incremented by step/manual_step


def section(key):
    """Register a new section.  Number is auto-assigned from position in script.json."""
    global _current_section, _step_counter
    _current_section += 1
    _step_counter = 0
    text = _SCRIPT['sections'][_current_section]
    assert text['key'] == key, (
        f"Section key mismatch at position {_current_section}: expected '{text['key']}', got '{key}'"
    )
    SECTIONS.append({'number': _current_section, 'title': text['title']})


def step(key):
    """
    Decorator that registers a tutorial step with auto-numbering.

    The step key is looked up in script.json for title and caption text.
    The decorated function receives (page, ctx) where ctx is a dict
    that persists across steps for carrying forward state.
    """
    global _step_counter
    _step_counter += 1
    text = _STEP_TEXT[key]
    step_id = f'{_current_section}.{_step_counter}'

    def decorator(fn):
        STEPS.append(
            {
                'section': _current_section,
                'id': step_id,
                'title': text['title'],
                'caption': text['caption'],
                'action': fn,
                'screenshot': f'screenshots/{step_id.replace(".", "-")}.png',
            }
        )
        return fn

    return decorator


def manual_step(key, filename):
    """Register a step with a pre-existing screenshot.  Auto-numbered like step()."""
    global _step_counter
    _step_counter += 1
    text = _STEP_TEXT[key]
    step_id = f'{_current_section}.{_step_counter}'
    STEPS.append(
        {
            'section': _current_section,
            'id': step_id,
            'title': text['title'],
            'caption': text['caption'],
            'action': None,
            'screenshot': f'manual/{filename}',
        }
    )


def setup_step(key):
    """Register a step whose screenshot is captured by ``--setup``.

    Auto-numbered identically to :func:`step` and :func:`manual_step`, but
    the screenshot path points into ``screenshots/`` (same directory as
    automated steps).  The action is ``None`` so the main capture loop skips
    it -- the image file is expected to already exist because
    :func:`setup_tutorials_dir` copies it from the setup cache.
    """
    global _step_counter
    _step_counter += 1
    text = _STEP_TEXT[key]
    step_id = f'{_current_section}.{_step_counter}'
    STEPS.append(
        {
            'section': _current_section,
            'id': step_id,
            'title': text['title'],
            'caption': text['caption'],
            'action': None,
            'screenshot': f'screenshots/{step_id.replace(".", "-")}.png',
        }
    )


# =========================================================================
# Helper functions (available to step actions)
# =========================================================================


def wait_for_thumbnails(page, selector='.gallery-item img[src]', count=1):
    """Wait until at least `count` thumbnail images have loaded."""
    page.wait_for_selector(selector, timeout=5000)
    page.wait_for_timeout(SETTLE_MS)


def wait_for_idle(page):
    """Wait for animations and network to settle."""
    page.wait_for_timeout(SETTLE_MS)


def click_toolbar(page, btn_id):
    """Click a toolbar button by its element ID."""
    page.click(f'#{btn_id}')
    wait_for_idle(page)


def navigate_to(page, screen):
    """Navigate to a screen and wait for it to appear."""
    btn_map = {
        'gallery': 'btn-back-gallery',
        'database': 'btn-database',
        'duplicates': 'btn-duplicates',
        'faces': 'btn-faces',
        'search': 'btn-filter',
    }
    click_toolbar(page, btn_map[screen])
    page.wait_for_selector(f'#screen-{screen}', state='visible', timeout=5000)
    wait_for_idle(page)


def set_similarity_slider(page, position):
    """Click the similarity slider to a specific position (0-5).

    Position 0 = Custom (leftmost), 5 = Identical (rightmost).
    Uses Playwright click with a calculated x-offset so the interaction
    looks like a real user clicking the slider track.
    """
    slider = page.locator('#similarity-slider')
    box = slider.bounding_box()
    # The usable range sits inside the slider's bounding box with a bit of
    # padding at each end for the thumb.  Calculate x so that position 0
    # lands near the left edge and position 5 near the right edge.
    padding = box['height']  # thumb is roughly as wide as the slider is tall
    usable = box['width'] - 2 * padding
    x = padding + (position / 5) * usable
    slider.click(position={'x': x, 'y': box['height'] / 2})
    wait_for_idle(page)


def nth_gallery_item(page, n):
    """Get the nth (1-based) visible gallery item."""
    return page.locator(f'.gallery-item:nth-child({n})')


def gallery_item_by_name(page, filename):
    """Get a gallery item by its filename, scrolling it into view first.

    VirtualGrid only renders items that are near the viewport, so the
    target element may not exist in the DOM yet.  We ask the grid to
    scroll to the image's ID (via AppState), which forces VirtualGrid
    to render it, then return a locator pinned to the item's data-id.
    """
    image_id = page.evaluate(f"""() => {{
        const img = AppState.images.getDisplayList()
            .find(i => i.basename === '{filename}');
        return img ? img.id : null;
    }}""")
    if not image_id:
        raise ValueError(f'Image not found in display list: {filename}')
    # Ask VirtualGrid to scroll the item into view so it gets rendered
    page.evaluate(f"() => Gallery._grid?.scrollToId('{image_id}')")
    locator = page.locator(f'.gallery-item[data-id="{image_id}"]')
    locator.wait_for(state='attached', timeout=5000)
    return locator


def nth_face_card(page, n):
    """
    Get the nth (1-based) face card by visual position in the unknown section.

    VirtualGrid appends cards asynchronously as thumbnails load, so DOM order
    may not match visual grid order.  We sort by bounding rect (top then left)
    and return a locator pinned to the card's data-id.
    """
    face_id = page.evaluate(f"""() => {{
        const cards = [...document.querySelectorAll(
            '.faces-unknown-container .face-card')];
        cards.sort((a, b) => {{
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
            return ar.left - br.left;
        }});
        return cards[{n - 1}]?.dataset?.id || null;
    }}""")
    if not face_id:
        raise ValueError(f'No face card found at visual position {n}')
    return page.locator(f'.face-card[data-id="{face_id}"]')


def click_face_card(page, n, button='left'):
    """
    Select/toggle a face card by dispatching a click event on its thumbnail.

    Playwright's native click() doesn't reliably reach GridSelection's
    event handler in headless mode — the click can land on invisible overlay
    buttons (opacity: 0, z-index: 10) which call stopPropagation.  We
    dispatch the event directly from JS to guarantee it bubbles correctly.

    Args:
        page: Playwright page
        n: 1-based visual position in the unknown faces grid
        button: 'left' for single select, 'right' for toggle (add to selection)
    Returns:
        The face card Playwright locator
    """
    result = page.evaluate(f"""() => {{
        const cards = [...document.querySelectorAll(
            '.faces-unknown-container .face-card')];
        cards.sort((a, b) => {{
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
            return ar.left - br.left;
        }});
        const card = cards[{n - 1}];
        if (!card) return null;
        const thumb = card.querySelector('.face-card-thumb');
        if (!thumb) return null;
        const rect = thumb.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const isRight = {'true' if button == 'right' else 'false'};
        if (isRight) {{
            thumb.dispatchEvent(new MouseEvent('contextmenu', {{
                bubbles: true, cancelable: true,
                clientX: cx, clientY: cy,
                button: 2, view: window
            }}));
        }} else {{
            thumb.dispatchEvent(new MouseEvent('click', {{
                bubbles: true, cancelable: true,
                clientX: cx, clientY: cy,
                button: 0, view: window
            }}));
        }}
        return card.dataset.id;
    }}""")
    if not result:
        raise ValueError(f'No face card found at visual position {n}')
    return page.locator(f'.face-card[data-id="{result}"]')


def highlight_element(page, selector, color='rgba(255, 96, 0, 0.35)', width='3px'):
    """
    Inject a temporary highlight outline on an element.
    Removed automatically after the screenshot by the main loop.
    """
    page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) {{
            el.dataset.tutorialHighlight = el.style.outline || '';
            el.style.outline = '{width} solid {color}';
        }}
    }}""")


def spotlight_element(page, selector, darkness='rgba(0, 0, 0, 0.55)'):
    """
    Darken everything except the target element, creating a spotlight effect.
    Uses a huge box-shadow spread to simulate a page-wide overlay while the
    element itself remains visible above the shadow.
    """
    page.evaluate(f"""() => {{
        const el = document.querySelector('{selector}');
        if (el) {{
            el.dataset.tutorialSpotlight = JSON.stringify({{
                position: el.style.position,
                zIndex: el.style.zIndex,
                boxShadow: el.style.boxShadow,
            }});
            el.style.position = 'relative';
            el.style.zIndex = '10000';
            el.style.boxShadow = '0 0 0 9999px {darkness}';
        }}
    }}""")


def remove_highlights(page):
    """Remove all tutorial highlights, spotlights, and SVG overlays."""
    page.evaluate("""() => {
        document.querySelectorAll('[data-tutorial-highlight]').forEach(el => {
            // SVG overlays (e.g. arrows) are removed entirely
            if (el.tagName === 'svg' || el.tagName === 'SVG') {
                el.remove();
                return;
            }
            el.style.outline = el.dataset.tutorialHighlight;
            delete el.dataset.tutorialHighlight;
        });
        document.querySelectorAll('[data-tutorial-spotlight]').forEach(el => {
            const saved = JSON.parse(el.dataset.tutorialSpotlight);
            el.style.position = saved.position;
            el.style.zIndex = saved.zIndex;
            el.style.boxShadow = saved.boxShadow;
            delete el.dataset.tutorialSpotlight;
        });
    }""")


# ---------------------------------------------------------------------------
# Face identification helpers — deterministic lookup by image filename
# ---------------------------------------------------------------------------

# Images known to contain real faces.  Any face detection on an image NOT
# in this set is treated as a false positive (non-face).
_FACE_IMAGES = frozenset(
    {
        'photo_125.jpg',  # ignore
        'photo_425.jpg',  # ignore
        'photo_240.jpg',  # ignore
        'photo_076.jpg',  # Nia (left), Alice (right)
        'photo_075.jpg',  # Alice (left), unknown (right)
        'photo_112.jpg',  # Geoff
    }
)


def face_card_by_image(page, basename, position='only'):
    """Find an unknown face card by its source image filename.

    Args:
        page: Playwright page.
        basename: Image filename (e.g. ``'photo_076.jpg'``).
        position: ``'only'`` if the image has a single face,
            ``'left'`` for the leftmost bounding box, or
            ``'right'`` for the rightmost.

    Returns:
        Playwright locator for the ``.face-card`` element.
    """
    face_id = page.evaluate(f"""() => {{
        const faces = AppState.faces.getAll();
        const images = AppState.images.getAll();
        const image = images.find(img => img.basename === '{basename}');
        if (!image) return null;
        let matches = faces.filter(
            f => f.image_id === image.id && !f.suppressed && !f.person_id);
        if (matches.length === 0) return null;
        if (matches.length === 1) return matches[0].id;
        matches.sort((a, b) => a.box_x - b.box_x);
        return '{position}' === 'right'
            ? matches[matches.length - 1].id : matches[0].id;
    }}""")
    if not face_id:
        raise ValueError(f'No unknown face card for {basename} ({position})')
    return page.locator(f'.face-card[data-id="{face_id}"]')


def click_face_id(page, face_id, button='left'):
    """Dispatch a click on a face card by its face ID.

    Uses JS-dispatched events to reliably reach GridSelection's handler
    (same approach as :func:`click_face_card`).

    Returns:
        Playwright locator for the face card.
    """
    is_right = 'true' if button == 'right' else 'false'
    result = page.evaluate(f"""() => {{
        const card = document.querySelector(
            '.face-card[data-id="{face_id}"]');
        if (!card) return null;
        const thumb = card.querySelector('.face-card-thumb');
        if (!thumb) return null;
        const rect = thumb.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        if ({is_right}) {{
            thumb.dispatchEvent(new MouseEvent('contextmenu', {{
                bubbles: true, cancelable: true,
                clientX: cx, clientY: cy,
                button: 2, view: window
            }}));
        }} else {{
            thumb.dispatchEvent(new MouseEvent('click', {{
                bubbles: true, cancelable: true,
                clientX: cx, clientY: cy,
                button: 0, view: window
            }}));
        }}
        return true;
    }}""")
    if not result:
        raise ValueError(f'Face card not found: {face_id}')
    return page.locator(f'.face-card[data-id="{face_id}"]')


def get_non_face_ids(page):
    """Return face IDs of likely false-positive detections.

    Any detected face whose source image is not in :data:`_FACE_IMAGES`
    is assumed to be a non-face.
    """
    known_json = json.dumps(list(_FACE_IMAGES))
    return page.evaluate(f"""() => {{
        const known = new Set({known_json});
        const faces = AppState.faces.getAll();
        const images = AppState.images.getAll();
        const imgMap = new Map(images.map(i => [i.id, i.basename]));
        return faces
            .filter(f => !f.suppressed && !f.person_id
                && !known.has(imgMap.get(f.image_id)))
            .map(f => f.id);
    }}""")


# =========================================================================
# Section 0: GETTING STARTED (setup screenshots)
# =========================================================================
section('getting-started')

setup_step('first-launch')  # 0-1.png -- light theme empty DB screen
setup_step('dark-theme')  # 0-2.png -- dark theme empty DB screen
setup_step('adding-images')  # 0-3.png -- composite: dark DB + OS picker overlay
setup_step('indexing')  # 0-4.png -- folder added, scan starting
setup_step('processing')  # 0-5.png -- importing in progress


# =========================================================================
# Section 1: GALLERY
# =========================================================================
section('gallery')


@step('the-gallery')
def step_gallery_the_gallery(page, ctx):
    page.wait_for_selector('.gallery-item img[src]', timeout=15000)
    wait_for_idle(page)


@step('gallery-toolbar')
def step_gallery_toolbar(page, ctx):
    # Highlight the toolbar for the overview screenshot
    highlight_element(page, '#toolbar')


@step('selecting-a-photo')
def step_gallery_selecting_a_photo(page, ctx):
    # Select photo_494.jpg — the one we'll add a description to
    item = gallery_item_by_name(page, 'photo_494.jpg')
    item.scroll_into_view_if_needed()
    item.click()
    wait_for_idle(page)


@step('the-info-panel')
def step_gallery_the_info_panel(page, ctx):
    spotlight_element(page, '#info-panel')
    page.wait_for_timeout(100)


@step('adding-a-description')
def step_gallery_adding_a_description(page, ctx):
    desc = page.locator('#info-description')
    desc.click()
    desc.fill('Steam rising from a hot drink next to a pink flower on a book')
    desc.press('Enter')
    wait_for_idle(page)
    highlight_element(page, '#info-description')


@step('auto-captioning')
def step_gallery_auto_captioning(page, ctx):
    # Select a different photo from the one we just described
    item = gallery_item_by_name(page, 'photo_499.jpg')
    item.scroll_into_view_if_needed()
    item.click()
    wait_for_idle(page)
    # Wait for info panel to populate with the new image
    page.wait_for_selector('#info-generate-caption-btn', state='visible', timeout=5000)
    # Click the sparkle button to generate a caption
    page.click('#info-generate-caption-btn')
    # Wait for caption to populate (backend ML — may take a while)
    page.wait_for_function('() => document.getElementById("info-description")?.value?.length > 0', timeout=30000)
    wait_for_idle(page)
    # Highlight the sparkle button so the screenshot shows what was clicked
    highlight_element(page, '#info-generate-caption-btn', color='rgba(255, 180, 0, 0.6)', width='3px')


@step('emojis')
def step_gallery_emojis(page, ctx):
    # The info panel emoji button is #info-emoji-btn (not #btn-emoji-picker
    # which is on the Search screen)
    page.click('#info-emoji-btn')
    page.wait_for_selector('#dialog-emoji[open]', timeout=5000)
    wait_for_idle(page)


@step('ratings')
def step_gallery_ratings(page, ctx):
    # Click a star emoji from the picker grid
    page.locator('#emoji-grid .emoji-btn').first.click()
    wait_for_idle(page)
    # Close the emoji picker
    page.click('#dialog-emoji-close')
    wait_for_idle(page)
    # Highlight the rating field to draw attention to the result
    highlight_element(page, '#info-rating')


@step('multiple-selection')
def step_gallery_multiple_selection(page, ctx):
    nth_gallery_item(page, 4).click()
    wait_for_idle(page)
    nth_gallery_item(page, 12).click(modifiers=['Shift'])
    wait_for_idle(page)


@step('sort-direction-flip')
def step_gallery_sort_direction_flip(page, ctx):
    click_toolbar(page, 'btn-sort-direction')
    page.wait_for_timeout(800)


@step('sort-direction-restore')
def step_gallery_sort_direction_restore(page, ctx):
    click_toolbar(page, 'btn-sort-direction')
    page.wait_for_timeout(800)


@step('similarity-select')
def step_gallery_similarity_select(page, ctx):
    item = gallery_item_by_name(page, 'photo_499.jpg')
    item.scroll_into_view_if_needed()
    item.click()
    wait_for_idle(page)


@step('similarity-result')
def step_gallery_similarity_result(page, ctx):
    click_toolbar(page, 'btn-sort-content')
    page.wait_for_timeout(1500)


# =========================================================================
# Section 2: FULL-SCREEN VIEWER
# =========================================================================
section('fullscreen')


@step('fullscreen-opening')
def step_fullscreen_opening(page, ctx):
    # Reset sort to date first
    click_toolbar(page, 'btn-sort-date')
    page.wait_for_timeout(500)
    nth_gallery_item(page, 5).dblclick()
    page.wait_for_selector('#fullscreen-overlay.visible', timeout=5000)
    wait_for_idle(page)


@step('fullscreen-controls')
def step_fullscreen_controls(page, ctx):
    # Highlight the fullscreen toolbar
    highlight_element(page, '#fullscreen-toolbar')


@step('navigating')
def step_fullscreen_navigating(page, ctx):
    page.keyboard.press('ArrowRight')
    page.wait_for_timeout(600)
    page.keyboard.press('ArrowRight')
    wait_for_idle(page)


@step('zooming-and-panning')
def step_fullscreen_zooming_and_panning(page, ctx):
    # Zoom in with mouse wheel at centre of viewport.
    # Each wheel tick zooms by 1.15x — need ~10 ticks for ~4x zoom.
    cx = VIEWPORT['width'] // 2
    cy = VIEWPORT['height'] // 2
    page.mouse.move(cx, cy)
    for _ in range(10):
        page.mouse.wheel(0, -1)
        page.wait_for_timeout(50)
    wait_for_idle(page)


@step('fullscreen-closing')
def step_fullscreen_closing(page, ctx):
    page.keyboard.press('Escape')
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)


# =========================================================================
# Section 3: SEARCH
# =========================================================================
section('search')


@step('search-opening')
def step_search_opening(page, ctx):
    navigate_to(page, 'search')


@step('search-by-description')
def step_search_by_description(page, ctx):
    text_input = page.locator('#filter-text')
    text_input.fill('red car')
    wait_for_idle(page)
    # Don't press Enter — that triggers Apply. Just show the typed text.
    highlight_element(page, '#filter-text')
    highlight_element(page, '#filter-similarity', color='rgba(255, 180, 0, 0.5)', width='2px')


@step('search-results')
def step_search_results(page, ctx):
    # Apply takes us to Gallery
    page.click('#btn-apply-filter')
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)


@step('negative-terms')
def step_search_negative_terms(page, ctx):
    navigate_to(page, 'search')
    text_input = page.locator('#filter-text')
    text_input.fill('trees plants -people -person -man -woman')
    wait_for_idle(page)
    highlight_element(page, '#filter-text')


@step('negative-results')
def step_search_negative_results(page, ctx):
    page.click('#btn-apply-filter')
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)


@step('date-ranges')
def step_search_date_ranges(page, ctx):
    navigate_to(page, 'search')
    page.fill('#filter-date-start', '2026-02-07')
    wait_for_idle(page)
    highlight_element(page, '#filter-date-start')


@step('combined-filters')
def step_search_combined_filters(page, ctx):
    page.click('#btn-apply-filter')
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)


@step('clearing-the-filter')
def step_search_clearing_the_filter(page, ctx):
    # Just highlight the clear filter button — don't click it yet, because
    # clicking disables the button (opacity 0.4) which looks confusing.
    # The filter gets cleared at the start of section 4.
    highlight_element(page, '#btn-clear-filter', color='rgba(255, 180, 0, 0.6)', width='3px')


# =========================================================================
# Section 4: GROUPS
# =========================================================================
section('groups')


@step('groups-opening')
def step_groups_opening(page, ctx):
    # Clear any active filter from section 3
    try:
        page.click('#btn-clear-filter')
        page.wait_for_timeout(300)
    except Exception:
        pass
    navigate_to(page, 'duplicates')
    page.wait_for_selector('.duplicate-stack', timeout=5000)
    wait_for_idle(page)


@step('groups-toolbar')
def step_groups_toolbar(page, ctx):
    highlight_element(page, '#toolbar')


@step('strictness')
def step_groups_strictness(page, ctx):
    # Move the similarity slider to "Related" level (position 2).
    set_similarity_slider(page, 2)
    page.wait_for_selector('.duplicate-stack', timeout=15000)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider', color='rgba(255, 180, 0, 0.5)', width='2px')


@step('opening-a-group')
def step_groups_opening_a_group(page, ctx):
    # Still on Related level from strictness step — open the first stack
    page.locator('.duplicate-stack').first.dblclick()
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)
    # Select the middle (worst) image to show comparison/selection
    nth_gallery_item(page, 2).click()
    wait_for_idle(page)


@step('moving-between-groups')
def step_groups_moving_between_groups(page, ctx):
    click_toolbar(page, 'btn-next-group')
    page.wait_for_timeout(800)


@step('pruning-button')
def step_groups_pruning_button(page, ctx):
    # Return to the Groups screen to show the prune toolbar button.
    navigate_to(page, 'duplicates')
    page.wait_for_selector('.duplicate-stack', timeout=5000)
    wait_for_idle(page)
    # Cache the 4th group's hash (aurora photos) for use in section 5.
    # Must use Duplicates.state.groups (display order) rather than
    # nth-child, because VirtualGrid appends DOM elements in thumbnail-
    # load order which doesn't match visual position.
    ctx['aurora_group_hash'] = page.evaluate('() => Duplicates.state.groups[3]?.group_hash || ""')
    highlight_element(page, '#btn-dup-refine', color='rgba(255, 180, 0, 0.6)', width='3px')


@step('pruning-dialog')
def step_groups_pruning_dialog(page, ctx):
    # Open the refine dialog without actually refining — we just want
    # to show what it looks like.  Don't click Trash!
    page.click('#btn-dup-refine')
    page.wait_for_selector('#dialog-refine[open]', timeout=5000)
    wait_for_idle(page)


@step('directories')
def step_groups_directories(page, ctx):
    # Close the refine dialog from the previous step
    page.click('#dialog-refine-cancel')
    page.wait_for_timeout(200)
    # Move slider to Directories (position 1).
    # Directory groups may or may not exist depending on demo-seed structure.
    set_similarity_slider(page, 1)
    # Give the level switch time to settle even if there are no stacks
    page.wait_for_timeout(1000)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider', color='rgba(255, 180, 0, 0.5)', width='2px')


# =========================================================================
# Section 5: CUSTOM GROUPS (ALBUMS)
# =========================================================================
section('custom-groups')


@step('custom-level')
def step_custom_groups_custom_level(page, ctx):
    # Move slider to Custom (position 0).
    # Custom level starts empty (no groups yet) so we just wait a beat
    # rather than waiting for stacks.
    set_similarity_slider(page, 0)
    page.wait_for_timeout(500)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider', color='rgba(255, 180, 0, 0.5)', width='2px')


@step('creating-a-group')
def step_custom_groups_creating_a_group(page, ctx):
    # Click New Group to open the prompt dialog, type a name but don't
    # confirm yet — the screenshot should show the dialog with the name.
    page.click('#btn-group-new')
    page.wait_for_selector('#dialog-prompt[open]', timeout=5000)
    page.fill('#dialog-prompt-input', 'Aurorae')
    wait_for_idle(page)


@step('adding-photos-from-gallery')
def step_custom_groups_adding_photos(page, ctx):
    # Confirm the "Aurora" group left open by the previous step
    page.click('#dialog-prompt-ok')
    page.wait_for_timeout(500)
    # Switch to Related level (position 2) and open the aurora group
    # (cached in step 4.7) — a perfect match for the "Aurora" custom group.
    # We're already on the Groups screen from the previous step.
    set_similarity_slider(page, 2)
    page.wait_for_selector('.duplicate-stack', timeout=15000)
    wait_for_idle(page)
    # Open the aurora group by its hash (position may vary between runs)
    aurora_hash = ctx.get('aurora_group_hash', '')
    page.locator(f'.duplicate-stack[data-group-hash="{aurora_hash}"]').dblclick()
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)
    # Select all photos so they'll all be added to the group
    page.keyboard.press('Control+a')
    wait_for_idle(page)
    # Hover the first thumbnail to reveal the group button
    first_item = page.locator('.gallery-item').first
    first_item.hover()
    page.wait_for_timeout(300)
    highlight_element(page, '.gallery-item-group-btn', color='rgba(206, 147, 216, 0.6)', width='2px')


@step('the-group-picker')
def step_custom_groups_the_group_picker(page, ctx):
    # Hover a thumbnail to reveal the group badge, then click it to open
    # the Group Picker dialog (the real user flow).
    first_item = page.locator('.gallery-item').first
    first_item.hover()
    page.wait_for_timeout(400)
    first_item.locator('.gallery-item-group-btn').click()
    page.wait_for_selector('#dialog-group-picker[open]', timeout=8000)
    page.wait_for_timeout(400)
    highlight_element(page, '.entity-picker-content', color='rgba(206, 147, 216, 0.4)', width='2px')


@step('managing-groups')
def step_custom_groups_managing_groups(page, ctx):
    # Add the selected aurora photos to the Aurorae group via the picker
    # left open by the previous step.  Click the Aurorae entry then Done.
    page.locator('.entity-picker-item:has-text("Aurora")').click()
    page.wait_for_timeout(300)
    page.click('#dialog-group-done')
    page.wait_for_timeout(2500)

    # Navigate to Groups screen and switch to Custom level.
    navigate_to(page, 'duplicates')
    set_similarity_slider(page, 0)
    # Wait for the custom group stacks to render
    page.wait_for_selector('.duplicate-stack', timeout=5000)
    wait_for_idle(page)
    highlight_element(page, '#toolbar')


# =========================================================================
# Section 6: FACES
# =========================================================================
section('faces')


@step('faces-opening')
def step_faces_opening(page, ctx):
    navigate_to(page, 'faces')
    page.wait_for_selector('.face-card', timeout=5000)
    wait_for_idle(page)


@step('faces-toolbar')
def step_faces_toolbar(page, ctx):
    highlight_element(page, '#toolbar')


@step('unknown-faces')
def step_faces_unknown_faces(page, ctx):
    wait_for_idle(page)
    # Highlight one of the name input fields to draw attention to it.
    # Use Alice's face on photo_076 (right bbox) — the one we'll name next.
    card = face_card_by_image(page, 'photo_076.jpg', 'right')
    face_id = card.get_attribute('data-id')
    highlight_element(
        page,
        f'[data-id="{face_id}"] .face-card-input',
        color='rgba(255, 96, 0, 0.35)',
    )


@step('typing')
def step_faces_typing(page, ctx):
    # Type "Alice" into Alice's face on photo_076 (right bounding box)
    card = face_card_by_image(page, 'photo_076.jpg', 'right')
    card.scroll_into_view_if_needed()
    wait_for_idle(page)
    input_el = card.locator('.face-card-input')
    input_el.click()
    input_el.fill('Alice')
    wait_for_idle(page)
    face_id = card.get_attribute('data-id')
    highlight_element(page, f'[data-id="{face_id}"] .face-card-input')


@step('naming')
def step_faces_naming(page, ctx):
    # The input from the previous step should still have "Alice" — press Enter to commit
    page.keyboard.press('Enter')
    # Wait for the person card to appear in the Known People section before
    # trying to force-load its thumbnail image.
    page.wait_for_function(
        """() => {
        const cards = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return cards.length > 0;
    }""",
        timeout=10000,
    )
    # Force-load lazy person-card thumbnails so Alice's face is visible
    page.evaluate("""() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }""")
    page.wait_for_function(
        """() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }""",
        timeout=10000,
    )
    page.wait_for_timeout(300)


@step('autocomplete')
def step_faces_autocomplete(page, ctx):
    # Wait for Alice to be fully persisted in the people cache so
    # autocomplete can find her.
    page.wait_for_function("() => AppState.people.getAll().some(p => p.name === 'Alice')", timeout=5000)
    # Type "Ali" into Alice's other face (photo_075, left bounding box)
    card = face_card_by_image(page, 'photo_075.jpg', 'left')
    card.scroll_into_view_if_needed()
    wait_for_idle(page)
    # Fire the 'input' event via JS to reliably trigger autocomplete.
    # Playwright's fill() doesn't always cause native 'input' events
    # in headless Chromium.
    card_id = card.get_attribute('data-id')
    page.evaluate(f'''() => {{
        const input = document.querySelector(
            '[data-id="{card_id}"] .face-card-input');
        if (!input) return;
        input.focus();
        input.value = 'Ali';
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
    }}''')
    # Wait for autocomplete dropdown to appear.
    # Don't click the item yet — the screenshot should show the dropdown.
    page.wait_for_selector('.face-card-autocomplete-item', state='visible', timeout=5000)
    page.wait_for_timeout(200)
    # Force-load lazy person-card thumbnails (Alice's Known People card)
    page.evaluate("""() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }""")
    page.wait_for_function(
        """() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }""",
        timeout=5000,
    )
    page.wait_for_timeout(200)


@step('failed-face-detections')
def step_faces_failed_detections(page, ctx):
    # Commit the autocomplete selection left open by the previous step.
    # The dropdown may have closed between steps (focus lost during
    # screenshot capture), so fall back to Enter.
    if page.locator('.face-card-autocomplete-item').count() > 0:
        page.locator('.face-card-autocomplete-item').first.click()
    else:
        page.keyboard.press('Enter')
    page.wait_for_timeout(800)
    # Identify all false-positive detections and select the first one
    non_face_ids = get_non_face_ids(page)
    ctx['non_face_ids'] = non_face_ids
    if non_face_ids:
        card = page.locator(f'.face-card[data-id="{non_face_ids[0]}"]')
        card.scroll_into_view_if_needed()
        click_face_id(page, non_face_ids[0])
    wait_for_idle(page)


@step('selecting-multiple')
def step_faces_selecting_multiple(page, ctx):
    # Right-click remaining non-faces to add them to the selection.
    # The first non-face was already selected in the previous step.
    for nf_id in ctx.get('non_face_ids', [])[1:]:
        card = page.locator(f'.face-card[data-id="{nf_id}"]')
        card.scroll_into_view_if_needed()
        click_face_id(page, nf_id, button='right')
        page.wait_for_timeout(150)
    wait_for_idle(page)


@step('removing')
def step_faces_removing(page, ctx):
    # Re-select all non-faces to ensure the selection survived the
    # screenshot capture between steps.  Left-click the first, then
    # right-click the rest to add them to the selection.
    non_face_ids = ctx.get('non_face_ids', [])
    if non_face_ids:
        click_face_id(page, non_face_ids[0])
        page.wait_for_timeout(100)
        for nf_id in non_face_ids[1:]:
            click_face_id(page, nf_id, button='right')
            page.wait_for_timeout(100)
    # Hover over a selected non-face to reveal the suppress button
    nf_id = non_face_ids[0] if non_face_ids else None
    if nf_id:
        card = page.locator(f'.face-card[data-id="{nf_id}"]')
        card.hover()
        page.wait_for_timeout(300)
        card.locator('.face-card-suppress').click()
    page.wait_for_selector('#dialog-confirm[open]', timeout=5000)
    wait_for_idle(page)


@step('the-ignore-control')
def step_faces_the_ignore_control(page, ctx):
    # Dismiss the suppress confirmation dialog from the previous step.
    # The grid re-renders after suppressing, so wait for idle before
    # querying face cards to avoid stale DOM references.
    page.click('#dialog-confirm-ok')
    page.wait_for_timeout(1000)
    wait_for_idle(page)
    # Select two of the three "ignore" faces (photo_125, photo_425).
    # Re-query each card freshly to avoid stale element references after
    # the grid re-render.
    card1_id = face_card_by_image(page, 'photo_125.jpg').get_attribute('data-id')
    page.locator(f'.face-card[data-id="{card1_id}"]').scroll_into_view_if_needed()
    click_face_id(page, card1_id)
    page.wait_for_timeout(100)
    card2_id = face_card_by_image(page, 'photo_425.jpg').get_attribute('data-id')
    page.locator(f'.face-card[data-id="{card2_id}"]').scroll_into_view_if_needed()
    click_face_id(page, card2_id, button='right')
    wait_for_idle(page)
    # Hover the first to reveal the ignore button and highlight it
    page.locator(f'.face-card[data-id="{card1_id}"]').hover()
    page.wait_for_timeout(300)
    highlight_element(
        page,
        f'[data-id="{card1_id}"] .face-card-ignore',
        color='rgba(255, 180, 0, 0.6)',
        width='3px',
    )


@step('ignoring')
def step_faces_ignoring(page, ctx):
    # Click the ignore button — shows confirmation for multiple faces
    card = face_card_by_image(page, 'photo_125.jpg')
    card.locator('.face-card-ignore').click()
    page.wait_for_selector('#dialog-confirm[open]', timeout=5000)
    wait_for_idle(page)


@step('ignored-group')
def step_faces_ignored_group(page, ctx):
    # Dismiss the ignore confirmation dialog from the previous step.
    # Wait for the grid to re-render after removing ignored faces.
    page.click('#dialog-confirm-ok')
    page.wait_for_timeout(1000)
    wait_for_idle(page)
    # Spotlight the Known People section to show the '-' person
    spotlight_element(page, '.faces-section.known')


@step('quick-match')
def step_faces_quick_match(page, ctx):
    # Use Nia's face (photo_076, left bbox) for the quick-match demo.
    # Re-query face ID freshly; the grid re-rendered after ignore.
    wait_for_idle(page)
    fc_id = face_card_by_image(page, 'photo_076.jpg', 'left').get_attribute('data-id')
    page.locator(f'.face-card[data-id="{fc_id}"]').scroll_into_view_if_needed()
    click_face_id(page, fc_id)
    wait_for_idle(page)
    page.locator(f'.face-card[data-id="{fc_id}"]').hover()
    page.wait_for_timeout(300)
    page.locator(f'.face-card[data-id="{fc_id}"] .face-card-quickmatch').click()
    page.wait_for_selector('.quick-match-card.visible', timeout=5000)
    wait_for_idle(page)


@step('naming-another-face')
def step_faces_naming_another_face(page, ctx):
    # Dismiss quick match first
    page.keyboard.press('Escape')
    page.wait_for_timeout(300)
    # Type "Nia" into the same face (photo_076, left bbox)
    card = face_card_by_image(page, 'photo_076.jpg', 'left')
    input_el = card.locator('.face-card-input')
    input_el.click()
    input_el.fill('Nia')
    wait_for_idle(page)
    face_id = card.get_attribute('data-id')
    highlight_element(page, f'[data-id="{face_id}"] .face-card-input')


@step('more-people')
def step_faces_more_people(page, ctx):
    page.keyboard.press('Enter')
    # Wait for Nia's person card to appear (we already have Alice + ignored,
    # so wait until there are at least 3 person-card images).
    page.wait_for_function(
        """() => {
        const cards = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return cards.length >= 3;
    }""",
        timeout=10000,
    )
    # Force-load lazy person-card thumbnails by re-setting their src,
    # then wait until every image has actually decoded
    page.evaluate("""() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }""")
    page.wait_for_function(
        """() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length >= 3
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }""",
        timeout=5000,
    )
    page.wait_for_timeout(300)


@step('drag-and-drop')
def step_faces_drag_and_drop(page, ctx):
    # Ensure person card thumbnails are loaded (lazy images may still be pending)
    page.evaluate("""() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }""")
    page.wait_for_function(
        """() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }""",
        timeout=5000,
    )

    # Draw an SVG arrow from the remaining ignore face (photo_240) to the
    # '-' ignore person card in the Known People section.
    arrow_card = face_card_by_image(page, 'photo_240.jpg')
    arrow_face_id = arrow_card.get_attribute('data-id')
    ctx['arrow_face_id'] = arrow_face_id
    page.evaluate(f"""() => {{
        const source = document.querySelector(
            '.face-card[data-id="{arrow_face_id}"]');
        const target = document.querySelector(
            '.faces-section.known .person-card');
        if (!source || !target) return;

        const sr = source.getBoundingClientRect();
        const tr = target.getBoundingClientRect();

        // Create SVG arrow overlay
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('data-tutorial-highlight', '');
        svg.style.cssText = `
            position: fixed; top: 0; left: 0;
            width: 100vw; height: 100vh;
            pointer-events: none; z-index: 10000;
        `;

        // Arrow marker
        const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
        const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
        marker.setAttribute('id', 'tutorial-arrowhead');
        marker.setAttribute('markerWidth', '10');
        marker.setAttribute('markerHeight', '7');
        marker.setAttribute('refX', '10');
        marker.setAttribute('refY', '3.5');
        marker.setAttribute('orient', 'auto');
        const polygon = document.createElementNS(
            'http://www.w3.org/2000/svg', 'polygon');
        polygon.setAttribute('points', '0 0, 10 3.5, 0 7');
        polygon.setAttribute('fill', '#ff6000');
        marker.appendChild(polygon);
        defs.appendChild(marker);
        svg.appendChild(defs);

        // Curved line from source centre to target centre
        const sx = sr.left + sr.width / 2;
        const sy = sr.top + sr.height / 2;
        const tx = tr.left + tr.width / 2;
        const ty = tr.top + tr.height / 2;
        const cx = (sx + tx) / 2 - 60;
        const cy = (sy + ty) / 2;

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', `M ${{sx}} ${{sy}} Q ${{cx}} ${{cy}} ${{tx}} ${{ty}}`);
        path.setAttribute('stroke', '#ff6000');
        path.setAttribute('stroke-width', '3');
        path.setAttribute('fill', 'none');
        path.setAttribute('marker-end', 'url(#tutorial-arrowhead)');
        svg.appendChild(path);

        document.body.appendChild(svg);
    }}""")
    wait_for_idle(page)


@step('view-all')
def step_faces_view_all(page, ctx):
    # Ignore the face shown with the arrow in the previous step (photo_240).
    # Single-face ignore skips the confirmation dialog and acts immediately.
    arrow_id = ctx.get('arrow_face_id', '')
    card = page.locator(f'.face-card[data-id="{arrow_id}"]')
    card.scroll_into_view_if_needed()
    click_face_id(page, arrow_id)
    page.wait_for_timeout(200)
    card.hover()
    page.wait_for_timeout(300)
    card.locator('.face-card-ignore').click()
    page.wait_for_timeout(800)

    # Click once first so the person is visibly selected, then double-click
    # to enter pick-preferred mode
    person = page.locator('.faces-section.known .person-card').first
    person.click()
    page.wait_for_timeout(300)
    person.dblclick()
    # Wait for the pick-preferred grid to appear and thumbnails to load
    page.wait_for_selector('.face-card-star', timeout=5000)
    page.wait_for_timeout(1000)


@step('preferred-face')
def step_faces_preferred_face(page, ctx):
    # Click the star on a face
    page.locator('.face-card-star').first.click()
    wait_for_idle(page)


@step('locking-and-unlocking')
def step_faces_locking_and_unlocking(page, ctx):
    # Toggle a padlock that is NOT on the preferred face. The preferred face's
    # sibling star has the .preferred class; unlocking it shows an error dialog
    # that persists across subsequent screenshots.
    page.evaluate("""() => {
        const cards = document.querySelectorAll('.face-card-padlock');
        for (const padlock of cards) {
            const card = padlock.closest('.face-card');
            if (!card) continue;
            const star = card.querySelector('.face-card-star.preferred');
            if (!star) { padlock.click(); return; }
        }
    }""")
    wait_for_idle(page)


@step('faces-exiting')
def step_faces_exiting(page, ctx):
    click_toolbar(page, 'btn-faces-focus-person')
    page.wait_for_timeout(800)


# =========================================================================
# Section 7: FACE TAGGING IN FULL-SCREEN
# =========================================================================
section('fullscreen-tagging')


@step('tagging-opening')
def step_tagging_opening(page, ctx):
    navigate_to(page, 'search')


@step('people-picker')
def step_tagging_people_picker(page, ctx):
    page.click('#btn-people-picker')
    page.wait_for_timeout(500)


@step('people-picker-select')
def step_tagging_people_picker_select(page, ctx):
    # Wait for picker to open, then select Alice so the screenshot shows
    # the dialog with a person already selected
    page.wait_for_selector('#dialog-people-picker[open]', state='visible', timeout=5000)
    wait_for_idle(page)
    page.locator('.people-picker-item:has-text("Alice")').click()
    wait_for_idle(page)


@step('applying-people-filter')
def step_tagging_applying_people_filter(page, ctx):
    # Confirm the picker, then apply the filter
    page.click('#dialog-people-done')
    page.wait_for_timeout(500)
    page.click('#btn-apply-filter')
    page.wait_for_selector('#screen-gallery', state='visible', timeout=5000)
    wait_for_idle(page)
    wait_for_thumbnails(page)


@step('tagging-fullscreen')
def step_tagging_fullscreen(page, ctx):
    gallery_item_by_name(page, 'photo_075.jpg').dblclick()
    page.wait_for_selector('#fullscreen-overlay.visible', timeout=5000)
    wait_for_idle(page)


@step('enabling-face-tagging')
def step_tagging_enabling(page, ctx):
    highlight_element(page, '#fullscreen-tagging')
    page.click('#fullscreen-tagging')
    page.wait_for_timeout(800)


@step('tagging-naming')
def step_tagging_naming(page, ctx):
    # Navigate through photos until we find one with an unknown face.
    # The filter shows photos of Alice (known), but some of those photos
    # may also contain other people who haven't been named yet.
    for _ in range(20):
        if page.locator('.face-box.unknown').count() > 0:
            break
        page.keyboard.press('ArrowRight')
        page.wait_for_timeout(600)
    red_box = page.locator('.face-box.unknown').first
    red_box.click()
    wait_for_idle(page)


@step('tagging-ignoring')
def step_tagging_ignoring(page, ctx):
    # Press Escape to cancel any active input from the previous step
    page.keyboard.press('Escape')
    page.wait_for_timeout(300)
    # Navigate until we find a photo with an unknown face (previous step
    # may have named the only unknown on the previous photo)
    for _ in range(20):
        if page.locator('.face-box.unknown').count() > 0:
            break
        page.keyboard.press('ArrowRight')
        page.wait_for_timeout(600)
    unknown = page.locator('.face-box.unknown').first
    unknown.hover()
    page.wait_for_timeout(500)


# =========================================================================
# Section 8: GALLERY EXTRAS
# =========================================================================
section('gallery-extras')


@step('sorting-by-people')
def step_extras_sorting_by_people(page, ctx):
    # Close fullscreen if open
    page.keyboard.press('Escape')
    page.wait_for_timeout(300)
    # Clear filter first
    try:
        click_toolbar(page, 'btn-clear-filter')
        page.wait_for_timeout(500)
    except Exception:
        pass
    click_toolbar(page, 'btn-sort-people')
    page.wait_for_timeout(1000)
    wait_for_thumbnails(page)


@step('switching-themes')
def step_extras_switching_themes(page, ctx):
    # Bypass AppState and set theme directly via DOM + localStorage.
    # AppState.view.setTheme() has an early-return guard that can silently
    # no-op if its internal _theme variable is out of sync with the DOM.
    page.evaluate("""() => {
        document.getElementById('app').dataset.theme = 'light';
        localStorage.setItem('photonarium-theme', '"light"');
    }""")
    page.wait_for_timeout(500)
    wait_for_thumbnails(page)
    # Highlight the toggle button
    highlight_element(page, '#btn-theme')


@step('larger-thumbnails')
def step_extras_larger_thumbnails(page, ctx):
    # Make thumbnails larger by clicking the larger button a few times
    click_toolbar(page, 'btn-thumb-larger')
    click_toolbar(page, 'btn-thumb-larger')
    click_toolbar(page, 'btn-thumb-larger')
    page.wait_for_timeout(500)
    wait_for_thumbnails(page)


@step('smaller-thumbnails')
def step_extras_smaller_thumbnails(page, ctx):
    # Make thumbnails smaller — go back past default to show a denser grid
    for _ in range(6):
        click_toolbar(page, 'btn-thumb-smaller')
    page.wait_for_timeout(500)
    wait_for_thumbnails(page)


# =========================================================================
# Section 9: USE IT ANYWHERE
# =========================================================================
section('anywhere')

manual_step('mobile-landscape', 'mobile-landscape.png')
manual_step('mobile-portrait', 'mobile-portrait.png')


# =========================================================================
# Server lifecycle
# =========================================================================


def setup_tutorials_dir():
    """Clean and prepare the tutorials output directory.

    Copies the generated demo data (DB, config, thumbnails, model files)
    from SCRIPT_DIR into TUTORIALS_DIR, and copies setup screenshots from
    SETUP_CACHE_DIR into the screenshots directory so that ``setup_step()``
    paths resolve correctly.
    """
    if TUTORIALS_DIR.exists():
        print(f'  Removing existing {TUTORIALS_DIR.name}/ ...')
        shutil.rmtree(TUTORIALS_DIR)

    TUTORIALS_DIR.mkdir()
    SCREENSHOTS_DIR.mkdir()

    # Copy generated demo data for the server to use
    for name in [
        'photonarium.db',
        'photonarium.db-wal',
        'photonarium.db-shm',
        'photonarium.yml',
        '.laion-aesthetic-head.pth',
        '.nima-mobilenetv2-ava.pth',
    ]:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, TUTORIALS_DIR / name)

    # Copy thumbnail cache
    thumb_src = SCRIPT_DIR / '.thumbnails'
    if thumb_src.exists():
        shutil.copytree(thumb_src, TUTORIALS_DIR / '.thumbnails')

    # Copy setup screenshots into the screenshots directory so that
    # setup_step() paths (screenshots/0-1.png etc.) resolve correctly
    if SETUP_CACHE_DIR.exists():
        for png in SETUP_CACHE_DIR.glob('*.png'):
            shutil.copy2(png, SCREENSHOTS_DIR / png.name)

    # Copy manual screenshots (mobile-landscape.png, mobile-portrait.png)
    manual_src = SCRIPT_DIR / 'manual'
    if manual_src.exists():
        shutil.copytree(manual_src, MANUAL_DIR)

    print(f'  Prepared {TUTORIALS_DIR.name}/ with demo data')


def start_server():
    """Start the Photonarium backend against the tutorials data directory.

    Uses --config to point at the demo config file inside TUTORIALS_DIR
    (config no longer lives inside the data directory by default) and
    --data-dir as a runtime override so the server reads demo data.

    Returns:
        Tuple of (process, log_file_handle).  The caller must close the
        file handle after stopping the server.
    """
    log_path = TUTORIALS_DIR / 'server.log'
    log_fh = open(log_path, 'w', encoding='utf-8')  # noqa: SIM115
    cmd = [
        sys.executable,
        str(PROJECT_DIR / 'app' / 'app.py'),
        '--config',
        str(TUTORIALS_DIR / 'photonarium.yml'),
        '--data-dir',
        str(TUTORIALS_DIR),
        '--port',
        str(SERVER_PORT),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    print(f'  Server log: {log_path}')
    return proc, log_fh


def wait_for_server():
    """Poll the server until it responds."""
    deadline = time.time() + SERVER_STARTUP_TIMEOUT
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f'{SERVER_URL}/api/status', timeout=2)
            if resp.status == 200:
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.5)
    raise TimeoutError(f'Server did not start within {SERVER_STARTUP_TIMEOUT}s')


def stop_server(proc):
    """Terminate the backend server."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# =========================================================================
# Setup mode (--setup)
# =========================================================================


def _wait_for_processing(timeout=600, stable_count=3):
    """Poll ``/api/status`` until all backend processing completes.

    Processing is done when the status is ``'up_to_date'`` and all queue
    counts are zero.  To avoid a race between pipeline stages (e.g.
    embeddings finishing and face detection being queued by the completion
    callback), the idle state must persist for *stable_count* consecutive
    polls before we consider processing truly complete.
    """
    deadline = time.time() + timeout
    start = time.time()
    consecutive_idle = 0
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(f'{SERVER_URL}/api/status', timeout=5)
            envelope = json.loads(resp.read())
            data = envelope.get('data', {})
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError):
            consecutive_idle = 0
            time.sleep(2)
            continue

        status = data.get('status', '')
        queues = {
            k: data.get(k, 0)
            for k in (
                'indexing_queue',
                'embedding_queue',
                'face_queue',
                'nima_queue',
                'trash_queue',
            )
        }
        # Phase-4 operations are signalled by their key being present
        phase4 = [k for k in ('duplicates', 'face_grouping', 'face_embeddings', 'face_reassess') if k in data]

        total_queued = sum(queues.values())
        elapsed = int(time.time() - start)
        parts = [f'{k}={v}' for k, v in queues.items() if v]
        queue_str = ', '.join(parts) if parts else 'all clear'
        phase4_str = f', active: {", ".join(phase4)}' if phase4 else ''
        idle_str = f'  (idle {consecutive_idle}/{stable_count})' if consecutive_idle else ''
        print(
            f'\r  [{elapsed}s] status={status}  queues: {queue_str}{phase4_str}{idle_str}    ',
            end='',
            flush=True,
        )

        if status == 'up_to_date' and total_queued == 0 and not phase4:
            consecutive_idle += 1
            if consecutive_idle >= stable_count:
                print()  # newline after progress
                return
        else:
            consecutive_idle = 0

        time.sleep(2)

    print()
    raise TimeoutError(f'Processing did not complete within {timeout}s')


def run_setup():
    """Initialise the tutorial data directory and capture setup screenshots.

    This is a one-time operation that:
    1. Creates ``photonarium.yml`` with data_dir pointing at SCRIPT_DIR
    2. Downloads ML model files into SCRIPT_DIR
    3. Starts the server against an empty database
    4. Captures the "Getting Started" screenshots via Playwright
    5. Composites the folder-picker overlay screenshot
    6. Adds a folder and waits for processing to complete
    7. Stops the server

    The captured screenshots are saved to ``SETUP_CACHE_DIR`` and later
    copied into the tutorial output by :func:`setup_tutorials_dir`.
    """
    from PIL import Image

    print('Photonarium Tutorial Setup')
    print('=' * 40)

    # ------------------------------------------------------------------
    # Step 1 — Init config
    # ------------------------------------------------------------------
    print('\n[1/7] Initialising config...')
    config_path = SCRIPT_DIR / 'photonarium.yml'
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_DIR / 'app' / 'app.py'),
            '--init-config',
            str(SCRIPT_DIR),
            '--config',
            str(config_path),
        ],
        cwd=str(PROJECT_DIR),
        check=True,
    )
    print(f'  Created {config_path}')

    # ------------------------------------------------------------------
    # Step 2 — Download models
    # ------------------------------------------------------------------
    print('\n[2/7] Downloading models...')
    subprocess.run(
        [
            sys.executable,
            str(PROJECT_DIR / 'download_models.py'),
            '--data-dir',
            str(SCRIPT_DIR),
            '--config',
            str(config_path),
        ],
        cwd=str(PROJECT_DIR),
        check=True,
    )

    # ------------------------------------------------------------------
    # Step 3 — Start server (empty DB, no --scan)
    # ------------------------------------------------------------------
    print('\n[3/7] Starting server (empty database)...')
    # Remove any existing DB so we start fresh
    for db_file in SCRIPT_DIR.glob('photonarium.db*'):
        db_file.unlink()
    server_log = SETUP_CACHE_DIR / 'server.log'
    server_log_fh = open(server_log, 'w', encoding='utf-8')  # noqa: SIM115
    cmd = [
        sys.executable,
        str(PROJECT_DIR / 'app' / 'app.py'),
        '--config',
        str(config_path),
        '--data-dir',
        str(SCRIPT_DIR),
        '--port',
        str(SERVER_PORT),
    ]
    server = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=server_log_fh,
        stderr=subprocess.STDOUT,
    )
    print(f'  Server log: {server_log}')

    try:
        # ------------------------------------------------------------------
        # Step 4 — Wait for server ready
        # ------------------------------------------------------------------
        print('\n[4/7] Waiting for server...')
        wait_for_server()
        print(f'  Server ready at {SERVER_URL}')

        # ------------------------------------------------------------------
        # Step 5 — Capture setup screenshots
        # ------------------------------------------------------------------
        print('\n[5/7] Capturing setup screenshots...')
        SETUP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport=VIEWPORT)
            page = context.new_page()
            page.goto(SERVER_URL)
            page.wait_for_timeout(1000)

            # (a) Set light theme
            page.evaluate("""() => {
                document.getElementById('app').dataset.theme = 'light';
                localStorage.setItem('photonarium-theme', '"light"');
            }""")
            page.wait_for_timeout(500)

            # (b) Capture 0-1.png — light theme, empty Database screen
            page.screenshot(path=str(SETUP_CACHE_DIR / '0-1.png'))
            print('  Captured 0-1.png (light theme)')

            # (c) Set dark theme
            page.evaluate("""() => {
                document.getElementById('app').dataset.theme = 'dark';
                localStorage.setItem('photonarium-theme', '"dark"');
            }""")
            page.wait_for_timeout(500)

            # (d) Capture 0-2.png — dark theme, empty Database screen
            # Highlight the "Add Folder" button so the user knows what to click
            highlight_element(page, '#btn-add-folder')
            page.screenshot(path=str(SETUP_CACHE_DIR / '0-2.png'))
            remove_highlights(page)
            print('  Captured 0-2.png (dark theme)')

            # (e) Capture background for composite, then build 0-3.png
            bg_path = SETUP_CACHE_DIR / '_bg_dark_db.png'
            page.screenshot(path=str(bg_path))
            # (f) Composite the OS picker crop onto the dark background
            overlay_path = SCRIPT_DIR / 'manual' / 'os-picker-crop.png'
            if overlay_path.exists():
                bg = Image.open(bg_path)
                overlay = Image.open(overlay_path).convert('RGBA')
                x = (bg.width - overlay.width) // 2
                y = (bg.height - overlay.height) // 2
                bg.paste(overlay, (x, y), overlay)
                bg.save(SETUP_CACHE_DIR / '0-3.png')
                print('  Composited 0-3.png (folder picker)')
            else:
                print(f'  WARNING: {overlay_path} not found, skipping 0-3.png composite')

            # (g) Add folder via API
            examples_dir = str((SCRIPT_DIR / 'examples').resolve())
            req = urllib.request.Request(
                f'{SERVER_URL}/api/folders',
                data=json.dumps({'path': examples_dir}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                body = json.loads(resp.read())
                print(f'  Added folder: {examples_dir} -> {body}')
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors='replace')
                raise RuntimeError(f'Failed to add folder ({e.code}): {body}') from e

            # Tell the frontend to refresh its folder list (don't reload
            # the page — a full reload after images exist would land on
            # Gallery instead of Database, and processing can finish in
            # seconds on a fast GPU).
            page.evaluate('() => AppState.folders.load()')
            page.wait_for_selector('.folder-item', timeout=10000)
            page.wait_for_timeout(500)

            # (h) Capture 0-4.png — folder added, scan starting
            page.screenshot(path=str(SETUP_CACHE_DIR / '0-4.png'))
            print('  Captured 0-4.png (folder added)')

            # (i) Wait for status bar to show processing, capture 0-5.png.
            # On fast GPUs processing may finish before we get here — that's
            # OK, we'll still have the Database screen with the folder.
            page.wait_for_timeout(2000)
            page.screenshot(path=str(SETUP_CACHE_DIR / '0-5.png'))
            print('  Captured 0-5.png (importing)')

            browser.close()

        # ------------------------------------------------------------------
        # Step 6 — Wait for processing to complete
        # ------------------------------------------------------------------
        print('\n[6/7] Waiting for processing to complete...')
        _wait_for_processing()
        print('  Processing complete')

    finally:
        # ------------------------------------------------------------------
        # Step 7 — Stop server
        # ------------------------------------------------------------------
        print('\n[7/7] Stopping server...')
        stop_server(server)
        server_log_fh.close()
        print(f'  Server log saved to: {server_log}')

    # Clean up temporary background screenshot
    bg_temp = SETUP_CACHE_DIR / '_bg_dark_db.png'
    if bg_temp.exists():
        bg_temp.unlink()

    print(f'\nSetup complete! Generated data in: {SCRIPT_DIR}')
    print(f'Setup screenshots in: {SETUP_CACHE_DIR}')


# =========================================================================
# HTML generation
# =========================================================================


def generate_html():
    """
    Generate a single-page slideshow tutorial from captured screenshots.

    Produces one ``index.html`` with all CSS/JS inline.  The slideshow uses
    two alternating slide layers for cross-fade transitions, keyboard
    navigation, browser TTS narration, a progress bar, slide counter, and
    a table-of-contents overlay.
    """
    # ------------------------------------------------------------------
    # Build the slides JSON array from the step/section registries
    # ------------------------------------------------------------------
    section_info = {s['number']: s for s in SECTIONS}
    slides = []
    for s in STEPS:
        sec = section_info[s['section']]
        slides.append(
            {
                'section': sec['title'],
                'sectionNum': sec['number'],
                'stepId': s['id'],
                'title': s['title'],
                'caption': s['caption'],
                'screenshot': s['screenshot'],
            }
        )

    slides_json = json.dumps(slides)
    total = len(slides)

    # ------------------------------------------------------------------
    # Build the TOC entries (section number → title + first slide index)
    # ------------------------------------------------------------------
    toc_entries = []
    seen_sections = set()
    for idx, sl in enumerate(slides):
        if sl['sectionNum'] not in seen_sections:
            seen_sections.add(sl['sectionNum'])
            # Find the last slide index in this section
            last_idx = idx
            for j in range(idx, total):
                if slides[j]['sectionNum'] == sl['sectionNum']:
                    last_idx = j
            toc_entries.append(
                {
                    'title': sl['section'],
                    'first': idx,
                    'range': f'{slides[idx]["stepId"]} – {slides[last_idx]["stepId"]}',
                }
            )

    toc_json = json.dumps(toc_entries)

    # ------------------------------------------------------------------
    # Load the HTML template and inject slide data
    # ------------------------------------------------------------------
    template_path = SCRIPT_DIR / 'template.html'
    html = template_path.read_text(encoding='utf-8')
    html = html.replace('{{SLIDES_JSON}}', slides_json)
    html = html.replace('{{TOC_JSON}}', toc_json)
    html = html.replace('{{TOTAL}}', str(total))

    with open(TUTORIALS_DIR / 'index.html', 'w', encoding='utf-8') as f:
        f.write(html)

    print(f'  Generated slideshow index.html ({total} slides)')


# =========================================================================
# Main
# =========================================================================


def main():
    print('Photonarium Tutorial Generator')
    print('=' * 40)

    # 1. Prepare output directory
    print('\n[1/4] Preparing tutorials directory...')
    setup_tutorials_dir()

    # 2. Start server
    print('\n[2/4] Starting Photonarium server...')
    server, server_log_fh = start_server()
    try:
        wait_for_server()
        print(f'  Server ready at {SERVER_URL}')

        # 3. Run Playwright
        auto_steps = sum(1 for s in STEPS if s['action'] is not None)
        print(f'\n[3/4] Capturing {auto_steps} screenshots...')
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport=VIEWPORT)
            page = context.new_page()
            page.goto(SERVER_URL)
            page.wait_for_timeout(1000)

            # Set dark theme (all screenshots use dark theme except 7.2)
            page.evaluate("""() => {
                document.getElementById('app').dataset.theme = 'dark';
                localStorage.setItem('photonarium-theme', '"dark"');
            }""")
            page.wait_for_timeout(300)

            ctx = {}  # shared context across steps
            current_section = None

            for step_def in STEPS:
                # Skip/stop sections for faster debugging
                if START_FROM_SECTION is not None and step_def['section'] < START_FROM_SECTION:
                    continue
                if STOP_AFTER_SECTION is not None and step_def['section'] > STOP_AFTER_SECTION:
                    print(f'\n  (stopping after section {STOP_AFTER_SECTION})')
                    break

                if step_def['section'] != current_section:
                    current_section = step_def['section']
                    sec_info = next(s for s in SECTIONS if s['number'] == current_section)
                    print(f'\n  --- {sec_info["title"]} ---')

                step_id = step_def['id']
                print(f'  [{step_id}] {step_def["title"]}')

                # Manual steps use pre-existing screenshots — skip them
                if step_def['action'] is None:
                    continue

                try:
                    step_def['action'](page, ctx)
                    page.wait_for_timeout(200)

                    # Screenshot first, THEN remove highlights so they
                    # appear in the screenshot but don't leak to next step
                    path = TUTORIALS_DIR / step_def['screenshot']
                    page.screenshot(path=str(path))
                    remove_highlights(page)
                except Exception as e:
                    print(f'    ERROR: {e}')
                    # Save a screenshot anyway for debugging
                    path = TUTORIALS_DIR / step_def['screenshot']
                    try:
                        page.screenshot(path=str(path))
                    except Exception:
                        pass

            browser.close()

    finally:
        # 4. Stop server
        print('\n[4/4] Stopping server...')
        stop_server(server)
        server_log_fh.close()

    # Generate slideshow HTML
    print('\nGenerating slideshow...')
    generate_html()

    print(f'\nDone! Tutorial output in: {TUTORIALS_DIR}')
    print(f'Open {TUTORIALS_DIR / "index.html"} in a browser to view.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate the Photonarium tutorial')
    parser.add_argument(
        '--setup',
        action='store_true',
        help='Initialise the tutorial data directory and capture setup screenshots',
    )
    parser.add_argument(
        '-f',
        '--from-section',
        type=int,
        default=None,
        metavar='N',
        help='Start from this section number (skip earlier sections)',
    )
    parser.add_argument(
        '-t',
        '--to-section',
        type=int,
        default=None,
        metavar='N',
        help='Stop after this section number (skip later sections)',
    )
    args = parser.parse_args()

    if args.setup:
        run_setup()
    else:
        START_FROM_SECTION = args.from_section
        STOP_AFTER_SECTION = args.to_section
        main()
