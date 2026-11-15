import os
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import (
    AuditLog,
    Base,
    Giveaway,
    HardGameAttempt,
    KnownChannel,
    Participant,
    ParticipationFlow,
    ParticipationJob,
)
from tg_drop_bot.services import giveaways
from tg_drop_bot.services.conditions import ConditionCheck
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.participation_queue import giveaway_has_pending_intake
from tg_drop_bot.services.participation_worker import RetryParticipation, process_participation_job

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL required")


@pytest.fixture
async def accepted_game():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    schema = "finish_game_" + uuid4().hex
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
    scoped = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={
            "server_settings": {"search_path": schema},
        },
    )
    sessions = async_sessionmaker(scoped, expire_on_commit=False)
    now = utc_now()
    received = now - timedelta(seconds=30)
    try:
        async with sessions.begin() as session:
            channel = KnownChannel(telegram_chat_id=-555, title="Test")
            session.add(channel)
            await session.flush()
            giveaway = Giveaway(
                channel_id=channel.id,
                creator_user_id=1,
                status="published",
                deadline_at=now - timedelta(seconds=10),
                intake_closed_at=now - timedelta(seconds=10),
            )
            session.add(giveaway)
            await session.flush()
            profile = {"id": 123, "is_bot": False, "first_name": "Test"}
            flow = ParticipationFlow(
                giveaway_id=giveaway.id,
                user_id=123,
                user_profile=profile,
                status="game",
                challenge_id=17,
                captcha_sent_at=received - timedelta(seconds=10),
                expires_at=now - timedelta(seconds=5),
            )
            session.add(flow)
            session.add(
                HardGameAttempt(
                    giveaway_id=giveaway.id,
                    user_id=123,
                    attempt_key="hard-key",
                    started_at=received - timedelta(seconds=9),
                )
            )
            audit = AuditLog(
                giveaway_id=giveaway.id,
                actor_user_id=123,
                action="frog.run.finished",
                payload={
                    "status": "won",
                    "game_result": {
                        "user": profile,
                        "started": received.timestamp() - 9,
                        "checked": received.timestamp(),
                        "difficulty": "hard",
                        "hard_key": "hard-key",
                        "challenge_id": 17,
                    },
                },
            )
            session.add(audit)
            await session.flush()
            job = ParticipationJob(
                update_id=-audit.id,
                flow_id=flow.id,
                kind="game_win",
                challenge_id=audit.id,
                received_at=received,
            )
            session.add(job)
            await session.flush()
            ids = giveaway.id, flow.id, job.id, audit.id
        yield sessions, ids, received
    finally:
        await scoped.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def test_accepted_finish_survives_temporary_membership_failure_and_flow_expiry(
    accepted_game,
    monkeypatch,
):
    sessions, (gid, flow_id, job_id, _), received = accepted_game
    check = AsyncMock(return_value=ConditionCheck(ok=False, membership_status="check_failed"))
    monkeypatch.setattr(giveaways, "check_participant_conditions", check)
    bot = AsyncMock()
    with pytest.raises(RetryParticipation):
        await process_participation_job(sessions, bot, Settings(), job_id)
    async with sessions() as session:
        job = await session.get(ParticipationJob, job_id)
        flow = await session.get(ParticipationFlow, flow_id)
        assert job is not None and job.processed_at is None
        assert flow is not None and flow.status == "game"
        assert await giveaway_has_pending_intake(session, gid, now=utc_now())
        assert await session.scalar(select(func.count()).select_from(Participant)) == 0
    bot.send_message.assert_not_awaited()
    check.return_value = ConditionCheck(ok=True, membership_status="member")
    await process_participation_job(sessions, bot, Settings(), job_id)
    await process_participation_job(sessions, bot, Settings(), job_id)
    bot.send_message.assert_awaited_once_with(123, "Лягушка добралась! Ваше участие засчитано.")
    async with sessions() as session:
        participant = await session.scalar(select(Participant))
        achievement = await session.get(HardGameAttempt, (gid, 123))
        flow = await session.get(ParticipationFlow, flow_id)
        assert participant is not None and participant.captcha_passed_at == received
        assert participant.captcha_response_ms == 10000
        assert achievement is not None and achievement.completed_at == received
        assert achievement.duration_ms == 9000
        assert flow is not None and flow.status == "done"
        assert not await giveaway_has_pending_intake(session, gid, now=utc_now())


@pytest.mark.parametrize(
    "invalid",
    [
        "actor",
        "owner",
        "status",
        "late",
        "at_expiry",
        "lineage",
    ],
)
async def test_finish_requires_server_win_owner_lineage_and_timely_receipt(
    accepted_game,
    monkeypatch,
    invalid,
):
    sessions, (_, flow_id, job_id, audit_id), _ = accepted_game
    check = AsyncMock(return_value=ConditionCheck(ok=True, membership_status="member"))
    monkeypatch.setattr(giveaways, "check_participant_conditions", check)
    async with sessions.begin() as session:
        audit = await session.get(AuditLog, audit_id)
        flow = await session.get(ParticipationFlow, flow_id)
        job = await session.get(ParticipationJob, job_id)
        assert audit is not None and flow is not None and job is not None
        payload = dict(audit.payload)
        result = dict(payload["game_result"])
        if invalid == "actor":
            audit.actor_user_id = 456
        elif invalid == "owner":
            result["user"] = {"id": 456, "first_name": "Other"}
        elif invalid == "status":
            payload["status"] = "lost"
        elif invalid in ("late", "at_expiry"):
            assert flow.expires_at is not None
            job.received_at = flow.expires_at + timedelta(seconds=1 if invalid == "late" else 0)
            result["started"] = job.received_at.timestamp() - 9
            result["checked"] = job.received_at.timestamp()
        else:
            result["challenge_id"] = 18
        payload["game_result"] = result
        audit.payload = payload
    await process_participation_job(sessions, AsyncMock(), Settings(), job_id)
    check.assert_not_awaited()
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Participant)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(HardGameAttempt)
                .where(
                    HardGameAttempt.completed_at.is_not(None),
                )
            )
            == 0
        )


async def test_definitive_membership_denial_does_not_create_participant_or_top(
    accepted_game,
    monkeypatch,
):
    sessions, (_, flow_id, job_id, _), _ = accepted_game
    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        AsyncMock(
            return_value=ConditionCheck(
                ok=False, membership_status="left", user_message="Нет подписки"
            ),
        ),
    )
    bot = AsyncMock()
    await process_participation_job(sessions, bot, Settings(), job_id)
    bot.send_message.assert_awaited_once_with(123, "Нет подписки")
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Participant)) == 0
        flow = await session.get(ParticipationFlow, flow_id)
        job = await session.get(ParticipationJob, job_id)
        assert flow is not None and flow.status == "done"
        assert job is not None and job.processed_at is not None
