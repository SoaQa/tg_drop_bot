from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from tg_drop_bot.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    reason: str = "ok"


class RedisRateLimiter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.redis: Redis | None = None
        if settings.rate_limit_enabled and settings.redis_url:
            self.redis = Redis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        if self.redis is not None:
            await self.redis.aclose()

    async def fixed_window(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        if not self.settings.rate_limit_enabled or self.redis is None:
            return RateLimitResult(True)
        try:
            value = await self.redis.incr(key)
            if value == 1:
                await self.redis.expire(key, window_seconds)
            if value > limit:
                return RateLimitResult(False, "rate_limit")
        except RedisError as exc:
            logger.warning("Redis rate limit failed open for key %s: %s", key, exc)
            return RateLimitResult(True, "redis_unavailable")
        return RateLimitResult(True)

    async def debounce(self, key: str, ttl_seconds: int) -> RateLimitResult:
        if not self.settings.rate_limit_enabled or self.redis is None:
            return RateLimitResult(True)
        try:
            acquired = await self.redis.set(key, "1", ex=ttl_seconds, nx=True)
            if not acquired:
                return RateLimitResult(False, "debounce")
        except RedisError as exc:
            logger.warning("Redis debounce failed open for key %s: %s", key, exc)
            return RateLimitResult(True, "redis_unavailable")
        return RateLimitResult(True)
