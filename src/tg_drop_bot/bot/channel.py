from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import KnownChannel

router = Router(name="channel")


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
