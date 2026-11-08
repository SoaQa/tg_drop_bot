from __future__ import annotations

import hashlib
import random
import string
from dataclasses import dataclass
from datetime import datetime, timedelta

from captcha.image import ImageCaptcha
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import CaptchaChallenge, Giveaway
from tg_drop_bot.services.dates import utc_now

CAPTCHA_ALPHABET = "".join(ch for ch in string.ascii_uppercase + string.digits if ch not in "0O1I")
FAST_CAPTCHA_THRESHOLD_MS = 2000


def captcha_response_ms(sent_at: datetime | None, received_at: datetime) -> int | None:
    if sent_at is None or received_at < sent_at:
        return None
    return (received_at - sent_at) // timedelta(milliseconds=1)


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


async def get_pending_captcha_challenges(
    session: AsyncSession,
    user_id: int,
) -> list[CaptchaChallenge]:
    result = await session.execute(
        select(CaptchaChallenge)
        .where(
            CaptchaChallenge.user_id == user_id,
            CaptchaChallenge.status == "pending",
        )
        .order_by(CaptchaChallenge.created_at.desc(), CaptchaChallenge.id.desc())
    )
    return list(result.scalars().all())


def find_challenge_matching_answer(
    challenges: list[CaptchaChallenge],
    answer: str,
    settings: Settings,
) -> CaptchaChallenge | None:
    """Pick the pending challenge whose answer matches the text.

    A user can hold captchas for several giveaways at once; matching by
    answer keeps a correct code from burning attempts on the wrong challenge.
    """
    hashed = hash_captcha_answer(answer, settings)
    for challenge in challenges:
        if challenge.answer_hash == hashed:
            return challenge
    return None


def looks_like_captcha_answer(text: str) -> bool:
    normalized = text.strip()
    return 3 <= len(normalized) <= 16 and normalized.isascii() and normalized.isalnum()


async def verify_captcha_answer(
    session: AsyncSession,
    settings: Settings,
    challenge: CaptchaChallenge,
    answer: str,
) -> CaptchaVerification:
    now = utc_now()
    if challenge.status == "solved":
        await session.delete(challenge)
        await session.flush()
        return CaptchaVerification(ok=True, reason="already_solved")
    if challenge.cooldown_until and challenge.cooldown_until > now:
        return CaptchaVerification(ok=False, reason="cooldown")
    if challenge.status != "pending" or challenge.expires_at <= now:
        await session.delete(challenge)
        await session.flush()
        return CaptchaVerification(ok=False, reason="expired")

    if hash_captcha_answer(answer, settings) == challenge.answer_hash:
        # The caller commits this deletion together with participant registration.
        await session.delete(challenge)
        await session.flush()
        return CaptchaVerification(ok=True, reason="ok")

    challenge.attempts = (challenge.attempts or 0) + 1
    attempts_left = max(settings.captcha_max_attempts - challenge.attempts, 0)
    if attempts_left == 0:
        challenge.status = "failed"
        challenge.cooldown_until = now + timedelta(seconds=settings.captcha_cooldown_seconds)
    await session.flush()
    return CaptchaVerification(ok=False, reason="wrong", attempts_left=attempts_left)


async def cleanup_captcha_challenges(session: AsyncSession, *, batch_size: int = 1000) -> int:
    now = utc_now()
    open_giveaway = select(Giveaway.id).where(
        Giveaway.id == CaptchaChallenge.giveaway_id,
        Giveaway.status == "published",
        Giveaway.deadline_at > now,
    ).exists()
    obsolete = or_(
        CaptchaChallenge.status.in_(["solved", "expired"]),
        and_(CaptchaChallenge.status == "pending", CaptchaChallenge.expires_at <= now),
        and_(
            CaptchaChallenge.status == "failed",
            or_(
                CaptchaChallenge.cooldown_until.is_(None),
                CaptchaChallenge.cooldown_until <= now,
            ),
        ),
        ~open_giveaway,
    )
    # Skip answers and refreshes currently being processed in another transaction.
    ids = list((await session.scalars(
        select(CaptchaChallenge.id)
        .where(obsolete)
        .order_by(CaptchaChallenge.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )).all())
    if not ids:
        return 0
    await session.execute(delete(CaptchaChallenge).where(CaptchaChallenge.id.in_(ids)))
    return len(ids)
