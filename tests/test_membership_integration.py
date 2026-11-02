import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tg_drop_bot.db.models import (
    Base,
    ChannelExclusion,
    ChannelMembership,
    Giveaway,
    KnownChannel,
    MembershipEvent,
    Participant,
)
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.membership import (
    cleanup_membership_events,
    reconcile_one,
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


async def test_out_of_order_rejoin_exclusion_revocation(membership_session: AsyncSession) -> None:
    s = membership_session
    c, g, p = await seed(s)
    now = utc_now()

    async def event(update_id: int, kind: str, at: datetime) -> bool:
        return await record_membership_event(
            s, channel_id=c.id, user_id=p.user_id, update_id=update_id, kind=kind, occurred_at=at
        )

    assert await event(3, "join", now - timedelta(minutes=2))
    assert await event(2, "leave", now - timedelta(minutes=3))
    assert await s.scalar(select(ChannelExclusion)) is None
    assert await event(1, "join", now - timedelta(days=1))
    assert not await event(2, "leave", now - timedelta(minutes=3))
    state = await s.scalar(select(ChannelMembership))
    assert state is not None and state.status == "join" and state.last_update_id == 3
    exclusion = await s.scalar(select(ChannelExclusion))
    assert exclusion is not None
    assert await revoke_channel_exclusion(s, exclusion.id, 1)
    assert not await revoke_channel_exclusion(s, exclusion.id, 1)
    assert await event(4, "leave", now - timedelta(minutes=4))
    assert exclusion.revoked_at is not None
    eligible = await snapshot_draw_eligibility(s, g, [p])
    assert eligible == [p] and p.selection_weight == 0.7
    assert await event(5, "leave", utc_now())
    assert exclusion.revoked_at is None


async def test_unknown_kick_channel_isolation_and_legacy_weights(
    membership_session: AsyncSession,
) -> None:
    s = membership_session
    c, g, p = await seed(s)
    await record_membership_event(
        s, channel_id=c.id, user_id=10, update_id=1, kind="leave", occurred_at=utc_now()
    )
    assert await s.scalar(select(ChannelExclusion)) is None
    await record_membership_event(
        s,
        channel_id=c.id,
        user_id=10,
        update_id=2,
        kind="join",
        occurred_at=utc_now() - timedelta(days=1),
    )
    await record_membership_event(
        s, channel_id=c.id, user_id=10, update_id=3, kind="kicked", occurred_at=utc_now()
    )
    # The earlier voluntary leave is independently proven and bans on late join arrival.
    exclusion = await s.scalar(select(ChannelExclusion))
    assert exclusion is not None
    future = Giveaway(
        channel_id=c.id,
        creator_user_id=1,
        status="finished",
        anti_abuse_enabled=False,
        published_at=g.published_at,
        finished_at=utc_now() + timedelta(seconds=1),
    )
    assert await snapshot_draw_eligibility(s, future, [p]) == []
    assert p.selection_weight == 1
    other = KnownChannel(telegram_chat_id=-124, title="Другой")
    s.add(other)
    await s.flush()
    future.channel_id = other.id
    assert await snapshot_draw_eligibility(s, future, [p]) == [p]


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


@pytest.mark.parametrize(
    "case", ["left", "prewindow_left", "kicked", "revoked", "expired", "unknown"]
)
async def test_polling_guards(membership_session: AsyncSession, case: str) -> None:
    s = membership_session
    c, g, p = await seed(s)
    now = utc_now()
    if case != "unknown":
        await record_membership_event(
            s,
            channel_id=c.id,
            user_id=p.user_id,
            update_id=1,
            kind="join",
            occurred_at=now - timedelta(days=1),
        )
    if case == "kicked":
        await record_membership_event(
            s,
            channel_id=c.id,
            user_id=p.user_id,
            update_id=2,
            kind="kicked",
            occurred_at=now - timedelta(minutes=5),
        )
    if case == "revoked":
        s.add(
            ChannelExclusion(
                channel_id=c.id,
                user_id=p.user_id,
                reason="Тест",
                revoked_at=now - timedelta(minutes=5),
            )
        )
    if case == "left":
        p.membership_status = "member"
        p.membership_checked_at = now - timedelta(minutes=10)
    if case == "prewindow_left":
        assert g.results_announced_at is not None
        await record_membership_event(
            s,
            channel_id=c.id,
            user_id=p.user_id,
            update_id=2,
            kind="leave",
            occurred_at=g.results_announced_at - timedelta(minutes=1),
        )
    if case == "expired":
        g.results_announced_at = now - timedelta(hours=73)
    await s.commit()
    factory = async_sessionmaker(s.bind, expire_on_commit=False)
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status="left")
    checked = await reconcile_one(factory, cast(Bot, bot))
    assert checked == (case not in {"expired", "unknown"})
    await s.rollback()
    exclusion = await s.scalar(select(ChannelExclusion))
    if case == "left":
        assert exclusion is not None and exclusion.revoked_at is None
    elif case == "revoked":
        assert exclusion is not None and exclusion.revoked_at is not None
    else:
        assert exclusion is None


async def test_event_window_boundary_and_kick(membership_session: AsyncSession) -> None:
    s = membership_session
    c, g, p = await seed(s)
    assert g.results_announced_at is not None
    await record_membership_event(
        s,
        channel_id=c.id,
        user_id=p.user_id,
        update_id=1,
        kind="join",
        occurred_at=utc_now() - timedelta(days=1),
    )
    await record_membership_event(
        s,
        channel_id=c.id,
        user_id=p.user_id,
        update_id=2,
        kind="kicked",
        occurred_at=g.results_announced_at + timedelta(minutes=1),
    )
    assert await s.scalar(select(ChannelExclusion)) is None


async def test_historical_exclusion_period_survives_revoke(
    membership_session: AsyncSession,
) -> None:
    from tg_drop_bot.services.membership import exclude_user

    s = membership_session
    c, g, p = await seed(s)
    await exclude_user(s, c.id, p.user_id, utc_now())
    await s.flush()
    exclusion = await s.scalar(select(ChannelExclusion))
    assert exclusion is not None
    g.finished_at = exclusion.created_at
    assert await snapshot_draw_eligibility(s, g, [p]) == []
    assert await revoke_channel_exclusion(s, exclusion.id, 1)
    assert await snapshot_draw_eligibility(s, g, [p]) == []
    g.finished_at = utc_now()
    assert await snapshot_draw_eligibility(s, g, [p]) == [p]


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


@pytest.mark.parametrize("event_seconds", [0, 1])
@pytest.mark.parametrize("api_status", ["left", "member"])
async def test_poll_discards_reply_when_rejoin_arrives_before_lock(
    membership_session: AsyncSession,
    monkeypatch,
    event_seconds: int,
    api_status: str,
) -> None:
    from tg_drop_bot.services import membership

    s = membership_session
    c, g, p = await seed(s)
    started = utc_now().replace(microsecond=500000)
    assert g.results_announced_at is not None
    for update_id, kind, at in (
        (1, "join", started - timedelta(days=1)),
        (2, "leave", g.results_announced_at - timedelta(minutes=1)),
    ):
        await record_membership_event(
            s, channel_id=c.id, user_id=p.user_id, update_id=update_id, kind=kind, occurred_at=at
        )
    p.membership_status = "left"
    p.membership_checked_at = started - timedelta(hours=2)
    await s.commit()
    factory = async_sessionmaker(s.bind, expire_on_commit=False)
    original_lock = membership.lock_user
    delivered = False
    bot = AsyncMock()
    bot.get_chat_member.return_value = SimpleNamespace(status=api_status)

    async def deliver_before_lock(session: AsyncSession, channel_id: int, user_id: int) -> None:
        nonlocal delivered
        if not delivered:
            delivered = True
            assert bot.get_chat_member.await_count == 1
            async with factory() as event_session:
                await record_membership_event(
                    event_session,
                    channel_id=channel_id,
                    user_id=user_id,
                    update_id=3,
                    kind="join",
                    occurred_at=started.replace(microsecond=0) + timedelta(seconds=event_seconds),
                )
                await event_session.commit()
        await original_lock(session, channel_id, user_id)

    monkeypatch.setattr(membership, "lock_user", deliver_before_lock)
    monkeypatch.setattr(membership, "utc_now", lambda: started)
    assert await membership.reconcile_one(factory, cast(Bot, bot))
    assert delivered
    await s.refresh(p)
    assert p.membership_status == "left"
    assert p.membership_checked_at == started - timedelta(hours=2)
    assert await s.scalar(select(ChannelExclusion)) is None
    state = await s.scalar(select(ChannelMembership).execution_options(populate_existing=True))
    assert state is not None and state.status == "join" and state.last_update_id == 3
