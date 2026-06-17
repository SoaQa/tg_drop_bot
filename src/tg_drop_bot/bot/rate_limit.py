from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from tg_drop_bot.config import Settings
from tg_drop_bot.services.rate_limit import RedisRateLimiter


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings: Settings = data["settings"]
        limiter: RedisRateLimiter = data["rate_limiter"]

        user_id = self._user_id(event)
        if user_id is None:
            return await handler(event, data)

        global_result = await limiter.fixed_window(
            f"rl:user:{user_id}:updates",
            settings.rate_limit_user_updates,
            settings.rate_limit_user_window_seconds,
        )
        if not global_result.allowed:
            await self._notify_limited(event)
            return None

        if (
            isinstance(event, CallbackQuery)
            and event.data
            and event.data.startswith("participate:")
        ):
            participate_result = await limiter.debounce(
                f"rl:user:{user_id}:participate",
                settings.rate_limit_participate_seconds,
            )
            if not participate_result.allowed:
                await event.answer("Слишком часто. Попробуйте через несколько секунд.")
                return None

        return await handler(event, data)

    @staticmethod
    def _user_id(event: TelegramObject) -> int | None:
        if isinstance(event, Message) and event.from_user:
            return event.from_user.id
        if isinstance(event, CallbackQuery) and event.from_user:
            return event.from_user.id
        return None

    @staticmethod
    async def _notify_limited(event: TelegramObject) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer("Слишком часто. Попробуйте чуть позже.")
