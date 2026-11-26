import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from redis.asyncio import Redis

from tg_drop_bot.services import game_runtime


def bounded_body(size: int) -> bytes:
    base = json.dumps({"initData": "synthetic", "padding": ""}, separators=(",", ":")).encode()
    return base[:-2] + b"x" * (size - len(base)) + base[-2:]


def request_client() -> TestClient:
    async def read(request: web.Request) -> web.Response:
        try:
            data = await game_runtime.read_game_request(request)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest() from None
        return web.json_response(
            {"initData": data["initData"], "chunked": request.content_length is None}
        )

    app = web.Application(client_max_size=game_runtime.MAX_GAME_BODY * 2)
    app.router.add_post("/read", read)
    return TestClient(TestServer(app))


async def test_json_body_accepts_exact_byte_limit_and_rejects_declared_overflow():
    async with request_client() as client:
        response = await client.post("/read", data=bounded_body(game_runtime.MAX_GAME_BODY))
        assert response.status == 200
        assert await response.json() == {"initData": "synthetic", "chunked": False}
        response = await client.post("/read", data=bounded_body(game_runtime.MAX_GAME_BODY + 1))
        assert response.status == 413


async def test_json_body_enforces_limit_for_chunked_requests_without_content_length():
    async def chunks(payload: bytes):
        for at in range(0, len(payload), 3072):
            yield payload[at : at + 3072]
            await asyncio.sleep(0)

    async with request_client() as client:
        response = await client.post("/read", data=chunks(bounded_body(8192)))
        assert response.status == 200
        assert (await response.json())["chunked"] is True
        response = await client.post(
            "/read",
            data=chunks(
                bounded_body(game_runtime.MAX_GAME_BODY + 1),
            ),
        )
        assert response.status == 413


async def test_oversized_declared_body_is_rejected_before_stream_consumption():
    unread = Mock()
    unread.iter_chunked.side_effect = AssertionError("Oversized body was read")
    request = cast(
        web.Request,
        SimpleNamespace(
            content_length=game_runtime.MAX_GAME_BODY + 1,
            content=unread,
        ),
    )
    with pytest.raises(web.HTTPRequestEntityTooLarge):
        await game_runtime.read_game_request(request)
    unread.iter_chunked.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        b"null",
        b"[]",
        b"42",
        b"{broken",
        b'{"initData":42}',
        b'{"initData":true}',
        b'{"missing":"initData"}',
        b'{"initData":"\xff"}',
    ],
)
async def test_invalid_json_and_wrong_init_data_types_are_bad_requests(payload):
    async with request_client() as client:
        response = await client.post("/read", data=payload)
        assert response.status == 400


class RecordingRedis:
    def __init__(self):
        self.data: dict[str, Any] = {}
        self.deadlines: dict[str, float] = {}
        self.clock = 0.0
        self.transactions: list[bool] = []
        self.round_trips = 0

    def pipeline(self, *, transaction):
        self.transactions.append(transaction)
        return RecordingPipeline(self)


class RecordingPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.pending = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def incr(self, key):
        self.pending.append(("incr", key))
        return self

    def expire(self, key, ttl, *, nx=False):
        self.pending.append(("expire", key, ttl, nx))
        return self

    def set(self, key, value, *, px):
        self.pending.append(("set", key, value, px))
        return self

    async def execute(self):
        self.redis.round_trips += 1
        results = []
        for operation in self.pending:
            name, key, *arguments = operation
            if name == "incr":
                self.redis.data[key] = self.redis.data.get(key, 0) + 1
                results.append(self.redis.data[key])
            elif name == "expire":
                ttl, nx = arguments
                if nx and key in self.redis.deadlines:
                    results.append(False)
                else:
                    self.redis.deadlines[key] = self.redis.clock + ttl
                    results.append(True)
            elif name == "set":
                value, ttl_ms = arguments
                self.redis.data[key] = value
                self.redis.deadlines[key] = self.redis.clock + ttl_ms / 1000
                results.append(True)
        return results


async def test_quota_is_one_transaction_per_call_and_does_not_extend_expiry():
    store = RecordingRedis()
    redis = cast(Redis, store)
    await game_runtime.quota(redis, "quota", 2, 20)
    assert store.round_trips == 1 and store.transactions == [True]
    assert store.deadlines["quota"] == 20
    store.clock = 7
    await game_runtime.quota(redis, "quota", 2, 20)
    assert store.data["quota"] == 2 and store.deadlines["quota"] == 20
    with pytest.raises(web.HTTPTooManyRequests) as failure:
        await game_runtime.quota(redis, "quota", 2, 20)
    assert failure.value.headers["Retry-After"] == "1"
    assert store.data["quota"] == 3 and store.deadlines["quota"] == 20
    assert store.round_trips == 3 and store.transactions == [True, True, True]


async def test_session_and_current_pointer_are_stored_with_one_matching_atomic_ttl():
    store = RecordingRedis()
    game = {"nonce": "synthetic-next", "seq": 1, "difficulty": "easy", "label": "Лягушка"}
    await game_runtime.store_game(
        cast(Redis, store),
        "current:fake",
        "session:fake",
        "synthetic-token",
        game,
        1550,
    )
    assert store.transactions == [True] and store.round_trips == 1
    assert store.data["current:fake"] == "synthetic-token"
    assert json.loads(store.data["session:fake"]) == game
    assert store.deadlines == {"current:fake": 1.55, "session:fake": 1.55}


def command_fixture(monkeypatch):
    calls = []

    def simulate(game, direction, seq, now, *, record_moves):
        calls.append((direction, seq, now, record_moves))
        game["seq"] += 1
        game["checked"] = now
        game["y"] += direction == "up"
        return game

    monkeypatch.setattr(game_runtime, "advance", simulate)
    return {"nonce": "original-nonce", "seq": 0, "checked": 0, "y": 0}, calls


def timed_hop_fixture(monkeypatch):
    clock, sleeps = [1000.3], []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(game_runtime, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(game_runtime.asyncio, "sleep", sleep)
    game = {
        "nonce": "first",
        "seq": 0,
        "started": 1000,
        "checked": 1000,
        "last_move": 999,
        "status": "playing",
        "x": 3,
        "y": 0,
        "rows": 6,
        "lanes": [],
        "moves": [],
    }
    return game, clock, sleeps


@pytest.mark.parametrize("record_moves", [True, False])
async def test_network_jitter_queues_an_early_hop_instead_of_dropping_it(monkeypatch, record_moves):
    game, clock, sleeps = timed_hop_fixture(monkeypatch)
    first = {"nonce": game["nonce"], "seq": 0, "direction": "up"}
    await game_runtime.execute_command(game, first, record_moves=record_moves)
    assert game["y"] == 1 and not sleeps
    # Sent 300 ms apart, but the first packet took 100 ms longer to arrive.
    clock[0] += 0.2
    second = {"nonce": game["nonce"], "seq": 1, "direction": "up"}
    await game_runtime.execute_command(game, second, record_moves=record_moves)
    assert game["y"] == 2 and game["seq"] == 2
    assert len(sleeps) == 1 and 0.08 < sleeps[0] < 0.082
    assert game["last_move"] - 1000.3 >= game_runtime.HOP_COOLDOWN
    assert len(game["moves"]) == (2 if record_moves else 0)
    original_time, nonce = game["checked"], game["nonce"]
    await game_runtime.execute_command(game, second, record_moves=record_moves)
    assert game["y"] == 2 and game["seq"] == 2 and game["nonce"] == nonce
    assert len(sleeps) == 1 and game["checked"] == original_time


@pytest.mark.parametrize("gap", [0, 0.279, 0.28, 0.3])
async def test_hop_handles_immediate_and_exact_cooldown_boundary_arrivals(monkeypatch, gap):
    game, clock, sleeps = timed_hop_fixture(monkeypatch)
    await game_runtime.execute_command(game, {"nonce": "first", "seq": 0, "direction": "up"})
    first_hop = game["last_move"]
    clock[0] = first_hop + gap
    await game_runtime.execute_command(
        game,
        {"nonce": game["nonce"], "seq": 1, "direction": "up"},
    )
    assert game["y"] == 2
    assert game["last_move"] >= first_hop + game_runtime.HOP_COOLDOWN
    assert bool(sleeps) == (gap < game_runtime.HOP_COOLDOWN)


@pytest.mark.parametrize("mutation", [{"seq": True}, {"seq": 7}, {"nonce": "wrong"}])
async def test_invalid_early_hop_cannot_wait_or_change_state(monkeypatch, mutation):
    game, clock, sleeps = timed_hop_fixture(monkeypatch)
    game["last_move"] = clock[0]
    data = {"nonce": game["nonce"], "seq": game["seq"], "direction": "up"} | mutation
    before = json.dumps(game, sort_keys=True)
    with pytest.raises(ValueError):
        await game_runtime.execute_command(game, data)
    assert not sleeps and json.dumps(game, sort_keys=True) == before


async def test_collision_during_cooldown_is_still_a_loss_not_an_extra_hop(monkeypatch):
    game, clock, sleeps = timed_hop_fixture(monkeypatch)
    clock[0] = 1001.01
    game.update(y=1, checked=clock[0], last_move=1001.0)
    game["lanes"] = [{"row": 1, "kind": "car", "phase": 2, "speed": 1, "count": 1}]
    await game_runtime.execute_command(game, {"nonce": "first", "seq": 0, "direction": "up"})
    assert sleeps and game["status"] == "lost" and game["y"] == 1
    assert game["seq"] == 1 and not game["moves"]


async def test_waiting_for_hop_cannot_extend_the_free_game_deadline(monkeypatch):
    game, clock, sleeps = timed_hop_fixture(monkeypatch)
    clock[0] = 1119.9
    game.update(y=4, checked=clock[0], last_move=1119.8)
    await game_runtime.execute_command(
        game,
        {"nonce": "first", "seq": 0, "direction": "up"},
        deadline=1120,
    )
    assert sleeps and game["status"] == "expired" and game["y"] == 4


async def test_retry_keeps_confirmed_finish_past_deadline(monkeypatch):
    game, clock, _ = timed_hop_fixture(monkeypatch)
    game["y"] = game["rows"] - 2
    final = {"nonce": game["nonce"], "seq": game["seq"], "direction": "up"}
    deadline = clock[0] + 1
    await game_runtime.execute_command(game, final, deadline=deadline)
    assert game["status"] == "won"
    recorded_time = game["checked"]
    clock[0] += 2
    await game_runtime.execute_command(game, final, deadline=deadline)
    assert game["status"] == "won" and game["checked"] == recorded_time


def test_exact_last_command_retry_returns_current_state_without_advancing(monkeypatch):
    game, calls = command_fixture(monkeypatch)
    request = {"nonce": "original-nonce", "seq": 0, "direction": "up"}
    game_runtime.command(game, request, 5, record_moves=False)
    nonce = game["nonce"]
    assert nonce != request["nonce"]
    assert game["last_command"]["nonce_hash"] == hashlib.sha256(b"original-nonce").hexdigest()
    assert game_runtime.command(game, request, 100, record_moves=True) is game
    assert game["nonce"] == nonce and game["seq"] == 1 and game["checked"] == 5 and game["y"] == 1
    assert calls == [("up", 0, 5, False)]


@pytest.mark.parametrize(
    "mutation",
    [
        {"nonce": "different-nonce"},
        {"seq": 1},
        {"direction": "wait"},
        {"seq": True},
        {"nonce": None},
        {"direction": "won"},
    ],
)
def test_mutated_replay_is_rejected_without_advancing(monkeypatch, mutation):
    game, calls = command_fixture(monkeypatch)
    original = {"nonce": "original-nonce", "seq": 0, "direction": "up"}
    game_runtime.command(game, original, 5)
    expected = json.dumps(game, sort_keys=True)
    with pytest.raises(ValueError):
        game_runtime.command(game, original | mutation, 6)
    assert json.dumps(game, sort_keys=True) == expected
    assert len(calls) == 1


def test_only_the_latest_command_can_be_retried_and_current_nonce_needs_current_seq(monkeypatch):
    game, calls = command_fixture(monkeypatch)
    first = {"nonce": "original-nonce", "seq": 0, "direction": "up"}
    game_runtime.command(game, first, 5)
    with pytest.raises(ValueError):
        game_runtime.command(game, {"nonce": game["nonce"], "seq": 0, "direction": "up"}, 6)
    second = {"nonce": game["nonce"], "seq": 1, "direction": "wait"}
    game_runtime.command(game, second, 7)
    with pytest.raises(ValueError):
        game_runtime.command(game, first, 8)
    game_runtime.command(game, second, 9)
    assert len(calls) == 2 and game["seq"] == 2 and game["checked"] == 7


async def test_generator_starts_clock_after_queue_wait_and_cpu_construction(monkeypatch):
    generator = game_runtime.GameGenerator()
    await generator.slots.acquire()
    await generator.slots.acquire()
    clock = [100.0]
    calls = []
    monkeypatch.setattr(game_runtime, "time", SimpleNamespace(time=lambda: clock[0]))

    def construct(user, gid, started, *, difficulty):
        calls.append((user, gid, started, difficulty))
        clock[0] = 250.0
        return {"started": 0, "checked": 0, "last_move": -1, "difficulty": difficulty}

    monkeypatch.setattr(game_runtime, "new_game", construct)
    task = asyncio.create_task(generator.generate({"id": 7}, 11, "hard"))
    await asyncio.sleep(0)
    assert not calls
    clock[0] = 200
    generator.slots.release()
    try:
        game = await asyncio.wait_for(task, timeout=1)
        assert game["started"] == game["checked"] == 250
        assert game["last_move"] == 249 and game["difficulty"] == "hard"
        assert len(calls) == 1
        assert calls[0][0] == {"id": 7} and calls[0][1] == 11 and calls[0][3] == "hard"
        assert not generator.slots.locked()
    finally:
        generator.slots.release()


async def test_generator_limits_parallel_cpu_jobs_and_releases_capacity(monkeypatch):
    generator = game_runtime.GameGenerator()
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = peak = started = 0

    def construct(*args, **kwargs):
        nonlocal active, peak, started
        with lock:
            active += 1
            peak = max(peak, active)
            started += 1
            if started == 2:
                two_started.set()
        try:
            release.wait(timeout=1)
            return {}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(game_runtime, "new_game", construct)
    tasks = [asyncio.create_task(generator.generate({}, 1, "easy")) for _ in range(4)]
    try:
        assert await asyncio.to_thread(two_started.wait, 0.5)
        assert started == 2 and peak == 2
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    assert len(results) == started == 4 and peak == 2 and active == 0


async def test_generator_saturation_returns_retryable_unavailable(monkeypatch):
    generator = game_runtime.GameGenerator()
    await generator.slots.acquire()
    await generator.slots.acquire()

    async def fast_wait(awaitable, *, timeout):
        assert timeout > 0
        return await asyncio.wait_for(awaitable, timeout=0.001)

    monkeypatch.setattr(game_runtime, "asyncio", SimpleNamespace(wait_for=fast_wait))
    construct = Mock(side_effect=AssertionError("CPU job started without a slot"))
    monkeypatch.setattr(game_runtime, "new_game", construct)
    try:
        with pytest.raises(web.HTTPServiceUnavailable) as failure:
            await generator.generate({}, 1, "hard")
        assert failure.value.headers["Retry-After"] == "1"
        construct.assert_not_called()
    finally:
        generator.slots.release()
        generator.slots.release()


async def test_generator_failure_does_not_leak_a_slot(monkeypatch):
    generator = game_runtime.GameGenerator()
    monkeypatch.setattr(
        game_runtime,
        "new_game",
        Mock(
            side_effect=RuntimeError("synthetic failure"),
        ),
    )
    with pytest.raises(RuntimeError, match="synthetic failure"):
        await generator.generate({}, 1, "easy")
    for _ in range(2):
        await asyncio.wait_for(generator.slots.acquire(), timeout=0.1)
    generator.slots.release()
    generator.slots.release()


def test_generator_is_shared_within_an_app_but_not_between_apps():
    first, second = web.Application(), web.Application()
    assert game_runtime.game_generator(first) is game_runtime.game_generator(first)
    assert game_runtime.game_generator(first) is not game_runtime.game_generator(second)


@pytest.mark.parametrize("background_failure", [False, True])
async def test_cancelled_request_keeps_cpu_slot_until_thread_really_finishes(
    monkeypatch,
    background_failure,
):
    generator = game_runtime.GameGenerator()
    # Reserve one slot, so the outstanding construction owns the last CPU permit.
    await generator.slots.acquire()
    entered, release = threading.Event(), threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def construct(*args, **kwargs):
        nonlocal calls
        with counter_lock:
            calls += 1
            number = calls
        if number == 1:
            entered.set()
            release.wait(timeout=2)
            if background_failure:
                raise RuntimeError("cancelled construction failed")
        return {}

    monkeypatch.setattr(game_runtime, "new_game", construct)
    first = asyncio.create_task(generator.generate({}, 1, "hard"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 0.5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert generator.slots.locked()
        second = asyncio.create_task(generator.generate({}, 1, "easy"))
        await asyncio.sleep(0.01)
        assert calls == 1, "Cancelled HTTP request released a still-running CPU slot"
        release.set()
        result = await asyncio.wait_for(second, timeout=1)
        assert calls == 2 and "started" in result
        assert not generator.slots.locked()
    finally:
        release.set()
        if not first.done():
            first.cancel()
        if second is not None and not second.done():
            second.cancel()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
        generator.slots.release()
