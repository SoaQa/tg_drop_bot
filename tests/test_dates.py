from datetime import UTC
from zoneinfo import ZoneInfo

import pytest

from tg_drop_bot.services.dates import format_admin_datetime, parse_admin_deadline


def test_parse_admin_deadline_as_timezone_aware_utc() -> None:
    deadline = parse_admin_deadline("25.06.2026 18:30", ZoneInfo("Europe/Moscow"))
    assert deadline.tzinfo == UTC
    assert deadline.hour == 15
    assert deadline.minute == 30


def test_parse_admin_deadline_rejects_bad_value() -> None:
    with pytest.raises(ValueError):
        parse_admin_deadline("06/25/26 18:30", ZoneInfo("Europe/Moscow"))


def test_format_admin_datetime() -> None:
    deadline = parse_admin_deadline("25.06.2026 18:30", ZoneInfo("Europe/Moscow"))
    assert format_admin_datetime(deadline, ZoneInfo("Europe/Moscow")) == "25.06.2026 18:30"
