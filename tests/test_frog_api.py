import os
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_frog import TOKEN, signed

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog, Base, Giveaway, KnownChannel, Participant
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


async def test_api_rejects_fake_success_wrong_owner_replays_and_records_real_finish(monkeypatch):
    import json

    from tg_drop_bot.services import giveaways

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
    frog_web.register_game_routes(app, Settings(BOT_TOKEN=TOKEN), sessions, bot)
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
            init = signed(at=int(clock[0]))
            async def post(action, **kw):
                return await client.post('/test/api/game/'+action, json={"initData": init, **kw})

            r = await client.post('/test/api/game/start', json={"passed": True, "giveaway": gid})
            assert r.status == 400
            r = await post('start', giveaway=gid)
            assert r.status == 200
            state = await r.json()
            token = state['token']
            assert (await post('move', token=token, seq=0, passed=True)).status == 400
            assert (await post('move', token=token, seq=0, direction='won')).status == 400
            for direction in ('left', 'right', 'down'):
                assert (await post('move', token=token, seq=0, direction=direction)).status == 400
            assert (await client.post('/test/api/game/move', json={
                "initData": signed(456, int(clock[0])), "token": token, "seq": 0, "direction": 'up',
            })).status == 400
            async with sessions() as s:
                assert await s.scalar(select(func.count()).select_from(Participant)) == 0
            # Deterministic safe lane fixture. Only the server store can change the road.
            game = json.loads(memory.data['frog:session:'+token])
            for lane in game['lanes']:
                lane.update(phase=0, speed=0.45)
            memory.data['frog:session:'+token] = json.dumps(game)
            jumps = state['rows'] - 1
            for seq in range(jumps):
                clock[0] += 0.3
                r = await post('move', token=token, seq=seq, direction='up')
                assert r.status == 200, (seq, await r.text())
                state = await r.json()
                if seq == 0:
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
            await post('start', giveaway=gid)
            assert (await post('move', token=token, seq=7, direction='wait')).status == 400
    finally:
        await scoped.dispose()
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
