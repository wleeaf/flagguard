import re
from datetime import datetime, timedelta, timezone

from config import APP_TIMEZONE_OFFSET

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _parse_offset(offset: str) -> timezone:
    m = _OFFSET_RE.fullmatch(offset.strip())
    if not m:
        raise RuntimeError(
            "Invalid APP_TIMEZONE_OFFSET format. Use +HH:MM or -HH:MM (example: +03:00)."
        )

    sign, hh, mm = m.groups()
    hours = int(hh)
    minutes = int(mm)

    if hours > 14 or minutes > 59 or (hours == 14 and minutes != 0):
        raise RuntimeError(
            "Invalid APP_TIMEZONE_OFFSET value. Hours must be 00..14 and minutes 00..59 "
            "(+14:00 is the maximum valid offset)."
        )

    delta = timedelta(hours=hours, minutes=minutes)
    if sign == "-":
        delta = -delta
    return timezone(delta)


APP_TIMEZONE = _parse_offset(APP_TIMEZONE_OFFSET)


def now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(APP_TIMEZONE)


def db_now() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def db_ago(
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    seconds: int = 0,
) -> str:
    dt = now_local() - timedelta(
        days=days, hours=hours, minutes=minutes, seconds=seconds
    )
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_db_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=APP_TIMEZONE)
    return dt.astimezone(APP_TIMEZONE)
