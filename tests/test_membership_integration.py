import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.types import User
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tg_drop_bot.db.models import (
    Base,
    ChannelExclusion,
    ChannelExclusionPeriod,
    ChannelMembership,
    Giveaway,
    KnownChannel,
    MembershipEvent,
    Participant,
)
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.membership import (
    cleanup_membership_events,
    get_active_exclusion,
    prohibit_participation,
    record_membership_event,
    revoke_channel_exclusion,
    snapshot_draw_eligibility,
)

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL required")


@pytest_asyncio.fixture
async def membership_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    schema = "membership_test_" + uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
    scoped_engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    try:
        async with async_sessionmaker(scoped_engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await scoped_engine.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def seed(session: AsyncSession) -> tuple[KnownChannel, Giveaway, Participant]:
    now = utc_now()
    channel = KnownChannel(telegram_chat_id=-123, title="Тест")
    session.add(channel)
    await session.flush()
    giveaway = Giveaway(
        channel_id=channel.id,
        creator_user_id=1,
        status="finished",
        anti_abuse_enabled=True,
        published_at=now - timedelta(days=2),
        finished_at=now - timedelta(hours=2),
        results_announced_at=now - timedelta(hours=1),
    )
    session.add(giveaway)
    await session.flush()
    participant = Participant(
        giveaway_id=giveaway.id,
        user_id=10,
        joined_at=now - timedelta(days=1),
        captcha_passed_at=now - timedelta(days=1),
    )
    session.add(participant)
    await session.flush()
    return channel, giveaway, participant


async def test_leave_after_results_is_recorded_without_excluding_user(
    membership_session: AsyncSession,
) -> None:
    s = membership_session
    c, g, p = await seed(s)
    now = utc_now()
    for update_id, kind, at in (
        (3, "join", now - timedelta(minutes=2)),
        (2, "leave", now - timedelta(minutes=3)),
        (1, "join", now - timedelta(days=1)),
    ):
        assert await record_membership_event(
            s, channel_id=c.id, user_id=p.user_id, update_id=update_id, kind=kind, occurred_at=at
        )
    assert not await record_membership_event(
        s,
        channel_id=c.id,
        user_id=p.user_id,
        update_id=2,
        kind="leave",
        occurred_at=now - timedelta(minutes=3),
    )
    assert await s.scalar(select(func.count()).select_from(MembershipEvent)) == 3
    state = await s.scalar(select(ChannelMembership))
    assert state is not None and state.status == "join" and state.last_update_id == 3
    assert await s.scalar(select(ChannelExclusion)) is None
    assert await s.scalar(select(ChannelExclusionPeriod)) is None

    assert await snapshot_draw_eligibility(s, g, [p]) == [p]
    assert p.selection_weight == 0.7
    g.published_at = now
    g.finished_at = now + timedelta(hours=1)
    assert await snapshot_draw_eligibility(s, g, [p]) == [p]
    assert p.selection_weight == 1


async def test_retention_keeps_evidence_and_boundary(membership_session: AsyncSession) -> None:
    s = membership_session
    c, g, p = await seed(s)
    now = utc_now()
    old = now - timedelta(days=366)
    for update_id, at in ((1, old), (2, now - timedelta(days=365))):
        s.add(
            MembershipEvent(
                channel_id=c.id, user_id=99, update_id=update_id, kind="leave", occurred_at=at
            )
        )
    await s.flush()
    assert await cleanup_membership_events(s, now=now) == 1
    assert await s.scalar(select(func.count()).select_from(MembershipEvent)) == 1


async def test_historical_exclusion_period_survives_revoke(
    membership_session: AsyncSession,
) -> None:
    s = membership_session
    c, g, p = await seed(s)
    now = utc_now()
    exclusion = ChannelExclusion(channel_id=c.id, user_id=p.user_id, reason="Тест", created_at=now)
    s.add_all(
        [
            exclusion,
            ChannelExclusionPeriod(
                channel_id=c.id, user_id=p.user_id, reason=exclusion.reason, started_at=now
            ),
        ]
    )
    await s.flush()
    g.finished_at = exclusion.created_at
    assert await snapshot_draw_eligibility(s, g, [p]) == []
    assert await revoke_channel_exclusion(s, exclusion.id, 1)
    assert await snapshot_draw_eligibility(s, g, [p]) == []
    g.finished_at = utc_now()
    assert await snapshot_draw_eligibility(s, g, [p]) == [p]


async def test_manual_prohibition_blocks_registration_and_draw(
    membership_session: AsyncSession, monkeypatch
) -> None:
    from tg_drop_bot.services import giveaways
    from tg_drop_bot.services.conditions import ConditionCheck
    from tg_drop_bot.services.membership import PARTICIPATION_DENIED

    s = membership_session
    c, g, p = await seed(s)
    other = KnownChannel(telegram_chat_id=-124, title="Другой")
    s.add(other)
    await s.flush()
    assert await prohibit_participation(s, c.id, p.user_id, 1)
    assert not await prohibit_participation(s, c.id, p.user_id, 1)
    assert await get_active_exclusion(s, other.id, p.user_id) is None
    g.finished_at = utc_now()
    assert await snapshot_draw_eligibility(s, g, [p]) == []
    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        AsyncMock(return_value=ConditionCheck(ok=True, membership_status="member")),
    )
    user = User(id=p.user_id, first_name="Test", is_bot=False)
    from tg_drop_bot.bot.participant import start_captcha_flow
    from tg_drop_bot.config import Settings

    g.status = "published"
    g.deadline_at = utc_now() + timedelta(hours=1)
    message = AsyncMock()
    bot = AsyncMock()
    await start_captcha_flow(message, AsyncMock(), s, Settings(), bot, g.id, user=user)
    message.answer.assert_awaited_once_with(PARTICIPATION_DENIED)
    message.answer_photo.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()
    blocked = await giveaways.register_participant_after_condition_check(
        s, cast(Bot, object()), g, user
    )
    assert blocked.participant is None
    assert blocked.condition_check.user_message == PARTICIPATION_DENIED
    exclusion = await get_active_exclusion(s, c.id, p.user_id)
    assert exclusion is not None
    assert await revoke_channel_exclusion(s, exclusion.id, 1)
    assert await get_active_exclusion(s, c.id, p.user_id) is None


async def test_draw_records_membership_exclusion(
    membership_session: AsyncSession, monkeypatch
) -> None:
    from tg_drop_bot.services import giveaways
    from tg_drop_bot.services.conditions import ConditionCheck

    s = membership_session
    c, g, p = await seed(s)
    g.results_state = "pending"
    g.winners_count = 1
    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        AsyncMock(return_value=ConditionCheck(ok=False, membership_status="left")),
    )
    assert await giveaways.draw_and_record_winners(s, cast(Bot, object()), g) == []
    assert p.draw_excluded and p.exclusion_reason == "Не выполнено условие подписки на канал"
