import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from tg_drop_bot.services.frog import advance, collision, new_game, public_game, validate_init_data

TOKEN = "123456:test"


def signed(user_id=123, at=1000):
    data = {"auth_date": str(at), "user": json.dumps({"id": user_id, "first_name": "Тест"})}
    key = hmac.digest(b"WebAppData", TOKEN.encode(), hashlib.sha256)
    data["hash"] = hmac.new(
        key, "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(), hashlib.sha256,
    ).hexdigest()
    return urlencode(data)


def test_telegram_auth_rejects_tampering_expiry_future_and_duplicate():
    assert validate_init_data(signed(), TOKEN, 1001)["id"] == 123
    for raw, now in [(signed().replace("123", "456"), 1001), (signed(), 5000),
                     (signed(), 999), (signed() + "&auth_date=1000", 1001)]:
        with pytest.raises(ValueError):
            validate_init_data(raw, TOKEN, now)
    with pytest.raises(ValueError):
        validate_init_data(signed(), "other-token", 1001)


def safe_game():
    game = new_game({"id": 123}, 7, 1000)
    for lane in game["lanes"]:
        lane.update(phase=0, speed=0.45)
    return game


def test_real_steps_reach_finish_and_public_state_hides_identity():
    game = safe_game()
    finish = game['rows'] - 1
    for i in range(finish):
        advance(game, "up", game["seq"], 1000 + (i+1)*0.3)
    assert game["status"] == "won" and game["y"] == finish
    assert "user" not in public_game(game, 1003)
    assert "giveaway_id" not in public_game(game, 1003)


def test_no_teleport_replay_or_client_declared_win():
    game = safe_game()
    with pytest.raises(ValueError):
        advance(game, "won", 0, 1000.3)
    with pytest.raises(ValueError):
        advance(game, "up", 5, 1000.3)
    advance(game, "up", 0, 1000.3)
    advance(game, "up", 1, 1000.31)
    assert game["y"] == 1
    with pytest.raises(ValueError):
        advance(game, "up", 1, 1000.7)


@pytest.mark.parametrize('first', [1, 2, 3])
@pytest.mark.parametrize('second', [1, 2, 3])
def test_random_roads_and_mower_all_layouts(monkeypatch, first, second):
    counts = iter([first-1, second-1])
    monkeypatch.setattr('tg_drop_bot.services.frog.secrets.randbelow',
                        lambda n: next(counts) if n == 3 else 0)
    game = new_game({'id': 123}, 1, 1000)
    assert game['rows'] == first + second + 3
    middle = first + 1
    assert [lane['row'] for lane in game['lanes'] if lane['kind'] == 'car'] == [
        *range(1, middle), *range(middle+1, middle+1+second),
    ]
    mower = [lane for lane in game['lanes'] if lane['kind'] == 'mower']
    assert len(mower) == 1 and mower[0]['row'] == middle
    mower[0].update(phase=4, speed=0)
    game.update(y=middle)
    advance(game, 'wait', 0, 1000.3)
    assert game['status'] == 'lost'


def test_collision_between_requests_and_expiration():
    game = safe_game()
    game.update(y=1, x=3)
    game["lanes"][0].update(phase=2.5, speed=0.6)
    advance(game, "wait", 0, 1010)
    assert game["status"] == "lost"
    game = safe_game()
    advance(game, "up", 0, 1301)
    assert game["status"] == "expired"


def test_only_forward_moves_and_never_move_after_collision():
    game = safe_game()
    for direction in ("down", "left", "right"):
        with pytest.raises(ValueError):
            advance(game, direction, 0, 1000.3)
    assert game["y"] == 0
    assert game["x"] == 3
    game["lanes"][0].update(phase=4, speed=0)
    advance(game, "up", 0, 1000.6)
    assert game["status"] == "lost"
    advance(game, "up", 1, 1001)
    assert game["y"] == 1


@pytest.mark.parametrize('interval', [0, 0.05, 0.15, 0.28, 0.35, 0.5])
def test_blind_immediate_tapping_does_not_win(interval):
    for _ in range(30):
        game = new_game({'id': 123}, 1, 1000)
        for i in range(15):
            advance(game, 'up', game['seq'], 1000 + i*interval)
        assert game['status'] == 'lost'


def test_timing_aware_player_can_cross_generated_roads(monkeypatch):
    import random

    rng = random.Random(20261115)
    monkeypatch.setattr('tg_drop_bot.services.frog.secrets.randbelow', rng.randrange)
    for _ in range(40):
        game = new_game({'id': 123}, 1, 1000)
        reachable = {0}
        finish = game['rows']-1
        for tick in range(1, 151):
            next_rows = set()
            previous, now = 1000+(tick-1)*0.3, 1000+tick*0.3
            for row in reachable:
                if any(collision(game, 3, row, previous+i*0.03) for i in range(11)):
                    continue
                for target in (row, min(row+1, finish)):
                    if not collision(game, 3, target, now):
                        next_rows.add(target)
            reachable = next_rows
            if finish in reachable:
                break
        assert finish in reachable, 'No safe forward-only route within 45 seconds'
