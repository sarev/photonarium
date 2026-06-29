"""
Image metadata and timestamp extraction for the Photonarium image database.

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

import dataclasses
import datetime as _dt
import fnmatch
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from PIL import Image
from PIL.ExifTags import IFD, TAGS

from exifutil import parse_exif_datetime
from rawimage import extract_raw_exif, is_raw_format

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
# SCORING-BASED FILENAME DATE PARSER
# =============================================================================
# A candidate/scoring model for parsing dates from filenames and path
# components.  Handles human-style dates (e.g. "Summer 2006", "Feb'03",
# "early May"), resolves DMY/MDY ambiguity via a configurable preference,
# and merges year/month/day hints from multiple path components.
#
# Runs alongside the legacy regex-cascade parser (below).  The scoring
# parser is tried first; if it doesn't reach min_score, the legacy parser
# gets a chance.


@dataclasses.dataclass(slots=True)
class _ParsePolicy:
    """Controls how ambiguous filenames are interpreted.

    Only ``date_order`` is exposed as a user-configurable setting.
    The remaining fields are sensible defaults documented here for
    future tuning.
    """

    # Date order bias for ambiguous numeric triplets (user-configurable).
    # Supported: "DMY", "MDY", "YMD"
    date_order: str = 'DMY'

    # If a month is known but day is missing, use this day.
    default_day: int = 1

    # If a year is missing entirely, allow the current year.
    assume_current_year: bool = True

    # If a parsed date would be in the future, reject it.
    forbid_future_dates: bool = True

    # Minimum score required to accept a parse.
    min_score: float = 3.0

    # Treat seasons as the start of the season by default.
    season_as_start: bool = True


@dataclasses.dataclass(slots=True)
class _Candidate:
    """A potential date interpretation with a running score and assumptions log."""

    year: int | None = None
    month: int | None = None
    day: int | None = None
    hour: int = 0
    minute: int = 0
    second: int = 0
    score: float = 0.0
    assumptions: list[str] = dataclasses.field(default_factory=list)

    def add(self, points: float, reason: str) -> None:
        """Add score points with a reason for debugging."""
        self.score += points
        self.assumptions.append(reason)

    def clone(self) -> _Candidate:
        """Create an independent copy of this candidate."""
        return _Candidate(
            year=self.year,
            month=self.month,
            day=self.day,
            hour=self.hour,
            minute=self.minute,
            second=self.second,
            score=self.score,
            assumptions=list(self.assumptions),
        )


# ---------------------------------------------------------------------------
# Scoring parser — lexicon
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {
    'jan': 1,
    'janu': 1,
    'january': 1,
    'feb': 2,
    'febr': 2,
    'february': 2,
    'mar': 3,
    'marc': 3,
    'march': 3,
    'apr': 4,
    'apri': 4,
    'april': 4,
    'may': 5,
    'jun': 6,
    'june': 6,
    'jul': 7,
    'july': 7,
    'aug': 8,
    'augu': 8,
    'august': 8,
    'sep': 9,
    'spt': 9,
    'sept': 9,
    'september': 9,
    'oct': 10,
    'octo': 10,
    'october': 10,
    'nov': 11,
    'nove': 11,
    'november': 11,
    'dec': 12,
    'dece': 12,
    'december': 12,
}

# Holidays and seasons mapped to (month, day).  These are used the same way
# as month words — if a year is found nearby it's attached, otherwise the
# current year is assumed.
_HOLIDAYS: dict[str, tuple[int, int]] = {
    'christmas': (12, 25),
    'xmas': (12, 25),
    'halloween': (10, 31),
    'nye': (12, 31),
}

_SEASONS: dict[str, tuple[int, int]] = {
    'spring': (3, 1),
    'summer': (6, 1),
    'autumn': (9, 1),
    'fall': (9, 1),
    'winter': (12, 1),
}

_POSITION_DAY: dict[str, int] = {
    'early': 1,
    'mid': 15,
    'late': 25,
}


# ---------------------------------------------------------------------------
# Scoring parser — helpers
# ---------------------------------------------------------------------------


def _expand_two_digit_year(yy: int, *, today: _dt.date) -> int:
    """Expand a 2-digit year, preferring 2000s unless that would be future."""
    year = 2000 + yy
    if year > today.year:
        year -= 100
    return year


def _safe_date(year: int, month: int, day: int) -> _dt.date | None:
    """Return a date if valid, else None — wraps existing _validate_date."""
    try:
        return _dt.date(year, month, day)
    except ValueError:
        return None


def _safe_datetime(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> _dt.datetime | None:
    """Return a datetime if valid, else None."""
    try:
        return _dt.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _component_weight(component_index: int, total_components: int) -> float:
    """Weight for coarse-grain signals (year) — earlier path components score higher."""
    if total_components <= 1:
        return 1.0
    frac = component_index / (total_components - 1)
    return 1.25 - 0.5 * frac


def _leaf_weight(component_index: int, total_components: int) -> float:
    """Weight for fine-grain signals (day/time) — later path components score higher."""
    if total_components <= 1:
        return 1.0
    frac = component_index / (total_components - 1)
    return 0.75 + 0.5 * frac


def _split_words(text: str) -> list[str]:
    """Split a path component into tokens on non-alnum and case/digit transitions.

    Preserves apostrophes (important for ``Feb'03``-style years).
    """
    text = text.strip()
    # lower->Upper
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # alpha<->digit
    text = re.sub(r'([A-Za-z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([A-Za-z])', r'\1 \2', text)
    # non-alnum except apostrophe
    text = re.sub(r"[^A-Za-z0-9']+", ' ', text)
    return [tok for tok in text.split() if tok]


def _month_from_word(token: str) -> int | None:
    """Return month number (1-12) if token is a month name/abbreviation."""
    return _MONTHS.get(token.lower())


def _season_from_word(token: str) -> tuple[int, int] | None:
    """Return (month, day) if token is a season name."""
    return _SEASONS.get(token.lower())


def _position_day_from_word(token: str) -> int | None:
    """Return day-of-month for positional words (early/mid/late)."""
    return _POSITION_DAY.get(token.lower())


def _holiday_from_word(token: str) -> tuple[int, int] | None:
    """Return (month, day) if token is a recognised holiday name."""
    return _HOLIDAYS.get(token.lower())


def _parse_hhmm_or_hhmmss(token: str) -> tuple[int, int, int] | None:
    """Parse a compact time token (HHMM or HHMMSS) to (h, m, s)."""
    if re.fullmatch(r'\d{4}', token):
        hh, mm = int(token[:2]), int(token[2:])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return hh, mm, 0
        return None

    if re.fullmatch(r'\d{6}', token):
        hh, mm, ss = int(token[:2]), int(token[2:4]), int(token[4:])
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return hh, mm, ss
        return None

    return None


# ---------------------------------------------------------------------------
# Scoring parser — year extraction
# ---------------------------------------------------------------------------


def _extract_resolution_numbers(text: str) -> set[str]:
    """Find numbers that are part of resolution patterns (e.g. 1920x1080).

    Detects patterns like ``1920x1080``, ``1920_1080``, ``1080-1920``,
    ``960x540``, etc.  Both numbers must be plausible pixel dimensions
    (120–8640).  Returns the matched number *strings* so callers can
    exclude them from year-hint consideration.
    """
    excluded: set[str] = set()
    for m in re.finditer(
        r'(?<!\d)(\d{3,4})\s*[x×_\-]\s*(\d{3,4})(?!\d)',
        text,
        re.IGNORECASE,
    ):
        a, b = int(m.group(1)), int(m.group(2))
        if 120 <= a <= 8640 and 120 <= b <= 8640:
            excluded.add(m.group(1))
            excluded.add(m.group(2))
    return excluded


def _extract_year_hints(
    components: Sequence[str],
    *,
    today: _dt.date,
) -> list[tuple[int, int, float]]:
    """Extract year hints from path components.

    Returns list of (component_index, year, score).
    """
    hints: list[tuple[int, int, float]] = []
    total = len(components)

    for i, comp in enumerate(components):
        words = _split_words(comp)
        if not words:
            continue

        weight = _component_weight(i, total)

        # Detect resolution patterns (e.g. 1920x1080, 960_540) so we
        # don't mistake pixel dimensions for years.
        resolution_nums = _extract_resolution_numbers(comp)

        # Pure 4-digit directory/file stem
        if len(words) == 1 and re.fullmatch(r'\d{4}', words[0]):
            y = int(words[0])
            if 1800 <= y <= today.year and words[0] not in resolution_nums:
                hints.append((i, y, 4.0 * weight))
                continue

        # Embedded 4-digit year(s)
        for w in words:
            if re.fullmatch(r'\d{4}', w):
                y = int(w)
                if 1800 <= y <= today.year and w not in resolution_nums:
                    hints.append((i, y, 2.5 * weight))

        # Apostrophe year, eg '03
        for w in words:
            m = re.fullmatch(r"'(\d{2})", w)
            if m:
                yy = int(m.group(1))
                y = _expand_two_digit_year(yy, today=today)
                hints.append((i, y, 1.75 * weight))

    return hints


def _nearest_parent_year(
    component_index: int,
    year_hints: Sequence[tuple[int, int, float]],
) -> tuple[int | None, float]:
    """Find the nearest earlier (or same) component's year hint."""
    best_year: int | None = None
    best_score = float('-inf')

    for i, year, score in year_hints:
        if i <= component_index and score > best_score:
            best_year = year
            best_score = score

    if best_year is None:
        return None, 0.0
    return best_year, best_score


# ---------------------------------------------------------------------------
# Scoring parser — candidate generators
# ---------------------------------------------------------------------------


def _candidate_from_compact_datetime(
    token: str,
    *,
    today: _dt.date,
    leaf_bias: float,
) -> _Candidate | None:
    """Try to parse a compact date/datetime token (YYYYMMDD, YYYYMMDDHHMM, etc.)."""
    if re.fullmatch(r'\d{8}', token):
        y, m, d = int(token[:4]), int(token[4:6]), int(token[6:8])
        if _safe_date(y, m, d):
            c = _Candidate(year=y, month=m, day=d)
            c.add(7.0 * leaf_bias, 'compact yyyymmdd')
            return c
        return None

    if re.fullmatch(r'\d{12}', token):
        y, m, d = int(token[:4]), int(token[4:6]), int(token[6:8])
        hh, mm = int(token[8:10]), int(token[10:12])
        if _safe_datetime(y, m, d, hh, mm):
            c = _Candidate(year=y, month=m, day=d, hour=hh, minute=mm)
            c.add(8.0 * leaf_bias, 'compact yyyymmddhhmm')
            return c
        return None

    if re.fullmatch(r'\d{14}', token):
        y, m, d = int(token[:4]), int(token[4:6]), int(token[6:8])
        hh, mm, ss = int(token[8:10]), int(token[10:12]), int(token[12:14])
        if _safe_datetime(y, m, d, hh, mm, ss):
            c = _Candidate(year=y, month=m, day=d, hour=hh, minute=mm, second=ss)
            c.add(8.5 * leaf_bias, 'compact yyyymmddhhmmss')
            return c
        return None

    if re.fullmatch(r'\d{6}', token):
        # Try YYYYMM first — if the first 4 digits form a plausible year
        # and the remaining 2 are a valid month, this is more likely than
        # YYMMDD (which has ambiguity with HHMMSS).
        ym_year, ym_month = int(token[:4]), int(token[4:6])
        if 1800 <= ym_year <= today.year and 1 <= ym_month <= 12:
            c = _Candidate(year=ym_year, month=ym_month, day=1)
            c.add(4.5 * leaf_bias, 'compact yyyymm')
            c.add(-0.5, 'default day of month')
            return c

        # Could be YYMMDD, but could also be HHMMSS — lower confidence
        yy, m, d = int(token[:2]), int(token[2:4]), int(token[4:6])
        y = _expand_two_digit_year(yy, today=today)
        if _safe_date(y, m, d):
            c = _Candidate(year=y, month=m, day=d)
            c.add(5.0 * leaf_bias, 'compact yymmdd')
            c.add(-1.0, 'could also be hhmmss')
            return c
        return None

    return None


def _numeric_triplet_candidates(
    a: int,
    b: int,
    c: int,
    *,
    len_a: int,
    len_b: int,
    len_c: int,
    policy: _ParsePolicy,
    today: _dt.date,
    coarse_bias: float,
    fine_bias: float,
) -> Iterator[_Candidate]:
    """Generate date candidates for an ambiguous numeric triplet (e.g. 07-03-2024)."""
    # Strong case: 4-digit leading year
    if len_a == 4:
        if _safe_date(a, b, c):
            cand = _Candidate(year=a, month=b, day=c)
            cand.add(6.0 * coarse_bias, 'numeric ymd with 4-digit year')
            yield cand
        return

    # Strong case: 4-digit trailing year
    if len_c == 4:
        valid_dmy = _safe_date(c, b, a)
        valid_mdy = _safe_date(c, a, b)

        if valid_dmy:
            cand = _Candidate(year=c, month=b, day=a)
            cand.add(4.0 * fine_bias, 'numeric dmy with 4-digit year')
            if policy.date_order == 'DMY':
                cand.add(1.5, 'policy prefers dmy')
            elif policy.date_order == 'MDY':
                cand.add(-1.0, 'policy disfavors dmy')
            yield cand

        if valid_mdy:
            cand = _Candidate(year=c, month=a, day=b)
            cand.add(4.0 * fine_bias, 'numeric mdy with 4-digit year')
            if policy.date_order == 'MDY':
                cand.add(1.5, 'policy prefers mdy')
            elif policy.date_order == 'DMY':
                cand.add(-1.0, 'policy disfavors mdy')
            yield cand

        return

    # 2-digit leading year: YY-MM-DD
    if len_a == 2:
        y = _expand_two_digit_year(a, today=today)
        if _safe_date(y, b, c):
            cand = _Candidate(year=y, month=b, day=c)
            cand.add(4.5 * fine_bias, 'numeric ymd with 2-digit year')
            yield cand

    # 2-digit trailing year: DD-MM-YY and MM-DD-YY
    if len_c == 2:
        y = _expand_two_digit_year(c, today=today)

        valid_dmy = _safe_date(y, b, a)
        valid_mdy = _safe_date(y, a, b)

        if valid_dmy:
            cand = _Candidate(year=y, month=b, day=a)
            cand.add(3.5 * fine_bias, 'numeric dmy with 2-digit year')
            if policy.date_order == 'DMY':
                cand.add(1.25, 'policy prefers dmy')
            elif policy.date_order == 'MDY':
                cand.add(-0.75, 'policy disfavors dmy')
            yield cand

        if valid_mdy:
            cand = _Candidate(year=y, month=a, day=b)
            cand.add(3.5 * fine_bias, 'numeric mdy with 2-digit year')
            if policy.date_order == 'MDY':
                cand.add(1.25, 'policy prefers mdy')
            elif policy.date_order == 'DMY':
                cand.add(-0.75, 'policy disfavors mdy')
            yield cand


def _extract_delimited_numeric_triplets(
    component: str,
    *,
    policy: _ParsePolicy,
    today: _dt.date,
    coarse_bias: float,
    fine_bias: float,
) -> list[_Candidate]:
    """Find delimited date-like triplets (e.g. 2024-03-07, 07.03.24, 2024_03_07)."""
    out: list[_Candidate] = []

    for m in re.finditer(r'(?<!\d)(\d{1,4})[._/\-](\d{1,2})[._/\-](\d{1,4})(?!\d)', component):
        s1, s2, s3 = m.groups()
        a, b, c_val = int(s1), int(s2), int(s3)

        # Skip triplets that look like HH.MM.SS times rather than dates.
        # A triplet is time-like when all values fit valid time ranges AND
        # it's preceded by whitespace (e.g. "2024-05-17 16.08.33") — the
        # space indicates it follows a date, making it a time suffix.
        if m.start() > 0 and component[m.start() - 1] == ' ' and 0 <= a <= 23 and 0 <= b <= 59 and 0 <= c_val <= 59:
            continue

        out.extend(
            _numeric_triplet_candidates(
                a,
                b,
                c_val,
                len_a=len(s1),
                len_b=len(s2),
                len_c=len(s3),
                policy=policy,
                today=today,
                coarse_bias=coarse_bias,
                fine_bias=fine_bias,
            )
        )

    return out


def _extract_month_word_candidates(
    words: Sequence[str],
    *,
    component_index: int,
    total_components: int,
    policy: _ParsePolicy,
    today: _dt.date,
) -> list[_Candidate]:
    """Handle month/season/holiday words like May, early May, June-02, Feb '03, Xmas 2019."""
    out: list[_Candidate] = []
    coarse_bias = _component_weight(component_index, total_components)
    fine_bias = _leaf_weight(component_index, total_components)

    lower = [w.lower() for w in words]

    for i, tok in enumerate(lower):
        month = _month_from_word(tok)
        season = _season_from_word(tok)
        holiday = _holiday_from_word(tok)

        if month is None and season is None and holiday is None:
            continue

        c = _Candidate()

        # Month, season, or holiday baseline
        if holiday is not None:
            # Holidays provide both month and day — stronger signal than a bare month
            h_month, h_day = holiday
            c.month = h_month
            c.day = h_day
            c.add(2.5 * fine_bias, 'holiday word')
        elif month is not None:
            c.month = month
            c.add(2.0 * fine_bias, 'month word')
        else:
            smonth, sday = season  # type: ignore[misc]
            if policy.season_as_start:
                c.month = smonth
                c.day = sday
                c.add(1.5 * coarse_bias, 'season word as season start')
            else:
                c.month = smonth + 1
                c.day = 15
                c.add(1.25 * coarse_bias, 'season word as season midpoint')

        # Look back for early/mid/late
        if i > 0:
            pos_day = _position_day_from_word(lower[i - 1])
            if pos_day is not None:
                c.day = pos_day
                c.add(0.75, f'position word {lower[i - 1]}')

        # Look around for an explicit day or year
        nearby = words[max(0, i - 2) : i + 3]

        found_year = False
        found_day = False

        for raw in nearby:
            if re.fullmatch(r'\d{4}', raw):
                y = int(raw)
                if 1800 <= y <= today.year:
                    c.year = y
                    found_year = True
                    c.add(2.0 * coarse_bias, 'nearby 4-digit year')
                    break

        if not found_year:
            for raw in nearby:
                m_apos = re.fullmatch(r"'(\d{2})", raw)
                if m_apos:
                    yy = int(m_apos.group(1))
                    c.year = _expand_two_digit_year(yy, today=today)
                    found_year = True
                    c.add(1.5 * coarse_bias, 'nearby apostrophe year')
                    break

        if not found_year:
            for raw in nearby:
                if re.fullmatch(r'\d{2}', raw):
                    val = int(raw)
                    # If day already implied by early/mid/late, prefer as year
                    if c.day is not None:
                        c.year = _expand_two_digit_year(val, today=today)
                        found_year = True
                        c.add(1.0, '2-digit year chosen because day already known')
                        break

        if not found_day:
            for raw in nearby:
                if re.fullmatch(r'\d{1,2}', raw):
                    val = int(raw)
                    if 1 <= val <= 31 and c.day is None:
                        c.day = val
                        found_day = True
                        c.add(1.0 * fine_bias, 'nearby day-of-month')
                        break

        # Fill default day if month is known but day is missing.
        # Year-filling is deferred to _finalise_candidate() which has
        # access to year_hints from parent path components and can apply
        # a stronger signal than the "assume current year" fallback.
        if c.day is None and c.month is not None:
            c.day = policy.default_day
            c.add(0.25, 'default day of month')

        # Allow year=None — _finalise_candidate will fill it later
        if c.month is not None and c.day is not None and (c.year is None or _safe_date(c.year, c.month, c.day)):
            out.append(c)

    return out


def _extract_compact_candidates_from_words(
    words: Sequence[str],
    *,
    today: _dt.date,
    leaf_bias: float,
) -> list[_Candidate]:
    """Extract candidates from compact date/datetime tokens in word list."""
    out: list[_Candidate] = []
    for w in words:
        c = _candidate_from_compact_datetime(w, today=today, leaf_bias=leaf_bias)
        if c is not None:
            out.append(c)
    return out


def _extract_time_from_leaf(words: Sequence[str], raw_leaf: str) -> tuple[int, int, int] | None:
    """Extract time from the leaf (filename) component.

    Tries compact tokens first (HHMM/HHMMSS), then separated patterns
    (HH:MM:SS, HH.MM.SS, HH_MM_SS) for apps like WhatsApp that use
    separators in timestamps.
    """
    # Compact time tokens — skip digit runs that were originally glued to
    # letters (e.g. "DSC0042" → word "0042" is a camera sequence number,
    # not a time).  We detect this by checking whether the digit token
    # appears directly after a letter in the raw leaf string.
    for w in words:
        if re.fullmatch(r'\d{4}', w):
            val = int(w)
            # If it looks like a plausible year, skip
            if 1800 <= val <= 2099:
                continue
        if w.isdigit() and re.search(r'[A-Za-z]' + re.escape(w), raw_leaf):
            continue
        t = _parse_hhmm_or_hhmmss(w)
        if t is not None:
            return t

    # Separated time patterns: HH:MM:SS, HH.MM.SS, HH_MM_SS
    for match in re.finditer(r'(\d{2})[.:_](\d{2})(?:[.:_](\d{2}))?', raw_leaf):
        hh, mm = int(match.group(1)), int(match.group(2))
        ss = int(match.group(3)) if match.group(3) else 0
        if _validate_time(hh, mm, ss):
            return (hh, mm, ss)

    return None


# ---------------------------------------------------------------------------
# Scoring parser — resolution and scoring
# ---------------------------------------------------------------------------


def _finalise_candidate(
    cand: _Candidate,
    *,
    component_index: int,
    year_hints: Sequence[tuple[int, int, float]],
    total_components: int,
    today: _dt.date,
    policy: _ParsePolicy,
    leaf_time: tuple[int, int, int] | None,
) -> _Candidate | None:
    """Fill gaps in a candidate (year from parent, default day) and validate."""
    c = cand.clone()

    # Fill missing year from nearest parent year
    if c.year is None:
        y, y_score = _nearest_parent_year(component_index, year_hints)
        if y is not None:
            c.year = y
            c.add(1.0 + (0.15 * y_score), 'filled year from parent path')
        elif policy.assume_current_year:
            c.year = today.year
            c.add(0.25, 'assumed current year')

    # Fill missing day if month exists
    if c.month is not None and c.day is None:
        c.day = policy.default_day
        c.add(0.25, 'default day of month')

    # If still incomplete, reject
    if c.year is None or c.month is None or c.day is None:
        return None

    # Apply leaf time if candidate is date-only
    if leaf_time is not None and c.hour == 0 and c.minute == 0 and c.second == 0:
        hh, mm, ss = leaf_time
        c.hour, c.minute, c.second = hh, mm, ss
        c.add(0.5 * _leaf_weight(component_index, total_components), 'time from leaf')

    parsed = _safe_datetime(c.year, c.month, c.day, c.hour, c.minute, c.second)
    if parsed is None:
        return None

    if policy.forbid_future_dates:
        now = _dt.datetime.combine(today, _dt.time.max)
        if parsed > now:
            return None

    return c


def _choose_best(
    candidates: list[tuple[int, _Candidate]],
    *,
    year_hints: Sequence[tuple[int, int, float]],
    total_components: int,
    today: _dt.date,
    policy: _ParsePolicy,
    leaf_time: tuple[int, int, int] | None,
) -> _Candidate | None:
    """Pick the highest-scoring candidate that passes finalisation and min_score."""
    best: _Candidate | None = None

    for component_index, cand in candidates:
        final = _finalise_candidate(
            cand,
            component_index=component_index,
            year_hints=year_hints,
            total_components=total_components,
            today=today,
            policy=policy,
            leaf_time=leaf_time,
        )
        if final is None:
            continue
        if final.score < policy.min_score:
            continue
        if best is None or final.score > best.score:
            best = final

    return best


# ---------------------------------------------------------------------------
# Scoring parser — internal entry point
# ---------------------------------------------------------------------------


def _parse_timestamp_scoring(
    path: Path | str,
    date_order: str = 'DMY',
) -> tuple[datetime | None, float, list[str]]:
    """Parse a photo timestamp from a path using the scoring model.

    Args:
        path: File path to parse.
        date_order: Preferred date order for ambiguous dates ('DMY', 'MDY', 'YMD').

    Returns:
        Tuple of (datetime or None, score, list of assumption strings).
    """
    today = _dt.date.today()
    policy = _ParsePolicy(date_order=date_order)

    # Normalise path separators so that Windows backslash paths are split
    # into components consistently regardless of host OS.  Without this,
    # PosixPath treats 'C:\foo\bar\file.jpg' as a single component while
    # WindowsPath correctly splits it into ['C:\\', 'foo', 'bar', 'file.jpg'].
    path_str = str(path).replace('\\', '/')
    p = Path(path_str)

    components = [part for part in p.parts if part not in ('', '/', '\\')]
    if not components:
        return None, 0.0, []

    total = len(components)
    year_hints = _extract_year_hints(components, today=today)

    # Extract leaf time once — used as a weak augmenting signal
    leaf_words = _split_words(components[-1])
    leaf_time = _extract_time_from_leaf(leaf_words, components[-1])

    raw_candidates: list[tuple[int, _Candidate]] = []

    for i, comp in enumerate(components):
        words = _split_words(comp)
        coarse_bias = _component_weight(i, total)
        fine_bias = _leaf_weight(i, total)

        # 1. Compact date/datetime tokens
        for cand in _extract_compact_candidates_from_words(
            words,
            today=today,
            leaf_bias=fine_bias,
        ):
            raw_candidates.append((i, cand))

        # 2. Delimited numeric triplets
        for cand in _extract_delimited_numeric_triplets(
            comp,
            policy=policy,
            today=today,
            coarse_bias=coarse_bias,
            fine_bias=fine_bias,
        ):
            raw_candidates.append((i, cand))

        # 3. Month words / seasons
        for cand in _extract_month_word_candidates(
            words,
            component_index=i,
            total_components=total,
            policy=policy,
            today=today,
        ):
            raw_candidates.append((i, cand))

    # 4. Cross-directory numeric date merging — detect consecutive pure-numeric
    #    path components that form a date (e.g. /2024/03/07/).  We try runs of
    #    2 or 3 adjacent numeric-only components.
    for start in range(total):
        w0 = _split_words(components[start])
        if len(w0) != 1 or not re.fullmatch(r'\d{2,4}', w0[0]):
            continue
        vals: list[tuple[int, int]] = [(int(w0[0]), len(w0[0]))]

        for offset in range(1, min(3, total - start)):
            w = _split_words(components[start + offset])
            if len(w) != 1 or not re.fullmatch(r'\d{1,4}', w[0]):
                break
            vals.append((int(w[0]), len(w[0])))

            if len(vals) == 3:
                # Three consecutive: try as numeric triplet (YYYY/MM/DD, etc.)
                coarse_bias = _component_weight(start, total)
                fine_bias = _leaf_weight(start + offset, total)
                for cand in _numeric_triplet_candidates(
                    vals[0][0],
                    vals[1][0],
                    vals[2][0],
                    len_a=vals[0][1],
                    len_b=vals[1][1],
                    len_c=vals[2][1],
                    policy=policy,
                    today=today,
                    coarse_bias=coarse_bias,
                    fine_bias=fine_bias,
                ):
                    # Slight penalty for being split across directories
                    cand.add(-0.5, 'date split across path components')
                    raw_candidates.append((start + offset, cand))

            elif len(vals) == 2:
                # Two consecutive: try as YYYY + MM
                y_val, y_len = vals[0]
                m_val, m_len = vals[1]
                if y_len == 4 and 1800 <= y_val <= today.year and 1 <= m_val <= 12 and m_len <= 2:
                    cand = _Candidate(year=y_val, month=m_val, day=1)
                    coarse_bias = _component_weight(start, total)
                    cand.add(3.5 * coarse_bias, 'year/month path components')
                    cand.add(-0.5, 'default day of month')
                    raw_candidates.append((start + 1, cand))

    # 5. Year-only fallback — if no date candidates were generated but we
    #    found year hints, create a low-confidence Jan 1st candidate.
    if not raw_candidates and year_hints:
        _hint_idx, best_year, _best_yscore = max(year_hints, key=lambda h: h[2])
        cand = _Candidate(year=best_year, month=1, day=1)
        cand.add(3.0, 'year-only fallback')
        raw_candidates.append((_hint_idx, cand))

    best = _choose_best(
        raw_candidates,
        year_hints=year_hints,
        total_components=total,
        today=today,
        policy=policy,
        leaf_time=leaf_time,
    )

    if best is None:
        return None, 0.0, []

    result = datetime(
        best.year,
        best.month,
        best.day,  # type: ignore[arg-type]
        best.hour,
        best.minute,
        best.second,
    )
    # logger.debug(
    #     'Scoring parser: score=%.1f assumptions=%s for %s',
    #     best.score,
    #     best.assumptions,
    #     path,
    # )
    return result, best.score, best.assumptions


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
                result = parse_exif_datetime(exif_dict['DateTimeOriginal'])
                if result:
                    return result

            # Fall back to DateTime (when file was modified)
            if 'DateTime' in exif_dict:
                result = parse_exif_datetime(exif_dict['DateTime'])
                if result:
                    return result

    except (OSError, AttributeError, KeyError):
        # logger.debug(f'Failed to extract EXIF from {path}: {e}')
        pass

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
            except Exception as e:
                logger.debug(f'Failed to read EXIF sub-IFD from {path}: {e}')

            # GPS IFD
            gps_info = None
            try:
                gps_ifd = exif.get_ifd(IFD.GPSInfo)
                if gps_ifd:
                    gps_info = gps_ifd
            except Exception as e:
                logger.debug(f'Failed to read GPS IFD from {path}: {e}')

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
            parsed = parse_exif_datetime(str(dt_val))
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
                    result['Shutter Speed'] = f'1/{round(1 / et)}s'
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
            parsed = parse_exif_datetime(str(val))
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
    # Skip numbers that are part of resolution patterns (e.g. 1920_1080).
    resolution_nums = _extract_resolution_numbers(text)
    for match in _PATTERN_DATE_YEAR_ONLY.finditer(text):
        year = int(match.group(1))
        if 1900 <= year <= 2099 and match.group(1) not in resolution_nums:
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
        # Skip digit runs directly preceded by a letter — these are typically
        # camera sequence numbers like DSC004283, not timestamps
        if match.start() > 0 and search_text[match.start() - 1].isalpha():
            continue
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
        # Skip digit runs directly preceded by a letter (camera sequence numbers)
        if match.start() > 0 and search_text[match.start() - 1].isalpha():
            continue
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
CONFIDENCE_EXIF = 1  # From EXIF metadata
CONFIDENCE_FILENAME = 2  # Parsed from filename/path
CONFIDENCE_FILESYSTEM = 3  # From filesystem metadata
CONFIDENCE_UNKNOWN = 4  # None/unknown


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
    filename_date_overrides: list[str] | None = None,
    date_order: str = 'DMY',
) -> tuple[datetime | None, int]:
    """Derive the best timestamp for an image with confidence level.

    Tries sources in priority order:
    1. EXIF DateTimeOriginal tag (confidence 1)
    2. EXIF DateTime tag (confidence 1)
    3. Parsed from filename/path (confidence 2)
    4. Filesystem creation/modification time (confidence 3)

    When ``filename_date_overrides`` patterns are provided and the filename
    matches one of them, the filename-derived timestamp takes priority over
    EXIF. This handles apps like WhatsApp that rewrite EXIF dates to the
    download time while encoding the actual capture time in the filename.

    When ``exif_data`` is provided (pre-read via ``extract_exif_data()``),
    the timestamp is extracted from it without re-opening the file. This
    avoids double I/O during indexing.

    Args:
        path: Path to the image file.
        exif_data: Optional pre-extracted EXIF key-value pairs from
            ``extract_exif_data()``. When provided, timestamp is derived
            from the 'Date Taken' key instead of re-reading the file.
        filename_date_overrides: Optional list of glob patterns. When the
            basename matches any pattern, the filename-derived timestamp
            is preferred over EXIF.
        date_order: Preferred date order for ambiguous numeric dates in
            filenames ('DMY', 'MDY', or 'YMD').

    Returns:
        Tuple of (datetime, confidence) where confidence is:
        - 0: user assigned (not returned by this function)
        - 1: from EXIF
        - 2: from filename
        - 3: from filesystem
        - 4: none/unknown
    """
    path = Path(path)

    # Check if filename matches an override pattern — if so, try filename first
    if filename_date_overrides:
        basename = path.name
        for pattern in filename_date_overrides:
            if fnmatch.fnmatch(basename, pattern):
                # Try scoring parser first, then legacy fallback
                timestamp, _score, _assumptions = _parse_timestamp_scoring(path, date_order)
                if not timestamp:
                    timestamp = parse_timestamp_from_path(path)
                if timestamp:
                    # logger.debug(f'Timestamp from filename (override match "{pattern}"): {timestamp} for {path}')
                    return (timestamp, CONFIDENCE_FILENAME)
                # Pattern matched but filename parsing failed — fall through to EXIF
                break

    # Try EXIF first — reuse pre-read data when available to avoid double I/O
    if exif_data is not None:
        date_taken = exif_data.get('Date Taken')
        if date_taken:
            timestamp = parse_exif_datetime(date_taken)
            if timestamp:
                # logger.debug(f'Timestamp from pre-read EXIF: {timestamp} for {path}')
                return (timestamp, CONFIDENCE_EXIF)
    else:
        # Fall back to opening the file for EXIF (backward compat)
        timestamp = extract_exif_timestamp(path)
        if timestamp:
            # logger.debug(f'Timestamp from EXIF: {timestamp} for {path}')
            return (timestamp, CONFIDENCE_EXIF)

    # Try parsing from filename/path (before filesystem, as files get copied around)
    # Scoring parser first — handles month words, seasons, DMY/MDY ambiguity
    timestamp, _score, _assumptions = _parse_timestamp_scoring(path, date_order)
    if timestamp:
        # logger.debug(f'Timestamp from scoring parser (score={score:.1f}): {timestamp} for {path}')
        return (timestamp, CONFIDENCE_FILENAME)

    # Legacy regex-cascade fallback
    timestamp = parse_timestamp_from_path(path)
    if timestamp:
        # logger.debug(f'Timestamp from legacy parser: {timestamp} for {path}')
        return (timestamp, CONFIDENCE_FILENAME)

    # Try filesystem timestamp as last resort
    timestamp = extract_filesystem_timestamp(path)
    if timestamp:
        # logger.debug(f'Timestamp from filesystem: {timestamp} for {path}')
        return (timestamp, CONFIDENCE_FILESYSTEM)

    logger.debug(f'No timestamp found for {path}')
    return (None, CONFIDENCE_UNKNOWN)


# =============================================================================
# CLI TEST HARNESS
# =============================================================================

if __name__ == '__main__':
    import argparse
    import sys

    # ANSI colour helpers (disabled when not a TTY)
    _USE_COLOUR = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

    def _green(s: str) -> str:
        return f'\033[32m{s}\033[0m' if _USE_COLOUR else s

    def _red(s: str) -> str:
        return f'\033[31m{s}\033[0m' if _USE_COLOUR else s

    def _cyan(s: str) -> str:
        return f'\033[36m{s}\033[0m' if _USE_COLOUR else s

    def _dim(s: str) -> str:
        return f'\033[2m{s}\033[0m' if _USE_COLOUR else s

    def _bold(s: str) -> str:
        return f'\033[1m{s}\033[0m' if _USE_COLOUR else s

    # ------------------------------------------------------------------
    # Test case table
    # ------------------------------------------------------------------
    # Each entry: (path, date_order, expected_date_str_or_None,
    #              expected_time_str_or_None, which_parsers, description)
    #
    # expected_date_str: 'YYYY-MM-DD' or None (no date expected)
    # expected_time_str: 'HH:MM:SS' or None (midnight / don't care)
    # which_parsers: 'both' | 'scoring' | 'legacy' — which parser(s) should match
    TEST_CASES: list[tuple[str, str, str | None, str | None, str, str]] = [
        # -- Compact dates --
        ('IMG_20240307.jpg', 'DMY', '2024-03-07', None, 'both', 'YYYYMMDD compact'),
        ('photo_20240307_1430.jpg', 'DMY', '2024-03-07', '14:30:00', 'both', 'YYYYMMDD_HHMM'),
        ('VID_20240307_143045.jpg', 'DMY', '2024-03-07', '14:30:45', 'both', 'YYYYMMDD_HHMMSS'),
        ('photo_240307.jpg', 'DMY', '2024-03-07', None, 'scoring', 'YYMMDD compact'),
        ('album_202403.jpg', 'DMY', '2024-03-01', None, 'scoring', 'YYYYMM compact'),
        ('album_2024.jpg', 'DMY', '2024-01-01', None, 'scoring', 'YYYY only'),
        # -- Separated dates --
        ('photo_2024-03-07.jpg', 'DMY', '2024-03-07', None, 'both', 'YYYY-MM-DD separated'),
        ('photo_2024.03.07.jpg', 'DMY', '2024-03-07', None, 'both', 'YYYY.MM.DD separated'),
        ('photo_2024_03_07.jpg', 'DMY', '2024-03-07', None, 'both', 'YYYY_MM_DD separated'),
        ('/photos/2024/03/07/pic.jpg', 'DMY', '2024-03-07', None, 'both', 'YYYY/MM/DD in path'),
        # -- DMY/MDY ambiguity --
        ('photo_07-03-2024.jpg', 'DMY', '2024-03-07', None, 'scoring', 'DD-MM-YYYY (DMY order)'),
        ('photo_07-03-2024.jpg', 'MDY', '2024-07-03', None, 'scoring', 'MM-DD-YYYY (MDY order)'),
        ('photo_03-07-2024.jpg', 'MDY', '2024-03-07', None, 'scoring', 'MM-DD-YYYY (MDY, Mar 7)'),
        # -- Month words --
        ('/Photos/May 2023/pic.jpg', 'DMY', '2023-05-01', None, 'scoring', 'Month Year folder'),
        ('/Photos/January 15 2024/pic.jpg', 'DMY', '2024-01-15', None, 'scoring', 'Month Day Year folder'),
        ('/Photos/15 March 2024/pic.jpg', 'DMY', '2024-03-15', None, 'scoring', 'Day Month Year folder'),
        ('/Photos/early June/pic.jpg', 'DMY', None, None, 'scoring', '"early June" — no year, no match'),
        ('/Photos/2023/early June/pic.jpg', 'DMY', '2023-06-01', None, 'scoring', '"early June" with year hint'),
        # -- Seasons / holidays --
        ('/Photos/Summer 2006/pic.jpg', 'DMY', '2006-06-01', None, 'scoring', 'Season word (June=start)'),
        ('/Photos/Christmas 2023/pic.jpg', 'DMY', '2023-12-25', None, 'scoring', 'Holiday word'),
        ('/Photos/Halloween 2020/pic.jpg', 'DMY', '2020-10-31', None, 'scoring', 'Holiday word'),
        # -- Path hierarchy dates --
        ('/Photos/2024/March/IMG_001.jpg', 'DMY', '2024-03-01', None, 'scoring', 'Year/Month path hierarchy'),
        ('/2023/06/photo.jpg', 'DMY', '2023-06-01', None, 'both', 'Year/Month numeric path'),
        # -- WhatsApp-style --
        ('WhatsApp Image 2024-03-07 at 14.30.45.jpg', 'DMY', '2024-03-07', '14:30:45', 'both', 'WhatsApp date+time'),
        # -- Camera-style --
        ('DSC_20240307_143045.jpg', 'DMY', '2024-03-07', '14:30:45', 'both', 'Camera prefix YYYYMMDD_HHMMSS'),
        ('IMG_20240307.jpg', 'DMY', '2024-03-07', None, 'both', 'Camera prefix YYYYMMDD'),
        # -- Resolution / technical metadata in filenames --
        ('cars-hd_1920_1080_25fps.mp4', 'DMY', None, None, 'both', 'Resolution 1920x1080 not a date'),
        ('flowers-hd_1080_1920_30fps.mp4', 'DMY', None, None, 'both', 'Resolution 1080x1920 (portrait) not a date'),
        ('apollo2-sd_960_540_30fps.mp4', 'DMY', None, None, 'both', 'Resolution 960x540 not a date'),
        # -- Date + time in filename (dot-delimited time after date) --
        # The HH.MM.SS portion must not be parsed as a DMY date.  These
        # tests use forward slashes so path splitting works on all platforms.
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2025/2025-07-13 14.02.30.mp4',
            'DMY',
            '2025-07-13',
            '14:02:30',
            'scoring',
            'Date + dot time (14.02.30 not a date)',
        ),
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2024/2024-05-17 16.08.33.jpg',
            'DMY',
            '2024-05-17',
            '16:08:33',
            'scoring',
            'Date + dot time (16.08.33 not a date)',
        ),
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2018/2018-12-14 17.11.33.mp4',
            'DMY',
            '2018-12-14',
            '17:11:33',
            'scoring',
            'Date + dot time (17.11.33 not a date)',
        ),
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2018/2018-01-06 11.10.40.mp4',
            'DMY',
            '2018-01-06',
            '11:10:40',
            'scoring',
            'Date + dot time (11.10.40 not a date)',
        ),
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2018/2018-08-04 21.01.52.mp4',
            'DMY',
            '2018-08-04',
            '21:01:52',
            'scoring',
            'Date + dot time (21.01.52 not a date)',
        ),
        # Time where middle value > 12 — already worked (can't be a month)
        (
            'C:/Users/srevi/Dropbox/Photos and Videos/Videos/2018/2018-02-10 10.36.08.mp4',
            'DMY',
            '2018-02-10',
            '10:36:08',
            'scoring',
            'Date + dot time (10.36.08 — minute > 12)',
        ),
        # Backslash variant — must also work (normalised internally)
        (
            r'C:\Users\srevi\Dropbox\Photos and Videos\Videos\2025\2025-07-13 14.02.30.mp4',
            'DMY',
            '2025-07-13',
            '14:02:30',
            'scoring',
            'Backslash path with date + dot time',
        ),
        # -- Edge cases --
        ('random_photo.jpg', 'DMY', None, None, 'both', 'No date at all'),
        ('photo_99991231.jpg', 'DMY', None, None, 'both', 'Future date (should be rejected)'),
        ('/Photos/2024/pic.jpg', 'DMY', '2024-01-01', None, 'scoring', 'Year-only directory'),
    ]

    # ------------------------------------------------------------------
    # Test runner
    # ------------------------------------------------------------------

    def _run_tests(date_order_override: str | None = None) -> bool:
        """Run built-in test suite. Returns True if all tests pass."""
        passed = 0
        failed = 0
        errors: list[str] = []

        skipped = 0
        for path, case_order, exp_date, exp_time, which, desc in TEST_CASES:
            order = date_order_override if date_order_override else case_order

            # Skip date-order-sensitive tests when the override conflicts
            # with the case's expected output (e.g. a DMY-specific test
            # would give a different result under MDY).
            if date_order_override and date_order_override != case_order:
                skipped += 1
                continue

            # Build expected date/datetime
            if exp_date is None:
                expected_date = None
                expected_dt = None
            else:
                parts = [int(x) for x in exp_date.split('-')]
                expected_date = _dt.date(parts[0], parts[1], parts[2])
                if exp_time:
                    tparts = [int(x) for x in exp_time.split(':')]
                    expected_dt = datetime(parts[0], parts[1], parts[2], tparts[0], tparts[1], tparts[2])
                else:
                    expected_dt = None  # date-only check

            # When exp_time is None, compare date portion only (time may
            # vary due to leaf-time extraction from the same token).
            check_time = exp_time is not None

            # Run scoring parser
            scoring_dt, score, assumptions = _parse_timestamp_scoring(path, order)

            # Run legacy parser (date_order not supported)
            legacy_dt = parse_timestamp_from_path(path)

            # Check results based on which parser(s) should match
            ok = True
            detail_parts: list[str] = []

            for label, actual in [('scoring', scoring_dt), ('legacy', legacy_dt)]:
                if label == 'scoring' and which not in ('both', 'scoring'):
                    continue
                if label == 'legacy' and which not in ('both', 'legacy'):
                    continue

                if expected_date is None:
                    if actual is not None:
                        ok = False
                        detail_parts.append(f'{label}: expected None, got {actual}')
                elif actual is None:
                    ok = False
                    detail_parts.append(f'{label}: expected {expected_date}, got None')
                elif check_time:
                    if actual != expected_dt:
                        ok = False
                        detail_parts.append(f'{label}: expected {expected_dt}, got {actual}')
                elif actual.date() != expected_date:
                    ok = False
                    detail_parts.append(f'{label}: expected date {expected_date}, got {actual}')

            # Format result line
            status = _green('PASS') if ok else _red('FAIL')
            score_str = f' score={score:.1f}' if scoring_dt else ''
            assume_str = f' [{", ".join(assumptions)}]' if assumptions else ''
            line = f'  {status}  {desc}'
            if scoring_dt:
                line += _dim(f'  →{scoring_dt}{score_str}{assume_str}')

            print(line)

            if ok:
                passed += 1
            else:
                failed += 1
                for d in detail_parts:
                    print(f'         {_red(d)}')
                errors.append(f'{desc}: {"; ".join(detail_parts)}')

        # Summary
        print()
        total = passed + failed
        summary = f'{passed}/{total} passed'
        if skipped:
            summary += f', {skipped} skipped (date_order mismatch)'
        if failed:
            print(_red(_bold(f'FAILED: {summary}')))
        else:
            print(_green(_bold(f'ALL PASSED: {summary}')))

        return failed == 0

    # ------------------------------------------------------------------
    # File/path inspection
    # ------------------------------------------------------------------

    def _inspect_path(path_str: str, date_order: str = 'DMY') -> None:
        """Inspect a file path or synthetic path string."""
        p = Path(path_str)
        is_real = p.exists()

        print(_bold(f'Path: {path_str}'))
        if is_real:
            print(f'  File exists: yes ({p.stat().st_size:,} bytes)')
        else:
            print('  File exists: no (treating as synthetic path)')
        print()

        # Scoring parser
        scoring_dt, score, assumptions = _parse_timestamp_scoring(path_str, date_order)
        print(_bold('Scoring parser:'))
        if scoring_dt:
            print(f'  Result:      {_cyan(str(scoring_dt))}')
            print(f'  Score:       {score:.1f}')
            if assumptions:
                print(f'  Assumptions: {", ".join(assumptions)}')
        else:
            print(f'  Result:      {_dim("(no match)")}')
        print()

        # Legacy parser
        legacy_dt = parse_timestamp_from_path(path_str)
        print(_bold('Legacy parser:'))
        if legacy_dt:
            print(f'  Result:      {_cyan(str(legacy_dt))}')
        else:
            print(f'  Result:      {_dim("(no match)")}')
        print()

        # derive_timestamp_with_confidence (only meaningful for real files
        # or paths — it will try EXIF + filesystem too)
        confidence_names = {
            CONFIDENCE_EXIF: 'EXIF',
            CONFIDENCE_FILENAME: 'filename',
            CONFIDENCE_FILESYSTEM: 'filesystem',
            CONFIDENCE_UNKNOWN: 'unknown',
        }

        if is_real:
            # Extract EXIF for real files
            exif_data = extract_exif_data(str(p))
            ts, conf = derive_timestamp_with_confidence(path_str, exif_data=exif_data, date_order=date_order)

            print(_bold('derive_timestamp_with_confidence:'))
            print(f'  Timestamp:   {_cyan(str(ts)) if ts else _dim("None")}')
            print(f'  Confidence:  {conf} ({confidence_names.get(conf, "?")})')
            print()

            # Show relevant EXIF dates
            if exif_data:
                date_keys = [k for k in exif_data if 'date' in k.lower() or 'time' in k.lower()]
                if date_keys:
                    print(_bold('EXIF date fields:'))
                    for k in sorted(date_keys):
                        print(f'  {k}: {exif_data[k]}')
        else:
            # For synthetic paths, just run without EXIF
            ts, conf = derive_timestamp_with_confidence(path_str, date_order=date_order)
            print(_bold('derive_timestamp_with_confidence:'))
            print(f'  Timestamp:   {_cyan(str(ts)) if ts else _dim("None")}')
            print(f'  Confidence:  {conf} ({confidence_names.get(conf, "?")})')

    # ------------------------------------------------------------------
    # CLI entry point
    # ------------------------------------------------------------------

    parser = argparse.ArgumentParser(
        description='Test harness for metadata.py filename date parsing.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python metadata.py --test                          Run built-in test suite
  python metadata.py --date-order MDY --test         Test with MDY date order
  python metadata.py /Photos/2024/March/IMG_001.jpg  Inspect a synthetic path
  python metadata.py ../tools/mktutorial/examples/some_image.jpg  Inspect real file
""",
    )
    parser.add_argument(
        'path',
        nargs='?',
        help='File path (real or synthetic) to inspect.  Runs both parsers and shows results.',
    )
    parser.add_argument(
        '--test',
        '-t',
        action='store_true',
        help='Run the built-in test suite.',
    )
    parser.add_argument(
        '--date-order',
        '-d',
        choices=['DMY', 'MDY', 'YMD'],
        default=None,
        help='Date order for ambiguous numeric dates (default: per-case or DMY).',
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    if not args.test and not args.path:
        parser.print_help()
        sys.exit(1)

    if args.test:
        print(_bold(f'Running metadata filename parser tests (date_order={args.date_order or "per-case"})'))
        print()
        ok = _run_tests(date_order_override=args.date_order)
        sys.exit(0 if ok else 1)

    if args.path:
        _inspect_path(args.path, date_order=args.date_order or 'DMY')
