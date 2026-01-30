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
import re
import threading
import time
from pathlib import Path

# Disable tokenizers parallelism to prevent Ctrl+C issues on Windows.
# Must be set before transformers/tokenizers is imported.
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

# Set HuggingFace Hub to offline mode to prevent network calls when cached.
os.environ.setdefault('HF_HUB_OFFLINE', '1')

# Import transformers BEFORE open_clip - open_clip checks for its availability
import transformers
import torch
import open_clip
from PIL import Image

# Configure module logger
logger = logging.getLogger(__name__)

# US to UK spelling conversions
# Substrings are safe to replace anywhere (e.g., "color" catches colored, colorful, etc.)
_US_TO_UK_SUBSTRINGS = [
    # -or → -our (root words)
    ('color', 'colour'),
    ('favor', 'favour'),
    ('flavor', 'flavour'),
    ('harbor', 'harbour'),
    ('honor', 'honour'),
    ('humor', 'humour'),
    ('labor', 'labour'),
    ('neighbor', 'neighbour'),
    ('rumor', 'rumour'),
    ('savior', 'saviour'),
    ('vapor', 'vapour'),
    # -ize → -ise (longer suffixes avoid "size", "prize", "seize")
    ('ognize', 'ognise'),   # recognize
    ('alize', 'alise'),     # realize, specialize, normalize, generalize, localize...
    ('ganize', 'ganise'),   # organize, reorganize
    ('orize', 'orise'),     # authorize, memorize, categorize
    ('icize', 'icise'),     # criticize, publicize
    ('asize', 'asise'),     # emphasize
    ('imize', 'imise'),     # minimize, maximize, optimize
    # -yze → -yse
    ('alyze', 'alyse'),     # analyze, paralyze
    # Doubled consonants before -ing/-ed/-er
    ('aveling', 'avelling'), ('aveled', 'avelled'), ('aveler', 'aveller'),  # travel
    ('anceling', 'ancelling'), ('anceled', 'ancelled'),  # cancel
    ('ueling', 'uelling'), ('ueled', 'uelled'),  # fuel
    ('abeling', 'abelling'), ('abeled', 'abelled'),  # label
    ('odeling', 'odelling'), ('odeled', 'odelled'),  # model
    # Other
    ('jewelry', 'jewellery'),
    ('pajamas', 'pyjamas'),
]

# Whole-word replacements (need word boundaries to avoid false positives)
_US_TO_UK_WORDS = [
    # -er → -re (can't use substring - "er" too common)
    ('center', 'centre'), ('centers', 'centres'), ('centered', 'centred'),
    ('fiber', 'fibre'), ('fibers', 'fibres'),
    ('liter', 'litre'), ('liters', 'litres'),
    ('meter', 'metre'), ('meters', 'metres'),
    ('theater', 'theatre'), ('theaters', 'theatres'),
    # Other whole words
    ('gray', 'grey'),
    ('airplane', 'aeroplane'),
    ('aluminum', 'aluminium'),
    ('catalog', 'catalogue'),
    ('dialog', 'dialogue'),
    ('draft', 'draught'),
    ('license', 'licence'),
    ('plow', 'plough'),
    ('program', 'programme'),
    ('skeptic', 'sceptic'),
    ('tire', 'tyre'),
]


class CaptionGenerator:
    """Image caption generator using OpenCLIP CoCa.

    Uses the CoCa (Contrastive Captioner) model from OpenCLIP for generating
    natural language descriptions of images. The model is loaded lazily on
    first use.

    Attributes:
        temperature: Sampling temperature for generation (0.0-2.0).
            Higher values produce more diverse/creative captions.
            Lower values produce more deterministic captions.
        british_english: If True, convert US spellings to UK spellings.
        device: PyTorch device ('cuda' or 'cpu').
    """

    def __init__(
        self,
        temperature: float = 1.0,
        british_english: bool = False,
        device: str | None = None,
    ):
        """Initialize the caption generator.

        Args:
            temperature: Sampling temperature (0.0-2.0). Default 1.0.
            british_english: Convert US spellings to UK. Default False.
            device: PyTorch device. If None, auto-selects CUDA if available.
        """
        self.temperature = temperature
        self.british_english = british_english
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

    def _convert_to_british(self, text: str) -> str:
        """Convert American English spellings to British English.

        Args:
            text: Input text with potential US spellings.

        Returns:
            Text with US spellings converted to UK equivalents.
        """
        result = text

        # Simple substring replacements (case-preserving)
        for us, uk in _US_TO_UK_SUBSTRINGS:
            if us in result.lower():
                # Case-preserving replace
                def replace_preserving_case(match):
                    original = match.group()
                    if original.isupper():
                        return uk.upper()
                    elif original[0].isupper():
                        return uk[0].upper() + uk[1:]
                    return uk
                result = re.sub(re.escape(us), replace_preserving_case, result, flags=re.IGNORECASE)

        # Whole-word replacements (need word boundaries)
        for us, uk in _US_TO_UK_WORDS:
            pattern = re.compile(r'\b' + re.escape(us) + r'\b', re.IGNORECASE)
            def replace_word(match, uk=uk):
                original = match.group()
                if original.isupper():
                    return uk.upper()
                elif original[0].isupper():
                    return uk[0].upper() + uk[1:]
                return uk
            result = pattern.sub(replace_word, result)

        return result

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

            # Fix common formatting issues from the model
            if caption:
                # Capitalize first letter
                caption = caption[0].upper() + caption[1:]
                # Remove space before final period
                if caption.endswith(' .'):
                    caption = caption[:-2] + '.'
                # Convert US to UK spellings if enabled
                if self.british_english:
                    caption = self._convert_to_british(caption)

            return caption

        except Exception as e:
            logger.error(f'Failed to generate caption for {image_path}: {e}')
            return None
