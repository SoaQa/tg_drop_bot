from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.rendering import render_giveaway_post


def test_render_giveaway_post_includes_participants_count_when_provided() -> None:
    giveaway = Giveaway(
        id=1,
        creator_user_id=1,
        title="Routerich AX3000",
        post_text="Текст розыгрыша",
        terms_text="Подписка на канал",
        winners_count=1,
        deadline_at=utc_now(),
    )

    text = render_giveaway_post(giveaway, Settings(), participants_count=12)

    assert "<b>Routerich AX3000</b>" in text
    assert "Текст розыгрыша" in text
    assert "<b>Участников:</b> 12" in text


def test_render_giveaway_post_omits_participants_count_by_default() -> None:
    giveaway = Giveaway(
        id=1,
        creator_user_id=1,
        title="Routerich AX3000",
        post_text="Текст розыгрыша",
        winners_count=1,
        deadline_at=utc_now(),
    )

    text = render_giveaway_post(giveaway, Settings())

    assert "Участников" not in text


def test_render_giveaway_post_does_not_duplicate_title_as_description() -> None:
    giveaway = Giveaway(
        id=1,
        creator_user_id=1,
        title="Routerich AX3000",
        post_text="Routerich AX3000",
        winners_count=1,
        deadline_at=utc_now(),
    )

    text = render_giveaway_post(giveaway, Settings())

    assert text.count("Routerich AX3000") == 1
