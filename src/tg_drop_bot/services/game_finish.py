"""Durable completion of an already server-validated raffle game."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog, Giveaway, ParticipationFlow, ParticipationJob
from tg_drop_bot.services.giveaways import register_participant_after_condition_check
from tg_drop_bot.services.hard_game import record_hard_completion
from tg_drop_bot.services.participation_worker import (
    CAPTCHA_EXPIRED,
    RetryParticipation,
    close_flow,
)

logger = logging.getLogger(__name__)


async def finish_game_job(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    flow: ParticipationFlow,
    job: ParticipationJob,
) -> str | None:
    """Trust only the database result, never a client supplied success flag.

    The caller holds the job and flow locks and commits registration, achievement
    and processed_at together. A temporary Telegram failure rolls that transaction
    back so the durable inbox can retry, even after the original flow expiry.
    """
    if job.kind != "game_win" or job.challenge_id is None:
        return None
    audit = await session.get(AuditLog, job.challenge_id)
    if (
        audit is None
        or audit.id != -job.update_id
        or audit.giveaway_id != giveaway.id
        or giveaway.id != flow.giveaway_id
        or job.flow_id != flow.id
        or audit.actor_user_id != flow.user_id
        or audit.action != "frog.run.finished"
        or audit.payload.get("status") != "won"
    ):
        logger.warning("Ignoring invalid game finish job %s", job.id)
        return None
    result = audit.payload.get("game_result")
    if not isinstance(result, dict):
        logger.warning("Missing server game result for finish job %s", job.id)
        return None
    raw_user = result.get("user")
    started, checked = result.get("started"), result.get("checked")
    if (
        not isinstance(raw_user, dict)
        or raw_user.get("id") != flow.user_id
        or not isinstance(started, (int, float))
        or isinstance(started, bool)
        or not isinstance(checked, (int, float))
        or isinstance(checked, bool)
        or not math.isfinite(started)
        or not math.isfinite(checked)
        or not 0 <= checked - started <= 300
    ):
        logger.warning("Invalid owner or timing for game finish job %s", job.id)
        return None
    try:
        completed_at = datetime.fromtimestamp(checked, UTC)
    except (OverflowError, OSError, ValueError):
        logger.warning("Invalid finish timestamp for game job %s", job.id)
        return None
    if completed_at != job.received_at:
        logger.warning("Mismatched event time for game finish job %s", job.id)
        return None
    if (
        flow.status != "game"
        or flow.expires_at is None
        or job.received_at >= flow.expires_at
        or result.get("challenge_id") != flow.challenge_id
    ):
        return CAPTCHA_EXPIRED
    registration = await register_participant_after_condition_check(
        session,
        bot,
        giveaway,
        User.model_validate(raw_user | {"is_bot": False}),
        received_at=job.received_at,
    )
    if registration.participant is None:
        if registration.condition_check.membership_status == "check_failed":
            raise RetryParticipation()
        close_flow(flow)
        return registration.condition_check.user_message or "Условия участия не выполнены."
    if registration.created:
        from tg_drop_bot.services.captcha import captcha_response_ms

        registration.participant.captcha_response_ms = captcha_response_ms(
            flow.captcha_sent_at,
            job.received_at,
        )
        session.add(
            AuditLog(
                giveaway_id=giveaway.id,
                actor_user_id=flow.user_id,
                action="participant.frog_passed",
                payload={
                    "seconds": round(checked - started, 2),
                    "difficulty": result.get("difficulty"),
                },
            )
        )
    if result.get("difficulty") == "hard" and isinstance(result.get("hard_key"), str):
        await record_hard_completion(
            session,
            giveaway.id,
            flow.user_id,
            result["hard_key"],
            round((checked - started) * 1000),
            completed_at,
        )
    close_flow(flow)
    return (
        "Лягушка добралась! Ваше участие засчитано."
        if registration.created
        else "Ваше участие уже засчитано."
    )
