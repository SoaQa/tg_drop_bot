from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatMemberUpdated
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.services.giveaways import upsert_known_group

router = Router(name="group")


@router.my_chat_member()
async def bot_membership_changed(event: ChatMemberUpdated, session: AsyncSession) -> None:
    if event.chat.type != "channel":
        return
    status = event.new_chat_member.status
    is_active = status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}
    bot_is_admin = status == ChatMemberStatus.ADMINISTRATOR
    await upsert_known_group(
        session,
        telegram_chat_id=event.chat.id,
        title=event.chat.title or str(event.chat.id),
        username=event.chat.username,
        is_active=is_active,
        bot_is_admin=bot_is_admin,
    )
    await session.commit()
