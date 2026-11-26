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
        (True, "denied_reissue"),
        (False, "retry"),
        (True, "retry"),
        (True, "abandoned"),
        (False, "transient_move"),
        (True, "transient_move"),
        (True, "transient_start"),
        (True, "transient_restore"),
        (True, "transient_condition"),
        (True, "transient_worker"),
        (True, "live"),
        (True, "mode_socket"),
        (True, "mode_http"),
        (True, "mode_reopen"),
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
        FROG_LIVE_TRANSPORT_ENABLED=failure in ("live", "mode_socket", "mode_reopen"),
        FROG_MODE_SELECTION_ENABLED=failure in ("mode_socket", "mode_http", "mode_reopen"),
        FROG_REOPEN_HARD_GIVEAWAY_ID=1 if failure == "mode_reopen" else 0,
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
                    if not settings.frog_mode_selection_enabled:
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
            if settings.frog_mode_selection_enabled:
                assert 'name="frog-mode-selection" content="enabled"' in html
                if failure == "mode_reopen":
                    await check_reopened_game(
                        client, sessions, settings, bot, memory, clock, gid, init, monkeypatch,
                    )
                    return
                await check_mode_selection(
                    client, sessions, settings, bot, memory, clock, gid, init,
                )
                return
            if failure == "live":
                await check_live_game(
                    client, sessions, settings, bot, memory, clock, gid, init, monkeypatch
                )
                return
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
                    failure in ("expired", "replaced", "finished", "conditions", "denied_reissue")
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
                    if failure in ("conditions", "denied_reissue"):
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
                    assert r.status == (403 if failure in ("conditions", "denied_reissue") else 409)
                    if failure == "denied_reissue":
                        # A completed but permanently denied result remains cached in Redis.
                        old_won = json.loads(memory.data["frog:session:" + token])
                        assert old_won["status"] == "won"
                        async with sessions.begin() as db:
                            pending_denial = await db.scalar(
                                select(ParticipationJob).where(
                                    ParticipationJob.kind == "game_win",
                                )
                            )
                            assert pending_denial is not None
                            # The background worker becomes eligible after its initial delay.
                            assert pending_denial.available_at is not None
                            pending_denial.available_at = None
                            denied_job = pending_denial.id
                        await participation_worker.process_participation_job(
                            sessions,
                            bot,
                            settings,
                            denied_job,
                        )
                        async with sessions() as db:
                            processed = await db.get(ParticipationJob, denied_job)
                            flow = await db.scalar(select(ParticipationFlow))
                            assert processed is not None and processed.processed_at is not None
                            assert flow is not None and flow.status == "done"
                        monkeypatch.setattr(
                            giveaways,
                            "check_participant_conditions",
                            AsyncMock(
                                return_value=ConditionCheck(ok=True, membership_status="member"),
                            ),
                        )
                        # The user rejoins and receives a genuinely new challenge in the same draw.
                        async with sessions.begin() as db:
                            giveaway = await db.get(Giveaway, gid)
                            assert giveaway is not None
                            giveaway.intake_closed_at = None
                            giveaway.deadline_at = utc_now() + timedelta(hours=1)
                            assert (
                                await admit_start(
                                    db,
                                    giveaway_id=gid,
                                    user=User(id=123, is_bot=False, first_name="Тест"),
                                    update_id=100,
                                    received_at=utc_now(),
                                    allow_new=AsyncMock(return_value=True),
                                )
                                == "accepted"
                            )
                            new_job = await db.scalar(
                                select(ParticipationJob.id).where(
                                    ParticipationJob.update_id == 100,
                                )
                            )
                            assert new_job is not None
                        await participation_worker.process_participation_job(
                            sessions,
                            bot,
                            settings,
                            new_job,
                        )
                        async with sessions() as db:
                            flow = await db.scalar(select(ParticipationFlow))
                            assert flow is not None and flow.status == "game"
                            assert flow.challenge_id != old_won["challenge_id"]
                        assert json.loads(memory.data["frog:session:" + token])["status"] == "won"
                        new_attempt = await post("start", giveaway=gid, difficulty="hard")
                        assert new_attempt.status == 200, await new_attempt.text()
                        next_game = await new_attempt.json()
                        assert next_game["status"] == "playing"
                        assert next_game["difficulty"] == "easy"
                        assert next_game["token"] != token
                        assert all("laser" not in lane for lane in next_game["lanes"])
                        assert (
                            await post(
                                "move",
                                token=token,
                                seq=old_won["seq"],
                                nonce=old_won["nonce"],
                                direction="wait",
                            )
                        ).status == 400
                        async with sessions() as db:
                            assert (
                                await db.scalar(
                                    select(func.count()).select_from(HardGameAttempt),
                                )
                                == 1
                            )
                            attempt = await db.get(HardGameAttempt, (gid, 123))
                            assert attempt is not None and attempt.completed_at is None
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


async def check_live_game(
    client, sessions, settings, bot, memory, clock, gid, init, monkeypatch
) -> None:
    import json

    from aiohttp import WSServerHandshakeError

    from tg_drop_bot.services import giveaways, participation_worker
    from tg_drop_bot.services.conditions import ConditionCheck

    url = "/game/live"
    headers = {"Origin": settings.webhook_url}
    with pytest.raises(WSServerHandshakeError) as error:
        await client.ws_connect(url, headers={"Origin": "https://other.example"})
    assert error.value.status == 403

    async def connect():
        socket = await client.ws_connect(url, headers=headers)
        await socket.send_json({"action": "hello", "clientVersion": __version__, "initData": init})
        assert (await socket.receive_json(timeout=3))["type"] == "ready"
        return socket

    async def command(socket, action, **args):
        await socket.send_json({"action": action, **args})
        response = await socket.receive_json(timeout=5)
        assert response["type"] == "state", response
        return response["state"]

    socket = await connect()
    state = await command(socket, "start", giveaway=gid)
    assert state["difficulty"] == "hard"
    assert "nonce" not in state and "token" not in state
    token = memory.data["frog:current:123"]
    key = "frog:session:" + token
    game = json.loads(memory.data[key])
    # Use a deterministic lane-free fixture in the local test only.
    game.update(lanes=[], rows=4)
    memory.data[key] = json.dumps(game)
    clock[0] += 0.4
    first = await command(socket, "move", seq=0)
    assert first["seq"] == 1 and first["y"] == 1
    assert (await command(socket, "move", seq=0))["y"] == 1
    assert (await command(socket, "sync"))["seq"] == 1

    resumed = await connect()
    snapshot = await command(resumed, "resume", giveaway=gid)
    assert snapshot["difficulty"] == "hard" and snapshot["y"] == 1
    assert memory.data["frog:current:123"] == token
    await socket.send_json({"action": "move", "seq": 1})
    assert (await socket.receive_json(timeout=3))["status"] == 409
    await socket.close()

    clock[0] += 0.4
    assert (await command(resumed, "move", seq=1))["y"] == 2
    clock[0] += 0.4
    bot.get_chat_member.side_effect = AssertionError("WebSocket must not wait for Telegram")
    won = await command(resumed, "move", seq=2)
    assert won["status"] == "won" and won["registration"] == "pending"
    assert (await command(resumed, "move", seq=2))["registration"] == "pending"
    assert (await command(resumed, "sync"))["registration"] == "pending"
    async with sessions() as s:
        jobs = (await s.scalars(select(ParticipationJob).where(
            ParticipationJob.kind == "game_win"
        ))).all()
        assert len(jobs) == 1 and jobs[0].available_at is None
        outbox = jobs[0].id
        assert await s.scalar(select(func.count()).select_from(Participant)) == 0
    check = AsyncMock(return_value=ConditionCheck(ok=False, membership_status="check_failed"))
    monkeypatch.setattr(giveaways, "check_participant_conditions", check)
    with pytest.raises(participation_worker.RetryParticipation):
        await participation_worker.process_participation_job(sessions, bot, settings, outbox)
    assert (await command(resumed, "sync"))["registration"] == "pending"
    await resumed.close()
    memory.data.pop("frog:current:123")
    memory.data.pop(key)
    resumed = await connect()
    assert (await command(resumed, "resume", giveaway=gid))["registration"] == "pending"
    check.return_value = ConditionCheck(ok=True, membership_status="member")
    await participation_worker.process_participation_job(sessions, bot, settings, outbox)
    await participation_worker.process_participation_job(sessions, bot, settings, outbox)
    assert (await command(resumed, "sync"))["registration"] == "registered"
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Participant)) == 1
        claim = await s.get(HardGameAttempt, (gid, 123))
        assert claim is not None and claim.completed_at is not None
    await resumed.close()


async def check_mode_selection(client, sessions, settings, bot, memory, clock, gid, init) -> None:
    import json

    from tg_drop_bot.services.participation_worker import process_participation_job

    live = settings.frog_live_transport_enabled
    socket = None

    async def connect():
        ws = await client.ws_connect("/game/live", headers={"Origin": settings.webhook_url})
        await ws.send_json({"action": "hello", "clientVersion": __version__, "initData": init})
        assert (await ws.receive_json(timeout=4))["type"] == "ready"
        return ws

    async def command(action, status=200, **data):
        if socket is not None:
            await socket.send_json({"action": action, **data})
            result = await socket.receive_json(timeout=5)
            if status != 200:
                assert result["type"] == "error" and result["status"] == status, result
                return None
            assert result["type"] == "state", result
            return result["state"]
        async with client.post("/game/api/" + action, json={
            "initData": init, "clientVersion": __version__, **data,
        }) as response:
            assert response.status == status, await response.text()
            return await response.json() if status == 200 else None

    async def finish(state, *, lost=False, pass_checkpoint=False):
        token = memory.data["frog:current:123"]
        key = "frog:session:" + token
        game = json.loads(memory.data[key])
        # Keep this route an integration fixture only; its result still comes from physics.
        dest = game["checkpoint_row"] + 1 if pass_checkpoint else game["y"] + 1
        game.update(rows=dest + 1, lanes=[
            {"row": dest, "count": 1, "kind": "car", "phase": 4, "speed": 0},
        ] if lost else [])
        memory.data[key] = json.dumps(game)
        final = state
        for _ in range(dest - game["y"]):
            clock[0] += 0.4
            auth = {} if live else {
                "token": final["token"], "nonce": final["nonce"], "direction": "up",
            }
            final = await command("move", seq=final["seq"], **auth)
        assert final["status"] == ("lost" if lost else "won")
        return final

    try:
        if live:
            socket = await connect()
        easy = await command("start", giveaway=gid)
        assert easy["difficulty"] == "easy" and easy["hardAttemptsLeft"] == 3
        assert easy["y"] == 0 and not easy["checkpointReached"]
        assert any(lane["kind"] == "mower" for lane in easy["lanes"])
        assert (await finish(easy, lost=True, pass_checkpoint=True))["checkpointReached"]
        easy = await command("start", giveaway=gid, difficulty="easy")
        assert easy["y"] == 3 and easy["checkpointReached"]
        assert any(lane["kind"] == "mower" for lane in easy["lanes"])
        await finish(easy, lost=True)
        easy = await command("start", giveaway=gid, difficulty="easy")
        assert easy["y"] == 3 and easy["checkpointReached"]
        assert all(lane["kind"] != "mower" for lane in easy["lanes"])
        hard = await command("start", giveaway=gid, difficulty="hard")
        assert hard["difficulty"] == "hard" and hard["hardAttemptsLeft"] == 2
        assert hard["y"] == 0 and "checkpointReached" not in hard
        if live:
            assert socket is not None
            await socket.close()
            socket = await connect()
            resumed = await command("resume", giveaway=gid)
            assert resumed["started"] == hard["started"] and resumed["hardAttemptsLeft"] == 2
        else:
            resumed = await command("start", giveaway=gid, difficulty="hard")
            assert resumed["token"] == hard["token"] and resumed["hardAttemptsLeft"] == 2
        easy = await command("start", giveaway=gid, difficulty="easy")
        assert easy["difficulty"] == "easy" and easy["hardAttemptsLeft"] == 2
        won = await finish(easy)
        if live:
            assert won["registration"] == "pending"
            assert (await command("start", giveaway=gid, difficulty="hard"))["difficulty"] == "easy"
            async with sessions() as db:
                job_id = await db.scalar(select(ParticipationJob.id).where(
                    ParticipationJob.kind == "game_win",
                ))
                assert job_id is not None
            await process_participation_job(sessions, bot, settings, job_id)
            assert (await command("sync"))["registration"] == "registered"
        else:
            assert won["registration"] == "registered"
        hard = await command("start", giveaway=gid, difficulty="hard")
        assert hard["hardAttemptsLeft"] == 1 and hard["registration"] == "registered"
        await finish(hard, lost=True)
        hard = await command("start", giveaway=gid, difficulty="hard")
        assert hard["hardAttemptsLeft"] == 0
        if live:
            await finish(hard, lost=True)
            await command("start", giveaway=gid, difficulty="hard", status=409)
        else:
            assert (await finish(hard))["registration"] == "registered"
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Participant)) == 1
            assert await db.scalar(select(func.count()).select_from(ParticipationJob).where(
                ParticipationJob.kind == "game_win",
            )) == 1
            claim = await db.get(HardGameAttempt, (gid, 123))
            assert claim is not None and claim.attempts_used == 3
            assert (claim.completed_at is None) == live
    finally:
        if socket is not None:
            await socket.close()


async def check_reopened_game(
    client, sessions, settings, bot, memory, clock, gid, init, monkeypatch,
) -> None:
    import json

    from redis.exceptions import ConnectionError as RedisConnectionError

    from tg_drop_bot.services.hard_game import claim_hard_attempt, record_hard_completion
    from tg_drop_bot.services.participation_queue import admit_start
    from tg_drop_bot.services.participation_worker import close_flow, process_participation_job

    finished_at = datetime.fromtimestamp(clock[0] - 1, UTC)
    async with sessions.begin() as db:
        db.add(Participant(giveaway_id=gid, user_id=123, joined_at=finished_at,
                           captcha_passed_at=finished_at))
        assert await claim_hard_attempt(db, gid, 123, "legacy")
        await record_hard_completion(db, gid, 123, "legacy", 19000, finished_at)
        flow = await db.scalar(select(ParticipationFlow).where(ParticipationFlow.user_id == 123))
        assert flow is not None
        close_flow(flow)
    async with sessions.begin() as db:
        assert await admit_start(
            db, giveaway_id=gid, user=User(id=123, is_bot=False, first_name="Тест"),
            update_id=3, received_at=utc_now(), allow_new=AsyncMock(return_value=True),
        ) == "accepted"
        job = await db.scalar(select(ParticipationJob.id).where(ParticipationJob.update_id == 3))
        assert job is not None
    await process_participation_job(sessions, bot, settings, job)
    keyboard = bot.send_message.call_args.kwargs["reply_markup"]
    assert f"/game/?giveaway={gid}&ranking=1" in keyboard.inline_keyboard[0][0].web_app.url

    store = frog_web.store_game

    async def write_then_disconnect(*args):
        await store(*args)
        raise RedisConnectionError("lost the reply after the Redis write")

    with monkeypatch.context() as patch:
        patch.setattr(frog_web, "store_game", write_then_disconnect)
        async with client.post("/game/api/start", json={
            "initData": init, "clientVersion": __version__, "giveaway": gid, "difficulty": "hard",
        }) as failed:
            assert failed.status == 503
    async with sessions() as db:
        attempt = await db.get(HardGameAttempt, (gid, 123))
        assert attempt is not None and not attempt.transition_budget_applied

    async with client.ws_connect("/game/live", headers={"Origin": settings.webhook_url}) as ws:
        await ws.send_json({"action": "hello", "clientVersion": __version__, "initData": init})
        assert (await ws.receive_json(timeout=4))["type"] == "ready"
        for remaining in (2, 1, 0):
            await ws.send_json({"action": "start", "giveaway": gid, "difficulty": "hard"})
            state = (await ws.receive_json(timeout=5))["state"]
            assert state["hardAttemptsLeft"] == remaining
            assert state["y"] == 0 and state["registration"] == "registered"
            token = memory.data["frog:current:123"]
            game_key = "frog:session:" + token
            game = json.loads(memory.data[game_key])
            game.update(rows=2, lanes=[])
            memory.data[game_key] = json.dumps(game)
            clock[0] += 0.4
            await ws.send_json({"action": "move", "seq": state["seq"]})
            won = (await ws.receive_json(timeout=5))["state"]
            assert won["status"] == "won" and won["hardAttemptsLeft"] == remaining
        await ws.send_json({"action": "start", "giveaway": gid, "difficulty": "hard"})
        assert (await ws.receive_json(timeout=5))["status"] == 409
    async with sessions() as db:
        score = await db.get(HardGameAttempt, (gid, 123))
        assert score is not None and score.attempts_used == 3
        assert score.completed_at == finished_at and score.duration_ms == 19000
        assert await db.scalar(select(func.count()).select_from(Participant)) == 1
