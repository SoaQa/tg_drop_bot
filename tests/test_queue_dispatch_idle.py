import os
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Base, Giveaway, ParticipationFlow, ParticipationJob
from tg_drop_bot.queue_worker import next_dispatch_batch
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.participation_worker import process_participation_job


async def test_empty_dispatch_skips_both_lease_queries(monkeypatch):
    from tg_drop_bot import queue_worker

    session = AsyncMock()
    session.scalar.return_value = False
    lease = AsyncMock()
    monkeypatch.setattr(queue_worker, "lease_jobs_for_dispatch", lease)
    assert await next_dispatch_batch(session) == ([], [])
    lease.assert_not_awaited()


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL required")
async def test_processed_job_with_pending_reply_is_dispatched_until_delivery():
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    schema = "dispatch_idle_" + uuid4().hex
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
    try:
        async with sessions.begin() as session:
            giveaway = Giveaway(creator_user_id=1, status="published")
            session.add(giveaway)
            await session.flush()
            flow = ParticipationFlow(
                giveaway_id=giveaway.id, user_id=123, user_profile={}, status="done"
            )
            session.add(flow)
            await session.flush()
            job = ParticipationJob(
                update_id=1,
                flow_id=flow.id,
                kind="start",
                received_at=utc_now(),
                processed_at=utc_now(),
            )
            session.add(job)
            await session.flush()
            job_id, flow_id = job.id, flow.id

        async with sessions.begin() as session:
            assert await next_dispatch_batch(session) == ([], [])
            loaded = await session.get(ParticipationJob, job_id)
            assert loaded is not None
            loaded.reply_text = "Готово, участие засчитано."

        async with sessions.begin() as session:
            starts, priority = await next_dispatch_batch(session)
            assert starts == [job_id] and priority == []

        bot = AsyncMock()
        await process_participation_job(sessions, bot, Settings(), job_id)
        bot.send_message.assert_awaited_once_with(123, "Готово, участие засчитано.")
        async with sessions.begin() as session:
            assert await next_dispatch_batch(session) == ([], [])
            # Unprocessed answer jobs still enter the priority lane normally.
            session.add(
                ParticipationJob(update_id=2, flow_id=flow_id, kind="answer", received_at=utc_now())
            )
        async with sessions.begin() as session:
            starts, priority = await next_dispatch_batch(session)
            assert starts == [] and len(priority) == 1
    finally:
        await scoped.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
