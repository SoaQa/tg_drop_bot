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
from redis.exceptions import RedisError

from tg_drop_bot.config import Settings
from tg_drop_bot.services.frog import public_game, validate_init_data
from tg_drop_bot.services.game_page import render_game_page, require_current_game_client
from tg_drop_bot.services.game_runtime import (
    command,
    game_generator,
    quota,
    read_game_request,
    store_game,
)


def register_free_game_routes(app: web.Application, settings: Settings) -> None:
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3
    )
    generator = game_generator(app)
    assets = Path(__file__).parent.parent / "static" / "frog"
    prefix = settings.free_game_path
    html = render_game_page(
        (assets / "index.html").read_text(encoding="utf-8"),
        captcha=False,
    ).replace(
        "/test/game/assets/",
        f"{prefix}/assets/",
    )

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def api(request: web.Request) -> web.Response:
        try:
            data = await read_game_request(request)
            now = time.time()
            user = validate_init_data(data["initData"], settings.bot_token, now)
            owner = hmac.new(
                settings.app_secret_key.encode(),
                str(user["id"]).encode(),
                hashlib.sha256,
            ).hexdigest()
            await quota(redis, f"freefrog:rate:{owner}:{int(now) // 10}", 100, 20)
            async with redis.lock(f"freefrog:lock:{owner}", timeout=10, blocking_timeout=2):
                return await locked(request.match_info["action"], data, owner, now)
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(
                text="Игра устарела. Начните новую партию через /game.",
            ) from None
        except (RedisError, TimeoutError):
            raise web.HTTPServiceUnavailable(
                text="Сервис временно занят. Повторяем запрос."
            ) from None

    async def locked(action: str, data: dict, owner: str, now: float) -> web.Response:
        now = time.time()
        current = f"freefrog:current:{owner}"
        if action == "start":
            require_current_game_client(data)
            difficulty = data.get("difficulty", "easy")
            if difficulty not in ("easy", "hard"):
                raise ValueError("invalid difficulty")
            await quota(redis, f"freefrog:start:{owner}:{int(now) // 60}", 20, 120)
            previous = await redis.get(current)
            if previous:
                await redis.delete(f"freefrog:session:{previous}")
            game = await generator.generate({}, 0, difficulty)
            for key in ("user", "giveaway_id", "moves"):
                game.pop(key, None)
            game["owner"] = owner
            game["nonce"] = secrets.token_urlsafe(24)
            token = secrets.token_urlsafe(32)
        elif action == "move":
            token = data["token"]
            if not isinstance(token, str) or len(token) > 100:
                raise ValueError("invalid token")
            if (
                not isinstance(data.get("nonce"), str)
                or type(data.get("seq")) is not int
                or data.get("direction") not in ("up", "wait")
            ):
                raise ValueError("invalid command")
            current_token, raw, receipt = await redis.mget(
                current,
                f"freefrog:session:{token}",
                f"freefrog:receipt:{token}",
            )
            if token != current_token or not raw:
                if receipt:
                    saved = json.loads(receipt)
                    nonce = data.get("nonce")
                    if not isinstance(nonce, str):
                        raise ValueError("invalid nonce")
                    fingerprint = {
                        "nonce_hash": hashlib.sha256(nonce.encode()).hexdigest(),
                        "seq": data.get("seq"),
                        "direction": data.get("direction"),
                    }
                    if saved["owner"] == owner and saved["command"] == fingerprint:
                        return web.json_response(
                            saved["response"], headers={"Cache-Control": "no-store"}
                        )
                raise ValueError("expired or replaced session")
            game = json.loads(raw)
            if game["owner"] != owner:
                raise ValueError("wrong owner")
            if now >= game["started"] + 120:
                game["status"] = "expired"
            game = command(game, data, now, record_moves=False)
        else:
            raise web.HTTPNotFound()
        remaining_ms = int((game["started"] + 120 - time.time()) * 1000)
        if game["status"] == "playing" and remaining_ms > 0:
            ttl = min(60_000, remaining_ms)
            await store_game(redis, current, f"freefrog:session:{token}", token, game, ttl)
        else:
            if game["status"] == "playing":
                game["status"] = "expired"
            async with redis.pipeline(transaction=True) as pipe:
                if game.get("last_command"):
                    receipt = {
                        "owner": owner,
                        "command": game["last_command"],
                        "response": public_game(game, time.time())
                        | {
                            "token": token,
                            "nonce": game["nonce"],
                        },
                    }
                    pipe.set(f"freefrog:receipt:{token}", json.dumps(receipt), ex=15)
                await pipe.delete(f"freefrog:session:{token}", current).execute()
        return web.json_response(
            public_game(game, time.time())
            | {
                "token": token,
                "nonce": game["nonce"],
            },
            headers={"Cache-Control": "no-store"},
        )

    async def cleanup(_app: web.Application) -> None:
        await redis.aclose()

    app.on_cleanup.append(cleanup)
    app.router.add_get(f"{prefix}/", page)
    app.router.add_post(f"{prefix}/api/{{action}}", api)
    app.router.add_static(f"{prefix}/assets/", assets / "assets")
