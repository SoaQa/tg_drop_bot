"""Standalone game: transient anonymous Redis state, no database or raffle access."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from aiohttp import web
from redis.asyncio import Redis

from tg_drop_bot.config import Settings
from tg_drop_bot.services.frog import advance, new_game, public_game, validate_init_data


def register_free_game_routes(app: web.Application, settings: Settings) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    assets = Path(__file__).parent.parent / "static" / "frog"
    prefix = settings.free_game_path
    html = (assets / "index.html").read_text(encoding="utf-8").replace(
        "/test/game/assets/", f"{prefix}/assets/",
    )

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text=html, content_type="text/html", headers={"Cache-Control": "no-store"},
        )

    async def api(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            now = time.time()
            user = validate_init_data(data['initData'], settings.bot_token, now)
            owner = hmac.new(
                settings.app_secret_key.encode(), str(user['id']).encode(), hashlib.sha256,
            ).hexdigest()
            quota = f"freefrog:rate:{owner}:{int(now)//10}"
            if await redis.incr(quota) == 1:
                await redis.expire(quota, 20)
            if int(await redis.get(quota) or 0) > 100:
                raise web.HTTPTooManyRequests(text="Слишком много нажатий. Подождите немного.")
            async with redis.lock(f"freefrog:lock:{owner}", timeout=10, blocking_timeout=2):
                return await locked(request.match_info['action'], data, owner, now)
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(
                text="Игра устарела. Начните новую партию через /game.",
            ) from None

    async def locked(action: str, data: dict, owner: str, now: float) -> web.Response:
        now = time.time()
        current = f"freefrog:current:{owner}"
        if action == 'start':
            quota = f"freefrog:start:{owner}:{int(now)//60}"
            if await redis.incr(quota) == 1:
                await redis.expire(quota, 120)
            if int(await redis.get(quota) or 0) > 20:
                raise web.HTTPTooManyRequests(text="Подождите минуту перед следующей партией.")
            previous = await redis.get(current)
            if previous:
                await redis.delete(f"freefrog:session:{previous}")
            game = new_game({}, 0, now)
            for key in ('user', 'giveaway_id', 'moves'):
                game.pop(key, None)
            game['owner'] = owner
            game['nonce'] = secrets.token_urlsafe(24)
            token = secrets.token_urlsafe(32)
        elif action == 'move':
            token = data['token']
            if token != await redis.get(current):
                raise ValueError("wrong session")
            raw = await redis.get(f"freefrog:session:{token}")
            if not raw:
                await redis.delete(current)
                raise ValueError("expired session")
            game = json.loads(raw)
            if game['owner'] != owner or not hmac.compare_digest(
                str(data.get('nonce', '')), game['nonce'],
            ):
                raise ValueError("wrong owner or replay")
            if now >= game['started'] + 120:
                game['status'] = 'expired'
            else:
                game = advance(game, data['direction'], data['seq'], now, record_moves=False)
            game['nonce'] = secrets.token_urlsafe(24)
        else:
            raise web.HTTPNotFound()
        remaining_ms = int((game['started']+120-time.time())*1000)
        if game['status'] == 'playing' and remaining_ms > 0:
            ttl = min(60_000, remaining_ms)
            await redis.set(f"freefrog:session:{token}", json.dumps(game), px=ttl)
            await redis.set(current, token, px=ttl)
        else:
            if game['status'] == 'playing':
                game['status'] = 'expired'
            await redis.delete(f"freefrog:session:{token}", current)
        return web.json_response(public_game(game, time.time()) | {
            'token': token, 'nonce': game['nonce'],
        }, headers={"Cache-Control": "no-store"})

    async def cleanup(_app: web.Application) -> None:
        await redis.aclose()

    app.on_cleanup.append(cleanup)
    app.router.add_get(f'{prefix}/', page)
    app.router.add_post(f'{prefix}/api/{{action}}', api)
    app.router.add_static(f'{prefix}/assets/', assets / 'assets')
