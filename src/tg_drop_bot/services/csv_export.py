from __future__ import annotations

import csv
from io import StringIO

from tg_drop_bot.db.models import Participant
from tg_drop_bot.services.rendering import membership_status_label


def participants_to_csv(participants: list[Participant]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "telegram_id",
            "имя_пользователя",
            "имя",
            "фамилия",
            "время_участия",
            "капча_пройдена",
            "статус_членства",
            "членство_проверено",
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
                membership_status_label(participant.membership_status),
                participant.membership_checked_at.isoformat()
                if participant.membership_checked_at
                else "",
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
