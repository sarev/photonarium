"""Standalone mockup for the "On this day..." feature.

Queries the Photonarium database for images taken on the same day & month
across different years, picks the best ones by quality, and generates a
self-contained HTML page showing them in a photo-album style layout.

Usage:
    python on_this_day_mockup.py --date 02-14
    python on_this_day_mockup.py --date 12-25 --max-per-year 2
    python on_this_day_mockup.py --random
    python on_this_day_mockup.py --random --data-dir /path/to/data

The generated HTML file is written to on_this_day.html and opened in the
default browser.
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import random
import sqlite3
import sys
import webbrowser
from datetime import date
from pathlib import Path

import yaml
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration / database discovery
# ---------------------------------------------------------------------------


def find_data_dir(cli_data_dir: str | None) -> Path:
    """Resolve the data directory from CLI flag or photonarium.yml."""
    if cli_data_dir:
        return Path(cli_data_dir)

    # Read from the OS-default config location
    if sys.platform == 'win32':
        config_dir = Path(os.environ.get('LOCALAPPDATA', ''))
        config_path = config_dir / 'Photonarium' / 'photonarium.yml'
    elif sys.platform == 'darwin':
        config_path = Path.home() / 'Library' / 'Application Support' / 'Photonarium' / 'photonarium.yml'
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
        config_path = Path(xdg) / 'photonarium' / 'photonarium.yml'

    if not config_path.exists():
        print(f'Config not found at {config_path}, using current directory', file=sys.stderr)
        return Path('.')

    with open(config_path, encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    data_dir = cfg.get('data_dir', '')
    return Path(data_dir) if data_dir else Path('.')


def open_database(data_dir: Path) -> sqlite3.Connection:
    """Open the Photonarium SQLite database (read-only)."""
    db_path = data_dir / 'photonarium.db'
    if not db_path.exists():
        print(f'Database not found: {db_path}', file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Image querying
# ---------------------------------------------------------------------------


def pick_random_date(conn: sqlite3.Connection) -> tuple[int, int]:
    """Pick a random month/day that actually has images across multiple years."""
    rows = conn.execute("""
        SELECT DISTINCT
            CAST(strftime('%m', timestamp) AS INTEGER) AS m,
            CAST(strftime('%d', timestamp) AS INTEGER) AS d,
            COUNT(DISTINCT strftime('%Y', timestamp)) AS year_count
        FROM images
        WHERE timestamp IS NOT NULL
          AND deleted = 0
        GROUP BY m, d
        HAVING year_count >= 2
        ORDER BY year_count DESC
    """).fetchall()

    if not rows:
        # Fall back to any date with images
        rows = conn.execute("""
            SELECT DISTINCT
                CAST(strftime('%m', timestamp) AS INTEGER) AS m,
                CAST(strftime('%d', timestamp) AS INTEGER) AS d
            FROM images
            WHERE timestamp IS NOT NULL AND deleted = 0
        """).fetchall()

    if not rows:
        print('No dated images found in the database.', file=sys.stderr)
        sys.exit(1)

    row = random.choice(rows)
    return row['m'], row['d']


def query_images(
    conn: sqlite3.Connection,
    month: int,
    day: int,
    max_per_year: int,
) -> dict[int, list[dict]]:
    """Find images matching a given day & month, grouped by year.

    Returns up to ``max_per_year`` images per year, ranked by aesthetic
    quality (LAION score first, then NIMA, then sharpness as tiebreaker).
    """
    rows = conn.execute(
        """
        SELECT
            id,
            path,
            basename,
            width,
            height,
            timestamp,
            checksum,
            CAST(strftime('%Y', timestamp) AS INTEGER) AS year,
            COALESCE(aesthetic_laion, 0) AS aesthetic_laion,
            COALESCE(aesthetic_nima, 0) AS aesthetic_nima,
            COALESCE(laplacian_var, 0) AS sharpness
        FROM images
        WHERE timestamp IS NOT NULL
          AND deleted = 0
          AND CAST(strftime('%m', timestamp) AS INTEGER) = ?
          AND CAST(strftime('%d', timestamp) AS INTEGER) = ?
        ORDER BY year ASC,
                 aesthetic_laion DESC,
                 aesthetic_nima DESC,
                 sharpness DESC
    """,
        (month, day),
    ).fetchall()

    # Group by year, keeping only the top N per year
    by_year: dict[int, list[dict]] = {}
    for row in rows:
        year = row['year']
        group = by_year.setdefault(year, [])
        if len(group) < max_per_year:
            group.append(dict(row))

    return by_year


def cap_total_images(
    by_year: dict[int, list[dict]],
    max_total: int,
) -> dict[int, list[dict]]:
    """Trim total images across all years to fit in the viewport.

    Uses round-robin selection (best image from each year first, then
    second-best, etc.) so that every year gets fair representation.
    """
    total = sum(len(imgs) for imgs in by_year.values())
    if total <= max_total:
        return by_year

    result: dict[int, list[dict]] = {year: [] for year in by_year}
    count = 0
    max_rounds = max(len(imgs) for imgs in by_year.values())

    for round_idx in range(max_rounds):
        for year in sorted(by_year):
            if round_idx < len(by_year[year]):
                result[year].append(by_year[year][round_idx])
                count += 1
                if count >= max_total:
                    return {y: imgs for y, imgs in result.items() if imgs}

    return {y: imgs for y, imgs in result.items() if imgs}


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

THUMB_SIZE = 400


def load_thumbnail(image: dict, data_dir: Path) -> str | None:
    """Load or generate a thumbnail and return it as a base64 data URI.

    Tries the Photonarium thumbnail cache first, falls back to reading
    the original file and resizing with Pillow.
    """
    # Try the thumbnail cache first
    checksum = image.get('checksum')
    if checksum:
        prefix = checksum[:2] if len(checksum) >= 2 else 'xx'
        cache_path = data_dir / '.thumbnails' / str(THUMB_SIZE) / prefix / f'{checksum}.jpg'
        if cache_path.exists():
            with open(cache_path, 'rb') as f:
                return 'data:image/jpeg;base64,' + base64.b64encode(f.read()).decode()

    # Fall back to reading the original
    original = Path(image['path'])
    if not original.exists():
        return None

    try:
        img = Image.open(original)
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        img = img.convert('RGB')

        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=85)
        return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f'  Could not load {original.name}: {e}', file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------


def generate_html(
    month: int,
    day: int,
    by_year: dict[int, list[dict]],
    data_dir: Path,
) -> str:
    """Generate a self-contained HTML page for the "On this day..." overlay.

    The layout fills the viewport: a header with title, a flexible photo grid
    that sizes images to fit the available space without scrolling, and a
    footer with action buttons (Dismiss / View in Gallery).
    """
    date_str = f'{date(2000, month, day).strftime("%B")} {day}'

    # Collect all images with their thumbnails and aspect ratios
    cards: list[dict] = []
    for year in sorted(by_year):
        for image in by_year[year]:
            thumb = load_thumbnail(image, data_dir)
            if thumb:
                w, h = image['width'], image['height']
                cards.append(
                    {
                        'year': year,
                        'thumb': thumb,
                        'id': image['id'],
                        'basename': image['basename'],
                        'aspect': round(w / h, 3) if h else 1.0,
                    }
                )

    if not cards:
        return '<html><body><h1>No images found</h1></body></html>'

    # Build card HTML — aspect ratio on each cell lets the grid size them
    card_html_parts = []
    for i, card in enumerate(cards):
        card_html_parts.append(
            f'            <div class="photo" style="'
            f'--delay: {i * 0.12:.2f}s; --aspect: {card["aspect"]}'
            f'">\n'
            f'                <div class="photo-frame">\n'
            f'                    <img src="{card["thumb"]}" '
            f'alt="{card["basename"]}">\n'
            f'                </div>\n'
            f'                <div class="photo-year">{card["year"]}</div>\n'
            f'            </div>'
        )

    cards_html = '\n'.join(card_html_parts)
    year_range = f'{min(by_year)} \u2013 {max(by_year)}' if len(by_year) > 1 else str(min(by_year))

    # Collect all image IDs for the "View in Gallery" action
    all_ids = [c['id'] for c in cards]
    ids_json = ','.join(f'"{i}"' for i in all_ids)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>On this day \u2014 {date_str}</title>
<style>
*, *::before, *::after {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}}

html, body {{
    height: 100%;
    overflow: hidden;
    background: #1a1a1a;
    font-family: 'Georgia', 'Times New Roman', serif;
    color: #3a3028;
}}

/* --- Full-screen overlay backdrop --- */
.overlay {{
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(8px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 1.25vh 1.25vw;
}}

/* --- Album page: fills viewport, flex column --- */
.album {{
    position: relative;
    width: 100%;
    height: 100%;
    max-width: 1600px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    /* Layered paper texture: fine horizontal fibres + diagonal weave +
       sparse speckles + warm base gradient */
    background-color: #ede6d8;
    background-image:
        /* Fine horizontal paper fibres */
        repeating-linear-gradient(
            0deg,
            transparent,
            transparent 2px,
            rgba(139, 119, 92, 0.025) 2px,
            rgba(139, 119, 92, 0.025) 3px
        ),
        /* Diagonal weave */
        repeating-linear-gradient(
            127deg,
            transparent,
            transparent 8px,
            rgba(160, 140, 110, 0.018) 8px,
            rgba(160, 140, 110, 0.018) 9px
        ),
        /* Sparse cross-fibres */
        repeating-linear-gradient(
            90deg,
            transparent,
            transparent 11px,
            rgba(120, 100, 75, 0.015) 11px,
            rgba(120, 100, 75, 0.015) 12px
        ),
        /* Warm base gradient */
        linear-gradient(135deg, #f5f0e8 0%, #ede6d8 40%, #e8e0d0 70%, #e3dbc8 100%);
    /* Subtle vignette darkening at the edges */
    box-shadow:
        0 8px 32px rgba(0, 0, 0, 0.5),
        0 2px 8px rgba(0, 0, 0, 0.3),
        inset 0 1px 0 rgba(255, 255, 255, 0.4),
        inset 0 0 80px rgba(100, 80, 50, 0.06);
    border-radius: 6px;
    padding: 1rem 1.5rem;
}}

/* --- Coffee mug ring stains --- */
.coffee-ring {{
    position: absolute;
    pointer-events: none;
    border-radius: 50%;
    /* Translucent brown ring with a hollow centre */
    background: radial-gradient(
        circle,
        transparent 52%,
        rgba(130, 95, 50, var(--ring-alpha, 0.06)) 54%,
        rgba(140, 100, 55, var(--ring-alpha, 0.06)) 62%,
        rgba(130, 95, 50, calc(var(--ring-alpha, 0.06) * 0.5)) 68%,
        transparent 70%
    );
    /* Slight blur to soften edges like a real stain */
    filter: blur(0.8px);
}}

/* --- Ring binder along one edge --- */
.binder {{
    position: absolute;
    top: 0;
    height: 100%;
    pointer-events: none;
    z-index: 2;
}}

.binder-left  {{ left: -16px; }}
.binder-right {{ right: -16px; }}

/* --- Header --- */
.album-header {{
    position: relative;
    z-index: 4;
    flex: 0 0 auto;
    text-align: center;
    padding-bottom: 0.6rem;
}}

.album-title {{
    margin-bottom: 0.2rem;
    font-size: 1.5rem;
    font-weight: normal;
    font-style: italic;
    color: #5a4a38;
    letter-spacing: 0.02em;
}}

.album-subtitle {{
    font-size: 0.85rem;
    color: #8a7a68;
    letter-spacing: 0.05em;
}}

/* --- Close button --- */
.close-btn {{
    position: absolute;
    top: 0.75rem;
    right: 0.75rem;
    z-index: 5;
    width: 28px;
    height: 28px;
    border: none;
    border-radius: 50%;
    background: rgba(90, 74, 56, 0.12);
    color: #8a7a68;
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.2s, color 0.2s;
}}

.close-btn:hover {{
    background: rgba(90, 74, 56, 0.25);
    color: #5a4a38;
}}

/* --- Photo scatter area: overlays the full album page --- */
.photos {{
    position: absolute;
    inset: 0;
    z-index: 1;
}}

/* --- Individual photo: absolutely positioned by JS scatter --- */
.photo {{
    position: absolute;
    display: flex;
    flex-direction: column;
    align-items: center;
    animation: fadeSlideIn 0.5s ease both;
    animation-delay: var(--delay, 0s);
}}

@keyframes fadeSlideIn {{
    from {{
        opacity: 0;
        transform: translateY(12px) rotate(var(--tilt, 0deg));
    }}
    to {{
        opacity: 1;
        transform: translateY(0) rotate(var(--tilt, 0deg));
    }}
}}

.photo-frame {{
    padding: 6px;
    border-radius: 1px;
    box-shadow:
        0 2px 8px rgba(0, 0, 0, 0.15),
        0 1px 2px rgba(0, 0, 0, 0.1);
    background: linear-gradient(145deg, #ffffff, #faf8f4);
    transition: transform 0.3s ease, box-shadow 0.3s ease;
}}

.photo-frame:hover {{
    transform: scale(1.05);
    box-shadow:
        0 6px 20px rgba(0, 0, 0, 0.2),
        0 2px 6px rgba(0, 0, 0, 0.12);
    z-index: 10;
}}

.photo-frame img {{
    display: block;
    height: var(--photo-h, 150px);
    width: auto;
    /* Slight vintage tint: gently desaturated with a warm sepia wash */
    filter: saturate(0.78) sepia(0.12);
    transition: filter 0.4s ease;
}}

.photo-frame:hover img {{
    filter: saturate(1) sepia(0);
}}

.photo-year {{
    margin-top: 0.3rem;
    font-size: 0.8rem;
    font-style: italic;
    color: #7a6a58;
    letter-spacing: 0.04em;
}}

/* --- Footer with action buttons --- */
.album-footer {{
    position: relative;
    z-index: 4;
    flex: 0 0 auto;
    display: flex;
    justify-content: center;
    gap: 1rem;
    padding-top: 0.6rem;
}}

.album-btn {{
    padding: 0.5rem 1.5rem;
    border: 1.5px solid #b8a890;
    border-radius: 4px;
    background: transparent;
    color: #6a5a48;
    font-family: inherit;
    font-size: 0.9rem;
    letter-spacing: 0.03em;
    cursor: pointer;
    transition: background 0.2s, color 0.2s, border-color 0.2s;
}}

.album-btn:hover {{
    background: rgba(90, 74, 56, 0.1);
    border-color: #8a7a68;
    color: #4a3a28;
}}

.album-btn-primary {{
    background: #6a5a48;
    border-color: #6a5a48;
    color: #f5f0e8;
}}

.album-btn-primary:hover {{
    background: #5a4a38;
    border-color: #5a4a38;
    color: #fff;
}}

/* --- Responsive --- */
@media (max-width: 600px) {{
    .album {{
        padding: 1rem;
        border-radius: 0;
    }}

    .album-title {{
        font-size: 1.25rem;
    }}

    .album-footer {{
        gap: 0.5rem;
    }}

    .album-btn {{
        padding: 0.4rem 1rem;
        font-size: 0.8rem;
    }}
}}
</style>
</head>
<body>

<div class="overlay" id="overlay">
    <div class="album" id="album">
        <button class="close-btn" title="Close" id="close-btn">&times;</button>

        <div class="album-header">
            <h1 class="album-title">On this day&hellip;</h1>
            <p class="album-subtitle">{date_str} &middot; {year_range}</p>
        </div>

        <div class="photos" id="photos">
{cards_html}
        </div>

        <div class="album-footer">
            <button class="album-btn" id="dismiss-btn">Dismiss</button>
            <button class="album-btn album-btn-primary" id="gallery-btn">
                View in Gallery
            </button>
        </div>
    </div>
</div>

<script>
(function() {{
    var album  = document.getElementById('album');
    var photos = document.getElementById('photos');
    var items  = photos.querySelectorAll('.photo');
    var n      = items.length;

    // --- Ring binder along a random edge (must run first so scatter ---
    //     knows which side to avoid)                                  ---
    // Draws an SVG spiral binding: a vertical spine with evenly-spaced wire
    // loops that appear to thread through punched holes in the paper.
    var binderSide = Math.random() < 0.5 ? 'left' : 'right';
    var binder = document.createElement('div');
    binder.className = 'binder binder-' + binderSide;

    var albumRect = album.getBoundingClientRect();
    var svgW = 34;
    var ringSpacing = 44;
    var ringH = 12;                   // half-height of each wire loop
    var margin = 30;                  // top/bottom margin
    var ringCount = Math.floor((albumRect.height - margin * 2) / ringSpacing);
    if (ringCount < 3) ringCount = 3;
    var startY = (albumRect.height - (ringCount - 1) * ringSpacing) / 2;

    // Spine x-position and loop direction
    var spineX = binderSide === 'left' ? svgW - 5 : 5;
    var loopDir = binderSide === 'left' ? -1 : 1;
    var loopExtent = 22;              // how far the loop extends from spine

    var svg = '<svg xmlns="http://www.w3.org/2000/svg"'
        + ' width="' + svgW + '" height="' + albumRect.height + '"'
        + ' viewBox="0 0 ' + svgW + ' ' + albumRect.height + '">'
        + '<defs>'
        // Metallic gradient for the wire
        + '<linearGradient id="wire" x1="0%" y1="0%" x2="100%" y2="0%">'
        + '<stop offset="0%"   stop-color="#b0b0b0"/>'
        + '<stop offset="25%"  stop-color="#e0e0e0"/>'
        + '<stop offset="50%"  stop-color="#c8c8c8"/>'
        + '<stop offset="75%"  stop-color="#dcdcdc"/>'
        + '<stop offset="100%" stop-color="#a0a0a0"/>'
        + '</linearGradient>'
        // Subtle drop shadow for depth
        + '<filter id="ws" x="-30%" y="-10%" width="160%" height="120%">'
        + '<feDropShadow dx="' + (loopDir * 1.5) + '" dy="1.5"'
        + ' stdDeviation="1" flood-color="rgba(0,0,0,0.22)"/>'
        + '</filter>'
        + '</defs>';

    // Spine line (thin vertical bar connecting all rings)
    svg += '<line x1="' + spineX + '" y1="' + (startY - ringH - 6) + '"'
        + ' x2="' + spineX + '" y2="' + (startY + (ringCount - 1) * ringSpacing + ringH + 6) + '"'
        + ' stroke="url(#wire)" stroke-width="1.8"/>';

    // Wire loops + punch holes
    for (var b = 0; b < ringCount; b++) {{
        var cy = startY + b * ringSpacing;
        var lx = spineX + loopDir * loopExtent;     // outer edge of loop
        var cornerR = 5;                             // rounded corner radius

        // Punch hole (dark circle behind the wire)
        svg += '<circle cx="' + spineX + '" cy="' + cy + '" r="3"'
            + ' fill="#3a3028" opacity="0.18"/>';

        // Wire loop: squared D-shape with rounded outer corners
        // Top arm, outer rounded corner, vertical, bottom rounded corner, bottom arm
        var p;
        if (binderSide === 'left') {{
            p = 'M ' + spineX + ',' + (cy - ringH)
                + ' L ' + (lx + cornerR) + ',' + (cy - ringH)
                + ' Q ' + lx + ',' + (cy - ringH)
                + ' ' + lx + ',' + (cy - ringH + cornerR)
                + ' L ' + lx + ',' + (cy + ringH - cornerR)
                + ' Q ' + lx + ',' + (cy + ringH)
                + ' ' + (lx + cornerR) + ',' + (cy + ringH)
                + ' L ' + spineX + ',' + (cy + ringH);
        }} else {{
            p = 'M ' + spineX + ',' + (cy - ringH)
                + ' L ' + (lx - cornerR) + ',' + (cy - ringH)
                + ' Q ' + lx + ',' + (cy - ringH)
                + ' ' + lx + ',' + (cy - ringH + cornerR)
                + ' L ' + lx + ',' + (cy + ringH - cornerR)
                + ' Q ' + lx + ',' + (cy + ringH)
                + ' ' + (lx - cornerR) + ',' + (cy + ringH)
                + ' L ' + spineX + ',' + (cy + ringH);
        }}
        svg += '<path d="' + p + '" stroke="url(#wire)" stroke-width="2"'
            + ' fill="none" stroke-linecap="round" filter="url(#ws)"/>';
    }}

    svg += '</svg>';
    binder.innerHTML = svg;
    album.appendChild(binder);

    // --- Scatter photos via Monte-Carlo recursive descent ---
    //
    // 1.  Each image's longest edge starts at 33 % of the relevant page
    //     dimension.  When the viewport and photo orientations differ, the
    //     minority orientation is boosted by 1.4x so portrait shots in a
    //     landscape viewport (and vice versa) aren't disproportionately tiny.
    // 2.  For each image we try up to 1 000 random positions.  A candidate
    //     is valid when its bbox (padded with a tilt margin) sits fully on
    //     the page and doesn't overlap any fixed bbox or already-placed photo.
    // 3.  If we place at least 4 images (or all of them) we're done.
    // 4.  Otherwise reduce the percentage by 3 pp and retry from scratch.
    //
    // The collision check is O(placed) per attempt — trivial AABB tests —
    // so the whole thing runs in well under a millisecond.
    (function scatter() {{
        if (!n) return;
        var aR  = album.getBoundingClientRect();
        var hR  = document.querySelector('.album-header').getBoundingClientRect();
        var fR  = document.querySelector('.album-footer').getBoundingClientRect();
        var pgW = aR.width;
        var pgH = aR.height;
        var vpLandscape = pgW > pgH;

        var tm  = 10;         // tilt margin on each side of bbox
        var pad = 12;         // photo-frame padding (6 px x 2)
        var yearLabel = 22;   // year text + gap beneath photo
        var minPlaced = Math.min(5, n);   // success threshold

        // Fixed exclusion bboxes (coords relative to album top-left)
        var fixed = [
            {{ x: 0, y: 0, w: pgW, h: hR.bottom - aR.top + 6 }},
            {{ x: 0, y: fR.top - aR.top - 6, w: pgW, h: pgH - (fR.top - aR.top) + 6 }},
            binderSide === 'left'
                ? {{ x: 0, y: 0, w: 42, h: pgH }}
                : {{ x: pgW - 42, y: 0, w: 42, h: pgH }}
        ];

        // Pre-read aspect ratios
        var aspects = [];
        for (var i = 0; i < n; i++) {{
            aspects.push(parseFloat(items[i].style.getPropertyValue('--aspect')) || 1);
        }}

        function overlaps(ax, ay, aw, ah, bx, by, bw, bh) {{
            return ax < bx + bw && ax + aw > bx && ay < by + bh && ay + ah > by;
        }}

        // Compute image pixel dimensions for a given pct and aspect ratio.
        // The minority orientation (photo vs viewport) gets a 1.4x boost
        // so both orientations appear visually balanced.
        function imgSize(aspect, pct) {{
            var landscape = aspect >= 1;
            var imgW, imgH;
            if (vpLandscape) {{
                if (landscape) {{
                    imgW = pgW * pct / 100;
                    imgH = imgW / aspect;
                }} else {{
                    imgH = pgH * 1.4 * pct / 100;
                    imgW = imgH * aspect;
                }}
            }} else {{
                if (landscape) {{
                    imgW = pgW * 1.4 * pct / 100;
                    imgH = imgW / aspect;
                }} else {{
                    imgH = pgH * pct / 100;
                    imgW = imgH * aspect;
                }}
            }}
            return {{ w: imgW, h: imgH }};
        }}

        // --- Outer loop: decrease pct until enough images fit ---
        var bestPositions = null;
        var bestPlacedIdx = null;

        for (var pct = 33; pct >= 8; pct -= 1) {{
            var bboxes = fixed.slice();
            var positions = [];
            var placedIdx = [];

            for (var i = 0; i < n; i++) {{
                var sz  = imgSize(aspects[i], pct);
                var bw  = sz.w + pad + tm * 2;
                var bh  = sz.h + pad + yearLabel + tm * 2;

                // Skip this image if its bbox can't possibly fit on the page
                if (bw > pgW || bh > pgH) continue;

                var placed = false;
                for (var t = 0; t < 1000; t++) {{
                    var tx = Math.random() * (pgW - bw);
                    var ty = Math.random() * (pgH - bh);

                    var hit = false;
                    for (var j = 0; j < bboxes.length; j++) {{
                        var b = bboxes[j];
                        if (overlaps(tx, ty, bw, bh, b.x, b.y, b.w, b.h)) {{
                            hit = true;
                            break;
                        }}
                    }}
                    if (!hit) {{
                        bboxes.push({{ x: tx, y: ty, w: bw, h: bh }});
                        positions.push({{ x: tx + tm, y: ty + tm, imgH: sz.h }});
                        placedIdx.push(i);
                        placed = true;
                        break;
                    }}
                }}
                // Couldn't place this image — skip it and keep trying the rest
            }}

            // Keep the best result we've seen so far
            if (!bestPositions || placedIdx.length > bestPlacedIdx.length) {{
                bestPositions = positions;
                bestPlacedIdx = placedIdx;
            }}

            // Success: placed at least our minimum threshold
            if (placedIdx.length >= minPlaced) {{
                console.log('Scatter: pct=' + pct + '%, placed '
                    + placedIdx.length + '/' + n + ' images');
                break;
            }}
        }}

        // Apply positions to the placed photos, hide any that didn't fit
        var placedSet = {{}};
        for (var k = 0; k < bestPlacedIdx.length; k++) {{
            var idx = bestPlacedIdx[k];
            placedSet[idx] = true;
            items[idx].style.left = bestPositions[k].x + 'px';
            items[idx].style.top  = bestPositions[k].y + 'px';
            items[idx].style.setProperty('--photo-h', Math.round(bestPositions[k].imgH) + 'px');
        }}
        for (var i = 0; i < n; i++) {{
            if (!placedSet[i]) items[i].style.display = 'none';
        }}
    }})();

    // --- Apply slight random tilts for casual album feel ---
    items.forEach(function(el) {{
        var tilt = (Math.random() - 0.5) * 6;  // -3 to +3 degrees
        el.style.setProperty('--tilt', tilt.toFixed(2) + 'deg');
        el.style.transform = 'rotate(' + tilt.toFixed(2) + 'deg)';
    }});

    // --- Generate random coffee mug ring stains on the album ---
    var coffeeCount = 2 + Math.floor(Math.random() * 3);  // 2-4 rings
    for (var r = 0; r < coffeeCount; r++) {{
        var ring = document.createElement('div');
        ring.className = 'coffee-ring';
        var size = 90 + Math.floor(Math.random() * 90);
        ring.style.width  = size + 'px';
        ring.style.height = size + 'px';
        ring.style.left = (Math.random() * 100 - 5).toFixed(1) + '%';
        ring.style.top  = (Math.random() * 100 - 5).toFixed(1) + '%';
        var alpha = (0.04 + Math.random() * 0.06).toFixed(3);
        ring.style.setProperty('--ring-alpha', alpha);
        var scaleX = (0.88 + Math.random() * 0.24).toFixed(2);
        var rotate = Math.floor(Math.random() * 360);
        ring.style.transform = 'scaleX(' + scaleX + ') rotate(' + rotate + 'deg)';
        album.appendChild(ring);
    }}

    // --- Close on Escape, X button, Dismiss, or backdrop click ---
    var overlay = document.getElementById('overlay');
    function close() {{ overlay.style.display = 'none'; }}

    document.getElementById('close-btn').addEventListener('click', close);
    document.getElementById('dismiss-btn').addEventListener('click', close);
    document.addEventListener('keydown', function(e) {{
        if (e.key === 'Escape') close();
    }});
    overlay.addEventListener('click', function(e) {{
        if (e.target === overlay) close();
    }});

    // --- "View in Gallery" button ---
    // In the real app this would apply a custom filter to the gallery.
    // For the mockup, log the image IDs that would be shown.
    var imageIds = [{ids_json}];
    document.getElementById('gallery-btn').addEventListener('click', function() {{
        console.log('View in Gallery - image IDs:', imageIds);
        alert(
            'In the real app, this would open the gallery filtered to these '
            + imageIds.length + ' images.\\n\\nImage IDs:\\n'
            + imageIds.join('\\n')
        );
    }});
}})();
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description='Generate an "On this day..." photo album mockup.',
    )
    parser.add_argument(
        '--date',
        metavar='MM-DD',
        help='Month and day to search for (e.g. 02-14, 12-25)',
    )
    parser.add_argument(
        '--random',
        action='store_true',
        help='Pick a random date that has images across multiple years',
    )
    parser.add_argument(
        '--data-dir',
        metavar='DIR',
        help='Photonarium data directory (default: read from config)',
    )
    parser.add_argument(
        '--max-per-year',
        type=int,
        default=3,
        metavar='N',
        help='Maximum images to show per year (default: 3)',
    )
    parser.add_argument(
        '--max-total',
        type=int,
        default=12,
        metavar='N',
        help='Maximum total images across all years (default: 12)',
    )
    parser.add_argument(
        '--output',
        default='on_this_day.html',
        metavar='FILE',
        help='Output HTML file (default: on_this_day.html)',
    )
    args = parser.parse_args()

    if not args.date and not args.random:
        parser.error('Specify --date MM-DD or --random')

    data_dir = find_data_dir(args.data_dir)
    print(f'Data directory: {data_dir}')

    conn = open_database(data_dir)

    if args.random:
        month, day = pick_random_date(conn)
        print(f'Random date picked: {month:02d}-{day:02d}')
    else:
        parts = args.date.split('-')
        if len(parts) != 2:
            parser.error('Date must be in MM-DD format')
        month, day = int(parts[0]), int(parts[1])

    date_str = f'{date(2000, month, day).strftime("%B")} {day}'
    print(f'Searching for images on {date_str}...')

    by_year = query_images(conn, month, day, args.max_per_year)
    conn.close()

    raw_total = sum(len(imgs) for imgs in by_year.values())
    by_year = cap_total_images(by_year, args.max_total)
    total = sum(len(imgs) for imgs in by_year.values())

    if total < raw_total:
        print(f'Found {raw_total} images across {len(by_year)} years, capped to {total}: {sorted(by_year.keys())}')
    else:
        print(f'Found {total} images across {len(by_year)} years: {sorted(by_year.keys())}')

    if total == 0:
        print('No images found for this date.', file=sys.stderr)
        sys.exit(1)

    print('Generating HTML...')
    html = generate_html(month, day, by_year, data_dir)

    output_path = Path(args.output)
    output_path.write_text(html, encoding='utf-8')
    print(f'Written to {output_path.resolve()}')

    # Open in browser
    webbrowser.open(output_path.resolve().as_uri())


if __name__ == '__main__':
    main()
