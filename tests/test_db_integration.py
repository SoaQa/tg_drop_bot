import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_drop_bot.db.models import KnownChannel

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_postgres_schema_accepts_known_channel() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        channel = KnownChannel(
            telegram_chat_id=-100123,
            title="Test channel",
            username="test_channel",
            is_active=True,
            bot_is_admin=True,
        )
        session.add(channel)
        await session.commit()
        result = await session.execute(
            select(KnownChannel).where(KnownChannel.telegram_chat_id == -100123)
        )
        assert result.scalar_one().title == "Test channel"
        await session.delete(channel)
        await session.commit()
    await engine.dispose()


@pytest.mark.asyncio
async def test_finish_claim_is_atomic_against_double_finish() -> None:
    from datetime import timedelta

    from tg_drop_bot.db.models import Giveaway
    from tg_drop_bot.services.dates import utc_now
    from tg_drop_bot.services.giveaways import try_status_transition

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        giveaway = Giveaway(
            status="published",
            creator_user_id=1,
            title="Гонка завершения",
            winners_count=1,
            deadline_at=utc_now() - timedelta(minutes=1),
        )
        session.add(giveaway)
        await session.commit()

        first = await try_status_transition(
            session,
            giveaway,
            expected_status="published",
            status="finished",
            finished_at=utc_now(),
            results_state="pending",
        )
        await session.commit()
        assert first is True
        assert giveaway.status == "finished"

        # Второй "работник" (например, планировщик против ручного завершения)
        # уже не может захватить розыгрыш повторно.
        async with session_factory() as other_session:
            other = await other_session.get(Giveaway, giveaway.id)
            assert other is not None
            second = await try_status_transition(
                other_session,
                other,
                expected_status="published",
                status="finished",
                finished_at=utc_now(),
                results_state="pending",
            )
            assert second is False
            await other_session.rollback()

        await session.delete(giveaway)
        await session.commit()
    await engine.dispose()
