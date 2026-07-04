from __future__ import annotations

import html

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway, KnownChannel, Participant
from tg_drop_bot.services.dates import format_admin_datetime

# Больше победителей в одном сообщении не показываем, чтобы не упереться
# в лимит Telegram на 4096 символов.
MAX_ANNOUNCED_WINNERS = 50

GIVEAWAY_STATUS_LABELS = {
    "draft": "черновик",
    "published": "опубликован",
    "finished": "завершен",
    "cancelled": "отменен",
}

MEMBERSHIP_STATUS_LABELS = {
    "creator": "создатель канала",
    "administrator": "администратор",
    "member": "участник",
    "restricted": "ограничен",
    "left": "не подписан на канал",
    "kicked": "исключен",
    "check_failed": "проверка не удалась",
}


def giveaway_status_label(status: str) -> str:
    return GIVEAWAY_STATUS_LABELS.get(status, status)


def membership_status_label(status: str) -> str:
    return MEMBERSHIP_STATUS_LABELS.get(status, status)


def giveaway_title(giveaway: Giveaway) -> str:
    return giveaway.title or f"Розыгрыш #{giveaway.id}"


def render_giveaway_post(
    giveaway: Giveaway,
    settings: Settings,
    *,
    closed: bool = False,
    participants_count: int | None = None,
) -> str:
    title = giveaway_title(giveaway)
    parts = [
        f"<b>{'Розыгрыш завершен: ' if closed else ''}{html.escape(title)}</b>",
    ]
    if giveaway.post_text and giveaway.post_text.strip() != title.strip():
        parts.append(html.escape(giveaway.post_text))
    if giveaway.terms_text:
        parts.extend(["", "<b>Условия участия</b>", html.escape(giveaway.terms_text)])
    if participants_count is not None:
        parts.extend(["", f"<b>Участников:</b> {participants_count}"])
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
    channel_name = giveaway.channel.title if giveaway.channel else "не выбран"
    return "\n".join(
        [
            f"<b>Розыгрыш #{giveaway.id}</b>",
            f"Название: {html.escape(giveaway_title(giveaway))}",
            f"Статус: <b>{html.escape(giveaway_status_label(giveaway.status))}</b>",
            f"Канал: {html.escape(channel_name)}",
            f"Описание: {html.escape(giveaway.post_text or 'не задано')}",
            f"Дедлайн: {format_admin_datetime(giveaway.deadline_at, settings.timezone)}",
            f"Участников: {participants_count}",
            f"Победителей: {giveaway.winners_count or 0}",
            f"Пост: {giveaway.message_id or 'не опубликован'}",
        ]
    )


def render_results_announcement(giveaway: Giveaway, winners: list[Participant]) -> str:
    lines = [f"<b>Итоги розыгрыша: {html.escape(giveaway_title(giveaway))}</b>"]
    if winners:
        lines.append("")
        lines.extend(
            f"{index}. {mention_participant(participant)}"
            for index, participant in enumerate(winners[:MAX_ANNOUNCED_WINNERS], 1)
        )
        hidden = len(winners) - MAX_ANNOUNCED_WINNERS
        if hidden > 0:
            lines.append(f"...и еще победителей: {hidden}. Полный список - у организатора.")
        if giveaway.winners_count and len(winners) < giveaway.winners_count:
            lines.extend(
                [
                    "",
                    "Валидных участников оказалось меньше призовых мест: "
                    f"разыграно {len(winners)} из {giveaway.winners_count}.",
                ]
            )
        lines.extend(["", "Поздравляем победителей!"])
    else:
        lines.extend(["", "Победителей нет: не нашлось валидных участников."])
    return "\n".join(lines)


def channel_post_link(channel: KnownChannel | None, message_id: int | None) -> str | None:
    """Public or private t.me link to a channel post, if one can be built."""
    if channel is None or message_id is None:
        return None
    if channel.username:
        return f"https://t.me/{channel.username}/{message_id}"
    chat_id = str(channel.telegram_chat_id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def mention_participant(participant: Participant) -> str:
    if participant.username:
        return f"@{html.escape(participant.username)}"
    name = " ".join(filter(None, [participant.first_name, participant.last_name])).strip()
    if not name:
        name = str(participant.user_id)
    return f'<a href="tg://user?id={participant.user_id}">{html.escape(name)}</a>'
