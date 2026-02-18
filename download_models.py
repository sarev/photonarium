#!/usr/bin/env python3
"""
Download required ML models for Photonarium.

This script reads the configuration from app.py and downloads all required
models from HuggingFace. Run this before first use or after changing model
settings in photonarium.yml.

Usage:
    python download_models.py

The script will:
1. Query app.py for required models based on current configuration
2. Download OpenCLIP embedding model
3. Download BLIP/BLIP-2 captioning model
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request


def get_required_models(
    data_dir: str | None = None,
    config_path: str | None = None,
) -> dict:
    """Get required models by querying app.py --list-models.

    Args:
        data_dir: Optional data directory to forward to app.py, so that
            the returned paths (e.g. laion_head.data_dir) match the
            actual runtime directory.
        config_path: Optional config file path to forward to app.py.
    """
    app_py = os.path.join(os.path.dirname(__file__), 'app', 'app.py')
    cmd = [sys.executable, app_py, '--list-models']
    if config_path is not None:
        cmd.extend(['--config', config_path])
    if data_dir is not None:
        cmd.extend(['--data-dir', data_dir])
    result = subprocess.run(cmd, capture_output=True, text=True)
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
        print('OpenCLIP model downloaded successfully')
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
            from transformers import Blip2ForConditionalGeneration, Blip2Processor

            print('Loading BLIP-2 processor...')
            Blip2Processor.from_pretrained(model_name)
            print('Loading BLIP-2 model (this may take a while)...')
            Blip2ForConditionalGeneration.from_pretrained(model_name)
        else:
            from transformers import BlipForConditionalGeneration, BlipProcessor

            print('Loading BLIP processor...')
            BlipProcessor.from_pretrained(model_name)
            print('Loading BLIP model (this may take a while)...')
            BlipForConditionalGeneration.from_pretrained(model_name)

        print('Caption model downloaded successfully')
        return True
    except Exception as e:
        print(f'Error downloading caption model: {e}', file=sys.stderr)
        return False


def download_laion_head(model: str, pretrained: str, data_dir: str = '.') -> bool:
    """Download the LAION aesthetic predictor head for the configured OpenCLIP model.

    The aesthetic head is a lightweight nn.Linear(embed_dim, 1) checkpoint (~2KB)
    that scores image quality via dot product with the CLIP embedding. Different
    CLIP model architectures require different checkpoints.

    Args:
        model: OpenCLIP model architecture name (e.g. 'ViT-B-32').
        pretrained: OpenCLIP pretrained weights name (e.g. 'openai').
        data_dir: Directory to store the downloaded checkpoint.

    Returns:
        True if downloaded successfully, False otherwise.
    """
    # Map of model architecture -> checkpoint URL (pretrained weights don't affect head)
    # The LAION aesthetic predictor heads are nn.Linear(embed_dim, 1) classifiers.
    # See: https://github.com/LAION-AI/aesthetic-predictor
    _LAION_HEAD_URLS = {
        'ViT-B-16': 'https://github.com/LAION-AI/aesthetic-predictor/blob/main/sa_0_4_vit_b_16_linear.pth?raw=true',
        'ViT-B-32': 'https://github.com/LAION-AI/aesthetic-predictor/blob/main/sa_0_4_vit_b_32_linear.pth?raw=true',
        'ViT-L-14': 'https://github.com/LAION-AI/aesthetic-predictor/blob/main/sa_0_4_vit_l_14_linear.pth?raw=true',
    }

    print(f'\n{"=" * 60}')
    print(f'Downloading LAION aesthetic head for: {model} ({pretrained})')
    print('=' * 60)

    url = _LAION_HEAD_URLS.get(model)
    if url is None:
        print(f'No LAION aesthetic head available for {model} ({pretrained})')
        print('Aesthetic scoring will be disabled — quality ranking will fall back to sharpness/resolution.')
        return True  # Not an error, just unsupported

    dest = os.path.join(data_dir, '.laion-aesthetic-head.pth')
    if os.path.exists(dest):
        print(f'LAION aesthetic head already exists: {dest}')
        return True

    try:
        print(f'Downloading from: {url}')
        urllib.request.urlretrieve(url, dest)
        file_size = os.path.getsize(dest)
        print(f'LAION aesthetic head downloaded successfully ({file_size:,} bytes)')
        return True
    except Exception as e:
        print(f'Error downloading LAION aesthetic head: {e}', file=sys.stderr)
        print('Aesthetic scoring will be disabled — quality ranking will fall back to sharpness/resolution.')
        # Clean up partial download
        if os.path.exists(dest):
            os.remove(dest)
        return True  # Non-fatal — app works without it


def download_nima_model(data_dir: str = '.') -> bool:
    """Download the NIMA MobileNetV2-AVA aesthetic scoring checkpoint.

    NIMA (Neural IMage Assessment) uses a MobileNetV2 backbone trained on the
    AVA dataset to predict aesthetic quality distributions.  The checkpoint is
    ~9MB and stored as ``<data_dir>/.nima-mobilenetv2-ava.pth``.

    Source: truskovskiyk/nima.pytorch (MIT licence), hosted on AWS S3.

    Args:
        data_dir: Directory to store the downloaded checkpoint.

    Returns:
        True if downloaded (or already present), False on fatal error.
    """
    # Publicly-hosted checkpoint from truskovskiyk/nima.pytorch (v1 branch, MIT licence)
    _NIMA_URL = 'https://s3-us-west-1.amazonaws.com/models-nima/pretrain-model.pth'

    print(f'\n{"=" * 60}')
    print('Downloading NIMA aesthetic model (MobileNetV2-AVA)')
    print('=' * 60)

    dest = os.path.join(data_dir, '.nima-mobilenetv2-ava.pth')
    if os.path.exists(dest):
        print(f'NIMA checkpoint already exists: {dest}')
        return True

    try:
        print(f'Downloading from: {_NIMA_URL}')
        urllib.request.urlretrieve(_NIMA_URL, dest)
        file_size = os.path.getsize(dest)
        print(f'NIMA checkpoint downloaded successfully ({file_size:,} bytes)')
        return True
    except Exception as e:
        print(f'Error downloading NIMA checkpoint: {e}', file=sys.stderr)
        print('NIMA aesthetic scoring will be disabled — quality ranking will use LAION only.')
        # Clean up partial download
        if os.path.exists(dest):
            os.remove(dest)
        return True  # Non-fatal — app works without it


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Download required ML models for Photonarium.')
    parser.add_argument(
        '-d',
        '--data-dir',
        type=str,
        default=None,
        help='Directory for user data (forwarded to app.py so paths resolve correctly)',
    )
    parser.add_argument(
        '-c',
        '--config',
        type=str,
        default=None,
        dest='config_path',
        help='Path to configuration file (forwarded to app.py)',
    )
    args = parser.parse_args()

    print('Photonarium Model Downloader')
    print('=' * 60)
    print('This script downloads the ML models required by Photonarium.')
    print('Models are cached in the HuggingFace cache directory.')
    print()

    # Get required models from app.py (forward --config/--data-dir so paths match runtime)
    print('Querying required models from configuration...')
    models = get_required_models(data_dir=args.data_dir, config_path=args.config_path)
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

    # Download LAION aesthetic head (non-fatal if unavailable)
    laion_info = models.get('laion_head', {})
    if laion_info:
        data_dir = laion_info.get('data_dir', '.')
        download_laion_head(
            laion_info['model'],
            laion_info['pretrained'],
            data_dir=data_dir,
        )

    # Download NIMA checkpoint (non-fatal if unavailable)
    nima_info = models.get('nima', {})
    if nima_info and nima_info.get('enabled', True):
        data_dir = nima_info.get('data_dir', '.')
        download_nima_model(data_dir=data_dir)

    print()
    print('=' * 60)
    if success:
        print('All models downloaded successfully!')
        print('You can now run: python app/app.py')
    else:
        print('Some models failed to download. Check errors above.')
        sys.exit(1)


if __name__ == '__main__':
    main()
