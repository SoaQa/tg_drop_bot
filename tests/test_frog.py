import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest

from tg_drop_bot.services.frog import advance, new_game, public_game, validate_init_data

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


def test_seven_real_steps_reach_finish_and_public_state_hides_identity():
    game = safe_game()
    for i in range(7):
        advance(game, "up", game["seq"], 1000 + (i+1)*0.3)
    assert game["status"] == "won" and game["y"] == 7
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


def test_collision_between_requests_and_expiration():
    game = safe_game()
    game.update(y=1, x=3)
    game["lanes"][0].update(phase=2.5, speed=0.6)
    advance(game, "wait", 0, 1010)
    assert game["status"] == "lost"
    game = safe_game()
    advance(game, "up", 0, 1301)
    assert game["status"] == "expired"


def test_stay_inside_board_and_never_move_after_collision():
    game = safe_game()
    advance(game, "down", 0, 1000.3)
    assert game["y"] == 0
    game["lanes"][0].update(phase=4, speed=0)
    advance(game, "up", 1, 1000.6)
    assert game["status"] == "lost"
    advance(game, "up", 2, 1001)
    assert game["y"] == 1
