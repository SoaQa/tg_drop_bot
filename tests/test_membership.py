from tg_drop_bot.db.models import Participant
from tg_drop_bot.services.giveaways import pick_weighted_winners


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
