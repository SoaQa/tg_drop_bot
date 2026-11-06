from __future__ import annotations

import html
import logging
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message, ReplyParameters, User
from sqlalchemy import CursorResult, func, inspect, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tg_drop_bot.bot.keyboards import participation_keyboard
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog, Giveaway, KnownChannel, Participant, Winner
from tg_drop_bot.services.conditions import ConditionCheck, check_participant_conditions
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.membership import (
    PARTICIPATION_DENIED,
    get_active_exclusion,
    lock_user,
    snapshot_draw_eligibility,
)
from tg_drop_bot.services.rendering import (
    channel_post_link,
    giveaway_title,
    render_giveaway_post,
    render_results_announcement,
)

logger = logging.getLogger(__name__)

# Telegram limits: 1024 characters for photo captions, 4096 for plain messages.
PHOTO_CAPTION_LIMIT = 1024
MESSAGE_TEXT_LIMIT = 4096
GIVEAWAY_TITLE_LIMIT = 255

RESULTS_STATE_PENDING = "pending"
RESULTS_STATE_DRAWN = "drawn"
RESULTS_STATE_ANNOUNCED = "announced"

# A finished giveaway stuck in results_state="pending" longer than this is
# considered orphaned by a crash and is safe to resume: a live finish keeps
# "pending" only while it re-checks participants.
RESULTS_RESUME_GRACE = timedelta(minutes=10)


class PublishGiveawayError(RuntimeError):
    """Publish failed for a reason the admin should see verbatim."""


@dataclass(frozen=True)
class ParticipantRegistration:
    participant: Participant | None
    created: bool
    condition_check: ConditionCheck


@dataclass(frozen=True)
class FinishResult:
    finished: bool
    winners: list[Participant] = field(default_factory=list)


async def add_audit(
    session: AsyncSession,
    action: str,
    *,
    giveaway_id: int | None = None,
    actor_user_id: int | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    session.add(
        AuditLog(
            giveaway_id=giveaway_id,
            actor_user_id=actor_user_id,
            action=action,
            payload=payload or {},
        )
    )


async def ensure_channel_loaded(session: AsyncSession, giveaway: Giveaway) -> None:
    """Load the channel relationship without triggering an async lazy-load."""
    if "channel" in inspect(giveaway).unloaded:
        await session.refresh(giveaway, attribute_names=["channel"])


async def try_status_transition(
    session: AsyncSession,
    giveaway: Giveaway,
    *,
    expected_status: str,
    **values: object,
) -> bool:
    """Atomically move the giveaway out of ``expected_status``.

    The UPDATE races safely against concurrent workers (scheduler tick vs
    manual admin action): exactly one of them observes rowcount == 1. Does not
    commit; the caller decides the transaction boundary.
    """
    result = await session.execute(
        update(Giveaway)
        .where(Giveaway.id == giveaway.id, Giveaway.status == expected_status)
        .values(**values)
        .execution_options(synchronize_session="fetch")
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def try_results_state_transition(
    session: AsyncSession,
    giveaway: Giveaway,
    *,
    expected_state: str,
    new_state: str,
) -> bool:
    """Atomically advance results_state; False if another worker got there first."""
    result = await session.execute(
        update(Giveaway)
        .where(Giveaway.id == giveaway.id, Giveaway.results_state == expected_state)
        .values(results_state=new_state)
        .execution_options(synchronize_session="fetch")
    )
    return cast(CursorResult[Any], result).rowcount == 1


async def upsert_known_channel(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    title: str,
    username: str | None,
    is_active: bool,
    bot_is_admin: bool,
) -> KnownChannel:
    result = await session.execute(
        select(KnownChannel).where(KnownChannel.telegram_chat_id == telegram_chat_id)
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        channel = KnownChannel(
            telegram_chat_id=telegram_chat_id,
            title=title,
            username=username,
            is_active=is_active,
            bot_is_admin=bot_is_admin,
        )
        session.add(channel)
    else:
        channel.title = title
        channel.username = username
        channel.is_active = is_active
        channel.bot_is_admin = bot_is_admin
    await session.flush()
    return channel


async def list_available_channels(session: AsyncSession) -> list[KnownChannel]:
    result = await session.execute(
        select(KnownChannel)
        .where(KnownChannel.is_active.is_(True), KnownChannel.bot_is_admin.is_(True))
        .order_by(KnownChannel.title)
    )
    return list(result.scalars().all())


async def create_draft(session: AsyncSession, creator_user_id: int) -> Giveaway:
    giveaway = Giveaway(status="draft", creator_user_id=creator_user_id)
    session.add(giveaway)
    await session.flush()
    await add_audit(
        session, "giveaway.created", giveaway_id=giveaway.id, actor_user_id=creator_user_id
    )
    return giveaway


async def get_giveaway(session: AsyncSession, giveaway_id: int) -> Giveaway | None:
    result = await session.execute(
        select(Giveaway).options(selectinload(Giveaway.channel)).where(Giveaway.id == giveaway_id)
    )
    return result.scalar_one_or_none()


async def count_participants(session: AsyncSession, giveaway_id: int) -> int:
    result = await session.execute(
        select(func.count(Participant.id)).where(Participant.giveaway_id == giveaway_id)
    )
    return int(result.scalar_one())


async def list_giveaways_by_status(
    session: AsyncSession,
    status: str,
    *,
    admin_user_id: int,
    current_admin_ids: set[int],
) -> list[Giveaway]:
    query = (
        select(Giveaway).options(selectinload(Giveaway.channel)).where(Giveaway.status == status)
    )
    if admin_user_id in current_admin_ids:
        query = query.where(
            (Giveaway.creator_user_id == admin_user_id)
            | (Giveaway.creator_user_id.not_in(current_admin_ids))
        )
    result = await session.execute(query.order_by(Giveaway.created_at.desc()))
    return list(result.scalars().all())


async def list_published_giveaways(session: AsyncSession) -> list[Giveaway]:
    result = await session.execute(
        select(Giveaway)
        .options(selectinload(Giveaway.channel))
        .where(Giveaway.status == "published")
        .order_by(Giveaway.deadline_at.asc(), Giveaway.created_at.desc())
    )
    return list(result.scalars().all())


def single_giveaway_or_none(giveaways: list[Giveaway]) -> Giveaway | None:
    if len(giveaways) == 1:
        return giveaways[0]
    return None


def validate_draft(giveaway: Giveaway) -> list[str]:
    missing: list[str] = []
    if giveaway.channel_id is None:
        missing.append("канал")
    if not giveaway.title:
        missing.append("название")
    if not giveaway.post_text:
        missing.append("описание")
    if not giveaway.terms_text:
        missing.append("условия")
    if not giveaway.winners_count:
        missing.append("количество победителей")
    if giveaway.deadline_at is None:
        missing.append("дедлайн")
    return missing


def giveaway_post_length_error(
    giveaway: Giveaway,
    settings: Settings,
    *,
    with_photo: bool | None = None,
    participants_count: int = 0,
) -> str | None:
    has_photo = bool(giveaway.image_file_id) if with_photo is None else with_photo
    # PostgreSQL COUNT returns bigint; leave room for all of its decimal digits.
    count = 2**63 - 1 if has_photo else participants_count
    text = render_giveaway_post(giveaway, settings, participants_count=count)
    length = len(text)
    if has_photo:
        if giveaway.deadline_at is None:
            length += len("00.00.0000 00:00") - len("не задано")
        if length > PHOTO_CAPTION_LIMIT:
            return (
                f"Подпись к картинке слишком длинная: лимит {PHOTO_CAPTION_LIMIT} символов "
                f"вместе с условиями и запасом для счётчика участников (сейчас {length}). "
                "Сократите название или описание."
            )
    elif length > MESSAGE_TEXT_LIMIT:
        return (
            f"Пост не может быть длиннее {MESSAGE_TEXT_LIMIT} символов (сейчас {length}). "
            "Сократите описание."
        )
    return None


async def publish_giveaway(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    actor_user_id: int,
) -> Message:
    """Publish a draft to its channel. Commits its own work.

    The draft is atomically claimed before the channel post is sent, so a
    double click or a concurrent worker cannot publish the same giveaway twice.
    """
    await ensure_channel_loaded(session, giveaway)
    if giveaway.channel is None:
        raise RuntimeError("Giveaway channel is not selected")
    missing = validate_draft(giveaway)
    if missing:
        raise RuntimeError("Draft is incomplete: " + ", ".join(missing))

    if length_error := giveaway_post_length_error(giveaway, settings):
        raise PublishGiveawayError(length_error)
    text = render_giveaway_post(giveaway, settings, participants_count=0)

    claimed = await try_status_transition(
        session,
        giveaway,
        expected_status="draft",
        status="published",
        published_at=utc_now(),
    )
    if not claimed:
        await session.rollback()
        raise PublishGiveawayError("Розыгрыш уже опубликован или недоступен.")
    await session.commit()

    me = await bot.me()
    keyboard = participation_keyboard(giveaway.id, me.username)
    try:
        if giveaway.image_file_id:
            message = await bot.send_photo(
                chat_id=giveaway.channel.telegram_chat_id,
                photo=giveaway.image_file_id,
                caption=text,
                reply_markup=keyboard,
            )
        else:
            message = await bot.send_message(
                chat_id=giveaway.channel.telegram_chat_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except TelegramAPIError as error:
        # Sending failed: return the giveaway to drafts so it can be retried.
        await try_status_transition(
            session,
            giveaway,
            expected_status="published",
            status="draft",
            published_at=None,
        )
        await session.commit()
        logger.exception("Failed to publish giveaway", extra={"giveaway_id": giveaway.id})
        raise PublishGiveawayError(
            "Не удалось опубликовать пост в канале. Проверьте, что бот остается "
            "администратором канала, и попробуйте еще раз."
        ) from error

    giveaway.message_id = message.message_id
    giveaway.anti_abuse_enabled = True
    await add_audit(
        session,
        "giveaway.published",
        giveaway_id=giveaway.id,
        actor_user_id=actor_user_id,
        payload={"message_id": message.message_id},
    )
    await session.commit()
    return message


async def edit_published_message(
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    *,
    participants_count: int | None = None,
) -> None:
    if giveaway.status != "published" or giveaway.channel is None or giveaway.message_id is None:
        return
    text = render_giveaway_post(
        giveaway,
        settings,
        participants_count=participants_count,
    )
    me = await bot.me()
    keyboard = participation_keyboard(giveaway.id, me.username)
    try:
        if giveaway.image_file_id:
            await bot.edit_message_caption(
                chat_id=giveaway.channel.telegram_chat_id,
                message_id=giveaway.message_id,
                caption=text,
                reply_markup=keyboard,
            )
        else:
            await bot.edit_message_text(
                chat_id=giveaway.channel.telegram_chat_id,
                message_id=giveaway.message_id,
                text=text,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return
        raise


async def replace_published_image(
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    image_file_id: str,
    *,
    participants_count: int | None = None,
) -> None:
    giveaway.image_file_id = image_file_id
    if giveaway.channel is None or giveaway.message_id is None:
        return
    await bot.edit_message_media(
        chat_id=giveaway.channel.telegram_chat_id,
        message_id=giveaway.message_id,
        media=InputMediaPhoto(
            media=image_file_id,
            caption=render_giveaway_post(
                giveaway,
                settings,
                participants_count=participants_count,
            ),
        ),
        reply_markup=participation_keyboard(giveaway.id, (await bot.me()).username),
    )


async def close_source_post(
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    *,
    participants_count: int | None = None,
) -> None:
    if giveaway.channel is None or giveaway.message_id is None:
        return
    text = render_giveaway_post(
        giveaway,
        settings,
        closed=True,
        participants_count=participants_count,
    )
    try:
        if giveaway.image_file_id:
            await bot.edit_message_caption(
                chat_id=giveaway.channel.telegram_chat_id,
                message_id=giveaway.message_id,
                caption=text,
                reply_markup=None,
            )
        else:
            await bot.edit_message_text(
                chat_id=giveaway.channel.telegram_chat_id,
                message_id=giveaway.message_id,
                text=text,
                reply_markup=None,
                disable_web_page_preview=True,
            )
    except TelegramAPIError:
        # Source post edits are best-effort; the DB status is still authoritative.
        return


async def cancel_giveaway(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    actor_user_id: int,
) -> bool:
    """Cancel a published giveaway. Commits its own work.

    Returns False when the giveaway was already finished or cancelled by a
    concurrent worker (e.g. the scheduler hit the deadline first).
    """
    await add_audit(
        session, "giveaway.cancelled", giveaway_id=giveaway.id, actor_user_id=actor_user_id
    )
    claimed = await try_status_transition(
        session,
        giveaway,
        expected_status="published",
        status="cancelled",
        cancelled_at=utc_now(),
    )
    if not claimed:
        await session.rollback()
        return False
    await session.commit()

    participants_count = await count_participants(session, giveaway.id)
    await close_source_post(
        bot,
        settings,
        giveaway,
        participants_count=participants_count,
    )
    return True


async def register_participant(
    session: AsyncSession,
    giveaway: Giveaway,
    user: User,
) -> tuple[Participant, bool]:
    result = await session.execute(
        select(Participant).where(
            Participant.giveaway_id == giveaway.id,
            Participant.user_id == user.id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing, False

    now = utc_now()
    participant = Participant(
        giveaway_id=giveaway.id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        joined_at=now,
        captcha_passed_at=now,
        membership_status="member",
        membership_checked_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(participant)
            await session.flush()
    except IntegrityError:
        # Гонка двух параллельных апдейтов: участник уже зарегистрирован.
        result = await session.execute(
            select(Participant).where(
                Participant.giveaway_id == giveaway.id,
                Participant.user_id == user.id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            raise
        return existing, False
    await add_audit(
        session,
        "participant.joined",
        giveaway_id=giveaway.id,
        actor_user_id=user.id,
        payload={"participant_id": participant.id},
    )
    return participant, True


async def register_participant_after_condition_check(
    session: AsyncSession,
    bot: Bot,
    giveaway: Giveaway,
    user: User,
) -> ParticipantRegistration:
    condition_check = await check_participant_conditions(bot, giveaway, user.id)
    if not condition_check.ok:
        return ParticipantRegistration(
            participant=None,
            created=False,
            condition_check=condition_check,
        )

    if giveaway.channel_id is not None:
        await lock_user(session, giveaway.channel_id, user.id)
        if await get_active_exclusion(session, giveaway.channel_id, user.id) is not None:
            return ParticipantRegistration(
                participant=None, created=False,
                condition_check=ConditionCheck(
                    ok=False, membership_status="excluded", user_message=PARTICIPATION_DENIED
                ),
            )
    participant, created = await register_participant(session, giveaway, user)
    participant.membership_status = condition_check.membership_status
    participant.membership_checked_at = utc_now()
    return ParticipantRegistration(
        participant=participant,
        created=created,
        condition_check=condition_check,
    )


async def list_participants(session: AsyncSession, giveaway_id: int) -> list[Participant]:
    result = await session.execute(
        select(Participant)
        .where(Participant.giveaway_id == giveaway_id)
        .order_by(Participant.joined_at.asc())
    )
    return list(result.scalars().all())


def pick_winners(participants: list[Participant], winners_count: int) -> list[Participant]:
    if winners_count <= 0 or not participants:
        return []
    count = min(winners_count, len(participants))
    return random.SystemRandom().sample(participants, count)


def pick_weighted_winners(
    participants: list[Participant],
    winners_count: int,
) -> list[Participant]:
    import numpy as np

    if winners_count <= 0 or not participants:
        return []
    weights = np.array([p.selection_weight for p in participants], dtype=float)
    indices = np.random.default_rng().choice(
        len(participants),
        size=min(winners_count, len(participants)),
        replace=False,
        p=weights / weights.sum(),
    )
    return [participants[int(i)] for i in indices]


async def finish_giveaway(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    *,
    actor_user_id: int | None,
    manual: bool,
) -> FinishResult:
    """Finish a published giveaway. Commits its own work in stages.

    Order matters: the giveaway is atomically claimed (status -> finished)
    and committed BEFORE any Telegram side effect, so a failed announcement
    can never roll the status back to published and cause repeated draws or
    duplicate result posts. Interrupted stages are resumed by the scheduler
    via resume_unfinished_results().
    """
    await ensure_channel_loaded(session, giveaway)
    if giveaway.status != "published" or giveaway.channel is None:
        return FinishResult(finished=False)

    await add_audit(
        session,
        "giveaway.finished.manual" if manual else "giveaway.finished.auto",
        giveaway_id=giveaway.id,
        actor_user_id=actor_user_id,
    )
    claimed = await try_status_transition(
        session,
        giveaway,
        expected_status="published",
        status="finished",
        finished_at=utc_now(),
        results_state=RESULTS_STATE_PENDING,
    )
    if not claimed:
        await session.rollback()
        return FinishResult(finished=False)
    await session.commit()

    await announce_results_preparing(session, bot, giveaway)
    winners = await draw_and_record_winners(session, bot, giveaway)
    await publish_giveaway_results(session, bot, settings, giveaway, winners)
    return FinishResult(finished=True, winners=winners)


async def announce_results_preparing(
    session: AsyncSession,
    bot: Bot,
    giveaway: Giveaway,
) -> None:
    """Immediately tell the channel that results are being prepared.

    Participant re-validation can take a while, so this message appears right
    at the deadline and is later edited into the final results. Best-effort:
    a failure here does not block the draw.
    """
    if giveaway.channel is None or giveaway.result_message_id is not None:
        return
    text = (
        f"<b>Розыгрыш «{html.escape(giveaway_title(giveaway))}» завершен.</b>\n\n"
        "Подводим итоги и проверяем участников. "
        "Результаты появятся в этом сообщении."
    )
    reply_parameters = None
    if giveaway.message_id is not None:
        reply_parameters = ReplyParameters(
            message_id=giveaway.message_id, allow_sending_without_reply=True
        )
    try:
        message = await bot.send_message(
            chat_id=giveaway.channel.telegram_chat_id,
            text=text,
            reply_parameters=reply_parameters,
            disable_web_page_preview=True,
        )
    except TelegramAPIError:
        logger.exception(
            "Failed to announce results preparation",
            extra={"giveaway_id": giveaway.id},
        )
        return
    giveaway.result_message_id = message.message_id
    await session.commit()


async def draw_and_record_winners(
    session: AsyncSession,
    bot: Bot,
    giveaway: Giveaway,
) -> list[Participant]:
    """Revalidate participants, pick winners and persist them atomically.

    The pending -> drawn transition is claimed atomically, so a slow live
    finish and the scheduler's resume sweep can never both record a winner
    set: the loser discards its draw and returns the persisted winners.
    """
    if giveaway.anti_abuse_enabled and giveaway.results_state in {
        RESULTS_STATE_DRAWN,
        RESULTS_STATE_ANNOUNCED,
    }:
        return await list_winner_participants(session, giveaway.id)
    participants = await list_participants(session, giveaway.id)
    eligible_participants = await snapshot_draw_eligibility(session, giveaway, participants)
    valid_participants: list[Participant] = []
    check_failures = 0
    now = utc_now()
    for participant in eligible_participants:
        condition_check = await check_participant_conditions(bot, giveaway, participant.user_id)
        if condition_check.membership_status == "check_failed":
            # Retry once: single flaky API call must not disqualify a participant.
            condition_check = await check_participant_conditions(bot, giveaway, participant.user_id)
        participant.membership_status = condition_check.membership_status
        participant.membership_checked_at = now
        if condition_check.ok:
            valid_participants.append(participant)
        elif condition_check.membership_status == "check_failed":
            # Telegram недоступен: не выкидываем участника из розыгрыша,
            # подписку он уже подтверждал при регистрации.
            check_failures += 1
            valid_participants.append(participant)
        else:
            participant.draw_excluded = True
            participant.exclusion_reason = "Не выполнено условие подписки на канал"

    selected = (
        pick_weighted_winners(valid_participants, giveaway.winners_count or 0)
        if giveaway.anti_abuse_enabled
        else pick_winners(valid_participants, giveaway.winners_count or 0)
    )

    claimed = await try_results_state_transition(
        session,
        giveaway,
        expected_state=RESULTS_STATE_PENDING,
        new_state=RESULTS_STATE_DRAWN,
    )
    if not claimed:
        # Другой работник уже разыграл — используем его победителей.
        await session.rollback()
        return await list_winner_participants(session, giveaway.id)

    for index, participant in enumerate(selected, start=1):
        session.add(
            Winner(
                giveaway_id=giveaway.id,
                participant_id=participant.id,
                user_id=participant.user_id,
                position=index,
            )
        )
    await add_audit(
        session,
        "giveaway.results.drawn",
        giveaway_id=giveaway.id,
        payload={
            "participants": len(participants),
            "valid_participants": len(valid_participants),
            "membership_check_failures": check_failures,
            "shadow_excluded": len(participants) - len(eligible_participants),
            "newcomers": sum(p.selection_reason == "newcomer" for p in participants),
            "winners": len(selected),
        },
    )
    await session.commit()
    return selected


async def publish_giveaway_results(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    winners: list[Participant],
    *,
    notify_creator_on_failure: bool = True,
) -> None:
    """Publish final results and notify everyone. Commits its own work.

    Edits the "подводим итоги" message into the final results (or sends a new
    message when there is none), closes the source post and notifies the
    creator and winners in private chat. Each step is best-effort;
    announcement success is recorded in results_state so the scheduler
    retries only what failed. ``notify_creator_on_failure=False`` keeps
    scheduled retries from spamming the creator with repeated failure DMs.
    """
    announced = False
    text = render_results_announcement(giveaway, winners)
    if giveaway.channel is not None:
        try:
            if giveaway.result_message_id is not None:
                try:
                    await bot.edit_message_text(
                        chat_id=giveaway.channel.telegram_chat_id,
                        message_id=giveaway.result_message_id,
                        text=text,
                        disable_web_page_preview=True,
                    )
                except TelegramBadRequest as error:
                    # "Not modified" means the results are already there.
                    if "message is not modified" not in str(error).lower():
                        raise
            else:
                reply_parameters = None
                if giveaway.message_id is not None:
                    reply_parameters = ReplyParameters(
                        message_id=giveaway.message_id, allow_sending_without_reply=True
                    )
                message = await bot.send_message(
                    chat_id=giveaway.channel.telegram_chat_id,
                    text=text,
                    reply_parameters=reply_parameters,
                    disable_web_page_preview=True,
                )
                giveaway.result_message_id = message.message_id
            announced = True
        except TelegramAPIError:
            logger.exception(
                "Failed to publish giveaway results",
                extra={"giveaway_id": giveaway.id},
            )
    if announced:
        giveaway.results_state = RESULTS_STATE_ANNOUNCED
        if giveaway.anti_abuse_enabled:
            # Coalesce in SQL keeps the first success stable under concurrent retries.
            await session.execute(
                update(Giveaway)
                .where(Giveaway.id == giveaway.id)
                .values(
                    results_announced_at=func.coalesce(Giveaway.results_announced_at, utc_now())
                )
            )
        elif giveaway.results_announced_at is None:
            giveaway.results_announced_at = utc_now()
        await add_audit(
            session,
            "giveaway.results.announced",
            giveaway_id=giveaway.id,
            payload={"winners": len(winners)},
        )
    await session.commit()

    participants_count = await count_participants(session, giveaway.id)
    await close_source_post(bot, settings, giveaway, participants_count=participants_count)
    if announced or notify_creator_on_failure:
        await notify_creator_about_results(bot, giveaway, winners, announced=announced)
    if announced:
        await notify_winners_about_results(bot, giveaway, winners)


async def notify_creator_about_results(
    bot: Bot,
    giveaway: Giveaway,
    winners: list[Participant],
    *,
    announced: bool,
) -> None:
    """Tell the giveaway creator in private chat that the results are ready."""
    lines = [f"Розыгрыш «{html.escape(giveaway_title(giveaway))}» завершен."]
    if winners:
        lines.append(f"Победителей: {len(winners)}.")
    else:
        lines.append("Победителей нет: не нашлось валидных участников.")
    link = channel_post_link(giveaway.channel, giveaway.result_message_id)
    if announced and link:
        lines.append(f"Итоги в канале: {link}")
    if not announced:
        lines.append(
            "Опубликовать итоги в канале не удалось - бот будет пробовать еще раз автоматически."
        )
    try:
        await bot.send_message(
            chat_id=giveaway.creator_user_id,
            text="\n".join(lines),
            disable_web_page_preview=True,
        )
    except TelegramAPIError as error:
        logger.warning(
            "Failed to notify creator %s about giveaway %s results: %s",
            giveaway.creator_user_id,
            giveaway.id,
            error,
        )


async def notify_winners_about_results(
    bot: Bot,
    giveaway: Giveaway,
    winners: list[Participant],
) -> None:
    """Congratulate each winner in private chat (they all started the bot for captcha)."""
    link = channel_post_link(giveaway.channel, giveaway.result_message_id)
    lines = [f"Поздравляем! Вы выиграли в розыгрыше «{html.escape(giveaway_title(giveaway))}»."]
    if link:
        lines.append(f"Итоги: {link}")
    lines.append("Организатор свяжется с вами или опубликует инструкции в канале.")
    text = "\n".join(lines)
    for participant in winners:
        try:
            await bot.send_message(
                chat_id=participant.user_id,
                text=text,
                disable_web_page_preview=True,
            )
        except TelegramAPIError as error:
            logger.warning(
                "Failed to notify winner %s of giveaway %s: %s",
                participant.user_id,
                giveaway.id,
                error,
            )


async def list_winner_participants(session: AsyncSession, giveaway_id: int) -> list[Participant]:
    result = await session.execute(
        select(Participant)
        .join(Winner, Winner.participant_id == Participant.id)
        .where(Winner.giveaway_id == giveaway_id)
        .order_by(Winner.position.asc())
    )
    return list(result.scalars().all())


async def resume_unfinished_results(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
) -> list[int]:
    """Complete result publication interrupted by a crash or Telegram failure.

    "drawn" giveaways only need the announcement retried. "pending" ones are
    resumed after a grace period (so a live, slow finish is not raced) by
    drawing winners from scratch - the draw and results_state advance in one
    transaction, so no partial winner set can exist.
    """
    stale_before = utc_now() - RESULTS_RESUME_GRACE
    result = await session.execute(
        select(Giveaway)
        .options(selectinload(Giveaway.channel))
        .where(
            Giveaway.status == "finished",
            Giveaway.results_state != RESULTS_STATE_ANNOUNCED,
            or_(
                Giveaway.results_state == RESULTS_STATE_DRAWN,
                Giveaway.finished_at.is_(None),
                Giveaway.finished_at <= stale_before,
            ),
        )
        .order_by(Giveaway.finished_at.asc())
    )
    resumed: list[int] = []
    for giveaway in result.scalars().all():
        try:
            if giveaway.results_state == RESULTS_STATE_PENDING:
                selected = await draw_and_record_winners(session, bot, giveaway)
            else:
                selected = await list_winner_participants(session, giveaway.id)
            await publish_giveaway_results(
                session,
                bot,
                settings,
                giveaway,
                selected,
                notify_creator_on_failure=False,
            )
            resumed.append(giveaway.id)
        except Exception:
            logger.exception(
                "Failed to resume giveaway results",
                extra={"giveaway_id": giveaway.id},
            )
    return resumed


def is_giveaway_open(giveaway: Giveaway) -> bool:
    """Whether the giveaway still accepts participants (published, deadline not passed)."""
    if giveaway.status != "published" or giveaway.channel is None:
        return False
    return giveaway.deadline_at is None or giveaway.deadline_at > utc_now()


async def due_published_giveaways(session: AsyncSession) -> list[Giveaway]:
    result = await session.execute(
        select(Giveaway)
        .options(selectinload(Giveaway.channel))
        .where(Giveaway.status == "published", Giveaway.deadline_at <= utc_now())
        .order_by(Giveaway.deadline_at.asc())
    )
    return list(result.scalars().all())


async def refresh_published_giveaway_messages(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    last_rendered_counts: dict[int, int] | None = None,
) -> None:
    """Refresh participant counters on published channel posts.

    ``last_rendered_counts`` lets the scheduler skip posts whose counter has
    not changed, so the bot does not burn the flood limit on no-op edits.
    """
    giveaways = await list_published_giveaways(session)
    seen_ids: set[int] = set()
    for giveaway in giveaways:
        seen_ids.add(giveaway.id)
        participants_count = await count_participants(session, giveaway.id)
        if (
            last_rendered_counts is not None
            and last_rendered_counts.get(giveaway.id) == participants_count
        ):
            continue
        try:
            await edit_published_message(
                bot,
                settings,
                giveaway,
                participants_count=participants_count,
            )
        except TelegramAPIError:
            logger.exception(
                "Failed to refresh giveaway message",
                extra={"giveaway_id": giveaway.id},
            )
            continue
        if last_rendered_counts is not None:
            last_rendered_counts[giveaway.id] = participants_count
    if last_rendered_counts is not None:
        for stale_id in set(last_rendered_counts) - seen_ids:
            last_rendered_counts.pop(stale_id)


async def build_csv_file(session: AsyncSession, giveaway_id: int) -> BufferedInputFile:
    from tg_drop_bot.services.csv_export import participants_to_csv

    participants = await list_participants(session, giveaway_id)
    return BufferedInputFile(
        participants_to_csv(participants), filename=f"giveaway-{giveaway_id}.csv"
    )
