"""
Shared EXIF utility functions for the Photonarium image database.

This leaf module contains pure-Python helpers used by both ``metadata.py``
and ``rawimage.py``.  It imports only the standard library so it can never
introduce circular-import issues.
"""

from __future__ import annotations

from datetime import datetime

# Every date format encountered in real-world EXIF data, ordered from most
# common to least common so the happy path returns on the first attempt.
_EXIF_DATETIME_FORMATS = [
    '%Y:%m:%d %H:%M:%S',  # Standard EXIF format: "2024:01:15 14:30:00"
    '%Y-%m-%d %H:%M:%S',
    '%Y/%m/%d %H:%M:%S',
    '%Y:%m:%d %H:%M',
    '%Y-%m-%d %H:%M',
]


def parse_exif_datetime(exif_value: str) -> datetime | None:
    """Parse an EXIF datetime string into a datetime object.

    EXIF datetime format is typically ``YYYY:MM:DD HH:MM:SS``.

    Args:
        exif_value: EXIF datetime string.

    Returns:
        datetime object if parsing succeeds, None otherwise.
    """
    if not exif_value or not isinstance(exif_value, str):
        return None

    stripped = exif_value.strip()
    for fmt in _EXIF_DATETIME_FORMATS:
        try:
            return datetime.strptime(stripped, fmt)
        except ValueError:
            continue

    return None
