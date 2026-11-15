import json
import time
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_frog import TOKEN, signed
from test_frog_api import MemoryRedis

from tg_drop_bot.bot.free_game import open_free_game
from tg_drop_bot.config import Settings
from tg_drop_bot.services import free_game_web


class TransientRedis(MemoryRedis):
    def __init__(self):
        super().__init__()
        self.ttls = {}

    async def set(self, k, v, **kw):
        self.ttls[k] = kw.get('ex')
        await super().set(k, v, **kw)

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


async def test_free_game_needs_no_database_and_erases_finished_state(monkeypatch):
    store = TransientRedis()
    monkeypatch.setattr(free_game_web.Redis, 'from_url', lambda *a, **kw: store)
    clock = [time.time()]
    monkeypatch.setattr(free_game_web.time, 'time', lambda: clock[0])
    app = web.Application()
    free_game_web.register_free_game_routes(app, Settings(BOT_TOKEN=TOKEN))
    async with TestClient(TestServer(app)) as client:
        init = signed(at=int(clock[0]))
        async def post(action, **data):
            return await client.post('/play/api/'+action, json={'initData': init, **data})

        response = await client.get('/play/')
        assert response.status == 200
        assert '/play/assets/app.js' in await response.text()
        response = await post('start')
        assert response.status == 200
        state = await response.json()
        key = 'freefrog:session:'+state['token']
        game = json.loads(store.data[key])
        assert not {'user', 'giveaway_id', 'moves', 'audit_id'} & game.keys()
        assert store.ttls[key] <= 300
        assert len(game['owner']) == 64
        # Reusing someone else's token cannot authenticate another account.
        assert (await client.post('/play/api/move', json={
            'initData': signed(456, int(clock[0])), 'token': state['token'],
            'nonce': state['nonce'], 'seq': 0, 'direction': 'up',
        })).status == 400
        assert (await post('move', token=state['token'], nonce='fake',
                           seq=0, direction='up')).status == 400
        for lane in game['lanes']:
            lane.update(phase=0, speed=0.45)
        store.data[key] = json.dumps(game)
        for _ in range(game['rows']-1):
            clock[0] += 0.3
            response = await post('move', token=state['token'], nonce=state['nonce'],
                                  seq=state['seq'], direction='up')
            assert response.status == 200
            state = await response.json()
            if state['status'] == 'playing':
                assert 'moves' not in json.loads(store.data[key])
        assert state['status'] == 'won'
        assert key not in store.data
        assert not any(k.startswith('freefrog:current:') for k in store.data)
        assert (await post('move', token=state['token'], nonce=state['nonce'],
                           seq=state['seq'], direction='up')).status == 400


async def test_game_buttons_in_private_and_group():
    settings = Settings(WEBHOOK_URL='https://example.com')
    bot = AsyncMock()
    bot.get_me.return_value.username = 'dropbot_dpi_bot'
    private = AsyncMock()
    private.chat.type = 'private'
    await open_free_game(private, settings, bot)
    button = private.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.web_app.url == 'https://example.com/play/'
    group = AsyncMock()
    group.chat.type = 'supergroup'
    await open_free_game(group, settings, bot)
    button = group.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.url == 'https://t.me/dropbot_dpi_bot?start=game'
    assert button.web_app is None
