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
    compute_quality_scores — Weighted quality ranking for images
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

    raise RuntimeError(f'Could not resolve unique trash path for {basename} after 10000 attempts')


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


def compute_keep_trash_split(
    groups: list[dict],
    config,
    keep_count: int | None = None,
    keep_percent: int | None = None,
    trash_count: int | None = None,
    trash_percent: int | None = None,
) -> tuple[list[str], list[str]]:
    """Rank images in each group by quality and split into keep/trash.

    Iterates groups, determines how many images to keep per group based
    on the provided parameters, scores all images with
    :func:`compute_quality_scores`, sorts by score descending, and
    splits into keep and trash sets.  Always keeps at least 1 image per
    group.  Groups where the keep count >= group size contribute all
    their images to the keep set.

    Exactly one of the four parameters (keep_count, keep_percent,
    trash_count, trash_percent) should be provided, or none (defaults
    to keep_count=1).

    Args:
        groups: From ``get_group_images_ranked()`` — list of dicts,
            each with ``group_hash`` and ``images`` (list of dicts
            with ``id``, ``aesthetic_laion``, ``aesthetic_nima``,
            ``laplacian_var``, ``width``, ``height``, ``size``).
        config: Config object with quality weight settings.
        keep_count: Number of best images to keep per group.
        keep_percent: Percentage of images to keep per group (rounded up).
        trash_count: Number of worst images to trash per group.
        trash_percent: Percentage of worst images to trash per group.

    Returns:
        Tuple of ``(keep_ids, trash_ids)`` — flat lists of image IDs,
        quality-ranked within each group (best first in keep, worst
        first in trash).
    """
    # Default to keep_count=1 when no mode specified
    if keep_count is None and keep_percent is None and trash_count is None and trash_percent is None:
        keep_count = 1

    all_keep_ids = []
    all_trash_ids = []

    for group in groups:
        images = group['images']
        n = len(images)

        # Determine how many to keep for this group.
        # Trash mode: trash the worst N, keep the rest (always keep >= 1).
        # Keep mode: keep the best N, trash the rest.
        if trash_percent is not None:
            group_trash = math.ceil(n * trash_percent / 100)
            group_trash = max(0, min(group_trash, n - 1))
            group_keep = n - group_trash
        elif trash_count is not None:
            group_trash = max(0, min(trash_count, n - 1))
            group_keep = n - group_trash
        elif keep_percent is not None:
            group_keep = math.ceil(n * keep_percent / 100)
        else:
            group_keep = keep_count
        # Always keep at least 1
        group_keep = max(1, min(group_keep, n))

        # Score and rank images
        scores = compute_quality_scores(images, config)

        # Sort by score descending — best first
        ranked = sorted(images, key=lambda img: scores.get(img['id'], 0), reverse=True)

        # Split: keep top N, trash the rest
        all_keep_ids.extend(img['id'] for img in ranked[:group_keep])
        all_trash_ids.extend(img['id'] for img in ranked[group_keep:])

    return all_keep_ids, all_trash_ids


def compute_quality_scores(
    images: list[dict],
    config,
) -> dict[str, float]:
    """Compute composite quality scores for a set of images.

    This is a Python port of the frontend ``_computeQualityScores()``
    function in ``static/appstate/images.js``. The aesthetic component uses
    absolute scores (LAION and NIMA normalised to 0-1 by dividing by 10),
    while sharpness, resolution, and BPP use percentile ranking:

    - **Aesthetic**: ``alpha * (nima / 10) + (1 - alpha) * (laion / 10)``
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
    """
    n = len(images)
    if n == 0:
        return {}

    w_a = config.quality_weight_aesthetic
    w_s = config.quality_weight_sharpness
    w_p = config.quality_weight_pixels
    w_b = config.quality_weight_bpp

    alpha = config.quality_alpha

    # Aesthetic: absolute scores normalised to [0..1] by dividing by 10.
    # Both LAION and NIMA output on a 0-10 scale.  When both are available,
    # blend with alpha (NIMA weight) and (1-alpha) (LAION weight).
    has_nima = any(img.get('aesthetic_nima') is not None for img in images)
    aesthetic_raw = []
    for img in images:
        laion = (img.get('aesthetic_laion') or 0) / 10
        if not has_nima or img.get('aesthetic_nima') is None:
            aesthetic_raw.append(laion)
        else:
            nima = img['aesthetic_nima'] / 10
            aesthetic_raw.append(alpha * nima + (1 - alpha) * laion)

    # Other components — percentile-ranked
    sharpness = _percentile_ranks([math.log1p(img.get('laplacian_var') or 0) for img in images])
    pixels = _percentile_ranks([img['width'] * img['height'] for img in images])
    bpp = _percentile_ranks([8 * img['size'] / max(1, img['width'] * img['height']) for img in images])

    # Combine with configurable weights
    scores = {}
    for i, img in enumerate(images):
        total = w_a * aesthetic_raw[i] + w_s * sharpness[i] + w_p * pixels[i] + w_b * bpp[i]
        scores[img['id']] = total

    return scores
