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
