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
        lane.update(phase=0, speed=0.45, count=1)
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


@pytest.mark.parametrize(('first', 'second'), [
    (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2), (3, 3),
])
def test_random_roads_and_mower_all_layouts(monkeypatch, first, second):
    counts = [first-1, second-2 if first == 1 else second-1]
    monkeypatch.setattr('tg_drop_bot.services.frog.secrets.randbelow',
                        lambda n: counts.pop(0) if counts else 0)
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


def test_no_single_lane_on_both_roads(monkeypatch):
    import random

    rng = random.Random(15112026)
    monkeypatch.setattr('tg_drop_bot.services.frog.secrets.randbelow', rng.randrange)
    seen = set()
    for _ in range(1000):
        game = new_game({'id': 123}, 1, 1000)
        middle = next(lane['row'] for lane in game['lanes'] if lane['kind'] == 'mower')
        first, second = middle-1, game['rows']-middle-2
        assert 1 <= first <= 3 and 1 <= second <= 3
        assert first != 1 or second >= 2
        assert second != 1 or first >= 2
        seen.add((first, second))
    assert len(seen) == 8


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
        for tick in range(1, 76):
            next_rows = set()
            previous, now = 1000+(tick-1)*0.4, 1000+tick*0.4
            for row in reachable:
                if any(collision(game, 3, row, previous+i*0.04) for i in range(11)):
                    continue
                for target in (row, min(row+1, finish)):
                    if not collision(game, 3, target, now):
                        next_rows.add(target)
            reachable = next_rows
            if finish in reachable:
                break
        assert finish in reachable, 'No safe forward-only route within 30 seconds'


@pytest.mark.parametrize(('first', 'second'), [
    (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2), (3, 3),
])
def test_fallback_is_solvable_and_blocks_delayed_autoclick(monkeypatch, first, second):
    from copy import deepcopy

    from tg_drop_bot.services.frog import blocks_blind_rushes, has_timed_route

    counts = [first-1, second-2 if first == 1 else second-1]
    monkeypatch.setattr('tg_drop_bot.services.frog.secrets.randbelow',
                        lambda n: counts.pop(0) if counts else 0)
    template = new_game({'id': 123}, 1, 1000)
    assert blocks_blind_rushes(template['lanes'])
    assert has_timed_route(template)
    for delay in (i*0.25 for i in range(81)):
        for interval in (0.05, 0.15, 0.28, 0.3, 0.35, 0.43, 0.5, 0.8, 1.2, 2, 3.1):
            game = deepcopy(template)
            for click in range(120):
                advance(game, 'up', game['seq'], 1000+delay+click*interval)
                if game['status'] != 'playing':
                    break
            assert game['status'] == 'lost', (first, second, delay, interval)


def test_second_vehicle_is_visible_to_server_and_legacy_sessions_keep_one_vehicle():
    game = safe_game()
    game['lanes'][0].update(phase=8.5, speed=0, count=2)
    assert collision(game, 3, 1, 1000)
    game['lanes'][0].pop('count')
    assert not collision(game, 3, 1, 1000)
