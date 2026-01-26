"""
Timestamp extraction utilities for the Imaginary image database.

This module provides functions to extract and derive timestamps from images
using multiple sources: EXIF metadata, filename patterns, and filesystem
metadata.

Priority order for timestamp derivation:
1. EXIF DateTimeOriginal tag (when photo was taken)
2. EXIF DateTime tag (when file was modified by software)
3. Parsed from filename/path (more reliable than filesystem dates)
4. Filesystem creation/modification time

Usage:
    from timestamps import derive_timestamp, extract_exif_timestamp

    # Get best available timestamp
    ts = derive_timestamp('/path/to/image.jpg')

    # Get EXIF timestamp only
    ts = extract_exif_timestamp('/path/to/image.jpg')
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS
from typing import Any

import logging
import os
import re

# Configure module logger
logger = logging.getLogger(__name__)


# =============================================================================
# REGEX PATTERNS FOR PARSING DATES AND TIMES
# =============================================================================

# 8 digits: YYYYMMDD
_PATTERN_DATE_8DIGITS = re.compile(r'(\d{8})')
# 6 digits: YYMMDD
_PATTERN_DATE_6DIGITS = re.compile(r'(\d{6})')
# 3 groups with separator: YYYY-MM-DD or YY-MM-DD (separator is single non-digit)
_PATTERN_DATE_SEPARATED = re.compile(r'(\d{2,4})\D(\d{2})\D(\d{2})')
# Partial date patterns (for incomplete dates - default missing parts to Jan 1)
# Year-month: YYYY-MM or YYYYMM (4 digits for month to avoid matching YYMMDD)
_PATTERN_DATE_YEAR_MONTH_SEP = re.compile(r'((?:19|20)\d{2})\D(\d{2})(?!\d)')
_PATTERN_DATE_YEAR_MONTH = re.compile(r'((?:19|20)\d{2})(\d{2})(?!\d)')
# Year only: standalone 4-digit year (1900-2099) with word boundaries
_PATTERN_DATE_YEAR_ONLY = re.compile(r'(?<!\d)((?:19|20)\d{2})(?!\d)')

# 6 digits for time: HHMMSS
_PATTERN_TIME_6DIGITS = re.compile(r'(\d{6})')
# 4 digits for time: HHMM
_PATTERN_TIME_4DIGITS = re.compile(r'(\d{4})')
# 2-3 groups with separator: HH:MM or HH:MM:SS
_PATTERN_TIME_SEPARATED = re.compile(r'(\d{2})\D(\d{2})(?:\D(\d{2}))?')


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

def _validate_date(year: int, month: int, day: int) -> bool:
    """Validate date components are within reasonable ranges.

    Args:
        year: Year value (should be 1900-2099).
        month: Month value (should be 1-12).
        day: Day value (should be 1-31).

    Returns:
        True if all components are valid, False otherwise.
    """
    return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31


def _validate_time(hour: int, minute: int, second: int) -> bool:
    """Validate time components are within reasonable ranges.

    Args:
        hour: Hour value (should be 0-23).
        minute: Minute value (should be 0-59).
        second: Second value (should be 0-59).

    Returns:
        True if all components are valid, False otherwise.
    """
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


# =============================================================================
# EXIF EXTRACTION
# =============================================================================

def _parse_exif_datetime(exif_value: str) -> datetime | None:
    """Parse an EXIF datetime string into a datetime object.

    EXIF datetime format is typically "YYYY:MM:DD HH:MM:SS".

    Args:
        exif_value: EXIF datetime string.

    Returns:
        datetime object if parsing succeeds, None otherwise.
    """
    if not exif_value or not isinstance(exif_value, str):
        return None

    # EXIF format: "2024:01:15 14:30:00"
    try:
        return datetime.strptime(exif_value.strip(), '%Y:%m:%d %H:%M:%S')
    except ValueError:
        pass

    # Some cameras use different formats, try alternatives
    alternative_formats = [
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S',
        '%Y:%m:%d %H:%M',
        '%Y-%m-%d %H:%M',
    ]
    for fmt in alternative_formats:
        try:
            return datetime.strptime(exif_value.strip(), fmt)
        except ValueError:
            continue

    return None


def extract_exif_timestamp(path: Path | str) -> datetime | None:
    """Extract timestamp from image EXIF data.

    Tries DateTimeOriginal first (when photo was taken), then DateTime
    (when file was last modified by software).

    Args:
        path: Path to the image file.

    Returns:
        datetime object if EXIF timestamp found, None otherwise.
    """
    path = Path(path)

    try:
        with Image.open(path) as img:
            exif_data = img._getexif()
            if exif_data is None:
                return None

            # Build tag name to value mapping
            exif_dict: dict[str, Any] = {}
            for tag_id, value in exif_data.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                exif_dict[tag_name] = value

            # Try DateTimeOriginal first (when photo was actually taken)
            if 'DateTimeOriginal' in exif_dict:
                result = _parse_exif_datetime(exif_dict['DateTimeOriginal'])
                if result:
                    return result

            # Fall back to DateTime (when file was modified)
            if 'DateTime' in exif_dict:
                result = _parse_exif_datetime(exif_dict['DateTime'])
                if result:
                    return result

    except (OSError, AttributeError, KeyError) as e:
        logger.debug(f'Failed to extract EXIF from {path}: {e}')

    return None


# =============================================================================
# FILESYSTEM TIMESTAMP
# =============================================================================

def extract_filesystem_timestamp(path: Path | str) -> datetime | None:
    """Extract timestamp from filesystem metadata.

    Prefers creation time (Windows) or birth time (Unix if available),
    falls back to modification time.

    Args:
        path: Path to the file.

    Returns:
        datetime object from filesystem metadata, or None if file doesn't exist.
    """
    path = Path(path)

    if not path.exists():
        return None

    try:
        stat_result = path.stat()

        # Try creation time first (Windows st_ctime, or st_birthtime on some Unix)
        # On Windows, st_ctime is creation time
        # On Unix, st_ctime is metadata change time, not creation time
        if os.name == 'nt':
            # Windows: st_ctime is creation time
            creation_time = stat_result.st_ctime
        else:
            # Unix: try st_birthtime if available (macOS, some BSDs)
            creation_time = getattr(stat_result, 'st_birthtime', None)

        if creation_time:
            return datetime.fromtimestamp(creation_time)

        # Fall back to modification time
        return datetime.fromtimestamp(stat_result.st_mtime)

    except OSError as e:
        logger.debug(f'Failed to get filesystem timestamp for {path}: {e}')
        return None


# =============================================================================
# FILENAME/PATH PARSING
# =============================================================================

def _parse_date_from_string(text: str) -> tuple[int, int, int, int] | None:
    """Parse a date from a string, returning (year, month, day, position).

    Tries multiple patterns in order of specificity. For partial dates where
    month or day cannot be determined, defaults to January 1st for missing parts.

    Pattern priority:
    1. YYYYMMDD (8 digits)
    2. YYYY-MM-DD or YY-MM-DD (separated)
    3. YYMMDD (6 digits)
    4. YYYY-MM (year-month, day defaults to 1)
    5. YYYY (year only, month and day default to January 1st)

    Args:
        text: String to search for date patterns.

    Returns:
        Tuple of (year, month, day, end_position) if found, None otherwise.
        The end_position indicates where the date pattern ends in the string.
    """
    # Try 8-digit pattern first: YYYYMMDD
    for match in _PATTERN_DATE_8DIGITS.finditer(text):
        digits = match.group(1)
        year = int(digits[0:4])
        month = int(digits[4:6])
        day = int(digits[6:8])
        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try separated pattern: YYYY-MM-DD or YY-MM-DD
    for match in _PATTERN_DATE_SEPARATED.finditer(text):
        year_str, month_str, day_str = match.groups()
        year = int(year_str)
        month = int(month_str)
        day = int(day_str)

        # Handle 2-digit year
        if year < 100:
            year = 1900 + year if year > 50 else 2000 + year

        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try 6-digit pattern: YYMMDD (must avoid matching time patterns)
    # Only use this if no 8-digit pattern found
    for match in _PATTERN_DATE_6DIGITS.finditer(text):
        digits = match.group(1)
        year = int(digits[0:2])
        month = int(digits[2:4])
        day = int(digits[4:6])

        # Handle 2-digit year
        year = 1900 + year if year > 50 else 2000 + year

        if _validate_date(year, month, day):
            return (year, month, day, match.end())

    # Try partial date patterns - default missing parts to January 1st
    # Year-month with separator: YYYY-MM
    for match in _PATTERN_DATE_YEAR_MONTH_SEP.finditer(text):
        year = int(match.group(1))
        month = int(match.group(2))
        if 1900 <= year <= 2099 and 1 <= month <= 12:
            return (year, month, 1, match.end())

    # Year-month without separator: YYYYMM (e.g., 202401)
    for match in _PATTERN_DATE_YEAR_MONTH.finditer(text):
        year = int(match.group(1))
        month = int(match.group(2))
        if 1900 <= year <= 2099 and 1 <= month <= 12:
            return (year, month, 1, match.end())

    # Year only: standalone 4-digit year (e.g., folder "2014" or "Photos 2014")
    for match in _PATTERN_DATE_YEAR_ONLY.finditer(text):
        year = int(match.group(1))
        if 1900 <= year <= 2099:
            return (year, 1, 1, match.end())

    return None


def _parse_time_from_string(text: str, start_pos: int = 0) -> tuple[int, int, int] | None:
    """Parse a time from a string, searching from a given position.

    Args:
        text: String to search for time patterns.
        start_pos: Position in string to start searching from.

    Returns:
        Tuple of (hour, minute, second) if found, None otherwise.
    """
    search_text = text[start_pos:]

    # Try 6-digit pattern: HHMMSS
    for match in _PATTERN_TIME_6DIGITS.finditer(search_text):
        digits = match.group(1)
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        second = int(digits[4:6])
        if _validate_time(hour, minute, second):
            return (hour, minute, second)

    # Try separated pattern: HH:MM:SS or HH:MM
    for match in _PATTERN_TIME_SEPARATED.finditer(search_text):
        hour_str, minute_str, second_str = match.groups()
        hour = int(hour_str)
        minute = int(minute_str)
        second = int(second_str) if second_str else 0
        if _validate_time(hour, minute, second):
            return (hour, minute, second)

    # Try 4-digit pattern: HHMM (less reliable, could be other numbers)
    for match in _PATTERN_TIME_4DIGITS.finditer(search_text):
        digits = match.group(1)
        hour = int(digits[0:2])
        minute = int(digits[2:4])
        if _validate_time(hour, minute, 0):
            return (hour, minute, 0)

    return None


def parse_timestamp_from_path(path: Path | str) -> datetime | None:
    """Parse timestamp from filename or path components.

    Searches the full path string for date patterns, optionally followed
    by time patterns.

    Args:
        path: File path to parse.

    Returns:
        datetime object if a valid date pattern is found, None otherwise.
    """
    path = Path(path)
    # Use the full path string for searching (includes directory names)
    path_str = str(path)

    # Parse date
    date_result = _parse_date_from_string(path_str)
    if date_result is None:
        return None

    year, month, day, date_end_pos = date_result

    # Try to parse time after the date
    time_result = _parse_time_from_string(path_str, date_end_pos)
    if time_result:
        hour, minute, second = time_result
    else:
        hour, minute, second = 0, 0, 0

    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError as e:
        # Invalid date (e.g., Feb 30)
        logger.debug(f'Invalid date from path {path}: {e}')
        return None


# =============================================================================
# MAIN TIMESTAMP DERIVATION
# =============================================================================

# =============================================================================
# TIMESTAMP CONFIDENCE LEVELS
# =============================================================================

# Confidence levels for timestamp sources (lower = more reliable)
CONFIDENCE_USER = 0       # User assigned (via info panel)
CONFIDENCE_EXIF = 1       # From EXIF metadata
CONFIDENCE_FILENAME = 2   # Parsed from filename/path
CONFIDENCE_FILESYSTEM = 3 # From filesystem metadata
CONFIDENCE_UNKNOWN = 4    # None/unknown


def derive_timestamp(path: Path | str) -> datetime | None:
    """Derive the best timestamp for an image using multiple sources.

    Tries sources in priority order:
    1. EXIF DateTimeOriginal tag
    2. EXIF DateTime tag
    3. Parsed from filename/path (more reliable than filesystem dates)
    4. Filesystem creation time
    5. Filesystem modification time

    Args:
        path: Path to the image file.

    Returns:
        datetime object from the highest-priority available source,
        or None if no timestamp could be determined.
    """
    timestamp, _ = derive_timestamp_with_confidence(path)
    return timestamp


def derive_timestamp_with_confidence(path: Path | str) -> tuple[datetime | None, int]:
    """Derive the best timestamp for an image with confidence level.

    Tries sources in priority order:
    1. EXIF DateTimeOriginal tag (confidence 1)
    2. EXIF DateTime tag (confidence 1)
    3. Parsed from filename/path (confidence 2)
    4. Filesystem creation/modification time (confidence 3)

    Args:
        path: Path to the image file.

    Returns:
        Tuple of (datetime, confidence) where confidence is:
        - 0: user assigned (not returned by this function)
        - 1: from EXIF
        - 2: from filename
        - 3: from filesystem
        - 4: none/unknown
    """
    path = Path(path)

    # Try EXIF first (handles both DateTimeOriginal and DateTime internally)
    timestamp = extract_exif_timestamp(path)
    if timestamp:
        logger.debug(f'Timestamp from EXIF: {timestamp} for {path}')
        return (timestamp, CONFIDENCE_EXIF)

    # Try parsing from filename/path (before filesystem, as files get copied around)
    timestamp = parse_timestamp_from_path(path)
    if timestamp:
        logger.debug(f'Timestamp from filename: {timestamp} for {path}')
        return (timestamp, CONFIDENCE_FILENAME)

    # Try filesystem timestamp as last resort
    timestamp = extract_filesystem_timestamp(path)
    if timestamp:
        logger.debug(f'Timestamp from filesystem: {timestamp} for {path}')
        return (timestamp, CONFIDENCE_FILESYSTEM)

    logger.debug(f'No timestamp found for {path}')
    return (None, CONFIDENCE_UNKNOWN)
