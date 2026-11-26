"""Opt-in transport benchmark with isolated PostgreSQL and Redis, never Telegram."""

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta
from statistics import quantiles
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_frog import TOKEN, signed

from tg_drop_bot import __version__
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Base, Giveaway, KnownChannel, ParticipationFlow
from tg_drop_bot.services import frog_web
from tg_drop_bot.services.dates import utc_now

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FROG_LOAD_TEST") != "1"
    or not os.getenv("TEST_DATABASE_URL")
    or not os.getenv("TEST_QUEUE_REDIS_URL"),
    reason="Set RUN_FROG_LOAD_TEST=1, TEST_DATABASE_URL and TEST_QUEUE_REDIS_URL",
)


@pytest.mark.parametrize("live", [False, True], ids=["http", "websocket"])
async def test_one_hundred_concurrent_games(live: bool) -> None:
    url = os.environ["TEST_DATABASE_URL"]
    schema = f"frog_load_{uuid4().hex}"
    engine = create_async_engine(url)
    scoped = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)
    try:
        sessions = async_sessionmaker(scoped, expire_on_commit=False)
        async with sessions.begin() as session:
            channel = KnownChannel(telegram_chat_id=-100123, title="Нагрузочная проверка")
            session.add(channel)
            await session.flush()
            game = Giveaway(
                channel_id=channel.id, creator_user_id=1, status="published",
                deadline_at=utc_now() + timedelta(hours=1),
            )
            session.add(game)
            await session.flush()
            gid = game.id
            owner = 1_000_000 + (uuid4().int % 10**9) * 100
            session.add_all([
                ParticipationFlow(
                    giveaway_id=gid, user_id=owner + i, status="game",
                    user_profile={}, challenge_id=i + 1,
                    expires_at=utc_now() + timedelta(minutes=5),
                )
                for i in range(100)
            ])
        settings = Settings(
            BOT_TOKEN=TOKEN, WEBHOOK_URL="https://example.test",
            REDIS_URL=os.environ["TEST_QUEUE_REDIS_URL"],
            FROG_GAME_ENABLED=True, FROG_LIVE_TRANSPORT_ENABLED=live,
            PARTICIPATION_QUEUE_ENABLED=True,
        )
        app = web.Application()
        frog_web.register_game_routes(app, settings, sessions, AsyncMock())
        async with TestClient(TestServer(app)) as client, AsyncExitStack() as stack:
            starts = []
            states = {}
            step_latency = []
            gate = asyncio.Event()

            async def client_flow(i: int) -> None:
                uid = owner + i
                auth = {"initData": signed(uid, int(time.time())), "clientVersion": __version__}
                if live:
                    socket = await client.ws_connect(
                        "/game/live", headers={"Origin": settings.webhook_url}
                    )
                    stack.push_async_callback(socket.close)
                    await socket.send_json(auth | {"action": "hello"})
                    assert (await socket.receive_json(timeout=10))["type"] == "ready"

                async def call(action, **data):
                    if live:
                        await socket.send_json({"action": action, **data})
                        reply = await socket.receive_json(timeout=10)
                        if reply["type"] == "error":
                            return reply["status"], reply
                        return 200, reply["state"]
                    async with client.post(f"/game/api/{action}", json=auth | data) as response:
                        return response.status, await (
                            response.json() if response.ok else response.text()
                        )

                start = time.perf_counter()
                retries = 0
                while True:
                    status, state = await call("start", giveaway=gid)
                    if status == 200:
                        break
                    assert status in (429, 503) and retries < 5, (status, state)
                    retries += 1
                    await asyncio.sleep(0.5 + i / 500)
                starts.append((time.perf_counter() - start, retries))
                assert state["difficulty"] == "hard"
                states[uid] = state
                await gate.wait()

                for step in range(5):
                    # 100 clients each offer one move every 290 ms, staggered across the cycle.
                    await asyncio.sleep(i * 0.0029 if step == 0 else 0.29)
                    kwargs = {"seq": state["seq"]}
                    if not live:
                        kwargs.update(token=state["token"], nonce=state["nonce"], direction="up")
                    start = time.perf_counter()
                    status, state = await call("move", **kwargs)
                    step_latency.append(time.perf_counter() - start)
                    assert status == 200 and state["status"] == "playing"

            tasks = [asyncio.create_task(client_flow(i)) for i in range(100)]
            try:
                async with asyncio.timeout(25):
                    while len(states) != 100:
                        for task in tasks:
                            if task.done():
                                task.result()
                        await asyncio.sleep(0.02)
                # Exercise the genuine level generator above. Use an isolated, lane-free
                # level here so the same movement workload runs on both transports.
                redis = frog_web.Redis.from_url(settings.redis_url, decode_responses=True)
                async with redis:
                    for uid in states:
                        token = await redis.get(f"frog:current:{uid}")
                        key = f"frog:session:{token}"
                        state = json.loads(await redis.get(key))
                        state.update(lanes=[], rows=20)
                        await redis.set(key, json.dumps(state), keepttl=True)
                gate.set()
                async with asyncio.timeout(20):
                    await asyncio.gather(*tasks)
                start_p95 = quantiles([sample[0] for sample in starts], n=100)[94] * 1000
                move_p95 = quantiles(step_latency, n=100)[94] * 1000
                print(
                    f"100 players, {len(step_latency)} moves, {sum(s[1] for s in starts)} retries; "
                    f"start p95={start_p95:.1f} ms, move p95={move_p95:.1f} ms"
                )
                assert len(step_latency) == 500
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await scoped.dispose()
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
