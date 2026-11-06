from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import Message, Update
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.captcha import hash_captcha_answer
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.participation_limits import ParticipationLimits, QueuePressure
from tg_drop_bot.services.participation_queue import Admission, admit_captcha_action, admit_start
from tg_drop_bot.services.participation_worker import CLOSED
from tg_drop_bot.services.rate_limit import RedisRateLimiter

logger = logging.getLogger(__name__)
START = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?(?:\s+giveaway_([0-9]{1,10}))?\s*$")
PARTICIPATE = re.compile(r"^participate:([0-9]{1,10})$")
REFRESH = re.compile(r"^queue:refresh:([0-9]{1,10})$")
OVERLOADED = "Бот перегружен. Попробуйте снова через пару минут."


class ParticipationWebhook(SimpleRequestHandler):
    def __init__(
        self,
        *,
        queue_settings: Settings,
        queue_sessions: async_sessionmaker[AsyncSession],
        limits: ParticipationLimits,
        user_limiter: RedisRateLimiter,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.queue_settings = queue_settings
        self.queue_sessions = queue_sessions
        self.limits = limits
        self.user_limiter = user_limiter
        self.pressure = QueuePressure(queue_sessions)

    async def _handle_request_background(self, bot: Bot, request: web.Request) -> web.Response:
        received_at = utc_now()
        update = Update.model_validate(await request.json(), context={"bot": bot})
        try:
            result = await self.handle_participation(bot, update, received_at)
        except (RedisError, SQLAlchemyError, TimeoutError):
            logger.warning("Participation intake unavailable; requesting webhook retry")
            return web.Response(status=503)
        if result is not None:
            return result
        return await super()._handle_request_background(bot, request)

    async def _notice(
        self,
        update: Update,
        user_id: int,
        outcome: Admission,
    ) -> web.Response:
        texts = {
            "overloaded": OVERLOADED,
            "closed": CLOSED,
            "cooldown": (
                f"Новую капчу можно получить через "
                f"{self.queue_settings.captcha_cooldown_seconds} сек."
            ),
        }
        text = texts.get(outcome)
        callback = update.callback_query
        if callback:
            payload = {"method": "answerCallbackQuery", "callback_query_id": callback.id}
            if text and await self.limits.allow_notice(user_id):
                payload["text"] = text
            elif outcome == "overloaded":
                await self.limits.queue_notice(user_id, OVERLOADED)
            return web.json_response(payload)
        if text and await self.limits.allow_notice(user_id):
            return web.json_response({"method": "sendMessage", "chat_id": user_id, "text": text})
        if outcome == "overloaded":
            await self.limits.queue_notice(user_id, OVERLOADED)
        return web.json_response({})

    async def handle_participation(
        self,
        bot: Bot,
        update: Update,
        received_at: datetime,
    ) -> web.Response | None:
        callback = update.callback_query
        message = update.message
        user = callback.from_user if callback else (message.from_user if message else None)
        if user is None:
            return None
        settings = self.queue_settings
        giveaway_id = challenge_id = None
        plain_start = False
        answer_hash = None
        if (
            callback
            and isinstance(callback.message, Message)
            and callback.message.chat.type == ChatType.PRIVATE
        ):
            data = callback.data or ""
            if match := PARTICIPATE.fullmatch(data):
                giveaway_id = int(match[1])
            elif match := REFRESH.fullmatch(data):
                challenge_id = int(match[1])
            else:
                return None
        elif message and message.chat.type == ChatType.PRIVATE and message.text:
            if match := START.fullmatch(message.text):
                if match[1]:
                    giveaway_id = int(match[1])
                else:
                    plain_start = user.id not in settings.admin_id_set
                    if not plain_start:
                        return None
            elif message.text.startswith("/"):
                return None
            else:
                if user.id in settings.admin_id_set:
                    context = self.dispatcher.fsm.get_context(
                        bot=bot, user_id=user.id, chat_id=message.chat.id
                    )
                    if await context.get_state():
                        return None
                answer_hash = hash_captcha_answer(message.text, settings)
        else:
            return None
        if any(
            value is not None and not 0 < value <= 2**31 - 1
            for value in (giveaway_id, challenge_id)
        ):
            return await self._notice(update, user.id, "closed")
        if giveaway_id is not None or plain_start or challenge_id is not None:
            quota = await self.user_limiter.fixed_window(
                f"rl:user:{user.id}:updates",
                settings.rate_limit_user_updates,
                settings.rate_limit_user_window_seconds,
            )
            if not quota.allowed:
                return await self._notice(update, user.id, "overloaded")
        if challenge_id is not None and not await self.limits.allow_captcha_callback(user.id):
            return await self._notice(update, user.id, "duplicate")
        if answer_hash is not None and not await self.limits.take(f"answer:{user.id}", 3, 6):
            return web.json_response({})
        permit = (
            await self.pressure.permits_start() if giveaway_id is not None or plain_start else True
        )

        async def allows_new() -> bool:
            return permit and await self.limits.admit_start()

        async with self.queue_sessions.begin() as session:
            if plain_start:
                ids = (
                    await session.scalars(
                        select(Giveaway.id)
                        .where(
                            Giveaway.status == "published",
                            Giveaway.intake_closed_at.is_(None),
                            Giveaway.deadline_at > received_at,
                        )
                        .limit(2)
                    )
                ).all()
                if len(ids) != 1:
                    return None
                giveaway_id = ids[0]
            if giveaway_id is not None:
                outcome = await admit_start(
                    session,
                    giveaway_id=giveaway_id,
                    user=user,
                    update_id=update.update_id,
                    received_at=received_at,
                    allow_new=allows_new,
                )
            else:
                outcome = await admit_captcha_action(
                    session,
                    user_id=user.id,
                    update_id=update.update_id,
                    received_at=received_at,
                    max_attempts=settings.captcha_max_attempts,
                    answer_hash=answer_hash,
                    challenge_id=challenge_id,
                )
        if outcome == "ignored":
            if callback:
                return await self._notice(update, user.id, "closed")
            return None
        return await self._notice(update, user.id, outcome)
