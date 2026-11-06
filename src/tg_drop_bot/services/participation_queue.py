from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Literal

from aiogram.types import User
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Exists

from tg_drop_bot.db.models import Giveaway, ParticipationFlow, ParticipationJob
from tg_drop_bot.services.dates import utc_now

Admission = Literal["accepted", "duplicate", "closed", "overloaded", "ignored", "cooldown"]


async def lock_giveaway_intake(session: AsyncSession, giveaway_id: int) -> Giveaway | None:
    # NO KEY UPDATE serializes admissions and closing without blocking FK inserts.
    return await session.scalar(
        select(Giveaway)
        .where(Giveaway.id == giveaway_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )


def accepts_new_requests(giveaway: Giveaway, received_at: datetime) -> bool:
    return (
        giveaway.status == "published"
        and giveaway.channel_id is not None
        and (giveaway.intake_closed_at is None or received_at < giveaway.intake_closed_at)
        and (giveaway.deadline_at is None or received_at < giveaway.deadline_at)
    )


async def admit_start(
    session: AsyncSession,
    *,
    giveaway_id: int,
    user: User,
    update_id: int,
    received_at: datetime,
    allow_new: Callable[[], Awaitable[bool]],
) -> Admission:
    giveaway = await lock_giveaway_intake(session, giveaway_id)
    if giveaway is None:
        return "closed"
    if await session.scalar(
        select(ParticipationJob.id).where(ParticipationJob.update_id == update_id)
    ):
        return "duplicate"
    if not accepts_new_requests(giveaway, received_at):
        return "closed"
    flow = await session.scalar(
        select(ParticipationFlow).where(
            ParticipationFlow.giveaway_id == giveaway_id, ParticipationFlow.user_id == user.id
        )
    )
    if flow is not None:
        if flow.status == "waiting" or (
            flow.status == "captcha" and flow.expires_at and flow.expires_at > received_at
        ):
            return "duplicate"
        if flow.cooldown_until and flow.cooldown_until > received_at:
            return "cooldown"
        if await session.scalar(
            select(ParticipationJob.id)
            .where(ParticipationJob.flow_id == flow.id, ParticipationJob.processed_at.is_(None))
            .limit(1)
        ):
            return "duplicate"
    if not await allow_new():
        return "overloaded"
    profile = user.model_dump(include={"id", "is_bot", "first_name", "last_name", "username"})
    if flow is None:
        flow = ParticipationFlow(
            giveaway_id=giveaway_id, user_id=user.id, user_profile=profile, status="waiting"
        )
        session.add(flow)
        await session.flush()
    else:
        flow.user_profile = profile
        flow.status = "waiting"
        flow.challenge_id = None
        flow.answer_hash = None
        flow.expires_at = None
        flow.cooldown_until = None
        flow.attempts = 0
    session.add(
        ParticipationJob(
            update_id=update_id, flow_id=flow.id, kind="start", received_at=received_at
        )
    )
    return "accepted"


async def admit_captcha_action(
    session: AsyncSession,
    *,
    user_id: int,
    update_id: int,
    received_at: datetime,
    max_attempts: int,
    answer_hash: str | None = None,
    challenge_id: int | None = None,
) -> Admission:
    if challenge_id is None and answer_hash is None:
        raise ValueError("A captcha action requires an answer or a challenge ID")
    statement = (
        select(ParticipationFlow)
        .join(Giveaway, Giveaway.id == ParticipationFlow.giveaway_id)
        .where(
            ParticipationFlow.user_id == user_id,
            ParticipationFlow.status == "captcha",
            ParticipationFlow.expires_at > received_at,
            Giveaway.status == "published",
        )
    )
    if challenge_id is not None:
        statement = statement.where(ParticipationFlow.challenge_id == challenge_id)
    else:
        statement = statement.order_by(
            (ParticipationFlow.answer_hash == answer_hash).desc(),
            ParticipationFlow.challenge_id.desc(),
        )
    flow = await session.scalar(statement.limit(1))
    if flow is None:
        return "ignored"
    giveaway = await lock_giveaway_intake(session, flow.giveaway_id)
    if giveaway is None or giveaway.status != "published":
        return "closed"
    await session.refresh(flow)
    if flow.status != "captcha" or flow.expires_at is None or flow.expires_at <= received_at:
        return "ignored"
    if challenge_id is not None and flow.challenge_id != challenge_id:
        return "closed"
    kind = "answer" if answer_hash is not None else "refresh"
    pending = (
        await session.scalar(
            select(func.count())
            .select_from(ParticipationJob)
            .where(
                ParticipationJob.flow_id == flow.id,
                ParticipationJob.kind == kind,
                ParticipationJob.challenge_id == flow.challenge_id,
                ParticipationJob.processed_at.is_(None),
            )
        )
        or 0
    )
    if (kind == "refresh" and pending) or (
        kind == "answer" and pending + flow.attempts >= max_attempts
    ):
        return "duplicate"
    inserted = await session.scalar(
        insert(ParticipationJob)
        .values(
            update_id=update_id,
            flow_id=flow.id,
            kind=kind,
            received_at=received_at,
            answer_hash=answer_hash,
            challenge_id=flow.challenge_id,
        )
        .on_conflict_do_nothing(index_elements=[ParticipationJob.update_id])
        .returning(ParticipationJob.id)
    )
    return "accepted" if inserted else "duplicate"


def earlier_unprocessed_job() -> Exists:
    previous = aliased(ParticipationJob)
    return (
        select(previous.id)
        .where(
            previous.flow_id == ParticipationJob.flow_id,
            previous.id < ParticipationJob.id,
            previous.processed_at.is_(None),
        )
        .exists()
    )


async def lease_jobs_for_dispatch(
    session: AsyncSession,
    *,
    priority: bool,
    batch_size: int = 16,
    lease_seconds: int = 15,
) -> list[int]:
    now = utc_now()
    unfinished = or_(
        ParticipationJob.processed_at.is_(None), ParticipationJob.reply_text.is_not(None)
    )
    lane = (ParticipationJob.kind != "start") if priority else (ParticipationJob.kind == "start")
    in_flight = (
        await session.scalar(
            select(func.count())
            .select_from(ParticipationJob)
            .where(
                lane,
                unfinished,
                ParticipationJob.dispatch_at > now,
            )
        )
        or 0
    )
    if in_flight >= batch_size:
        return []
    ids = list(
        (
            await session.scalars(
                select(ParticipationJob.id)
                .where(
                    lane,
                    unfinished,
                    or_(
                        ParticipationJob.dispatch_at.is_(None), ParticipationJob.dispatch_at <= now
                    ),
                    ~earlier_unprocessed_job(),
                )
                .order_by((ParticipationJob.kind == "answer").desc(), ParticipationJob.id)
                .limit(batch_size - in_flight)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    if ids:
        await session.execute(
            update(ParticipationJob)
            .where(ParticipationJob.id.in_(ids))
            .values(dispatch_at=now + timedelta(seconds=lease_seconds))
        )
    return ids


async def giveaway_has_pending_intake(
    session: AsyncSession, giveaway_id: int, *, now: datetime
) -> bool:
    pending = (
        select(ParticipationJob.id)
        .where(
            ParticipationJob.flow_id == ParticipationFlow.id,
            ParticipationJob.processed_at.is_(None),
        )
        .exists()
    )
    return bool(
        await session.scalar(
            select(
                select(ParticipationFlow.id)
                .where(
                    ParticipationFlow.giveaway_id == giveaway_id,
                    or_(
                        pending,
                        (ParticipationFlow.status == "captcha")
                        & (ParticipationFlow.expires_at > now),
                    ),
                )
                .exists()
            )
        )
    )


async def cleanup_participation_queue(session: AsyncSession) -> None:
    now = utc_now()
    # Finished jobs retain only their ID briefly for Telegram redelivery deduplication.
    terminal = select(Giveaway.id).where(Giveaway.status.in_(["finished", "cancelled"]))
    await session.execute(
        delete(ParticipationFlow).where(
            ParticipationFlow.id.in_(
                select(ParticipationFlow.id)
                .where(
                    ParticipationFlow.giveaway_id.in_(terminal),
                    ~select(ParticipationJob.id)
                    .where(
                        ParticipationJob.flow_id == ParticipationFlow.id,
                        ParticipationJob.received_at >= now - timedelta(days=1),
                    )
                    .exists(),
                )
                .limit(1000)
            ),
        )
    )
    await session.execute(
        delete(ParticipationJob).where(
            ParticipationJob.id.in_(
                select(ParticipationJob.id)
                .where(
                    ParticipationJob.processed_at < now - timedelta(days=1),
                    ParticipationJob.reply_text.is_(None),
                )
                .limit(1000)
            ),
        )
    )
    await session.execute(
        update(ParticipationFlow)
        .where(
            ParticipationFlow.status == "captcha",
            ParticipationFlow.expires_at < now - timedelta(minutes=1),
            ~select(ParticipationJob.id)
            .where(
                ParticipationJob.flow_id == ParticipationFlow.id,
                ParticipationJob.processed_at.is_(None),
            )
            .exists(),
        )
        .values(status="done", answer_hash=None)
    )
