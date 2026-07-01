"""Neural image enhancement — denoise / super-resolution / artefact removal.

Mirrors :mod:`caption` and :mod:`nima` as a **compute-arbiter client**: the
heavy SwinIR inference is submitted to the shared arbiter (via
``run_exclusive``) so it serialises against indexing and search instead of
competing for the GPU.  Photonarium never edits an original in place — an
enhanced result is always written as a *new* image and catalogued as a derived
version (see :func:`imagedb.derived_image_name` and the ``derived_from`` /
``processing_ops`` lineage columns).

Design notes:

- **Tiled inference is the core primitive.**  A large image (or any upscale) is
  processed in overlapping tiles that are feather-blended back together; each
  tile is one ``arbiter.run_exclusive`` call, so interactive work can slip in
  between tiles and a tile transparently re-runs if needed.
- **Adaptive tile sizing.**  The first tile size is guessed from the device (or
  taken from config); on an out-of-memory error the whole image is retried at a
  smaller tile size (the standard shrink-and-retry pattern), and the last good
  size is remembered for the session.
- **GPU-optional.**  Everything works on CPU (slowly); the caller passes the
  device resolved from :class:`gputil.GpuHealth`.

The architecture itself is vendored under ``app/archs/`` and loaded on plain
``torch``; no training framework is required.
"""

from __future__ import annotations

import io
import logging
import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch
from PIL import Image

from arbiter import Priority
from gputil import is_oom_error
from rawimage import open_image

logger = logging.getLogger(__name__)

# Preview crops jump ahead of bulk enhancement work so the dialog stays snappy.
_INTERACTIVE = Priority.INTERACTIVE

# SwinIR's relative-position-index construction calls torch.meshgrid without the
# 'indexing' kwarg, which emits a harmless UserWarning on modern torch (the
# default indexing is exactly what SwinIR expects).  Silence it so it does not
# spam the logs once per tile.
warnings.filterwarnings('ignore', message='torch.meshgrid:.*', category=UserWarning)

# Subdirectory under the data directory holding downloaded enhancement weights.
ENHANCE_WEIGHTS_SUBDIR = '.enhance'

# Overlap (px) between adjacent tiles.  The models restore a tile's centre well
# but its edges poorly (truncated receptive field), so each tile contributes only
# its central core and the outer ~_TILE_OVERLAP/2 px on every side are discarded,
# covered by a neighbour's centre.  This is a direct quality-vs-speed dial: every
# pixel is processed (tile/stride)^2 times, so a big overlap on a small tile
# triples the compute.  ~19% of the (larger) tile keeps edges clean without the
# ~3x redundancy the old 160/384 (42%) setting cost.
_TILE_OVERLAP = 96
# Width (px) of the seam blend centred on each overlap midpoint (the boundary
# between two tiles' cores).  Only this thin band is cross-faded; every other
# pixel is taken verbatim from its nearest tile, so the result is never a wide
# average of two passes.
_BLEND_BAND = 16
# Smallest tile we will shrink to before giving up on an out-of-memory error.
_MIN_TILE = 64
# Per-device starting tile size when config requests auto (0).  Larger tiles
# amortise the fixed overlap (fewer tiles, less redundant edge compute) and cut
# per-tile dispatch overhead; FP16 on CUDA halves the activation memory that
# would otherwise make 512 too big.  On an OOM the shrink-retry drops this.
_AUTO_TILE = {'cuda': 512, 'mps': 512, 'cpu': 512}
# Minimum correlation between an enhanced result and its input before we trust
# it.  Some models (NAFNet on out-of-distribution blur) can diverge into
# high-frequency colour noise — valid-range but uncorrelated garbage; we reject
# rather than catalogue it.  Genuine enhancements stay well above this (>0.8).
_PLAUSIBLE_MIN_CORR = 0.2
# Upper bound on output pixels (after upscaling) we will attempt on the host.
# The blend runs through full-resolution float32 accumulators in RAM (out: 3ch +
# weight: 1ch ≈ 16 bytes/px, plus the uint8 array and PIL image on top), so a 4×
# upscale of a large photo can demand many gigabytes.  Tile shrink-retry can't
# help — it bounds GPU tile memory, not the host output buffers — so we refuse
# up front with a clear message rather than risk an OS out-of-memory kill of the
# whole process.  ~180 MP caps peak host use around ~4 GB; 4× of a ~11 MP photo
# or 2× of a ~45 MP photo still fits.
_MAX_OUTPUT_PIXELS = 180_000_000
# Architectures that are stable under FP16 autocast on CUDA (big speed/memory win).
# SwinIR is excluded: it overflows in FP16 and produces garbage (the plausibility
# guard rejects it), so denoise runs in FP32 — correct over fast.
_FP16_SAFE_ARCHS = frozenset({'restormer', 'rrdbnet'})


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """A user-facing enhancement outcome backed by one model weight file.

    A capability appears in the Enhance dialog only when its ``config_flag`` is
    on *and* its weight file is present in ``<data_dir>/.enhance/`` — which keeps
    the UI honest about what is actually installed.
    """

    key: str
    label: str
    description: str
    weight_filename: str
    # Where download_models.py fetches the weight from (permissively licensed).
    weight_url: str
    # Output scale factor: 1 for restoration (denoise, artefact removal),
    # >1 for super-resolution.
    scale: int
    # Config attribute that gates this capability.
    config_flag: str
    # Which vendored architecture backs this capability ('swinir' or 'rrdbnet').
    arch: str = 'swinir'
    # Keyword arguments for constructing that architecture.
    arch_kwargs: dict[str, Any] = field(default_factory=dict)


# SwinIR-M colour-denoise architecture (DFWB, window 8, mid noise level).
# Matches the 005_colorDN_DFWB_s128w8_SwinIR-M_noise25 checkpoint.
_SWINIR_COLOR_DN = {
    'upscale': 1,
    'in_chans': 3,
    'img_size': 128,
    'window_size': 8,
    'img_range': 1.0,
    'depths': [6, 6, 6, 6, 6, 6],
    'embed_dim': 180,
    'num_heads': [6, 6, 6, 6, 6, 6],
    'mlp_ratio': 2,
    'upsampler': '',
    'resi_connection': '1conv',
}

# RRDBNet super-resolution architecture (Real-ESRGAN x4plus / x2plus — identical
# but for the scale factor, which is baked into each weight file).
_RRDBNET_X4 = {
    'num_in_ch': 3,
    'num_out_ch': 3,
    'scale': 4,
    'num_feat': 64,
    'num_block': 23,
    'num_grow_ch': 32,
}
_RRDBNET_X2 = {**_RRDBNET_X4, 'scale': 2}

# Restormer architecture (from the official test config).  The same network
# backs both the motion-deblurring and single-image defocus-deblurring weights —
# only the checkpoint differs.
_RESTORMER_DEBLUR = {
    'inp_channels': 3,
    'out_channels': 3,
    'dim': 48,
    'num_blocks': [4, 6, 6, 8],
    'num_refinement_blocks': 4,
    'heads': [1, 2, 4, 8],
    'ffn_expansion_factor': 2.66,
    'bias': False,
    'LayerNorm_type': 'WithBias',
    'dual_pixel_task': False,
}

# Registry of enhancement capabilities.  Each is backed by one permissively
# licensed weight file and one vendored architecture.
CAPABILITIES: dict[str, Capability] = {
    'denoise': Capability(
        key='denoise',
        label='Reduce noise',
        description='Remove sensor noise and grain while preserving detail.',
        weight_filename='005_colorDN_DFWB_s128w8_SwinIR-M_noise25.pth',
        weight_url=(
            'https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/005_colorDN_DFWB_s128w8_SwinIR-M_noise25.pth'
        ),
        scale=1,
        config_flag='enhance_denoise_enabled',
        arch='swinir',
        arch_kwargs=_SWINIR_COLOR_DN,
    ),
    'deblur': Capability(
        key='deblur',
        label='Remove motion blur',
        description='Undo camera shake and motion streaks, while keeping soft backgrounds soft.',
        weight_filename='motion_deblurring.pth',
        weight_url='https://github.com/swz30/Restormer/releases/download/v1.0/motion_deblurring.pth',
        scale=1,
        config_flag='enhance_deblur_enabled',
        arch='restormer',
        arch_kwargs=_RESTORMER_DEBLUR,
    ),
    'sharpen': Capability(
        key='sharpen',
        label='Auto-sharpen',
        description='Strongly sharpen a soft or out-of-focus photo (also crispens blurred backgrounds).',
        weight_filename='single_image_defocus_deblurring.pth',
        weight_url=('https://github.com/swz30/Restormer/releases/download/v1.0/single_image_defocus_deblurring.pth'),
        scale=1,
        config_flag='enhance_sharpen_enabled',
        arch='restormer',
        arch_kwargs=_RESTORMER_DEBLUR,
    ),
    'upscale_2x': Capability(
        key='upscale_2x',
        label='Increase resolution (2×)',
        description='Upscale to twice the size with sharp, natural detail.',
        weight_filename='RealESRGAN_x2plus.pth',
        weight_url=('https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth'),
        scale=2,
        config_flag='enhance_upscale_enabled',
        arch='rrdbnet',
        arch_kwargs=_RRDBNET_X2,
    ),
    'upscale_4x': Capability(
        key='upscale_4x',
        label='Increase resolution (4×)',
        description='Upscale to four times the size with sharp, natural detail.',
        weight_filename='RealESRGAN_x4plus.pth',
        weight_url=('https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth'),
        scale=4,
        config_flag='enhance_upscale_enabled',
        arch='rrdbnet',
        arch_kwargs=_RRDBNET_X4,
    ),
}


def weights_dir(data_dir: str) -> Path:
    """Return the directory holding downloaded enhancement weights."""
    return Path(data_dir) / ENHANCE_WEIGHTS_SUBDIR


def capability_weight_path(data_dir: str, cap: Capability) -> Path:
    """Absolute path to a capability's weight file under the data directory."""
    return weights_dir(data_dir) / cap.weight_filename


def available_capabilities(config: Any, data_dir: str) -> list[Capability]:
    """List capabilities that are both enabled in config and have weights present.

    Args:
        config: The application :class:`config.Config`.
        data_dir: Data directory whose ``.enhance/`` subdir holds the weights.

    Returns:
        Capabilities ready to offer in the Enhance dialog.  Empty if the feature
        is disabled or no weights are downloaded.
    """
    if not getattr(config, 'enhance_enabled', False):
        return []
    out = []
    for cap in CAPABILITIES.values():
        if not getattr(config, cap.config_flag, False):
            continue
        if capability_weight_path(data_dir, cap).is_file():
            out.append(cap)
    return out


# ---------------------------------------------------------------------------
# Model cache — at most one enhancement model resident at a time
# ---------------------------------------------------------------------------


class _ModelCache:
    """Holds at most one loaded SwinIR model, swapped when the recipe changes.

    Keeping a single model resident bounds VRAM use: enhancement is on-demand,
    so we trade a reload when the user switches capability for never holding
    several large models at once.  A per-key failure flag prevents retry loops
    when a weight is missing or a load OOMs.
    """

    def __init__(self) -> None:
        self._key: str | None = None
        self._device: str | None = None
        self._model: torch.nn.Module | None = None
        self._failed: set[str] = set()

    def get(self, cap: Capability, device: str, data_dir: str) -> torch.nn.Module:
        """Return the model for *cap* on *device*, loading (and caching) if needed.

        Must be called on the arbiter owner thread (via ``run_exclusive``) so
        loads are serialised against all other GPU work.

        Raises:
            RuntimeError: If the model previously failed to load, or loads fail
                now (missing weight, out of memory, incompatible checkpoint).
        """
        if cap.key in self._failed:
            raise RuntimeError(f'enhancement model {cap.key!r} previously failed to load')
        if self._key == cap.key and self._device == device and self._model is not None:
            return self._model

        # Switching models — release the previous one first.
        self._release()
        path = capability_weight_path(data_dir, cap)
        try:
            self._model = _load_model(cap, path, device)
            self._key = cap.key
            self._device = device
            logger.info('Enhancement model loaded: %s (%s) on %s', cap.key, path.name, device)
            return self._model
        except (MemoryError, RuntimeError, FileNotFoundError) as e:
            self._failed.add(cap.key)
            self._release()
            logger.error('Failed to load enhancement model %s: %s', cap.key, e)
            raise RuntimeError(f'could not load enhancement model {cap.key!r}: {e}') from e

    def _release(self) -> None:
        """Drop the cached model and free GPU memory."""
        self._model = None
        self._key = None
        self._device = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def reset_failure(self, key: str) -> None:
        """Clear a recorded load failure (e.g. after weights are downloaded)."""
        self._failed.discard(key)


def _build_arch(cap: Capability) -> torch.nn.Module:
    """Construct the vendored architecture for a capability (no weights loaded)."""
    if cap.arch == 'swinir':
        from archs.swinir import SwinIR

        return SwinIR(**cap.arch_kwargs)
    if cap.arch == 'rrdbnet':
        from archs.rrdbnet import RRDBNet

        return RRDBNet(**cap.arch_kwargs)
    if cap.arch == 'restormer':
        from archs.restormer import Restormer

        return Restormer(**cap.arch_kwargs)
    raise RuntimeError(f'unknown enhancement architecture {cap.arch!r}')


def _load_model(cap: Capability, weight_path: Path, device: str) -> torch.nn.Module:
    """Construct *cap*'s architecture and load its checkpoint onto *device*."""
    if not weight_path.is_file():
        raise FileNotFoundError(f'enhancement weight not found: {weight_path}')

    model = _build_arch(cap)
    state = torch.load(weight_path, map_location='cpu', weights_only=True)
    # Checkpoints wrap the weights under 'params' or 'params_ema' (SwinIR,
    # Real-ESRGAN); some are a bare state dict.
    for wrapper in ('params_ema', 'params'):
        if isinstance(state, dict) and wrapper in state:
            state = state[wrapper]
            break
    model.load_state_dict(state, strict=True)
    model.eval()
    model = model.to(device)
    if logger.isEnabledFor(logging.DEBUG):
        params = sum(p.numel() for p in model.parameters())
        logger.debug(
            'Built %s: arch=%s, %.1fM params, checkpoint=%s, device=%s',
            cap.key,
            cap.arch,
            params / 1e6,
            weight_path.name,
            device,
        )
    return model


# Module-level cache shared across enhancement jobs (the worker is single-threaded).
_CACHE = _ModelCache()


def _forward_model(cap: Capability, model: torch.nn.Module, x: torch.Tensor) -> torch.Tensor:
    """Run *model* on *x*, handling any input-size constraint the arch imposes.

    RRDBNet pixel-unshuffles its input by its pre-conv factor (scale=2 → ÷2,
    scale=1 → ÷4); Restormer's 4-level encoder needs input divisible by 8.  We
    reflect-pad up to the nearest multiple and crop the (scaled) output back.
    SwinIR pads internally (window alignment), so it needs no handling here.
    """
    if cap.arch == 'rrdbnet':
        factor = {2: 2, 1: 4}.get(cap.scale, 1)
    elif cap.arch == 'restormer':
        factor = 8
    else:
        factor = 1
    h, w = x.shape[-2], x.shape[-1]
    pad_h = (factor - h % factor) % factor
    pad_w = (factor - w % factor) % factor
    if pad_h or pad_w:
        x = torch.nn.functional.pad(x, (0, pad_w, 0, pad_h), mode='reflect')
    # FP16 autocast on CUDA for FP16-safe archs: runs the heavy conv/matmul in
    # half precision (~2-4x faster, ~half the activation memory — what lets us use
    # the 512 tile) while keeping precision-sensitive ops in FP32.  Autocast (not
    # a hard model.half()) because some archs build FP32 tensors internally that a
    # blanket half() would leave mismatched.  SwinIR is excluded entirely — it
    # overflows in FP16 (see _FP16_SAFE_ARCHS).  The output may be FP16; callers
    # cast back.
    if x.is_cuda and cap.arch in _FP16_SAFE_ARCHS:
        with torch.autocast(device_type='cuda', dtype=torch.float16):
            out = model(x)
    else:
        out = model(x)
    if pad_h or pad_w:
        out = out[..., : h * cap.scale, : w * cap.scale]
    return out


class EnhanceUnstable(RuntimeError):
    """Raised when a model's output is implausible (uncorrelated with the input)."""


class EnhanceTooLarge(RuntimeError):
    """Raised when the output would exceed the host memory budget (see _MAX_OUTPUT_PIXELS)."""


def _is_plausible(src: torch.Tensor, out: torch.Tensor) -> bool:
    """Cheap check that *out* actually relates to *src* (Pearson correlation).

    Catches the divergence-into-colour-noise failure mode: such output is valid
    [0, 1] data but essentially uncorrelated with the input.  *src* and *out* may
    differ in size (super-resolution); *out* is area-resized down to compare.
    """
    if out.shape[-2:] != src.shape[-2:]:
        out = torch.nn.functional.interpolate(out, size=src.shape[-2:], mode='area')
    a = src.flatten().float()
    b = out.flatten().float()
    a = a - a.mean()
    b = b - b.mean()
    denom = (a.norm() * b.norm()).clamp_min(1e-8)
    corr = float((a @ b) / denom)
    logger.debug('plausibility check: correlation=%.4f (reject below %.2f)', corr, _PLAUSIBLE_MIN_CORR)
    return corr >= _PLAUSIBLE_MIN_CORR


# ---------------------------------------------------------------------------
# Tiled inference
# ---------------------------------------------------------------------------


def _auto_tile_size(device: str) -> int:
    """Starting tile size for a device when config requests automatic sizing."""
    base = device.split(':', 1)[0]
    return _AUTO_TILE.get(base, 256)


def _tile_starts(length: int, tile: int, stride: int) -> list[int]:
    """Tile start offsets along one axis, with the last tile pinned to the edge."""
    if length <= tile:
        return [0]
    starts = list(range(0, length - tile, stride))
    starts.append(length - tile)
    return starts


def _axis_overlaps(starts: list[int], tile: int) -> list[tuple[int, int]]:
    """Per-tile (overlap-with-previous, overlap-with-next) along one axis, in px.

    The last tile is pinned to the image edge, so overlaps are *not* uniform —
    they can be most of a tile.  The blend must taper over the real overlap, not
    a fixed amount, or it degenerates into a flat 50/50 average of the two tiles.
    """
    out = []
    for i, s in enumerate(starts):
        prev_ov = (starts[i - 1] + tile) - s if i > 0 else 0
        next_ov = (s + tile) - starts[i + 1] if i < len(starts) - 1 else 0
        out.append((max(0, prev_ov), max(0, next_ov)))
    return out


def _blend_ramp(n: int) -> torch.Tensor:
    """Raised-cosine ramp rising from ~0 to ~1 over *n* samples."""
    j = torch.arange(n, dtype=torch.float32)
    return 0.5 - 0.5 * torch.cos(np.pi * (j + 0.5) / n)


def _axis_weight(length: int, left_overlap: int, right_overlap: int, feather: int) -> torch.Tensor:
    """1-D tile blend weight: each tile owns its core, with a thin seam cross-fade.

    A tile is solely responsible for the output up to the *midpoint* of each
    overlap with a neighbour; the handover is a short raised-cosine ramp of width
    *feather* centred on that midpoint (so seams are hidden) and the weight is
    zero beyond it.  Crucially the ramp width is fixed, independent of how large
    the overlap is — so a big overlap (an image only slightly larger than the
    tile) is *not* averaged across its whole width, which would blur the result.
    Adjacent tiles' ramps are exact complements, so weights sum to 1 everywhere.
    """
    w = torch.ones(length)
    if left_overlap > 0:
        mid = left_overlap / 2.0  # core boundary, px from this tile's left edge
        k = min(int(feather), int(left_overlap)) or 1
        lo = max(0, round(mid - k / 2.0))
        hi = min(length, lo + k)
        w[:lo] = 0.0
        if hi > lo:
            w[lo:hi] = _blend_ramp(hi - lo)
    if right_overlap > 0:
        mid = length - right_overlap / 2.0
        k = min(int(feather), int(right_overlap)) or 1
        hi = min(length, round(mid + k / 2.0))
        lo = max(0, hi - k)
        if hi > lo:
            w[lo:hi] = torch.minimum(w[lo:hi], _blend_ramp(hi - lo).flip(0))
        w[hi:] = 0.0
    return w


def _process_tiles(
    img: torch.Tensor,
    run_tile: Callable[[torch.Tensor], torch.Tensor],
    *,
    tile: int,
    scale: int,
    device: str,
    stop_event: Any | None,
    on_progress: Callable[[int, int], None] | None,
) -> torch.Tensor:
    """Run *run_tile* over overlapping tiles of *img* and feather-blend the output.

    The feather-blend accumulators live on the GPU when the finished image fits
    in VRAM, so each tile is blended on-device and the result is copied to the
    host exactly once — no per-tile GPU->CPU transfer, and none of the per-tile
    CPU tensor maths that otherwise pins every core.  For a large upscale that
    won't fit, the accumulators fall back to the host.

    Args:
        img: Input tensor, shape (1, C, H, W), values in [0, 1], on CPU.
        run_tile: Callable that maps one input tile to its enhanced tile, left on
            *device* (shape (1, C, th*scale, tw*scale)); may be FP16.
        tile: Tile size in input pixels.
        scale: Output upscale factor.
        device: Device the tiles are produced on ('cuda[:n]', 'mps', 'cpu').
        stop_event: Optional event; if set between tiles, raises :class:`EnhanceAborted`.
        on_progress: Optional callback ``(tiles_done, tiles_total)``.

    Returns:
        Output tensor on CPU, shape (1, C, H*scale, W*scale), values in [0, 1].
    """
    _, c, h, w = img.shape
    tile = min(tile, h, w)
    # Step by tile-minus-overlap, but never less than a quarter-tile — so an
    # OOM-shrunk tile smaller than the overlap can't collapse the stride to a
    # sliver and explode the tile count.
    stride = max(tile // 4, tile - _TILE_OVERLAP)
    h_starts = _tile_starts(h, tile, stride)
    w_starts = _tile_starts(w, tile, stride)
    # Real per-tile overlaps (input px) so the blend tapers over the actual
    # shared region, not a fixed width — otherwise heavily-overlapping tiles
    # (e.g. an image only a little taller than the tile) average together and
    # blur the whole output.
    h_over = _axis_overlaps(h_starts, tile)
    w_over = _axis_overlaps(w_starts, tile)
    total = len(h_starts) * len(w_starts)

    # Blend on the GPU when the full-resolution FP32 accumulators (out: C ch +
    # weight: 1 ch) fit in a safe slice of free VRAM — that keeps every tile on
    # the device and copies the finished image to the host just once.  A large
    # upscale won't fit, so fall back to a host accumulator (per-tile transfer).
    accum_bytes = (c + 1) * (h * scale) * (w * scale) * 4
    accum_device = 'cpu'
    if device.startswith('cuda'):
        try:
            free, _total_vram = torch.cuda.mem_get_info()
            if accum_bytes < free * 0.5:
                accum_device = device
            logger.debug(
                'blend accumulator: need %.0f MB, free VRAM %.0f MB -> %s',
                accum_bytes / 1e6,
                free / 1e6,
                accum_device,
            )
        except Exception:
            accum_device = 'cpu'
    logger.debug(
        'tile grid: %d x %d = %d tiles, stride=%d, tile=%d, overlap=%d',
        len(w_starts),
        len(h_starts),
        total,
        stride,
        tile,
        _TILE_OVERLAP,
    )

    # This is a long, GPU-pinning, otherwise-silent loop (hundreds of tiles for a
    # full-size photo through a heavy model), so announce the scale up front and
    # emit a throttled heartbeat — a run must never look like a hang.  Logging
    # each pass also makes an OOM shrink-retry visible (it re-enters here with a
    # smaller tile and a larger tile count).
    started = time.monotonic()
    logger.info(
        'Tiled enhancement: %d tiles (%dx%d input, tile=%d, overlap=%d, scale=%d, blend=%s)',
        total,
        w,
        h,
        tile,
        _TILE_OVERLAP,
        scale,
        accum_device,
    )
    last_log = started

    try:
        out = torch.zeros(1, c, h * scale, w * scale, device=accum_device)
        weight = torch.zeros(1, 1, h * scale, w * scale, device=accum_device)
    except (MemoryError, RuntimeError) as e:
        # The GPU accumulator didn't fit after all — retry the blend on the host
        # rather than fail the whole run.
        if accum_device == 'cpu' or not is_oom_error(e):
            raise
        logger.warning('GPU blend buffer did not fit; blending on the host instead')
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        accum_device = 'cpu'
        out = torch.zeros(1, c, h * scale, w * scale)
        weight = torch.zeros(1, 1, h * scale, w * scale)

    done = 0
    debug_tiles = logger.isEnabledFor(logging.DEBUG)
    for hi, hs in enumerate(h_starts):
        for wi, ws in enumerate(w_starts):
            if stop_event is not None and stop_event.is_set():
                raise EnhanceAborted('enhancement aborted')
            tile_started = time.monotonic() if debug_tiles else 0.0
            patch = img[..., hs : hs + tile, ws : ws + tile]
            result = run_tile(patch)
            oh, ow = hs * scale, ws * scale
            th, tw = result.shape[-2], result.shape[-1]
            feather = _BLEND_BAND * scale
            wy = _axis_weight(th, h_over[hi][0] * scale, h_over[hi][1] * scale, feather)
            wx = _axis_weight(tw, w_over[wi][0] * scale, w_over[wi][1] * scale, feather)
            mask = torch.outer(wy, wx).view(1, 1, th, tw).to(accum_device)
            # Match the accumulator (FP32): the tile may be FP16 and/or on the GPU.
            tile_out = result.to(device=accum_device, dtype=torch.float32)
            out[..., oh : oh + th, ow : ow + tw].add_(tile_out * mask)
            weight[..., oh : oh + th, ow : ow + tw].add_(mask)
            done += 1
            if debug_tiles:
                logger.debug(
                    '  tile %d/%d [y %d:%d, x %d:%d] -> %dx%d in %.2fs',
                    done,
                    total,
                    hs,
                    hs + tile,
                    ws,
                    ws + tile,
                    th,
                    tw,
                    time.monotonic() - tile_started,
                )
            if on_progress is not None:
                on_progress(done, total)
            # Heartbeat at most every ~15 s so the log shows steady progress and
            # a rough rate without spamming a line per tile.
            now = time.monotonic()
            if now - last_log >= 15.0:
                rate = done / (now - started)
                eta = (total - done) / rate if rate > 0 else 0.0
                logger.info(
                    '  enhancement progress: %d/%d tiles (%.0f%%), ~%.0fs remaining',
                    done,
                    total,
                    100.0 * done / total,
                    eta,
                )
                last_log = now
    weight.clamp_(min=1e-6)
    logger.info('Tiled enhancement: %d/%d tiles done in %.0fs', done, total, time.monotonic() - started)
    # Single host transfer of the finished image (a no-op if we blended on CPU).
    return (out / weight).cpu()


class EnhanceAborted(Exception):
    """Raised when an enhancement run is stopped (e.g. graceful shutdown)."""


def enhance_preview(
    cap: Capability,
    src_path: str | Path,
    *,
    arbiter: Any,
    device: str,
    data_dir: str,
    crop_size: int = 256,
    crop_left: int | None = None,
    crop_top: int | None = None,
) -> bytes:
    """Enhance a crop of *src_path* for a fast before/after comparison.

    Runs a single small tile through the arbiter at INTERACTIVE priority (so it
    slips ahead of bulk work), returning the *enhanced* crop as PNG bytes.  The
    dialog renders the "before" by panning the original itself, so there's no
    need to ship it.  Used to preview a capability before committing to the
    full-resolution run.

    The crop is centred by default.  The dialog lets the user drag to reposition
    it (a client-side pan over the original), passing the chosen top-left so the
    preview reflects the region they actually care about.

    Args:
        cap: Capability to apply.
        src_path: Source image.
        arbiter: Shared compute arbiter.
        device: Device to run on.
        data_dir: Data directory (for locating weights).
        crop_size: Side length of the crop sampled from the source.
        crop_left: Left edge of the crop in source pixels (centred if None).
        crop_top: Top edge of the crop in source pixels (centred if None).

    Returns:
        The enhanced crop as PNG bytes (the after crop is *cap.scale*× larger).
    """
    pil = open_image(src_path).convert('RGB')
    w, h = pil.size
    side = min(crop_size, w, h)
    # Default to a centred crop; otherwise honour the requested top-left, clamped
    # so the crop always lies fully inside the image.
    if crop_left is None or crop_top is None:
        left, top = (w - side) // 2, (h - side) // 2
    else:
        left = max(0, min(w - side, int(crop_left)))
        top = max(0, min(h - side, int(crop_top)))
    crop = pil.crop((left, top, left + side, top + side))
    logger.debug(
        'preview %r: source %dx%d, crop %dx%d at (%d,%d), device=%s',
        cap.key,
        w,
        h,
        side,
        side,
        left,
        top,
        device,
    )

    arr = np.asarray(crop, dtype=np.float32) / 255.0
    img = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).contiguous()

    def _infer() -> torch.Tensor:
        model = _CACHE.get(cap, device, data_dir)
        with torch.no_grad():
            out = _forward_model(cap, model, img.to(device))
        return out.detach().float().clamp_(0, 1).cpu()

    result = arbiter.run_exclusive(_infer, _INTERACTIVE)
    if not _is_plausible(img, result):
        raise EnhanceUnstable('This image could not be enhanced \u2014 the model produced an unstable result.')
    out_arr = (result.squeeze(0).permute(1, 2, 0).numpy() * 255.0).round().astype(np.uint8)
    return _to_png_bytes(Image.fromarray(out_arr))


def _to_png_bytes(image: Image.Image) -> bytes:
    """Encode a PIL image as PNG bytes."""
    buf = io.BytesIO()
    image.save(buf, format='PNG')
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def enhance_image_to_file(
    cap: Capability,
    src_path: str | Path,
    dst_path: str | Path,
    *,
    arbiter: Any,
    device: str,
    data_dir: str,
    tile_size: int = 0,
    output_format: str = 'png',
    strength: float = 1.0,
    stop_event: Any | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Enhance *src_path* with *cap* and write the result to *dst_path*.

    Inference is submitted to the compute arbiter one tile at a time, so it
    serialises against indexing/search and yields between tiles.  On an
    out-of-memory error the whole image is retried at a smaller tile size.

    Args:
        cap: The capability (model) to apply.
        src_path: Source image (any supported format, including RAW).
        dst_path: Destination file to write (lossless).
        arbiter: The shared :class:`arbiter.ComputeArbiter`.
        device: Device the model runs on ('cuda', 'mps', 'cpu').
        data_dir: Data directory (for locating the weight file).
        tile_size: Tile size in pixels, or 0 to choose automatically.
        output_format: 'png' or 'tiff' (both lossless).
        strength: Blend of the enhanced result with the original, 0.0–1.0
            (1.0 = fully enhanced).  Lets a restoration be dialled back to keep
            natural texture; offered for denoise/deblur (scale 1) in the UI.
        stop_event: Optional stop event checked between tiles.
        on_progress: Optional ``(tiles_done, tiles_total)`` callback.

    Returns:
        Dict with the output ``width``, ``height`` and ``format``.

    Raises:
        EnhanceAborted: If *stop_event* fires mid-run.
        RuntimeError: If the model cannot be loaded or inference fails.
    """
    # Load and normalise the source to a (1, 3, H, W) float tensor in [0, 1].
    pil = open_image(src_path).convert('RGB')
    logger.info('Enhancing %s with %r (%dx%d) on %s', Path(src_path).name, cap.key, pil.width, pil.height, device)
    precision = 'FP16 autocast' if (device.startswith('cuda') and cap.arch in _FP16_SAFE_ARCHS) else 'FP32'
    logger.debug(
        'enhance params: arch=%s, scale=%d, tile_size=%s, strength=%.2f, format=%s, precision=%s',
        cap.arch,
        cap.scale,
        tile_size or 'auto',
        strength,
        output_format,
        precision,
    )
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    img = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0).contiguous()

    def run_tile(patch: torch.Tensor) -> torch.Tensor:
        """Run one tile through the arbiter on the owner thread."""

        def _infer() -> torch.Tensor:
            model = _CACHE.get(cap, device, data_dir)
            with torch.no_grad():
                out = _forward_model(cap, model, patch.to(device))
            # Leave the tile on the device: _process_tiles blends it there (single
            # host transfer at the end).  Clamp now; the blend casts to FP32.
            return out.detach().clamp_(0, 1)

        return arbiter.run_exclusive(_infer)

    # Refuse outputs too large to blend in host RAM (the accumulators are
    # full-resolution float32).  Tile shrink-retry can't rescue this — it only
    # bounds GPU tile memory — so fail cleanly up front rather than risk an OS
    # out-of-memory kill.
    out_pixels = img.shape[-2] * img.shape[-1] * cap.scale * cap.scale
    if out_pixels > _MAX_OUTPUT_PIXELS:
        out_mp = out_pixels / 1_000_000
        raise EnhanceTooLarge(
            f'This image is too large to enhance at this scale (would produce '
            f'{out_mp:.0f} megapixels).' + (' Try the 2× option instead.' if cap.scale > 2 else '')
        )

    tile = tile_size if tile_size and tile_size >= _MIN_TILE else _auto_tile_size(device)
    while True:
        try:
            result = _process_tiles(
                img,
                run_tile,
                tile=tile,
                scale=cap.scale,
                device=device,
                stop_event=stop_event,
                on_progress=on_progress,
            )
            break
        except (MemoryError, RuntimeError) as e:
            if not is_oom_error(e) or tile <= _MIN_TILE:
                raise
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            tile = max(_MIN_TILE, tile // 2)
            logger.warning('Enhancement hit out-of-memory; retrying with tile size %d', tile)

    # Reject divergent (uncorrelated colour-noise) output rather than write garbage.
    if not _is_plausible(img, result):
        raise EnhanceUnstable('This image could not be enhanced \u2014 the model produced an unstable result.')

    # Optional strength blend with the original (a post-process, model-agnostic).
    if strength < 1.0:
        base = img
        if cap.scale != 1:
            base = torch.nn.functional.interpolate(
                img, size=result.shape[-2:], mode='bicubic', align_corners=False
            ).clamp_(0, 1)
        result = (strength * result + (1.0 - strength) * base).clamp_(0, 1)

    out_arr = (result.squeeze(0).permute(1, 2, 0).numpy() * 255.0).round().astype(np.uint8)
    out_img = Image.fromarray(out_arr)
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    fmt = 'TIFF' if output_format.lower() == 'tiff' else 'PNG'
    out_img.save(dst_path, format=fmt)
    logger.info(
        'Enhanced %s → %s (%dx%d, %s)',
        Path(src_path).name,
        Path(dst_path).name,
        out_img.width,
        out_img.height,
        fmt,
    )
    return {'width': out_img.width, 'height': out_img.height, 'format': fmt}
