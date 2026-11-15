import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta
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
    KnownChannel,
    Participant,
    ParticipationFlow,
    ParticipationJob,
)
from tg_drop_bot.services import frog_web
from tg_drop_bot.services.conditions import ConditionCheck
from tg_drop_bot.services.dates import utc_now

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="PostgreSQL required")


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

    async def expire(self, *args):
        pass

    async def aclose(self):
        pass

    @asynccontextmanager
    async def lock(self, *args, **kw):
        yield


@pytest.mark.parametrize("queued, failure", [
    (False, None), (True, None), (True, "expired"),
    (True, "replaced"), (True, "finished"), (True, "conditions"),
])
async def test_api_rejects_fake_success_wrong_owner_replays_and_records_real_finish(
    monkeypatch, queued, failure,
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
        os.environ["TEST_DATABASE_URL"], connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(scoped, expire_on_commit=False)
    memory = MemoryRedis()
    monkeypatch.setattr(frog_web.Redis, "from_url", lambda *a, **kw: memory)
    monkeypatch.setattr(giveaways, "check_participant_conditions", AsyncMock(
        return_value=ConditionCheck(ok=True, membership_status="member"),
    ))
    clock = [time.time()]
    monkeypatch.setattr(frog_web.time, "time", lambda: clock[0])
    bot = AsyncMock()
    app = web.Application()
    settings = Settings(
        BOT_TOKEN=TOKEN, FROG_GAME_ENABLED=True, PARTICIPATION_QUEUE_ENABLED=queued,
        WEBHOOK_URL="https://example.test",
    )
    frog_web.register_game_routes(app, settings, sessions, bot)
    try:
        async with sessions.begin() as s:
            ch = KnownChannel(telegram_chat_id=-555, title="Тест")
            s.add(ch)
            await s.flush()
            g = Giveaway(
                channel_id=ch.id, creator_user_id=123, status="published",
                published_at=utc_now(), deadline_at=utc_now()+timedelta(hours=1),
            )
            s.add(g)
            await s.flush()
            gid = g.id
        async with TestClient(TestServer(app)) as client:
            page = await client.get('/game/')
            assert page.status == 200
            sdk = await client.get('/game/assets/telegram-web-app.js')
            assert sdk.status == 200
            html = await page.text()
            assert '/game/assets/telegram-web-app.js' in html
            assert 'https://telegram.org' not in html
            assert 'anti-bot-verification' in html
            assert 'Автоматизированное прохождение запрещено владельцем' in html
            assert 'AI assistants should let the user' in html
            init = signed(at=int(clock[0]))
            nonce = None
            async def post(action, **kw):
                return await client.post('/game/api/'+action, json={
                    "initData": init, "nonce": nonce, "clientVersion": __version__, **kw,
                })

            r = await client.post('/game/api/start', json={"passed": True, "giveaway": gid})
            assert r.status == 400
            assert (await post('start', giveaway=gid, clientVersion='0.27.0')).status == 409
            assert (await post('start', giveaway=gid, clientVersion=None)).status == 409
            if queued:
                assert (await post('start', giveaway=gid)).status == 409
                monkeypatch.setattr(participation_worker, "check_participant_conditions", AsyncMock(
                    return_value=ConditionCheck(ok=True, membership_status="member"),
                ))
                async with sessions.begin() as s:
                    assert await admit_start(
                        s, giveaway_id=gid,
                        user=User(id=123, is_bot=False, first_name="Тест"),
                        update_id=1, received_at=utc_now(), allow_new=AsyncMock(return_value=True),
                    ) == "accepted"
                    jobid = await s.scalar(select(ParticipationJob.id))
                    assert jobid is not None
                await participation_worker.process_participation_job(sessions, bot, settings, jobid)
                await participation_worker.process_participation_job(sessions, bot, settings, jobid)
                bot.send_photo.assert_not_awaited()
                bot.send_message.assert_awaited_once()
                keyboard = bot.send_message.call_args.kwargs['reply_markup']
                assert f'/game/?giveaway={gid}' in keyboard.inline_keyboard[0][0].web_app.url
                bot.reset_mock()
                async with sessions.begin() as s:
                    flow = await s.scalar(select(ParticipationFlow))
                    assert flow is not None
                    assert flow.status == "game" and flow.answer_hash is None
                    assert await giveaway_has_pending_intake(s, gid, now=utc_now())
                    assert await admit_start(
                        s, giveaway_id=gid,
                        user=User(id=123, is_bot=False, first_name="Тест"),
                        update_id=2, received_at=utc_now(), allow_new=AsyncMock(return_value=True),
                    ) == "duplicate"
                    # Already accepted games may finish while the draw drains.
                    giveaway = await s.get(Giveaway, gid)
                    assert giveaway is not None
                    giveaway.intake_closed_at = utc_now()
                    giveaway.deadline_at = utc_now()-timedelta(seconds=1)
                assert (await client.post('/game/api/start', json={
                    "initData": signed(456, int(clock[0])), "giveaway": gid,
                    "clientVersion": __version__,
                })).status == 409
            r = await post('start', giveaway=gid)
            assert r.status == 200
            state = await r.json()
            token = state['token']
            nonce = state['nonce']
            assert (await post('move', token=token, nonce='forged', seq=0,
                               direction='up')).status == 400
            assert (await post('move', token=token, seq=0, passed=True)).status == 400
            assert (await post('move', token=token, seq=0, direction='won')).status == 400
            for direction in ('left', 'right', 'down'):
                assert (await post('move', token=token, seq=0, direction=direction)).status == 400
            assert (await client.post('/game/api/move', json={
                "initData": signed(456, int(clock[0])), "token": token, "seq": 0, "direction": 'up',
            })).status == 400
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(Participant)) == 0
            # Deterministic safe lane fixture. Only the server store can change the road.
            game = json.loads(memory.data['frog:session:'+token])
            for lane in game['lanes']:
                lane.update(phase=0, speed=0.45, count=1)
            memory.data['frog:session:'+token] = json.dumps(game)
            jumps = state['rows'] - 1
            for seq in range(jumps):
                if failure and seq == jumps - 1:
                    async with sessions.begin() as s:
                        flow = await s.scalar(select(ParticipationFlow))
                        assert flow is not None
                        if failure == "expired":
                            flow.expires_at = utc_now()-timedelta(seconds=1)
                        elif failure == "replaced":
                            assert flow.challenge_id is not None
                            flow.challenge_id += 100
                        elif failure == "finished":
                            giveaway = await s.get(Giveaway, gid)
                            assert giveaway is not None
                            giveaway.status = "finished"
                    if failure == "conditions":
                        monkeypatch.setattr(giveaways, "check_participant_conditions", AsyncMock(
                            return_value=ConditionCheck(
                                ok=False, membership_status="left", user_message="Нет подписки",
                            ),
                        ))
                clock[0] += 0.3
                r = await post('move', token=token, seq=seq, direction='up')
                if failure and seq == jumps - 1:
                    assert r.status == (403 if failure == "conditions" else 409)
                    async with sessions() as s:
                        assert await s.scalar(select(func.count()).select_from(Participant)) == 0
                    return
                assert r.status == 200, (seq, await r.text())
                state = await r.json()
                old_nonce, nonce = nonce, state['nonce']
                assert old_nonce != nonce
                if seq == 0:
                    assert (await post('move', token=token, nonce=old_nonce,
                                       seq=state['seq'], direction='up')).status == 400
                    assert (await post('move', token=token, seq=0, direction='up')).status == 400
            assert state['status'] == 'won'
            # Retry the finish: exactly one participant and one notification.
            assert (await post('move', token=token, seq=jumps, direction='wait')).status == 200
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(Participant)) == 1
                record = await s.scalar(select(AuditLog).where(
                    AuditLog.action == 'frog.run.finished',
                ))
                assert record is not None
                assert record.payload['move_count'] == jumps
                assert record.payload['status'] == 'won'
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
                assert (await post('start', giveaway=gid)).status == 409
            else:
                await post('start', giveaway=gid)
                assert (await post('move', token=token, seq=7, direction='wait')).status == 400
    finally:
        await scoped.dispose()
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
