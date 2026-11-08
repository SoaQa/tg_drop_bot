from __future__ import annotations

import csv
from io import StringIO

from tg_drop_bot.db.models import Participant
from tg_drop_bot.services.rendering import membership_status_label

WEIGHT_LABELS = {
    "newcomer": "Подписался после начала",
    "fast_captcha": "Быстро ответил на капчу",
    "newcomer_fast_captcha": "Подписался после начала и быстро ответил на капчу",
}


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
            "вес_заявки",
            "основание_веса",
            "допуск_к_выбору",
            "причина_исключения",
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
                participant.selection_weight if participant.selection_weight is not None else 1,
                WEIGHT_LABELS.get(participant.selection_reason or "", "Равные условия"),
                "Исключён" if participant.draw_excluded else "Допущен",
                participant.exclusion_reason or "",
            ]
        )
    return buffer.getvalue().encode("utf-8-sig")
