from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User, WebAppInfo
from aiohttp import web
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import (
    AuditLog,
    HardGameAttempt,
    Participant,
    ParticipationFlow,
    ParticipationJob,
)
from tg_drop_bot.services.captcha import captcha_response_ms
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.frog import advance, public_game, validate_init_data
from tg_drop_bot.services.frog_socket import register_frog_socket
from tg_drop_bot.services.game_page import render_game_page, require_current_game_client
from tg_drop_bot.services.game_runtime import (
    execute_command,
    game_generator,
    quota,
    read_game_request,
    store_game,
)
from tg_drop_bot.services.giveaways import (
    get_giveaway,
    is_giveaway_open,
    register_participant_after_condition_check,
)
from tg_drop_bot.services.hard_game import (
    HARD_ATTEMPT_LIMIT,
    claim_hard_attempt,
    claim_hard_game_slot,
    hard_attempts_left,
    record_hard_completion,
)
from tg_drop_bot.services.participation_queue import accepts_new_requests, lock_giveaway_intake


def game_keyboard(
    settings: Settings, giveaway_id: int, *, ranking: bool = False,
) -> InlineKeyboardMarkup:
    url = f"{settings.webhook_url}{settings.frog_game_path}/?giveaway={giveaway_id}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Играть на сложном для /top" if ranking else "Играть и участвовать",
                    web_app=WebAppInfo(url=f"{url}&ranking=1" if ranking else url),
                ),
            ]
        ]
    )


def register_game_routes(
    app: web.Application, settings: Settings, sessions: async_sessionmaker[AsyncSession], bot: Bot
) -> None:
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=3
    )
    generator = game_generator(app)
    assets = Path(__file__).parent.parent / "static" / "frog"
    prefix = settings.frog_game_path
    choose_mode = settings.frog_mode_selection_enabled
    html = render_game_page(
        (assets / "index.html").read_text(encoding="utf-8"), captcha=True,
        live_transport=settings.frog_live_transport_enabled,
        mode_selection=choose_mode,
    ).replace(
        "/test/game/assets/",
        f"{prefix}/assets/",
    )

    async def registered(session: AsyncSession, gid: int, uid: int) -> bool:
        return (await session.scalar(select(Participant.id).where(
            Participant.giveaway_id == gid, Participant.user_id == uid,
        ))) is not None

    async def context(
        session: AsyncSession, gid: int, uid: int, *, locked: bool,
        received: datetime | None = None, ranking_only: bool = False,
    ):
        if locked:
            await lock_giveaway_intake(session, gid)
        giveaway = await get_giveaway(session, gid)
        if not giveaway or giveaway.status != "published":
            raise web.HTTPConflict(text="Приём заявок закрыт.")
        if ranking_only:
            if not accepts_new_requests(giveaway, received or utc_now()):
                raise web.HTTPConflict(text="Приём результатов для рейтинга завершён.")
            if not await registered(session, gid, uid):
                raise web.HTTPConflict(text="Сначала подтвердите участие в розыгрыше.")
            return giveaway, None
        flow = None
        if settings.participation_queue_enabled:
            query = select(ParticipationFlow).where(
                ParticipationFlow.giveaway_id == gid,
                ParticipationFlow.user_id == uid,
            )
            if locked:
                query = query.with_for_update()
            flow = await session.scalar(query)
            if (
                flow is None
                or flow.status != "game"
                or flow.expires_at is None
                or flow.expires_at <= (received or utc_now())
            ):
                raise web.HTTPConflict(text="Проверка истекла. Нажмите кнопку участия в боте.")
        elif not is_giveaway_open(giveaway):
            raise web.HTTPConflict(text="Приём заявок закрыт.")
        return giveaway, flow

    async def persist(game: dict, token: str) -> None:
        ttl = max(1000, min(360_000, int((game["started"] + 360 - time.time()) * 1000)))
        await store_game(
            redis, f"frog:current:{game['user']['id']}", f"frog:session:{token}", token, game, ttl
        )

    def snapshot(game: dict, token: str) -> dict:
        public = public_game(game, time.time())
        if choose_mode:
            public["hardAttemptsLeft"] = game.get("hard_attempts_left", HARD_ATTEMPT_LIMIT)
            if game.get("ranking_only"):
                public["registration"] = "registered"
        if not game.get("live"):
            public.update(token=token, nonce=game["nonce"])
        return public

    def payload(game: dict) -> dict:
        result = {
            key: game.get(key)
            for key in (
                "user",
                "started",
                "checked",
                "difficulty",
                "hard_key",
                "challenge_id",
            )
        }
        if game["status"] == "won":
            result["state"] = game.copy()
        return {
            "started": game["started"],
            "finished": game["checked"],
            "status": game["status"],
            "seconds": round(game["checked"] - game["started"], 3),
            "moves": game["moves"],
            "move_count": len(game["moves"]),
            "lanes": game["lanes"],
            "difficulty": game.get("difficulty", "easy"),
            "checkpoint_reached": game.get("checkpoint_reached", False),
            "game_result": result,
        }

    async def prepare_result(game: dict) -> None:
        if game.get("logged"):
            return
        uid, gid = game["user"]["id"], game["giveaway_id"]
        async with sessions.begin() as session:
            if game.get("ranking_only"):
                if game["status"] == "won":
                    finished = datetime.fromtimestamp(game["checked"], UTC)
                    await context(session, gid, uid, locked=True, received=finished,
                                  ranking_only=True)
                    await record_hard_completion(
                        session, gid, uid, game["hard_key"],
                        round((game["checked"] - game["started"]) * 1000), finished,
                    )
                await session.execute(update(AuditLog).where(
                    AuditLog.id == game["audit_id"], AuditLog.actor_user_id == uid,
                ).values(action="frog.run.finished", payload=payload(game)))
                game["logged"] = True
                return
            # A negative update ID is reserved for a server-verified game result.
            job = (
                await session.scalar(
                    select(ParticipationJob)
                    .where(
                        ParticipationJob.update_id == -game["audit_id"],
                    )
                    .with_for_update()
                )
                if settings.participation_queue_enabled
                else None
            )
            if game["status"] == "won" and settings.participation_queue_enabled and job is None:
                _, flow = await context(
                    session,
                    gid,
                    uid,
                    locked=True,
                    received=datetime.fromtimestamp(game["checked"], UTC),
                )
                assert flow is not None
                if flow.challenge_id != game["challenge_id"]:
                    raise web.HTTPConflict(text="Проверка заменена. Откройте новую игру из бота.")
                session.add(
                    ParticipationJob(
                        update_id=-game["audit_id"],
                        flow_id=flow.id,
                        kind="game_win",
                        received_at=datetime.fromtimestamp(game["checked"], UTC),
                        challenge_id=game["audit_id"],
                        available_at=(
                            None if game.get("live") else utc_now() + timedelta(seconds=15)
                        ),
                    )
                )
            await session.execute(
                update(AuditLog)
                .where(
                    AuditLog.id == game["audit_id"],
                    AuditLog.actor_user_id == uid,
                )
                .values(action="frog.run.finished", payload=payload(game))
            )
        game["logged"] = True

    async def finalize(game: dict) -> None:
        if game.get("finalized"):
            return
        uid, gid = game["user"]["id"], game["giveaway_id"]
        async with sessions() as session:
            existing = await session.scalar(
                select(Participant.id).where(
                    Participant.giveaway_id == gid,
                    Participant.user_id == uid,
                )
            )
            snapshot = await get_giveaway(session, gid)
        condition = None
        if not existing:
            if snapshot is None:
                raise web.HTTPConflict(text="Розыгрыш недоступен.")
            # No database connection or giveaway-wide lock is held during Telegram I/O.
            from tg_drop_bot.services import giveaways

            async with asyncio.timeout(12):
                condition = await giveaways.check_participant_conditions(bot, snapshot, uid)
            if condition.membership_status == "check_failed":
                raise web.HTTPServiceUnavailable(
                    text="Проверка подписки временно недоступна. Повторяем.",
                )
        created = False
        async with sessions.begin() as session:
            job = (
                await session.scalar(
                    select(ParticipationJob)
                    .where(
                        ParticipationJob.update_id == -game["audit_id"],
                    )
                    .with_for_update()
                )
                if settings.participation_queue_enabled
                else None
            )
            # Match the worker's lock order: job -> giveaway -> flow.
            if await session.scalar(
                select(Participant.id).where(
                    Participant.giveaway_id == gid,
                    Participant.user_id == uid,
                )
            ):
                if game.get("difficulty") == "hard" and game.get("hard_key"):
                    await record_hard_completion(
                        session,
                        gid,
                        uid,
                        game["hard_key"],
                        round((game["checked"] - game["started"]) * 1000),
                        datetime.fromtimestamp(game["checked"], UTC),
                    )
                if job is not None and job.processed_at is None:
                    job.processed_at, job.reply_text = utc_now(), None
                game["finalized"] = True
                return
            assert condition is not None and snapshot is not None
            giveaway, flow = await context(
                session,
                gid,
                uid,
                locked=True,
                received=datetime.fromtimestamp(game["checked"], UTC),
            )
            if flow is not None and flow.challenge_id != game["challenge_id"]:
                raise web.HTTPConflict(text="Проверка заменена. Откройте новую игру из бота.")
            if giveaway.channel_id != snapshot.channel_id:
                raise web.HTTPServiceUnavailable(text="Канал изменился. Повторяем проверку.")
            registration = await register_participant_after_condition_check(
                session,
                bot,
                giveaway,
                User.model_validate(game["user"] | {"is_bot": False}),
                received_at=datetime.fromtimestamp(game["checked"], UTC),
                condition_check=condition,
            )
            if registration.participant is None:
                raise web.HTTPForbidden(
                    text=registration.condition_check.user_message
                    or "Условия участия не выполнены."
                )
            created = registration.created
            if created:
                registration.participant.captcha_response_ms = captcha_response_ms(
                    flow.captcha_sent_at if flow else None,
                    datetime.fromtimestamp(game["checked"], UTC),
                )
                session.add(
                    AuditLog(
                        giveaway_id=gid,
                        actor_user_id=uid,
                        action="participant.frog_passed",
                        payload={"seconds": round(game["checked"] - game["started"], 2)},
                    )
                )
            if flow is not None:
                from tg_drop_bot.services.participation_worker import close_flow

                close_flow(flow)
            if game.get("difficulty") == "hard" and game.get("hard_key"):
                await record_hard_completion(
                    session,
                    gid,
                    uid,
                    game["hard_key"],
                    round((game["checked"] - game["started"]) * 1000),
                    datetime.fromtimestamp(game["checked"], UTC),
                )
            if job is not None:
                job.processed_at = utc_now()
                job.reply_text = "Лягушка добралась! Ваше участие засчитано." if created else None
                job.available_at = None
                job.dispatch_at = utc_now()
        game["finalized"] = True
        if created and not settings.participation_queue_enabled:
            with contextlib.suppress(TelegramAPIError, TimeoutError):
                async with asyncio.timeout(3):
                    await bot.send_message(uid, "Лягушка добралась! Ваше участие засчитано.")

    async def registration_status(game: dict) -> str:
        async with sessions() as session:
            registered = select(Participant.id).where(
                Participant.giveaway_id == game["giveaway_id"],
                Participant.user_id == game["user"]["id"],
            ).exists()
            pending = select(ParticipationJob.id).where(
                ParticipationJob.update_id == -game["audit_id"],
                ParticipationJob.processed_at.is_(None),
            ).exists()
            return (
                await session.scalar(
                    select(case((registered, "registered"), (pending, "pending"), else_="rejected"))
                )
                or "rejected"
            )

    def live_clock(game: dict) -> None:
        now = time.time()
        if now >= game["flow_expires_at"] and game["status"] == "playing":
            game["status"] = "expired"
        else:
            seq = game["seq"]
            advance(game, "wait", seq, now)
            game["seq"] = seq

    async def locked(
        action: str, data: dict, user: dict, *, connection: str | None = None,
    ) -> dict:
        uid = user["id"]
        live = connection is not None
        if action in ("start", "resume"):
            if action == "resume" and not live:
                raise web.HTTPNotFound()
            require_current_game_client(data)
            gid = int(data["giveaway"])
            reopening = gid == settings.frog_reopen_hard_giveaway_id
            requested = None
            if choose_mode and action == "start":
                requested = data.get("difficulty", "easy")
                if requested not in ("easy", "hard"):
                    raise ValueError("invalid game mode")
            token = await redis.get(f"frog:current:{uid}")
            raw = await redis.get(f"frog:session:{token}") if token else None
            previous = json.loads(raw) if raw else None
            if previous and previous.get("user", {}).get("id") != uid:
                raise ValueError("wrong owner")
            if (
                settings.participation_queue_enabled
                and previous
                and previous.get("giveaway_id") == gid
                and previous.get("user", {}).get("id") == uid
                and previous.get("status") == "won"
            ):
                async with sessions() as session:
                    challenge_id = await session.scalar(
                        select(ParticipationFlow.challenge_id).where(
                            ParticipationFlow.giveaway_id == gid,
                            ParticipationFlow.user_id == uid,
                        )
                    )
                    if challenge_id != previous.get("challenge_id") and not await session.scalar(
                        select(Participant.id).where(
                            Participant.giveaway_id == gid,
                            Participant.user_id == uid,
                        )
                    ):
                        # A processed refusal can be followed by a new bot challenge.
                        # Only an unfinished result for the current challenge may resume.
                        previous = None
            if not (
                previous and previous.get("giveaway_id") == gid and previous.get("status") == "won"
            ):
                async with sessions() as session:
                    proof = await session.scalar(
                        select(AuditLog)
                        .join(
                            ParticipationJob,
                            ParticipationJob.challenge_id == AuditLog.id,
                        )
                        .join(ParticipationFlow, ParticipationFlow.id == ParticipationJob.flow_id)
                        .where(
                            ParticipationJob.kind == "game_win",
                            ParticipationJob.processed_at.is_(None),
                            ParticipationFlow.user_id == uid,
                            ParticipationFlow.giveaway_id == gid,
                            ParticipationFlow.status == "game",
                            AuditLog.actor_user_id == uid,
                            AuditLog.giveaway_id == gid,
                            AuditLog.payload["game_result"]["challenge_id"].as_integer()
                            == ParticipationFlow.challenge_id,
                        )
                        .limit(1)
                    )
                    if proof is not None:
                        previous = proof.payload.get("game_result", {}).get("state")
                        if not previous or previous.get("status") != "won":
                            raise web.HTTPServiceUnavailable(
                                text="Финиш принят, повторяем проверку."
                            )
                        previous["logged"] = True
                        token = secrets.token_urlsafe(32)
            won_here = previous and previous["giveaway_id"] == gid and previous["status"] == "won"
            new_ranking_attempt = False
            if (
                previous is not None and won_here and requested == "hard"
                and (previous["difficulty"] == "easy" or reopening)
            ):
                async with sessions() as session:
                    new_ranking_attempt = await registered(session, gid, uid)
            switching = (
                requested is not None and previous and previous["giveaway_id"] == gid
                and previous["status"] == "playing"
                and (previous["difficulty"] != requested or "hard_attempts_left" not in previous)
            )
            if previous is not None and won_here and not new_ranking_attempt:
                game = previous
                if bool(game.get("live")) != live:
                    if live and action == "start":
                        return {"transport": "http"}
                    raise web.HTTPConflict(text="Откройте игру так же, как начали партию.")
            else:
                if (
                    live and previous and previous.get("live") and previous["giveaway_id"] == gid
                    and not switching
                    and (action == "resume" or previous["status"] == "playing")
                ):
                    if previous["status"] == "playing":
                        live_clock(previous)
                    if previous["status"] == "playing":
                        async with sessions() as session:
                            ranking_only = bool(previous.get("ranking_only"))
                            _, flow = await context(
                                session, gid, uid, locked=False, ranking_only=ranking_only,
                            )
                            if not ranking_only and (
                                flow is None or flow.challenge_id != previous["challenge_id"]
                            ):
                                raise web.HTTPConflict(
                                    text="Проверка заменена. Откройте игру из бота."
                                )
                    previous["connection"] = connection
                    await persist(previous, token)
                    if previous["status"] != "playing":
                        await prepare_result(previous)
                        await persist(previous, token)
                    return snapshot(previous, token)
                if action == "resume":
                    raise web.HTTPConflict(text="Партия недоступна. Начните заново из игры.")
                async with sessions() as session:
                    already_registered = await registered(session, gid, uid)
                    ranking_only = requested == "hard" and already_registered
                    if already_registered and not ranking_only:
                        raise web.HTTPConflict(text="Ваше участие уже засчитано.")
                    _, flow = await context(
                        session, gid, uid, locked=False, ranking_only=ranking_only,
                    )
                    challenge_id = flow.challenge_id if flow else None
                    attempt = await session.get(HardGameAttempt, (gid, uid))
                    used = attempt.attempts_used if attempt else 0
                    hard_left = hard_attempts_left(attempt, reopening=reopening)
                    assisted = False
                    from_checkpoint = False
                    if requested == "easy":
                        easy_losses, checkpoint = (await session.execute(select(
                            func.count(),
                            func.bool_or(AuditLog.payload["checkpoint_reached"].as_boolean()),
                        ).where(
                                AuditLog.giveaway_id == gid, AuditLog.actor_user_id == uid,
                                AuditLog.action == "frog.run.finished",
                                AuditLog.payload["difficulty"].as_string() == "easy",
                                AuditLog.payload["status"].as_string() == "lost",
                        ))).one()
                        assisted = easy_losses >= 2
                        from_checkpoint = bool(checkpoint)
                if (
                    previous
                    and previous["giveaway_id"] == gid
                    and previous.get("user", {}).get("id") == uid
                    and previous.get("challenge_id") == challenge_id
                    and previous["status"] == "playing"
                    and not switching
                    and (
                        choose_mode or previous.get("difficulty") == "hard" or previous.get("live")
                    )
                ):
                    if bool(previous.get("live")) != live:
                        if live:
                            return {"transport": "http"}
                        raise web.HTTPConflict(text="Откройте игру так же, как начали партию.")
                    previous = advance(previous, "wait", previous["seq"], time.time())
                    if previous["status"] == "playing":
                        await persist(previous, token)
                        return snapshot(previous, token)
                    await prepare_result(previous)
                # CPU generation is outside transactions and giveaway-wide locks.
                await quota(redis, f"frog:start:{uid}:{int(time.time()) // 60}", 10, 120)
                if requested == "hard" and not hard_left:
                    raise web.HTTPConflict(
                        text="Сложные попытки закончились. Можно выбрать лёгкий режим.",
                    )
                if requested is not None:
                    game = await generator.generate(
                        user, gid, requested, relaxed=True, assisted=assisted,
                        from_checkpoint=from_checkpoint,
                    )
                else:
                    game = await generator.generate(user, gid, "easy" if used else "hard")
                hard_key = secrets.token_urlsafe(24)
                async with sessions.begin() as session:
                    giveaway, flow = await context(
                        session, gid, uid, locked=True, ranking_only=ranking_only,
                    )
                    challenge_id = flow.challenge_id if flow else None
                    flow_expires_at = (
                        flow.expires_at.timestamp() if flow and flow.expires_at else None
                    )
                    if ranking_only:
                        flow_expires_at = (
                            giveaway.deadline_at.timestamp() if giveaway.deadline_at
                            else time.time() + 300
                        )
                    elif await registered(session, gid, uid):
                        raise web.HTTPConflict(text="Ваше участие уже засчитано.")
                    if choose_mode:
                        hard = requested == "hard"
                        if hard:
                            result = await claim_hard_game_slot(
                                session, gid, uid, hard_key, reopening=reopening,
                            )
                            if result is None:
                                raise web.HTTPConflict(text="Сложные попытки закончились.")
                            hard_left = result
                    else:
                        hard = await claim_hard_attempt(session, gid, uid, hard_key)
                if hard != (game["difficulty"] == "hard"):
                    game = await generator.generate(user, gid, "hard" if hard else "easy")
                issued = time.time()
                game.update(
                    started=issued,
                    checked=issued,
                    last_move=issued - 1,
                    hard_key=hard_key if hard else None,
                    challenge_id=challenge_id,
                    nonce=secrets.token_urlsafe(24),
                )
                if live or choose_mode:
                    assert flow_expires_at is not None
                    game.update(flow_expires_at=flow_expires_at)
                if live:
                    game.update(live=True, connection=connection)
                if choose_mode:
                    game.update(hard_attempts_left=hard_left, ranking_only=ranking_only)
                async with sessions.begin() as session:
                    if switching and previous is not None:
                        abandoned = previous | {"status": "abandoned"}
                        await session.execute(update(AuditLog).where(
                            AuditLog.id == previous["audit_id"], AuditLog.actor_user_id == uid,
                        ).values(action="frog.run.finished", payload=payload(abandoned)))
                    record = AuditLog(
                        giveaway_id=gid,
                        actor_user_id=uid,
                        action="frog.run.started",
                        payload={
                            "started": issued,
                            "status": "started",
                            "difficulty": game["difficulty"],
                        },
                    )
                    session.add(record)
                    await session.flush()
                    game["audit_id"] = record.id
                token = secrets.token_urlsafe(32)
        elif action in ("move", "sync"):
            if action == "sync" and not live:
                raise web.HTTPNotFound()
            token = await redis.get(f"frog:current:{uid}") if live else data["token"]
            if not isinstance(token, str) or len(token) > 100:
                raise ValueError("invalid token")
            current, raw = await redis.mget(f"frog:current:{uid}", f"frog:session:{token}")
            if token != current or raw is None:
                raise ValueError("expired or replaced session")
            game = json.loads(raw)
            if game["user"]["id"] != uid:
                raise ValueError("wrong owner")
            if bool(game.get("live")) != live or (live and game["connection"] != connection):
                raise web.HTTPConflict(text="Партия открыта в другом окне. Откройте игру заново.")
            if not live:
                game = await execute_command(game, data, deadline=game.get("flow_expires_at"))
            elif game["status"] == "playing":
                if action == "sync":
                    live_clock(game)
                elif data["seq"] == game["seq"]:
                    game = await execute_command(
                        game, {"seq": data["seq"], "direction": "up", "nonce": game["nonce"]},
                        deadline=game["flow_expires_at"],
                    )
        else:
            raise web.HTTPNotFound()
        if live:
            game["connection"] = connection
        await persist(game, token)
        if game["status"] != "playing":
            await prepare_result(game)
            await persist(game, token)
        if game["status"] == "won" and not live and not game.get("ranking_only"):
            await finalize(game)
            await persist(game, token)
        public = snapshot(game, token)
        if live and game["status"] == "won":
            public["registration"] = await registration_status(game)
        if choose_mode and game["status"] == "won":
            if not live:
                public["registration"] = "registered"
            if (
                game["difficulty"] == "hard" and public.get("registration") == "registered"
                and game["giveaway_id"] != settings.frog_reopen_hard_giveaway_id
            ):
                public["hardAttemptsLeft"] = 0
        return public

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text=html, content_type="text/html", headers={"Cache-Control": "no-store"}
        )

    async def api(request: web.Request) -> web.Response:
        try:
            data = await read_game_request(request)
            user = validate_init_data(data["initData"], settings.bot_token, time.time())
            await quota(redis, f"frog:rate:{user['id']}:{int(time.time()) // 10}", 100, 20)
            async with redis.lock(f"frog:lock:{user['id']}", timeout=30, blocking_timeout=2):
                state = await locked(request.match_info["action"], data, user)
                return web.json_response(
                    state,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="Проверка устарела. Откройте игру из бота.") from None
        except (RedisError, TimeoutError):
            raise web.HTTPServiceUnavailable(
                text="Сервис временно занят. Повторяем запрос."
            ) from None

    async def cleanup(_app: web.Application) -> None:
        await redis.aclose()

    app.on_cleanup.append(cleanup)
    app.router.add_get(f"{prefix}/", page)
    app.router.add_post(f"{prefix}/api/{{action}}", api)
    app.router.add_static(f"{prefix}/assets/", assets / "assets")
    if settings.frog_live_transport_enabled:
        register_frog_socket(app, settings, redis, locked)
