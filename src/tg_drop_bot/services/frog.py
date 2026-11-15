"""Server-authoritative, deliberately easy road crossing for the test bot."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from urllib.parse import parse_qsl


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


def new_game(user: dict, giveaway_id: int, now: float) -> dict:
    first, second = secrets.randbelow(3) + 1, secrets.randbelow(3) + 1
    middle = first + 1
    roads = [*range(1, middle), *range(middle + 1, middle + 1 + second)]
    return {
        "user": user, "giveaway_id": giveaway_id, "started": now, "checked": now,
        "last_move": now - 1, "x": 3, "y": 0, "seq": 0, "status": "playing", "moves": [],
        "rows": first + second + 3,
        "lanes": [
            {"row": row, "phase": secrets.randbelow(900) / 100,
             "kind": "car",
             "speed": (0.45 + secrets.randbelow(20) / 100) * (-1 if row % 2 else 1)}
            for row in roads
        ] + [{"row": middle, "phase": secrets.randbelow(900) / 100,
              "kind": "mower", "speed": 0.32 * (-1 if secrets.randbelow(2) else 1)}],
    }


def collision(game: dict, x: int, y: int, at: float) -> bool:
    for lane in game["lanes"]:
        if lane["row"] == y:
            center = (lane["phase"] + lane["speed"] * (at - game["started"])) % 9 - 1
            margin = 0.65 if lane.get("kind") == "mower" else 0.88
            if abs(center - x) < margin:
                return True
    return False


def advance(game: dict, direction: str, seq: int, now: float) -> dict:
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
    if now - game["last_move"] < 0.22:
        return game
    finish = game.get("rows", 8) - 1
    game["y"] = min(finish, game["y"] + 1)
    game["last_move"] = now
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
        "now": now, "rows": game.get("rows", 8),
    }
