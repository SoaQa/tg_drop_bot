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


async def test_captcha_cleanup_preserves_active_checks_and_cooldowns() -> None:
    from datetime import timedelta

    from sqlalchemy import delete

    from tg_drop_bot.db.models import CaptchaChallenge, Giveaway
    from tg_drop_bot.services.captcha import cleanup_captcha_challenges
    from tg_drop_bot.services.dates import utc_now

    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        now = utc_now()
        open_giveaway = Giveaway(
            creator_user_id=1, status="published", deadline_at=now + timedelta(hours=1)
        )
        closed_giveaway = Giveaway(creator_user_id=1, status="finished")
        session.add_all([open_giveaway, closed_giveaway])
        await session.flush()
        cases = [
            (open_giveaway.id, "pending", 300, None),
            (open_giveaway.id, "failed", -1, 60),
            (open_giveaway.id, "solved", 300, None),
            (open_giveaway.id, "expired", 300, None),
            (open_giveaway.id, "pending", -1, None),
            (open_giveaway.id, "failed", 300, -1),
            (closed_giveaway.id, "pending", 300, None),
        ]
        challenges = [
            CaptchaChallenge(
                giveaway_id=giveaway_id, user_id=100 + index, status=status,
                answer_hash="hash", expires_at=now + timedelta(seconds=ttl),
                cooldown_until=now + timedelta(seconds=cooldown) if cooldown else None,
            )
            for index, (giveaway_id, status, ttl, cooldown) in enumerate(cases)
        ]
        session.add_all(challenges)
        await session.commit()
        try:
            # Cleanup is bounded and idempotent, with no active state removed.
            deleted = 0
            while count := await cleanup_captcha_challenges(session, batch_size=2):
                deleted += count
                await session.commit()
            await session.commit()
            remaining = list((await session.scalars(
                select(CaptchaChallenge.id).where(
                    CaptchaChallenge.giveaway_id.in_([open_giveaway.id, closed_giveaway.id])
                )
            )).all())
            assert deleted == 5
            assert set(remaining) == {challenges[0].id, challenges[1].id}
        finally:
            await session.execute(delete(CaptchaChallenge).where(
                CaptchaChallenge.giveaway_id.in_([open_giveaway.id, closed_giveaway.id])
            ))
            await session.delete(open_giveaway)
            await session.delete(closed_giveaway)
            await session.commit()
    await engine.dispose()


async def test_captcha_deletion_and_registration_share_transaction() -> None:
    from datetime import timedelta

    from aiogram.types import User
    from sqlalchemy import delete

    from tg_drop_bot.config import Settings
    from tg_drop_bot.db.models import AuditLog, CaptchaChallenge, Giveaway, Participant
    from tg_drop_bot.services.captcha import hash_captcha_answer, verify_captcha_answer
    from tg_drop_bot.services.dates import utc_now
    from tg_drop_bot.services.giveaways import register_participant

    settings = Settings(APP_SECRET_KEY="test")
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        giveaway = Giveaway(
            creator_user_id=1, status="published", deadline_at=utc_now() + timedelta(hours=1)
        )
        session.add(giveaway)
        await session.flush()
        challenge = CaptchaChallenge(
            giveaway_id=giveaway.id, user_id=101, status="pending",
            answer_hash=hash_captcha_answer("ABCDE", settings),
            expires_at=utc_now() + timedelta(minutes=5),
        )
        session.add(challenge)
        await session.commit()
        challenge_id, giveaway_id = challenge.id, giveaway.id
        try:
            assert (await verify_captcha_answer(session, settings, challenge, "abcde")).ok
            await session.rollback()
            restored_challenge = await session.get(CaptchaChallenge, challenge_id)
            restored_giveaway = await session.get(Giveaway, giveaway_id)
            assert restored_challenge is not None and restored_giveaway is not None
            assert (await verify_captcha_answer(
                session, settings, restored_challenge, "ABCDE"
            )).ok
            participant, created = await register_participant(
                session, restored_giveaway, User(id=101, first_name="Test", is_bot=False)
            )
            await session.commit()
            assert created and participant.captcha_passed_at is not None
            assert await session.get(CaptchaChallenge, challenge_id) is None
        finally:
            await session.execute(delete(AuditLog).where(AuditLog.giveaway_id == giveaway_id))
            await session.execute(delete(Participant).where(Participant.giveaway_id == giveaway_id))
            await session.execute(delete(CaptchaChallenge).where(
                CaptchaChallenge.giveaway_id == giveaway_id
            ))
            await session.execute(delete(Giveaway).where(Giveaway.id == giveaway_id))
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
