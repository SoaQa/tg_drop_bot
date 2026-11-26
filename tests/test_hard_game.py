import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_drop_bot.db.models import Base, Giveaway, HardGameAttempt, KnownChannel, Participant
from tg_drop_bot.services.hard_game import (
    claim_hard_attempt,
    claim_hard_game_slot,
    hard_attempts_left,
    leaderboard_text,
    record_hard_completion,
)


@pytest.mark.skipif(not os.getenv('TEST_DATABASE_URL'), reason='PostgreSQL required')
async def test_unique_attempts_cumulative_count_and_chronological_finish_order():
    engine = create_async_engine(os.environ['TEST_DATABASE_URL'])
    schema = 'hard_game_' + uuid4().hex
    async with engine.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
        await c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await c.run_sync(Base.metadata.create_all)
    scoped = create_async_engine(os.environ['TEST_DATABASE_URL'], connect_args={
        'server_settings': {'search_path': schema},
    })
    sessions = async_sessionmaker(scoped, expire_on_commit=False)
    stamp = datetime(2026, 11, 15, 10, 0, 0, 123000, UTC)
    try:
        async with sessions.begin() as s:
            channel = KnownChannel(telegram_chat_id=-555, title='Test')
            s.add(channel)
            await s.flush()
            g1 = Giveaway(channel_id=channel.id, creator_user_id=1, status='published',
                          title='Розыгрыш <1>')
            g2 = Giveaway(channel_id=channel.id, creator_user_id=1, status='finished')
            s.add_all([g1, g2])
            await s.flush()
            gid1, gid2 = g1.id, g2.id
            for gid, uid, name in [(gid1, 10, 'early'), (gid1, 20, 'fast'), (gid2, 10, 'early')]:
                s.add(Participant(giveaway_id=gid, user_id=uid, username=name,
                                  joined_at=stamp, captcha_passed_at=stamp))

        async def claim(index):
            async with sessions.begin() as s:
                return index, await claim_hard_attempt(s, gid1, 10, str(index))

        results = await asyncio.gather(*(claim(i) for i in range(12)))
        winners = [str(i) for i, won in results if won]
        assert len(winners) == 1
        async with sessions.begin() as s:
            await record_hard_completion(s, gid1, 10, 'forged', 1, stamp)
            record = await s.get(HardGameAttempt, (gid1, 10))
            assert record is not None and record.completed_at is None
            await record_hard_completion(s, gid1, 10, winners[0], 9000, stamp)
            # A replay cannot change the duration or finish timestamp.
            await record_hard_completion(s, gid1, 10, winners[0], 1, stamp-timedelta(days=1))
            assert await claim_hard_attempt(s, gid2, 10, 'next-draw')
            await record_hard_completion(s, gid2, 10, 'next-draw', 5000, stamp+timedelta(days=1))
            assert await claim_hard_attempt(s, gid1, 20, 'other')
            await record_hard_completion(s, gid1, 20, 'other', 1000, stamp+timedelta(seconds=1))
            # Unfinished/abandoned attempts do not count in the score.
            assert await claim_hard_attempt(s, gid1, 30, 'unfinished')
            assert await claim_hard_game_slot(s, gid1, 30, 'second') == 1
            assert await claim_hard_game_slot(s, gid1, 30, 'third') == 0
            assert await claim_hard_game_slot(s, gid1, 30, 'fourth') is None
            assert await claim_hard_game_slot(s, gid1, 10, 'after-win') is None

        async with sessions() as s:
            record = await s.get(HardGameAttempt, (gid1, 10))
            assert record is not None and record.duration_ms == 9000
            assert record.completed_at == stamp
            page = await leaderboard_text(s)
            current, total = page.split('<b>Всего')
            assert current.index('@early') < current.index('@fast')
            assert '13:00:00.123' in current and '13:00:01.123' in current
            assert '9.00 сек.' in current and '1.00 сек.' in current
            assert '@early: 2' in total and '@fast: 1' in total
            assert 'Розыгрыш &lt;1&gt;' in current
            assert '30' not in total
            assert 'нет розыгрышей' in await leaderboard_text(s, -999)
            assert (await s.scalars(select(HardGameAttempt))).all()
        async with sessions.begin() as s:
            legacy_win = await s.get(HardGameAttempt, (gid1, 10))
            assert legacy_win is not None
            first_key, first_start = legacy_win.attempt_key, legacy_win.started_at
            assert hard_attempts_left(legacy_win) == 0
            assert hard_attempts_left(legacy_win, reopening=True) == 3
            for remaining in (2, 1, 0):
                new_key = f"replay-{remaining}"
                assert await claim_hard_game_slot(s, gid1, 10, new_key, reopening=True) == remaining
                await record_hard_completion(s, gid1, 10, new_key, 6000, stamp+timedelta(days=1))
            assert await claim_hard_game_slot(s, gid1, 10, 'fourth', reopening=True) is None
            assert await claim_hard_game_slot(s, gid1, 30, 'interrupted', reopening=True) == 2
            await s.refresh(legacy_win)
            assert legacy_win.attempt_key == first_key and legacy_win.started_at == first_start
            assert legacy_win.completed_at == stamp and legacy_win.duration_ms == 9000
            assert hard_attempts_left(legacy_win, reopening=True) == 0
    finally:
        await scoped.dispose()
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def test_top_in_private_and_group_preserves_context(monkeypatch):
    from tg_drop_bot.bot.free_game import show_top
    from tg_drop_bot.services import hard_game

    lookup = AsyncMock(return_value='Топ')
    monkeypatch.setattr(hard_game, 'leaderboard_text', lookup)
    session = AsyncMock()
    for kind, chat_id in [('private', 1), ('supergroup', -555)]:
        message = AsyncMock()
        message.chat.type, message.chat.id = kind, chat_id
        await show_top(message, session)
        lookup.assert_awaited_with(
            session, chat_id if kind != 'private' else None, mode_selection=False,
        )
        message.answer.assert_awaited_once_with('Топ')
