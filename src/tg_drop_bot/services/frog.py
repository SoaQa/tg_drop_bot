"""Server-authoritative road crossing with vetted traffic layouts."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from urllib.parse import parse_qsl

# All traffic repeats after 20 seconds. Two cars per road lane leave readable gaps.
TRAFFIC_PERIOD = 20.0
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


def _rush_band_blocked(lanes: list[dict], low: float, high: float) -> bool:
    """Cover every possible starting delay for every constant hop interval in a band.

    A frog occupies row r from delay+(r-1)*hop to delay+r*hop. Intersect the
    forbidden delay intervals over the entire hop band, then union all lanes.
    Narrower collision margins make coverage conservative, including boundaries.
    """
    cycle = 9 / abs(next(lane for lane in lanes if lane['kind'] == 'mower')['speed'])
    intervals = []
    for lane in lanes:
        speed = abs(lane['speed'])
        period = 9 / (lane['count'] * speed)
        margin = (0.65 if lane['kind'] == 'mower' else 0.88) - 0.02
        crossing = ((4 - lane['phase']) / lane['speed']) % period
        left = crossing - margin / speed - lane['row'] * low
        right = crossing + margin / speed - (lane['row'] - 1) * high
        for k in range(math.ceil(-right / period), math.floor((cycle-left)/period)+1):
            intervals.append((max(0, left+k*period), min(cycle, right+k*period)))
    covered = 0.0
    for left, right in sorted(intervals):
        if left > covered:
            return False
        covered = max(covered, right)
    return covered >= cycle


def blocks_blind_rushes(lanes: list[dict], *, minimum_hop: float = 0.28) -> bool:
    car = next(lane for lane in lanes if lane['kind'] == 'car')
    longest_gap = (9/car['count']-2*0.86)/abs(car['speed'])
    bands = math.ceil((longest_gap-minimum_hop)/0.02)
    return all(_rush_band_blocked(lanes, minimum_hop+i*0.02, minimum_hop+(i+1)*0.02)
               for i in range(bands))


def validate_init_data(raw: str, token: str, now: float) -> dict:
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
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


def _easy_game(user: dict, giveaway_id: int, now: float, *, hard_layout: bool = False) -> dict:
    first = 3 if hard_layout else secrets.randbelow(3) + 1
    second = 3 if hard_layout else (
        secrets.randbelow(2) + 2 if first == 1 else secrets.randbelow(2) + 1 if first == 2 else 1
    )
    middle = first + 1
    roads = [*range(1, middle), *range(middle + 1, middle + 1 + second)]
    game: dict = {
        "user": user, "giveaway_id": giveaway_id, "started": now, "checked": now,
        "last_move": now - 1, "x": 3, "y": 0, "seq": 0, "status": "playing", "moves": [],
        "rows": first + second + 3,
        "difficulty": "easy",
        "lanes": [
            {"row": row, "phase": secrets.randbelow(900) / 100,
             "kind": "car",
             "count": 2, "speed": 0.9 * (-1 if row % 2 else 1)}
            for row in roads
        ] + [{"row": middle, "phase": secrets.randbelow(900) / 100,
              "kind": "mower", "count": 1,
              "speed": 0.45 * (-1 if secrets.randbelow(2) else 1)}],
    }
    for _ in range(64):
        for lane in game['lanes']:
            lane['phase'] = secrets.randbelow(900) / 100
        game['lanes'][0]['phase'] = 4 + (secrets.randbelow(31) - 15) / 100
        slowed = game | {'lanes': [lane | {'speed': lane['speed']*.75}
                                  for lane in game['lanes']]}
        if (blocks_blind_rushes(game['lanes'], minimum_hop=.21) and has_timed_route(game)
            and (hard_layout or has_timed_route(slowed))):
            break
    else:
        # Bounded generation, also when an RNG repeatedly produces the same easy layout.
        for lane, phase in zip(game['lanes'], FALLBACK_PHASES[first, second], strict=True):
            lane['phase'] = phase
        game['lanes'][-1]['speed'] = 0.45
    return game


def new_game(user: dict, giveaway_id: int, now: float, *, difficulty: str = 'easy') -> dict:
    if difficulty == 'easy':
        game = _easy_game(user, giveaway_id, now)
        for lane in game['lanes']:
            lane['speed'] *= 0.75
        return game
    if difficulty != 'hard':
        raise ValueError('invalid difficulty')
    for _ in range(8):
        game = _easy_game(user, giveaway_id, now, hard_layout=True)
        game['difficulty'] = 'hard'
        for lane in game['lanes']:
            lane['speed'] *= 4/3
            if lane['kind'] == 'car':
                lane['laser'] = {'length': 2.0, 'period': 3.0, 'warning': 0.6,
                                 'duration': 0.75, 'offset': secrets.randbelow(300)/100}
        if has_timed_route(game):
            return game
    # Validated with the same buffered 400 ms route search, no random retry loop.
    phases = (4.0, 7.14, 2.91, 1.98, 8.95, 2.29, 7.78)
    offsets = (2.48, 0.68, 0.7, 0.13, 0.87, 1.71)
    for i, lane in enumerate(game['lanes']):
        lane['phase'] = phases[i]
        if lane['kind'] == 'car':
            lane['laser']['offset'] = offsets[i]
        else:
            lane['speed'] = 0.6
    return game


def laser_active(lane: dict, elapsed: float, vehicle: int = 0) -> bool:
    laser = lane.get('laser')
    if not laser:
        return False
    phase = (elapsed + laser['offset'] + vehicle*0.4) % laser['period']
    return laser['warning'] <= phase < laser['warning']+laser['duration']


def collision(game: dict, x: int, y: int, at: float, *, padding: float = 0) -> bool:
    for lane in game["lanes"]:
        if lane["row"] == y:
            period = 9 / lane.get('count', 1)
            center = (lane["phase"] + lane["speed"] * (at - game["started"])) % period
            distance = abs(center - (x + 1) % period)
            distance = min(distance, period-distance)
            margin = (0.65 if lane.get("kind") == "mower" else 0.88) + padding
            if distance < margin:
                return True
            if lane.get('laser'):
                sign = 1 if lane['speed'] > 0 else -1
                for vehicle in range(lane.get('count', 1)):
                    if not laser_active(lane, at-game['started'], vehicle):
                        continue
                    car = (lane['phase']+9*vehicle/lane['count']
                           + lane['speed']*(at-game['started'])) % 9 - 1
                    near, far = car+sign*0.65, car+sign*(0.65+lane['laser']['length'])
                    if min(near, far)-0.25-padding < x < max(near, far)+0.25+padding:
                        return True
    return False


def has_timed_route(game: dict) -> bool:
    """Require a route within 30 seconds with 400 ms hops and a spatial safety buffer."""
    reachable = {0}
    finish = game['rows']-1
    for tick in range(1, 76):
        previous, now = game['started']+(tick-1)*0.4, game['started']+tick*0.4
        following = set()
        for row in reachable:
            if any(collision(game, 3, row, previous+i*0.04, padding=0.10) for i in range(11)):
                continue
            for target in (row, min(row+1, finish)):
                if not collision(game, 3, target, now, padding=0.10):
                    following.add(target)
        reachable = following
        if finish in reachable:
            return True
    return False


def advance(
    game: dict, direction: str, seq: int, now: float, *, record_moves: bool = True,
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
    if now - game["last_move"] < 0.28:
        return game
    finish = game.get("rows", 8) - 1
    game["y"] = min(finish, game["y"] + 1)
    game["last_move"] = now
    if record_moves:
        game["moves"].append({
            "t": round(now - game["started"], 3), "direction": direction,
            "x": game["x"], "y": game["y"],
        })
    if collision(game, game["x"], game["y"], now):
        game["status"] = "lost"
    elif game["y"] == finish:
        game["status"] = "won"
    return game


def public_game(game: dict, now: float) -> dict:
    return {k: game[k] for k in ("x", "y", "seq", "status", "lanes", "started")} | {
        "now": now, "rows": game.get("rows", 8), 'difficulty': game.get('difficulty', 'easy'),
    }
