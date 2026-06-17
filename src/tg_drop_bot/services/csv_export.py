from __future__ import annotations

import csv
from io import StringIO

from tg_drop_bot.db.models import Participant


def participants_to_csv(participants: list[Participant]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "telegram_user_id",
            "username",
            "first_name",
            "last_name",
            "joined_at",
            "captcha_passed_at",
            "membership_status",
            "membership_checked_at",
        ]
    )
    for participant in participants:
        writer.writerow(
            [
                participant.user_id,
                participant.username or "",
                participant.first_name or "",
                participant.last_name or "",
                participant.joined_at.isoformat(),
                participant.captcha_passed_at.isoformat(),
                participant.membership_status,
                participant.membership_checked_at.isoformat()
                if participant.membership_checked_at
                else "",
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
