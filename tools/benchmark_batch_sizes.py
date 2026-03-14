#!/usr/bin/env python3
"""Find optimal GPU batch sizes for Photonarium's CUDA pipeline stages.

Loads the user's real config to pick the correct models, then benchmarks
each pipeline stage at increasing batch sizes to find the fastest setting
before OOM or throughput degradation.

Algorithm per stage:
  1.  Load the model (once).
  2.  Run a warmup batch at size 1.
  3.  Binary-search for the maximum viable batch size (no OOM).
  4.  Within the viable range, sweep batch sizes and time them.
  5.  Report the batch size with the highest throughput (images/sec).

Usage:
    python tools/benchmark_batch_sizes.py                     # Use default config
    python tools/benchmark_batch_sizes.py --config /path/to/photonarium.yml
    python tools/benchmark_batch_sizes.py --images /path/to/photos  # Custom images
    python tools/benchmark_batch_sizes.py --stage embeddings  # Single stage only
    python tools/benchmark_batch_sizes.py --max 64            # Search up to 64
"""

from __future__ import annotations

import argparse
import datetime
import gc
import logging
import os
import statistics
import sys
import time
from pathlib import Path

# Allow imports from app/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))

os.environ['HF_HUB_OFFLINE'] = '1'

import torch
from PIL import Image

from config import Config, get_default_config_path, load_config

logger = logging.getLogger('benchmark')


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now() -> str:
    """Return the current local time as a short HH:MM:SS string."""
    return datetime.datetime.now().strftime('%H:%M:%S')


def _elapsed(start: float) -> str:
    """Return a human-readable elapsed time string."""
    secs = time.perf_counter() - start
    if secs < 60:
        return f'{secs:.1f}s'
    mins = int(secs) // 60
    remaining = secs - mins * 60
    return f'{mins}m {remaining:.0f}s'


def detect_device() -> str:
    """Detect the best available torch device."""
    if torch.cuda.is_available():
        return 'cuda'
    if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
        return 'mps'
    return 'cpu'


def gpu_mem_mb() -> str:
    """Return a short string describing current GPU memory usage."""
    if torch.cuda.is_available():
        alloc = torch.cuda.memory_allocated() / 1024 / 1024
        resrv = torch.cuda.memory_reserved() / 1024 / 1024
        return f'{alloc:.0f} MB allocated, {resrv:.0f} MB reserved'
    return 'N/A'


def flush_gpu():
    """Release cached GPU memory."""
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()


def collect_images(image_dir: Path, limit: int = 200) -> list[Path]:
    """Collect image paths from a directory (non-recursive for speed)."""
    exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif'}
    images = []
    for p in sorted(image_dir.iterdir()):
        if p.suffix.lower() in exts and p.is_file():
            images.append(p)
            if len(images) >= limit:
                break
    if not images:
        # Try recursive
        for p in sorted(image_dir.rglob('*')):
            if p.suffix.lower() in exts and p.is_file():
                images.append(p)
                if len(images) >= limit:
                    break
    return images


def print_header(text: str):
    """Print a section header."""
    width = 60
    print(f'\n{"=" * width}')
    print(f'  {text}')
    print(f'{"=" * width}')


def print_result_table(results: list[dict]):
    """Print a table of batch-size timing results."""
    if not results:
        print('  No results.')
        return
    print(f'  {"Batch":>5}  {"Img/sec":>8}  {"Batch ms":>9}  {"GPU mem":>16}  {"Status"}')
    print(f'  {"─" * 5}  {"─" * 8}  {"─" * 9}  {"─" * 16}  {"─" * 8}')
    for r in results:
        status = r.get('status', 'ok')
        marker = ' ◄ best' if r.get('best') else ''
        if status == 'oom':
            print(f'  {r["batch"]:>5}  {"OOM":>8}  {"":>9}  {r.get("gpu_mem", ""):>16}  ✗ OOM')
        else:
            ips = f'{r["ips"]:.1f}'
            ms = f'{r["ms"]:.0f}'
            print(f'  {r["batch"]:>5}  {ips:>8}  {ms:>9}  {r.get("gpu_mem", ""):>16}  {status}{marker}')


# ── Stage benchmarks ─────────────────────────────────────────────────────────


def _time_batch(run_fn, batch_size: int, images: list, trials: int = 3) -> dict | None:
    """Time a batch operation, returning stats or None on OOM.

    Args:
        run_fn: Callable(batch_of_images) that runs the GPU workload.
        batch_size: Number of images in this batch.
        images: Full list of available images (will be sliced).
        trials: Number of timed runs (after warmup).

    Returns:
        Dict with timing stats, or None on OOM.
    """
    batch = images[:batch_size]

    # Warmup (untimed)
    flush_gpu()
    try:
        run_fn(batch)
    except (MemoryError, RuntimeError) as e:
        if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
            flush_gpu()
            return None
        raise

    # Timed trials
    times = []
    for _ in range(trials):
        flush_gpu()
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        try:
            run_fn(batch)
        except (MemoryError, RuntimeError) as e:
            if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
                flush_gpu()
                return None
            raise
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        times.append(time.perf_counter() - t0)

    median = statistics.median(times)
    return {
        'batch': batch_size,
        'seconds': median,
        'ms': median * 1000,
        'ips': batch_size / median if median > 0 else 0,
        'gpu_mem': gpu_mem_mb(),
        'status': 'ok',
    }


def _find_max_viable(run_fn, images: list, max_batch: int) -> int:
    """Binary search for the largest batch size that doesn't OOM."""
    lo, hi = 1, min(max_batch, len(images))
    best = 1

    while lo <= hi:
        mid = (lo + hi) // 2
        flush_gpu()
        result = _time_batch(run_fn, mid, images, trials=1)
        if result is not None:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return best


def _sweep_batch_sizes(run_fn, images: list, max_viable: int, trials: int = 3) -> list[dict]:
    """Sweep batch sizes from 1 to max_viable and time each.

    Uses a geometric progression to keep the sweep short: powers of 2
    plus max_viable and a few points near the expected optimum.
    """
    # Build candidate set: powers of 2 up to max_viable
    candidates = set()
    b = 1
    while b <= max_viable:
        candidates.add(b)
        b *= 2
    # Also include max_viable and a few nearby values
    candidates.add(max_viable)
    if max_viable > 2:
        candidates.add(max(1, max_viable - max_viable // 4))
        candidates.add(max(1, max_viable * 3 // 4))
    # Cap at available images
    candidates = sorted(c for c in candidates if c <= len(images))

    results = []
    for bs in candidates:
        r = _time_batch(run_fn, bs, images, trials=trials)
        if r is None:
            results.append({'batch': bs, 'status': 'oom', 'gpu_mem': gpu_mem_mb()})
            break  # No point going higher
        results.append(r)
        print(f'    batch={bs:>3}  {r["ips"]:.1f} img/s  {r["ms"]:.0f} ms  [{r["gpu_mem"]}]')

    # Mark best
    ok_results = [r for r in results if r['status'] == 'ok']
    if ok_results:
        best = max(ok_results, key=lambda r: r['ips'])
        best['best'] = True

    return results


def benchmark_embeddings(config: Config, images: list[Path], max_batch: int, trials: int = 3) -> dict | None:
    """Benchmark OpenCLIP image embedding batch sizes."""
    print_header(f'OpenCLIP Embeddings ({config.openclip_model} / {config.openclip_pretrained})')
    stage_start = time.perf_counter()
    print(f'  Started: {_now()}')

    from imagedb import OpenCLIPModel

    device = detect_device()
    print(f'  Device: {device}')
    print('  Loading model...', end=' ', flush=True)

    clip = OpenCLIPModel(
        model_name=config.openclip_model,
        pretrained=config.openclip_pretrained,
        max_dimension=config.max_image_dimension,
    )
    # Force model load
    clip._load_model()
    if clip._load_failed:
        print('FAILED (model load failed — check download_models.py)')
        return None
    print(f'done  [{gpu_mem_mb()}]')

    # Preload images as PIL once (disk I/O is not what we're measuring)
    print(f'  Preloading {len(images)} images...', end=' ', flush=True)
    pil_images = []
    for p in images:
        try:
            img = Image.open(p).convert('RGB')
            pil_images.append((p, img))
        except Exception:
            pass
    print(f'{len(pil_images)} loaded')

    if len(pil_images) < 2:
        print('  Not enough images to benchmark.')
        return None

    # Preprocess all images once
    preprocessed = []
    for _path, img in pil_images:
        try:
            preprocessed.append(clip.preprocess(img))
        except Exception:
            pass

    if not preprocessed:
        print('  Preprocessing failed for all images.')
        return None

    def run_batch(batch_tensors):
        stack = torch.stack(batch_tensors).to(clip.device)  # noqa: F821
        with torch.inference_mode():
            if clip.device == 'cuda':  # noqa: F821
                with torch.amp.autocast('cuda'):
                    emb = clip.model.encode_image(stack)  # noqa: F821
            else:
                emb = clip.model.encode_image(stack)  # noqa: F821
            emb = emb / emb.norm(dim=-1, keepdim=True)
            emb.cpu()
        del stack

    print(f'  Finding maximum viable batch size (up to {max_batch})...')
    max_viable = _find_max_viable(run_batch, preprocessed, max_batch)
    print(f'  Max viable: {max_viable}')

    print('  Sweeping batch sizes...')
    results = _sweep_batch_sizes(run_batch, preprocessed, max_viable, trials=trials)
    print_result_table(results)

    print(f'  Finished: {_now()} (took {_elapsed(stage_start)})')

    # Clean up
    del clip, preprocessed, pil_images
    flush_gpu()

    ok_results = [r for r in results if r.get('best')]
    return ok_results[0] if ok_results else None


def benchmark_nima(config: Config, images: list[Path], max_batch: int, data_dir: Path, trials: int = 3) -> dict | None:
    """Benchmark NIMA aesthetic scoring batch sizes."""
    print_header('NIMA Aesthetic Scoring (MobileNetV2-AVA)')
    stage_start = time.perf_counter()
    print(f'  Started: {_now()}')

    checkpoint = data_dir / '.nima-mobilenetv2-ava.pth'
    if not checkpoint.exists():
        print(f'  Skipped — checkpoint not found at {checkpoint}')
        print('  Run download_models.py first.')
        return None

    device = detect_device()
    print(f'  Device: {device}')
    print('  Loading model...', end=' ', flush=True)

    from nima import NIMA_TRANSFORM, load_nima_model

    try:
        model = load_nima_model(str(checkpoint), device=device)
    except Exception as e:
        print(f'FAILED ({e})')
        return None
    print(f'done  [{gpu_mem_mb()}]')

    # Preprocess images as tensors (removing I/O from timing)
    print(f'  Preprocessing {len(images)} images...', end=' ', flush=True)
    tensors = []
    for p in images:
        try:
            img = Image.open(p).convert('RGB')
            tensors.append(NIMA_TRANSFORM(img))
        except Exception:
            pass
    print(f'{len(tensors)} ready')

    if len(tensors) < 2:
        print('  Not enough images to benchmark.')
        return None

    ratings = torch.arange(1, 11, dtype=torch.float32, device=device)

    def run_batch(batch_tensors):
        stack = torch.stack(batch_tensors).to(device)
        with torch.no_grad():
            probs = model(stack)  # noqa: F821
        scores = (probs * ratings).sum(dim=1)
        scores.cpu()
        del stack

    print(f'  Finding maximum viable batch size (up to {max_batch})...')
    max_viable = _find_max_viable(run_batch, tensors, max_batch)
    print(f'  Max viable: {max_viable}')

    print('  Sweeping batch sizes...')
    results = _sweep_batch_sizes(run_batch, tensors, max_viable, trials=trials)
    print_result_table(results)
    print(f'  Finished: {_now()} (took {_elapsed(stage_start)})')

    del model, tensors
    flush_gpu()

    ok_results = [r for r in results if r.get('best')]
    return ok_results[0] if ok_results else None


def benchmark_faces(config: Config, images: list[Path], max_batch: int, trials: int = 3) -> dict | None:
    """Benchmark face detection batch sizes.

    Face detection is more complex than the other stages: MTCNN groups
    images by dimension for batching, then InceptionResnetV1 computes
    embeddings for all detected faces.  ``detect_faces_from_preloaded``
    closes PIL images after processing, so we must re-preload for each
    trial.  To keep I/O out of the timing we cache the raw JPEG bytes
    and reconstruct PIL images from memory.
    """
    print_header('Face Detection (MTCNN + InceptionResnetV1)')
    stage_start = time.perf_counter()
    print(f'  Started: {_now()}')

    import io

    from faces import FaceDetector

    device = detect_device()
    print(f'  Device: {device}')
    print('  Loading models...', end=' ', flush=True)

    detector = FaceDetector(
        min_confidence=config.face_detection_min_confidence,
        min_face_size=config.face_detection_min_size,
    )
    # Force-load both models
    _ = detector.mtcnn
    if detector._mtcnn_failed:
        print('FAILED (MTCNN load failed)')
        return None
    _ = detector.resnet
    if detector._resnet_failed:
        print('FAILED (ResNet load failed)')
        return None
    print(f'done  [{gpu_mem_mb()}]')

    # Cache raw bytes + precomputed scales so we can reconstruct
    # PIL images cheaply without disk I/O on every trial.
    print(f'  Preloading {len(images)} images into RAM...', end=' ', flush=True)
    cached: list[tuple[Path, bytes, float]] = []
    max_dim = detector.max_dimension if hasattr(detector, 'max_dimension') else 4096
    for p in images:
        try:
            img = Image.open(p).convert('RGB')
            w, h = img.size
            scale = 1.0
            if max(w, h) > max_dim:
                scale = max_dim / max(w, h)
                new_w, new_h = int(w * scale), int(h * scale)
                img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=95)
            cached.append((p, buf.getvalue(), scale))
            img.close()
        except Exception:
            pass
    print(f'{len(cached)} cached')

    if len(cached) < 2:
        print('  Not enough images to benchmark.')
        return None

    def rebuild_loaded(batch_cached):
        """Reconstruct (path, PIL.Image, scale) tuples from cached bytes."""
        result = []
        for path, raw, scale in batch_cached:
            img = Image.open(io.BytesIO(raw)).convert('RGB')
            result.append((path, img, scale))
        return result

    def time_face_batch(batch_cached, n_trials):
        """Time face detection with fresh PIL images each trial."""
        batch_size = len(batch_cached)

        # Warmup
        flush_gpu()
        try:
            loaded = rebuild_loaded(batch_cached)
            detector.detect_faces_from_preloaded(loaded)  # noqa: F821
        except (MemoryError, RuntimeError) as e:
            if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
                flush_gpu()
                return None
            raise

        # Timed trials
        times = []
        for _ in range(n_trials):
            flush_gpu()
            loaded = rebuild_loaded(batch_cached)
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            try:
                detector.detect_faces_from_preloaded(loaded)  # noqa: F821
            except (MemoryError, RuntimeError) as e:
                if isinstance(e, MemoryError) or 'out of memory' in str(e).lower():
                    flush_gpu()
                    return None
                raise
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            times.append(time.perf_counter() - t0)

        median = statistics.median(times)
        return {
            'batch': batch_size,
            'seconds': median,
            'ms': median * 1000,
            'ips': batch_size / median if median > 0 else 0,
            'gpu_mem': gpu_mem_mb(),
            'status': 'ok',
        }

    # Binary search for max viable
    print(f'  Finding maximum viable batch size (up to {max_batch})...')
    lo, hi = 1, min(max_batch, len(cached))
    max_viable = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        flush_gpu()
        r = time_face_batch(cached[:mid], 1)
        if r is not None:
            max_viable = mid
            lo = mid + 1
        else:
            hi = mid - 1
    print(f'  Max viable: {max_viable}')

    # Sweep
    print('  Sweeping batch sizes...')
    candidates = set()
    b = 1
    while b <= max_viable:
        candidates.add(b)
        b *= 2
    candidates.add(max_viable)
    if max_viable > 2:
        candidates.add(max(1, max_viable * 3 // 4))
    candidates = sorted(c for c in candidates if c <= len(cached))

    results = []
    for bs in candidates:
        r = time_face_batch(cached[:bs], trials)
        if r is None:
            results.append({'batch': bs, 'status': 'oom', 'gpu_mem': gpu_mem_mb()})
            break
        results.append(r)
        print(f'    batch={bs:>3}  {r["ips"]:.1f} img/s  {r["ms"]:.0f} ms  [{r["gpu_mem"]}]')

    ok_results = [r for r in results if r['status'] == 'ok']
    if ok_results:
        best = max(ok_results, key=lambda r: r['ips'])
        best['best'] = True

    print_result_table(results)
    print(f'  Finished: {_now()} (took {_elapsed(stage_start)})')

    del detector, cached
    flush_gpu()

    best_results = [r for r in results if r.get('best')]
    return best_results[0] if best_results else None


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description='Find optimal GPU batch sizes for Photonarium pipeline stages.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        '--config',
        '-c',
        type=Path,
        default=None,
        help='Path to photonarium.yml (default: OS default location)',
    )
    parser.add_argument(
        '--images',
        type=Path,
        default=None,
        help='Directory of test images (default: tools/mktutorial/examples/)',
    )
    parser.add_argument(
        '--stage',
        '-s',
        choices=['embeddings', 'nima', 'faces', 'all'],
        default='all',
        help='Which stage to benchmark (default: all)',
    )
    parser.add_argument(
        '--max',
        type=int,
        default=128,
        help='Maximum batch size to search up to (default: 128)',
    )
    parser.add_argument(
        '--trials',
        type=int,
        default=3,
        help='Number of timed trials per batch size (default: 3)',
    )
    args = parser.parse_args()
    overall_start = time.perf_counter()
    print(f'Benchmark started at {_now()}')

    # Set up logging
    logging.basicConfig(
        level=logging.WARNING,
        format='%(levelname)s: %(message)s',
    )

    # Load config
    config_path = args.config
    if config_path is None:
        config_path = get_default_config_path()
    print(f'Config: {config_path}')
    config = load_config(config_path)

    # Resolve data_dir (for NIMA checkpoint)
    data_dir = Path(config.data_dir).expanduser().resolve()
    print(f'Data dir: {data_dir}')

    # Device info
    device = detect_device()
    if device == 'cpu':
        print('\nWARNING: No GPU detected.  Batch-size tuning only makes sense')
        print('with a CUDA or MPS device.  Results on CPU will not be meaningful.')
        print('Continuing anyway...\n')
    elif device == 'cuda':
        name = torch.cuda.get_device_name(0)
        mem = torch.cuda.get_device_properties(0).total_memory / 1024 / 1024
        print(f'GPU: {name} ({mem:.0f} MB)')

    # Collect test images
    if args.images:
        image_dir = args.images
    else:
        image_dir = Path(__file__).resolve().parent / 'mktutorial' / 'examples'
    print(f'Image source: {image_dir}')

    images = collect_images(image_dir)
    if not images:
        print(f'ERROR: No images found in {image_dir}')
        sys.exit(1)
    print(f'Found {len(images)} images')

    # Print current config values
    print('\nCurrent config values:')
    print(f'  embedding_batch_size:       {config.embedding_batch_size}')
    print(f'  nima_batch_size:            {config.nima_batch_size}')
    print(f'  face_detection_batch_size:  {config.face_detection_batch_size}')
    print(f'  openclip_model:             {config.openclip_model}')
    print(f'  openclip_pretrained:        {config.openclip_pretrained}')
    print(f'  nima_enabled:               {config.nima_enabled}')
    print(f'  face_detection_enabled:     {config.face_detection_enabled}')

    max_batch = args.max
    recommendations = {}

    # ── Run benchmarks ───────────────────────────────────────────────────
    if args.stage in ('all', 'embeddings'):
        result = benchmark_embeddings(config, images, max_batch, args.trials)
        if result:
            recommendations['embedding_batch_size'] = result['batch']

    if args.stage in ('all', 'nima'):
        if config.nima_enabled:
            result = benchmark_nima(config, images, max_batch, data_dir, args.trials)
            if result:
                recommendations['nima_batch_size'] = result['batch']
        else:
            print_header('NIMA Aesthetic Scoring')
            print('  Skipped — nima_enabled is false in config')

    if args.stage in ('all', 'faces'):
        if config.face_detection_enabled:
            result = benchmark_faces(config, images, max_batch, args.trials)
            if result:
                recommendations['face_detection_batch_size'] = result['batch']
        else:
            print_header('Face Detection')
            print('  Skipped — face_detection_enabled is false in config')

    # ── Summary ──────────────────────────────────────────────────────────
    print_header('RECOMMENDATIONS')

    if not recommendations:
        print('  No recommendations — benchmarks did not complete.')
        return

    any_changed = False
    for key, value in recommendations.items():
        current = getattr(config, key)
        marker = '' if value == current else f'  (currently {current})'
        if value != current:
            any_changed = True
        print(f'  {key}: {value}{marker}')

    if any_changed:
        print(f'\nTo apply, edit {config_path} and set the values above.')
    else:
        print('\n  Your current settings are already optimal!')

    print(f'\nBenchmark finished at {_now()} (total: {_elapsed(overall_start)})')


if __name__ == '__main__':
    main()
