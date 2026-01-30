#!/usr/bin/env python3
"""
Download required ML models for Imaginary.

This script reads the configuration from app.py and downloads all required
models from HuggingFace. Run this before first use or after changing model
settings in .imaginary.yml.

Usage:
    python download_models.py

The script will:
1. Query app.py for required models based on current configuration
2. Download OpenCLIP embedding model
3. Download BLIP/BLIP-2 captioning model
"""

from __future__ import annotations

import json
import subprocess
import sys


def get_required_models() -> dict:
    """Get required models by querying app.py --list-models."""
    result = subprocess.run(
        [sys.executable, 'app.py', '--list-models'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f'Error querying models: {result.stderr}', file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def download_openclip_model(model: str, pretrained: str) -> bool:
    """Download an OpenCLIP model."""
    print(f'\n{"=" * 60}')
    print(f'Downloading OpenCLIP model: {model} ({pretrained})')
    print('=' * 60)

    try:
        import open_clip
        # This will download the model if not cached
        model_obj, _, _ = open_clip.create_model_and_transforms(
            model,
            pretrained=pretrained,
        )
        del model_obj  # Free memory
        print(f'OpenCLIP model downloaded successfully')
        return True
    except Exception as e:
        print(f'Error downloading OpenCLIP model: {e}', file=sys.stderr)
        return False


def download_caption_model(model_name: str) -> bool:
    """Download a BLIP/BLIP-2 captioning model."""
    print(f'\n{"=" * 60}')
    print(f'Downloading caption model: {model_name}')
    print('=' * 60)

    try:
        is_blip2 = 'blip2' in model_name.lower() or 'blip-2' in model_name.lower()

        if is_blip2:
            from transformers import Blip2Processor, Blip2ForConditionalGeneration
            print('Loading BLIP-2 processor...')
            Blip2Processor.from_pretrained(model_name)
            print('Loading BLIP-2 model (this may take a while)...')
            Blip2ForConditionalGeneration.from_pretrained(model_name)
        else:
            from transformers import BlipProcessor, BlipForConditionalGeneration
            print('Loading BLIP processor...')
            BlipProcessor.from_pretrained(model_name)
            print('Loading BLIP model (this may take a while)...')
            BlipForConditionalGeneration.from_pretrained(model_name)

        print(f'Caption model downloaded successfully')
        return True
    except Exception as e:
        print(f'Error downloading caption model: {e}', file=sys.stderr)
        return False


def main():
    print('Imaginary Model Downloader')
    print('=' * 60)
    print('This script downloads the ML models required by Imaginary.')
    print('Models are cached in the HuggingFace cache directory.')
    print()

    # Get required models from app.py
    print('Querying required models from configuration...')
    models = get_required_models()
    print(f'OpenCLIP: {models["openclip"]["model"]} ({models["openclip"]["pretrained"]})')
    print(f'Caption:  {models["caption"]["model"]}')

    success = True

    # Download OpenCLIP model
    if not download_openclip_model(
        models['openclip']['model'],
        models['openclip']['pretrained'],
    ):
        success = False

    # Download caption model
    if not download_caption_model(models['caption']['model']):
        success = False

    print()
    print('=' * 60)
    if success:
        print('All models downloaded successfully!')
        print('You can now run: python app.py')
    else:
        print('Some models failed to download. Check errors above.')
        sys.exit(1)


if __name__ == '__main__':
    main()
