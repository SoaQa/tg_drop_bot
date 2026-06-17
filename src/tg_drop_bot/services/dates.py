from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ADMIN_DEADLINE_FORMAT = "%d.%m.%Y %H:%M"


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_admin_deadline(value: str, timezone: ZoneInfo) -> datetime:
    parsed = datetime.strptime(value.strip(), ADMIN_DEADLINE_FORMAT).replace(tzinfo=timezone)
    return parsed.astimezone(UTC)


def format_admin_datetime(value: datetime | None, timezone: ZoneInfo) -> str:
    if value is None:
        return "не задано"
    return value.astimezone(timezone).strftime(ADMIN_DEADLINE_FORMAT)
