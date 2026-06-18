from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from tg_drop_bot.bot import admin, channel, participant
from tg_drop_bot.bot.rate_limit import RateLimitMiddleware
from tg_drop_bot.config import Settings
from tg_drop_bot.db.middleware import DbSessionMiddleware
from tg_drop_bot.db.session import create_session_factory
from tg_drop_bot.logging import setup_logging
from tg_drop_bot.services.rate_limit import RedisRateLimiter
from tg_drop_bot.services.scheduler import GiveawayScheduler


async def run_bot() -> None:
    setup_logging()
    settings = Settings()
    settings.validate_runtime()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    session_factory = create_session_factory(settings)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.message.middleware(RateLimitMiddleware())
    dispatcher.callback_query.middleware(RateLimitMiddleware())
    dispatcher.update.middleware(DbSessionMiddleware())
    dispatcher.include_router(channel.router)
    dispatcher.include_router(participant.router)
    dispatcher.include_router(admin.router)

    scheduler = GiveawayScheduler(settings, session_factory, bot)
    rate_limiter = RedisRateLimiter(settings)

    if settings.telegram_mode == "polling":
        await bot.delete_webhook(drop_pending_updates=True)
        scheduler.start()
        try:
            await dispatcher.start_polling(
                bot,
                settings=settings,
                session_factory=session_factory,
                rate_limiter=rate_limiter,
            )
        finally:
            await scheduler.stop()
            await rate_limiter.close()
            await bot.session.close()
        return

    app = web.Application()
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.webhook_secret,
        settings=settings,
        session_factory=session_factory,
        rate_limiter=rate_limiter,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)

    async def on_startup(_app: web.Application) -> None:
        await bot.set_webhook(
            settings.full_webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
        scheduler.start()

    async def on_cleanup(_app: web.Application) -> None:
        await scheduler.stop()
        await rate_limiter.close()
        await bot.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.webapp_host, port=settings.webapp_port)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
