from datetime import timedelta

import pytest

from tg_drop_bot.db.models import Participant
from tg_drop_bot.services.captcha import captcha_response_ms
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import pick_weighted_winners
from tg_drop_bot.services.membership import draw_weight


@pytest.mark.parametrize(
    ("newcomer", "response_ms", "expected"),
    [
        (False, 1999, (0.5, "fast_captcha")),
        (True, 1999, (0.35, "newcomer_fast_captcha")),
        (False, 2000, (1.0, "unknown")),
        (True, 2000, (0.7, "newcomer")),
        (False, None, (1.0, "unknown")),
        (True, None, (0.7, "newcomer")),
    ],
)
def test_fast_captcha_halves_draw_weight(newcomer, response_ms, expected) -> None:
    assert draw_weight(newcomer, response_ms) == expected


def test_captcha_response_time_requires_valid_send_timestamp() -> None:
    sent_at = utc_now()
    assert captcha_response_ms(None, sent_at) is None
    assert captcha_response_ms(sent_at, sent_at - timedelta(microseconds=1)) is None
    assert captcha_response_ms(sent_at, sent_at + timedelta(microseconds=1_999_999)) == 1999
    assert captcha_response_ms(sent_at, sent_at + timedelta(seconds=2)) == 2000


def test_weighted_selection_unique_and_bounded() -> None:
    participants = [Participant(user_id=i, selection_weight=0.7 if i % 2 else 1) for i in range(20)]
    selected = pick_weighted_winners(participants, 100)
    assert len(selected) == 20
    assert len({p.user_id for p in selected}) == 20
    assert pick_weighted_winners(participants, 0) == []
    assert pick_weighted_winners([], 3) == []


def test_weighted_selection_distribution() -> None:
    participants = [
        Participant(user_id=1, selection_weight=1),
        Participant(user_id=2, selection_weight=0.7),
    ]
    # Wide bounds keep the production entropy path tested without a flaky exact assertion.
    old_wins = sum(pick_weighted_winners(participants, 1)[0].user_id == 1 for _ in range(5000))
    assert 2700 < old_wins < 3200
