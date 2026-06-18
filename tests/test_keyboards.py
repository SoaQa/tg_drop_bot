from tg_drop_bot.bot.keyboards import GROUP_REQUEST_ID, group_request_keyboard


def test_group_request_keyboard_requests_group_chat_with_bot_member() -> None:
    keyboard = group_request_keyboard()
    button = keyboard.keyboard[0][0]

    assert button.text == "Выбрать группу"
    assert button.request_chat is not None
    assert button.request_chat.request_id == GROUP_REQUEST_ID
    assert button.request_chat.chat_is_channel is False
    assert button.request_chat.bot_is_member is True
    assert button.request_chat.request_title is True
