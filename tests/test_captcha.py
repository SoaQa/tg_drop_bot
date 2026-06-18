from datetime import timedelta

import pytest

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import CaptchaChallenge
from tg_drop_bot.services.captcha import (
    generate_captcha_code,
    hash_captcha_answer,
    is_blocking_captcha_challenge,
    render_captcha_png,
    verify_captcha_answer,
)
from tg_drop_bot.services.dates import utc_now


def test_captcha_code_uses_safe_alphabet() -> None:
    code = generate_captcha_code(16)
    assert len(code) == 16
    assert not set(code) & set("0O1I")


def test_captcha_png_is_generated() -> None:
    png = render_captcha_png("ABCDE")
    assert png.startswith(b"\x89PNG")


def test_pending_captcha_blocks_repeated_start() -> None:
    challenge = CaptchaChallenge(
        giveaway_id=1,
        user_id=1,
        answer_hash="hash",
        status="pending",
        expires_at=utc_now() + timedelta(minutes=5),
    )

    assert is_blocking_captcha_challenge(challenge)


def test_expired_captcha_does_not_block_repeated_start() -> None:
    challenge = CaptchaChallenge(
        giveaway_id=1,
        user_id=1,
        answer_hash="hash",
        status="pending",
        expires_at=utc_now() - timedelta(seconds=1),
    )

    assert not is_blocking_captcha_challenge(challenge)


def test_failed_captcha_blocks_until_cooldown_expires() -> None:
    challenge = CaptchaChallenge(
        giveaway_id=1,
        user_id=1,
        answer_hash="hash",
        status="failed",
        expires_at=utc_now() + timedelta(minutes=5),
        cooldown_until=utc_now() + timedelta(minutes=5),
    )

    assert is_blocking_captcha_challenge(challenge)


@pytest.mark.asyncio
async def test_verify_captcha_answer_success() -> None:
    settings = Settings(APP_SECRET_KEY="secret")
    challenge = CaptchaChallenge(
        giveaway_id=1,
        user_id=1,
        answer_hash=hash_captcha_answer("ABCDE", settings),
        expires_at=utc_now() + timedelta(minutes=5),
    )

    class Session:
        async def flush(self) -> None:
            return None

    result = await verify_captcha_answer(Session(), settings, challenge, "abcde")  # type: ignore[arg-type]
    assert result.ok
    assert challenge.status == "solved"


@pytest.mark.asyncio
async def test_verify_captcha_answer_locks_after_attempts() -> None:
    settings = Settings(APP_SECRET_KEY="secret", CAPTCHA_MAX_ATTEMPTS=1)
    challenge = CaptchaChallenge(
        giveaway_id=1,
        user_id=1,
        answer_hash=hash_captcha_answer("ABCDE", settings),
        expires_at=utc_now() + timedelta(minutes=5),
    )

    class Session:
        async def flush(self) -> None:
            return None

    result = await verify_captcha_answer(Session(), settings, challenge, "wrong")  # type: ignore[arg-type]
    assert not result.ok
    assert challenge.status == "failed"
    assert challenge.cooldown_until is not None
