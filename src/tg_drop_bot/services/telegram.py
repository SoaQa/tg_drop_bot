from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

VALID_MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.MEMBER,
}


async def is_group_member(bot: Bot, chat_id: int, user_id: int) -> tuple[bool, str]:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning(
            "Failed to check membership for user %s in chat %s: %s", user_id, chat_id, exc
        )
        return False, "check_failed"
    status = member.status
    return status in VALID_MEMBER_STATUSES, str(status.value)
