from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from aiogram.types import User

from tg_drop_bot.bot import exclusions
from tg_drop_bot.bot.exclusions import (
    ExclusionSearch,
    exclusions_list,
    parse_callback_int,
    prohibit_confirm,
    prohibit_preview,
    revoke_confirm,
)
from tg_drop_bot.bot.keyboards import admin_main_keyboard, giveaway_card_keyboard
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway, KnownChannel
from tg_drop_bot.services import exclusion_identity


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
    assert "anti:giveaway:42" in [b.callback_data for row in keyboard.inline_keyboard for b in row]


def test_exclusion_callbacks_reject_overflowing_database_ids() -> None:
    assert parse_callback_int("anti:revoke:99999999999999999999999", 2) is None
    assert parse_callback_int("anti:revoke:42", 2) == 42


@pytest.mark.parametrize(
    ("value", "target"),
    [
        (" @RaffleSample ", "rafflesample"),
        ("RaffleSample", "rafflesample"),
        ("12345", 12345),
        ("0", None),
        (str(2**52), None),
        ("@@name", None),
        ("@имя", None),
        ("@name example", None),
    ],
)
def test_parse_exclusion_target(value, target) -> None:
    assert exclusion_identity.parse_exclusion_target(value) == target


async def test_resolve_uses_current_owner_not_historical_owner(monkeypatch) -> None:
    monkeypatch.setattr(exclusion_identity, "username_candidates", AsyncMock(return_value=[42, 43]))
    bot = AsyncMock()
    bot.get_chat_member.side_effect = [
        SimpleNamespace(user=User(id=42, first_name="Old", is_bot=False, username="other")),
        SimpleNamespace(user=User(id=43, first_name="New", is_bot=False, username="RaffleSample")),
    ]
    user = await exclusion_identity.resolve_exclusion_username(
        AsyncMock(), bot, -100123, "rafflesample"
    )
    assert user is not None and user.id == 43


async def test_username_api_error_does_not_trust_historical_match(monkeypatch) -> None:
    monkeypatch.setattr(exclusion_identity, "username_candidates", AsyncMock(return_value=[42]))
    bot = AsyncMock()
    bot.get_chat_member.side_effect = TelegramBadRequest(
        GetChatMember(chat_id=-100123, user_id=42), "user not found"
    )
    assert (
        await exclusion_identity.resolve_exclusion_username(
            AsyncMock(), bot, -100123, "rafflesample"
        )
        is None
    )


@pytest.mark.parametrize(("value", "username"), [("@RaffleSample", "rafflesample"), ("42", None)])
async def test_prohibition_preview_preserves_numeric_id_and_resolves_username(
    monkeypatch,
    value,
    username,
) -> None:
    settings = Settings(ADMIN_IDS="1")
    message = AsyncMock(text=value, from_user=SimpleNamespace(id=1))
    state = AsyncMock(get_data=AsyncMock(return_value={"prohibit_channel_id": 3}))
    session = AsyncMock(
        get=AsyncMock(return_value=KnownChannel(title="Канал", telegram_chat_id=-10))
    )
    bot = AsyncMock()
    resolve = AsyncMock(
        return_value=User(id=42, first_name="User", is_bot=False, username="RaffleSample")
    )
    monkeypatch.setattr(exclusions, "resolve_exclusion_username", resolve)

    await prohibit_preview(message, state, settings, session, bot)

    state.set_state.assert_awaited_once_with(ExclusionSearch.confirm_prohibition)
    state.update_data.assert_awaited_once_with(prohibit_user_id=42, prohibit_username=username)
    prompt = message.answer.await_args.args[0]
    assert "<code>42</code>" in prompt and "«Канал»" in prompt
    if username:
        assert "@RaffleSample" in prompt
        resolve.assert_awaited_once_with(session, bot, -10, username)
    else:
        resolve.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("username_still_matches", [True, False])
async def test_prohibition_checks_username_again_before_saving(
    monkeypatch,
    username_still_matches,
) -> None:
    settings = Settings(ADMIN_IDS="1")
    callback = AsyncMock(data="anti:block:3:42", from_user=SimpleNamespace(id=1))
    state = AsyncMock(
        get_data=AsyncMock(
            return_value={
                "prohibit_channel_id": 3,
                "prohibit_user_id": 42,
                "prohibit_username": "rafflesample",
            }
        )
    )
    session = AsyncMock(get=AsyncMock(return_value=KnownChannel(telegram_chat_id=-10)))
    bot = AsyncMock()
    verify = AsyncMock(
        return_value=User(id=42, first_name="User", is_bot=False)
        if username_still_matches
        else None
    )
    prohibit = AsyncMock(return_value=True)
    monkeypatch.setattr(exclusions, "verify_username", verify)
    monkeypatch.setattr(exclusions, "prohibit_participation", prohibit)

    await prohibit_confirm(callback, state, session, settings, bot)

    verify.assert_awaited_once_with(bot, -10, 42, "rafflesample")
    if username_still_matches:
        prohibit.assert_awaited_once_with(session, 3, 42, 1)
        session.commit.assert_awaited_once()
    else:
        prohibit.assert_not_awaited()
        session.commit.assert_not_awaited()
        state.set_state.assert_awaited_once_with(ExclusionSearch.prohibit_id)
