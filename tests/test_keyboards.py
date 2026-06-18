from tg_drop_bot.bot.keyboards import CHANNEL_REQUEST_ID, channel_request_keyboard


def test_channel_request_keyboard_requests_channel_with_bot_member() -> None:
    keyboard = channel_request_keyboard()
    button = keyboard.keyboard[0][0]

    assert button.text == "Выбрать канал"
    assert button.request_chat is not None
    assert button.request_chat.request_id == CHANNEL_REQUEST_ID
    assert button.request_chat.chat_is_channel is True
    assert button.request_chat.bot_is_member is True
    assert button.request_chat.request_title is True
