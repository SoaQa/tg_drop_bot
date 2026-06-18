from tg_drop_bot.bot.admin import parse_edit_callback_data


def test_parse_edit_callback_data_accepts_known_shape() -> None:
    assert parse_edit_callback_data("giveaway:edit_title:7") == ("title", 7)


def test_parse_edit_callback_data_rejects_malformed_payload() -> None:
    assert parse_edit_callback_data(None) is None
    assert parse_edit_callback_data("giveaway:edit_title") is None
    assert parse_edit_callback_data("giveaway:edit_title:not-number") is None
    assert parse_edit_callback_data("draft:edit_title:7") is None
