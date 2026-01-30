"""
Image captioning for the Imaginary image database.

This module provides automatic image description generation using the CoCa
(Contrastive Captioner) model from OpenCLIP. The generate() function requires
the transformers library for text decoding.

The model is loaded lazily on first use to avoid startup delays.

Usage:
    from caption import CaptionGenerator

    generator = CaptionGenerator(temperature=1.0)
    caption = generator.generate(image_path)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

# Disable tokenizers parallelism to prevent Ctrl+C issues on Windows.
# Must be set before transformers/tokenizers is imported.
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

# Import transformers BEFORE open_clip - open_clip checks for its availability
import transformers
import torch
import open_clip
from PIL import Image

# Configure module logger
logger = logging.getLogger(__name__)


class CaptionGenerator:
    """Image caption generator using OpenCLIP CoCa.

    Uses the CoCa (Contrastive Captioner) model from OpenCLIP for generating
    natural language descriptions of images. The model is loaded lazily on
    first use.

    Attributes:
        temperature: Sampling temperature for generation (0.0-2.0).
            Higher values produce more diverse/creative captions.
            Lower values produce more deterministic captions.
        device: PyTorch device ('cuda' or 'cpu').
    """

    def __init__(
        self,
        temperature: float = 1.0,
        device: str | None = None,
    ):
        """Initialize the caption generator.

        Args:
            temperature: Sampling temperature (0.0-2.0). Default 1.0.
            device: PyTorch device. If None, auto-selects CUDA if available.
        """
        self.temperature = temperature
        self._device = device
        self._model = None
        self._transform = None
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        """Get the PyTorch device, auto-detecting if not set."""
        if self._device is None:
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
            logger.info(f'Caption generator using device: {self._device}')
        return self._device

    def _load_model(self) -> None:
        """Load the CoCa model (called on first use)."""
        if self._model is not None:
            return

        with self._lock:
            if self._model is not None:
                return

            logger.info('=' * 60)
            logger.info('Loading CoCa image captioning model (OpenCLIP)...')
            logger.info(f'Device: {self.device}')
            logger.info('-' * 60)
            logger.info('If this is the first run, the model weights will be downloaded.')
            logger.info('This may take a minute depending on your connection...')
            logger.info('-' * 60)

            start_time = time.time()

            # CoCa model - generates captions natively
            model, _, transform = open_clip.create_model_and_transforms(
                model_name='coca_ViT-L-14',
                pretrained='mscoco_finetuned_laion2B-s13B-b90k',
            )
            model = model.to(self.device)
            model.eval()

            self._model = model
            self._transform = transform

            elapsed = time.time() - start_time
            logger.info('-' * 60)
            logger.info(f'CoCa model loaded in {elapsed:.1f}s')
            logger.info('=' * 60)

    @property
    def model(self):
        """Get the model, loading if necessary."""
        self._load_model()
        return self._model

    @property
    def transform(self):
        """Get the image transform, loading model if necessary."""
        self._load_model()
        return self._transform

    def generate(
        self,
        image_path: Path | str,
        temperature: float | None = None,
        max_length: int = 30,
    ) -> str | None:
        """Generate a caption for an image.

        Args:
            image_path: Path to the image file.
            temperature: Override default temperature for this generation.
                If None, uses the instance's temperature setting.
            max_length: Maximum length of generated caption in tokens.

        Returns:
            Generated caption string, or None if generation fails.
        """
        try:
            # Load and preprocess image
            image = Image.open(image_path).convert('RGB')
            image_tensor = self.transform(image).unsqueeze(0).to(self.device)

            # Use provided temperature or instance default
            temp = temperature if temperature is not None else self.temperature

            # Generate caption
            with torch.no_grad(), torch.amp.autocast(device_type=self.device):
                if temp > 0:
                    # Temperature-based sampling for variety
                    generated = self.model.generate(
                        image_tensor,
                        seq_len=max_length,
                        temperature=temp,
                    )
                else:
                    # Greedy decoding (deterministic)
                    generated = self.model.generate(
                        image_tensor,
                        seq_len=max_length,
                    )

            # Decode tokens to text
            caption = open_clip.decode(generated[0]).split('<end_of_text>')[0]
            # Clean up: remove start token and extra whitespace
            caption = caption.replace('<start_of_text>', '').strip()

            return caption

        except Exception as e:
            logger.error(f'Failed to generate caption for {image_path}: {e}')
            return None
