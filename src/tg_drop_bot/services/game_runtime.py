"""Bounded game requests, atomic Redis updates and retry-safe commands."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time

from aiohttp import web
from redis.asyncio import Redis

from tg_drop_bot.services.frog import HOP_COOLDOWN, advance, new_game

MAX_GAME_BODY = 16_384


async def read_game_request(request: web.Request) -> dict:
    if request.content_length is not None and request.content_length > MAX_GAME_BODY:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_GAME_BODY, actual_size=request.content_length
        )
    raw = bytearray()
    async with asyncio.timeout(10):
        async for chunk in request.content.iter_chunked(MAX_GAME_BODY + 1):
            raw.extend(chunk)
            if len(raw) > MAX_GAME_BODY:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_GAME_BODY, actual_size=len(raw))
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("initData"), str):
        raise ValueError("invalid request")
    return data


async def quota(redis: Redis, key: str, limit: int, ttl: int) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        count, _ = await pipe.execute()
    if count > limit:
        raise web.HTTPTooManyRequests(
            text="Слишком много запросов. Подождите немного.",
            headers={"Retry-After": "1"},
        )


def command(game: dict, data: dict, now: float, *, record_moves: bool = True) -> dict:
    nonce, seq, direction = data.get("nonce"), data.get("seq"), data.get("direction")
    if not isinstance(nonce, str) or type(seq) is not int or direction not in ("up", "wait"):
        raise ValueError("invalid command")
    fingerprint = {
        "nonce_hash": hashlib.sha256(nonce.encode()).hexdigest(),
        "seq": seq,
        "direction": direction,
    }
    if not hmac.compare_digest(nonce, game["nonce"]):
        if game.get("last_command") == fingerprint:
            return game
        raise ValueError("wrong nonce")
    if seq != game["seq"]:
        raise ValueError("stale sequence")
    game = advance(game, direction, seq, now, record_moves=record_moves)
    game["last_command"] = fingerprint
    game["nonce"] = secrets.token_urlsafe(24)
    return game


async def execute_command(
    game: dict,
    data: dict,
    *,
    record_moves: bool = True,
    deadline: float | None = None,
) -> dict:
    """Keep a valid hop when network jitter compresses two arrival times.

    The caller holds this owner's Redis lock. Only a fresh, correctly sequenced
    up command can wait, for at most one cooldown. Retries and invalid commands
    go straight through the normal validation; physics still uses real server time.
    """
    if (
        game["status"] == "playing"
        and data.get("direction") == "up"
        and isinstance(data.get("nonce"), str)
        and hmac.compare_digest(data["nonce"], game["nonce"])
        and type(data.get("seq")) is int
        and data["seq"] == game["seq"]
    ):
        async with asyncio.timeout(HOP_COOLDOWN + 0.05):
            while (
                0
                < (delay := game["last_move"] + HOP_COOLDOWN - time.time())
                <= HOP_COOLDOWN + 0.001
            ):
                # A small timer margin also avoids floating point boundary drops.
                await asyncio.sleep(delay + 0.001)
    now = time.time()
    if deadline is not None and now >= deadline and game["status"] == "playing":
        game["status"] = "expired"
    return command(game, data, now, record_moves=record_moves)


async def store_game(
    redis: Redis,
    current_key: str,
    session_key: str,
    token: str,
    game: dict,
    ttl_ms: int,
) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(
            session_key, json.dumps(game, ensure_ascii=False, separators=(",", ":")), px=ttl_ms
        )
        pipe.set(current_key, token, px=ttl_ms)
        await pipe.execute()


class GameGenerator:
    def __init__(self) -> None:
        self.slots = asyncio.Semaphore(2)

    async def generate(
        self,
        user: dict,
        gid: int,
        difficulty: str,
        *,
        relaxed: bool = False,
        assisted: bool = False,
        from_checkpoint: bool = False,
    ) -> dict:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=2)
        except TimeoutError:
            raise web.HTTPServiceUnavailable(
                text="Сервер занят. Повторите через секунду.",
                headers={"Retry-After": "1"},
            ) from None
        # Cancelling an awaiting request cannot stop an already running CPU thread.
        # Retain its slot until the actual construction task has completed.
        layout = ({"relaxed": True, "assisted": assisted, "from_checkpoint": from_checkpoint}
                  if relaxed else {})
        construction = asyncio.create_task(
            asyncio.to_thread(new_game, user, gid, 0, difficulty=difficulty, **layout)
        )

        def completed(task: asyncio.Task[dict]) -> None:
            self.slots.release()
            if not task.cancelled():
                # Retrieve a failure even when the original request was cancelled.
                task.exception()

        construction.add_done_callback(completed)
        game = await asyncio.shield(construction)
        # Queueing/generation time must not consume the player's time or move traffic.
        started = time.time()
        game.update(started=started, checked=started, last_move=started - 1)
        return game


GENERATOR = web.AppKey("game_generator", GameGenerator)


def game_generator(app: web.Application) -> GameGenerator:
    if GENERATOR not in app:
        app[GENERATOR] = GameGenerator()
    return app[GENERATOR]
