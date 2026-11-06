from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.services.captcha import cleanup_captcha_challenges
from tg_drop_bot.services.giveaways import (
    due_published_giveaways,
    finish_giveaway,
    get_giveaway,
    refresh_published_giveaway_messages,
    resume_unfinished_results,
)
from tg_drop_bot.services.membership import cleanup_membership_events

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
        self._last_rendered_counts: dict[int, int] = {}

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
        await self._cleanup_captchas()
        await self._refresh_counters()
        await self._finish_due_giveaways()
        await self._resume_unfinished_results()
        async with self.session_factory() as session:
            await cleanup_membership_events(session)
            await session.commit()

    async def _cleanup_captchas(self) -> None:
        async with self.session_factory() as session:
            try:
                deleted = await cleanup_captcha_challenges(session)
                await session.commit()
                if deleted:
                    logger.info("Deleted %s obsolete captcha challenges", deleted)
            except Exception:
                logger.exception("Captcha cleanup failed")

    async def _refresh_counters(self) -> None:
        async with self.session_factory() as session:
            try:
                await refresh_published_giveaway_messages(
                    session,
                    self.bot,
                    self.settings,
                    self._last_rendered_counts,
                )
            except Exception:
                logger.exception("Giveaway participant counter refresh failed")

    async def _finish_due_giveaways(self) -> None:
        async with self.session_factory() as session:
            due = await due_published_giveaways(session)
            due_ids = [giveaway.id for giveaway in due]

        # Each giveaway is finished in its own session so a failure in one
        # cannot roll back or repeat side effects of another.
        for giveaway_id in due_ids:
            async with self.session_factory() as session:
                giveaway = await get_giveaway(session, giveaway_id)
                if giveaway is None:
                    continue
                try:
                    await finish_giveaway(
                        session,
                        self.bot,
                        self.settings,
                        giveaway,
                        actor_user_id=None,
                        manual=False,
                    )
                except Exception:
                    logger.exception(
                        "Failed to finish giveaway",
                        extra={"giveaway_id": giveaway_id},
                    )

    async def _resume_unfinished_results(self) -> None:
        async with self.session_factory() as session:
            try:
                await resume_unfinished_results(session, self.bot, self.settings)
            except Exception:
                logger.exception("Failed to resume unfinished giveaway results")
