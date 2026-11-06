from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from time import monotonic

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import ParticipationJob
from tg_drop_bot.services.dates import utc_now

TOKEN_BUCKET = """
local time = redis.call('TIME')
local now = time[1] * 1000 + math.floor(time[2] / 1000)
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'time')
local tokens = math.min(capacity, (tonumber(bucket[1]) or capacity)
  + math.max(0, now - (tonumber(bucket[2]) or now)) * rate / 1000)
if tokens < 1 then return 0 end
redis.call('HSET', KEYS[1], 'tokens', tokens - 1, 'time', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(1000 * capacity / rate) + 1000)
return 1
"""
EXTEND_PAUSE = """
if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('PSETEX', KEYS[1], ARGV[1], '1')
end
return 1
"""
QUEUE_NOTICE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= 200 then return 0 end
if not redis.call('SET', KEYS[2], '1', 'NX', 'EX', 120) then return 0 end
return redis.call('ZADD', KEYS[1], ARGV[2], ARGV[3])
"""
PAUSE_KEY = "participation:telegram_pause"
NOTICES_KEY = "participation:pending_notices"
logger = logging.getLogger(__name__)


class ParticipationLimits:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis = Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)

    async def close(self) -> None:
        await self.redis.aclose()

    async def take(self, key: str, rate: int, burst: int) -> bool:
        return bool(await self.redis.eval(TOKEN_BUCKET, 1, f"participation:{key}", rate, burst))

    async def admit_start(self) -> bool:
        return await self.take(
            "ingress",
            self.settings.participation_queue_rate,
            self.settings.participation_queue_burst,
        )

    async def allow_notice(self, user_id: int) -> bool:
        if await self.redis.exists(PAUSE_KEY):
            return False
        key = f"participation:notice:{user_id}"
        if await self.redis.exists(key) or not await self.take("notices", 2, 4):
            return False
        return bool(await self.redis.set(key, "1", nx=True, ex=120))

    async def queue_notice(self, user_id: int, text: str) -> None:
        now = utc_now().timestamp()
        await self.redis.eval(
            QUEUE_NOTICE,
            2,
            NOTICES_KEY,
            f"participation:notice:{user_id}",
            now,
            now + 120,
            json.dumps([user_id, text]),
        )

    async def deliver_notices(self, bot: Bot) -> None:
        while True:
            try:
                if (
                    not await self.redis.exists(PAUSE_KEY)
                    and await self.redis.zcard(NOTICES_KEY)
                    and await self.take("notices", 2, 4)
                ):
                    notices = await self.redis.zpopmin(NOTICES_KEY)
                    if notices:
                        raw, expires = notices[0]
                        if expires > utc_now().timestamp():
                            user_id, message = json.loads(raw)
                            try:
                                await bot.send_message(user_id, message, request_timeout=5)
                            except TelegramRetryAfter as error:
                                await self.pause_telegram(error.retry_after)
                                await self.redis.zadd(NOTICES_KEY, {raw: expires})
                            except TelegramAPIError:
                                logger.debug("Could not deliver participation overload notice")
            except Exception:
                logger.exception("Could not drain participation overload notices")
                await asyncio.sleep(1)
            await asyncio.sleep(0.25)

    async def allow_captcha_callback(self, user_id: int) -> bool:
        return bool(
            await self.redis.set(
                f"participation:refresh:{user_id}",
                "1",
                nx=True,
                ex=self.settings.rate_limit_participate_seconds,
            )
        )

    async def pause_telegram(self, seconds: int) -> None:
        await self.redis.eval(EXTEND_PAUSE, 1, PAUSE_KEY, max(seconds, 1) * 1000)

    async def wait_outbound(self) -> int:
        """Return remaining global pause in seconds, or acquire a send token."""
        while True:
            remaining_ms = await self.redis.pttl(PAUSE_KEY)
            if remaining_ms > 0:
                return (remaining_ms + 999) // 1000
            elif await self.take("outbound", 18, 4):
                return 0
            else:
                await asyncio.sleep(0.06)


class QueuePressure:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.next_sample = 0.0
        self.overloaded = False
        self.lock = asyncio.Lock()

    async def permits_start(self) -> bool:
        if monotonic() < self.next_sample:
            return not self.overloaded
        async with self.lock:
            if monotonic() < self.next_sample:
                return not self.overloaded
            now = utc_now()
            async with self.sessions() as session:
                pending, oldest = (
                    await session.execute(
                        select(func.count(), func.min(ParticipationJob.received_at)).where(
                            ParticipationJob.kind == "start",
                            ParticipationJob.processed_at.is_(None),
                        )
                    )
                ).one()
                recent = (
                    await session.scalar(
                        select(func.count())
                        .select_from(ParticipationJob)
                        .where(
                            ParticipationJob.kind == "start",
                            ParticipationJob.processed_at >= now - timedelta(seconds=30),
                        )
                    )
                    or 0
                )
            age = max(0, (now - oldest).total_seconds()) if oldest else 0
            # An idle window is not evidence of a slow worker. Use measured throughput
            # once there has been a sustained backlog, with a conservative initial estimate.
            throughput = max(1, recent / 30) if age >= 30 else 9
            wait = max(age, pending / throughput)
            overloaded = wait >= 30 if self.overloaded else wait >= 60
            if overloaded != self.overloaded:
                logger.info(
                    "Participation intake overloaded=%s pending=%s estimated_wait=%.1fs",
                    overloaded,
                    pending,
                    wait,
                )
            self.overloaded = overloaded
            self.next_sample = monotonic() + 1
            return not self.overloaded
