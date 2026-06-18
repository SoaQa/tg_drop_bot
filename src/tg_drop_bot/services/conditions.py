from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot

from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.telegram import is_group_member

CHANNEL_SUBSCRIPTION_CONDITION = "channel_subscription"

CONDITION_LABELS = {
    CHANNEL_SUBSCRIPTION_CONDITION: "Подписка на канал",
}


@dataclass(frozen=True)
class ConditionCheck:
    ok: bool
    membership_status: str
    user_message: str | None = None


def condition_label(condition: str) -> str:
    return CONDITION_LABELS.get(condition, condition)


async def check_participant_conditions(
    bot: Bot,
    giveaway: Giveaway,
    user_id: int,
) -> ConditionCheck:
    if giveaway.group is None:
        return ConditionCheck(
            ok=False,
            membership_status="check_failed",
            user_message="Канал розыгрыша не найден.",
        )

    is_member, status = await is_group_member(bot, giveaway.group.telegram_chat_id, user_id)
    if is_member:
        return ConditionCheck(ok=True, membership_status=status)
    if status == "check_failed":
        return ConditionCheck(
            ok=False,
            membership_status=status,
            user_message="Не удалось проверить подписку на канал. Попробуйте позже.",
        )
    return ConditionCheck(
        ok=False,
        membership_status=status,
        user_message="Участвовать могут только подписчики канала розыгрыша.",
    )
