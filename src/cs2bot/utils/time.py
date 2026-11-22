"""Time helpers. Store and compute in UTC; convert to Europe/Warsaw only for display / day boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dateutil_parser

WARSAW = ZoneInfo("Europe/Warsaw")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes (assumed UTC) and convert aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_datetime(value: str | int | float | datetime | None) -> datetime | None:
    """Parse ISO strings / unix timestamps (s or ms) into aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    return ensure_utc(dateutil_parser.isoparse(value) if "T" in value else dateutil_parser.parse(value))


def to_local(dt: datetime, tz: str | ZoneInfo = WARSAW) -> datetime:
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ensure_utc(dt).astimezone(zone)


def local_day_start(now: datetime | None = None, tz: str | ZoneInfo = WARSAW) -> datetime:
    """UTC instant of the most recent local midnight (loss limits reset at local midnight)."""
    local = to_local(now or utcnow(), tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC)


def local_week_start(now: datetime | None = None, tz: str | ZoneInfo = WARSAW) -> datetime:
    """UTC instant of the most recent local Monday 00:00."""
    local = to_local(now or utcnow(), tz)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(UTC)
