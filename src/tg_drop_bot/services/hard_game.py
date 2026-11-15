from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import Giveaway, HardGameAttempt, KnownChannel, Participant
from tg_drop_bot.services.dates import utc_now


async def claim_hard_attempt(
    session: AsyncSession, giveaway_id: int, user_id: int, attempt_key: str,
) -> bool:
    """Durable claim, independent of Redis expiry, restarts and browser refreshes."""
    return bool(await session.scalar(insert(HardGameAttempt).values(
        giveaway_id=giveaway_id, user_id=user_id, attempt_key=attempt_key, started_at=utc_now(),
    ).on_conflict_do_nothing().returning(HardGameAttempt.user_id)))


async def record_hard_completion(
    session: AsyncSession, giveaway_id: int, user_id: int, attempt_key: str,
    duration_ms: int, completed_at: datetime,
) -> None:
    # Called in the same transaction as successful, condition-checked registration.
    await session.execute(update(HardGameAttempt).where(
        HardGameAttempt.giveaway_id == giveaway_id, HardGameAttempt.user_id == user_id,
        HardGameAttempt.attempt_key == attempt_key, HardGameAttempt.completed_at.is_(None),
    ).values(completed_at=completed_at, duration_ms=duration_ms))


async def leaderboard_text(session: AsyncSession, chat_id: int | None = None) -> str:
    query = select(Giveaway).where(Giveaway.status.in_(['published', 'finished']))
    if chat_id is not None:
        query = query.join(KnownChannel).where(KnownChannel.telegram_chat_id == chat_id)
    giveaway = await session.scalar(query.order_by(
        (Giveaway.status == 'published').desc(), Giveaway.id.desc(),
    ).limit(1))
    if giveaway is None:
        return "Пока нет розыгрышей для таблицы /top."
    records = (await session.execute(select(HardGameAttempt, Participant).join(
        Participant, (Participant.giveaway_id == HardGameAttempt.giveaway_id)
        & (Participant.user_id == HardGameAttempt.user_id),
    ).where(
        HardGameAttempt.giveaway_id == giveaway.id, HardGameAttempt.completed_at.is_not(None),
    ).order_by(
        HardGameAttempt.completed_at, HardGameAttempt.user_id,
    ).limit(20))).all()
    title = escape((giveaway.title or f"Розыгрыш №{giveaway.id}")[:80])
    lines = [f"<b>Сложный режим: {title}</b>"]
    lines.append("Порядок финиша, время московское:")
    if not records:
        lines.append("Пока никто не прошёл сложную попытку.")
    for place, (attempt, participant) in enumerate(records, 1):
        assert attempt.completed_at is not None
        name = ('@'+participant.username if participant.username
                else participant.first_name or 'Игрок')
        finished = attempt.completed_at.astimezone(ZoneInfo('Europe/Moscow')).strftime(
            '%d.%m %H:%M:%S.%f',
        )[:-3]
        lines.append(
            f"{place}. {escape(name[:32])}: {finished}, {(attempt.duration_ms or 0)/1000:.2f} сек.",
        )
    total = func.count(HardGameAttempt.giveaway_id).label('total')
    global_query = select(
        HardGameAttempt.user_id, total, func.min(HardGameAttempt.completed_at).label('first'),
    ).where(HardGameAttempt.completed_at.is_not(None))
    if chat_id is not None:
        global_query = global_query.join(Giveaway).join(KnownChannel).where(
            KnownChannel.telegram_chat_id == chat_id,
        )
    totals = (await session.execute(global_query.group_by(HardGameAttempt.user_id).order_by(
        total.desc(), func.min(HardGameAttempt.completed_at), HardGameAttempt.user_id,
    ).limit(20))).all()
    lines.append('\n<b>Всего сложных прохождений за все розыгрыши:</b>')
    if not totals:
        lines.append('Пока нет результатов.')
    profiles = {p.user_id: p for p in (await session.scalars(select(Participant).where(
        Participant.user_id.in_([row[0] for row in totals]),
    ).distinct(Participant.user_id).order_by(
        Participant.user_id, Participant.joined_at.desc(),
    ))).all()}
    for place, (uid, count, _) in enumerate(totals, 1):
        profile = profiles.get(uid)
        name = ('@'+profile.username if profile and profile.username
                else profile.first_name if profile and profile.first_name else 'Игрок')
        lines.append(f'{place}. {escape(name[:32])}: {count}')
    lines.append("Одна сложная попытка на розыгрыш. Место в топе не меняет шанс на приз.")
    return '\n'.join(lines)
