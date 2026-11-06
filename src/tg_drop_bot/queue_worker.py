from __future__ import annotations

import asyncio
import logging
from functools import cache

import dramatiq
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods.base import Response, TelegramMethod, TelegramType
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import AsyncIO
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.session import create_session_factory
from tg_drop_bot.services.participation_limits import ParticipationLimits
from tg_drop_bot.services.participation_queue import lease_jobs_for_dispatch
from tg_drop_bot.services.participation_worker import (
    RetryParticipation,
    defer_participation_job,
    process_participation_job,
)

logger = logging.getLogger(__name__)
settings = Settings()
broker = RedisBroker(
    url=settings.redis_url,
    namespace="giveaway",
    socket_timeout=2,
    socket_connect_timeout=2,
)
broker.add_middleware(AsyncIO())


class WorkerRequestBudget(BaseRequestMiddleware):
    def __init__(self, limits: ParticipationLimits) -> None:
        self.limits = limits

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        pause = await self.limits.wait_outbound()
        if pause:
            raise RetryParticipation(pause)
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as error:
            await self.limits.pause_telegram(error.retry_after)
            raise RetryParticipation(error.retry_after) from error
        except (TelegramNetworkError, TelegramServerError) as error:
            raise RetryParticipation() from error


@cache
def worker_runtime() -> tuple[async_sessionmaker[AsyncSession], Bot]:
    session = AiohttpSession(timeout=10)
    session.middleware(WorkerRequestBudget(ParticipationLimits(settings)))
    return create_session_factory(settings), Bot(
        settings.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )


async def run_job(job_id: int) -> None:
    sessions, bot = worker_runtime()
    try:
        await process_participation_job(sessions, bot, settings, job_id)
    except RetryParticipation as error:
        await defer_participation_job(sessions, job_id, error.delay)
    except Exception:
        logger.exception("Participation job %s failed; retaining it for retry", job_id)
        await defer_participation_job(sessions, job_id, 0)


start_job: dramatiq.Actor[[int], None] = dramatiq.actor(
    run_job,
    broker=broker,
    actor_name="start_job",
    queue_name="participation_start",
    priority=10,
    max_retries=0,
)
priority_job: dramatiq.Actor[[int], None] = dramatiq.actor(
    run_job,
    broker=broker,
    actor_name="priority_job",
    queue_name="participation_priority",
    priority=0,
    max_retries=0,
)


def publish_jobs(starts: list[int], priority: list[int]) -> None:
    for job_id in priority:
        priority_job.send(job_id)
    for job_id in starts:
        start_job.send(job_id)


async def dispatch_participation_jobs(sessions: async_sessionmaker[AsyncSession]) -> None:
    while True:
        try:
            async with sessions.begin() as session:
                priority = await lease_jobs_for_dispatch(session, priority=True, batch_size=16)
                starts = await lease_jobs_for_dispatch(session, priority=False, batch_size=12)
            if priority or starts:
                await asyncio.to_thread(publish_jobs, starts, priority)
        except Exception:
            logger.exception("Could not dispatch participation jobs; the inbox will be retried")
            await asyncio.sleep(1)
        await asyncio.sleep(0.25)
