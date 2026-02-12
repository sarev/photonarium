"""
Trash directory and quality scoring utilities for Photonarium.

This module provides pure utility functions for the trash-based deletion
workflow and the composite quality scoring algorithm used by duplicate
pruning. It has no dependency on ImageDatabase, no threading, and no
direct database access — callers pass in paths and data as arguments.

The quality scoring algorithm is a Python port of the frontend
``_computeQualityScores()`` in ``static/appstate/images.js``, ensuring
that the backend prune endpoint ranks images identically to the frontend
Quality sort.

Functions:
    validate_trash_dir  — Check trash dir doesn't overlap indexed folders
    resolve_trash_path  — Collision-safe destination path inside trash dir
    move_to_trash       — Move a single file into the trash directory
    compute_quality_scores — Weighted-percentile quality ranking for images
"""

from __future__ import annotations

import logging
import math
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


# =============================================================================
# TRASH DIRECTORY FUNCTIONS
# =============================================================================

def validate_trash_dir(trash_dir: Path, indexed_folders: list[str]) -> None:
    """Check that the trash directory does not overlap any indexed folder.

    Performs a bidirectional containment check: the trash directory must not
    be inside any indexed folder, and no indexed folder may be inside the
    trash directory.  Uses ``Path.resolve()`` for robust comparison across
    symlinks and relative segments.

    Args:
        trash_dir: Resolved path to the trash directory.
        indexed_folders: List of registered folder path strings.

    Raises:
        ValueError: If the trash directory overlaps an indexed folder,
            with a message describing the conflict.
    """
    trash_resolved = trash_dir.resolve()

    for folder_str in indexed_folders:
        folder_resolved = Path(folder_str).resolve()

        # Check: trash inside folder?
        try:
            trash_resolved.relative_to(folder_resolved)
            raise ValueError(
                f'Trash directory {trash_dir} is inside indexed folder '
                f'{folder_str}. Trashed images would be re-indexed.'
            )
        except ValueError as e:
            # Re-raise our own ValueError, ignore the relative_to failure
            if 'inside indexed folder' in str(e):
                raise

        # Check: folder inside trash?
        try:
            folder_resolved.relative_to(trash_resolved)
            raise ValueError(
                f'Indexed folder {folder_str} is inside trash directory '
                f'{trash_dir}. Indexed images could be trashed by accident.'
            )
        except ValueError as e:
            if 'inside trash directory' in str(e):
                raise


def resolve_trash_path(basename: str, trash_dir: Path) -> Path:
    """Compute a collision-safe destination path inside the trash directory.

    If ``trash_dir/basename`` does not exist, returns it directly.
    Otherwise appends a counter suffix: ``stem (2).suffix``,
    ``stem (3).suffix``, etc., up to a defensive limit of 10 000.

    Args:
        basename: Original filename (e.g. ``beach.jpg``).
        trash_dir: Path to the trash directory (must already exist).

    Returns:
        Destination path that does not currently exist.

    Raises:
        RuntimeError: If the counter exceeds 10 000 (defensive limit).
    """
    dest = trash_dir / basename
    if not dest.exists():
        return dest

    stem = Path(basename).stem
    suffix = Path(basename).suffix

    for counter in range(2, 10_001):
        dest = trash_dir / f'{stem} ({counter}){suffix}'
        if not dest.exists():
            return dest

    raise RuntimeError(
        f'Could not resolve unique trash path for {basename} '
        f'after 10000 attempts'
    )


def move_to_trash(src: Path, trash_dir: Path) -> Path | None:
    """Move a single file into the trash directory.

    Resolves the destination via :func:`resolve_trash_path` and uses
    ``shutil.move`` so it works even when src and trash_dir are on
    different filesystems.

    Args:
        src: Source file path to move.
        trash_dir: Destination trash directory (must already exist).

    Returns:
        The destination path on success, or ``None`` if the source file
        was not found (caller should handle DB cleanup for missing files).
    """
    if not src.exists():
        logger.warning(f'move_to_trash: Source file not found: {src}')
        return None

    dest = resolve_trash_path(src.name, trash_dir)
    shutil.move(str(src), str(dest))
    logger.info(f'Trashed: {src.name} -> {dest}')
    return dest


# =============================================================================
# QUALITY SCORING (Python port of frontend _computeQualityScores)
# =============================================================================

def _percentile_ranks(values: list[float]) -> list[float]:
    """Convert raw values to percentile ranks in [0..1] with average-rank
    for ties.

    This is a faithful port of the frontend ``_percentileRanks()`` in
    ``static/appstate/images.js`` (lines 217-238).

    Args:
        values: Raw numeric values to rank.

    Returns:
        List of percentile ranks, same length as input. Each rank is in
        the range [0, 1].  A single-element list returns ``[0.5]``.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [0.5]

    # Create (value, original_index) pairs, sort ascending
    indexed = sorted(enumerate(values), key=lambda pair: pair[1])

    # Assign average ranks for ties, normalised to [0..1]
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j - 1) / 2  # 0-based average rank
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank / (n - 1)  # Normalise to [0..1]
        i = j

    return ranks


def compute_quality_scores(
    images: list[dict],
    config,
) -> dict[str, float]:
    """Compute composite quality scores for a set of images.

    This is a Python port of the frontend ``_computeQualityScores()``
    function in ``static/appstate/images.js``. It uses weighted percentile
    ranking across four dimensions:

    - **Aesthetic**: Blended NIMA + LAION percentile ranks (configurable alpha)
    - **Sharpness**: ``log1p(laplacian_var)`` percentile rank
    - **Pixels**: ``width * height`` percentile rank
    - **BPP**: ``8 * size / pixels`` percentile rank

    Args:
        images: List of image dicts, each with keys: ``id``,
            ``aesthetic_laion``, ``aesthetic_nima``, ``laplacian_var``,
            ``width``, ``height``, ``size``.
        config: Config object with ``quality_weight_aesthetic``,
            ``quality_weight_sharpness``, ``quality_weight_pixels``,
            ``quality_weight_bpp``, and ``quality_alpha``.

    Returns:
        Dict mapping image ID to quality score (higher is better).
        A single image returns a score of 0.5.
    """
    n = len(images)
    if n == 0:
        return {}
    if n == 1:
        return {images[0]['id']: 0.5}

    w_a = config.quality_weight_aesthetic
    w_s = config.quality_weight_sharpness
    w_p = config.quality_weight_pixels
    w_b = config.quality_weight_bpp

    alpha = config.quality_alpha

    # Blend NIMA and LAION into a single aesthetic raw value
    has_nima = any(img.get('aesthetic_nima') is not None for img in images)

    if has_nima:
        laion_ranks = _percentile_ranks(
            [img.get('aesthetic_laion') or 0 for img in images]
        )
        nima_ranks = _percentile_ranks(
            [img.get('aesthetic_nima') or 0 for img in images]
        )
        # Blend, then re-rank
        blended = []
        for idx, img in enumerate(images):
            if img.get('aesthetic_nima') is None:
                blended.append(laion_ranks[idx])
            else:
                blended.append(
                    alpha * nima_ranks[idx] + (1 - alpha) * laion_ranks[idx]
                )
        aesthetic_raw = _percentile_ranks(blended)
    else:
        aesthetic_raw = _percentile_ranks(
            [img.get('aesthetic_laion') or 0 for img in images]
        )

    # Other components — percentile-ranked
    sharpness = _percentile_ranks(
        [math.log1p(img.get('laplacian_var') or 0) for img in images]
    )
    pixels = _percentile_ranks(
        [img['width'] * img['height'] for img in images]
    )
    bpp = _percentile_ranks(
        [8 * img['size'] / max(1, img['width'] * img['height']) for img in images]
    )

    # Combine with configurable weights
    scores = {}
    for i, img in enumerate(images):
        total = (
            w_a * aesthetic_raw[i]
            + w_s * sharpness[i]
            + w_p * pixels[i]
            + w_b * bpp[i]
        )
        scores[img['id']] = total

    return scores
