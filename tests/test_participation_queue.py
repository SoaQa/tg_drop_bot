from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from time import monotonic
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import User
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tg_drop_bot.bot.participation_webhook import ParticipationWebhook
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import (
    AuditLog,
    Giveaway,
    KnownChannel,
    Participant,
    ParticipationFlow,
    ParticipationJob,
)
from tg_drop_bot.services import giveaways, participation_worker
from tg_drop_bot.services.captcha import hash_captcha_answer
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.participation_limits import ParticipationLimits, QueuePressure
from tg_drop_bot.services.participation_queue import (
    admit_captcha_action,
    admit_start,
    giveaway_has_pending_intake,
)
from tg_drop_bot.services.participation_worker import process_participation_job, queued_captcha_code
from tg_drop_bot.services.rate_limit import RedisRateLimiter

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Requires the PostgreSQL integration database"
)
SECRET = "queue-test-secret"


@pytest.fixture
async def giveaway_db() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], int]]:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        channel = KnownChannel(title="Очередь", telegram_chat_id=-(uuid4().int % 10**15))
        session.add(channel)
        await session.flush()
        giveaway = Giveaway(
            status="published",
            creator_user_id=1,
            channel_id=channel.id,
            deadline_at=utc_now() + timedelta(minutes=10),
            published_at=utc_now(),
        )
        session.add(giveaway)
        await session.flush()
        giveaway_id, channel_id = giveaway.id, channel.id
    try:
        yield sessions, giveaway_id
    finally:
        async with sessions.begin() as session:
            await session.execute(delete(AuditLog).where(AuditLog.giveaway_id == giveaway_id))
            await session.execute(delete(Participant).where(Participant.giveaway_id == giveaway_id))
            await session.execute(delete(Giveaway).where(Giveaway.id == giveaway_id))
            await session.execute(delete(KnownChannel).where(KnownChannel.id == channel_id))
        await engine.dispose()


async def test_draw_waits_for_accepted_request_and_late_captcha(
    giveaway_db,
    monkeypatch,
) -> None:
    sessions, giveaway_id = giveaway_db
    settings = Settings(PARTICIPATION_QUEUE_ENABLED=True, APP_SECRET_KEY=SECRET)
    bot = AsyncMock(spec=Bot)
    bot.get_chat_member.return_value.status = "member"
    draw = AsyncMock(return_value=[])
    monkeypatch.setattr(giveaways, "announce_results_preparing", AsyncMock())
    monkeypatch.setattr(giveaways, "draw_and_record_winners", draw)
    monkeypatch.setattr(giveaways, "publish_giveaway_results", AsyncMock())
    user = User(id=42, is_bot=False, first_name="Участник")
    update_id = uuid4().int % 10**15
    allow = AsyncMock(return_value=True)

    async with sessions.begin() as session:
        assert (
            await admit_start(
                session,
                giveaway_id=giveaway_id,
                user=user,
                update_id=update_id,
                received_at=utc_now(),
                allow_new=allow,
            )
            == "accepted"
        )
        assert (
            await admit_start(
                session,
                giveaway_id=giveaway_id,
                user=user,
                update_id=update_id + 1,
                received_at=utc_now(),
                allow_new=allow,
            )
            == "duplicate"
        )
        job_id = await session.scalar(
            select(ParticipationJob.id).where(ParticipationJob.update_id == update_id)
        )
        assert job_id is not None

    async with sessions() as session:
        giveaway = await giveaways.get_giveaway(session, giveaway_id)
        assert giveaway is not None
        result = await giveaways.finish_giveaway(
            session, bot, settings, giveaway, actor_user_id=1, manual=True
        )
        assert result.draining and not result.finished
        draw.assert_not_awaited()
    async with sessions.begin() as session:
        assert (
            await admit_start(
                session,
                giveaway_id=giveaway_id,
                user=User(id=43, is_bot=False, first_name="Later"),
                update_id=update_id + 2,
                received_at=utc_now(),
                allow_new=allow,
            )
            == "closed"
        )

    await process_participation_job(sessions, bot, settings, job_id)
    await process_participation_job(sessions, bot, settings, job_id)
    bot.send_photo.assert_awaited_once()
    async with sessions.begin() as session:
        flow = await session.scalar(
            select(ParticipationFlow).where(ParticipationFlow.giveaway_id == giveaway_id)
        )
        assert flow is not None and flow.expires_at is not None
        assert flow.captcha_sent_at is not None
        assert flow.expires_at > utc_now() + timedelta(seconds=295)
        assert await giveaway_has_pending_intake(session, giveaway_id, now=utc_now())
        code = queued_captcha_code(settings, job_id)
        received_at = flow.captcha_sent_at + timedelta(milliseconds=1500)
        assert (
            await admit_captcha_action(
                session,
                user_id=user.id,
                update_id=update_id + 3,
                received_at=received_at,
                max_attempts=3,
                answer_hash=hash_captcha_answer(code, settings),
            )
            == "accepted"
        )
        assert (
            await admit_captcha_action(
                session,
                user_id=user.id,
                update_id=update_id + 3,
                received_at=received_at,
                max_attempts=3,
                answer_hash=hash_captcha_answer(code, settings),
            )
            == "duplicate"
        )
        # The answer was on time, even if processing starts after the challenge expires.
        flow.expires_at = received_at + timedelta(milliseconds=1)
        processed_at = flow.expires_at + timedelta(seconds=3)
        answer_id = await session.scalar(
            select(ParticipationJob.id).where(ParticipationJob.update_id == update_id + 3)
        )
        assert answer_id is not None
        assert await giveaway_has_pending_intake(session, giveaway_id, now=processed_at)

    monkeypatch.setattr(participation_worker, "utc_now", lambda: processed_at)
    await process_participation_job(sessions, bot, settings, answer_id)
    await process_participation_job(sessions, bot, settings, answer_id)
    bot.send_message.assert_awaited_once_with(user.id, "Готово, участие засчитано.")
    monkeypatch.setattr(giveaways, "utc_now", lambda: utc_now() + timedelta(seconds=3))
    async with sessions() as session:
        participant = await session.scalar(
            select(Participant).where(Participant.giveaway_id == giveaway_id)
        )
        assert participant is not None
        assert participant.joined_at == received_at
        assert participant.captcha_response_ms == 1500
        giveaway = await giveaways.get_giveaway(session, giveaway_id)
        assert giveaway is not None
        result = await giveaways.finish_giveaway(
            session, bot, settings, giveaway, actor_user_id=None, manual=False
        )
        assert result.finished
        draw.assert_awaited_once()


WORKER = """
import sys
import threading
from aiogram.client.telegram import TelegramAPIServer
from dramatiq import Worker
from tg_drop_bot.queue_worker import broker, worker_runtime
worker_runtime()[1].session.api = TelegramAPIServer.from_base(sys.argv[1], is_local=True)
worker = Worker(broker, worker_threads=6, worker_timeout=100)
broker.emit_after("process_boot")
worker.start()
threading.Event().wait()
"""


async def wait_for(check: Callable[[], Awaitable[bool]], timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.1)


@pytest.mark.skipif(
    not os.getenv("TEST_QUEUE_REDIS_URL") or os.getenv("RUN_QUEUE_LOAD_TEST") != "1",
    reason="Set RUN_QUEUE_LOAD_TEST=1 and a dedicated TEST_QUEUE_REDIS_URL",
)
async def test_webhook_load_and_worker_restart(giveaway_db, monkeypatch) -> None:
    from tg_drop_bot import queue_worker

    sessions, giveaway_id = giveaway_db
    redis_url = os.environ["TEST_QUEUE_REDIS_URL"]
    settings = Settings(
        BOT_TOKEN="123456:test",
        APP_SECRET_KEY=SECRET,
        ADMIN_IDS="1",
        REDIS_URL=redis_url,
        WEBHOOK_SECRET=SECRET,
        PARTICIPATION_QUEUE_ENABLED=True,
    )
    from dramatiq.brokers.redis import RedisBroker

    namespace = f"queue-test-{uuid4().hex}"
    # Isolate the real broker in the test Redis DB, keeping the production actor names.
    test_broker = RedisBroker(url=redis_url, namespace=namespace)
    for actor in (queue_worker.start_job, queue_worker.priority_job):
        test_broker.declare_actor(actor)
        monkeypatch.setattr(actor, "broker", test_broker)
    limits = ParticipationLimits(settings)
    await limits.redis.delete(
        "participation:ingress", "participation:outbound", "participation:telegram_pause"
    )
    user_limiter = RedisRateLimiter(settings)
    bot = Bot(settings.bot_token)
    webhook_app = web.Application()
    ParticipationWebhook(
        dispatcher=Dispatcher(),
        bot=bot,
        secret_token=SECRET,
        queue_settings=settings,
        queue_sessions=sessions,
        limits=limits,
        user_limiter=user_limiter,
    ).register(webhook_app, path="/hook")
    api_app = web.Application()
    api_entered = asyncio.Event()
    hold_api = asyncio.Event()
    api_calls = 0
    photos = 0
    rate_limited_user: int | None = None
    retried_at = throttled_at = 0.0

    async def telegram_api(request: web.Request) -> web.Response:
        nonlocal api_calls, photos, rate_limited_user, retried_at, throttled_at
        api_calls += 1
        data = await request.post()
        if request.match_info["method"] == "getChatMember":
            api_entered.set()
            was_held = not hold_api.is_set()
            await hold_api.wait()
            user_id = int(str(data["user_id"]))
            if rate_limited_user is None and not was_held:
                rate_limited_user = user_id
                throttled_at = monotonic()
                return web.json_response(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Test rate limit",
                        "parameters": {"retry_after": 1},
                    },
                    status=429,
                )
            if user_id == rate_limited_user and not retried_at:
                retried_at = monotonic()
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        "status": "member",
                        "user": {
                            "id": user_id,
                            "is_bot": False,
                            "first_name": "Test",
                        },
                    },
                }
            )
        photos += request.match_info["method"] == "sendPhoto"
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": api_calls,
                    "date": int(utc_now().timestamp()),
                    "chat": {"type": "private", "id": int(str(data["chat_id"]))},
                },
            }
        )

    api_app.router.add_post("/{token}/{method}", telegram_api)
    seed = uuid4().int % 10**12
    async with TestServer(api_app) as api, TestClient(TestServer(webhook_app)) as client:

        async def start_worker() -> asyncio.subprocess.Process:
            code = WORKER.replace(
                "worker = Worker", f"broker.namespace = {namespace!r}\nworker = Worker"
            )
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                str(api.make_url("/")).rstrip("/"),
                env={
                    **os.environ,
                    "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
                    "REDIS_URL": redis_url,
                    "BOT_TOKEN": settings.bot_token,
                    "APP_SECRET_KEY": SECRET,
                },
            )

        async def send(index: int, *, expect_admission: bool = True) -> float:
            started = monotonic()
            user_id = seed + index + 10
            async with client.post(
                "/hook",
                headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
                json={
                    "update_id": user_id,
                    "message": {
                        "message_id": index + 1,
                        "date": int(utc_now().timestamp()),
                        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                        "chat": {"id": user_id, "type": "private"},
                        "text": f"/start giveaway_{giveaway_id}",
                    },
                },
            ) as response:
                assert response.status == 200
                payload = await response.json()
                if expect_admission:
                    assert payload == {}
                else:
                    assert payload == {} or payload["method"] == "sendMessage"
            return monotonic() - started

        async def paced(index: int) -> float:
            await asyncio.sleep(index / 50)
            return await send(index)

        relay = asyncio.create_task(queue_worker.dispatch_participation_jobs(sessions))
        worker = await start_worker()
        try:
            steady = await asyncio.gather(*(paced(i) for i in range(100)))
            await asyncio.sleep(0.1)
            burst = await asyncio.gather(*(send(i) for i in range(100, 200)))
            await asyncio.wait_for(api_entered.wait(), timeout=15)
            worker.kill()
            await worker.wait()
            hold_api.set()
            worker = await start_worker()

            async def flow_count(*, ready: bool = False) -> int:
                async with sessions() as session:
                    stmt = (
                        select(func.count())
                        .select_from(ParticipationFlow)
                        .where(
                            ParticipationFlow.giveaway_id == giveaway_id,
                        )
                    )
                    if ready:
                        stmt = stmt.where(ParticipationFlow.status == "captcha")
                    return await session.scalar(stmt) or 0

            async def drained() -> bool:
                return await flow_count(ready=True) == 200

            recovery_started = monotonic()
            await wait_for(drained, 80)
            assert photos == 200
            assert retried_at - throttled_at >= 0.9
            latencies = sorted(steady + burst)
            print(
                f"admitted=200, webhook p95={latencies[189] * 1000:.0f} ms, "
                f"max={latencies[-1] * 1000:.0f} ms, drain after restart="
                f"{monotonic() - recovery_started:.1f} s"
            )

            async def flood(index: int) -> float:
                await asyncio.sleep(index / 100)
                return await send(200 + index, expect_admission=False)

            flood_latencies = sorted(await asyncio.gather(*(flood(i) for i in range(5000))))
            admitted = await flow_count() - 200
            assert 0 < admitted < 5000

            async def flood_drained() -> bool:
                return await flow_count(ready=True) == admitted + 200

            drain_started = monotonic()
            await wait_for(flood_drained, 100)
            assert photos == admitted + 200
            print(
                f"flood=5000 at 100/s, admitted={admitted}, rejected={5000 - admitted}, "
                f"webhook p95={flood_latencies[4749] * 1000:.0f} ms, "
                f"drain after flood={monotonic() - drain_started:.1f} s"
            )
        finally:
            hold_api.set()
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay
            if worker.returncode is None:
                worker.kill()
                await worker.wait()
            await user_limiter.close()
            await limits.close()


async def test_queue_pressure_reopens_before_empty(giveaway_db) -> None:
    sessions, giveaway_id = giveaway_db
    async with sessions.begin() as session:
        flow = ParticipationFlow(
            giveaway_id=giveaway_id, user_id=7, user_profile={}, status="waiting"
        )
        session.add(flow)
        await session.flush()
        job = ParticipationJob(
            update_id=uuid4().int % 10**15,
            flow_id=flow.id,
            kind="start",
            received_at=utc_now() - timedelta(seconds=61),
        )
        session.add(job)
        await session.flush()
        job_id = job.id
    pressure = QueuePressure(sessions)
    assert not await pressure.permits_start()
    for age, allowed in [(35, False), (20, True)]:
        async with sessions.begin() as session:
            job = await session.get(ParticipationJob, job_id)
            assert job is not None
            job.received_at = utc_now() - timedelta(seconds=age)
        pressure.next_sample = 0
        assert await pressure.permits_start() is allowed
