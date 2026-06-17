from __future__ import annotations

import html

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway, Participant
from tg_drop_bot.services.dates import format_admin_datetime

GIVEAWAY_STATUS_LABELS = {
    "draft": "черновик",
    "published": "опубликован",
    "finished": "завершен",
    "cancelled": "отменен",
}

MEMBERSHIP_STATUS_LABELS = {
    "creator": "создатель группы",
    "administrator": "администратор",
    "member": "участник",
    "restricted": "ограничен",
    "left": "не состоит в группе",
    "kicked": "исключен",
    "check_failed": "проверка не удалась",
}


def giveaway_status_label(status: str) -> str:
    return GIVEAWAY_STATUS_LABELS.get(status, status)


def membership_status_label(status: str) -> str:
    return MEMBERSHIP_STATUS_LABELS.get(status, status)


def render_giveaway_post(giveaway: Giveaway, settings: Settings, *, closed: bool = False) -> str:
    title = "Розыгрыш завершен" if closed else "Розыгрыш"
    parts = [
        f"<b>{title}</b>",
        html.escape(giveaway.post_text or ""),
    ]
    if giveaway.terms_text:
        parts.extend(["", "<b>Условия участия</b>", html.escape(giveaway.terms_text)])
    parts.extend(
        [
            "",
            f"<b>Победителей:</b> {giveaway.winners_count or 0}",
            f"<b>Завершение:</b> {format_admin_datetime(giveaway.deadline_at, settings.timezone)}",
        ]
    )
    if giveaway.status == "cancelled":
        parts.extend(["", "Розыгрыш отменен."])
    elif closed:
        parts.extend(["", "Участие закрыто."])
    return "\n".join(parts).strip()


def render_giveaway_card(giveaway: Giveaway, settings: Settings, participants_count: int) -> str:
    group_name = giveaway.group.title if giveaway.group else "не выбрана"
    return "\n".join(
        [
            f"<b>Розыгрыш #{giveaway.id}</b>",
            f"Статус: <b>{html.escape(giveaway_status_label(giveaway.status))}</b>",
            f"Группа: {html.escape(group_name)}",
            f"Дедлайн: {format_admin_datetime(giveaway.deadline_at, settings.timezone)}",
            f"Участников: {participants_count}",
            f"Победителей: {giveaway.winners_count or 0}",
            f"Пост: {giveaway.message_id or 'не опубликован'}",
        ]
    )


def mention_participant(participant: Participant) -> str:
    if participant.username:
        return f"@{html.escape(participant.username)}"
    name = " ".join(filter(None, [participant.first_name, participant.last_name])).strip()
    if not name:
        name = str(participant.user_id)
    return f'<a href="tg://user?id={participant.user_id}">{html.escape(name)}</a>'
