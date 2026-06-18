from types import SimpleNamespace
from typing import cast

from aiogram import Bot
from aiogram.enums import ChatMemberStatus

from tg_drop_bot.services.telegram import chat_member_status_value, is_group_member


def test_chat_member_status_value_accepts_enum_and_string() -> None:
    assert chat_member_status_value(ChatMemberStatus.MEMBER) == "member"
    assert chat_member_status_value("left") == "left"


async def test_is_group_member_handles_string_status() -> None:
    class BotStub:
        async def get_chat_member(self, chat_id: int, user_id: int) -> SimpleNamespace:
            assert chat_id == -100123
            assert user_id == 42
            return SimpleNamespace(status="left")

    is_member, status = await is_group_member(cast(Bot, BotStub()), -100123, 42)

    assert is_member is False
    assert status == "left"
