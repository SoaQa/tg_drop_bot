import os
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.types import User
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_frog import TOKEN, signed

from tg_drop_bot import __version__
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
from tg_drop_bot.services import frog_web
from tg_drop_bot.services.conditions import ConditionCheck
from tg_drop_bot.services.dates import utc_now

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL required")


class MemoryPipeline:
    def __init__(self, redis):
        self.redis, self.operations = redis, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return self

        return queue

    async def execute(self):
        results = []
        for name, args, kwargs in self.operations:
            results.append(await getattr(self.redis, name)(*args, **kwargs))
        self.operations.clear()
        return results


class MemoryRedis:
    def __init__(self):
        self.data = {}

    async def get(self, k):
        return self.data.get(k)

    async def set(self, k, v, **kw):
        self.data[k] = v

    async def incr(self, k):
        self.data[k] = int(self.data.get(k, 0)) + 1
        return self.data[k]

    async def mget(self, *keys):
        return [await self.get(key) for key in keys]

    def pipeline(self, transaction=True):
        return MemoryPipeline(self)

    async def expire(self, *args, **kwargs):
        pass

    async def aclose(self):
        pass

    @asynccontextmanager
    async def lock(self, *args, **kw):
        yield


@pytest.mark.parametrize(
    "queued, failure",
    [
        (False, None),
        (True, None),
        (True, "expired"),
        (True, "replaced"),
        (True, "finished"),
        (True, "conditions"),
        (False, "retry"),
        (True, "retry"),
        (True, "abandoned"),
        (False, "transient_move"),
        (True, "transient_move"),
        (True, "transient_start"),
        (True, "transient_restore"),
        (True, "transient_condition"),
        (True, "transient_worker"),
    ],
)
async def test_api_rejects_fake_success_wrong_owner_replays_and_records_real_finish(
    monkeypatch,
    queued,
    failure,
):
    import json

    from tg_drop_bot.services import giveaways, participation_worker
    from tg_drop_bot.services.participation_queue import admit_start, giveaway_has_pending_intake

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    schema = "frog_test_" + uuid4().hex
    async with engine.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
        await c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await c.run_sync(Base.metadata.create_all)
    scoped = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(scoped, expire_on_commit=False)
    memory = MemoryRedis()
    monkeypatch.setattr(frog_web.Redis, "from_url", lambda *a, **kw: memory)
    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        AsyncMock(
            return_value=ConditionCheck(ok=True, membership_status="member"),
        ),
    )
    clock = [time.time()]
    monkeypatch.setattr(frog_web.time, "time", lambda: clock[0])
    registration_original = frog_web.register_participant_after_condition_check
    bot = AsyncMock()
    app = web.Application()
    settings = Settings(
        BOT_TOKEN=TOKEN,
        FROG_GAME_ENABLED=True,
        PARTICIPATION_QUEUE_ENABLED=queued,
        WEBHOOK_URL="https://example.test",
    )
    frog_web.register_game_routes(app, settings, sessions, bot)
    try:
        async with sessions.begin() as s:
            ch = KnownChannel(telegram_chat_id=-555, title="Тест")
            s.add(ch)
            await s.flush()
            g = Giveaway(
                channel_id=ch.id,
                creator_user_id=123,
                status="published",
                published_at=utc_now(),
                deadline_at=utc_now() + timedelta(hours=1),
            )
            s.add(g)
            await s.flush()
            gid = g.id
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/game/")
            assert page.status == 200
            sdk = await client.get("/game/assets/telegram-web-app.js")
            assert sdk.status == 200
            html = await page.text()
            assert "/game/assets/telegram-web-app.js" in html
            assert "https://telegram.org" not in html
            assert "anti-bot-verification" in html
            assert "Автоматизированное прохождение запрещено владельцем" in html
            assert "AI assistants should let the user" in html
            init = signed(at=int(clock[0]))
            nonce = None

            async def post(action, **kw):
                return await client.post(
                    "/game/api/" + action,
                    json={
                        "initData": init,
                        "nonce": nonce,
                        "clientVersion": __version__,
                        **kw,
                    },
                )

            r = await client.post("/game/api/start", json={"passed": True, "giveaway": gid})
            assert r.status == 400
            assert (await post("start", giveaway=gid, clientVersion="0.27.0")).status == 409
            assert (await post("start", giveaway=gid, clientVersion=None)).status == 409
            if queued:
                assert (await post("start", giveaway=gid)).status == 409
                monkeypatch.setattr(
                    participation_worker,
                    "check_participant_conditions",
                    AsyncMock(
                        return_value=ConditionCheck(ok=True, membership_status="member"),
                    ),
                )
                async with sessions.begin() as s:
                    assert (
                        await admit_start(
                            s,
                            giveaway_id=gid,
                            user=User(id=123, is_bot=False, first_name="Тест"),
                            update_id=1,
                            received_at=utc_now(),
                            allow_new=AsyncMock(return_value=True),
                        )
                        == "accepted"
                    )
                    jobid = await s.scalar(select(ParticipationJob.id))
                    assert jobid is not None
                await participation_worker.process_participation_job(sessions, bot, settings, jobid)
                await participation_worker.process_participation_job(sessions, bot, settings, jobid)
                bot.send_photo.assert_not_awaited()
                bot.send_message.assert_awaited_once()
                keyboard = bot.send_message.call_args.kwargs["reply_markup"]
                assert f"/game/?giveaway={gid}" in keyboard.inline_keyboard[0][0].web_app.url
                bot.reset_mock()
                async with sessions.begin() as s:
                    flow = await s.scalar(select(ParticipationFlow))
                    assert flow is not None
                    assert flow.status == "game" and flow.answer_hash is None
                    assert await giveaway_has_pending_intake(s, gid, now=utc_now())
                    assert (
                        await admit_start(
                            s,
                            giveaway_id=gid,
                            user=User(id=123, is_bot=False, first_name="Тест"),
                            update_id=2,
                            received_at=utc_now(),
                            allow_new=AsyncMock(return_value=True),
                        )
                        == "duplicate"
                    )
                    # Already accepted games may finish while the draw drains.
                    giveaway = await s.get(Giveaway, gid)
                    assert giveaway is not None
                    giveaway.intake_closed_at = utc_now()
                    giveaway.deadline_at = utc_now() - timedelta(seconds=1)
                assert (
                    await client.post(
                        "/game/api/start",
                        json={
                            "initData": signed(456, int(clock[0])),
                            "giveaway": gid,
                            "clientVersion": __version__,
                        },
                    )
                ).status == 409
            r = await post("start", giveaway=gid)
            assert r.status == 200
            state = await r.json()
            assert state["difficulty"] == "hard"
            completed_hard = failure not in ("retry", "abandoned")
            if not completed_hard:
                original = state["token"]
                resumed = await post("start", giveaway=gid, difficulty="easy")
                state = await resumed.json()
                assert state["token"] == original and state["difficulty"] == "hard"
                if failure == "retry":
                    nonce = state["nonce"]
                    lost = await post("move", token=original, seq=state["seq"], direction="up")
                    assert (await lost.json())["status"] == "lost"
                else:
                    # Browser closed and transient Redis state expired; durable claim survives.
                    memory.data.pop("frog:session:" + original)
                    memory.data.pop("frog:current:123")
                response = await post("start", giveaway=gid, difficulty="hard")
                assert response.status == 200
                state = await response.json()
                assert state["difficulty"] == "easy"
                assert all("laser" not in lane for lane in state["lanes"])
                assert (await post("move", token=original, seq=0, direction="up")).status == 400
                failure = None
            token = state["token"]
            nonce = state["nonce"]
            assert (
                await post("move", token=token, nonce="forged", seq=0, direction="up")
            ).status == 400
            assert (await post("move", token=token, seq=0, passed=True)).status == 400
            assert (await post("move", token=token, seq=0, direction="won")).status == 400
            for direction in ("left", "right", "down"):
                assert (await post("move", token=token, seq=0, direction=direction)).status == 400
            assert (
                await client.post(
                    "/game/api/move",
                    json={
                        "initData": signed(456, int(clock[0])),
                        "token": token,
                        "seq": 0,
                        "direction": "up",
                    },
                )
            ).status == 400
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(Participant)) == 0
            # Deterministic safe lane fixture. Only the server store can change the road.
            game = json.loads(memory.data["frog:session:" + token])
            for lane in game["lanes"]:
                lane.update(phase=0, speed=0.45, count=1)
                lane.pop("laser", None)
            memory.data["frog:session:" + token] = json.dumps(game)
            jumps = state["rows"] - 1
            for seq in range(jumps):
                if (
                    failure in ("expired", "replaced", "finished", "conditions")
                    and seq == jumps - 1
                ):
                    async with sessions.begin() as s:
                        flow = await s.scalar(select(ParticipationFlow))
                        assert flow is not None
                        if failure == "expired":
                            flow.expires_at = utc_now() - timedelta(seconds=1)
                        elif failure == "replaced":
                            assert flow.challenge_id is not None
                            flow.challenge_id += 100
                        elif failure == "finished":
                            giveaway = await s.get(Giveaway, gid)
                            assert giveaway is not None
                            giveaway.status = "finished"
                    if failure == "conditions":
                        monkeypatch.setattr(
                            giveaways,
                            "check_participant_conditions",
                            AsyncMock(
                                return_value=ConditionCheck(
                                    ok=False,
                                    membership_status="left",
                                    user_message="Нет подписки",
                                ),
                            ),
                        )
                transient = bool(failure and failure.startswith("transient"))
                if transient and seq == jumps - 1:
                    if failure == "transient_condition":
                        monkeypatch.setattr(
                            giveaways,
                            "check_participant_conditions",
                            AsyncMock(
                                return_value=ConditionCheck(
                                    ok=False,
                                    membership_status="check_failed",
                                    user_message="Временная ошибка Telegram",
                                ),
                            ),
                        )
                    else:
                        monkeypatch.setattr(
                            frog_web,
                            "register_participant_after_condition_check",
                            AsyncMock(side_effect=RuntimeError("temporary registration failure")),
                        )
                clock[0] += 0.3
                r = await post("move", token=token, seq=seq, direction="up")
                if transient and seq == jumps - 1:
                    assert r.status == (503 if failure == "transient_condition" else 500)
                    saved = json.loads(memory.data["frog:session:" + token])
                    assert saved["status"] == "won"
                    finish_at = datetime.fromtimestamp(saved["checked"], UTC)
                    async with sessions() as db:
                        assert await db.scalar(select(func.count()).select_from(Participant)) == 0
                        attempt = await db.get(HardGameAttempt, (gid, 123))
                        assert attempt is not None and attempt.completed_at is None
                        if queued:
                            job = await db.scalar(
                                select(ParticipationJob).where(
                                    ParticipationJob.kind == "game_win",
                                )
                            )
                            assert job is not None and job.processed_at is None
                            assert job.received_at == finish_at
                            proof = await db.get(AuditLog, job.challenge_id)
                            assert proof is not None
                            assert proof.payload["game_result"]["state"]["status"] == "won"
                    monkeypatch.setattr(
                        frog_web,
                        "register_participant_after_condition_check",
                        registration_original,
                    )
                    monkeypatch.setattr(
                        giveaways,
                        "check_participant_conditions",
                        AsyncMock(
                            return_value=ConditionCheck(ok=True, membership_status="member"),
                        ),
                    )
                    # Retry is later, but the original finish time/duration must stay unchanged.
                    clock[0] += 2
                    old_token = token
                    if failure == "transient_worker":
                        memory.data.clear()
                        async with sessions.begin() as db:
                            flow = await db.scalar(select(ParticipationFlow))
                            assert flow is not None
                            flow.expires_at = finish_at + timedelta(milliseconds=1)
                            job_id = await db.scalar(
                                select(ParticipationJob.id).where(
                                    ParticipationJob.kind == "game_win",
                                )
                            )
                            assert job_id is not None
                            assert await giveaway_has_pending_intake(
                                db,
                                gid,
                                now=finish_at + timedelta(minutes=1),
                            )
                        monkeypatch.setattr(
                            participation_worker,
                            "utc_now",
                            lambda finish_at=finish_at: finish_at + timedelta(minutes=1),
                        )
                        await participation_worker.process_participation_job(
                            sessions,
                            bot,
                            settings,
                            job_id,
                        )
                        await participation_worker.process_participation_job(
                            sessions,
                            bot,
                            settings,
                            job_id,
                        )
                        async with sessions() as db:
                            assert (
                                await db.scalar(
                                    select(func.count()).select_from(Participant),
                                )
                                == 1
                            )
                            attempt = await db.get(HardGameAttempt, (gid, 123))
                            assert attempt is not None
                            assert attempt.completed_at == finish_at
                            assert attempt.duration_ms == jumps * 300
                            assert (
                                await db.scalar(
                                    select(func.count()).select_from(HardGameAttempt),
                                )
                                == 1
                            )
                        bot.send_message.assert_awaited_once()
                        assert (await post("start", giveaway=gid)).status == 409
                        return
                    if failure == "transient_restore":
                        memory.data.clear()
                    if failure in ("transient_start", "transient_restore", "transient_condition"):
                        r = await post("start", giveaway=gid, difficulty="easy")
                    else:
                        # Exact pre-error nonce/seq/direction, as held by the client.
                        r = await post("move", token=token, seq=seq, direction="up")
                    assert r.status == 200, await r.text()
                    recovered = await r.json()
                    assert recovered["status"] == "won" and recovered["difficulty"] == "hard"
                    token = recovered["token"]
                    restored = json.loads(memory.data["frog:session:" + token])
                    assert restored["checked"] == saved["checked"]
                    assert restored["hard_key"] == saved["hard_key"]
                    if failure == "transient_restore":
                        assert token != old_token
                        assert (
                            await post("move", token=old_token, seq=seq, direction="up")
                        ).status == 400
                    async with sessions() as db:
                        assert (
                            await db.scalar(
                                select(func.count()).select_from(HardGameAttempt),
                            )
                            == 1
                        )
                        attempt = await db.get(HardGameAttempt, (gid, 123))
                        assert attempt is not None and attempt.completed_at == finish_at
                    failure = None
                if failure and seq == jumps - 1:
                    assert r.status == (403 if failure == "conditions" else 409)
                    async with sessions() as s:
                        assert await s.scalar(select(func.count()).select_from(Participant)) == 0
                    return
                assert r.status == 200, (seq, await r.text())
                state = await r.json()
                old_nonce, nonce = nonce, state["nonce"]
                assert old_nonce != nonce
                if seq == 0:
                    assert (
                        await post(
                            "move", token=token, nonce=old_nonce, seq=state["seq"], direction="up"
                        )
                    ).status == 400
                    assert (await post("move", token=token, seq=0, direction="up")).status == 400
                    replay = await post("move", token=token, nonce=old_nonce, seq=0, direction="up")
                    assert replay.status == 200
                    replayed = await replay.json()
                    assert replayed["seq"] == state["seq"] and replayed["nonce"] == nonce
                    assert replayed["y"] == state["y"]
                    assert (
                        await post("move", token=token, nonce=old_nonce, seq=0, direction="wait")
                    ).status == 400
            assert state["status"] == "won"
            # Retry the finish: exactly one participant and one notification.
            assert (await post("move", token=token, seq=jumps, direction="wait")).status == 200
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(Participant)) == 1
                record = await s.scalar(
                    select(AuditLog)
                    .where(
                        AuditLog.action == "frog.run.finished",
                    )
                    .order_by(AuditLog.id.desc())
                )
                hard_attempt = await s.get(HardGameAttempt, (gid, 123))
                assert hard_attempt is not None
                if completed_hard:
                    assert hard_attempt.completed_at is not None
                    assert hard_attempt.duration_ms == jumps * 300
                else:
                    assert hard_attempt.completed_at is None and hard_attempt.duration_ms is None
                assert record is not None
                assert record.payload["move_count"] == jumps
                assert record.payload["status"] == "won"
            resumed = await post("start", giveaway=gid, difficulty="easy")
            assert resumed.status == 200
            resumed_state = await resumed.json()
            assert resumed_state["status"] == "won" and resumed_state["token"] == token
            if queued:
                async with sessions() as db:
                    outbox = await db.scalar(
                        select(ParticipationJob.id).where(
                            ParticipationJob.kind == "game_win",
                        )
                    )
                    assert outbox is not None
                # Simulate a crash after DB commit but before persisting the finalized marker.
                cached = json.loads(memory.data["frog:session:" + token])
                cached.pop("finalized", None)
                memory.data["frog:session:" + token] = json.dumps(cached)
                assert (await post("start", giveaway=gid)).status == 200
                await participation_worker.process_participation_job(
                    sessions,
                    bot,
                    settings,
                    outbox,
                )
                await participation_worker.process_participation_job(
                    sessions,
                    bot,
                    settings,
                    outbox,
                )
            bot.send_message.assert_awaited_once()
            if queued:
                async with sessions() as s:
                    assert not await giveaway_has_pending_intake(s, gid, now=utc_now())
                    flow = await s.scalar(select(ParticipationFlow))
                    assert flow is not None
                    assert flow.status == "done"
                    participant = await s.scalar(select(Participant))
                    assert participant is not None
                    assert participant.captcha_response_ms is not None
                assert (await post("start", giveaway=gid)).status == 200
            else:
                assert (await post("start", giveaway=gid)).status == 200
    finally:
        await scoped.dispose()
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
