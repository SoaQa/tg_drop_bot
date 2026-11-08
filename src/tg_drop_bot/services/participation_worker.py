from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.bot.keyboards import captcha_keyboard
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway, Participant, ParticipationFlow, ParticipationJob
from tg_drop_bot.services.captcha import (
    CAPTCHA_ALPHABET,
    captcha_response_ms,
    hash_captcha_answer,
    render_captcha_png,
)
from tg_drop_bot.services.conditions import check_participant_conditions
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import get_giveaway, register_participant_after_condition_check
from tg_drop_bot.services.membership import PARTICIPATION_DENIED, get_active_exclusion
from tg_drop_bot.services.participation_queue import accepts_new_requests, earlier_unprocessed_job

CLOSED = "Прием заявок в этом розыгрыше уже закрыт."
CAPTCHA_EXPIRED = "Капча истекла. Нажмите кнопку участия еще раз."


class RetryParticipation(Exception):
    def __init__(self, delay: int = 0) -> None:
        self.delay = delay


def queued_captcha_code(settings: Settings, challenge_id: int) -> str:
    # The same job can be redelivered after a crash. Recreate the same code without storing it.
    return "".join(
        CAPTCHA_ALPHABET[
            int.from_bytes(
                hmac.digest(
                    settings.app_secret_key.encode(),
                    f"queued-captcha:{challenge_id}:{i}".encode(),
                    hashlib.sha256,
                )[:4],
                "big",
            )
            % len(CAPTCHA_ALPHABET)
        ]
        for i in range(settings.captcha_length)
    )


def close_flow(flow: ParticipationFlow) -> None:
    flow.status = "done"
    flow.answer_hash = None
    flow.captcha_sent_at = None
    flow.expires_at = None
    flow.challenge_id = None


async def issue_captcha(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    flow: ParticipationFlow,
    job: ParticipationJob,
) -> str | None:
    previous_expiry = flow.expires_at
    if job.kind == "refresh" and (
        flow.status != "captcha"
        or job.challenge_id != flow.challenge_id
        or previous_expiry is None
        or previous_expiry <= utc_now()
    ):
        return None
    condition = await check_participant_conditions(bot, giveaway, flow.user_id)
    if not condition.ok:
        close_flow(flow)
        return condition.user_message
    if await session.scalar(
        select(Participant.id).where(
            Participant.giveaway_id == flow.giveaway_id, Participant.user_id == flow.user_id
        )
    ):
        close_flow(flow)
        return "Ваше участие уже засчитано."

    code = queued_captcha_code(settings, job.id)
    picture = await asyncio.to_thread(render_captcha_png, code)
    try:
        await bot.send_photo(
            flow.user_id,
            BufferedInputFile(picture, filename="captcha.png"),
            caption="Введите код с картинки. Капча действует несколько минут.",
            reply_markup=captcha_keyboard(job.id, queued=True),
        )
    except (TelegramForbiddenError, TelegramBadRequest):
        close_flow(flow)
        return None
    delivered_at = utc_now()
    expires_at = delivered_at + timedelta(seconds=settings.captcha_ttl_seconds)
    if previous_expiry is not None and not accepts_new_requests(giveaway, delivered_at):
        expires_at = min(expires_at, previous_expiry)
    flow.status = "captcha"
    flow.challenge_id = job.id
    flow.answer_hash = hash_captcha_answer(code, settings)
    flow.captcha_sent_at = delivered_at
    flow.expires_at = expires_at
    # Refreshes change the picture, not the attempt budget.
    return None


async def handle_answer(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    flow: ParticipationFlow,
    job: ParticipationJob,
) -> str | None:
    if flow.status != "captcha" or flow.challenge_id != job.challenge_id:
        return None
    if flow.expires_at is None or job.received_at >= flow.expires_at:
        close_flow(flow)
        return CAPTCHA_EXPIRED
    if (
        not flow.answer_hash
        or not job.answer_hash
        or not hmac.compare_digest(flow.answer_hash, job.answer_hash)
    ):
        flow.attempts += 1
        remaining = settings.captcha_max_attempts - flow.attempts
        if remaining > 0:
            return f"Код неверный. Осталось попыток: {remaining}."
        close_flow(flow)
        flow.cooldown_until = utc_now() + timedelta(seconds=settings.captcha_cooldown_seconds)
        return (
            f"Попытки закончились. Новую капчу можно получить через "
            f"{settings.captcha_cooldown_seconds} сек."
        )
    registration = await register_participant_after_condition_check(
        session, bot, giveaway, User.model_validate(flow.user_profile), received_at=job.received_at
    )
    if registration.created and registration.participant is not None:
        registration.participant.captcha_response_ms = captcha_response_ms(
            flow.captcha_sent_at, job.received_at
        )
    close_flow(flow)
    if registration.participant is None:
        return registration.condition_check.user_message or "Условия участия не выполнены."
    return "Готово, участие засчитано." if registration.created else "Ваше участие уже засчитано."


async def process_participation_job(
    sessions: async_sessionmaker[AsyncSession],
    bot: Bot,
    settings: Settings,
    job_id: int,
) -> None:
    async with sessions.begin() as session:
        job = await session.scalar(
            select(ParticipationJob)
            .where(
                ParticipationJob.id == job_id,
                ParticipationJob.processed_at.is_(None),
                ~earlier_unprocessed_job(),
            )
            .with_for_update(skip_locked=True)
        )
        if job is not None:
            flow = await session.get(ParticipationFlow, job.flow_id, with_for_update=True)
            if flow is None:
                return
            giveaway = await get_giveaway(session, flow.giveaway_id)
            if giveaway is None or giveaway.status != "published":
                close_flow(flow)
                job.reply_text = CLOSED
            elif giveaway.channel_id is not None and await get_active_exclusion(
                session, giveaway.channel_id, flow.user_id
            ):
                close_flow(flow)
                job.reply_text = PARTICIPATION_DENIED
            elif job.kind == "answer":
                job.reply_text = await handle_answer(session, bot, settings, giveaway, flow, job)
            else:
                job.reply_text = await issue_captcha(session, bot, settings, giveaway, flow, job)
            job.processed_at = utc_now()
            job.answer_hash = None
            job.failures = 0
    # Failure delivering a confirmation must not undo a registration or consume a second attempt.
    async with sessions.begin() as session:
        job = await session.scalar(
            select(ParticipationJob)
            .where(
                ParticipationJob.id == job_id,
                ParticipationJob.reply_text.is_not(None),
                ParticipationJob.processed_at.is_not(None),
            )
            .with_for_update(skip_locked=True)
        )
        if job is not None and job.reply_text is not None:
            flow = await session.get(ParticipationFlow, job.flow_id)
            if flow is not None:
                with contextlib.suppress(TelegramForbiddenError, TelegramBadRequest):
                    await bot.send_message(flow.user_id, job.reply_text)
            job.reply_text = None


async def defer_participation_job(
    sessions: async_sessionmaker[AsyncSession],
    job_id: int,
    delay: int,
) -> None:
    async with sessions.begin() as session:
        job = await session.get(ParticipationJob, job_id, with_for_update=True)
        if job is not None and (job.processed_at is None or job.reply_text is not None):
            job.failures += 1
            retry_delay = max(delay, min(2 ** min(job.failures, 6), 60))
            job.dispatch_at = utc_now() + timedelta(seconds=retry_delay)
