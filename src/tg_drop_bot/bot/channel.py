from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated, Update
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import KnownChannel
from tg_drop_bot.services.membership import record_membership_event

router = Router(name="channel")


@router.chat_member()
async def channel_membership_changed(
    event: ChatMemberUpdated,
    event_update: Update,
    session: AsyncSession,
) -> None:
    if event.chat.type != "channel":
        return
    channel = await session.scalar(
        select(KnownChannel).where(KnownChannel.telegram_chat_id == event.chat.id)
    )
    if channel is None:
        return
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    absent = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    if old in absent and new not in absent:
        kind = "join"
    elif old not in absent and new in absent:
        kind = (
            "leave"
            if new == ChatMemberStatus.LEFT and event.from_user.id == event.new_chat_member.user.id
            else "kicked"
        )
    else:
        return
    await record_membership_event(
        session,
        channel_id=channel.id,
        user_id=event.new_chat_member.user.id,
        update_id=event_update.update_id,
        kind=kind,
        occurred_at=event.date,
    )
    await session.commit()


@router.my_chat_member()
async def bot_membership_changed(event: ChatMemberUpdated, session: AsyncSession) -> None:
    if event.chat.type != "channel":
        return
    status = event.new_chat_member.status
    still_in_channel = status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    bot_is_admin = status == ChatMemberStatus.ADMINISTRATOR

    result = await session.execute(
        select(KnownChannel).where(KnownChannel.telegram_chat_id == event.chat.id)
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        # Не активируем канал автоматически: кто угодно может добавить бота
        # в свой канал. Канал становится доступным только после того, как
        # админ явно выберет его через «Выбрать канал».
        channel = KnownChannel(
            telegram_chat_id=event.chat.id,
            title=event.chat.title or str(event.chat.id),
            username=event.chat.username,
            is_active=False,
            bot_is_admin=bot_is_admin,
        )
        session.add(channel)
    else:
        channel.title = event.chat.title or str(event.chat.id)
        channel.username = event.chat.username
        channel.bot_is_admin = bot_is_admin
        if not still_in_channel:
            channel.is_active = False
    await session.commit()
