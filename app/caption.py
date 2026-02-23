"""
Image captioning for the Photonarium image database.

This module provides automatic image description generation using BLIP/BLIP-2
(Bootstrapping Language-Image Pre-training) models from the transformers library.

Supported models (smallest to largest):
- Salesforce/blip-image-captioning-base   (~1GB)
- Salesforce/blip-image-captioning-large  (~2GB, default)
- Salesforce/blip2-opt-2.7b               (~5GB)
- Salesforce/blip2-flan-t5-xl             (~8GB)

The model is loaded lazily on first use to avoid startup delays.

Usage:
    from caption import CaptionGenerator

    generator = CaptionGenerator()
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

# Set HuggingFace Hub to offline mode - models must be pre-downloaded.
# Use download_models.py to download required models before first run.
os.environ['HF_HUB_OFFLINE'] = '1'

import torch

from rawimage import open_image as raw_open_image


def _is_blip2_model(model_name: str) -> bool:
    """Check if the model name refers to a BLIP-2 model."""
    return 'blip2' in model_name.lower() or 'blip-2' in model_name.lower()


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
    ('ognize', 'ognise'),  # recognize
    ('alize', 'alise'),  # realize, specialize, normalize, generalize, localize...
    ('ganize', 'ganise'),  # organize, reorganize
    ('orize', 'orise'),  # authorize, memorize, categorize
    ('icize', 'icise'),  # criticize, publicize
    ('asize', 'asise'),  # emphasize
    ('imize', 'imise'),  # minimize, maximize, optimize
    # -yze → -yse
    ('alyze', 'alyse'),  # analyze, paralyze
    # Doubled consonants before -ing/-ed/-er
    ('aveling', 'avelling'),
    ('aveled', 'avelled'),
    ('aveler', 'aveller'),  # travel
    ('anceling', 'ancelling'),
    ('anceled', 'ancelled'),  # cancel
    ('ueling', 'uelling'),
    ('ueled', 'uelled'),  # fuel
    ('abeling', 'abelling'),
    ('abeled', 'abelled'),  # label
    ('odeling', 'odelling'),
    ('odeled', 'odelled'),  # model
    # Other
    ('jewelry', 'jewellery'),
    ('pajamas', 'pyjamas'),
]

# Whole-word replacements (need word boundaries to avoid false positives)
_US_TO_UK_WORDS = [
    # -er → -re (can't use substring - "er" too common)
    ('center', 'centre'),
    ('centers', 'centres'),
    ('centered', 'centred'),
    ('fiber', 'fibre'),
    ('fibers', 'fibres'),
    ('liter', 'litre'),
    ('liters', 'litres'),
    ('meter', 'metre'),
    ('meters', 'metres'),
    ('theater', 'theatre'),
    ('theaters', 'theatres'),
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
    """Image caption generator using BLIP/BLIP-2.

    Uses BLIP (Bootstrapping Language-Image Pre-training) models from
    Hugging Face transformers for generating natural language descriptions
    of images. Supports both standard BLIP and larger BLIP-2 models.
    The model is loaded lazily on first use.

    Attributes:
        model_name: BLIP model variant to use.
        max_length: Maximum length of generated caption in tokens.
        min_length: Minimum length of generated caption in tokens.
        num_beams: Number of beams for beam search (1 = greedy).
        british_english: If True, convert US spellings to UK spellings.
        device: PyTorch device ('cuda' or 'cpu').
    """

    def __init__(
        self,
        model_name: str = 'Salesforce/blip-image-captioning-large',
        max_length: int = 50,
        min_length: int = 10,
        num_beams: int = 5,
        british_english: bool = False,
        device: str | None = None,
    ):
        """Initialize the caption generator.

        Args:
            model_name: BLIP model to use. Default 'Salesforce/blip-image-captioning-large'.
                Options:
                - 'Salesforce/blip-image-captioning-base' (~1GB, fast)
                - 'Salesforce/blip-image-captioning-large' (~2GB, better quality)
                - 'Salesforce/blip2-opt-2.7b' (~5GB, BLIP-2, best quality)
                - 'Salesforce/blip2-flan-t5-xl' (~8GB, BLIP-2, most descriptive)
            max_length: Maximum caption length in tokens (10-200). Default 50.
            min_length: Minimum caption length in tokens (1-50). Default 10.
            num_beams: Beam search width (1-10). Higher = better but slower. Default 5.
            british_english: Convert US spellings to UK. Default False.
            device: PyTorch device. If None, auto-selects CUDA if available.
        """
        self.model_name = model_name
        self.max_length = max_length
        self.min_length = min_length
        self.num_beams = num_beams
        self.british_english = british_english
        self._device = device
        self._model = None
        self._processor = None
        self._is_blip2 = None  # Set during model loading
        self._load_failed = False
        self._lock = threading.Lock()

    @property
    def device(self) -> str:
        """Get the PyTorch device, auto-detecting if not set."""
        if self._device is None:
            # Priority: CUDA (NVIDIA GPU) > MPS (Apple Silicon) > CPU
            if torch.cuda.is_available():
                self._device = 'cuda'
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                self._device = 'mps'
            else:
                self._device = 'cpu'
            logger.info(f'Caption generator using device: {self._device}')
        return self._device

    def _load_model(self) -> None:
        """Load the BLIP/BLIP-2 model (called on first use)."""
        if self._model is not None:
            return
        if self._load_failed:
            return

        with self._lock:
            if self._model is not None:
                return
            if self._load_failed:
                return

            model_type = 'BLIP-2' if _is_blip2_model(self.model_name) else 'BLIP'
            logger.info('=' * 60)
            logger.info(f'Loading {model_type} image captioning model...')
            logger.info(f'Model: {self.model_name}')
            logger.info(f'Device: {self.device}')
            logger.info('-' * 60)
            logger.info('If this is the first run, the model weights will be downloaded.')
            logger.info('This may take several minutes depending on your connection...')
            logger.info('-' * 60)

            start_time = time.time()

            try:
                # Import and load the appropriate model type
                self._is_blip2 = _is_blip2_model(self.model_name)

                if self._is_blip2:
                    # BLIP-2 models (larger, more capable)
                    from transformers import Blip2ForConditionalGeneration, Blip2Processor

                    self._processor = Blip2Processor.from_pretrained(
                        self.model_name,
                        clean_up_tokenization_spaces=False,
                    )
                    model = Blip2ForConditionalGeneration.from_pretrained(
                        self.model_name,
                        torch_dtype=torch.float16 if self.device != 'cpu' else torch.float32,
                    )
                else:
                    # Standard BLIP models (smaller, faster)
                    from transformers import BlipForConditionalGeneration, BlipProcessor

                    self._processor = BlipProcessor.from_pretrained(
                        self.model_name,
                        clean_up_tokenization_spaces=False,
                    )
                    model = BlipForConditionalGeneration.from_pretrained(
                        self.model_name,
                        torch_dtype=torch.float16 if self.device != 'cpu' else torch.float32,
                    )

                model = model.to(self.device)
                model.eval()

                self._model = model
            except (MemoryError, RuntimeError) as e:
                if not isinstance(e, MemoryError) and 'out of memory' not in str(e).lower():
                    raise  # Re-raise non-OOM RuntimeErrors
                self._load_failed = True
                self._model = None
                self._processor = None
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                logger.error(
                    f'Out of memory loading {model_type} model ({self.model_name}): {e} — image captioning disabled'
                )
                return

            elapsed = time.time() - start_time
            logger.info('-' * 60)
            logger.info(f'{model_type} model loaded in {elapsed:.1f}s')
            logger.info('=' * 60)

    @property
    def model(self):
        """Get the model, loading if necessary."""
        self._load_model()
        return self._model

    @property
    def processor(self):
        """Get the processor, loading model if necessary."""
        self._load_model()
        return self._processor

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
                def replace_preserving_case(match, uk=uk):
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
        max_length: int | None = None,
        min_length: int | None = None,
        num_beams: int | None = None,
    ) -> str | None:
        """Generate a caption for an image.

        Args:
            image_path: Path to the image file.
            max_length: Override max caption length for this generation.
            min_length: Override min caption length for this generation.
            num_beams: Override beam search width for this generation.

        Returns:
            Generated caption string, or None if generation fails.
        """
        try:
            # Load and preprocess image (handles both standard and RAW formats)
            image = raw_open_image(image_path).convert('RGB')
            inputs = self.processor(images=image, return_tensors='pt').to(
                self.device, torch.float16 if self.device != 'cpu' else torch.float32
            )

            # Use provided parameters or instance defaults
            max_len = max_length if max_length is not None else self.max_length
            min_len = min_length if min_length is not None else self.min_length
            beams = num_beams if num_beams is not None else self.num_beams

            # Generate caption using beam search for quality
            with torch.no_grad():
                generated_ids = self.model.generate(
                    **inputs,
                    max_new_tokens=max_len,
                    min_length=min_len,
                    num_beams=beams,
                )

            # Decode tokens to text
            caption = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()

            # Fix common formatting issues
            if caption:
                # Capitalize first letter
                caption = caption[0].upper() + caption[1:]
                # Remove space before final period
                if caption.endswith(' .'):
                    caption = caption[:-2] + '.'
                # Add period if missing
                if caption and not caption.endswith(('.', '!', '?')):
                    caption += '.'
                # Convert US to UK spellings if enabled
                if self.british_english:
                    caption = self._convert_to_british(caption)

            return caption

        except Exception as e:
            logger.error(f'Failed to generate caption for {image_path}: {e}')
            return None
