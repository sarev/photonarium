"""
NIMA (Neural IMage Assessment) model for image aesthetic quality scoring.

Implements the MobileNetV2-based NIMA architecture from Talebi & Milanfar (2018).
The model predicts a probability distribution over aesthetic ratings 1-10,
and the weighted mean of that distribution serves as the aesthetic score.

Uses the pretrained checkpoint from truskovskiyk/nima.pytorch (MIT licence)
which was trained on the AVA (Aesthetic Visual Analysis) dataset (~255k images).
The MobileNetV2 backbone is lightweight (~9MB) and runs efficiently on both
GPU and CPU.

This is a standalone implementation using only torch and torchvision (already
installed for OpenCLIP and facenet-pytorch). No additional dependencies needed.

Usage:
    model = load_nima_model('/path/to/nima-mobilenetv2-ava.pth', device='cuda')
    scores = score_images_batch(model, pil_images, device='cuda')
    # scores: list of floats in [1, 10]

Reference:
    Talebi, H. & Milanfar, P. (2018). NIMA: Neural Image Assessment.
    IEEE Transactions on Image Processing, 27(8), 3998-4011.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import torch
import torch.nn as nn
from torchvision import models, transforms

from gputil import is_gpu_error

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)


# Standard ImageNet preprocessing: resize shortest edge to 256, centre-crop
# 224, ImageNet normalise.  Reusable for single-image scoring if needed.
NIMA_TRANSFORM = transforms.Compose(
    [
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]
)


class NIMA(nn.Module):
    """MobileNetV2-based NIMA model.

    Architecture: MobileNetV2 feature extractor (1280-D) → ReLU → Dropout(0.75)
    → Linear(1280, 10) → Softmax.  Output is a 10-class probability
    distribution over aesthetic ratings 1-10.  The weighted mean yields a
    single quality score.

    Matches the architecture from truskovskiyk/nima.pytorch (v1 branch) whose
    pretrained checkpoint is publicly available.
    """

    def __init__(self) -> None:
        super().__init__()
        # Use MobileNetV2 features (everything except final classifier)
        mobilenet = models.mobilenet_v2(weights=None)
        self.base_model = nn.Sequential(*list(mobilenet.children())[:-1])
        self.head = nn.Sequential(
            nn.ReLU(inplace=True),
            nn.Dropout(p=0.75),
            nn.Linear(1280, 10),
            nn.Softmax(dim=1),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Forward pass.

        Args:
            x: Input tensor of shape (N, 3, 224, 224).

        Returns:
            Probability distribution of shape (N, 10) over ratings 1-10.
        """
        out = self.base_model(x)
        # Global average pooling: [N, 1280, H, W] → [N, 1280]
        out = out.mean([2, 3])
        return self.head(out)


def _patch_nima_block1(model: NIMA) -> None:
    """Replace block 1's conv with old-style flat Sequential including expand layer.

    The truskovskiyk/nima.pytorch checkpoint was saved with an older torchvision
    where InvertedResidual block 1 (expand_ratio=1) included an expand 1x1 conv.
    Current torchvision omits that layer when expand_ratio=1.  We reconstruct the
    old flat layout so checkpoint keys (conv.{0,1,3,4,6,7}) map directly onto the
    parameterised layers at the expected indices.
    """
    block1 = model.base_model[0][1]  # features[1] = first InvertedResidual
    block1.conv = nn.Sequential(
        # 0: expand 1x1  (32→32, identity-like — present in old torchvision)
        nn.Conv2d(32, 32, 1, 1, 0, bias=False),
        # 1: expand BN
        nn.BatchNorm2d(32),
        # 2: ReLU6 (no params)
        nn.ReLU6(inplace=True),
        # 3: depthwise 3x3
        nn.Conv2d(32, 32, 3, 1, 1, groups=32, bias=False),
        # 4: depthwise BN
        nn.BatchNorm2d(32),
        # 5: ReLU6 (no params)
        nn.ReLU6(inplace=True),
        # 6: project 1x1  (32→16)
        nn.Conv2d(32, 16, 1, 1, 0, bias=False),
        # 7: project BN
        nn.BatchNorm2d(16),
    )


def _remap_nima_state_dict(state_dict: dict) -> dict:
    """Remap old-format checkpoint keys to current torchvision MobileNetV2 layout.

    The checkpoint uses a flat nn.Sequential for InvertedResidual.conv::

        Old:  conv.{0=expand, 1=BN, 2=ReLU6, 3=dw, 4=BN, 5=ReLU6, 6=proj, 7=BN}

    Current torchvision nests these into ConvBNActivation sub-modules::

        New:  conv.{0.0=expand, 0.1=BN, 0.2=ReLU6, 1.0=dw, 1.1=BN, 1.2=ReLU6, 2=proj, 3=BN}

    Block 1 (expand_ratio=1) is *not* remapped here because its model architecture
    is patched by :func:`_patch_nima_block1` to match the old flat layout directly.
    Only blocks 2–17 (expand_ratio=6) need key remapping.

    Args:
        state_dict: Checkpoint state_dict with old-format keys.

    Returns:
        New state_dict with keys matching current torchvision MobileNetV2.
    """
    # Flat index → nested index for InvertedResidual conv layers
    _INDEX_MAP = {'0': '0.0', '1': '0.1', '3': '1.0', '4': '1.1', '6': '2', '7': '3'}

    new_state_dict = {}
    for key, value in state_dict.items():
        # Only remap InvertedResidual blocks 2–17
        # Key pattern: base_model.0.{block}.conv.{flat_idx}.{suffix}
        parts = key.split('.')
        if len(parts) >= 6 and parts[0] == 'base_model' and parts[3] == 'conv' and parts[2].isdigit():
            block_idx = int(parts[2])
            if 2 <= block_idx <= 17:
                flat_idx = parts[4]
                if flat_idx in _INDEX_MAP:
                    new_key = '.'.join(parts[:4]) + '.' + _INDEX_MAP[flat_idx] + '.' + '.'.join(parts[5:])
                    new_state_dict[new_key] = value
                    continue
        new_state_dict[key] = value

    return new_state_dict


def load_nima_model(checkpoint_path: str, device: str = 'cpu') -> NIMA:
    """Load a NIMA model from a checkpoint file.

    Handles compatibility between the truskovskiyk/nima.pytorch checkpoint
    (saved with older torchvision) and current torchvision's MobileNetV2 by:

    1. Patching block 1 to include the expand layer the checkpoint expects.
    2. Remapping flat conv indices to nested ConvBNActivation indices for blocks 2–17.

    Args:
        checkpoint_path: Path to the .pth checkpoint (MobileNetV2-AVA weights).
        device: Device to load the model onto ('cpu' or 'cuda').

    Returns:
        NIMA model in eval mode, ready for inference.

    Raises:
        FileNotFoundError: If checkpoint_path does not exist.
        RuntimeError: If the checkpoint is incompatible.
    """
    model = NIMA()
    state_dict = torch.load(checkpoint_path, map_location=device, weights_only=True)

    # Check whether remapping is needed (in case future torchvision reverts or
    # a pre-remapped checkpoint is used)
    model_keys = set(model.state_dict().keys())
    ckpt_keys = set(state_dict.keys())
    needs_remap = not ckpt_keys <= model_keys

    if needs_remap:
        _patch_nima_block1(model)
        state_dict = _remap_nima_state_dict(state_dict)

    # strict=False tolerates missing num_batches_tracked counters (absent in
    # old checkpoints, default to 0 in BatchNorm — harmless for inference)
    model.load_state_dict(state_dict, strict=False)
    try:
        model = model.to(device)
    except (MemoryError, RuntimeError) as e:
        if not is_gpu_error(e):
            raise
        logger.error(f'GPU error moving NIMA model to {device}: {e}')
        raise
    model.eval()
    logger.info(f'NIMA model loaded from {checkpoint_path} on {device}')
    return model


def score_images_batch(
    model: NIMA,
    images: list[Image.Image],
    device: str = 'cpu',
) -> list[float]:
    """Score a batch of PIL images for aesthetic quality.

    Preprocesses each image with the standard ImageNet transform, runs
    inference, and returns the weighted mean of the predicted distribution
    as the score.

    Args:
        model: Loaded NIMA model (in eval mode).
        images: List of PIL Image objects (any size/mode — will be resized).
        device: Device the model lives on ('cpu' or 'cuda').

    Returns:
        List of aesthetic scores in [1, 10], one per input image.
    """
    if not images:
        return []

    # Preprocess: convert to RGB, apply transform, stack into batch
    tensors = []
    for img in images:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        tensors.append(NIMA_TRANSFORM(img))

    try:
        batch = torch.stack(tensors).to(device)

        # Inference (no gradient computation needed)
        with torch.no_grad():
            probs = model(batch)  # (N, 10)

        # Weighted mean: sum(prob[i] * (i+1) for i in range(10))
        ratings = torch.arange(1, 11, dtype=torch.float32, device=device)
        scores = (probs * ratings).sum(dim=1)

        return scores.cpu().tolist()
    except (MemoryError, RuntimeError) as e:
        if not is_gpu_error(e):
            raise
        logger.warning(f'OOM scoring batch of {len(tensors)} images, falling back to single-item')
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

        # Single-item fallback
        results = []
        ratings = torch.arange(1, 11, dtype=torch.float32, device=device)
        for t in tensors:
            try:
                single = t.unsqueeze(0).to(device)
                with torch.no_grad():
                    probs = model(single)
                score = (probs * ratings).sum(dim=1)
                results.append(score.item())
            except (MemoryError, RuntimeError):
                logger.warning('OOM on single NIMA image, skipping')
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                results.append(0.0)
        return results
