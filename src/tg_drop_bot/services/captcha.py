from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass
from datetime import timedelta

from captcha.image import ImageCaptcha
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import CaptchaChallenge
from tg_drop_bot.services.dates import utc_now

CAPTCHA_ALPHABET = "".join(ch for ch in string.ascii_uppercase + string.digits if ch not in "0O1I")


@dataclass(frozen=True)
class CaptchaVerification:
    ok: bool
    reason: str
    attempts_left: int = 0


def is_blocking_captcha_challenge(challenge: CaptchaChallenge) -> bool:
    now = utc_now()
    if challenge.status == "pending":
        return challenge.expires_at > now
    return bool(
        challenge.status == "failed"
        and challenge.cooldown_until is not None
        and challenge.cooldown_until > now
    )


def hash_captcha_answer(answer: str, settings: Settings) -> str:
    normalized = answer.strip().upper()
    payload = f"{settings.app_secret_key}:{normalized}".encode()
    return hashlib.sha256(payload).hexdigest()


def generate_captcha_code(length: int) -> str:
    generator = random.SystemRandom()
    return "".join(generator.choice(CAPTCHA_ALPHABET) for _ in range(length))


def render_captcha_png(code: str) -> bytes:
    image = ImageCaptcha(width=220, height=90)
    buffer = image.generate(code)
    return buffer.getvalue()


async def create_captcha_challenge(
    session: AsyncSession,
    settings: Settings,
    giveaway_id: int,
    user_id: int,
) -> tuple[CaptchaChallenge, bytes]:
    code = generate_captcha_code(settings.captcha_length)
    challenge = CaptchaChallenge(
        giveaway_id=giveaway_id,
        user_id=user_id,
        answer_hash=hash_captcha_answer(code, settings),
        expires_at=utc_now() + timedelta(seconds=settings.captcha_ttl_seconds),
    )
    session.add(challenge)
    await session.flush()
    return challenge, render_captcha_png(code)


async def get_blocking_captcha_challenge(
    session: AsyncSession,
    giveaway_id: int,
    user_id: int,
) -> CaptchaChallenge | None:
    now = utc_now()
    result = await session.execute(
        select(CaptchaChallenge)
        .where(
            CaptchaChallenge.giveaway_id == giveaway_id,
            CaptchaChallenge.user_id == user_id,
            or_(
                and_(
                    CaptchaChallenge.status == "pending",
                    CaptchaChallenge.expires_at > now,
                ),
                and_(
                    CaptchaChallenge.status == "failed",
                    CaptchaChallenge.cooldown_until.is_not(None),
                    CaptchaChallenge.cooldown_until > now,
                ),
            ),
        )
        .order_by(CaptchaChallenge.created_at.desc(), CaptchaChallenge.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def verify_captcha_answer(
    session: AsyncSession,
    settings: Settings,
    challenge: CaptchaChallenge,
    answer: str,
) -> CaptchaVerification:
    now = utc_now()
    if challenge.status == "solved":
        return CaptchaVerification(ok=True, reason="already_solved")
    if challenge.cooldown_until and challenge.cooldown_until > now:
        return CaptchaVerification(ok=False, reason="cooldown")
    if challenge.expires_at <= now:
        challenge.status = "expired"
        await session.flush()
        return CaptchaVerification(ok=False, reason="expired")

    if hash_captcha_answer(answer, settings) == challenge.answer_hash:
        challenge.status = "solved"
        challenge.solved_at = now
        await session.flush()
        return CaptchaVerification(ok=True, reason="ok")

    challenge.attempts = (challenge.attempts or 0) + 1
    attempts_left = max(settings.captcha_max_attempts - challenge.attempts, 0)
    if attempts_left == 0:
        challenge.status = "failed"
        challenge.cooldown_until = now + timedelta(seconds=settings.captcha_cooldown_seconds)
    await session.flush()
    return CaptchaVerification(ok=False, reason="wrong", attempts_left=attempts_left)
