import pytest

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


@pytest.mark.parametrize(("status", "count"), [("draft", 0), ("published", 1250)])
def test_active_template_keeps_description_and_adds_antifraud_notice(
    status: str, count: int
) -> None:
    giveaway = Giveaway(
        id=1, creator_user_id=1, status=status, title="Тестовый приз",
        post_text="Мой текст\n\n1) Мои условия", terms_text="Подписка на канал",
        winners_count=2, deadline_at=utc_now(),
    )
    text = render_giveaway_post(giveaway, Settings(), participants_count=count)
    assert "<b>Тестовый приз</b>\nМой текст\n\n1) Мои условия" in text
    assert f"<b>Участников:</b> {count}" in text
    assert text.index("антифрод-проверками") > text.index("<b>Завершение:</b>")
    assert text.endswith("указанного в описании канала.")
    assert "шансы на победу" not in text


@pytest.mark.parametrize(
    ("status", "closed", "footer"),
    [
        ("finished", False, "Участие закрыто."),
        ("published", True, "Участие закрыто."),
        ("cancelled", True, "Розыгрыш отменен."),
    ],
)
def test_closed_post_replaces_antifraud_notice(status: str, closed: bool, footer: str) -> None:
    giveaway = Giveaway(id=1, creator_user_id=1, status=status, post_text="Мой текст")
    text = render_giveaway_post(giveaway, Settings(), closed=closed)
    assert "Мой текст" in text
    assert text.endswith(footer)
    assert "антифрод" not in text


def test_render_results_announcement_lists_winners() -> None:
    from tg_drop_bot.db.models import Participant
    from tg_drop_bot.services.rendering import render_results_announcement

    giveaway = Giveaway(id=1, creator_user_id=1, title="Routerich AX3000", winners_count=2)
    winners = [
        Participant(
            giveaway_id=1,
            user_id=100,
            username="winner_one",
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        ),
        Participant(
            giveaway_id=1,
            user_id=200,
            first_name="Имя",
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        ),
    ]

    text = render_results_announcement(giveaway, winners)

    assert "Итоги розыгрыша: Routerich AX3000" in text
    assert "1. @winner_one" in text
    assert '2. <a href="tg://user?id=200">Имя</a>' in text
    assert "Поздравляем победителей!" in text
    assert "меньше призовых мест" not in text
    assert "антифрод" not in text
    assert "бота обратной связи" not in text
    assert "t.me/" not in text


def test_render_results_announcement_without_winners() -> None:
    from tg_drop_bot.services.rendering import render_results_announcement

    giveaway = Giveaway(id=1, creator_user_id=1, title="Приз", winners_count=3)

    text = render_results_announcement(giveaway, [])

    assert "Победителей нет: не нашлось валидных участников." in text


def test_render_results_announcement_reports_winner_shortage() -> None:
    from tg_drop_bot.db.models import Participant
    from tg_drop_bot.services.rendering import render_results_announcement

    giveaway = Giveaway(id=1, creator_user_id=1, title="Приз", winners_count=5)
    winners = [
        Participant(
            giveaway_id=1,
            user_id=100,
            username="winner",
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        )
    ]

    text = render_results_announcement(giveaway, winners)

    assert "разыграно 1 из 5" in text


def test_render_results_announcement_truncates_huge_winner_lists() -> None:
    from tg_drop_bot.db.models import Participant
    from tg_drop_bot.services.rendering import MAX_ANNOUNCED_WINNERS, render_results_announcement

    giveaway = Giveaway(id=1, creator_user_id=1, title="Приз", winners_count=80)
    winners = [
        Participant(
            giveaway_id=1,
            user_id=index,
            username=f"user{index}",
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        )
        for index in range(80)
    ]

    text = render_results_announcement(giveaway, winners)

    assert f"...и еще победителей: {80 - MAX_ANNOUNCED_WINNERS}" in text
    assert len(text) < 4096


def test_channel_post_link_variants() -> None:
    from tg_drop_bot.db.models import KnownChannel
    from tg_drop_bot.services.rendering import channel_post_link

    public = KnownChannel(telegram_chat_id=-1001234, title="Канал", username="my_channel")
    private = KnownChannel(telegram_chat_id=-1001234, title="Канал", username=None)
    group_like = KnownChannel(telegram_chat_id=-555, title="Канал", username=None)

    assert channel_post_link(public, 42) == "https://t.me/my_channel/42"
    assert channel_post_link(private, 42) == "https://t.me/c/1234/42"
    assert channel_post_link(group_like, 42) is None
    assert channel_post_link(None, 42) is None
    assert channel_post_link(public, None) is None
