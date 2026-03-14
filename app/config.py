"""
Configuration management for the Photonarium image database.

This module handles loading, saving, and validating configuration from a YAML
file stored at an OS-appropriate location.

Config file location (resolution order):
    1. --config / -c CLI flag
    2. PHOTONARIUM_CONFIG environment variable
    3. OS default:
       - Windows:  %LOCALAPPDATA%\\Photonarium\\photonarium.yml
       - macOS:    ~/Library/Application Support/Photonarium/photonarium.yml
       - Linux:    $XDG_CONFIG_HOME/photonarium/photonarium.yml
                   (defaults to ~/.config/photonarium/photonarium.yml)

On first run, if no config exists at the OS default location but a legacy
``.photonarium.yml`` is found in the current working directory, the legacy
file is migrated automatically (copied to the new location with ``data_dir``
injected pointing at the old working directory).

Usage:
    from config import Config, load_config, save_config, get_default_config_path

    config = load_config()                          # OS default path
    config = load_config('/path/to/config.yml')     # Custom path
    save_config(config, '/path/to/config.yml')      # Write config to disk
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

# Configure module logger
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OS-default config path
# ---------------------------------------------------------------------------


def get_default_config_path() -> Path:
    """Return the OS-appropriate default path for the configuration file.

    - Windows:  %LOCALAPPDATA%\\Photonarium\\photonarium.yml
    - macOS:    ~/Library/Application Support/Photonarium/photonarium.yml
    - Linux:    $XDG_CONFIG_HOME/photonarium/photonarium.yml
                (defaults to ~/.config/photonarium/photonarium.yml)
    """
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return base / 'Photonarium' / 'photonarium.yml'
    elif sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'Photonarium' / 'photonarium.yml'
    else:
        xdg = os.environ.get('XDG_CONFIG_HOME', '')
        base = Path(xdg) if xdg else Path.home() / '.config'
        return base / 'photonarium' / 'photonarium.yml'


# Legacy config filename (pre-relocation, lived inside the data directory)
_LEGACY_CONFIG_NAME = '.photonarium.yml'


# ---------------------------------------------------------------------------
# CONFIG_SCHEMA — single source of truth for YAML structure and comments
# ---------------------------------------------------------------------------
# Each entry is (section_header, [(field_name, comment_lines), ...]).
# comment_lines are written as YAML comments above each field.  Empty strings
# produce blank lines for visual spacing.

CONFIG_SCHEMA: list[tuple[str, list[tuple[str, list[str]]]]] = [
    # ── 1. Storage & Server ──────────────────────────────────────────
    (
        'Storage & Server',
        [
            (
                'data_dir',
                [
                    '[!] Where Photonarium stores its database, thumbnails, and model files.',
                    'Changing this without manually moving the contents of the old directory',
                    'will lose your database and thumbnails. You will also need to re-run',
                    'download_models.py to restore the LAION and NIMA weight files.',
                    'Set automatically by the installer. Use an absolute path for reliability.',
                    'Leave empty to use the current working directory.',
                ],
            ),
            (
                'trash_dir',
                [
                    'When images are deleted (from Gallery, Fullscreen, or duplicate pruning),',
                    'they are moved to this directory instead of being permanently removed.',
                    'Files keep their original names (with a counter suffix on collision).',
                    'Leave empty to use the default: <data-dir>/trash/',
                ],
            ),
            (
                'catalogue_dir',
                [
                    'Path to a managed catalogue directory for imported images.',
                    'Images imported via the UI are copied here, organised by date.',
                    'Leave empty to use the default (<data-dir>/catalogue/).',
                    '[!] Changing this does not move previously imported files.',
                ],
            ),
            (
                'server_host',
                [
                    '[!] Network interface to bind to.',
                    '  0.0.0.0   = listen on all interfaces (accessible from other devices on your network)',
                    '  127.0.0.1 = localhost only (only this machine can connect)',
                ],
            ),
            (
                'server_port',
                [
                    '[!] Port number for the web server. Range: 1024-65535.',
                    'Any open browser tabs or bookmarks will need updating to the new URL.',
                ],
            ),
            (
                'headless',
                [
                    'Headless mode disables features that require a local desktop:',
                    'the "Add Local Folder" button is hidden and file paths are not',
                    'clickable (since there is no file manager to reveal files in).',
                    'Enable this for Docker, NAS, or other server deployments.',
                ],
            ),
            (
                'scan_interval_minutes',
                [
                    'Interval in minutes for automatic folder rescans (0 = disabled).',
                    'Useful for Docker/NAS deployments where photos sync continuously.',
                    'Range: 0-1440 (0 = off, 1440 = daily)',
                ],
            ),
        ],
    ),
    # ── 2. Performance ───────────────────────────────────────────────
    (
        'Performance',
        [
            (
                'embedding_batch_size',
                [
                    'Batch size for embedding computation (1-256)',
                    'Higher values are faster but use more VRAM. Reduce if you get out-of-memory errors.',
                ],
            ),
            (
                'nima_batch_size',
                [
                    'Batch size for NIMA scoring (1-64).',
                    'Higher values are faster but use more VRAM (~500MB base for VGG16).',
                ],
            ),
            (
                'face_detection_batch_size',
                [
                    'Batch size for face detection (number of images processed together).',
                    'Higher values improve GPU utilization but use more VRAM.',
                    'Reduce if you get out-of-memory errors. Range: 1-64, recommended: 16-32',
                ],
            ),
            (
                'indexing_threads',
                [
                    'Number of threads for parallel image indexing (1-16)',
                    'Higher values speed up initial scanning but use more CPU/disk I/O.',
                    'Recommended: 4-8 for HDD, 8-16 for SSD',
                ],
            ),
            (
                'import_threads',
                [
                    'Number of parallel threads for copying files during import (1-16).',
                ],
            ),
            (
                'trash_threads',
                [
                    'Number of threads for parallel file moves when trashing images (1-32).',
                    'File I/O benefits from high parallelism, especially on SSDs or NAS.',
                    'Recommended: 4-8 for HDD, 8-16 for SSD, 16-32 for NAS',
                ],
            ),
            (
                'max_incremental_duplicates',
                [
                    'Maximum number of new/modified images to process incrementally for duplicates.',
                    'If more images need checking, falls back to full recomputation which is faster',
                    'for large batches. Range: 1-10000',
                ],
            ),
            (
                'incremental_threshold_percent',
                [
                    'Percentage of total images that triggers full recomputation instead of incremental.',
                    'If dirty_count > (total_count * threshold), does full rebuild.',
                    'Range: 5-50, recommended: 15-25',
                ],
            ),
        ],
    ),
    # ── 3. Models ────────────────────────────────────────────────────
    (
        'Models',
        [
            (
                'openclip_model',
                [
                    '[M] The model used for semantic image search and similarity detection.',
                    'Changing these settings will require re-embedding all images.',
                    '',
                    'Model architecture. Common options:',
                    '  - ViT-B-32  (fast, good quality, ~400MB VRAM)',
                    '  - ViT-B-16  (slower, better quality, ~400MB VRAM)',
                    '  - ViT-L-14  (slow, high quality, ~900MB VRAM)',
                    '',
                    'For non-English text search, consider multilingual models:',
                    '  - nllb-clip-base-siglip  (200+ languages, ~600 MB)',
                    '  - nllb-clip-large-siglip (higher quality, ~1.2 GB)',
                    'Multilingual models require matching pretrained weights.',
                    'Changing model requires re-embedding all images and re-running download_models.py.',
                ],
            ),
            (
                'openclip_pretrained',
                [
                    '[M] Pretrained weights. Common options:',
                    '  - openai           (original CLIP weights)',
                    '  - laion2b_s34b_b88k (trained on LAION-2B, often better for photos)',
                    '  - laion400m_e32    (trained on LAION-400M)',
                    '  - v1               (for nllb-clip multilingual models)',
                    'Check open_clip.list_pretrained() for valid model/weight combinations.',
                ],
            ),
            (
                'caption_model',
                [
                    '[M] BLIP model to use for caption generation. Options:',
                    '  - Salesforce/blip-image-captioning-base   (~1GB, fast)',
                    '  - Salesforce/blip-image-captioning-large  (~2GB, better quality)',
                    '  - Salesforce/blip2-opt-2.7b               (~5GB, BLIP-2, best quality)',
                    '  - Salesforce/blip2-flan-t5-xl             (~8GB, BLIP-2, most descriptive)',
                    'All BLIP/BLIP-2 models generate English-only captions.',
                    'BLIP-2 models require significantly more VRAM.',
                ],
            ),
            (
                'stt_model',
                [
                    '[M] Whisper model size for transcription. Larger models are more accurate',
                    'but slower and require more VRAM.',
                    '  tiny   - fastest, English-focused (~75MB)',
                    '  base   - good balance for English (~140MB)',
                    '  small  - good multilingual accuracy (~460MB)',
                    '  medium - high accuracy, strong multilingual (~1.5GB)',
                    '  large-v3 - best accuracy, best for non-English (~3GB)',
                    'For non-English audio, small or above is strongly recommended.',
                ],
            ),
        ],
    ),
    # ── 4. Image & Video Processing ──────────────────────────────────
    (
        'Image & Video Processing',
        [
            (
                'image_extensions',
                [
                    'File extensions recognised as images (lowercase, with leading dot)',
                ],
            ),
            (
                'video_extensions',
                [
                    'File extensions recognised as video files (lowercase, with leading dot)',
                ],
            ),
            (
                'max_image_dimension',
                [
                    'Maximum image dimension (width or height) to process.',
                    'Images larger than this will be downsampled before embedding/hashing.',
                    'This prevents memory issues with huge panoramas or scanned images.',
                    'Set to 0 to disable (not recommended). Range: 0 or 1024-65536',
                ],
            ),
            (
                'thumbnail_quality',
                [
                    'JPEG quality for generated thumbnails (1-100, higher = better quality, larger files)',
                ],
            ),
            (
                'date_order',
                [
                    'Preferred date order for ambiguous numeric dates in filenames.',
                    'Only affects dates where day/month cannot be determined from context.',
                    'For example, "07-03-2024" is 7 March with DMY, or 3 July with MDY.',
                    'This is a preference, not absolute — if the preferred interpretation',
                    'produces an invalid date (e.g. month 13), valid alternatives are used.',
                    'Options: DMY (day-month-year), MDY (month-day-year), YMD (year-month-day)',
                ],
            ),
            (
                'filename_date_overrides',
                [
                    'Filename patterns where the date embedded in the filename takes priority',
                    'over EXIF metadata for timestamp derivation. Uses glob-style matching.',
                    'Useful for apps like WhatsApp that rewrite EXIF dates to the download',
                    'time while encoding the actual capture time in the filename.',
                    'One pattern per line (e.g. "WhatsApp Image *").',
                ],
            ),
        ],
    ),
    # ── 5. Face Detection & Recognition ──────────────────────────────
    (
        'Face Detection & Recognition',
        [
            (
                'face_detection_enabled',
                [
                    'Enable face detection during image indexing.',
                    'When disabled, face-related UI buttons are greyed out.',
                ],
            ),
            (
                'face_detection_min_confidence',
                [
                    'MTCNN confidence threshold for face detection.',
                    'Higher values = fewer false positives, may miss some faces.',
                    'Range: 0.0-1.0, recommended: 0.90-0.99',
                ],
            ),
            (
                'face_detection_min_size',
                [
                    'Minimum face size in pixels (width/height of bounding box).',
                    'Faces smaller than this are ignored. Range: 20-200, recommended: 40-80',
                ],
            ),
            (
                'face_recognition_threshold',
                [
                    'Cosine similarity threshold for auto-matching faces to known people.',
                    'Higher values = stricter matching (fewer false matches, more unknowns).',
                    'Range: 0.0-1.0, recommended: 0.65-0.90',
                ],
            ),
        ],
    ),
    # ── 6. Captioning ────────────────────────────────────────────────
    (
        'Captioning',
        [
            (
                'caption_max_length',
                [
                    'Maximum length of generated captions in tokens.',
                    'Higher values allow longer, more detailed descriptions.',
                    'Range: 10-200, recommended: 30-75',
                ],
            ),
            (
                'caption_min_length',
                [
                    'Minimum length of generated captions in tokens.',
                    'Higher values force more descriptive captions.',
                    'Range: 1-50, recommended: 5-20',
                ],
            ),
            (
                'caption_num_beams',
                [
                    'Number of beams for beam search during generation.',
                    'Higher values produce better quality but are slower.',
                    'Set to 1 for greedy decoding (fastest, lower quality).',
                    'Range: 1-10, recommended: 3-5',
                ],
            ),
            (
                'caption_british_english',
                [
                    'Convert American English spellings to British English in generated captions.',
                    'Handles common differences like color\u2192colour, center\u2192centre, gray\u2192grey, etc.',
                ],
            ),
        ],
    ),
    # ── 7. Video & Speech-to-Text ────────────────────────────────────
    (
        'Video & Speech-to-Text',
        [
            (
                'video_scene_detection_threshold',
                [
                    'Scene change detection threshold (0-100). Higher = fewer scene boundaries.',
                    'Lower values detect more subtle transitions. Range: 1.0-100.0, recommended: 20-35',
                ],
            ),
            (
                'video_max_scene_duration',
                [
                    'Maximum scene duration in seconds.',
                    'Scenes longer than this are subdivided. Also the fallback when no cuts are detected.',
                ],
            ),
            (
                'stt_enabled',
                [
                    'Enable speech-to-text transcription of video audio using faster-whisper.',
                    'Requires the faster-whisper package to be installed.',
                    'When disabled, scene detection and frame embeddings still work.',
                ],
            ),
            (
                'stt_language',
                [
                    'Language code for transcription (e.g. "en", "fr", "de").',
                    'Leave empty for automatic language detection.',
                    'Auto-detection works well with small model and above;',
                    'less reliable with tiny and base.',
                ],
            ),
            (
                'stt_languages',
                [
                    'List of language codes available in the per-video language dropdown.',
                    'Only these languages appear as options in the UI; all Whisper-supported',
                    'codes still work if set via the API.',
                    'Whisper-supported codes: af, am, ar, as, az, ba, be, bg, bn, bo, br,',
                    '  bs, ca, cs, cy, da, de, el, en, es, et, eu, fa, fi, fo, fr, gl, gu,',
                    '  ha, haw, he, hi, hr, ht, hu, hy, id, is, it, ja, jw, ka, kk, km, kn,',
                    '  ko, la, lb, ln, lo, lt, lv, mg, mi, mk, ml, mn, mr, ms, mt, my, ne,',
                    '  nl, nn, no, oc, pa, pl, ps, pt, ro, ru, sa, sd, si, sk, sl, sn, so,',
                    '  sq, sr, su, sv, sw, ta, te, tg, th, tk, tl, tr, tt, uk, ur, uz, vi,',
                    '  yo, zh, yue',
                ],
            ),
        ],
    ),
    # ── 8. Duplicate Detection ───────────────────────────────────────
    (
        'Duplicate Detection',
        [
            (
                'perceptual_hash_threshold',
                [
                    'Perceptual hash hamming distance threshold for "near-identical" (level 1)',
                    'Range: 0-64, lower = stricter matching. Recommended: 4-8',
                ],
            ),
            (
                'similarity_threshold_level2',
                [
                    'Cosine similarity threshold for "similar" images (level 2)',
                    'Range: 0.0-1.0, higher = stricter matching. Recommended: 0.93-0.97',
                ],
            ),
            (
                'similarity_threshold_level3',
                [
                    'Cosine similarity threshold for "related" images (level 3)',
                    'Range: 0.0-1.0, higher = stricter matching. Recommended: 0.80-0.90',
                ],
            ),
        ],
    ),
    # ── 9. Quality Scoring ───────────────────────────────────────────
    (
        'Quality Scoring',
        [
            (
                'nima_enabled',
                [
                    'NIMA (Neural IMage Assessment) provides a second aesthetic quality signal',
                    'alongside the LAION aesthetic predictor. The two scores are blended for',
                    'the Quality sort in Gallery and best-image ranking in duplicate groups.',
                    '',
                    'Enable NIMA aesthetic scoring during image indexing.',
                    'When disabled, the NIMA thread sits idle and quality ranking falls back to',
                    'LAION-only. Existing NIMA scores are preserved.',
                ],
            ),
            (
                'quality_weight_aesthetic',
                [
                    'These weights control how the composite quality score is computed in the',
                    'frontend. They are applied at sort time and do not affect stored data.',
                    '',
                    'Component weights (should sum to ~1.0):',
                    '  aesthetic - blended NIMA+LAION score (absolute, normalised to 0-1)',
                    '  sharpness - log Laplacian variance (percentile rank)',
                    '  pixels    - total pixel count (percentile rank)',
                    '  bpp       - bits per pixel (percentile rank)',
                ],
            ),
            ('quality_weight_sharpness', []),
            ('quality_weight_pixels', []),
            ('quality_weight_bpp', []),
            (
                'quality_alpha',
                [
                    'Blend ratio for NIMA vs LAION aesthetic scores.',
                    'A = alpha * (NIMA / 10) + (1 - alpha) * (LAION / 10)',
                    'Set to 0.0 to use LAION only, 1.0 for NIMA only.',
                    'Range: 0.0-1.0',
                ],
            ),
        ],
    ),
    # ── 10. Features ─────────────────────────────────────────────────
    (
        'Features',
        [
            (
                'on_this_day_enabled',
                [
                    'Show a nostalgic "On this day..." photo album when returning to the app',
                    'after a long period of inactivity (8+ hours), if there are photos from',
                    "today's date across multiple years.",
                ],
            ),
            (
                'slideshow_interval',
                [
                    'Seconds each image is displayed during a slideshow in the',
                    'fullscreen viewer (e.g. 3.5 for three and a half seconds).',
                    'Range: 1.0-60.0',
                ],
            ),
        ],
    ),
    # ── 11. Thumbnail Loading ────────────────────────────────────────
    (
        'Thumbnail Loading (Frontend)',
        [
            (
                'thumbnail_concurrent_requests',
                [
                    'Maximum concurrent thumbnail fetch requests from the browser.',
                    'Higher values load thumbnails faster but increase backend load.',
                    'Range: 1-12, recommended: 4-8',
                ],
            ),
            (
                'thumbnail_extra_rows',
                [
                    'Extra rows above/below the viewport to prefetch thumbnails for.',
                    'Higher values reduce blank thumbnails when scrolling but increase memory usage.',
                    'Range: 1-20, recommended: 3-8',
                ],
            ),
            (
                'thumbnail_timeout_ms',
                [
                    'Timeout for thumbnail fetch requests in milliseconds.',
                    "If a request takes longer than this, it's aborted and the slot freed.",
                    'Range: 1000-60000, recommended: 5000-15000',
                ],
            ),
            (
                'thumbnail_scroll_throttle_ms',
                [
                    'Scroll event throttle in milliseconds.',
                    'How often the thumbnail queue is re-evaluated during scrolling.',
                    'Lower values = more responsive, higher values = less CPU usage.',
                    'Range: 50-1000, recommended: 150-300',
                ],
            ),
            (
                'thumbnail_cache_size_mb',
                [
                    'RAM cache size for thumbnail bytes in megabytes.',
                    'Caches recently-accessed thumbnails in memory to avoid disk reads.',
                    'Set to 0 to disable caching. Range: 0-1000, recommended: 50-200',
                ],
            ),
        ],
    ),
    # ── 12. Logging ──────────────────────────────────────────────────
    (
        'Logging',
        [
            (
                'log_retention_lines',
                [
                    'Maximum number of log lines to retain in the database (100-100000).',
                    'These are viewable from the Management screen.',
                    'Set to 0 to disable database logging.',
                ],
            ),
        ],
    ),
]

# Ordered list of image extensions for the YAML file (matches the original
# template ordering, with a comment before the RAW formats)
_IMAGE_EXTENSIONS_ORDERED: list[str | tuple[str, str]] = [
    '.jpg',
    '.jpeg',
    '.png',
    '.gif',
    '.bmp',
    '.tiff',
    '.tif',
    '.webp',
    ('# Camera RAW formats (require rawpy)', ''),
    '.cr2',
    '.cr3',
    '.nef',
    '.nrw',
    '.arw',
    '.srf',
    '.dng',
    '.raf',
    '.rw2',
    '.orf',
    '.pef',
    '.srw',
    '.x3f',
    '.3fr',
    '.iiq',
    '.rwl',
    '.kdc',
    '.dcr',
    '.erf',
]


# Ordered list of video extensions for the YAML file
_VIDEO_EXTENSIONS_ORDERED: list[str] = [
    '.mp4',
    '.mkv',
    '.avi',
    '.mov',
    '.webm',
    '.m4v',
    '.wmv',
    '.flv',
]


# ---------------------------------------------------------------------------
# FIELD_CONSTRAINTS — single source of truth for numeric ranges
# ---------------------------------------------------------------------------
# Maps field name → {min, max, step, [special_zero]}.  Used by both
# _validate() and get_config_schema() so the API and validation stay in sync.
# ``special_zero`` means 0 is accepted even when it's below ``min``.

FIELD_CONSTRAINTS: dict[str, dict[str, int | float | bool]] = {
    'server_port': {'min': 1024, 'max': 65535, 'step': 1},
    'thumbnail_quality': {'min': 1, 'max': 100, 'step': 1},
    'max_image_dimension': {'min': 1024, 'max': 65536, 'step': 1, 'special_zero': True},
    'embedding_batch_size': {'min': 1, 'max': 256, 'step': 1},
    'perceptual_hash_threshold': {'min': 0, 'max': 64, 'step': 1},
    'similarity_threshold_level2': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'similarity_threshold_level3': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'indexing_threads': {'min': 1, 'max': 16, 'step': 1},
    'trash_threads': {'min': 1, 'max': 32, 'step': 1},
    'max_incremental_duplicates': {'min': 1, 'max': 10000, 'step': 1},
    'incremental_threshold_percent': {'min': 5, 'max': 50, 'step': 1},
    'thumbnail_concurrent_requests': {'min': 1, 'max': 12, 'step': 1},
    'thumbnail_extra_rows': {'min': 1, 'max': 20, 'step': 1},
    'thumbnail_timeout_ms': {'min': 1000, 'max': 60000, 'step': 100},
    'thumbnail_scroll_throttle_ms': {'min': 50, 'max': 1000, 'step': 10},
    'thumbnail_cache_size_mb': {'min': 0, 'max': 1000, 'step': 1},
    'face_detection_min_confidence': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'face_detection_min_size': {'min': 20, 'max': 200, 'step': 1},
    'face_recognition_threshold': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'face_detection_batch_size': {'min': 1, 'max': 256, 'step': 1},
    'caption_max_length': {'min': 10, 'max': 200, 'step': 1},
    'caption_min_length': {'min': 1, 'max': 50, 'step': 1},
    'caption_num_beams': {'min': 1, 'max': 10, 'step': 1},
    'nima_batch_size': {'min': 1, 'max': 256, 'step': 1},
    'quality_weight_aesthetic': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'quality_weight_sharpness': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'quality_weight_pixels': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'quality_weight_bpp': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'quality_alpha': {'min': 0.0, 'max': 1.0, 'step': 0.01},
    'video_scene_detection_threshold': {'min': 1.0, 'max': 100.0, 'step': 0.5},
    'video_max_scene_duration': {'min': 2.0, 'max': 60.0, 'step': 0.5},
    'slideshow_interval': {'min': 1.0, 'max': 60.0, 'step': 0.5},
    'import_threads': {'min': 1, 'max': 16, 'step': 1},
    'scan_interval_minutes': {'min': 1, 'max': 1440, 'step': 1, 'special_zero': True},
    'log_retention_lines': {'min': 100, 'max': 100000, 'step': 100, 'special_zero': True},
}

# STT_LANGUAGES_DEFAULT — top 15 global languages for the per-video dropdown
# ---------------------------------------------------------------------------
_STT_LANGUAGES_DEFAULT = [
    'en',
    'zh',
    'es',
    'ar',
    'hi',
    'fr',
    'pt',
    'ru',
    'ja',
    'de',
    'ko',
    'it',
    'nl',
    'tr',
    'sv',
]

# FIELD_CHOICES — single source of truth for enumerated string options
# ---------------------------------------------------------------------------
# Maps field name → list of valid values.  Used by _validate() and
# get_config_schema() so the API, validation, and settings UI stay in sync.

FIELD_CHOICES: dict[str, list[str]] = {
    'date_order': ['DMY', 'MDY', 'YMD'],
    'stt_model': ['tiny', 'base', 'small', 'medium', 'large-v3'],
}


# ---------------------------------------------------------------------------
# HARDWARE_PRESETS — pre-built performance profiles for common hardware
# ---------------------------------------------------------------------------
# Each entry is (id, label, values_dict).  The wizard applies values_dict to
# the config; fields not listed here keep their defaults.  Feature flags
# (face_detection_enabled, nima_enabled, stt_enabled) are deliberately
# excluded — those are user decisions, not hardware-dependent.

HARDWARE_PRESETS: list[tuple[str, str, dict[str, Any]]] = [
    (
        'low',
        'Low-end / NAS (ARM \u00b7 2-4 GB \u00b7 no GPU)',
        {
            'indexing_threads': 2,
            'trash_threads': 4,
            'import_threads': 2,
            'embedding_batch_size': 4,
            'face_detection_batch_size': 4,
            'nima_batch_size': 4,
            'thumbnail_cache_size_mb': 25,
            'thumbnail_concurrent_requests': 3,
            'stt_model': 'tiny',
            'openclip_model': 'ViT-B-32',
            'openclip_pretrained': 'openai',
            'caption_model': 'Salesforce/blip-image-captioning-base',
        },
    ),
    (
        'moderate',
        'Moderate PC (8-16 GB \u00b7 SSD \u00b7 no GPU)',
        {
            'indexing_threads': 4,
            'trash_threads': 8,
            'import_threads': 4,
            'embedding_batch_size': 8,
            'face_detection_batch_size': 8,
            'nima_batch_size': 8,
            'thumbnail_cache_size_mb': 100,
            'thumbnail_concurrent_requests': 6,
            'stt_model': 'base',
            'openclip_model': 'ViT-B-32',
            'openclip_pretrained': 'openai',
            'caption_model': 'Salesforce/blip-image-captioning-large',
        },
    ),
    (
        'high_laptop',
        'High-end laptop (32 GB \u00b7 RTX GPU)',
        {
            'indexing_threads': 8,
            'trash_threads': 12,
            'import_threads': 8,
            'embedding_batch_size': 64,
            'face_detection_batch_size': 32,
            'nima_batch_size': 16,
            'thumbnail_cache_size_mb': 200,
            'thumbnail_concurrent_requests': 8,
            'stt_model': 'small',
            'openclip_model': 'ViT-B-16',
            'openclip_pretrained': 'laion2b_s34b_b88k',
            'caption_model': 'Salesforce/blip-image-captioning-large',
        },
    ),
    (
        'high_desktop',
        'High-end desktop (64 GB \u00b7 RTX 5090)',
        {
            'indexing_threads': 12,
            'trash_threads': 16,
            'import_threads': 8,
            'embedding_batch_size': 128,
            'face_detection_batch_size': 64,
            'nima_batch_size': 32,
            'thumbnail_cache_size_mb': 400,
            'thumbnail_concurrent_requests': 10,
            'stt_model': 'medium',
            'openclip_model': 'ViT-L-14',
            'openclip_pretrained': 'laion2b_s32b_b82k',
            'caption_model': 'Salesforce/blip2-opt-2.7b',
        },
    ),
]


# ---------------------------------------------------------------------------
# LANGUAGE_RECOMMENDATIONS — model overrides for multilingual use
# ---------------------------------------------------------------------------
# The wizard applies the relevant overrides on top of the hardware preset.
# STT language guidance is surfaced in UI text, not as a constant.

LANGUAGE_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    'english': {
        'description': 'English only \u2014 best quality for English text search and captions.',
        'openclip_model': 'ViT-B-32',
        'openclip_pretrained': 'openai',
    },
    'multilingual': {
        'description': 'Non-English or mixed \u2014 enables search in 200+ languages.',
        'openclip_model': 'nllb-clip-base-siglip',
        'openclip_pretrained': 'v1',
    },
    'multilingual_hq': {
        'description': 'Non-English, higher quality \u2014 larger model, better accuracy.',
        'openclip_model': 'nllb-clip-large-siglip',
        'openclip_pretrained': 'v1',
    },
}


@dataclass
class Config:
    """Application configuration with validation.

    Field documentation is kept inline with each declaration below.
    See ``CONFIG_SCHEMA`` for the user-facing help text shown in the
    settings editor and YAML comments.
    """

    # Directory for user data (database, thumbnails, models).
    # Empty string means use the current working directory.
    # Set by the installer; can be overridden at runtime with --data-dir.
    data_dir: str = ''
    # Network interface to bind to ('0.0.0.0' for LAN, '127.0.0.1' for local only).
    server_host: str = '0.0.0.0'
    # Port number for the web server (1024-65535).
    server_port: int = 5000
    # When True, hides desktop-only UI features (folder picker, reveal in
    # file manager). Set automatically by Docker entrypoint.
    headless: bool = False
    # Set of lowercase file extensions to treat as images.
    # Derived from _IMAGE_EXTENSIONS_ORDERED to avoid drift.
    image_extensions: set[str] = field(
        default_factory=lambda: {ext for ext in _IMAGE_EXTENSIONS_ORDERED if isinstance(ext, str)}
    )
    # JPEG quality for generated thumbnails (1-100).
    thumbnail_quality: int = 85
    # Max width/height before downsampling (0 to disable).
    max_image_dimension: int = 16384
    # Glob patterns for filenames where the filename-derived timestamp should
    # take priority over EXIF (e.g. WhatsApp images that rewrite EXIF dates).
    filename_date_overrides: list[str] = field(default_factory=lambda: ['WhatsApp Image *', 'WhatsApp Video *'])
    # Preferred date order for ambiguous numeric dates in filenames.
    # Only affects dates where day/month are ambiguous (e.g. 07-03-2024).
    # Options: DMY (day-month-year), MDY (month-day-year), YMD (year-month-day).
    date_order: str = 'DMY'
    # OpenCLIP model architecture name.
    openclip_model: str = 'ViT-B-32'
    # OpenCLIP pretrained weights name.
    openclip_pretrained: str = 'openai'
    # Batch size for embedding computation (1-256).  Higher values improve
    # GPU utilisation but use more VRAM.  Run tools/benchmark_batch_sizes.py
    # to find the optimal value for your hardware.
    embedding_batch_size: int = 16
    # Hamming distance threshold for level 1 (near-identical) duplicates.
    perceptual_hash_threshold: int = 4
    # Cosine similarity threshold for level 2 (similar) duplicates.
    similarity_threshold_level2: float = 0.93
    # Cosine similarity threshold for level 3 (related) duplicates.
    similarity_threshold_level3: float = 0.85
    # Number of threads for parallel image indexing (1-16).
    indexing_threads: int = 4
    # Number of threads for parallel file moves when trashing (1-32).
    trash_threads: int = 8
    # Max dirty images for incremental duplicate detection.
    # If more images need checking, falls back to full recomputation.
    max_incremental_duplicates: int = 500
    # Percentage of total images that triggers full recomputation instead
    # of incremental (5-50).
    incremental_threshold_percent: int = 20
    # Max concurrent thumbnail fetch requests (1-12).
    thumbnail_concurrent_requests: int = 6
    # Extra rows above/below viewport to prefetch (1-20).
    thumbnail_extra_rows: int = 5
    # Timeout for thumbnail fetch requests in ms (1000-60000).
    thumbnail_timeout_ms: int = 10000
    # Scroll event throttle in ms (50-1000).
    thumbnail_scroll_throttle_ms: int = 250
    # RAM cache size for thumbnail bytes in MB (0-1000).
    thumbnail_cache_size_mb: int = 100
    # Whether to detect faces during image indexing.
    face_detection_enabled: bool = True
    # MTCNN confidence threshold (0.0-1.0).
    face_detection_min_confidence: float = 0.95
    # Minimum face size in pixels (20-200).
    face_detection_min_size: int = 40
    # Cosine similarity threshold for auto-matching faces to people (0.0-1.0).
    face_recognition_threshold: float = 0.70
    # Batch size for face detection (1-256).  MTCNN groups images by
    # dimension, so larger batches may not help as much as for other
    # stages.  Run tools/benchmark_batch_sizes.py to find the optimal
    # value for your hardware.
    face_detection_batch_size: int = 24
    # BLIP/BLIP-2 model name for captioning.
    caption_model: str = 'Salesforce/blip-image-captioning-large'
    # Maximum caption length in tokens (10-200).
    caption_max_length: int = 50
    # Minimum caption length in tokens (1-50).
    caption_min_length: int = 10
    # Beam search width for caption generation (1-10).
    caption_num_beams: int = 5
    # Convert US spellings to UK in captions.
    caption_british_english: bool = False
    # Whether to run NIMA aesthetic scoring during indexing.
    nima_enabled: bool = True
    # Batch size for NIMA scoring (1-256).  Run
    # tools/benchmark_batch_sizes.py to find the optimal value for
    # your hardware.
    nima_batch_size: int = 16
    # Weight for aesthetic component in quality sort.
    quality_weight_aesthetic: float = 0.60
    # Weight for sharpness component in quality sort.
    quality_weight_sharpness: float = 0.20
    # Weight for pixel count component in quality sort.
    quality_weight_pixels: float = 0.15
    # Weight for bits-per-pixel component in quality sort.
    quality_weight_bpp: float = 0.05
    # Blend ratio for NIMA vs LAION aesthetic scores (0=LAION only, 1=NIMA only).
    quality_alpha: float = 0.60
    # Whether to show "On This Day" memories on the gallery screen.
    on_this_day_enabled: bool = True
    # Set of lowercase file extensions to treat as videos.
    # Derived from _VIDEO_EXTENSIONS_ORDERED to avoid drift.
    video_extensions: set[str] = field(default_factory=lambda: set(_VIDEO_EXTENSIONS_ORDERED))
    # Scene change detection threshold (1-100). Higher = fewer scene cuts.
    video_scene_detection_threshold: float = 27.0
    # Maximum scene duration in seconds. Scenes longer than this are
    # subdivided into uniform segments. Also used as the fallback segment
    # length when scene detection finds no cuts.
    video_max_scene_duration: float = 8.0
    # Whether to run speech-to-text transcription on video audio.
    stt_enabled: bool = True
    # Whisper model size for transcription.
    stt_model: str = 'base'
    # Language code for transcription (empty = auto-detect).
    stt_language: str = ''
    # Language codes available in the per-video language dropdown.
    stt_languages: list[str] = field(default_factory=lambda: list(_STT_LANGUAGES_DEFAULT))
    # Seconds each image is displayed during a slideshow (1.0-60.0).
    slideshow_interval: float = 5.0
    # Path to trash directory. Empty string means <data-dir>/trash/.
    trash_dir: str = ''
    # Path to managed catalogue directory for imported images.
    # Empty string means <data-dir>/catalogue/.
    catalogue_dir: str = ''
    # Number of parallel threads for file copying during import (1-16).
    import_threads: int = 4
    # Interval in minutes for automatic folder rescans (0 = disabled).
    # Useful for Docker/NAS deployments where photos sync continuously.
    scan_interval_minutes: int = 0
    # Maximum log lines to retain in the database (0 = disabled, 100-100000).
    log_retention_lines: int = 1000

    def __post_init__(self) -> None:
        """Validate configuration values after initialisation."""
        self._validate()

    def _validate(self) -> None:
        """Validate all configuration values are within acceptable ranges.

        Uses FIELD_CONSTRAINTS for numeric range checks.  Fields that need
        non-numeric validation (strings, booleans, cross-field) are handled
        explicitly.

        Raises:
            ValueError: If any configuration value is invalid.
        """
        # --- String fields: must be non-empty ---
        if not isinstance(self.data_dir, str):
            raise ValueError('data_dir must be a string')
        if not isinstance(self.catalogue_dir, str):
            raise ValueError('catalogue_dir must be a string')
        if not isinstance(self.server_host, str) or not self.server_host:
            raise ValueError('server_host must be a non-empty string')
        if not self.openclip_model or not isinstance(self.openclip_model, str):
            raise ValueError('openclip_model must be a non-empty string')
        if not self.openclip_pretrained or not isinstance(self.openclip_pretrained, str):
            raise ValueError('openclip_pretrained must be a non-empty string')
        if not self.caption_model or not isinstance(self.caption_model, str):
            raise ValueError('caption_model must be a non-empty string')
        if not isinstance(self.stt_model, str) or not self.stt_model:
            raise ValueError('stt_model must be a non-empty string')
        if not isinstance(self.stt_language, str):
            raise ValueError('stt_language must be a string')

        # --- Boolean fields ---
        if not isinstance(self.face_detection_enabled, bool):
            raise ValueError('face_detection_enabled must be a boolean')
        if not isinstance(self.caption_british_english, bool):
            raise ValueError('caption_british_english must be a boolean')
        if not isinstance(self.nima_enabled, bool):
            raise ValueError('nima_enabled must be a boolean')
        if not isinstance(self.on_this_day_enabled, bool):
            raise ValueError('on_this_day_enabled must be a boolean')
        if not isinstance(self.stt_enabled, bool):
            raise ValueError('stt_enabled must be a boolean')

        # --- Enumerated choice fields (from FIELD_CHOICES) ---
        for field_name, choices in FIELD_CHOICES.items():
            value = getattr(self, field_name)
            if value not in choices:
                raise ValueError(f'{field_name} must be one of {choices}, got {value!r}')

        # --- filename_date_overrides: must be a list of strings ---
        if not isinstance(self.filename_date_overrides, (list, tuple)):
            raise ValueError('filename_date_overrides must be a list')
        if not all(isinstance(p, str) for p in self.filename_date_overrides):
            raise ValueError('filename_date_overrides entries must be strings')

        # --- stt_languages: must be a list of language code strings ---
        if not isinstance(self.stt_languages, (list, tuple)):
            raise ValueError('stt_languages must be a list')
        if not all(isinstance(c, str) and c for c in self.stt_languages):
            raise ValueError('stt_languages entries must be non-empty strings')

        # --- image_extensions: coerce to set of dotted lowercase strings ---
        if not isinstance(self.image_extensions, (set, list, tuple)):
            raise ValueError('image_extensions must be a collection')
        self.image_extensions = {
            ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in self.image_extensions
        }

        # --- video_extensions: coerce to set of dotted lowercase strings ---
        if not isinstance(self.video_extensions, (set, list, tuple)):
            raise ValueError('video_extensions must be a collection')
        self.video_extensions = {
            ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in self.video_extensions
        }

        # --- Numeric range checks from FIELD_CONSTRAINTS ---
        for field_name, c in FIELD_CONSTRAINTS.items():
            value = getattr(self, field_name)
            lo, hi = c['min'], c['max']
            # special_zero: accept 0 even when it's below min
            if c.get('special_zero') and value == 0:
                continue
            if not lo <= value <= hi:
                raise ValueError(f'{field_name} must be {lo}-{hi}, got {value}')

        # --- Cross-field validation ---
        if self.caption_min_length > self.caption_max_length:
            raise ValueError(
                f'caption_min_length ({self.caption_min_length}) cannot exceed '
                f'caption_max_length ({self.caption_max_length})'
            )

        # Quality weights should sum to ~1.0 (warning only, not an error)
        weight_sum = (
            self.quality_weight_aesthetic
            + self.quality_weight_sharpness
            + self.quality_weight_pixels
            + self.quality_weight_bpp
        )
        if abs(weight_sum - 1.0) > 0.05:
            logger.warning(
                f'Quality weights sum to {weight_sum:.3f} (expected ~1.0). Rankings may behave unexpectedly.'
            )


# ---------------------------------------------------------------------------
# Installer-shipped defaults — values that differ from the dataclass defaults
# when a *new* config file is written by save_config().  The dataclass holds
# the fallback when a key is absent from YAML; this dict holds the "first-run
# experience" values shipped in the template.
# ---------------------------------------------------------------------------
_TEMPLATE_OVERRIDES: dict[str, Any] = {
    'indexing_threads': 8,
    'similarity_threshold_level2': 0.93,
    'face_detection_min_size': 60,
    'face_recognition_threshold': 0.70,
}


# ---------------------------------------------------------------------------
# save_config — write a Config to disk with full comments
# ---------------------------------------------------------------------------


def _format_yaml_value(value: Any, field_name: str) -> str:
    """Format a single Config field value as a YAML string.

    Handles booleans, numbers, strings, and the special ``image_extensions``
    list.  Returns the formatted text *without* a trailing newline.
    """
    if field_name == 'image_extensions':
        # Use the canonical ordered list with inline comment for RAW formats
        lines: list[str] = [f'{field_name}:']
        for item in _IMAGE_EXTENSIONS_ORDERED:
            if isinstance(item, tuple):
                # Inline comment line (e.g. "# Camera RAW formats")
                comment_text, _ = item
                lines.append(f'  {comment_text}')
            else:
                lines.append(f'  - {item}')
        return '\n'.join(lines)

    if field_name == 'video_extensions':
        # Use the canonical ordered list for consistent output
        lines: list[str] = [f'{field_name}:']
        for ext in _VIDEO_EXTENSIONS_ORDERED:
            lines.append(f'  - {ext}')
        return '\n'.join(lines)

    if isinstance(value, bool):
        return f'{field_name}: {str(value).lower()}'
    elif isinstance(value, (float, int)):
        return f'{field_name}: {value}'
    elif isinstance(value, str):
        if value == '':
            # Write empty strings explicitly so YAML parser always sees the key
            # (prevents the config upgrade logic from treating it as "missing")
            return f"{field_name}: ''"
        # Quote strings that contain special YAML characters or backslashes.
        # Use single quotes (YAML literal strings) to avoid backslash
        # interpretation — important for Windows paths like C:\Users\...
        if any(c in value for c in ':#{}[]&*?|>!%@`\\'):
            # Single quotes only need escaping for embedded single quotes
            escaped = value.replace("'", "''")
            return f"{field_name}: '{escaped}'"
        return f'{field_name}: {value}'
    elif isinstance(value, list):
        if not value:
            return f'{field_name}: []'
        # YAML block list — one item per line, quoting items with special chars
        lines: list[str] = [f'{field_name}:']
        for item in value:
            s = str(item)
            if any(c in s for c in ':#{}[]&*?|>!%@`\\\'"'):
                escaped = s.replace("'", "''")
                lines.append(f"  - '{escaped}'")
            else:
                lines.append(f'  - {s}')
        return '\n'.join(lines)
    else:
        return f'{field_name}: {value}'


def save_config(config: Config, config_path: Path | str) -> None:
    """Write a Config object to disk as a commented YAML file.

    The output preserves all section headers and per-field comments defined in
    ``CONFIG_SCHEMA``.  Fields that were added since the file was last written
    are included automatically (config upgrade).

    Args:
        config: The Config object to serialise.
        config_path: Destination file path.
    """
    config_path = Path(config_path)
    lines: list[str] = [
        '# Photonarium Configuration File',
        '# ============================',
        '# This file controls the behaviour of the Photonarium image database.',
        '# Edit values as needed. Delete this file to reset to defaults.',
    ]

    for section_header, section_fields in CONFIG_SCHEMA:
        # Section separator
        lines.append('')
        lines.append(f'# {"─" * 78}')
        lines.append(f'# {section_header}')
        lines.append(f'# {"─" * 78}')

        for field_name, comment_lines in section_fields:
            # Blank line before each field (visual spacing)
            lines.append('')

            # Comment lines — strip [!] and [M] prefixes used by the schema
            # API; they are metadata for the frontend, not for the YAML file
            for comment in comment_lines:
                if comment == '':
                    lines.append('#')
                else:
                    clean = comment
                    for prefix in ('[!] ', '[M] '):
                        if clean.startswith(prefix):
                            clean = clean[4:]
                            break
                    lines.append(f'# {clean}')

            # Value
            value = getattr(config, field_name)
            lines.append(_format_yaml_value(value, field_name))

    lines.append('')  # Trailing newline
    config_path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------------------
# get_config_schema — schema + current values for the settings editor
# ---------------------------------------------------------------------------

# Map from Python type annotation strings to JSON-compatible type names
_TYPE_MAP: dict[str, str] = {
    'str': 'string',
    'int': 'integer',
    'float': 'number',
    'bool': 'boolean',
    'set[str]': 'set',
    'list[str]': 'list',
}


def get_config_schema(config: Config) -> dict[str, Any]:
    """Build a JSON-serialisable schema describing all config fields.

    The schema is consumed by the frontend settings editor.  Each field
    includes its current value, JSON type, help text, optional numeric
    constraints, and a ``warning`` flag for dangerous settings.

    Args:
        config: The currently loaded Config instance.

    Returns:
        ``{"sections": [...]}`` where each section has a ``title`` and a
        list of ``fields``.  Each field dict contains ``key``, ``value``,
        ``type``, ``comment``, optional ``constraints``, and optional
        ``warning``.
    """
    # Build a lookup of field name → Python type string
    field_types: dict[str, str] = {f.name: f.type for f in fields(Config)}

    sections: list[dict[str, Any]] = []

    for section_title, section_fields in CONFIG_SCHEMA:
        field_defs: list[dict[str, Any]] = []

        for field_name, comment_lines in section_fields:
            # Detect and strip the [!] warning and [M] model-affecting prefixes
            warning = False
            model_field = False
            cleaned_comments: list[str] = []
            for line in comment_lines:
                if line.startswith('[!] '):
                    warning = True
                    cleaned_comments.append(line[4:])
                elif line.startswith('[M] '):
                    model_field = True
                    cleaned_comments.append(line[4:])
                else:
                    cleaned_comments.append(line)

            # Join comment lines into help text (empty lines become newlines)
            comment = '\n'.join(cleaned_comments).strip()

            # Current value
            value = getattr(config, field_name)
            # Convert set to sorted list for JSON serialisation
            if isinstance(value, set):
                value = sorted(value)

            # Derive JSON type from the dataclass field annotation
            py_type = field_types.get(field_name, 'str')
            json_type = _TYPE_MAP.get(py_type, 'string')

            entry: dict[str, Any] = {
                'key': field_name,
                'value': value,
                'type': json_type,
                'comment': comment,
            }

            # Attach numeric constraints if defined
            if field_name in FIELD_CONSTRAINTS:
                entry['constraints'] = dict(FIELD_CONSTRAINTS[field_name])

            # Attach enumerated choices if defined
            if field_name in FIELD_CHOICES:
                entry['choices'] = FIELD_CHOICES[field_name]

            if warning:
                entry['warning'] = True
            if model_field:
                entry['model'] = True

            field_defs.append(entry)

        sections.append(
            {
                'title': section_title,
                'fields': field_defs,
            }
        )

    return {
        'sections': sections,
        'presets': [{'id': pid, 'label': lbl, 'values': vals} for pid, lbl, vals in HARDWARE_PRESETS],
        'language_recommendations': LANGUAGE_RECOMMENDATIONS,
    }


# ---------------------------------------------------------------------------
# load_config — load from YAML with auto-migration and config upgrade
# ---------------------------------------------------------------------------


def _parse_config_data(config_data: dict[str, Any]) -> Config:
    """Build a Config from a parsed YAML dict, coercing types as needed.

    Keys present in the dict are mapped to Config fields; missing keys fall
    back to the dataclass defaults.
    """
    # Map of field name -> type coercion function
    _FIELD_TYPES: dict[str, type] = {f.name: f.type for f in fields(Config)}

    kwargs: dict[str, Any] = {}

    for field_name, field_type in _FIELD_TYPES.items():
        if field_name not in config_data:
            continue

        raw = config_data[field_name]

        # Coerce to the expected type
        if field_type == 'str':
            kwargs[field_name] = str(raw) if raw is not None else ''
        elif field_type == 'int':
            kwargs[field_name] = int(raw)
        elif field_type == 'float':
            kwargs[field_name] = float(raw)
        elif field_type == 'bool':
            kwargs[field_name] = bool(raw)
        elif field_type == 'set[str]':
            kwargs[field_name] = set(raw) if raw else set()
        else:
            kwargs[field_name] = raw

    return Config(**kwargs)


def _try_migrate_legacy_config(new_path: Path) -> Config | None:
    """Attempt to migrate a legacy ``.photonarium.yml`` from cwd.

    If a legacy config is found in the current working directory, it is loaded,
    ``data_dir`` is set to the absolute cwd path, and the result is saved to
    ``new_path``.  The old file is left untouched.

    Returns:
        A Config if migration succeeded, or None if no legacy file was found.
    """
    legacy_path = Path.cwd() / _LEGACY_CONFIG_NAME
    if not legacy_path.exists():
        return None

    logger.info(f'Found legacy config at {legacy_path}, migrating to {new_path}')

    # Load the legacy file
    config_text = legacy_path.read_text(encoding='utf-8')
    config_data = yaml.safe_load(config_text)

    if config_data is None:
        config_data = {}

    # Inject data_dir pointing at the old working directory
    config_data['data_dir'] = str(Path.cwd().resolve())

    config = _parse_config_data(config_data)

    # Save to new location
    new_path.parent.mkdir(parents=True, exist_ok=True)
    save_config(config, new_path)
    logger.info(f'Migrated legacy config to {new_path} (data_dir={config.data_dir})')

    return config


def load_config(
    config_path: Path | str | None = None,
    initial_data_dir: str | None = None,
) -> Config:
    """Load configuration from YAML file, creating default if not exists.

    Resolution order for config_path:
        1. Explicit ``config_path`` argument (from ``--config`` CLI flag)
        2. ``PHOTONARIUM_CONFIG`` environment variable
        3. OS-default path from ``get_default_config_path()``

    When creating a new config file (no existing file found), ``initial_data_dir``
    is used to set the ``data_dir`` field.  This is how the installer persists the
    user's chosen data directory into the config.

    Args:
        config_path: Explicit path to configuration file, or None for auto-resolution.
        initial_data_dir: Data directory to inject when creating a new config.
            Only used during file creation (--init-config / first run).

    Returns:
        Config object with loaded (or default) values.

    Raises:
        ValueError: If configuration values are invalid.
        yaml.YAMLError: If YAML parsing fails.
    """
    # --- Resolve config path ---
    if config_path is not None:
        config_path = Path(config_path)
    else:
        env_path = os.environ.get('PHOTONARIUM_CONFIG')
        if env_path:
            config_path = Path(env_path)
        else:
            config_path = get_default_config_path()

    # Ensure parent directory exists
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Load or create ---
    if not config_path.exists():
        # Try migrating legacy config from cwd
        migrated = _try_migrate_legacy_config(config_path)
        if migrated is not None:
            return migrated

        # No legacy file either — create fresh config with defaults
        logger.info(f'Creating default configuration file: {config_path}')

        # Apply template overrides for a nicer first-run experience
        create_kwargs: dict[str, Any] = dict(_TEMPLATE_OVERRIDES)
        if initial_data_dir is not None:
            create_kwargs['data_dir'] = initial_data_dir

        config = Config(**create_kwargs)
        save_config(config, config_path)
        return config

    # --- Existing file: load it ---
    logger.info(f'Loading configuration from: {config_path}')
    config_text = config_path.read_text(encoding='utf-8')
    config_data = yaml.safe_load(config_text)

    if config_data is None:
        # Empty file, use defaults
        logger.warning('Configuration file is empty, using defaults')
        config_data = {}

    config = _parse_config_data(config_data)

    # --- Config upgrade: re-save to pick up any new fields ---
    # Compare the set of fields in the YAML with the set of fields in the
    # dataclass.  If the YAML is missing any, re-save so the user gets the
    # new settings with comments.
    all_field_names = {f.name for f in fields(Config)}
    yaml_keys = set(config_data.keys())
    if not all_field_names.issubset(yaml_keys):
        missing = all_field_names - yaml_keys
        logger.info(f'Config upgrade: adding new settings {sorted(missing)}')
        save_config(config, config_path)

    return config


def get_default_config() -> Config:
    """Get a Config object with all default values.

    Returns:
        Config object with default values.
    """
    return Config()
