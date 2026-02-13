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

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import logging
import os
import sys

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
    ('Data Directory', [
        ('data_dir', [
            'Where Photonarium stores its database, thumbnails, and model files.',
            'Set automatically by the installer. Use an absolute path for reliability.',
            'Leave empty to use the current working directory.',
        ]),
    ]),

    ('Server', [
        ('server_host', [
            'Network interface to bind to.',
            '  0.0.0.0   = listen on all interfaces (accessible from other devices on your network)',
            '  127.0.0.1 = localhost only (only this machine can connect)',
        ]),
        ('server_port', [
            'Port number for the web server. Range: 1024-65535',
        ]),
    ]),

    ('Image Processing', [
        ('image_extensions', [
            'File extensions recognised as images (lowercase, with leading dot)',
        ]),
        ('thumbnail_quality', [
            'JPEG quality for generated thumbnails (1-100, higher = better quality, larger files)',
        ]),
        ('max_image_dimension', [
            'Maximum image dimension (width or height) to process.',
            'Images larger than this will be downsampled before embedding/hashing.',
            'This prevents memory issues with huge panoramas or scanned images.',
            'Set to 0 to disable (not recommended). Range: 0 or 1024-65536',
        ]),
    ]),

    ('OpenCLIP Embedding Model', [
        ('openclip_model', [
            'The model used for semantic image search and similarity detection.',
            'Changing these settings will require re-embedding all images.',
            '',
            'Model architecture. Common options:',
            '  - ViT-B-32  (fast, good quality, ~400MB VRAM)',
            '  - ViT-B-16  (slower, better quality, ~400MB VRAM)',
            '  - ViT-L-14  (slow, high quality, ~900MB VRAM)',
        ]),
        ('openclip_pretrained', [
            'Pretrained weights. Common options:',
            '  - openai           (original CLIP weights)',
            '  - laion2b_s34b_b79k (trained on LAION-2B, often better for photos)',
            '  - laion400m_e32    (trained on LAION-400M)',
        ]),
        ('embedding_batch_size', [
            'Batch size for embedding computation (1-64)',
            'Higher values are faster but use more VRAM. Reduce if you get out-of-memory errors.',
        ]),
    ]),

    ('Duplicate Detection Thresholds', [
        ('perceptual_hash_threshold', [
            'Perceptual hash hamming distance threshold for "near-identical" (level 1)',
            'Range: 0-64, lower = stricter matching. Recommended: 4-8',
        ]),
        ('similarity_threshold_level2', [
            'Cosine similarity threshold for "similar" images (level 2)',
            'Range: 0.0-1.0, higher = stricter matching. Recommended: 0.93-0.97',
        ]),
        ('similarity_threshold_level3', [
            'Cosine similarity threshold for "related" images (level 3)',
            'Range: 0.0-1.0, higher = stricter matching. Recommended: 0.80-0.90',
        ]),
    ]),

    ('Performance', [
        ('indexing_threads', [
            'Number of threads for parallel image indexing (1-16)',
            'Higher values speed up initial scanning but use more CPU/disk I/O.',
            'Recommended: 4-8 for HDD, 8-16 for SSD',
        ]),
        ('max_incremental_duplicates', [
            'Maximum number of new/modified images to process incrementally for duplicates.',
            'If more images need checking, falls back to full recomputation which is faster',
            'for large batches. Range: 1-10000',
        ]),
        ('incremental_threshold_percent', [
            'Percentage of total images that triggers full recomputation instead of incremental.',
            'If dirty_count > (total_count * threshold), does full rebuild.',
            'Range: 5-50, recommended: 15-25',
        ]),
    ]),

    ('Thumbnail Loading (Frontend)', [
        ('thumbnail_concurrent_requests', [
            'Maximum concurrent thumbnail fetch requests from the browser.',
            'Higher values load thumbnails faster but increase backend load.',
            'Range: 1-12, recommended: 4-8',
        ]),
        ('thumbnail_extra_rows', [
            'Extra rows above/below the viewport to prefetch thumbnails for.',
            'Higher values reduce blank thumbnails when scrolling but increase memory usage.',
            'Range: 1-20, recommended: 3-8',
        ]),
        ('thumbnail_timeout_ms', [
            'Timeout for thumbnail fetch requests in milliseconds.',
            'If a request takes longer than this, it\'s aborted and the slot freed.',
            'Range: 1000-60000, recommended: 5000-15000',
        ]),
        ('thumbnail_scroll_throttle_ms', [
            'Scroll event throttle in milliseconds.',
            'How often the thumbnail queue is re-evaluated during scrolling.',
            'Lower values = more responsive, higher values = less CPU usage.',
            'Range: 50-1000, recommended: 150-300',
        ]),
        ('thumbnail_cache_size_mb', [
            'RAM cache size for thumbnail bytes in megabytes.',
            'Caches recently-accessed thumbnails in memory to avoid disk reads.',
            'Set to 0 to disable caching. Range: 0-1000, recommended: 50-200',
        ]),
    ]),

    ('Face Recognition', [
        ('face_detection_enabled', [
            'Enable face detection during image indexing.',
            'When disabled, face-related UI buttons are greyed out.',
        ]),
        ('face_detection_min_confidence', [
            'MTCNN confidence threshold for face detection.',
            'Higher values = fewer false positives, may miss some faces.',
            'Range: 0.0-1.0, recommended: 0.90-0.99',
        ]),
        ('face_detection_min_size', [
            'Minimum face size in pixels (width/height of bounding box).',
            'Faces smaller than this are ignored. Range: 20-200, recommended: 40-80',
        ]),
        ('face_recognition_threshold', [
            'Cosine similarity threshold for auto-matching faces to known people.',
            'Higher values = stricter matching (fewer false matches, more unknowns).',
            'Range: 0.0-1.0, recommended: 0.65-0.90',
        ]),
        ('face_detection_batch_size', [
            'Batch size for face detection (number of images processed together).',
            'Higher values improve GPU utilization but use more VRAM.',
            'Reduce if you get out-of-memory errors. Range: 1-64, recommended: 16-32',
        ]),
    ]),

    ('Image Captioning (BLIP/BLIP-2)', [
        ('caption_model', [
            'BLIP model to use for caption generation. Options:',
            '  - Salesforce/blip-image-captioning-base   (~1GB, fast)',
            '  - Salesforce/blip-image-captioning-large  (~2GB, better quality)',
            '  - Salesforce/blip2-opt-2.7b               (~5GB, BLIP-2, best quality)',
            '  - Salesforce/blip2-flan-t5-xl             (~8GB, BLIP-2, most descriptive)',
        ]),
        ('caption_max_length', [
            'Maximum length of generated captions in tokens.',
            'Higher values allow longer, more detailed descriptions.',
            'Range: 10-200, recommended: 30-75',
        ]),
        ('caption_min_length', [
            'Minimum length of generated captions in tokens.',
            'Higher values force more descriptive captions.',
            'Range: 1-50, recommended: 5-20',
        ]),
        ('caption_num_beams', [
            'Number of beams for beam search during generation.',
            'Higher values produce better quality but are slower.',
            'Set to 1 for greedy decoding (fastest, lower quality).',
            'Range: 1-10, recommended: 3-5',
        ]),
        ('caption_british_english', [
            'Convert American English spellings to British English in generated captions.',
            'Handles common differences like color\u2192colour, center\u2192centre, gray\u2192grey, etc.',
        ]),
    ]),

    ('NIMA Aesthetic Scoring', [
        ('nima_enabled', [
            'NIMA (Neural IMage Assessment) provides a second aesthetic quality signal',
            'alongside the LAION aesthetic predictor. The two scores are blended for',
            'the Quality sort in Gallery and best-image ranking in duplicate groups.',
            '',
            'Enable NIMA aesthetic scoring during image indexing.',
            'When disabled, the NIMA thread sits idle and quality ranking falls back to',
            'LAION-only. Existing NIMA scores are preserved.',
        ]),
        ('nima_batch_size', [
            'Batch size for NIMA scoring (1-64).',
            'Higher values are faster but use more VRAM (~500MB base for VGG16).',
        ]),
    ]),

    ('Quality Scoring Weights', [
        ('quality_weight_aesthetic', [
            'These weights control how the composite quality score is computed in the',
            'frontend. They are applied at sort time and do not affect stored data.',
            '',
            'Component weights (should sum to ~1.0):',
            '  aesthetic - blended NIMA+LAION aesthetic score (percentile rank)',
            '  sharpness - log Laplacian variance (percentile rank)',
            '  pixels    - total pixel count (percentile rank)',
            '  bpp       - bits per pixel (percentile rank)',
        ]),
        ('quality_weight_sharpness', []),
        ('quality_weight_pixels', []),
        ('quality_weight_bpp', []),
        ('quality_alpha', [
            'Blend ratio for NIMA vs LAION aesthetic scores.',
            'A = alpha * NIMA_normalised + (1 - alpha) * LAION',
            'Set to 0.0 to use LAION only, 1.0 for NIMA only.',
            'Range: 0.0-1.0',
        ]),
    ]),

    ('Trash Directory', [
        ('trash_dir', [
            'When images are deleted (from Gallery, Fullscreen, or duplicate pruning),',
            'they are moved to this directory instead of being permanently removed.',
            'Files keep their original names (with a counter suffix on collision).',
            'Leave empty to use the default: <data-dir>/trash/',
        ]),
    ]),
]

# Ordered list of image extensions for the YAML file (matches the original
# template ordering, with a comment before the RAW formats)
_IMAGE_EXTENSIONS_ORDERED: list[str | tuple[str, str]] = [
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
    ('# Camera RAW formats (require rawpy)', ''),
    '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.srf', '.dng', '.raf',
    '.rw2', '.orf', '.pef', '.srw', '.x3f', '.3fr', '.iiq', '.rwl',
    '.kdc', '.dcr', '.erf',
]


@dataclass
class Config:
    """Application configuration with validation.

    Attributes:
        data_dir: Directory for user data (database, thumbnails, models).
            Empty string means use the current working directory. Set by the
            installer; can be overridden at runtime with ``--data-dir``.
        server_host: Network interface to bind to ('0.0.0.0' for LAN, '127.0.0.1' for local only).
        server_port: Port number for the web server (1024-65535).
        image_extensions: Set of lowercase file extensions to treat as images.
        thumbnail_quality: JPEG quality for thumbnails (1-100).
        max_image_dimension: Max width/height before downsampling (0 to disable).
        openclip_model: OpenCLIP model architecture name.
        openclip_pretrained: OpenCLIP pretrained weights name.
        embedding_batch_size: Batch size for embedding computation.
        perceptual_hash_threshold: Hamming distance threshold for level 1 duplicates.
        similarity_threshold_level2: Cosine similarity threshold for level 2.
        similarity_threshold_level3: Cosine similarity threshold for level 3.
        indexing_threads: Number of threads for parallel image indexing (1-16).
        max_incremental_duplicates: Max dirty images for incremental duplicate detection.
            If more images need checking, falls back to full recomputation.
        incremental_threshold_percent: Percentage of total images that triggers full
            recomputation instead of incremental (5-50).
        thumbnail_concurrent_requests: Max concurrent thumbnail fetch requests (1-12).
        thumbnail_extra_rows: Extra rows above/below viewport to prefetch (1-20).
        thumbnail_timeout_ms: Timeout for thumbnail fetch requests in ms (1000-60000).
        thumbnail_scroll_throttle_ms: Scroll event throttle in ms (50-1000).
        thumbnail_cache_size_mb: RAM cache size for thumbnail bytes in MB (0-1000).
        face_detection_enabled: Whether to detect faces during image indexing.
        face_detection_min_confidence: MTCNN confidence threshold (0.0-1.0).
        face_detection_min_size: Minimum face size in pixels (20-200).
        face_recognition_threshold: Cosine similarity threshold for auto-matching (0.0-1.0).
        face_detection_batch_size: Batch size for face detection (1-64).
        caption_model: BLIP/BLIP-2 model name for captioning.
        caption_max_length: Maximum caption length in tokens (10-200).
        caption_min_length: Minimum caption length in tokens (1-50).
        caption_num_beams: Beam search width for generation (1-10).
        caption_british_english: Convert US spellings to UK in captions.
        nima_enabled: Whether to run NIMA aesthetic scoring during indexing.
        nima_batch_size: Batch size for NIMA scoring (1-64).
        quality_weight_aesthetic: Weight for aesthetic component in quality sort.
        quality_weight_sharpness: Weight for sharpness component in quality sort.
        quality_weight_pixels: Weight for pixel count component in quality sort.
        quality_weight_bpp: Weight for bits-per-pixel component in quality sort.
        quality_alpha: Blend ratio for NIMA vs LAION aesthetic scores (0-1).
        trash_dir: Path to trash directory for deleted images. Empty string means
            use the default (<data-dir>/trash/). Set to a custom path to move
            trashed images elsewhere.
    """

    data_dir: str = ''
    server_host: str = '0.0.0.0'
    server_port: int = 5000
    image_extensions: set[str] = field(default_factory=lambda: {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
        # Camera RAW formats (require rawpy)
        '.cr2', '.cr3', '.nef', '.nrw', '.arw', '.srf', '.dng', '.raf',
        '.rw2', '.orf', '.pef', '.srw', '.x3f', '.3fr', '.iiq', '.rwl',
        '.kdc', '.dcr', '.erf',
    })
    thumbnail_quality: int = 85
    max_image_dimension: int = 16384
    openclip_model: str = 'ViT-B-32'
    openclip_pretrained: str = 'openai'
    embedding_batch_size: int = 16
    perceptual_hash_threshold: int = 4
    similarity_threshold_level2: float = 0.93
    similarity_threshold_level3: float = 0.85
    indexing_threads: int = 4
    max_incremental_duplicates: int = 500
    incremental_threshold_percent: int = 20
    thumbnail_concurrent_requests: int = 6
    thumbnail_extra_rows: int = 5
    thumbnail_timeout_ms: int = 10000
    thumbnail_scroll_throttle_ms: int = 250
    thumbnail_cache_size_mb: int = 100
    face_detection_enabled: bool = True
    face_detection_min_confidence: float = 0.95
    face_detection_min_size: int = 40
    face_recognition_threshold: float = 0.65
    face_detection_batch_size: int = 32
    caption_model: str = 'Salesforce/blip-image-captioning-large'
    caption_max_length: int = 50
    caption_min_length: int = 10
    caption_num_beams: int = 5
    caption_british_english: bool = False
    nima_enabled: bool = True
    nima_batch_size: int = 16
    quality_weight_aesthetic: float = 0.60
    quality_weight_sharpness: float = 0.20
    quality_weight_pixels: float = 0.15
    quality_weight_bpp: float = 0.05
    quality_alpha: float = 0.60
    trash_dir: str = ''

    def __post_init__(self) -> None:
        """Validate configuration values after initialisation."""
        self._validate()

    def _validate(self) -> None:
        """Validate all configuration values are within acceptable ranges.

        Raises:
            ValueError: If any configuration value is invalid.
        """
        # Validate data_dir (must be a string, can be empty)
        if not isinstance(self.data_dir, str):
            raise ValueError('data_dir must be a string')

        # Validate server settings
        if not isinstance(self.server_host, str) or not self.server_host:
            raise ValueError('server_host must be a non-empty string')
        if not 1024 <= self.server_port <= 65535:
            raise ValueError(f'server_port must be 1024-65535, got {self.server_port}')

        # Validate image_extensions
        if not isinstance(self.image_extensions, (set, list, tuple)):
            raise ValueError('image_extensions must be a collection')
        self.image_extensions = {
            ext.lower() if ext.startswith('.') else f'.{ext.lower()}'
            for ext in self.image_extensions
        }

        # Validate thumbnail_quality
        if not 1 <= self.thumbnail_quality <= 100:
            raise ValueError(f'thumbnail_quality must be 1-100, got {self.thumbnail_quality}')

        # Validate max_image_dimension (0 = disabled, or 1024-65536)
        if self.max_image_dimension != 0 and not 1024 <= self.max_image_dimension <= 65536:
            raise ValueError(f'max_image_dimension must be 0 or 1024-65536, got {self.max_image_dimension}')

        # Validate embedding_batch_size
        if not 1 <= self.embedding_batch_size <= 64:
            raise ValueError(f'embedding_batch_size must be 1-64, got {self.embedding_batch_size}')

        # Validate perceptual_hash_threshold
        if not 0 <= self.perceptual_hash_threshold <= 64:
            raise ValueError(f'perceptual_hash_threshold must be 0-64, got {self.perceptual_hash_threshold}')

        # Validate similarity thresholds
        if not 0.0 <= self.similarity_threshold_level2 <= 1.0:
            raise ValueError(f'similarity_threshold_level2 must be 0.0-1.0, got {self.similarity_threshold_level2}')
        if not 0.0 <= self.similarity_threshold_level3 <= 1.0:
            raise ValueError(f'similarity_threshold_level3 must be 0.0-1.0, got {self.similarity_threshold_level3}')

        # Validate openclip_model and openclip_pretrained are non-empty strings
        if not self.openclip_model or not isinstance(self.openclip_model, str):
            raise ValueError('openclip_model must be a non-empty string')
        if not self.openclip_pretrained or not isinstance(self.openclip_pretrained, str):
            raise ValueError('openclip_pretrained must be a non-empty string')

        # Validate indexing_threads
        if not 1 <= self.indexing_threads <= 16:
            raise ValueError(f'indexing_threads must be 1-16, got {self.indexing_threads}')

        # Validate max_incremental_duplicates
        if not 1 <= self.max_incremental_duplicates <= 10000:
            raise ValueError(f'max_incremental_duplicates must be 1-10000, got {self.max_incremental_duplicates}')

        # Validate incremental_threshold_percent
        if not 5 <= self.incremental_threshold_percent <= 50:
            raise ValueError(f'incremental_threshold_percent must be 5-50, got {self.incremental_threshold_percent}')

        # Validate thumbnail loading settings
        if not 1 <= self.thumbnail_concurrent_requests <= 12:
            raise ValueError(f'thumbnail_concurrent_requests must be 1-12, got {self.thumbnail_concurrent_requests}')
        if not 1 <= self.thumbnail_extra_rows <= 20:
            raise ValueError(f'thumbnail_extra_rows must be 1-20, got {self.thumbnail_extra_rows}')
        if not 1000 <= self.thumbnail_timeout_ms <= 60000:
            raise ValueError(f'thumbnail_timeout_ms must be 1000-60000, got {self.thumbnail_timeout_ms}')
        if not 50 <= self.thumbnail_scroll_throttle_ms <= 1000:
            raise ValueError(f'thumbnail_scroll_throttle_ms must be 50-1000, got {self.thumbnail_scroll_throttle_ms}')
        if not 0 <= self.thumbnail_cache_size_mb <= 1000:
            raise ValueError(f'thumbnail_cache_size_mb must be 0-1000, got {self.thumbnail_cache_size_mb}')

        # Validate face detection settings
        if not isinstance(self.face_detection_enabled, bool):
            raise ValueError('face_detection_enabled must be a boolean')
        if not 0.0 <= self.face_detection_min_confidence <= 1.0:
            raise ValueError(f'face_detection_min_confidence must be 0.0-1.0, got {self.face_detection_min_confidence}')
        if not 20 <= self.face_detection_min_size <= 200:
            raise ValueError(f'face_detection_min_size must be 20-200, got {self.face_detection_min_size}')
        if not 0.0 <= self.face_recognition_threshold <= 1.0:
            raise ValueError(f'face_recognition_threshold must be 0.0-1.0, got {self.face_recognition_threshold}')
        if not 1 <= self.face_detection_batch_size <= 64:
            raise ValueError(f'face_detection_batch_size must be 1-64, got {self.face_detection_batch_size}')

        # Validate caption settings
        if not self.caption_model or not isinstance(self.caption_model, str):
            raise ValueError('caption_model must be a non-empty string')
        if not 10 <= self.caption_max_length <= 200:
            raise ValueError(f'caption_max_length must be 10-200, got {self.caption_max_length}')
        if not 1 <= self.caption_min_length <= 50:
            raise ValueError(f'caption_min_length must be 1-50, got {self.caption_min_length}')
        if self.caption_min_length > self.caption_max_length:
            raise ValueError(f'caption_min_length ({self.caption_min_length}) cannot exceed caption_max_length ({self.caption_max_length})')
        if not 1 <= self.caption_num_beams <= 10:
            raise ValueError(f'caption_num_beams must be 1-10, got {self.caption_num_beams}')
        if not isinstance(self.caption_british_english, bool):
            raise ValueError('caption_british_english must be a boolean')

        # Validate NIMA settings
        if not isinstance(self.nima_enabled, bool):
            raise ValueError('nima_enabled must be a boolean')
        if not 1 <= self.nima_batch_size <= 64:
            raise ValueError(f'nima_batch_size must be 1-64, got {self.nima_batch_size}')

        # Validate quality scoring weights
        weight_sum = (self.quality_weight_aesthetic + self.quality_weight_sharpness
                      + self.quality_weight_pixels + self.quality_weight_bpp)
        if abs(weight_sum - 1.0) > 0.05:
            logger.warning(
                f'Quality weights sum to {weight_sum:.3f} (expected ~1.0). '
                'Rankings may behave unexpectedly.'
            )
        if not 0.0 <= self.quality_alpha <= 1.0:
            raise ValueError(f'quality_alpha must be 0.0-1.0, got {self.quality_alpha}')


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
    'face_recognition_threshold': 0.90,
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

    if isinstance(value, bool):
        return f'{field_name}: {str(value).lower()}'
    elif isinstance(value, float):
        return f'{field_name}: {value}'
    elif isinstance(value, int):
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

            # Comment lines
            for comment in comment_lines:
                if comment == '':
                    lines.append('#')
                else:
                    lines.append(f'# {comment}')

            # Value
            value = getattr(config, field_name)
            lines.append(_format_yaml_value(value, field_name))

    lines.append('')  # Trailing newline
    config_path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------------------
# load_config — load from YAML with auto-migration and config upgrade
# ---------------------------------------------------------------------------

def _parse_config_data(config_data: dict[str, Any]) -> Config:
    """Build a Config from a parsed YAML dict, coercing types as needed.

    Keys present in the dict are mapped to Config fields; missing keys fall
    back to the dataclass defaults.
    """
    # Map of field name -> type coercion function
    _FIELD_TYPES: dict[str, type] = {
        f.name: f.type for f in fields(Config)
    }

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
        elif field_type == "set[str]":
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
