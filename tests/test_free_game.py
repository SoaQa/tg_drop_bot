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
        self.expires = {}

    async def set(self, k, v, **kw):
        self.ttls[k] = kw.get('px', kw.get('ex', 0)*1000)
        self.expires[k] = time.time() + self.ttls[k]/1000
        await super().set(k, v, **kw)

    async def get(self, k):
        if self.expires.get(k, float('inf')) <= time.time():
            await self.delete(k)
        return await super().get(k)

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
        assert 0 < store.ttls[key] <= 60_000
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


async def test_abandoned_game_expires_and_active_game_cannot_extend_two_minutes(monkeypatch):
    store = TransientRedis()
    monkeypatch.setattr(free_game_web.Redis, 'from_url', lambda *a, **kw: store)
    clock = [time.time()]
    monkeypatch.setattr(free_game_web.time, 'time', lambda: clock[0])
    app = web.Application()
    free_game_web.register_free_game_routes(app, Settings(BOT_TOKEN=TOKEN))
    async with TestClient(TestServer(app)) as client:
        init = signed(at=int(clock[0]))
        async def start():
            r = await client.post('/play/api/start', json={'initData': init})
            assert r.status == 200
            return await r.json()

        async def move(state):
            return await client.post('/play/api/move', json={
                'initData': init, 'token': state['token'], 'nonce': state['nonce'],
                'seq': state['seq'], 'direction': 'wait',
            })

        state = await start()
        key = 'freefrog:session:'+state['token']
        current = next(k for k in store.data if k.startswith('freefrog:current:'))
        # Browser closed: no request arrives for one minute.
        clock[0] += 60
        assert await store.get(key) is None
        assert await store.get(current) is None
        assert (await move(state)).status == 400

        state = await start()
        key = 'freefrog:session:'+state['token']
        started = clock[0]
        # The browser keeps polling while the player never moves.
        for seconds in (40, 80, 110, 119):
            clock[0] = started+seconds
            response = await move(state)
            assert response.status == 200
            state = await response.json()
            assert store.expires[key] <= started+120
            assert store.expires[current] <= started+120
            assert store.ttls[key] <= 60_000
        clock[0] = started+120
        assert await store.get(key) is None
        assert await store.get(current) is None
        assert (await move(state)).status == 400

        # Server rejects an over-time session even if its Redis TTL was accidentally extended.
        state = await start()
        key = 'freefrog:session:'+state['token']
        game = json.loads(store.data[key])
        game['started'] = clock[0]-120
        store.data[key] = json.dumps(game)
        response = await move(state)
        assert response.status == 200
        assert (await response.json())['status'] == 'expired'
        assert key not in store.data and current not in store.data


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


async def test_game_commands_preserve_pending_captcha(monkeypatch):
    from aiogram import Bot, Dispatcher
    from aiogram.types import Chat, Message, MessageEntity, Update, User

    from tg_drop_bot.bot.free_game import router
    from tg_drop_bot.services.dates import utc_now

    bot = Bot(TOKEN)
    bot.session = AsyncMock()
    monkeypatch.setattr(bot, 'get_me', AsyncMock(return_value=User(
        id=123456, is_bot=True, first_name='Bot', username='dropbot_dpi_bot',
    )))
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=123, user_id=123)
    await state.set_state('CaptchaStates:answer')
    for i, (text, chat_id, kind) in enumerate([
        ('/game', 123, 'private'), ('/start game', 123, 'private'),
        ('/game@dropbot_dpi_bot', -333, 'supergroup'),
    ], 1):
        update = Update(update_id=i, message=Message(
            message_id=i, date=utc_now(), chat=Chat(id=chat_id, type=kind),
            from_user=User(id=123, first_name='User', is_bot=False), text=text,
            entities=[MessageEntity(type='bot_command', offset=0, length=len(text.split()[0]))],
        ))
        await dispatcher.feed_update(bot, update, settings=Settings(WEBHOOK_URL='https://example.com'))
    assert bot.session.await_count == 3
    assert await state.get_state() == 'CaptchaStates:answer'
    await dispatcher.storage.close()
