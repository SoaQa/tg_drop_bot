from __future__ import annotations

import contextlib
import json
import secrets
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User, WebAppInfo
from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.frog import advance, new_game, public_game, validate_init_data
from tg_drop_bot.services.giveaways import (
    get_giveaway,
    is_giveaway_open,
    register_participant_after_condition_check,
)


def game_keyboard(settings: Settings, giveaway_id: int) -> InlineKeyboardMarkup:
    url = f"{settings.webhook_url}/test/game/?giveaway={giveaway_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Играть и участвовать", web_app=WebAppInfo(url=url)),
    ]])


def register_game_routes(
    app: web.Application, settings: Settings,
    sessions: async_sessionmaker[AsyncSession], bot: Bot,
) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    assets = Path(__file__).parent.parent / "static" / "frog"

    async def page(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(assets / "index.html", headers={"Cache-Control": "no-store"})

    async def api(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            user = validate_init_data(data["initData"], settings.bot_token, time.time())
            uid = user["id"]
            # Quotas are identity-bound, not Telegram webhook IP-bound.
            key = f"frog:rate:{uid}:{int(time.time()) // 10}"
            if await redis.incr(key) == 1:
                await redis.expire(key, 20)
            if int(await redis.get(key) or 0) > 100:
                raise web.HTTPTooManyRequests(text="Слишком много запросов. Подождите немного.")
            async with redis.lock(f"frog:lock:{uid}", timeout=30, blocking_timeout=2):
                return await locked(request, data, user)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="Проверка устарела. Откройте игру из бота.") from None

    async def locked(request: web.Request, data: dict, user: dict) -> web.Response:
        now = time.time()
        uid = user["id"]
        if request.match_info["action"] == "start":
            countkey = f"frog:start:{uid}:{int(now) // 60}"
            if await redis.incr(countkey) == 1:
                await redis.expire(countkey, 120)
            if int(await redis.get(countkey) or 0) > 20:
                raise web.HTTPTooManyRequests(text="Отдохните минуту и попробуйте снова.")
            gid = int(data["giveaway"])
            async with sessions() as session:
                giveaway = await get_giveaway(session, gid)
                if not giveaway or not is_giveaway_open(giveaway):
                    raise web.HTTPConflict(text="Приём заявок закрыт.")
            game = new_game(user, gid, now)
            async with sessions() as session:
                record = AuditLog(
                    giveaway_id=gid, actor_user_id=uid, action="frog.run.started",
                    payload={"started": now, "status": "started"},
                )
                session.add(record)
                await session.flush()
                game["audit_id"] = record.id
                await session.commit()
            token = secrets.token_urlsafe(32)
            await redis.set(f"frog:current:{uid}", token, ex=360)
        elif request.match_info["action"] == "move":
            token = data["token"]
            if token != await redis.get(f"frog:current:{uid}"):
                raise ValueError("replaced session")
            raw = await redis.get(f"frog:session:{token}")
            if raw is None:
                raise ValueError("expired session")
            game = json.loads(raw)
            if game["user"]["id"] != uid:
                raise ValueError("wrong owner")
            game = advance(game, data["direction"], data["seq"], now)
        else:
            raise web.HTTPNotFound()
        # Persist before registering: process crashes cannot allow a replayed move to win twice.
        await redis.set(f"frog:session:{token}", json.dumps(game), ex=360)
        if game["status"] != "playing" and not game.get("logged"):
            async with sessions() as session:
                await session.execute(update(AuditLog).where(
                    AuditLog.id == game["audit_id"], AuditLog.actor_user_id == uid,
                ).values(action="frog.run.finished", payload={
                    "started": game["started"], "finished": now, "status": game["status"],
                    "seconds": round(now-game["started"], 3), "moves": game["moves"],
                    "move_count": len(game["moves"]), "lanes": game["lanes"],
                }))
                await session.commit()
            game["logged"] = True
            await redis.set(f"frog:session:{token}", json.dumps(game), ex=360)
        if game["status"] == "won":
            async with sessions() as session:
                giveaway = await get_giveaway(session, game["giveaway_id"])
                if not giveaway or not is_giveaway_open(giveaway):
                    raise web.HTTPConflict(text="Приём заявок закрыт.")
                result = await register_participant_after_condition_check(
                    session, bot, giveaway, User.model_validate(game["user"] | {"is_bot": False}),
                    received_at=utc_now(),
                )
                if result.participant is None:
                    raise web.HTTPForbidden(
                        text=result.condition_check.user_message or "Условия участия не выполнены.",
                    )
                if result.created:
                    session.add(AuditLog(
                        giveaway_id=giveaway.id, actor_user_id=uid,
                        action="participant.frog_passed",
                        payload={"seconds": round(now-game["started"], 2)},
                    ))
                await session.commit()
                if result.created:
                    with contextlib.suppress(TelegramAPIError):
                        await bot.send_message(uid, "Лягушка добралась! Ваше участие засчитано.")
        return web.json_response(public_game(game, now) | {"token": token}, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    async def cleanup(_app: web.Application) -> None:
        await redis.aclose()

    app.on_cleanup.append(cleanup)
    app.router.add_get("/test/game/", page)
    app.router.add_post("/test/api/game/{action}", api)
    app.router.add_static("/test/game/assets/", assets / "assets")
