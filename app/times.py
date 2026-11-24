"""UTC time parsing / formatting helpers.

All timestamps stored in SQLite use the canonical form
``YYYY-MM-DDTHH:MM:SSZ`` so that lexicographic ordering equals chronological
ordering.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

HOUR = timedelta(hours=1)
_TZ = timezone.utc

# "2026-05-01" parses as midnight but the data contract always needs an hour.
_DATE_ONLY = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")

_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)

# All-digit stamps are length-ambiguous: strptime would read "2026050105" under
# "%Y%m%d%H%M" as minute 05 rather than hour 05, so length decides.
_COMPACT_FORMATS = {
    10: "%Y%m%d%H",      # YYYYMMDDHH
    12: "%Y%m%d%H%M",    # YYYYMMDDHHMM
    14: "%Y%m%d%H%M%S",  # YYYYMMDDHHMMSS
}


def parse_utc(value: str) -> datetime:
    """Parse a user supplied timestamp into a naive UTC datetime.

    Naive timestamps are interpreted as UTC.  Explicit ``Z`` / ``+00:00`` is
    accepted; any other UTC offset is rejected because the data contract is
    "times are UTC" and silently shifting offsets invites mistakes.
    """
    if value is None:
        raise ValueError("时刻为空")
    text = str(value).strip()
    if not text:
        raise ValueError("时刻为空")
    text = text.replace("/", "-")
    if text.endswith(("Z", "z")):
        text = text[:-1]
    if _DATE_ONLY.match(text):
        raise ValueError(f"时刻缺少小时部分：{value!r}")

    # Explicit formats first: fromisoformat accepts the compact "2026050105"
    # form but reads it as 2026-05-01 00:05 rather than hour 05.
    parsed: datetime | None = None
    formats = list(_FORMATS)
    compact = _COMPACT_FORMATS.get(len(text))
    if compact and text.isdigit():
        formats.insert(0, compact)
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
    if parsed is None:
        raise ValueError(f"无法识别的时刻格式：{value!r}")

    if parsed.tzinfo is not None:
        if parsed.utcoffset() != timedelta(0):
            raise ValueError(f"时刻必须为 UTC：{value!r}")
        parsed = parsed.astimezone(_TZ).replace(tzinfo=None)
    if parsed.microsecond:
        raise ValueError(f"时刻精度最多到秒：{value!r}")
    return parsed


def require_whole_hour(value: str, field: str = "时刻") -> datetime:
    dt = parse_utc(value)
    if dt.minute != 0 or dt.second != 0:
        raise ValueError(f"{field}必须是整点（分、秒为 0）：{value!r}")
    return dt


def parse_hour_input(value: str, field: str = "时刻") -> datetime:
    """Parse the value of an ``<input type="datetime-local" step="3600">``."""
    return require_whole_hour(value, field)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_hour(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00Z")


def parse_stored(value: str) -> datetime:
    return parse_utc(value)


def display(value: str | None) -> str:
    """Canonical DB string -> ``YYYY-MM-DD HH:MMZ`` for rendering."""
    if not value:
        return ""
    try:
        dt = parse_utc(value)
    except ValueError:
        return str(value)
    if dt.second:
        return dt.strftime("%Y-%m-%d %H:%M:%SZ")
    return dt.strftime("%Y-%m-%d %H:%MZ")


def datetime_local_value(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00")


def utc_now() -> datetime:
    """Naive UTC 'now' with second resolution, matching stored strings."""
    return datetime.now(_TZ).replace(tzinfo=None, microsecond=0)
