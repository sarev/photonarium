#!/usr/bin/env python3
"""
Download required ML models for Photonarium.

This script downloads all ML models required by Photonarium:
- OpenCLIP (image embeddings for semantic search)
- BLIP/BLIP-2 (image captioning)
- FaceNet (MTCNN face detection + InceptionResnetV1 face recognition)
- LAION aesthetic head (image quality scoring)
- NIMA (aesthetic scoring)

The script queries app.py --list-models to determine which models are needed
based on the current configuration. This ensures the downloaded models always
match the app's config.py settings.

Usage:
    # Standard usage:
    python download_models.py

    # With custom data directory (for LAION/NIMA weights):
    python download_models.py --data-dir /path/to/data

    # For Docker builds (redirect HuggingFace/PyTorch caches):
    HF_HOME=docker/models/huggingface \\
    TORCH_HOME=docker/models/torch \\
    python download_models.py --data-dir docker/models

Environment Variables:
    HF_HOME: HuggingFace cache directory (default: ~/.cache/huggingface)
    TORCH_HOME: PyTorch cache directory (default: ~/.cache/torch)
    HF_TOKEN: HuggingFace token for faster downloads (optional)
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


def download_facenet_models() -> bool:
    """Download FaceNet face detection and recognition models.

    Downloads:
    - MTCNN weights (bundled with facenet-pytorch, but triggers download on import)
    - InceptionResnetV1 with vggface2 weights (~107MB) for face embeddings

    Returns:
        True if downloaded successfully, False otherwise.
    """
    print(f'\n{"=" * 60}')
    print('Downloading FaceNet models (MTCNN + InceptionResnetV1)')
    print('=' * 60)

    try:
        from facenet_pytorch import MTCNN, InceptionResnetV1

        print('Loading MTCNN (face detection)...')
        # MTCNN weights are bundled, but this ensures they're extracted
        _mtcnn = MTCNN()
        del _mtcnn

        print('Loading InceptionResnetV1 with vggface2 weights (face recognition)...')
        # This downloads the ~107MB vggface2 checkpoint
        _resnet = InceptionResnetV1(pretrained='vggface2').eval()
        del _resnet

        print('FaceNet models downloaded successfully')
        return True
    except Exception as e:
        print(f'Error downloading FaceNet models: {e}', file=sys.stderr)
        return False


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


def download_stt_model(model_size: str = 'base') -> bool:
    """Download a faster-whisper STT model.

    faster-whisper uses CTranslate2 models hosted on HuggingFace. The model
    is downloaded on first use, but pre-downloading avoids runtime delays.

    Skipped if faster-whisper is not installed.

    Args:
        model_size: Whisper model size (tiny, base, small, medium, large-v3).

    Returns:
        True if downloaded (or already present / package missing), False on fatal error.
    """
    print(f'\n{"=" * 60}')
    print(f'Downloading faster-whisper STT model: {model_size}')
    print('=' * 60)

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print('faster-whisper not installed — skipping STT model download.')
        print('Install with: pip install faster-whisper')
        return True  # Non-fatal

    try:
        # Loading the model triggers the download from HuggingFace
        print(f'Downloading model (this may take a while for larger sizes)...')
        _model = WhisperModel(model_size, device='cpu', compute_type='int8')
        del _model
        print('STT model downloaded successfully')
        return True
    except Exception as e:
        print(f'Error downloading STT model: {e}', file=sys.stderr)
        print('Speech-to-text will be unavailable until the model is downloaded.')
        return True  # Non-fatal — app works without it


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='Download required ML models for Photonarium.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  HF_HOME      HuggingFace cache directory (default: ~/.cache/huggingface)
  TORCH_HOME   PyTorch cache directory (default: ~/.cache/torch)
  HF_TOKEN     HuggingFace token for faster downloads (optional)

Examples:
  # Standard usage (reads model config from app.py):
  python download_models.py

  # For Docker builds (redirect caches to local directory):
  HF_HOME=docker/models/huggingface TORCH_HOME=docker/models/torch \\
  python download_models.py --data-dir docker/models
""",
    )
    parser.add_argument(
        '-d',
        '--data-dir',
        type=str,
        default=None,
        help='Directory for LAION/NIMA weights (default: current directory)',
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
    print()

    # Show cache locations
    hf_home = os.environ.get('HF_HOME', '~/.cache/huggingface')
    torch_home = os.environ.get('TORCH_HOME', '~/.cache/torch')
    data_dir = args.data_dir or '.'
    print(f'HuggingFace cache: {hf_home}')
    print(f'PyTorch cache:     {torch_home}')
    print(f'Data directory:    {data_dir}')
    print()

    # Get model configuration by querying app.py
    print('Querying required models from app.py...')
    models = get_required_models(data_dir=args.data_dir, config_path=args.config_path)

    print(f'OpenCLIP: {models["openclip"]["model"]} ({models["openclip"]["pretrained"]})')
    print(f'Caption:  {models["caption"]["model"]}')
    stt_cfg = models.get('stt', {})
    if stt_cfg.get('enabled'):
        print(f'STT:      faster-whisper ({stt_cfg.get("model", "base")})')

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

    # Download FaceNet models (MTCNN + InceptionResnetV1)
    if not download_facenet_models():
        success = False

    # Download STT model (always attempt — non-fatal if faster-whisper not installed)
    stt_info = models.get('stt', {})
    download_stt_model(model_size=stt_info.get('model', 'base') if stt_info else 'base')

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
