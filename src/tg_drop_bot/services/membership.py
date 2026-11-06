from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import (
    AuditLog,
    ChannelExclusion,
    ChannelExclusionPeriod,
    ChannelMembership,
    Giveaway,
    MembershipEvent,
    Participant,
)
from tg_drop_bot.services.dates import utc_now

PARTICIPATION_DENIED = (
    "Ваше участие в розыгрыше невозможно из-за подозрения в ботоводстве. "
    "Если вы не согласны с решением, напишите через бота обратной связи, "
    "указанного в описании канала."
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


async def lock_user(session: AsyncSession, channel_id: int, user_id: int) -> None:
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"membership:{channel_id}:{user_id}", 0)
            )
        )
    )


async def get_active_exclusion(
    session: AsyncSession, channel_id: int, user_id: int
) -> ChannelExclusion | None:
    return await session.scalar(
        select(ChannelExclusion).where(
            ChannelExclusion.channel_id == channel_id,
            ChannelExclusion.user_id == user_id,
            ChannelExclusion.revoked_at.is_(None),
        )
    )


async def prohibit_participation(
    session: AsyncSession, channel_id: int, user_id: int, actor_user_id: int
) -> bool:
    await lock_user(session, channel_id, user_id)
    exclusion = await session.scalar(
        select(ChannelExclusion).where(
            ChannelExclusion.channel_id == channel_id, ChannelExclusion.user_id == user_id
        )
    )
    if exclusion is not None and exclusion.revoked_at is None:
        return False
    now = utc_now()
    reason = "Подозрение в ботоводстве"
    if exclusion is None:
        exclusion = ChannelExclusion(channel_id=channel_id, user_id=user_id)
        session.add(exclusion)
    exclusion.reason = reason
    exclusion.created_at = now
    exclusion.revoked_at = None
    exclusion.evidence_event_id = None
    session.add_all(
        [
            ChannelExclusionPeriod(
                channel_id=channel_id, user_id=user_id, reason=reason, started_at=now
            ),
            AuditLog(
                actor_user_id=actor_user_id,
                action="membership.exclusion.created",
                payload={"channel_id": channel_id, "user_id": user_id},
            ),
        ]
    )
    await session.flush()
    return True


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
