"""
Image metadata and timestamp extraction for the Imaginary image database.

This module provides functions to extract EXIF metadata and derive timestamps
from images using multiple sources: EXIF metadata, filename patterns, and
filesystem metadata.

EXIF extraction reads all available tags in a single pass and returns
normalised, human-readable key-value pairs (e.g. "Camera": "Nikon D850").
The same data is reused for timestamp derivation to avoid opening the file
twice during indexing.

Priority order for timestamp derivation:
1. EXIF DateTimeOriginal tag (when photo was taken)
2. EXIF DateTime tag (when file was modified by software)
3. Parsed from filename/path (more reliable than filesystem dates)
4. Filesystem creation/modification time

Usage:
    from metadata import extract_exif_data, derive_timestamp_with_confidence

    # Extract all EXIF metadata as human-readable key-value pairs
    exif = extract_exif_data('/path/to/image.jpg')

    # Get best available timestamp (reusing pre-read EXIF data)
    ts, confidence = derive_timestamp_with_confidence('/path/to/image.jpg', exif_data=exif)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS, IFD
from typing import Any

import logging
import os
import re

from rawimage import is_raw_format, extract_raw_exif

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
    """Validate date components are a real calendar date.

    Uses datetime construction to catch invalid combinations like Feb 31.
    Without this, an invalid date string '20240231' would pass validation
    but then fail later when constructing a datetime, losing the timestamp.

    Args:
        year: Year value (should be 1900-2099).
        month: Month value (should be 1-12).
        day: Day value (valid for the given month/year).

    Returns:
        True if all components form a valid date, False otherwise.
    """
    if not (1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return False
    try:
        datetime(year, month, day)
        return True
    except ValueError:
        return False


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

    # Pillow cannot read EXIF from camera RAW formats — use exifread instead
    if is_raw_format(path):
        return extract_raw_exif(path)

    try:
        with Image.open(path) as img:
            exif_data = img.getexif()
            if not exif_data:
                return None

            # Build tag name to value mapping (getexif() returns an Exif
            # object keyed by tag ID — map to tag names for readability)
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
# FULL EXIF EXTRACTION
# =============================================================================

# EXIF ExposureProgram code → human-readable name
_EXPOSURE_PROGRAMS = {
    0: 'Not Defined',
    1: 'Manual',
    2: 'Program AE',
    3: 'Aperture Priority',
    4: 'Shutter Priority',
    5: 'Creative (Slow)',
    6: 'Action (Fast)',
    7: 'Portrait',
    8: 'Landscape',
}

# EXIF MeteringMode code → human-readable name
_METERING_MODES = {
    0: 'Unknown',
    1: 'Average',
    2: 'Center-Weighted',
    3: 'Spot',
    4: 'Multi-Spot',
    5: 'Multi-Segment',
    6: 'Partial',
    255: 'Other',
}

# EXIF WhiteBalance code → human-readable name
_WHITE_BALANCE = {
    0: 'Auto',
    1: 'Manual',
}

# EXIF ColorSpace code → human-readable name
_COLOR_SPACES = {
    1: 'sRGB',
    2: 'Adobe RGB',
    0xFFFF: 'Uncalibrated',
}

# EXIF Flash code → human-readable description (bit-packed field)
_FLASH_MODES = {
    0x00: 'No Flash',
    0x01: 'Fired',
    0x05: 'Fired, Return not detected',
    0x07: 'Fired, Return detected',
    0x08: 'On, Did not fire',
    0x09: 'On, Fired',
    0x0D: 'On, Return not detected',
    0x0F: 'On, Return detected',
    0x10: 'Off, Did not fire',
    0x14: 'Off, Did not fire, Return not detected',
    0x18: 'Auto, Did not fire',
    0x19: 'Auto, Fired',
    0x1D: 'Auto, Fired, Return not detected',
    0x1F: 'Auto, Fired, Return detected',
    0x20: 'No Flash function',
    0x30: 'Off, No Flash function',
    0x41: 'Fired, Red-eye reduction',
    0x45: 'Fired, Red-eye, Return not detected',
    0x47: 'Fired, Red-eye, Return detected',
    0x49: 'On, Red-eye',
    0x4D: 'On, Red-eye, Return not detected',
    0x4F: 'On, Red-eye, Return detected',
    0x59: 'Auto, Fired, Red-eye',
    0x5D: 'Auto, Fired, Red-eye, Return not detected',
    0x5F: 'Auto, Fired, Red-eye, Return detected',
}


def _format_rational(value: Any) -> float | None:
    """Convert an EXIF rational value to a float.

    Handles Pillow's IFDRational, tuples, and plain numbers.

    Args:
        value: EXIF rational value (IFDRational, tuple, int, or float).

    Returns:
        Float value, or None if conversion fails.
    """
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, tuple) and len(value) == 2:
            num, den = value
            return float(num) / float(den) if den != 0 else None
        # IFDRational or similar — just float() it
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _format_focal_length(value: Any) -> str | None:
    """Format a focal length value as 'Xmm'.

    Args:
        value: EXIF FocalLength rational.

    Returns:
        Formatted string like '50mm', or None.
    """
    fl = _format_rational(value)
    if fl is None:
        return None
    # Show as integer if whole number, otherwise 1 decimal
    if fl == int(fl):
        return f'{int(fl)}mm'
    return f'{fl:.1f}mm'


def _format_aperture(value: Any) -> str | None:
    """Format an F-number value as 'f/X.Y'.

    Args:
        value: EXIF FNumber rational.

    Returns:
        Formatted string like 'f/2.8', or None.
    """
    fnum = _format_rational(value)
    if fnum is None or fnum <= 0:
        return None
    if fnum == int(fnum):
        return f'f/{int(fnum)}'
    return f'f/{fnum:.1f}'


def _format_exposure_time(value: Any) -> str | None:
    """Format an exposure time value as a human-readable string.

    Converts to fraction notation for short exposures (e.g. '1/250s')
    and decimal for long exposures (e.g. '2.5s').

    Args:
        value: EXIF ExposureTime rational.

    Returns:
        Formatted string like '1/250s' or '2s', or None.
    """
    et = _format_rational(value)
    if et is None or et <= 0:
        return None
    if et >= 1:
        if et == int(et):
            return f'{int(et)}s'
        return f'{et:.1f}s'
    # Express as fraction: 1/N
    denominator = round(1.0 / et)
    return f'1/{denominator}s'


def _format_exposure_bias(value: Any) -> str | None:
    """Format an exposure bias value as '+X.Y EV' or '-X.Y EV'.

    Args:
        value: EXIF ExposureBiasValue rational.

    Returns:
        Formatted string like '+0.7 EV' or '0 EV', or None.
    """
    bias = _format_rational(value)
    if bias is None:
        return None
    if bias == 0:
        return '0 EV'
    sign = '+' if bias > 0 else ''
    # Show as fraction-like if close to common values
    if bias == int(bias):
        return f'{sign}{int(bias)} EV'
    return f'{sign}{bias:.1f} EV'


def _format_gps_coord(ref: str, degrees: Any, minutes: Any, seconds: Any) -> float | None:
    """Convert GPS DMS (degrees/minutes/seconds) to decimal degrees.

    Args:
        ref: Reference direction ('N', 'S', 'E', or 'W').
        degrees: Degrees value (rational or number).
        minutes: Minutes value (rational or number).
        seconds: Seconds value (rational or number).

    Returns:
        Decimal degrees (negative for S/W), or None if conversion fails.
    """
    d = _format_rational(degrees)
    m = _format_rational(minutes)
    s = _format_rational(seconds)
    if d is None or m is None or s is None:
        return None
    decimal = d + m / 60.0 + s / 3600.0
    if ref in ('S', 'W'):
        decimal = -decimal
    return decimal


def _format_gps(gps_info: dict[int | str, Any]) -> str | None:
    """Format GPS data from EXIF into a human-readable coordinate string.

    Args:
        gps_info: Dictionary of GPS IFD tags (numeric or named keys).

    Returns:
        Formatted string like '48.8566° N, 2.3522° E', or None.
    """
    # GPS tags can have numeric or string keys depending on source
    # Pillow's get_ifd(IFD.GPSInfo) returns numeric keys
    lat_ref = gps_info.get(1) or gps_info.get('GPSLatitudeRef')
    lat = gps_info.get(2) or gps_info.get('GPSLatitude')
    lon_ref = gps_info.get(3) or gps_info.get('GPSLongitudeRef')
    lon = gps_info.get(4) or gps_info.get('GPSLongitude')

    if not (lat_ref and lat and lon_ref and lon):
        return None

    try:
        # lat/lon are tuples of 3 rationals: (degrees, minutes, seconds)
        lat_ref = str(lat_ref).strip()
        lon_ref = str(lon_ref).strip()
        lat_decimal = _format_gps_coord(lat_ref, lat[0], lat[1], lat[2])
        lon_decimal = _format_gps_coord(lon_ref, lon[0], lon[1], lon[2])
        if lat_decimal is None or lon_decimal is None:
            return None

        lat_dir = 'N' if lat_decimal >= 0 else 'S'
        lon_dir = 'E' if lon_decimal >= 0 else 'W'
        return f'{abs(lat_decimal):.4f}° {lat_dir}, {abs(lon_decimal):.4f}° {lon_dir}'
    except (IndexError, TypeError):
        return None


def _extract_exif_pillow(path: Path) -> dict[str, str] | None:
    """Extract all EXIF metadata from a standard (Pillow-readable) image.

    Uses Pillow's getexif() API to read IFD0, EXIF sub-IFD, and GPS IFD
    tags in a single pass. Returns normalised human-readable key-value pairs.

    Args:
        path: Path to the image file.

    Returns:
        Dictionary of normalised EXIF key-value pairs, or None on error.
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None

            # Collect raw tag values from all IFDs
            raw: dict[str, Any] = {}

            # IFD0 (main image tags: Make, Model, Software, Artist, Copyright, etc.)
            for tag_id, value in exif.items():
                tag_name = TAGS.get(tag_id, str(tag_id))
                raw[tag_name] = value

            # EXIF sub-IFD (camera settings: exposure, aperture, ISO, etc.)
            try:
                exif_ifd = exif.get_ifd(IFD.Exif)
                if exif_ifd:
                    for tag_id, value in exif_ifd.items():
                        tag_name = TAGS.get(tag_id, str(tag_id))
                        raw[tag_name] = value
            except Exception:
                pass

            # GPS IFD
            gps_info = None
            try:
                gps_ifd = exif.get_ifd(IFD.GPSInfo)
                if gps_ifd:
                    gps_info = gps_ifd
            except Exception:
                pass

            return _normalise_exif(raw, gps_info)

    except (OSError, AttributeError, KeyError, SyntaxError) as e:
        logger.debug(f'Failed to extract EXIF via Pillow from {path}: {e}')
        return None


def _extract_exif_raw(path: Path) -> dict[str, str] | None:
    """Extract all EXIF metadata from a camera RAW file using exifread.

    Reads all tags (not just DateTime) and converts to normalised
    human-readable key-value pairs.

    Args:
        path: Path to the RAW file.

    Returns:
        Dictionary of normalised EXIF key-value pairs, or None on error.
    """
    try:
        import exifread
    except ImportError:
        logger.debug(f'Cannot extract RAW EXIF from {path}: exifread not installed')
        return None

    try:
        with open(path, 'rb') as f:
            # Read all tags (no stop_tag) for full metadata
            tags = exifread.process_file(f, details=False)

        if not tags:
            return None

        return _normalise_exifread_tags(tags)

    except Exception as e:
        logger.debug(f'Failed to extract EXIF from RAW file {path}: {e}')
        return None


def _normalise_exif(raw: dict[str, Any], gps_info: dict | None = None) -> dict[str, str] | None:
    """Normalise raw Pillow EXIF tags into human-readable key-value pairs.

    Converts numeric codes to names, formats rationals, combines related
    tags (Make+Model → Camera), and omits empty/null values.

    Args:
        raw: Dictionary of raw EXIF tag name → value pairs from Pillow.
        gps_info: Optional GPS IFD dictionary.

    Returns:
        Normalised dictionary with human-readable keys and string values,
        or None if no meaningful data was extracted.
    """
    result: dict[str, str] = {}

    # Camera: combine Make and Model (avoiding duplication like "Nikon Nikon D850")
    make = str(raw.get('Make', '')).strip()
    model = str(raw.get('Model', '')).strip()
    if model:
        # Many cameras include the make in the model string already
        if make and not model.lower().startswith(make.lower()):
            result['Camera'] = f'{make} {model}'
        else:
            result['Camera'] = model
    elif make:
        result['Camera'] = make

    # Lens
    lens_model = str(raw.get('LensModel', '')).strip()
    if lens_model:
        result['Lens'] = lens_model

    # Focal length
    if 'FocalLength' in raw:
        val = _format_focal_length(raw['FocalLength'])
        if val:
            result['Focal Length'] = val

    # Aperture (F-number)
    if 'FNumber' in raw:
        val = _format_aperture(raw['FNumber'])
        if val:
            result['Aperture'] = val

    # Shutter speed
    if 'ExposureTime' in raw:
        val = _format_exposure_time(raw['ExposureTime'])
        if val:
            result['Shutter Speed'] = val

    # ISO
    iso = raw.get('ISOSpeedRatings') or raw.get('PhotographicSensitivity')
    if iso is not None:
        # ISO can be an int or a tuple
        if isinstance(iso, (list, tuple)):
            iso = iso[0] if iso else None
        if iso is not None:
            result['ISO'] = str(int(iso))

    # Exposure compensation
    if 'ExposureBiasValue' in raw:
        val = _format_exposure_bias(raw['ExposureBiasValue'])
        if val:
            result['Exposure Comp'] = val

    # Exposure program
    prog = raw.get('ExposureProgram')
    if prog is not None:
        prog_int = int(prog) if not isinstance(prog, int) else prog
        result['Exposure Program'] = _EXPOSURE_PROGRAMS.get(prog_int, f'Unknown ({prog_int})')

    # Metering mode
    meter = raw.get('MeteringMode')
    if meter is not None:
        meter_int = int(meter) if not isinstance(meter, int) else meter
        result['Metering'] = _METERING_MODES.get(meter_int, f'Unknown ({meter_int})')

    # Flash
    flash = raw.get('Flash')
    if flash is not None:
        flash_int = int(flash) if not isinstance(flash, int) else flash
        result['Flash'] = _FLASH_MODES.get(flash_int, f'Flash ({flash_int})')

    # White balance
    wb = raw.get('WhiteBalance')
    if wb is not None:
        wb_int = int(wb) if not isinstance(wb, int) else wb
        result['White Balance'] = _WHITE_BALANCE.get(wb_int, f'Unknown ({wb_int})')

    # Color space
    cs = raw.get('ColorSpace')
    if cs is not None:
        cs_int = int(cs) if not isinstance(cs, int) else cs
        result['Color Space'] = _COLOR_SPACES.get(cs_int, f'Unknown ({cs_int})')

    # Software
    software = str(raw.get('Software', '')).strip()
    if software:
        result['Software'] = software

    # Artist
    artist = str(raw.get('Artist', '')).strip()
    if artist:
        result['Artist'] = artist

    # Copyright
    copyright_val = str(raw.get('Copyright', '')).strip()
    if copyright_val:
        result['Copyright'] = copyright_val

    # GPS
    if gps_info:
        gps_str = _format_gps(gps_info)
        if gps_str:
            result['GPS'] = gps_str

    # Date taken (for display — authoritative timestamp is images.timestamp)
    for tag in ('DateTimeOriginal', 'DateTime'):
        dt_val = raw.get(tag)
        if dt_val:
            parsed = _parse_exif_datetime(str(dt_val))
            if parsed:
                result['Date Taken'] = parsed.strftime('%Y-%m-%d %H:%M:%S')
                break

    return result if result else None


def _normalise_exifread_tags(tags: dict[str, Any]) -> dict[str, str] | None:
    """Normalise exifread tags (from RAW files) into human-readable pairs.

    exifread returns tags as {tag_name: IfdTag} where tag_name is like
    'EXIF FocalLength' and IfdTag has a .printable string representation.

    Args:
        tags: Dictionary from exifread.process_file().

    Returns:
        Normalised dictionary with human-readable keys and string values,
        or None if no meaningful data was extracted.
    """
    result: dict[str, str] = {}

    def _get(name: str) -> str:
        """Get a tag's printable value, trying both 'Image' and 'EXIF' prefixes."""
        for prefix in ('EXIF', 'Image', 'GPS'):
            key = f'{prefix} {name}'
            if key in tags:
                return str(tags[key]).strip()
        return ''

    # Camera
    make = _get('Make')
    model = _get('Model')
    if model:
        if make and not model.lower().startswith(make.lower()):
            result['Camera'] = f'{make} {model}'
        else:
            result['Camera'] = model
    elif make:
        result['Camera'] = make

    # Lens
    lens = _get('LensModel')
    if lens:
        result['Lens'] = lens

    # Focal length: exifread gives "50" or "50/1"
    fl_str = _get('FocalLength')
    if fl_str:
        try:
            if '/' in fl_str:
                num, den = fl_str.split('/')
                fl = float(num) / float(den)
            else:
                fl = float(fl_str)
            if fl == int(fl):
                result['Focal Length'] = f'{int(fl)}mm'
            else:
                result['Focal Length'] = f'{fl:.1f}mm'
        except (ValueError, ZeroDivisionError):
            pass

    # Aperture
    fn_str = _get('FNumber')
    if fn_str:
        try:
            if '/' in fn_str:
                num, den = fn_str.split('/')
                fnum = float(num) / float(den)
            else:
                fnum = float(fn_str)
            if fnum > 0:
                if fnum == int(fnum):
                    result['Aperture'] = f'f/{int(fnum)}'
                else:
                    result['Aperture'] = f'f/{fnum:.1f}'
        except (ValueError, ZeroDivisionError):
            pass

    # Shutter speed
    et_str = _get('ExposureTime')
    if et_str:
        try:
            if '/' in et_str:
                num, den = et_str.split('/')
                et = float(num) / float(den)
                if et >= 1:
                    result['Shutter Speed'] = f'{et:.1f}s' if et != int(et) else f'{int(et)}s'
                else:
                    result['Shutter Speed'] = f'{int(num)}/{int(den)}s'
            else:
                et = float(et_str)
                if et >= 1:
                    result['Shutter Speed'] = f'{et:.1f}s' if et != int(et) else f'{int(et)}s'
                else:
                    result['Shutter Speed'] = f'1/{round(1/et)}s'
        except (ValueError, ZeroDivisionError):
            pass

    # ISO
    iso_str = _get('ISOSpeedRatings')
    if iso_str:
        try:
            result['ISO'] = str(int(float(iso_str)))
        except ValueError:
            pass

    # Exposure compensation
    eb_str = _get('ExposureBiasValue')
    if eb_str:
        try:
            if '/' in eb_str:
                num, den = eb_str.split('/')
                bias = float(num) / float(den)
            else:
                bias = float(eb_str)
            if bias == 0:
                result['Exposure Comp'] = '0 EV'
            else:
                sign = '+' if bias > 0 else ''
                result['Exposure Comp'] = f'{sign}{bias:.1f} EV'
        except (ValueError, ZeroDivisionError):
            pass

    # Exposure program
    prog_str = _get('ExposureProgram')
    if prog_str:
        try:
            prog_int = int(prog_str)
            result['Exposure Program'] = _EXPOSURE_PROGRAMS.get(prog_int, f'Unknown ({prog_int})')
        except ValueError:
            # exifread may return the name directly
            result['Exposure Program'] = prog_str

    # Metering mode
    meter_str = _get('MeteringMode')
    if meter_str:
        try:
            meter_int = int(meter_str)
            result['Metering'] = _METERING_MODES.get(meter_int, f'Unknown ({meter_int})')
        except ValueError:
            result['Metering'] = meter_str

    # Flash
    flash_str = _get('Flash')
    if flash_str:
        try:
            flash_int = int(flash_str)
            result['Flash'] = _FLASH_MODES.get(flash_int, f'Flash ({flash_int})')
        except ValueError:
            result['Flash'] = flash_str

    # White balance
    wb_str = _get('WhiteBalance')
    if wb_str:
        try:
            wb_int = int(wb_str)
            result['White Balance'] = _WHITE_BALANCE.get(wb_int, f'Unknown ({wb_int})')
        except ValueError:
            result['White Balance'] = wb_str

    # Color space
    cs_str = _get('ColorSpace')
    if cs_str:
        try:
            cs_int = int(cs_str)
            result['Color Space'] = _COLOR_SPACES.get(cs_int, f'Unknown ({cs_int})')
        except ValueError:
            result['Color Space'] = cs_str

    # Software
    software = _get('Software')
    if software:
        result['Software'] = software

    # Artist
    artist = _get('Artist')
    if artist:
        result['Artist'] = artist

    # Copyright
    copyright_val = _get('Copyright')
    if copyright_val:
        result['Copyright'] = copyright_val

    # GPS (exifread uses 'GPS GPSLatitude' etc.)
    lat_ref = _get('GPSLatitudeRef')
    lat_str = tags.get('GPS GPSLatitude')
    lon_ref = _get('GPSLongitudeRef')
    lon_str = tags.get('GPS GPSLongitude')
    if lat_ref and lat_str and lon_ref and lon_str:
        try:
            # exifread GPS values are like "[48, 51, 24]" (degrees, minutes, seconds)
            lat_vals = lat_str.values
            lon_vals = lon_str.values
            lat_d = float(lat_vals[0])
            lat_m = float(lat_vals[1])
            lat_s = float(lat_vals[2])
            lon_d = float(lon_vals[0])
            lon_m = float(lon_vals[1])
            lon_s = float(lon_vals[2])
            lat_dec = lat_d + lat_m / 60 + lat_s / 3600
            lon_dec = lon_d + lon_m / 60 + lon_s / 3600
            if lat_ref == 'S':
                lat_dec = -lat_dec
            if lon_ref == 'W':
                lon_dec = -lon_dec
            lat_dir = 'N' if lat_dec >= 0 else 'S'
            lon_dir = 'E' if lon_dec >= 0 else 'W'
            result['GPS'] = f'{abs(lat_dec):.4f}° {lat_dir}, {abs(lon_dec):.4f}° {lon_dir}'
        except (AttributeError, IndexError, TypeError, ValueError):
            pass

    # Date taken
    for tag_name in ('EXIF DateTimeOriginal', 'Image DateTime'):
        val = tags.get(tag_name)
        if val:
            parsed = _parse_exif_datetime(str(val))
            if parsed:
                result['Date Taken'] = parsed.strftime('%Y-%m-%d %H:%M:%S')
                break

    return result if result else None


def extract_exif_data(path: Path | str) -> dict[str, str] | None:
    """Extract all EXIF metadata from an image as human-readable key-value pairs.

    Reads EXIF tags in a single pass and normalises them into consistent,
    human-readable keys and values. For standard images uses Pillow; for
    camera RAW files uses exifread.

    The returned dictionary uses standardised keys:
        Camera, Lens, Focal Length, Aperture, Shutter Speed, ISO,
        Exposure Comp, Exposure Program, Metering, Flash, White Balance,
        Color Space, Software, Artist, Copyright, GPS, Date Taken

    Args:
        path: Path to the image file.

    Returns:
        Dictionary of key-value pairs, or None if no EXIF data found.
    """
    path = Path(path)

    if is_raw_format(path):
        return _extract_exif_raw(path)

    return _extract_exif_pillow(path)


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
CONFIDENCE_USER = 0        # User assigned (via info panel)
CONFIDENCE_EXIF = 1        # From EXIF metadata
CONFIDENCE_FILENAME = 2    # Parsed from filename/path
CONFIDENCE_FILESYSTEM = 3  # From filesystem metadata
CONFIDENCE_UNKNOWN = 4     # None/unknown


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


def derive_timestamp_with_confidence(
    path: Path | str,
    exif_data: dict[str, str] | None = None,
) -> tuple[datetime | None, int]:
    """Derive the best timestamp for an image with confidence level.

    Tries sources in priority order:
    1. EXIF DateTimeOriginal tag (confidence 1)
    2. EXIF DateTime tag (confidence 1)
    3. Parsed from filename/path (confidence 2)
    4. Filesystem creation/modification time (confidence 3)

    When ``exif_data`` is provided (pre-read via ``extract_exif_data()``),
    the timestamp is extracted from it without re-opening the file. This
    avoids double I/O during indexing.

    Args:
        path: Path to the image file.
        exif_data: Optional pre-extracted EXIF key-value pairs from
            ``extract_exif_data()``. When provided, timestamp is derived
            from the 'Date Taken' key instead of re-reading the file.

    Returns:
        Tuple of (datetime, confidence) where confidence is:
        - 0: user assigned (not returned by this function)
        - 1: from EXIF
        - 2: from filename
        - 3: from filesystem
        - 4: none/unknown
    """
    path = Path(path)

    # Try EXIF first — reuse pre-read data when available to avoid double I/O
    if exif_data is not None:
        date_taken = exif_data.get('Date Taken')
        if date_taken:
            timestamp = _parse_exif_datetime(date_taken)
            if timestamp:
                logger.debug(f'Timestamp from pre-read EXIF: {timestamp} for {path}')
                return (timestamp, CONFIDENCE_EXIF)
    else:
        # Fall back to opening the file for EXIF (backward compat)
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
