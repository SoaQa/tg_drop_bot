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
        key,
        "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(data)


def test_telegram_auth_rejects_tampering_expiry_future_and_duplicate():
    assert validate_init_data(signed(), TOKEN, 1001)["id"] == 123
    for raw, now in [
        (signed().replace("123", "456"), 1001),
        (signed(), 5000),
        (signed(), 999),
        (signed() + "&auth_date=1000", 1001),
    ]:
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
    finish = game["rows"] - 1
    for i in range(finish):
        advance(game, "up", game["seq"], 1000 + (i + 1) * 0.3)
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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (1, 2),
        (1, 3),
        (2, 1),
        (2, 2),
        (3, 1),
    ],
)
def test_random_roads_and_mower_all_layouts(monkeypatch, first, second):
    counts = [first - 1, second - 2 if first == 1 else second - 1]
    monkeypatch.setattr(
        "tg_drop_bot.services.frog.secrets.randbelow", lambda n: counts.pop(0) if counts else 0
    )
    game = new_game({"id": 123}, 1, 1000)
    assert game["rows"] == first + second + 3
    middle = first + 1
    assert [lane["row"] for lane in game["lanes"] if lane["kind"] == "car"] == [
        *range(1, middle),
        *range(middle + 1, middle + 1 + second),
    ]
    mower = [lane for lane in game["lanes"] if lane["kind"] == "mower"]
    assert len(mower) == 1 and mower[0]["row"] == middle
    mower[0].update(phase=4, speed=0)
    game.update(y=middle)
    advance(game, "wait", 0, 1000.3)
    assert game["status"] == "lost"


def test_no_single_lane_on_both_roads(monkeypatch):
    import random

    rng = random.Random(15112026)
    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", rng.randrange)
    seen = set()
    for _ in range(1000):
        game = new_game({"id": 123}, 1, 1000)
        middle = next(lane["row"] for lane in game["lanes"] if lane["kind"] == "mower")
        first, second = middle - 1, game["rows"] - middle - 2
        assert 1 <= first <= 3 and 1 <= second <= 3
        assert first != 1 or second >= 2
        assert second != 1 or first >= 2
        seen.add((first, second))
    assert len(seen) == 5
    assert all(a + b <= 4 for a, b in seen)


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


@pytest.mark.parametrize("interval", [0, 0.05, 0.15, 0.28, 0.35, 0.5])
def test_blind_immediate_tapping_does_not_win(interval):
    for _ in range(30):
        game = new_game({"id": 123}, 1, 1000)
        for i in range(15):
            advance(game, "up", game["seq"], 1000 + i * interval)
        assert game["status"] == "lost"


def test_timing_aware_player_can_cross_generated_roads(monkeypatch):
    import random

    rng = random.Random(20261115)
    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", rng.randrange)
    for _ in range(40):
        game = new_game({"id": 123}, 1, 1000)
        reachable = {0}
        finish = game["rows"] - 1
        for tick in range(1, 76):
            next_rows = set()
            previous, now = 1000 + (tick - 1) * 0.4, 1000 + tick * 0.4
            for row in reachable:
                if any(collision(game, 3, row, previous + i * 0.04) for i in range(11)):
                    continue
                for target in (row, min(row + 1, finish)):
                    if not collision(game, 3, target, now):
                        next_rows.add(target)
            reachable = next_rows
            if finish in reachable:
                break
        assert finish in reachable, "No safe forward-only route within 30 seconds"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (1, 2),
        (1, 3),
        (2, 1),
        (2, 2),
        (3, 1),
    ],
)
def test_fallback_is_solvable_and_blocks_delayed_autoclick(monkeypatch, first, second):
    from copy import deepcopy

    from tg_drop_bot.services.frog import blocks_blind_rushes, has_timed_route

    counts = [first - 1, second - 2 if first == 1 else second - 1]
    monkeypatch.setattr(
        "tg_drop_bot.services.frog.secrets.randbelow", lambda n: counts.pop(0) if counts else 0
    )
    template = new_game({"id": 123}, 1, 1000)
    assert blocks_blind_rushes(template["lanes"])
    assert has_timed_route(template)
    for delay in (i * 0.25 for i in range(81)):
        for interval in (0.05, 0.15, 0.28, 0.3, 0.35, 0.43, 0.5, 0.8, 1.2, 2, 3.1):
            game = deepcopy(template)
            for click in range(120):
                advance(game, "up", game["seq"], 1000 + delay + click * interval)
                if game["status"] != "playing":
                    break
            assert game["status"] == "lost", (first, second, delay, interval)


def test_second_vehicle_is_visible_to_server_and_legacy_sessions_keep_one_vehicle():
    game = safe_game()
    game["lanes"][0].update(phase=8.5, speed=0, count=2)
    assert collision(game, 3, 1, 1000)
    game["lanes"][0].pop("count")
    assert not collision(game, 3, 1, 1000)


@pytest.mark.parametrize(("relaxed", "clear_gap"), [(False, 2.74), (True, 3.15)])
def test_easy_clear_gap_keeps_vehicle_count_speed_and_hitbox(relaxed, clear_gap):
    game = new_game({}, 1, 0, relaxed=relaxed)
    lane = game["lanes"][0]
    assert lane["count"] == 2
    assert abs(lane["speed"]) == pytest.approx(0.675)
    public_lane = public_game(game, 0)["lanes"][0]
    assert public_lane.get("trackLength") == (9.82 if relaxed else None)

    lane["phase"] = 4
    first_exit = 0.88 / abs(lane["speed"])
    next_entry = (0.88 + clear_gap) / abs(lane["speed"])
    assert collision(game, 3, 1, first_exit - 0.001)
    assert not collision(game, 3, 1, first_exit + 0.001)
    assert not collision(game, 3, 1, next_entry - 0.001)
    assert collision(game, 3, 1, next_entry + 0.001)


@pytest.mark.parametrize("assisted", [False, True])
def test_relaxed_fallback_keeps_four_road_lanes(monkeypatch, assisted):
    from tg_drop_bot.services import frog

    monkeypatch.setattr(frog.secrets, "randbelow", lambda n: 0)
    validations = []

    def reject_layout(lanes, *, minimum_hop):
        validations.append(minimum_hop)
        return False

    # Exhaust the random retry budget independently of which phases the RNG produces.
    with monkeypatch.context() as patch:
        patch.setattr(frog, "blocks_blind_rushes", reject_layout)
        game = new_game({}, 1, 0, relaxed=True, assisted=assisted)

    assert validations == [frog.HOP_COOLDOWN] * 64
    assert game["rows"] == 7
    assert [lane["row"] for lane in game["lanes"] if lane["kind"] == "car"] == [1, 2, 4, 5]
    assert all(lane["trackLength"] == 9.82 for lane in game["lanes"])
    median = [lane for lane in game["lanes"] if lane["row"] == 3]
    if assisted:
        assert not median
        assert not collision(game, 3, 3, 5)
    else:
        assert len(median) == 1 and median[0]["kind"] == "mower"
        assert abs(median[0]["speed"]) == pytest.approx(0.3375)
    assert frog.blocks_blind_rushes(game["lanes"])
    assert frog.has_timed_route(game)


@pytest.mark.parametrize("fallback", [False, True])
def test_checkpoint_restart_keeps_mower_away_and_has_a_route(monkeypatch, fallback):
    from random import Random

    from tg_drop_bot.services import frog

    monkeypatch.setattr(frog.secrets, "randbelow", Random(330).randrange)
    with monkeypatch.context() as patch:
        if fallback:
            patch.setattr(frog, "blocks_blind_rushes", lambda *args, **kwargs: False)
        for _ in range(8):
            game = new_game({}, 1, 1000, relaxed=True, from_checkpoint=True)
            assert game["y"] == game["checkpoint_row"] == 3
            assert public_game(game, 1000)["checkpointReached"]
            assert any(lane["kind"] == "mower" for lane in game["lanes"])
            assert frog.has_timed_route(game, start_row=3)
            advance(game, "wait", 0, 1003)
            assert game["status"] == "playing"


@pytest.mark.parametrize("safe_landing", [False, True])
def test_only_a_safe_landing_claims_checkpoint(safe_landing):
    game = new_game({}, 1, 1000, relaxed=True)
    game.update(y=2, lanes=[] if safe_landing else [
        {"kind": "mower", "row": 3, "phase": 4, "speed": 0},
    ])
    advance(game, "up", 0, 1000.4)
    assert game["y"] == 3
    assert public_game(game, 1000.4)["checkpointReached"] is safe_landing


@pytest.mark.parametrize("assisted", [False, True])
async def test_generator_forwards_easy_layout_options(monkeypatch, assisted):
    from random import Random

    from tg_drop_bot.services.frog import blocks_blind_rushes, has_timed_route
    from tg_drop_bot.services.game_runtime import GameGenerator

    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", Random(4511).randrange)
    generator = GameGenerator()
    for _ in range(15):
        game = await generator.generate({}, 1, "easy", relaxed=True, assisted=assisted)
        assert game["difficulty"] == "easy"
        assert game["rows"] == 7
        assert [lane["row"] for lane in game["lanes"] if lane["kind"] == "car"] == [1, 2, 4, 5]
        assert all(lane["trackLength"] == 9.82 for lane in game["lanes"])
        assert any(lane["kind"] == "mower" for lane in game["lanes"]) == (not assisted)
        assert blocks_blind_rushes(game["lanes"])
        assert has_timed_route(game)


@pytest.mark.parametrize("difficulty", ["easy", "hard"])
def test_assistance_does_not_change_legacy_or_hard_physics(monkeypatch, difficulty):
    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", lambda n: 0)
    legacy = new_game({}, 1, 0, difficulty=difficulty)
    assert new_game({}, 1, 0, difficulty=difficulty, assisted=True) == legacy
    assert all("trackLength" not in lane for lane in legacy["lanes"])
    assert any(lane["kind"] == "mower" for lane in legacy["lanes"])
    if difficulty == "hard":
        hard = new_game({}, 1, 0, difficulty=difficulty, relaxed=True, assisted=True,
                        from_checkpoint=True)
        assert hard == legacy
        assert "checkpointReached" not in public_game(hard, 0)


def test_hard_lasers_solvable_and_easy_has_no_lasers(monkeypatch):
    import random

    from tg_drop_bot.services.frog import has_timed_route

    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", random.Random(409).randrange)
    for _ in range(40):
        easy = new_game({}, 1, 1000)
        assert easy["difficulty"] == "easy"
        assert easy["rows"] <= 7
        assert all(
            abs(lane["speed"]) <= (0.675 if lane["kind"] == "car" else 0.3375)
            for lane in easy["lanes"]
        )
        assert all("laser" not in lane for lane in easy["lanes"])
        hard = new_game({}, 1, 1000, difficulty="hard")
        assert hard["rows"] == 9 and hard["difficulty"] == "hard"
        assert sum("laser" in lane for lane in hard["lanes"]) == 6
        assert has_timed_route(hard)
    # Degenerate RNG must still yield a vetted, solvable fallback.
    monkeypatch.setattr("tg_drop_bot.services.frog.secrets.randbelow", lambda n: 0)
    assert has_timed_route(new_game({}, 1, 1000, difficulty="hard"))


def test_laser_warning_active_segment_and_collision_between_requests():
    from tg_drop_bot.services.frog import laser_active

    game = safe_game()
    lane = game["lanes"][0]
    lane.update(
        phase=6,
        speed=-0.001,
        laser={
            "period": 3,
            "warning": 0.6,
            "duration": 0.75,
            "length": 2,
            "offset": 0,
        },
    )
    assert not laser_active(lane, 0.59)
    assert laser_active(lane, 0.6)
    assert not laser_active(lane, 1.35)
    assert not collision(game, 3, 1, 1000.59)
    assert collision(game, 3, 1, 1000.7)
    assert not collision(game, 1, 1, 1000.7)
    game["y"] = 1
    # The laser fires and stops between these requests; it must still hit.
    advance(game, "wait", 0, 1002)
    assert game["status"] == "lost"


def test_route_search_keeps_overlapping_hazards_and_does_not_mutate_game():
    from copy import deepcopy

    from tg_drop_bot.services.frog import has_timed_route

    game = safe_game()
    original = deepcopy(game)
    assert has_timed_route(game)
    assert game == original
    # Two hazards on one row must be combined, not overwritten in a row index.
    game["lanes"].append(game["lanes"][0] | {"phase": 4, "speed": 0})
    assert not has_timed_route(game)
    game["lanes"].pop()
    assert has_timed_route(game)
