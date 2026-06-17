from datetime import timedelta

from tg_drop_bot.db.models import Giveaway, Participant
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import pick_winners, validate_draft


def test_validate_draft_reports_missing_fields() -> None:
    giveaway = Giveaway(creator_user_id=1)
    assert validate_draft(giveaway) == [
        "группа",
        "текст",
        "условия",
        "количество победителей",
        "дедлайн",
    ]


def test_pick_winners_uses_available_participants_when_shortage() -> None:
    participants = [
        Participant(
            giveaway_id=1,
            user_id=1,
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        ),
        Participant(
            giveaway_id=1,
            user_id=2,
            joined_at=utc_now() + timedelta(seconds=1),
            captcha_passed_at=utc_now() + timedelta(seconds=1),
        ),
    ]
    winners = pick_winners(participants, 5)
    assert sorted(participant.user_id for participant in winners) == [1, 2]
