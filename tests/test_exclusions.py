from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_drop_bot.bot.exclusions import exclusions_list, parse_callback_int, revoke_confirm
from tg_drop_bot.bot.keyboards import admin_main_keyboard, giveaway_card_keyboard
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway


async def test_nonadmin_cannot_revoke_exclusions() -> None:
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    session = AsyncMock()
    await revoke_confirm(callback, session, Settings(ADMIN_IDS="1"))
    callback.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
    session.commit.assert_not_awaited()
    session.execute.assert_not_awaited()


async def test_nonadmin_cannot_list_exclusions() -> None:
    callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock())
    session = AsyncMock()
    await exclusions_list(callback, session, Settings(ADMIN_IDS="1"))
    callback.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
    session.scalars.assert_not_awaited()


def test_antibot_controls_are_available_to_organizer() -> None:
    assert "Исключения" in [b.text for row in admin_main_keyboard().keyboard for b in row]
    keyboard = giveaway_card_keyboard(Giveaway(id=42, creator_user_id=1, status="draft"))
    assert "anti:giveaway:42" in [
        b.callback_data for row in keyboard.inline_keyboard for b in row
    ]


def test_exclusion_callbacks_reject_overflowing_database_ids() -> None:
    assert parse_callback_int("anti:revoke:99999999999999999999999", 2) is None
    assert parse_callback_int("anti:revoke:42", 2) == 42
