from __future__ import annotations

import asyncio
import re

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import User
from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import Participant, ParticipationFlow

USERNAME = re.compile(r"[A-Za-z0-9_]{1,32}")
MAX_CANDIDATES = 8


def parse_exclusion_target(value: str) -> int | str | None:
    value = value.strip()
    if value.isascii() and value.isdecimal():
        if len(value) <= 16 and 0 < (user_id := int(value)) < 2**52:
            return user_id
        return None
    username = value.removeprefix("@")
    return username.lower() if USERNAME.fullmatch(username) else None


async def username_candidates(session: AsyncSession, username: str) -> list[int]:
    matches = union(
        select(Participant.user_id).where(func.lower(Participant.username) == username),
        select(ParticipationFlow.user_id).where(
            func.lower(ParticipationFlow.user_profile["username"].astext) == username
        ),
    )
    return list((await session.scalars(matches.limit(MAX_CANDIDATES + 1))).all())


async def verify_username(bot: Bot, chat_id: int, user_id: int, username: str) -> User | None:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id, request_timeout=5)
    except TelegramAPIError:
        return None
    user = member.user
    if user.id == user_id and user.username and user.username.lower() == username:
        return user
    return None


async def resolve_exclusion_username(
    session: AsyncSession, bot: Bot, chat_id: int, username: str
) -> User | None:
    candidates = await username_candidates(session, username)
    if not candidates or len(candidates) > MAX_CANDIDATES:
        return None
    # Usernames can be reassigned; historic applications only identify candidates.
    verified = await asyncio.gather(
        *(verify_username(bot, chat_id, user_id, username) for user_id in candidates)
    )
    matches = [user for user in verified if user is not None]
    return matches[0] if len(matches) == 1 else None
