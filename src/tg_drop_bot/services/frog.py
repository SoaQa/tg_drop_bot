"""Server-authoritative road crossing with vetted traffic layouts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from urllib.parse import parse_qsl

TRACK_LENGTH = 9.0
# With two cars of collision radius 0.88, this increases the clear gap by 15%.
RELAXED_TRACK_LENGTH = 9.82
CAR_RADIUS = 0.88
MOWER_RADIUS = 0.65
HOP_COOLDOWN = 0.28
FALLBACK_PHASES = {
    (1, 2): [4, 3.07, 1.82, 6.12],
    (1, 3): [4, 7.89, 4.96, 6.63, 6.24],
    (2, 1): [4, 5.4, 1.58, 4.48],
    (2, 2): [4, 0.95, 6.06, 5.98, 1.66],
    (2, 3): [4, 4.05, 0.05, 3.85, 1.11, 3.8],
    (3, 1): [4, 7.87, 7.45, 2.06, 0.33],
    (3, 2): [4, 7.32, 8.99, 3.95, 5.43, 0.62],
    (3, 3): [4, 5.28, 4.32, 1.49, 0.01, 1.13, 4.99],
}
RELAXED_FALLBACK_PHASES = [4.05, 0.42, 5.87, 6.17, 0.0]


def _rush_band_blocked(
    crossings: list[tuple[int, float, float, float]], cycle: float, low: float, high: float
) -> bool:
    """Cover every possible starting delay for every constant hop interval in a band.

    A frog occupies row r from delay+(r-1)*hop to delay+r*hop. Intersect the
    forbidden delay intervals over the entire hop band, then union all lanes.
    Narrower collision margins make coverage conservative, including boundaries.
    """
    intervals = []
    for row, period, crossing, half_width in crossings:
        left = crossing - half_width - row * low
        right = crossing + half_width - (row - 1) * high
        for k in range(math.ceil(-right / period), math.floor((cycle - left) / period) + 1):
            intervals.append((max(0, left + k * period), min(cycle, right + k * period)))
    covered = 0.0
    for left, right in sorted(intervals):
        if left > covered:
            return False
        covered = max(covered, right)
    return covered >= cycle


def blocks_blind_rushes(lanes: list[dict], *, minimum_hop: float = 0.28) -> bool:
    crossings = []
    longest_gap = math.inf
    for lane in lanes:
        speed = abs(lane["speed"])
        spacing = lane.get("trackLength", TRACK_LENGTH) / lane.get("count", 1)
        period = spacing / speed
        mower = lane["kind"] == "mower"
        margin = (MOWER_RADIUS if mower else CAR_RADIUS) - 0.02
        crossing = ((4 - lane["phase"]) / lane["speed"]) % period
        crossings.append((lane["row"], period, crossing, margin / speed))
        if not mower:
            longest_gap = min(longest_gap, (spacing - 2 * margin) / speed)
    # Generated car lanes share a period; the optional mower takes four such periods.
    cycle = max(period for _, period, _, _ in crossings)
    bands = math.ceil((longest_gap - minimum_hop) / 0.02)
    return all(
        _rush_band_blocked(crossings, cycle, minimum_hop + i * 0.02, minimum_hop + (i + 1) * 0.02)
        for i in range(bands)
    )


def validate_init_data(raw: str, token: str, now: float) -> dict:
    if not isinstance(raw, str) or len(raw) > 16_384:
        raise ValueError("invalid authentication size")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate fields")
    signature = values.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    key = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
    expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid signature")
    if not 0 <= now - int(values["auth_date"]) <= 3600:
        raise ValueError("expired authentication")
    user = json.loads(values["user"])
    if type(user.get("id")) is not int or user["id"] <= 0:
        raise ValueError("invalid identity")
    return user


def _easy_game(
    user: dict,
    giveaway_id: int,
    now: float,
    *,
    hard_layout: bool = False,
    relaxed: bool = False,
    assisted: bool = False,
    from_checkpoint: bool = False,
) -> dict:
    if hard_layout:
        first = second = 3
    elif relaxed:
        first = second = 2
    else:
        first = secrets.randbelow(3) + 1
        second = (
            secrets.randbelow(2) + 2
            if first == 1
            else secrets.randbelow(2) + 1
            if first == 2
            else 1
        )
    middle = first + 1
    roads = [*range(1, middle), *range(middle + 1, middle + 1 + second)]
    phase_steps = round((RELAXED_TRACK_LENGTH if relaxed else TRACK_LENGTH) * 100)
    game: dict = {
        "user": user,
        "giveaway_id": giveaway_id,
        "started": now,
        "checked": now,
        "last_move": now - 1,
        "x": 3,
        "y": middle if from_checkpoint else 0,
        "seq": 0,
        "status": "playing",
        "moves": [],
        "rows": first + second + 3,
        "difficulty": "easy",
        "lanes": [
            {
                "row": row,
                "phase": secrets.randbelow(phase_steps) / 100,
                "kind": "car",
                "count": 2,
                "speed": 0.9 * (-1 if row % 2 else 1),
            }
            for row in roads
        ],
    }
    if not assisted:
        game["lanes"].append(
            {
                "row": middle,
                "phase": secrets.randbelow(phase_steps) / 100,
                "kind": "mower",
                "count": 1,
                "speed": 0.45 * (-1 if secrets.randbelow(2) else 1),
            }
        )
    if relaxed:
        game.update(checkpoint_row=middle, checkpoint_reached=from_checkpoint)
        for lane in game["lanes"]:
            lane["trackLength"] = RELAXED_TRACK_LENGTH
    phases = RELAXED_FALLBACK_PHASES if relaxed else FALLBACK_PHASES[first, second]
    if assisted:
        phases = phases[:-1]
    for attempt in range(64):
        # Wider gaps rarely pass the blind-rush check on short roads with uniform phases.
        nearby = relaxed and attempt % 2 == 1
        jitter = 20 if attempt < 32 else 10
        for lane, phase in zip(game["lanes"], phases, strict=True):
            if nearby and lane["kind"] == "car":
                offset = (secrets.randbelow(2 * jitter + 1) - jitter) / 100
                lane["phase"] = (phase + offset) % RELAXED_TRACK_LENGTH
            else:
                lane["phase"] = secrets.randbelow(phase_steps) / 100
        if not nearby:
            game["lanes"][0]["phase"] = 4 + (secrets.randbelow(31) - 15) / 100
        if from_checkpoint and not assisted:
            # Keep the mower half a loop away from the checkpoint at the start.
            game["lanes"][-1]["phase"] = (
                game["x"] + 1 + RELAXED_TRACK_LENGTH / 2
                + (secrets.randbelow(41) - 20) / 100
            ) % RELAXED_TRACK_LENGTH
        slowed = game | {
            "lanes": [lane | {"speed": lane["speed"] * 0.75} for lane in game["lanes"]]
        }
        layout = slowed if relaxed else game
        if (
            blocks_blind_rushes(layout["lanes"], minimum_hop=HOP_COOLDOWN if relaxed else 0.21)
            and has_timed_route(layout)
            and (relaxed or hard_layout or has_timed_route(slowed))
            and (not from_checkpoint or has_timed_route(layout, start_row=middle))
        ):
            break
    else:
        # Bounded generation, also when an RNG repeatedly produces the same easy layout.
        for lane, phase in zip(game["lanes"], phases, strict=True):
            lane["phase"] = phase
        if not assisted:
            game["lanes"][-1]["speed"] = 0.45
    return game


def new_game(
    user: dict,
    giveaway_id: int,
    now: float,
    *,
    difficulty: str = "easy",
    relaxed: bool = False,
    assisted: bool = False,
    from_checkpoint: bool = False,
) -> dict:
    """Opt-in easy traffic uses a wider gap, with a safe median when assisted."""
    if difficulty == "easy":
        game = _easy_game(
            user, giveaway_id, now, relaxed=relaxed, assisted=relaxed and assisted,
            from_checkpoint=relaxed and from_checkpoint,
        )
        for lane in game["lanes"]:
            lane["speed"] *= 0.75
        return game
    if difficulty != "hard":
        raise ValueError("invalid difficulty")
    for _ in range(8):
        game = _easy_game(user, giveaway_id, now, hard_layout=True)
        game["difficulty"] = "hard"
        for lane in game["lanes"]:
            lane["speed"] *= 4 / 3
            if lane["kind"] == "car":
                lane["laser"] = {
                    "length": 2.0,
                    "period": 3.0,
                    "warning": 0.6,
                    "duration": 0.75,
                    "offset": secrets.randbelow(300) / 100,
                }
        if has_timed_route(game):
            return game
    # Validated with the same buffered 400 ms route search, no random retry loop.
    phases = (4.0, 7.14, 2.91, 1.98, 8.95, 2.29, 7.78)
    offsets = (2.48, 0.68, 0.7, 0.13, 0.87, 1.71)
    for i, lane in enumerate(game["lanes"]):
        lane["phase"] = phases[i]
        if lane["kind"] == "car":
            lane["laser"]["offset"] = offsets[i]
        else:
            lane["speed"] = 0.6
    return game


def laser_active(lane: dict, elapsed: float, vehicle: int = 0) -> bool:
    laser = lane.get("laser")
    if not laser:
        return False
    phase = (elapsed + laser["offset"] + vehicle * 0.4) % laser["period"]
    return laser["warning"] <= phase < laser["warning"] + laser["duration"]


def _lane_collision(lane: dict, x: int, elapsed: float, padding: float) -> bool:
    track_length = lane.get("trackLength", TRACK_LENGTH)
    count = lane.get("count", 1)
    spacing = track_length / count
    travel = lane["speed"] * elapsed
    position = lane["phase"] + travel
    distance = abs(position % spacing - (x + 1) % spacing)
    distance = min(distance, spacing - distance)
    margin = (MOWER_RADIUS if lane.get("kind") == "mower" else CAR_RADIUS) + padding
    if distance < margin:
        return True
    if lane.get("laser"):
        sign = 1 if lane["speed"] > 0 else -1
        for vehicle in range(count):
            if not laser_active(lane, elapsed, vehicle):
                continue
            car = (lane["phase"] + spacing * vehicle + travel) % track_length - 1
            near, far = car + sign * 0.65, car + sign * (0.65 + lane["laser"]["length"])
            if min(near, far) - 0.25 - padding < x < max(near, far) + 0.25 + padding:
                return True
    return False


def collision(game: dict, x: int, y: int, at: float, *, padding: float = 0) -> bool:
    elapsed = at - game["started"]
    for lane in game["lanes"]:
        if lane["row"] == y and _lane_collision(lane, x, elapsed, padding):
            return True
    return False


def has_timed_route(game: dict, *, start_row: int = 0) -> bool:
    """Require a route within 30 seconds with 400 ms hops and a spatial safety buffer."""
    lanes_by_row: dict[int, list[dict]] = {}
    for lane in game["lanes"]:
        lanes_by_row.setdefault(lane["row"], []).append(lane)

    def row_collision(row: int, at: float) -> bool:
        elapsed = at - game["started"]
        for lane in lanes_by_row.get(row, ()):  # noqa: SIM110 - hot loop avoids a generator
            if _lane_collision(lane, 3, elapsed, 0.10):
                return True
        return False

    reachable = {start_row}
    finish = game["rows"] - 1
    for tick in range(1, 76):
        previous, now = game["started"] + (tick - 1) * 0.4, game["started"] + tick * 0.4
        following = set()
        # A destination can be checked by both a waiting and a jumping path.
        # Cache only this exact timestamp, leaving swept collision samples unchanged.
        at_end: dict[int, bool] = {}
        for row in reachable:
            if row in lanes_by_row and any(
                row_collision(row, previous + i * 0.04) for i in range(11)
            ):
                continue
            for target in (row, min(row + 1, finish)):
                if target not in at_end:
                    at_end[target] = row_collision(target, now)
                if not at_end[target]:
                    if target == finish:
                        return True
                    following.add(target)
        reachable = following
    return False


def advance(
    game: dict,
    direction: str,
    seq: int,
    now: float,
    *,
    record_moves: bool = True,
) -> dict:
    if game["status"] != "playing":
        return game
    if type(seq) is not int or seq != game["seq"]:
        raise ValueError("stale sequence")
    if now < game["checked"] or now - game["started"] > 300:
        game["status"] = "expired"
        return game
    if direction not in ("up", "wait"):
        raise ValueError("invalid move")
    # Sample the elapsed interval too: a client cannot skip time while standing in traffic.
    steps = max(1, math.ceil((now - game["checked"]) / 0.04))
    for i in range(steps + 1):
        at = game["checked"] + (now - game["checked"]) * i / steps
        if collision(game, game["x"], game["y"], at):
            game["status"] = "lost"
            break
    game["checked"] = now
    game["seq"] += 1
    if game["status"] != "playing" or direction == "wait":
        return game
    if now < game["last_move"] + HOP_COOLDOWN:
        return game
    finish = game.get("rows", 8) - 1
    game["y"] = min(finish, game["y"] + 1)
    game["last_move"] = now
    if record_moves:
        game["moves"].append(
            {
                "t": round(now - game["started"], 3),
                "direction": direction,
                "x": game["x"],
                "y": game["y"],
            }
        )
    if collision(game, game["x"], game["y"], now):
        game["status"] = "lost"
    elif game["y"] == finish:
        game["status"] = "won"
    elif game["y"] == game.get("checkpoint_row"):
        game["checkpoint_reached"] = True
    return game


def public_game(game: dict, now: float) -> dict:
    return {k: game[k] for k in ("x", "y", "seq", "status", "lanes", "started")} | {
        "now": now if game["status"] == "playing" else game.get("checked", now),
        "rows": game.get("rows", 8),
        "difficulty": game.get("difficulty", "easy"),
    } | ({"checkpointReached": game["checkpoint_reached"]} if "checkpoint_row" in game else {})
