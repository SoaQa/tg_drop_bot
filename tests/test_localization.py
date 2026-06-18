from tg_drop_bot.db.models import Giveaway, Participant
from tg_drop_bot.services.csv_export import participants_to_csv
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.rendering import giveaway_status_label, membership_status_label


def test_giveaway_statuses_have_russian_labels() -> None:
    assert giveaway_status_label("draft") == "черновик"
    assert giveaway_status_label("published") == "опубликован"
    assert giveaway_status_label("finished") == "завершен"
    assert giveaway_status_label("cancelled") == "отменен"


def test_membership_statuses_have_russian_labels() -> None:
    assert membership_status_label("member") == "участник"
    assert membership_status_label("left") == "не подписан на канал"
    assert membership_status_label("check_failed") == "проверка не удалась"


def test_csv_export_is_user_facing_russian() -> None:
    participant = Participant(
        giveaway=Giveaway(id=1, creator_user_id=10),
        giveaway_id=1,
        user_id=100,
        username="user",
        first_name="Имя",
        last_name="Фамилия",
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
        membership_status="member",
    )

    csv_text = participants_to_csv([participant]).decode("utf-8-sig")

    assert "имя_пользователя" in csv_text
    assert "статус_членства" in csv_text
    assert "участник" in csv_text
