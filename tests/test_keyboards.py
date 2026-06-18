from tg_drop_bot.bot.keyboards import (
    CHANNEL_REQUEST_ID,
    active_giveaways_keyboard,
    channel_request_keyboard,
    draft_conditions_keyboard,
    giveaway_card_keyboard,
    participation_keyboard,
)
from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.conditions import CHANNEL_SUBSCRIPTION_CONDITION


def test_channel_request_keyboard_requests_channel_with_bot_member() -> None:
    keyboard = channel_request_keyboard()
    button = keyboard.keyboard[0][0]

    assert button.text == "Выбрать канал"
    assert button.request_chat is not None
    assert button.request_chat.request_id == CHANNEL_REQUEST_ID
    assert button.request_chat.chat_is_channel is True
    assert button.request_chat.bot_is_member is True
    assert button.request_chat.request_title is True


def test_draft_conditions_keyboard_has_channel_subscription_option() -> None:
    keyboard = draft_conditions_keyboard()
    button = keyboard.inline_keyboard[0][0]

    assert button.text == "Подписчики канала"
    assert button.callback_data == f"draft:condition:{CHANNEL_SUBSCRIPTION_CONDITION}"


def test_giveaway_card_keyboard_uses_human_friendly_labels() -> None:
    keyboard = giveaway_card_keyboard(Giveaway(id=1, creator_user_id=1))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Список участников" in labels
    assert "CSV" not in labels
    assert "Изменить текст" in labels
    assert "Условие участия" in labels


def test_draft_giveaway_card_keyboard_has_delete_button() -> None:
    keyboard = giveaway_card_keyboard(Giveaway(id=1, creator_user_id=1, status="draft"))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "Удалить черновик" in labels
    assert "draft:delete:1" in callbacks


def test_participation_keyboard_uses_direct_deep_link_when_bot_username_known() -> None:
    keyboard = participation_keyboard(7, "dropbot")
    button = keyboard.inline_keyboard[0][0]

    assert button.text == "Участвовать"
    assert button.url == "https://t.me/dropbot?start=giveaway_7"
    assert button.callback_data is None


def test_active_giveaways_keyboard_uses_private_callback_selection() -> None:
    keyboard = active_giveaways_keyboard([Giveaway(id=7, creator_user_id=1)])
    button = keyboard.inline_keyboard[0][0]

    assert button.text == "Участвовать в розыгрыше #7"
    assert button.callback_data == "participate:7"
