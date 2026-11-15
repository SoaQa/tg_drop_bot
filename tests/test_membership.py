from datetime import timedelta

import pytest

from tg_drop_bot.db.models import Participant
from tg_drop_bot.services.captcha import captcha_response_ms
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import pick_winners
from tg_drop_bot.services.membership import draw_weight


@pytest.mark.parametrize(
    ("newcomer", "response_ms", "expected"),
    [
        (False, 1999, (1.0, "fast_captcha")),
        (True, 1999, (1.0, "newcomer_fast_captcha")),
        (False, 2000, (1.0, "unknown")),
        (True, 2000, (1.0, "newcomer")),
        (False, None, (1.0, "unknown")),
        (True, None, (1.0, "newcomer")),
        (False, 0, (1.0, "fast_captcha")),
        (True, -1, (1.0, "newcomer")),
    ],
)
def test_equal_weight_preserves_diagnostic_facts(newcomer, response_ms, expected) -> None:
    assert draw_weight(newcomer, response_ms) == expected


def test_captcha_response_time_requires_valid_send_timestamp() -> None:
    sent_at = utc_now()
    assert captcha_response_ms(None, sent_at) is None
    assert captcha_response_ms(sent_at, sent_at - timedelta(microseconds=1)) is None
    assert captcha_response_ms(sent_at, sent_at + timedelta(microseconds=1_999_999)) == 1999
    assert captcha_response_ms(sent_at, sent_at + timedelta(seconds=2)) == 2000


def test_equal_selection_unique_and_bounded() -> None:
    participants = [Participant(user_id=i, selection_weight=0.7 if i % 2 else 1) for i in range(20)]
    selected = pick_winners(participants, 100)
    assert len(selected) == 20
    assert len({p.user_id for p in selected}) == 20
    assert pick_winners(participants, 0) == []
    assert pick_winners([], 3) == []


def test_equal_selection_ignores_legacy_weights(monkeypatch) -> None:
    from unittest.mock import Mock

    from tg_drop_bot.services import giveaways

    participants = [
        Participant(user_id=1, selection_weight=1),
        Participant(user_id=2, selection_weight=0.35),
        Participant(user_id=3, selection_weight=0.5),
    ]
    sampler = Mock()
    sampler.sample.return_value = [participants[1]]
    monkeypatch.setattr(giveaways.random, "SystemRandom", lambda: sampler)
    assert pick_winners(participants, 1) == [participants[1]]
    sampler.sample.assert_called_once_with(participants, 1)
