"""Eastern-Time clock — the ONE place time is formatted for the whole project.

Every stored and displayed timestamp uses US Eastern Time (America/New_York)
in the format ``YYYY-MM-DD HH:MM:SS``. This module is dependency-free (no tzdata
needed on Lambda or in the container): it computes the US Eastern UTC offset with
the statutory DST rule — EDT (UTC-4) from the 2nd Sunday of March 02:00 to the
1st Sunday of November 02:00, otherwise EST (UTC-5).

NOTE: bff/clock.py is a byte-for-byte copy of this file (the BFF Lambda is a
separate deployment artifact and cannot import app.common). Keep them in sync.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"


def _et_offset_hours(dt_utc: datetime) -> int:
    """US Eastern offset (in hours) for a given UTC instant: -4 (EDT) or -5 (EST)."""
    y = dt_utc.year
    # 2nd Sunday of March at 02:00 EST == 07:00 UTC.
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    mar += timedelta(days=(6 - mar.weekday()) % 7)
    # 1st Sunday of November at 02:00 EDT == 06:00 UTC.
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    nov += timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if mar <= dt_utc < nov else -5


def _to_et(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc + timedelta(hours=_et_offset_hours(dt_utc))


def now_et() -> datetime:
    """Current Eastern wall-clock time as a naive datetime (tz stripped)."""
    return _to_et(datetime.now(timezone.utc)).replace(tzinfo=None)


def now_str() -> str:
    """Current Eastern time as 'YYYY-MM-DD HH:MM:SS'."""
    return now_et().strftime(FMT)


def today_str() -> str:
    """Current Eastern date as 'YYYY-MM-DD'."""
    return now_et().strftime(DATE_FMT)
