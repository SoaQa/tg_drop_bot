from typing import cast

from aiogram import Bot

from tg_drop_bot.db.models import Giveaway, KnownChannel
from tg_drop_bot.services import conditions
from tg_drop_bot.services.conditions import (
    CHANNEL_SUBSCRIPTION_CONDITION,
    check_participant_conditions,
    condition_label,
)


def test_condition_label_is_russian() -> None:
    assert condition_label(CHANNEL_SUBSCRIPTION_CONDITION) == "Подписка на канал"


async def test_check_participant_conditions_accepts_channel_subscriber(monkeypatch) -> None:
    async def fake_is_channel_member(bot, chat_id: int, user_id: int) -> tuple[bool, str]:
        return True, "member"

    monkeypatch.setattr(conditions, "is_channel_member", fake_is_channel_member)
    giveaway = Giveaway(
        channel=KnownChannel(
            telegram_chat_id=-100123,
            title="Канал",
            is_active=True,
            bot_is_admin=True,
        )
    )

    result = await check_participant_conditions(cast(Bot, object()), giveaway, 42)

    assert result.ok is True
    assert result.membership_status == "member"
    assert result.user_message is None


async def test_check_participant_conditions_rejects_non_subscriber(monkeypatch) -> None:
    async def fake_is_channel_member(bot, chat_id: int, user_id: int) -> tuple[bool, str]:
        return False, "left"

    monkeypatch.setattr(conditions, "is_channel_member", fake_is_channel_member)
    giveaway = Giveaway(
        channel=KnownChannel(
            telegram_chat_id=-100123,
            title="Канал",
            is_active=True,
            bot_is_admin=True,
        )
    )

    result = await check_participant_conditions(cast(Bot, object()), giveaway, 42)

    assert result.ok is False
    assert result.membership_status == "left"
    assert result.user_message == "Участвовать могут только подписчики канала розыгрыша."
