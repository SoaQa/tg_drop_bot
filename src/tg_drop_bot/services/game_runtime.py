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

from tg_drop_bot.services.frog import advance, new_game

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

    async def generate(self, user: dict, gid: int, difficulty: str) -> dict:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=2)
        except TimeoutError:
            raise web.HTTPServiceUnavailable(
                text="Сервер занят. Повторите через секунду.",
                headers={"Retry-After": "1"},
            ) from None
        try:
            game = await asyncio.to_thread(new_game, user, gid, 0, difficulty=difficulty)
            # Queueing/generation time must not consume the player's time or move traffic.
            started = time.time()
            game.update(started=started, checked=started, last_move=started - 1)
            return game
        finally:
            self.slots.release()


GENERATOR = web.AppKey("game_generator", GameGenerator)


def game_generator(app: web.Application) -> GameGenerator:
    if GENERATOR not in app:
        app[GENERATOR] = GameGenerator()
    return app[GENERATOR]
