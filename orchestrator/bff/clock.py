"""Eastern-Time clock for the BFF Lambda — byte-for-byte copy of
app/common/clock.py (the BFF is a separate deployment artifact and cannot import
app.common). Keep the two in sync.

Every stored and displayed timestamp uses US Eastern Time (America/New_York) in
the format ``YYYY-MM-DD HH:MM:SS``. Dependency-free (no tzdata): EDT (UTC-4) from
the 2nd Sunday of March 02:00 to the 1st Sunday of November 02:00, else EST (UTC-5).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"


def _et_offset_hours(dt_utc: datetime) -> int:
    y = dt_utc.year
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    mar += timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    nov += timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if mar <= dt_utc < nov else -5


def _to_et(dt_utc: datetime) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc + timedelta(hours=_et_offset_hours(dt_utc))


def now_et() -> datetime:
    return _to_et(datetime.now(timezone.utc)).replace(tzinfo=None)


def now_str() -> str:
    return now_et().strftime(FMT)


def today_str() -> str:
    return now_et().strftime(DATE_FMT)
