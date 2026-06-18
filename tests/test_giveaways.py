from datetime import timedelta
from types import SimpleNamespace
from typing import cast

from aiogram import Bot
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import Giveaway, Participant
from tg_drop_bot.services import giveaways
from tg_drop_bot.services.conditions import ConditionCheck
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import (
    pick_winners,
    register_participant_after_condition_check,
    single_giveaway_or_none,
    validate_draft,
)


def test_validate_draft_reports_missing_fields() -> None:
    giveaway = Giveaway(creator_user_id=1)
    assert validate_draft(giveaway) == [
        "канал",
        "название",
        "описание",
        "условия",
        "количество победителей",
        "дедлайн",
    ]


def test_pick_winners_uses_available_participants_when_shortage() -> None:
    participants = [
        Participant(
            giveaway_id=1,
            user_id=1,
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        ),
        Participant(
            giveaway_id=1,
            user_id=2,
            joined_at=utc_now() + timedelta(seconds=1),
            captcha_passed_at=utc_now() + timedelta(seconds=1),
        ),
    ]
    winners = pick_winners(participants, 5)
    assert sorted(participant.user_id for participant in winners) == [1, 2]


def test_single_giveaway_or_none_returns_only_unambiguous_giveaway() -> None:
    giveaway = Giveaway(id=7, creator_user_id=1)

    assert single_giveaway_or_none([giveaway]) is giveaway
    assert single_giveaway_or_none([]) is None
    assert (
        single_giveaway_or_none(
            [giveaway, Giveaway(id=8, creator_user_id=1)]
        )
        is None
    )


async def test_register_participant_after_condition_check_rejects_non_subscriber(
    monkeypatch,
) -> None:
    async def fake_check_participant_conditions(
        bot: Bot,
        giveaway: Giveaway,
        user_id: int,
    ) -> ConditionCheck:
        return ConditionCheck(
            ok=False,
            membership_status="left",
            user_message="Участвовать могут только подписчики канала розыгрыша.",
        )

    async def fake_register_participant(
        session: AsyncSession,
        giveaway: Giveaway,
        user: User,
    ) -> tuple[Participant, bool]:
        raise AssertionError("Неподписанного пользователя нельзя регистрировать")

    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        fake_check_participant_conditions,
    )
    monkeypatch.setattr(giveaways, "register_participant", fake_register_participant)

    result = await register_participant_after_condition_check(
        cast(AsyncSession, object()),
        cast(Bot, object()),
        Giveaway(id=1, creator_user_id=1),
        cast(User, SimpleNamespace(id=42)),
    )

    assert result.participant is None
    assert result.created is False
    assert result.condition_check.membership_status == "left"


async def test_register_participant_after_condition_check_keeps_membership_status(
    monkeypatch,
) -> None:
    participant = Participant(
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
        membership_status="member",
    )

    async def fake_check_participant_conditions(
        bot: Bot,
        giveaway: Giveaway,
        user_id: int,
    ) -> ConditionCheck:
        return ConditionCheck(ok=True, membership_status="administrator")

    async def fake_register_participant(
        session: AsyncSession,
        giveaway: Giveaway,
        user: User,
    ) -> tuple[Participant, bool]:
        return participant, True

    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        fake_check_participant_conditions,
    )
    monkeypatch.setattr(giveaways, "register_participant", fake_register_participant)

    result = await register_participant_after_condition_check(
        cast(AsyncSession, object()),
        cast(Bot, object()),
        Giveaway(id=1, creator_user_id=1),
        cast(User, SimpleNamespace(id=42)),
    )

    assert result.participant is participant
    assert result.created is True
    assert participant.membership_status == "administrator"
    assert participant.membership_checked_at is not None
