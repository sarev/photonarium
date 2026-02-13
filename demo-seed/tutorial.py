#!/usr/bin/env python3
"""
Tutorial Generator for Photonarium
=================================

Automates screenshot capture and HTML tutorial generation using Playwright.
Reads the frozen demo-seed database, starts the real Photonarium backend,
drives the browser through each tutorial step, captures screenshots, and
generates static HTML pages.

Prerequisites:
    pip install playwright
    playwright install chromium

Usage (from the project root):
    python demo-seed/tutorial.py

Creates a 'tutorials' folder next to 'demo-seed' with:
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
import urllib.request
import urllib.error
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

SCRIPT_DIR = Path(__file__).resolve().parent          # demo-seed/
PROJECT_DIR = SCRIPT_DIR.parent                       # project root
TUTORIALS_DIR = PROJECT_DIR / 'tutorials'
SCREENSHOTS_DIR = TUTORIALS_DIR / 'screenshots'
MANUAL_DIR = TUTORIALS_DIR / 'manual'

# ---------------------------------------------------------------------------
# Server configuration
# ---------------------------------------------------------------------------

SERVER_PORT = 5111          # non-standard port to avoid clashing
SERVER_URL = f'http://localhost:{SERVER_PORT}'
SERVER_STARTUP_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Viewport and timing
# ---------------------------------------------------------------------------

VIEWPORT = {'width': 1280, 'height': 800}
SETTLE_MS = 400             # ms to wait after actions for animations

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

_current_section = -1   # auto-incremented by section()
_step_counter = 0       # reset to 0 by section(), incremented by step/manual_step

def section(key):
    """Register a new section.  Number is auto-assigned from position in script.json."""
    global _current_section, _step_counter
    _current_section += 1
    _step_counter = 0
    text = _SCRIPT['sections'][_current_section]
    assert text['key'] == key, (
        f"Section key mismatch at position {_current_section}: "
        f"expected '{text['key']}', got '{key}'")
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
        STEPS.append({
            'section': _current_section,
            'id': step_id,
            'title': text['title'],
            'caption': text['caption'],
            'action': fn,
            'screenshot': f'screenshots/{step_id.replace(".", "-")}.png',
        })
        return fn
    return decorator

def manual_step(key, filename):
    """Register a step with a pre-existing screenshot.  Auto-numbered like step()."""
    global _step_counter
    _step_counter += 1
    text = _STEP_TEXT[key]
    step_id = f'{_current_section}.{_step_counter}'
    STEPS.append({
        'section': _current_section,
        'id': step_id,
        'title': text['title'],
        'caption': text['caption'],
        'action': None,
        'screenshot': f'manual/{filename}',
    })


# =========================================================================
# Helper functions (available to step actions)
# =========================================================================

def wait_for_thumbnails(page, selector='.gallery-item img[src]', count=1):
    """Wait until at least `count` thumbnail images have loaded."""
    page.wait_for_selector(selector, timeout=10000)
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

def nth_gallery_item(page, n):
    """Get the nth (1-based) visible gallery item."""
    return page.locator(f'.gallery-item:nth-child({n})')

def gallery_item_by_name(page, filename):
    """Get a gallery item by its filename label."""
    return page.locator('.gallery-item').filter(has_text=filename)

def nth_face_card(page, n):
    """
    Get the nth (1-based) face card by visual position in the unknown section.

    VirtualGrid appends cards asynchronously as thumbnails load, so DOM order
    may not match visual grid order.  We sort by bounding rect (top then left)
    and return a locator pinned to the card's data-id.
    """
    face_id = page.evaluate(f'''() => {{
        const cards = [...document.querySelectorAll(
            '.faces-unknown-container .face-card')];
        cards.sort((a, b) => {{
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
            return ar.left - br.left;
        }});
        return cards[{n - 1}]?.dataset?.id || null;
    }}''')
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
    result = page.evaluate(f'''() => {{
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
    }}''')
    if not result:
        raise ValueError(f'No face card found at visual position {n}')
    return page.locator(f'.face-card[data-id="{result}"]')

def highlight_element(page, selector, color='rgba(255, 96, 0, 0.35)', width='3px'):
    """
    Inject a temporary highlight outline on an element.
    Removed automatically after the screenshot by the main loop.
    """
    page.evaluate(f'''() => {{
        const el = document.querySelector('{selector}');
        if (el) {{
            el.dataset.tutorialHighlight = el.style.outline || '';
            el.style.outline = '{width} solid {color}';
        }}
    }}''')

def spotlight_element(page, selector, darkness='rgba(0, 0, 0, 0.55)'):
    """
    Darken everything except the target element, creating a spotlight effect.
    Uses a huge box-shadow spread to simulate a page-wide overlay while the
    element itself remains visible above the shadow.
    """
    page.evaluate(f'''() => {{
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
    }}''')

def remove_highlights(page):
    """Remove all tutorial highlights, spotlights, and SVG overlays."""
    page.evaluate('''() => {
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
    }''')


# =========================================================================
# Section 0: GETTING STARTED (manual screenshots)
# =========================================================================
section('getting-started')

manual_step('first-launch', '00-light-theme.png')
manual_step('dark-theme', '01-start-screen-.png')
manual_step('adding-images', '02-folder-picker-dialogue.png')
manual_step('indexing', '03-added-folder.png')
manual_step('processing', '04-importing.png')


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
    page.wait_for_selector('#info-generate-caption-btn', state='visible',
                           timeout=5000)
    # Click the sparkle button to generate a caption
    page.click('#info-generate-caption-btn')
    # Wait for caption to populate (backend ML — may take a while)
    page.wait_for_function(
        '() => document.getElementById("info-description")?.value?.length > 0',
        timeout=30000)
    wait_for_idle(page)
    # Highlight the sparkle button so the screenshot shows what was clicked
    highlight_element(page, '#info-generate-caption-btn',
                      color='rgba(255, 180, 0, 0.6)', width='3px')

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
    highlight_element(page, '#filter-similarity',
                      color='rgba(255, 180, 0, 0.5)', width='2px')

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
    highlight_element(page, '#btn-clear-filter',
                      color='rgba(255, 180, 0, 0.6)', width='3px')


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
    page.wait_for_selector('.duplicate-stack', timeout=10000)
    wait_for_idle(page)

@step('groups-toolbar')
def step_groups_toolbar(page, ctx):
    highlight_element(page, '#toolbar')

@step('strictness')
def step_groups_strictness(page, ctx):
    # Move the similarity slider to "Related" level.
    # Slider is inverted: position 0=Custom, 1=Directories, 2=Related, 3=Similar, 4=Near-identical, 5=Identical
    # Remove existing stacks first so wait_for_selector only matches fresh ones
    # (avoids race where stale stacks from the previous level match instantly).
    page.evaluate('''() => {
        document.querySelectorAll('.duplicate-stack').forEach(el => el.remove());
        const slider = document.querySelector('#similarity-slider');
        if (slider) {
            slider.value = 2;
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            slider.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }''')
    page.wait_for_selector('.duplicate-stack', timeout=15000)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider',
                      color='rgba(255, 180, 0, 0.5)', width='2px')

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
    # We're in Gallery group-view mode where both btn-back-gallery
    # and btn-duplicates are hidden.  Route via Database.
    navigate_to(page, 'database')
    navigate_to(page, 'duplicates')
    page.wait_for_selector('.duplicate-stack', timeout=10000)
    wait_for_idle(page)
    # Cache the 4th group's hash (aurora photos) for use in section 5.
    # Must use Duplicates.state.groups (display order) rather than
    # nth-child, because VirtualGrid appends DOM elements in thumbnail-
    # load order which doesn't match visual position.
    ctx['aurora_group_hash'] = page.evaluate(
        '() => Duplicates.state.groups[3]?.group_hash || ""')
    highlight_element(page, '#btn-dup-prune',
                      color='rgba(255, 180, 0, 0.6)', width='3px')

@step('pruning-dialog')
def step_groups_pruning_dialog(page, ctx):
    # Open the prune dialog without actually pruning — we just want
    # to show what it looks like.  Don't click Trash!
    page.click('#btn-dup-prune')
    page.wait_for_selector('#dialog-prune[open]', timeout=5000)
    wait_for_idle(page)

@step('directories')
def step_groups_directories(page, ctx):
    # Close the prune dialog from the previous step
    page.click('#dialog-prune-cancel')
    page.wait_for_timeout(200)
    # Move slider to Directories (position 1)
    page.evaluate('''() => {
        const slider = document.querySelector('#similarity-slider');
        if (slider) {
            slider.value = 1;
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            slider.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }''')
    # Wait for groups to load — directory groups may or may not exist yet
    # depending on whether the demo-seed has subdirectories.
    try:
        page.wait_for_selector('#loading-overlay.visible', timeout=2000)
        page.wait_for_selector('#loading-overlay:not(.visible)', timeout=15000)
    except Exception:
        pass
    # Give the level switch time to settle even if there are no stacks
    page.wait_for_timeout(1000)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider',
                      color='rgba(255, 180, 0, 0.5)', width='2px')


# =========================================================================
# Section 5: CUSTOM GROUPS (ALBUMS)
# =========================================================================
section('custom-groups')

@step('custom-level')
def step_custom_groups_custom_level(page, ctx):
    # Navigate to Groups via Database (always visible) to ensure
    # a clean onLeave/onEnter cycle regardless of starting screen.
    navigate_to(page, 'database')
    navigate_to(page, 'duplicates')
    page.wait_for_timeout(300)
    # Move slider to Custom (position 0).  Remove stale stacks first
    # so we don't race with the level transition.
    # Custom level starts empty (no groups yet) so we just wait a beat
    # rather than waiting for stacks.
    page.evaluate('''() => {
        document.querySelectorAll('.duplicate-stack').forEach(el => el.remove());
        const slider = document.querySelector('#similarity-slider');
        if (slider) {
            slider.value = 0;
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            slider.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }''')
    page.wait_for_timeout(500)
    wait_for_idle(page)
    highlight_element(page, '#similarity-slider',
                      color='rgba(255, 180, 0, 0.5)', width='2px')

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
    # Navigate to the Groups screen and open the aurora group (cached
    # in step 4.7) — a perfect match for the "Aurora" custom group.
    navigate_to(page, 'database')
    navigate_to(page, 'duplicates')
    page.evaluate('''() => {
        document.querySelectorAll('.duplicate-stack').forEach(el => el.remove());
        const slider = document.querySelector('#similarity-slider');
        if (slider) {
            slider.value = 2;
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            slider.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }''')
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
    highlight_element(page, '.gallery-item-group-btn',
                      color='rgba(206, 147, 216, 0.6)', width='2px')

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
    highlight_element(page, '.entity-picker-content',
                      color='rgba(206, 147, 216, 0.4)', width='2px')

@step('managing-groups')
def step_custom_groups_managing_groups(page, ctx):
    # Add the selected aurora photos to the Aurorae group via the picker
    # left open by the previous step.  Click the Aurorae entry then Done.
    page.locator('.entity-picker-item:has-text("Aurora")').click()
    page.wait_for_timeout(300)
    page.click('#dialog-group-done')
    page.wait_for_timeout(500)

    # Create another group so this step has multiple to show.
    # The await ensures backend persistence completes before we navigate.
    # createGroup() does an optimistic cache update followed by a forced
    # loadLevel(5, true) reload, so _groupCache[5] is correct when this
    # returns.  We do NOT invalidate afterwards — that would clear the
    # valid cache and force a redundant re-fetch.
    page.evaluate('''async () => {
        const images = AppState.images.getAll();
        if (images.length < 10) return;

        const setB = images.slice(5, 10).map(i => i.id);

        await AppState.duplicates.createGroup('Holiday snaps', setB);
    }''')

    # Navigate to Groups screen and switch to Custom level.
    navigate_to(page, 'database')
    navigate_to(page, 'duplicates')
    page.evaluate('''() => {
        document.querySelectorAll('.duplicate-stack').forEach(el => el.remove());
        const slider = document.querySelector('#similarity-slider');
        if (slider) {
            slider.value = 0;
            slider.dispatchEvent(new Event('input', { bubbles: true }));
            slider.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }''')
    # Wait for the custom group stacks to render
    page.wait_for_selector('.duplicate-stack', timeout=10000)
    wait_for_idle(page)
    highlight_element(page, '#toolbar')


# =========================================================================
# Section 6: FACES
# =========================================================================
section('faces')

@step('faces-opening')
def step_faces_opening(page, ctx):
    navigate_to(page, 'faces')
    page.wait_for_selector('.face-card', timeout=10000)
    wait_for_idle(page)

@step('faces-toolbar')
def step_faces_toolbar(page, ctx):
    highlight_element(page, '#toolbar')

@step('unknown-faces')
def step_faces_unknown_faces(page, ctx):
    wait_for_idle(page)
    # Highlight one of the name input fields to draw attention to it
    page.evaluate('''() => {
        const cards = [...document.querySelectorAll(
            '.faces-unknown-container .face-card')];
        cards.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
            return ar.left - br.left;
        });
        const input = cards[0]?.querySelector('.face-card-input');
        if (input) {
            input.dataset.tutorialHighlight = input.style.outline || '';
            input.style.outline = '3px solid rgba(255, 96, 0, 0.35)';
        }
    }''')

@step('typing')
def step_faces_typing(page, ctx):
    card = nth_face_card(page, 10)
    card.scroll_into_view_if_needed()
    wait_for_idle(page)
    # Click the input directly to focus it
    input_el = card.locator('.face-card-input')
    input_el.click()
    input_el.fill('Alice')
    wait_for_idle(page)
    # Highlight the input field — don't press Enter yet
    highlight_element(page, f'[data-id="{card.get_attribute("data-id")}"] .face-card-input')

@step('naming')
def step_faces_naming(page, ctx):
    # The input from the previous step should still have "Alice" — press Enter to commit
    page.keyboard.press('Enter')
    page.wait_for_timeout(800)
    # Force-load lazy person-card thumbnails so Alice's face is visible
    page.evaluate('''() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }''')
    page.wait_for_function('''() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }''', timeout=10000)
    page.wait_for_timeout(300)

@step('autocomplete')
def step_faces_autocomplete(page, ctx):
    # After naming the 10th face "Alice", it moved to the known section —
    # so faces 1-9 stay in place and the old 11th is now the 10th.
    # First, wait for Alice to be fully persisted in the people cache so
    # autocomplete can find her (the optimistic update + API call needs
    # a moment to propagate).
    page.wait_for_function(
        "() => AppState.people.getAll().some(p => p.name === 'Alice')",
        timeout=5000)
    card = nth_face_card(page, 10)
    card.scroll_into_view_if_needed()
    wait_for_idle(page)
    # Focus the input, then type via JS to reliably fire the 'input' event
    # that triggers the autocomplete.  Playwright's fill() and
    # press_sequentially() don't always cause the browser to fire native
    # 'input' events in headless Chromium.
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
    # Item class is 'face-card-autocomplete-item' (not 'autocomplete-item').
    # Don't click the item yet — the screenshot should show the dropdown.
    page.wait_for_selector('.face-card-autocomplete-item',
                           state='visible', timeout=5000)
    page.wait_for_timeout(200)
    # Force-load lazy person-card thumbnails (Alice's Known People card)
    page.evaluate('''() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }''')
    page.wait_for_function('''() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }''', timeout=10000)
    page.wait_for_timeout(200)

@step('failed-face-detections')
def step_faces_failed_detections(page, ctx):
    # First, commit the autocomplete selection left open by the previous step
    page.locator('.face-card-autocomplete-item').first.click()
    page.wait_for_timeout(800)
    # Now select the first non-face via JS-dispatched click
    click_face_card(page, 1)
    wait_for_idle(page)

@step('selecting-multiple')
def step_faces_selecting_multiple(page, ctx):
    # Right-click each additional non-face to add to selection.
    # Face 1 was already selected; add the rest.
    for n in [2, 3, 4, 7, 8, 13, 15, 16]:
        click_face_card(page, n, button='right')
        page.wait_for_timeout(150)
    wait_for_idle(page)

@step('removing')
def step_faces_removing(page, ctx):
    # Hover over one of the selected cards to reveal suppress button
    nth_face_card(page, 3).hover()
    page.wait_for_timeout(300)
    # Click the suppress (red X) button — this opens a confirmation dialog.
    # Don't click OK yet; the screenshot should show the dialog.
    nth_face_card(page, 3).locator('.face-card-suppress').click()
    page.wait_for_selector('#dialog-confirm[open]', timeout=5000)
    wait_for_idle(page)

@step('the-ignore-control')
def step_faces_the_ignore_control(page, ctx):
    # Dismiss the suppress confirmation dialog from the previous step
    page.click('#dialog-confirm-ok')
    page.wait_for_timeout(800)
    # Select two faces via JS-dispatched events
    click_face_card(page, 1)
    page.wait_for_timeout(100)
    click_face_card(page, 2, button='right')
    wait_for_idle(page)
    # Hover to reveal the ignore button and highlight it
    nth_face_card(page, 1).hover()
    page.wait_for_timeout(300)
    # Highlight the ignore button
    face_id = nth_face_card(page, 1).get_attribute('data-id')
    highlight_element(page, f'[data-id="{face_id}"] .face-card-ignore',
                      color='rgba(255, 180, 0, 0.6)', width='3px')

@step('ignoring')
def step_faces_ignoring(page, ctx):
    # Click the ignore button — shows confirmation for multiple faces
    nth_face_card(page, 1).locator('.face-card-ignore').click()
    page.wait_for_selector('#dialog-confirm[open]', timeout=5000)
    wait_for_idle(page)

@step('ignored-group')
def step_faces_ignored_group(page, ctx):
    # Dismiss the ignore confirmation dialog from the previous step
    page.click('#dialog-confirm-ok')
    page.wait_for_timeout(800)
    # Spotlight the Known People section to show the '-' person
    spotlight_element(page, '.faces-section.known')

@step('quick-match')
def step_faces_quick_match(page, ctx):
    card = click_face_card(page, 2)
    wait_for_idle(page)
    card.hover()
    page.wait_for_timeout(300)
    card.locator('.face-card-quickmatch').click()
    # Wait for quick match card to appear
    page.wait_for_selector('.quick-match-card.visible', timeout=5000)
    wait_for_idle(page)

@step('naming-another-face')
def step_faces_naming_another_face(page, ctx):
    # Dismiss quick match first
    page.keyboard.press('Escape')
    page.wait_for_timeout(300)
    # Type the name but don't press Enter yet — screenshot shows the input
    card = nth_face_card(page, 2)
    input_el = card.locator('.face-card-input')
    input_el.click()
    input_el.fill('Nia')
    wait_for_idle(page)
    # Highlight the input field
    highlight_element(page, f'[data-id="{card.get_attribute("data-id")}"] .face-card-input')

@step('more-people')
def step_faces_more_people(page, ctx):
    page.keyboard.press('Enter')
    # Wait for the new person card to appear in the Known People section
    page.wait_for_timeout(500)
    # Force-load lazy person-card thumbnails by re-setting their src,
    # then wait until every image has actually decoded
    page.evaluate('''() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }''')
    page.wait_for_function('''() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }''', timeout=10000)
    page.wait_for_timeout(300)

@step('drag-and-drop')
def step_faces_drag_and_drop(page, ctx):
    # Ensure person card thumbnails are loaded (lazy images may still be pending)
    page.evaluate('''() => {
        document.querySelectorAll(
            '.faces-section.known .person-card img'
        ).forEach(img => {
            img.loading = 'eager';
            const src = img.src;
            img.src = '';
            img.src = src;
        });
    }''')
    page.wait_for_function('''() => {
        const imgs = document.querySelectorAll(
            '.faces-section.known .person-card img');
        return imgs.length > 0
            && [...imgs].every(img => img.complete && img.naturalWidth > 0);
    }''', timeout=10000)

    # Instead of performing an actual drag (which is hard to visualise in a
    # static screenshot), draw a CSS arrow from the third unknown face to
    # the first known person card (the '-' ignore group).
    page.evaluate('''() => {
        const source = (() => {
            const cards = [...document.querySelectorAll(
                '.faces-unknown-container .face-card')];
            cards.sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                if (Math.abs(ar.top - br.top) > 10) return ar.top - br.top;
                return ar.left - br.left;
            });
            return cards[2];
        })();
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
        const polygon = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
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
        path.setAttribute('d', `M ${sx} ${sy} Q ${cx} ${cy} ${tx} ${ty}`);
        path.setAttribute('stroke', '#ff6000');
        path.setAttribute('stroke-width', '3');
        path.setAttribute('fill', 'none');
        path.setAttribute('marker-end', 'url(#tutorial-arrowhead)');
        svg.appendChild(path);

        document.body.appendChild(svg);
    }''')
    wait_for_idle(page)

@step('view-all')
def step_faces_view_all(page, ctx):
    # First, ignore the 3rd unknown face shown with the arrow in the previous step.
    # Single-face ignore skips the confirmation dialog and acts immediately.
    card = click_face_card(page, 3)
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
    page.evaluate('''() => {
        const cards = document.querySelectorAll('.face-card-padlock');
        for (const padlock of cards) {
            const card = padlock.closest('.face-card');
            if (!card) continue;
            const star = card.querySelector('.face-card-star.preferred');
            if (!star) { padlock.click(); return; }
        }
    }''')
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
    page.wait_for_selector('#dialog-people-picker[open]', state='visible',
                           timeout=5000)
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
    page.evaluate('''() => {
        document.getElementById('app').dataset.theme = 'light';
        localStorage.setItem('photonarium-theme', '"light"');
    }''')
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
# Server lifecycle
# =========================================================================

def setup_tutorials_dir():
    """Clean and prepare the tutorials output directory."""
    if TUTORIALS_DIR.exists():
        print(f'  Removing existing {TUTORIALS_DIR.name}/ ...')
        shutil.rmtree(TUTORIALS_DIR)

    TUTORIALS_DIR.mkdir()
    SCREENSHOTS_DIR.mkdir()

    # Copy frozen demo data for the server to use
    for name in ['photonarium.db', 'photonarium.db-wal', 'photonarium.db-shm',
                 '.photonarium.yml']:
        src = SCRIPT_DIR / name
        if src.exists():
            shutil.copy2(src, TUTORIALS_DIR / name)

    # Copy thumbnail cache
    thumb_src = SCRIPT_DIR / '.thumbnails'
    if thumb_src.exists():
        shutil.copytree(thumb_src, TUTORIALS_DIR / '.thumbnails')

    # Copy manual screenshots
    manual_src = SCRIPT_DIR / 'manual'
    if manual_src.exists():
        shutil.copytree(manual_src, MANUAL_DIR)

    print(f'  Prepared {TUTORIALS_DIR.name}/ with demo data')


def start_server():
    """Start the Photonarium backend against the tutorials data directory.

    Uses --config to point at the demo config file inside TUTORIALS_DIR
    (config no longer lives inside the data directory by default) and
    --data-dir as a runtime override so the server reads demo data.
    """
    cmd = [
        sys.executable, str(PROJECT_DIR / 'app.py'),
        '--config', str(TUTORIALS_DIR / '.photonarium.yml'),
        '--data-dir', str(TUTORIALS_DIR),
        '--port', str(SERVER_PORT),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc


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
    raise TimeoutError(
        f'Server did not start within {SERVER_STARTUP_TIMEOUT}s')


def stop_server(proc):
    """Terminate the backend server."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


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
        slides.append({
            'section':    sec['title'],
            'sectionNum': sec['number'],
            'stepId':     s['id'],
            'title':      s['title'],
            'caption':    s['caption'],
            'screenshot': s['screenshot'],
        })

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
            toc_entries.append({
                'title': sl['section'],
                'first': idx,
                'range': f'{slides[idx]["stepId"]} – {slides[last_idx]["stepId"]}',
            })

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
    server = start_server()
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
            page.evaluate('''() => {
                document.getElementById('app').dataset.theme = 'dark';
                localStorage.setItem('photonarium-theme', '"dark"');
            }''')
            page.wait_for_timeout(300)

            ctx = {}  # shared context across steps
            current_section = None

            for step_def in STEPS:
                # Skip/stop sections for faster debugging
                if (START_FROM_SECTION is not None
                        and step_def['section'] < START_FROM_SECTION):
                    continue
                if (STOP_AFTER_SECTION is not None
                        and step_def['section'] > STOP_AFTER_SECTION):
                    print(f'\n  (stopping after section '
                          f'{STOP_AFTER_SECTION})')
                    break

                if step_def['section'] != current_section:
                    current_section = step_def['section']
                    sec_info = next(s for s in SECTIONS
                                   if s['number'] == current_section)
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

    # Generate slideshow HTML
    print('\nGenerating slideshow...')
    generate_html()

    print(f'\nDone! Tutorial output in: {TUTORIALS_DIR}')
    print(f'Open {TUTORIALS_DIR / "index.html"} in a browser to view.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Generate the Photonarium tutorial')
    parser.add_argument('-f', '--from-section', type=int, default=None, metavar='N',
                        help='Start from this section number (skip earlier sections)')
    parser.add_argument('-t', '--to-section', type=int, default=None, metavar='N',
                        help='Stop after this section number (skip later sections)')
    args = parser.parse_args()

    START_FROM_SECTION = args.from_section
    STOP_AFTER_SECTION = args.to_section

    main()
