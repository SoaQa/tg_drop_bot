from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.services.giveaways import (
    due_published_giveaways,
    finish_giveaway,
    refresh_published_giveaway_messages,
)

logger = logging.getLogger(__name__)


class GiveawayScheduler:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.bot = bot
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="giveaway-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("Giveaway scheduler tick failed")
            await asyncio.sleep(self.settings.scheduler_interval_seconds)

    async def tick(self) -> None:
        async with self.session_factory() as session:
            try:
                await refresh_published_giveaway_messages(session, self.bot, self.settings)
            except Exception:
                logger.exception("Giveaway participant counter refresh failed")

            giveaways = await due_published_giveaways(session)
            for giveaway in giveaways:
                await finish_giveaway(
                    session,
                    self.bot,
                    self.settings,
                    giveaway,
                    actor_user_id=None,
                    manual=False,
                )
            await session.commit()
