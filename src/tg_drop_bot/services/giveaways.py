from __future__ import annotations

import html
import logging
import random
from dataclasses import dataclass
from datetime import UTC

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tg_drop_bot.bot.keyboards import participation_keyboard
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import AuditLog, Giveaway, KnownChannel, Participant, Winner
from tg_drop_bot.services.conditions import ConditionCheck, check_participant_conditions
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.rendering import giveaway_title, mention_participant, render_giveaway_post

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParticipantRegistration:
    participant: Participant | None
    created: bool
    condition_check: ConditionCheck


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
        select(Giveaway)
        .options(selectinload(Giveaway.channel))
        .where(Giveaway.status == status)
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


async def publish_giveaway(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    actor_user_id: int,
) -> Message:
    if giveaway.channel is None:
        await session.refresh(giveaway, attribute_names=["channel"])
    if giveaway.channel is None:
        raise RuntimeError("Giveaway channel is not selected")
    missing = validate_draft(giveaway)
    if missing:
        raise RuntimeError("Draft is incomplete: " + ", ".join(missing))

    text = render_giveaway_post(giveaway, settings, participants_count=0)
    me = await bot.get_me()
    keyboard = participation_keyboard(giveaway.id, me.username)
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
    giveaway.status = "published"
    giveaway.message_id = message.message_id
    giveaway.published_at = utc_now()
    await add_audit(
        session,
        "giveaway.published",
        giveaway_id=giveaway.id,
        actor_user_id=actor_user_id,
        payload={"message_id": message.message_id},
    )
    await session.flush()
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
    me = await bot.get_me()
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
    if giveaway.channel is None or giveaway.message_id is None:
        return
    giveaway.image_file_id = image_file_id
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
        reply_markup=participation_keyboard(giveaway.id, (await bot.get_me()).username),
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
) -> None:
    participants_count = await count_participants(session, giveaway.id)
    giveaway.status = "cancelled"
    giveaway.cancelled_at = utc_now()
    await close_source_post(
        bot,
        settings,
        giveaway,
        participants_count=participants_count,
    )
    await add_audit(
        session, "giveaway.cancelled", giveaway_id=giveaway.id, actor_user_id=actor_user_id
    )
    await session.flush()


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
    session.add(participant)
    await session.flush()
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


async def finish_giveaway(
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    giveaway: Giveaway,
    *,
    actor_user_id: int | None,
    manual: bool,
) -> list[Winner]:
    if giveaway.status != "published" or giveaway.channel is None:
        return []

    participants = await list_participants(session, giveaway.id)
    valid_participants: list[Participant] = []
    now = utc_now()
    for participant in participants:
        condition_check = await check_participant_conditions(bot, giveaway, participant.user_id)
        participant.membership_status = condition_check.membership_status
        participant.membership_checked_at = now
        if condition_check.ok:
            valid_participants.append(participant)

    selected = pick_winners(valid_participants, giveaway.winners_count or 0)
    winners: list[Winner] = []
    for index, participant in enumerate(selected, start=1):
        winner = Winner(
            giveaway_id=giveaway.id,
            participant_id=participant.id,
            user_id=participant.user_id,
            position=index,
        )
        session.add(winner)
        winners.append(winner)

    giveaway.status = "finished"
    giveaway.finished_at = now.astimezone(UTC)
    await session.flush()

    await publish_winner_announcement(bot, giveaway, selected, len(valid_participants))
    await close_source_post(bot, settings, giveaway, participants_count=len(participants))
    await add_audit(
        session,
        "giveaway.finished.manual" if manual else "giveaway.finished.auto",
        giveaway_id=giveaway.id,
        actor_user_id=actor_user_id,
        payload={"valid_participants": len(valid_participants), "winners": len(winners)},
    )
    await session.flush()
    return winners


async def publish_winner_announcement(
    bot: Bot,
    giveaway: Giveaway,
    winners: list[Participant],
    valid_count: int,
) -> None:
    if giveaway.channel is None:
        return
    lines = [f"<b>Итоги розыгрыша: {html.escape(giveaway_title(giveaway))}</b>"]
    if winners:
        lines.append("")
        lines.extend(
            f"{index}. {mention_participant(participant)}"
            for index, participant in enumerate(winners, 1)
        )
    else:
        lines.extend(["", "Победителей нет: не нашлось валидных участников."])
    if giveaway.winners_count and valid_count < giveaway.winners_count:
        lines.extend(["", f"Валидных участников было меньше: {valid_count}."])
    message = await bot.send_message(
        chat_id=giveaway.channel.telegram_chat_id,
        text="\n".join(lines),
        disable_web_page_preview=True,
    )
    giveaway.result_message_id = message.message_id


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
) -> None:
    giveaways = await list_published_giveaways(session)
    for giveaway in giveaways:
        participants_count = await count_participants(session, giveaway.id)
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


async def build_csv_file(session: AsyncSession, giveaway_id: int) -> BufferedInputFile:
    from tg_drop_bot.services.csv_export import participants_to_csv

    participants = await list_participants(session, giveaway_id)
    return BufferedInputFile(
        participants_to_csv(participants), filename=f"giveaway-{giveaway_id}.csv"
    )
