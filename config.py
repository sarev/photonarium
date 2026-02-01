"""
Configuration management for the Imaginary image database.

This module handles loading and validating configuration from a YAML file.
If no configuration file exists, a default one is created with sensible defaults.

Usage:
    from config import Config, load_config

    config = load_config()  # Uses default path .imaginary.yml
    config = load_config('/path/to/config.yml')  # Custom path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import logging
import yaml

# Configure module logger
logger = logging.getLogger(__name__)


# Default configuration file path (relative to working directory)
DEFAULT_CONFIG_PATH = Path('.imaginary.yml')

# Default configuration template with comments
# This is written to disk when no config file exists
DEFAULT_CONFIG_TEMPLATE = """\
# Imaginary Configuration File
# ============================
# This file controls the behaviour of the Imaginary image database.
# Edit values as needed. Delete this file to reset to defaults.

# ------------------------------------------------------------------------------
# Image Processing
# ------------------------------------------------------------------------------

# File extensions recognised as images (lowercase, with leading dot)
image_extensions:
  - .jpg
  - .jpeg
  - .png
  - .gif
  - .bmp
  - .tiff
  - .tif
  - .webp

# JPEG quality for generated thumbnails (1-100, higher = better quality, larger files)
thumbnail_quality: 85

# Maximum image dimension (width or height) to process.
# Images larger than this will be downsampled before embedding/hashing.
# This prevents memory issues with huge panoramas or scanned images.
# Set to 0 to disable (not recommended). Range: 0 or 1024-65536
max_image_dimension: 16384

# ------------------------------------------------------------------------------
# OpenCLIP Embedding Model
# ------------------------------------------------------------------------------
# The model used for semantic image search and similarity detection.
# Changing these settings will require re-embedding all images.

# Model architecture. Common options:
#   - ViT-B-32  (fast, good quality, ~400MB VRAM)
#   - ViT-B-16  (slower, better quality, ~400MB VRAM)
#   - ViT-L-14  (slow, high quality, ~900MB VRAM)
openclip_model: ViT-B-32

# Pretrained weights. Common options:
#   - openai           (original CLIP weights)
#   - laion2b_s34b_b79k (trained on LAION-2B, often better for photos)
#   - laion400m_e32    (trained on LAION-400M)
openclip_pretrained: openai

# Batch size for embedding computation (1-64)
# Higher values are faster but use more VRAM. Reduce if you get out-of-memory errors.
embedding_batch_size: 16

# ------------------------------------------------------------------------------
# Duplicate Detection Thresholds
# ------------------------------------------------------------------------------

# Perceptual hash hamming distance threshold for "near-identical" (level 1)
# Range: 0-64, lower = stricter matching. Recommended: 4-8
perceptual_hash_threshold: 4

# Cosine similarity threshold for "similar" images (level 2)
# Range: 0.0-1.0, higher = stricter matching. Recommended: 0.93-0.97
similarity_threshold_level2: 0.93

# Cosine similarity threshold for "related" images (level 3)
# Range: 0.0-1.0, higher = stricter matching. Recommended: 0.80-0.90
similarity_threshold_level3: 0.85

# ------------------------------------------------------------------------------
# Performance
# ------------------------------------------------------------------------------

# Number of threads for parallel image indexing (1-16)
# Higher values speed up initial scanning but use more CPU/disk I/O.
# Recommended: 4-8 for HDD, 8-16 for SSD
indexing_threads: 8

# Maximum number of new/modified images to process incrementally for duplicates.
# If more images need checking, falls back to full recomputation which is faster
# for large batches. Range: 1-10000
max_incremental_duplicates: 500

# Percentage of total images that triggers full recomputation instead of incremental.
# If dirty_count > (total_count * threshold), does full rebuild.
# Range: 5-50, recommended: 15-25
incremental_threshold_percent: 20

# ------------------------------------------------------------------------------
# Thumbnail Loading (Frontend)
# ------------------------------------------------------------------------------

# Maximum concurrent thumbnail fetch requests from the browser.
# Higher values load thumbnails faster but increase backend load.
# Range: 1-12, recommended: 4-8
thumbnail_concurrent_requests: 6

# Extra rows above/below the viewport to prefetch thumbnails for.
# Higher values reduce blank thumbnails when scrolling but increase memory usage.
# Range: 1-20, recommended: 3-8
thumbnail_extra_rows: 5

# Timeout for thumbnail fetch requests in milliseconds.
# If a request takes longer than this, it's aborted and the slot freed.
# Range: 1000-60000, recommended: 5000-15000
thumbnail_timeout_ms: 10000

# Scroll event throttle in milliseconds.
# How often the thumbnail queue is re-evaluated during scrolling.
# Lower values = more responsive, higher values = less CPU usage.
# Range: 50-1000, recommended: 150-300
thumbnail_scroll_throttle_ms: 250

# RAM cache size for thumbnail bytes in megabytes.
# Caches recently-accessed thumbnails in memory to avoid disk reads.
# Set to 0 to disable caching. Range: 0-1000, recommended: 50-200
thumbnail_cache_size_mb: 100

# ------------------------------------------------------------------------------
# Face Recognition
# ------------------------------------------------------------------------------

# Enable face detection during image indexing.
# When disabled, face-related UI buttons are greyed out.
face_detection_enabled: true

# MTCNN confidence threshold for face detection.
# Higher values = fewer false positives, may miss some faces.
# Range: 0.0-1.0, recommended: 0.90-0.99
face_detection_min_confidence: 0.95

# Minimum face size in pixels (width/height of bounding box).
# Faces smaller than this are ignored. Range: 20-200, recommended: 40-80
face_detection_min_size: 60

# Cosine similarity threshold for auto-matching faces to known people.
# Higher values = stricter matching (fewer false matches, more unknowns).
# Range: 0.0-1.0, recommended: 0.65-0.90
face_recognition_threshold: 0.90

# Batch size for face detection (number of images processed together).
# Higher values improve GPU utilization but use more VRAM.
# Reduce if you get out-of-memory errors. Range: 1-64, recommended: 16-32
face_detection_batch_size: 32

# ------------------------------------------------------------------------------
# Image Captioning (BLIP/BLIP-2)
# ------------------------------------------------------------------------------

# BLIP model to use for caption generation. Options:
#   - Salesforce/blip-image-captioning-base   (~1GB, fast)
#   - Salesforce/blip-image-captioning-large  (~2GB, better quality)
#   - Salesforce/blip2-opt-2.7b               (~5GB, BLIP-2, best quality)
#   - Salesforce/blip2-flan-t5-xl             (~8GB, BLIP-2, most descriptive)
caption_model: Salesforce/blip-image-captioning-large

# Maximum length of generated captions in tokens.
# Higher values allow longer, more detailed descriptions.
# Range: 10-200, recommended: 30-75
caption_max_length: 50

# Minimum length of generated captions in tokens.
# Higher values force more descriptive captions.
# Range: 1-50, recommended: 5-20
caption_min_length: 10

# Number of beams for beam search during generation.
# Higher values produce better quality but are slower.
# Set to 1 for greedy decoding (fastest, lower quality).
# Range: 1-10, recommended: 3-5
caption_num_beams: 5

# Convert American English spellings to British English in generated captions.
# Handles common differences like color→colour, center→centre, gray→grey, etc.
caption_british_english: false
"""


@dataclass
class Config:
    """Application configuration with validation.

    Attributes:
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
    """

    image_extensions: set[str] = field(default_factory=lambda: {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp'
    })
    thumbnail_quality: int = 85
    max_image_dimension: int = 16384
    openclip_model: str = 'ViT-B-32'
    openclip_pretrained: str = 'openai'
    embedding_batch_size: int = 16
    perceptual_hash_threshold: int = 4
    similarity_threshold_level2: float = 0.95
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

    def __post_init__(self) -> None:
        """Validate configuration values after initialisation."""
        self._validate()

    def _validate(self) -> None:
        """Validate all configuration values are within acceptable ranges.

        Raises:
            ValueError: If any configuration value is invalid.
        """
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


def load_config(config_path: Path | str | None = None) -> Config:
    """Load configuration from YAML file, creating default if not exists.

    Args:
        config_path: Path to configuration file. If None, uses DEFAULT_CONFIG_PATH.

    Returns:
        Config object with loaded (or default) values.

    Raises:
        ValueError: If configuration values are invalid.
        yaml.YAMLError: If YAML parsing fails.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    else:
        config_path = Path(config_path)

    # Create default config file if it doesn't exist
    if not config_path.exists():
        logger.info(f'Creating default configuration file: {config_path}')
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding='utf-8')
        return Config()  # Return defaults

    # Load existing config
    logger.info(f'Loading configuration from: {config_path}')
    config_text = config_path.read_text(encoding='utf-8')
    config_data = yaml.safe_load(config_text)

    if config_data is None:
        # Empty file, use defaults
        logger.warning('Configuration file is empty, using defaults')
        return Config()

    # Map YAML keys to Config fields
    kwargs: dict[str, Any] = {}

    if 'image_extensions' in config_data:
        kwargs['image_extensions'] = set(config_data['image_extensions'])

    if 'thumbnail_quality' in config_data:
        kwargs['thumbnail_quality'] = int(config_data['thumbnail_quality'])

    if 'max_image_dimension' in config_data:
        kwargs['max_image_dimension'] = int(config_data['max_image_dimension'])

    if 'openclip_model' in config_data:
        kwargs['openclip_model'] = str(config_data['openclip_model'])

    if 'openclip_pretrained' in config_data:
        kwargs['openclip_pretrained'] = str(config_data['openclip_pretrained'])

    if 'embedding_batch_size' in config_data:
        kwargs['embedding_batch_size'] = int(config_data['embedding_batch_size'])

    if 'perceptual_hash_threshold' in config_data:
        kwargs['perceptual_hash_threshold'] = int(config_data['perceptual_hash_threshold'])

    if 'similarity_threshold_level2' in config_data:
        kwargs['similarity_threshold_level2'] = float(config_data['similarity_threshold_level2'])

    if 'similarity_threshold_level3' in config_data:
        kwargs['similarity_threshold_level3'] = float(config_data['similarity_threshold_level3'])

    if 'indexing_threads' in config_data:
        kwargs['indexing_threads'] = int(config_data['indexing_threads'])

    if 'max_incremental_duplicates' in config_data:
        kwargs['max_incremental_duplicates'] = int(config_data['max_incremental_duplicates'])

    if 'incremental_threshold_percent' in config_data:
        kwargs['incremental_threshold_percent'] = int(config_data['incremental_threshold_percent'])

    if 'thumbnail_concurrent_requests' in config_data:
        kwargs['thumbnail_concurrent_requests'] = int(config_data['thumbnail_concurrent_requests'])

    if 'thumbnail_extra_rows' in config_data:
        kwargs['thumbnail_extra_rows'] = int(config_data['thumbnail_extra_rows'])

    if 'thumbnail_timeout_ms' in config_data:
        kwargs['thumbnail_timeout_ms'] = int(config_data['thumbnail_timeout_ms'])

    if 'thumbnail_scroll_throttle_ms' in config_data:
        kwargs['thumbnail_scroll_throttle_ms'] = int(config_data['thumbnail_scroll_throttle_ms'])

    if 'thumbnail_cache_size_mb' in config_data:
        kwargs['thumbnail_cache_size_mb'] = int(config_data['thumbnail_cache_size_mb'])

    if 'face_detection_enabled' in config_data:
        kwargs['face_detection_enabled'] = bool(config_data['face_detection_enabled'])

    if 'face_detection_min_confidence' in config_data:
        kwargs['face_detection_min_confidence'] = float(config_data['face_detection_min_confidence'])

    if 'face_detection_min_size' in config_data:
        kwargs['face_detection_min_size'] = int(config_data['face_detection_min_size'])

    if 'face_recognition_threshold' in config_data:
        kwargs['face_recognition_threshold'] = float(config_data['face_recognition_threshold'])

    if 'face_detection_batch_size' in config_data:
        kwargs['face_detection_batch_size'] = int(config_data['face_detection_batch_size'])

    if 'caption_model' in config_data:
        kwargs['caption_model'] = str(config_data['caption_model'])

    if 'caption_max_length' in config_data:
        kwargs['caption_max_length'] = int(config_data['caption_max_length'])

    if 'caption_min_length' in config_data:
        kwargs['caption_min_length'] = int(config_data['caption_min_length'])

    if 'caption_num_beams' in config_data:
        kwargs['caption_num_beams'] = int(config_data['caption_num_beams'])

    if 'caption_british_english' in config_data:
        kwargs['caption_british_english'] = bool(config_data['caption_british_english'])

    return Config(**kwargs)


def get_default_config() -> Config:
    """Get a Config object with all default values.

    Returns:
        Config object with default values.
    """
    return Config()
