"""Date/time helpers shared by the tools."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from exchangelib import EWSDateTime, EWSTimeZone

_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
)


def get_timezone(name: str = "") -> EWSTimeZone:
    """Return an EWSTimeZone for an IANA name, or the host's local zone when empty."""
    if not name:
        return EWSTimeZone.localzone()
    factory = getattr(EWSTimeZone, "timezone", None)  # exchangelib < 5
    if factory is not None:
        try:
            return factory(name)
        except Exception:  # noqa: BLE001, S110 - fall through to the 5.x constructor
            pass
    return EWSTimeZone(name)


def parse_datetime(value: str, tz: EWSTimeZone) -> EWSDateTime:
    """Parse ``YYYY-MM-DD HH:MM`` (and a few variants / ISO-8601 with offset) into an aware EWSDateTime."""
    text = (value or "").strip()
    if not text:
        raise ValueError("A date/time value is required (format YYYY-MM-DD HH:MM).")
    parsed: datetime | None = None
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Cannot parse date/time {value!r}; use YYYY-MM-DD HH:MM.") from exc
    if parsed.tzinfo is not None:
        return EWSDateTime.from_datetime(parsed).astimezone(tz)
    return EWSDateTime.from_datetime(parsed.replace(tzinfo=tz))


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Cannot parse date {value!r}; use YYYY-MM-DD.") from exc


def day_bounds(start: date, end: date, tz: EWSTimeZone) -> tuple[EWSDateTime, EWSDateTime]:
    """Return [start 00:00, end+1 00:00) in ``tz``."""
    if end < start:
        raise ValueError("end_date must not be before start_date.")
    lo = EWSDateTime(start.year, start.month, start.day, tzinfo=tz)
    nxt = end + timedelta(days=1)
    hi = EWSDateTime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
    return lo, hi


def now(tz: EWSTimeZone) -> EWSDateTime:
    return EWSDateTime.now(tz)


def fmt(dt, tz: EWSTimeZone | None = None) -> str | None:
    """Format a datetime/date for output; converts aware datetimes into ``tz``."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if tz is not None and dt.tzinfo is not None:
            dt = dt.astimezone(tz)
        return dt.strftime("%Y-%m-%d %H:%M")
    if isinstance(dt, date):
        return dt.isoformat()
    return str(dt)
