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
similarity_threshold_level2: 0.95

# Cosine similarity threshold for "related" images (level 3)
# Range: 0.0-1.0, higher = stricter matching. Recommended: 0.80-0.90
similarity_threshold_level3: 0.85

# ------------------------------------------------------------------------------
# Performance
# ------------------------------------------------------------------------------

# Number of threads for parallel image indexing (1-16)
# Higher values speed up initial scanning but use more CPU/disk I/O.
# Recommended: 4-8 for HDD, 8-16 for SSD
indexing_threads: 4
"""


@dataclass
class Config:
    """Application configuration with validation.

    Attributes:
        image_extensions: Set of lowercase file extensions to treat as images.
        thumbnail_quality: JPEG quality for thumbnails (1-100).
        openclip_model: OpenCLIP model architecture name.
        openclip_pretrained: OpenCLIP pretrained weights name.
        embedding_batch_size: Batch size for embedding computation.
        perceptual_hash_threshold: Hamming distance threshold for level 1 duplicates.
        similarity_threshold_level2: Cosine similarity threshold for level 2.
        similarity_threshold_level3: Cosine similarity threshold for level 3.
        indexing_threads: Number of threads for parallel image indexing (1-16).
    """

    image_extensions: set[str] = field(default_factory=lambda: {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp'
    })
    thumbnail_quality: int = 85
    openclip_model: str = 'ViT-B-32'
    openclip_pretrained: str = 'openai'
    embedding_batch_size: int = 16
    perceptual_hash_threshold: int = 4
    similarity_threshold_level2: float = 0.95
    similarity_threshold_level3: float = 0.85
    indexing_threads: int = 4

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
        logger.warning(f'Configuration file is empty, using defaults')
        return Config()

    # Map YAML keys to Config fields
    kwargs: dict[str, Any] = {}

    if 'image_extensions' in config_data:
        kwargs['image_extensions'] = set(config_data['image_extensions'])

    if 'thumbnail_quality' in config_data:
        kwargs['thumbnail_quality'] = int(config_data['thumbnail_quality'])

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

    return Config(**kwargs)


def get_default_config() -> Config:
    """Get a Config object with all default values.

    Returns:
        Config object with default values.
    """
    return Config()
