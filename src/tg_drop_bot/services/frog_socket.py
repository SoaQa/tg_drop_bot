"""Bounded WebSocket transport for the server-authoritative raffle game."""

from __future__ import annotations

import json
import secrets
import time
from typing import Protocol
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web
from redis.asyncio import Redis
from redis.exceptions import RedisError

from tg_drop_bot.config import Settings
from tg_drop_bot.services.frog import validate_init_data
from tg_drop_bot.services.game_page import require_current_game_client
from tg_drop_bot.services.game_runtime import MAX_GAME_BODY, quota

MAX_CONNECTIONS = 256
MAX_ACTION_BYTES = 1024


class GameCommand(Protocol):
    async def __call__(
        self, action: str, data: dict, user: dict, *, connection: str | None
    ) -> dict: ...


def read_frame(body: str) -> dict:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("expected object")
    return data


def game_origin(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None or parsed.scheme not in {"http", "https"}:
        raise ValueError("invalid game origin")
    port = parsed.port
    default = 443 if parsed.scheme == "https" else 80
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != default:
        authority += f":{port}"
    return f"{parsed.scheme}://{authority}"


def register_frog_socket(
    app: web.Application, settings: Settings, redis: Redis, command: GameCommand,
) -> None:
    origin = game_origin(settings.webhook_url)
    active: set[web.WebSocketResponse] = set()

    async def shutdown(_app: web.Application) -> None:
        for socket in tuple(active):
            await socket.close(code=1001)

    async def handler(request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Origin") != origin:
            raise web.HTTPForbidden()
        if len(active) >= MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable()
        socket = web.WebSocketResponse(
            heartbeat=10, receive_timeout=35, max_msg_size=MAX_GAME_BODY, compress=False,
        )
        active.add(socket)
        try:
            await socket.prepare(request)
            try:
                first = await socket.receive(timeout=5)
                if first.type != WSMsgType.TEXT:
                    raise ValueError("expected authentication")
                hello = read_frame(first.data)
                if hello.get("action") != "hello":
                    raise ValueError("expected hello")
                require_current_game_client(hello)
                user = validate_init_data(hello["initData"], settings.bot_token, time.time())
            except (ValueError, KeyError, TypeError, web.HTTPException, TimeoutError):
                await socket.close(code=1008)
                return socket
            await socket.send_json({"type": "ready"})
            uid = user["id"]
            connection = secrets.token_urlsafe(24)
            expires = time.monotonic() + 600
            bound = False
            async for frame in socket:
                if time.monotonic() >= expires:
                    await socket.close(code=1001)
                    break
                if frame.type != WSMsgType.TEXT or len(frame.data) > MAX_ACTION_BYTES:
                    await socket.close(code=1008)
                    break
                try:
                    data = read_frame(frame.data)
                    action = data.get("action")
                    if action in ("start", "resume"):
                        gid = data.get("giveaway")
                        if type(gid) is not int or not 0 < gid < 2**31:
                            raise ValueError("invalid giveaway")
                        data["clientVersion"] = hello["clientVersion"]
                    elif action == "move":
                        if not bound or type(data.get("seq")) is not int or data["seq"] < 0:
                            raise ValueError("invalid move")
                    elif action != "sync" or not bound:
                        raise ValueError("invalid action")
                    await quota(redis, f"frog:rate:{uid}:{int(time.time()) // 10}", 100, 20)
                    async with redis.lock(f"frog:lock:{uid}", timeout=30, blocking_timeout=2):
                        state = await command(action, data, user, connection=connection)
                    bound = True
                    await socket.send_json({"type": "state", "action": action, "state": state})
                except (ValueError, KeyError, TypeError):
                    await socket.close(code=1008)
                    break
                except web.HTTPException as exc:
                    await socket.send_json(
                        {
                            "type": "error", "action": action,
                            "status": exc.status, "message": exc.text,
                        }
                    )
                except (RedisError, TimeoutError):
                    await socket.close(code=1013)
                    break
        except TimeoutError:
            await socket.close(code=1001)
        finally:
            active.discard(socket)
        return socket

    app.on_shutdown.append(shutdown)
    app.router.add_get(f"{settings.frog_game_path}/live", handler)
