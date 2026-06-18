from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

VALID_MEMBER_STATUSES = {
    ChatMemberStatus.CREATOR.value,
    ChatMemberStatus.ADMINISTRATOR.value,
    ChatMemberStatus.MEMBER.value,
}


def chat_member_status_value(status: ChatMemberStatus | str) -> str:
    return status.value if isinstance(status, ChatMemberStatus) else status


async def is_channel_member(bot: Bot, channel_chat_id: int, user_id: int) -> tuple[bool, str]:
    try:
        member = await bot.get_chat_member(chat_id=channel_chat_id, user_id=user_id)
    except TelegramAPIError as exc:
        logger.warning(
            "Failed to check membership for user %s in channel %s: %s",
            user_id,
            channel_chat_id,
            exc,
        )
        return False, "check_failed"
    status = chat_member_status_value(member.status)
    return status in VALID_MEMBER_STATUSES, status
