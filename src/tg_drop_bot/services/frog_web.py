from __future__ import annotations

import contextlib
import hmac
import json
import secrets
import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User, WebAppInfo
from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog, Participant, ParticipationFlow
from tg_drop_bot.services.captcha import captcha_response_ms
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.frog import advance, new_game, public_game, validate_init_data
from tg_drop_bot.services.game_page import render_game_page
from tg_drop_bot.services.giveaways import (
    get_giveaway,
    is_giveaway_open,
    register_participant_after_condition_check,
)
from tg_drop_bot.services.participation_queue import lock_giveaway_intake


def game_keyboard(settings: Settings, giveaway_id: int) -> InlineKeyboardMarkup:
    url = f"{settings.webhook_url}{settings.frog_game_path}/?giveaway={giveaway_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Играть и участвовать", web_app=WebAppInfo(url=url)),
    ]])


def register_game_routes(
    app: web.Application, settings: Settings,
    sessions: async_sessionmaker[AsyncSession], bot: Bot,
) -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    assets = Path(__file__).parent.parent / "static" / "frog"
    prefix = settings.frog_game_path
    html = render_game_page(
        (assets / "index.html").read_text(encoding="utf-8"), captcha=True,
    ).replace("/test/game/assets/", f"{prefix}/assets/")

    async def queued_flow(session: AsyncSession, gid: int, uid: int) -> ParticipationFlow:
        # Same intake lock as closing the draw: an admitted game keeps it draining.
        giveaway = await lock_giveaway_intake(session, gid)
        flow = await session.scalar(select(ParticipationFlow).where(
            ParticipationFlow.giveaway_id == gid, ParticipationFlow.user_id == uid,
        ).with_for_update())
        if (
            giveaway is None or giveaway.status != "published" or flow is None
            or flow.status != "game" or flow.expires_at is None or flow.expires_at <= utc_now()
        ):
            raise web.HTTPConflict(text="Проверка истекла. Нажмите кнопку участия в боте.")
        return flow

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text=html, content_type="text/html", headers={"Cache-Control": "no-store"},
        )

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
        flow: ParticipationFlow | None = None
        if request.match_info["action"] == "start":
            countkey = f"frog:start:{uid}:{int(now) // 60}"
            if await redis.incr(countkey) == 1:
                await redis.expire(countkey, 120)
            if int(await redis.get(countkey) or 0) > 10:
                raise web.HTTPTooManyRequests(text="Отдохните минуту и попробуйте снова.")
            gid = int(data["giveaway"])
            challenge_id = None
            async with sessions() as session:
                if settings.participation_queue_enabled:
                    flow = await queued_flow(session, gid, uid)
                    challenge_id = flow.challenge_id
                else:
                    giveaway = await get_giveaway(session, gid)
                    if not giveaway or not is_giveaway_open(giveaway):
                        raise web.HTTPConflict(text="Приём заявок закрыт.")
            game = new_game(user, gid, now)
            game["challenge_id"] = challenge_id
            game['nonce'] = secrets.token_urlsafe(24)
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
            if not hmac.compare_digest(str(data.get('nonce', '')), game.get('nonce', 'invalid')):
                raise ValueError("replayed command token")
            game = advance(game, data["direction"], data["seq"], now)
            game['nonce'] = secrets.token_urlsafe(24)
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
                # A committed registration can be retried after a lost HTTP response.
                existing = await session.scalar(select(Participant.id).where(
                    Participant.giveaway_id == game["giveaway_id"], Participant.user_id == uid,
                ))
                if existing:
                    return web.json_response(public_game(game, time.time()) | {
                        "token": token, "nonce": game['nonce'],
                    }, headers={"Cache-Control": "no-store"})
                flow = None
                if settings.participation_queue_enabled:
                    flow = await queued_flow(session, game["giveaway_id"], uid)
                    if flow.challenge_id != game["challenge_id"]:
                        raise web.HTTPConflict(
                            text="Проверка заменена. Откройте новую игру из бота.",
                        )
                giveaway = await get_giveaway(session, game["giveaway_id"])
                if not giveaway or (flow is None and not is_giveaway_open(giveaway)):
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
                    result.participant.captcha_response_ms = captcha_response_ms(
                        flow.captcha_sent_at if flow else None, utc_now(),
                    )
                    session.add(AuditLog(
                        giveaway_id=giveaway.id, actor_user_id=uid,
                        action="participant.frog_passed",
                        payload={"seconds": round(now-game["started"], 2)},
                    ))
                if flow is not None:
                    from tg_drop_bot.services.participation_worker import close_flow

                    close_flow(flow)
                await session.commit()
                if result.created:
                    with contextlib.suppress(TelegramAPIError):
                        await bot.send_message(uid, "Лягушка добралась! Ваше участие засчитано.")
        return web.json_response(public_game(game, time.time()) | {
            "token": token, "nonce": game['nonce'],
        }, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    async def cleanup(_app: web.Application) -> None:
        await redis.aclose()

    app.on_cleanup.append(cleanup)
    app.router.add_get(f"{prefix}/", page)
    app.router.add_post(f"{prefix}/api/{{action}}", api)
    app.router.add_static(f"{prefix}/assets/", assets / "assets")
