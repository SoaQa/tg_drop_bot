import os

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tg_drop_bot.db.models import KnownGroup

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration tests",
)


@pytest.mark.asyncio
async def test_postgres_schema_accepts_known_group() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        group = KnownGroup(
            telegram_chat_id=-100123,
            title="Test group",
            username="test_group",
            is_active=True,
            bot_is_admin=True,
        )
        session.add(group)
        await session.commit()
        result = await session.execute(
            select(KnownGroup).where(KnownGroup.telegram_chat_id == -100123)
        )
        assert result.scalar_one().title == "Test group"
        await session.delete(group)
        await session.commit()
    await engine.dispose()
