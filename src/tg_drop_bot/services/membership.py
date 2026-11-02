from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import Select, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tg_drop_bot.db.models import (
    AuditLog,
    ChannelExclusion,
    ChannelExclusionPeriod,
    ChannelMembership,
    Giveaway,
    KnownChannel,
    MembershipEvent,
    Participant,
)
from tg_drop_bot.services.dates import utc_now

WINDOW = timedelta(hours=72)
CHECK_INTERVAL = timedelta(hours=6)


def newcomer_events(giveaway: Giveaway, user_id: int) -> Select[tuple[MembershipEvent]]:
    return select(MembershipEvent).where(
        MembershipEvent.channel_id == giveaway.channel_id,
        MembershipEvent.user_id == user_id,
        MembershipEvent.kind == "join",
        MembershipEvent.occurred_at > giveaway.published_at,
        MembershipEvent.occurred_at <= (giveaway.finished_at or utc_now()),
    )


async def snapshot_draw_eligibility(
    session: AsyncSession,
    giveaway: Giveaway,
    participants: list[Participant],
) -> list[Participant]:
    if not giveaway.anti_abuse_enabled and not isinstance(session, AsyncSession):
        return participants
    # One query per dataset, independent of participant count.
    newcomers = set(
        (
            await session.scalars(
                select(MembershipEvent.user_id).where(
                    MembershipEvent.channel_id == giveaway.channel_id,
                    MembershipEvent.kind == "join",
                    MembershipEvent.occurred_at > giveaway.published_at,
                    MembershipEvent.occurred_at <= (giveaway.finished_at or utc_now()),
                )
            )
        ).all()
    )
    exclusions = {
        e.user_id: e
        for e in (
            await session.scalars(
                select(ChannelExclusionPeriod).where(
                    ChannelExclusionPeriod.channel_id == giveaway.channel_id,
                    ChannelExclusionPeriod.started_at <= (giveaway.finished_at or utc_now()),
                    or_(
                        ChannelExclusionPeriod.ended_at.is_(None),
                        ChannelExclusionPeriod.ended_at > (giveaway.finished_at or utc_now()),
                    ),
                )
            )
        ).all()
    }
    for participant in participants:
        is_newcomer = giveaway.anti_abuse_enabled and participant.user_id in newcomers
        participant.selection_weight = 0.7 if is_newcomer else 1.0
        participant.selection_reason = "newcomer" if is_newcomer else "unknown"
        exclusion = exclusions.get(participant.user_id)
        participant.draw_excluded = exclusion is not None
        participant.exclusion_reason = exclusion.reason if exclusion else None
    return [p for p in participants if not p.draw_excluded]


async def exclude_user(
    session: AsyncSession,
    channel_id: int,
    user_id: int,
    occurred_at: datetime,
    evidence_event_id: int | None = None,
) -> bool:
    # Same transaction lock as event ingestion and revocation, including first insert.
    await lock_user(session, channel_id, user_id)
    exclusion = await session.scalar(
        select(ChannelExclusion).where(
            ChannelExclusion.channel_id == channel_id,
            ChannelExclusion.user_id == user_id,
        )
    )
    if exclusion is not None:
        if exclusion.revoked_at is None or occurred_at <= exclusion.revoked_at:
            return False
        exclusion.revoked_at = None
        exclusion.created_at = utc_now()
        exclusion.evidence_event_id = evidence_event_id
    else:
        exclusion = ChannelExclusion(
            channel_id=channel_id,
            user_id=user_id,
            created_at=utc_now(),
            evidence_event_id=evidence_event_id,
        )
        session.add(exclusion)
    exclusion.reason = "Отписка в течение 72 часов после итогов розыгрыша"
    session.add(
        ChannelExclusionPeriod(
            channel_id=channel_id,
            user_id=user_id,
            reason=exclusion.reason,
            started_at=exclusion.created_at,
        )
    )
    return True


async def lock_user(session: AsyncSession, channel_id: int, user_id: int) -> None:
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"membership:{channel_id}:{user_id}", 0)
            )
        )
    )


async def revoke_channel_exclusion(
    session: AsyncSession,
    exclusion_id: int,
    actor_user_id: int,
) -> bool:
    exclusion = await session.get(ChannelExclusion, exclusion_id)
    if exclusion is None:
        return False
    await lock_user(session, exclusion.channel_id, exclusion.user_id)
    await session.refresh(exclusion)
    if exclusion.revoked_at is not None:
        return False
    exclusion.revoked_at = utc_now()
    await session.execute(
        update(ChannelExclusionPeriod)
        .where(
            ChannelExclusionPeriod.channel_id == exclusion.channel_id,
            ChannelExclusionPeriod.user_id == exclusion.user_id,
            ChannelExclusionPeriod.ended_at.is_(None),
        )
        .values(ended_at=exclusion.revoked_at)
    )
    session.add(
        AuditLog(
            actor_user_id=actor_user_id,
            action="membership.exclusion.revoked",
            payload={"exclusion_id": exclusion_id},
        )
    )
    await session.flush()
    return True


async def evaluate_leaves(session: AsyncSession, channel_id: int, user_id: int) -> None:
    giveaways = (
        await session.scalars(
            select(Giveaway)
            .join(Participant, Participant.giveaway_id == Giveaway.id)
            .where(
                Giveaway.channel_id == channel_id,
                Giveaway.anti_abuse_enabled.is_(True),
                Giveaway.results_announced_at.is_not(None),
                Participant.user_id == user_id,
                Giveaway.results_announced_at >= utc_now() - timedelta(days=365),
            )
        )
    ).all()
    for giveaway in giveaways:
        if giveaway.results_announced_at is None:
            continue
        if await session.scalar(newcomer_events(giveaway, user_id).limit(1)) is None:
            continue
        leave = await session.scalar(
            select(MembershipEvent)
            .where(
                MembershipEvent.channel_id == channel_id,
                MembershipEvent.user_id == user_id,
                MembershipEvent.kind == "leave",
                MembershipEvent.occurred_at >= giveaway.results_announced_at,
                MembershipEvent.occurred_at < giveaway.results_announced_at + WINDOW,
            )
            .order_by(MembershipEvent.occurred_at.desc())
            .limit(1)
        )
        if leave is not None:
            await exclude_user(session, channel_id, user_id, leave.occurred_at, leave.id)


async def record_membership_event(
    session: AsyncSession,
    *,
    channel_id: int,
    user_id: int,
    update_id: int,
    kind: str,
    occurred_at: datetime,
) -> bool:
    if kind not in {"join", "leave", "kicked"}:
        return False
    await lock_user(session, channel_id, user_id)
    inserted = await session.scalar(
        insert(MembershipEvent)
        .values(
            channel_id=channel_id,
            user_id=user_id,
            update_id=update_id,
            kind=kind,
            occurred_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["update_id"])
        .returning(MembershipEvent.id)
    )
    if inserted is None:
        return False
    state = await session.scalar(
        select(ChannelMembership).where(
            ChannelMembership.channel_id == channel_id, ChannelMembership.user_id == user_id
        )
    )
    if state is None:
        session.add(
            ChannelMembership(
                channel_id=channel_id,
                user_id=user_id,
                status=kind,
                last_event_at=occurred_at,
                last_update_id=update_id,
            )
        )
    elif (occurred_at, update_id) > (state.last_event_at, state.last_update_id):
        state.status, state.last_event_at, state.last_update_id = kind, occurred_at, update_id
    await session.flush()
    await evaluate_leaves(session, channel_id, user_id)
    return True


async def reconcile_one(session_factory: async_sessionmaker[AsyncSession], bot: Bot) -> bool:
    now = utc_now()
    async with session_factory() as session:
        join_known = exists(
            select(MembershipEvent.id)
            .where(
                MembershipEvent.channel_id == Giveaway.channel_id,
                MembershipEvent.user_id == Participant.user_id,
                MembershipEvent.kind == "join",
                MembershipEvent.occurred_at > Giveaway.published_at,
                MembershipEvent.occurred_at <= Giveaway.finished_at,
            )
            .correlate(Giveaway, Participant)
        )
        row = (
            await session.execute(
                select(Participant, Giveaway, KnownChannel.telegram_chat_id)
                .join(Giveaway, Giveaway.id == Participant.giveaway_id)
                .join(KnownChannel, KnownChannel.id == Giveaway.channel_id)
                .where(
                    Giveaway.anti_abuse_enabled.is_(True),
                    Giveaway.status == "finished",
                    Giveaway.results_announced_at <= now,
                    Giveaway.results_announced_at > now - WINDOW,
                    join_known,
                    or_(
                        Participant.membership_next_check_at.is_(None),
                        Participant.membership_next_check_at <= now,
                    ),
                )
                .order_by(Participant.membership_next_check_at.asc().nullsfirst(), Participant.id)
                .limit(1)
                .with_for_update(of=Participant, skip_locked=True)
            )
        ).first()
        if row is None:
            return False
        participant, giveaway, chat_id = row
        await evaluate_leaves(session, giveaway.channel_id, participant.user_id)
        request_started_at = utc_now()
        request_second = request_started_at.replace(microsecond=0)
        try:
            member = await bot.get_chat_member(chat_id, participant.user_id)
        except TelegramRetryAfter as error:
            participant.membership_next_check_at = now + timedelta(seconds=error.retry_after)
            await session.commit()
            await asyncio.sleep(error.retry_after)
            return True
        except TelegramAPIError:
            participant.membership_next_check_at = now + CHECK_INTERVAL
        else:
            checked_at = utc_now()
            participant.membership_next_check_at = checked_at + CHECK_INTERVAL
            await lock_user(session, giveaway.channel_id, participant.user_id)
            state = await session.scalar(
                select(ChannelMembership)
                .where(
                    ChannelMembership.channel_id == giveaway.channel_id,
                    ChannelMembership.user_id == participant.user_id,
                )
                .execution_options(populate_existing=True)
            )
            # Telegram event times have second precision. Same-second events cannot
            # be ordered against the request, so neither positive nor negative
            # API observations may supersede them.
            if (state is not None and state.last_event_at >= request_second) or (
                participant.membership_checked_at is not None
                and participant.membership_checked_at >= request_second
            ):
                await session.commit()
                return True
            if member.status == "left" and checked_at < giveaway.results_announced_at + WINDOW:
                exclusion = await session.scalar(
                    select(ChannelExclusion).where(
                        ChannelExclusion.channel_id == giveaway.channel_id,
                        ChannelExclusion.user_id == participant.user_id,
                    )
                )
                fresh_join = True
                if exclusion is not None and exclusion.revoked_at is not None:
                    fresh_join = (
                        await session.scalar(
                            select(MembershipEvent)
                            .where(
                                MembershipEvent.channel_id == giveaway.channel_id,
                                MembershipEvent.user_id == participant.user_id,
                                MembershipEvent.kind == "join",
                                MembershipEvent.occurred_at > exclusion.revoked_at,
                                MembershipEvent.occurred_at < request_second,
                            )
                            .limit(1)
                        )
                        is not None
                    )
                member_observed = (
                    participant.membership_status in {"member", "administrator", "creator"}
                    and participant.membership_checked_at is not None
                    and participant.membership_checked_at >= giveaway.results_announced_at
                    and participant.membership_checked_at < request_second
                ) or (
                    state is not None
                    and state.status == "join"
                    and state.last_event_at >= giveaway.results_announced_at
                    and state.last_event_at < request_second
                )
                if fresh_join and member_observed and (state is None or state.status != "kicked"):
                    await exclude_user(
                        session, giveaway.channel_id, participant.user_id, checked_at
                    )
            if member.status in {"member", "administrator", "creator"}:
                participant.membership_status = member.status
                participant.membership_checked_at = request_started_at
        await session.commit()
    return True


async def cleanup_membership_events(session: AsyncSession, *, now: datetime | None = None) -> int:
    # Keep joins proving classification and exclusion evidence permanently.
    needed_join = exists(
        select(Participant.id)
        .join(Giveaway)
        .where(
            Giveaway.anti_abuse_enabled.is_(True),
            Giveaway.channel_id == MembershipEvent.channel_id,
            Participant.user_id == MembershipEvent.user_id,
            MembershipEvent.kind == "join",
            MembershipEvent.occurred_at > Giveaway.published_at,
            or_(
                Giveaway.finished_at.is_(None), MembershipEvent.occurred_at <= Giveaway.finished_at
            ),
        )
    )
    ids = (
        select(MembershipEvent.id)
        .where(
            MembershipEvent.occurred_at < (now or utc_now()) - timedelta(days=365),
            ~needed_join,
            ~exists(
                select(ChannelExclusion.id).where(
                    ChannelExclusion.evidence_event_id == MembershipEvent.id
                )
            ),
        )
        .limit(1000)
    )
    result = await session.execute(delete(MembershipEvent).where(MembershipEvent.id.in_(ids)))
    return int(getattr(result, "rowcount", 0))
