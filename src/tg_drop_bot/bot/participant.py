from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.bot.keyboards import admin_main_keyboard
from tg_drop_bot.bot.states import CaptchaStates
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import CaptchaChallenge
from tg_drop_bot.services.access import is_admin
from tg_drop_bot.services.captcha import create_captcha_challenge, verify_captcha_answer
from tg_drop_bot.services.conditions import check_participant_conditions
from tg_drop_bot.services.giveaways import get_giveaway, register_participant

router = Router(name="participant")

GIVEAWAY_PAYLOAD_RE = re.compile(r"^giveaway_(\d+)$")


@router.callback_query(F.data.startswith("participate:"))
async def participate_callback(callback: CallbackQuery, bot: Bot) -> None:
    assert callback.data is not None
    giveaway_id = int(callback.data.split(":")[1])
    me = await bot.get_me()
    if me.username is None:
        await callback.answer(
            "У бота нет username, участие через ссылку недоступно.", show_alert=True
        )
        return
    await callback.answer(url=f"https://t.me/{me.username}?start=giveaway_{giveaway_id}")


@router.message(CommandStart())
async def start_message(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    await state.clear()
    payload = command.args or ""
    match = GIVEAWAY_PAYLOAD_RE.match(payload)
    if match:
        await start_captcha_flow(message, state, session, settings, bot, int(match.group(1)))
        return
    if message.from_user and is_admin(message.from_user.id, settings):
        await message.answer("Админка открыта.", reply_markup=admin_main_keyboard())
    else:
        await message.answer("Чтобы участвовать в розыгрыше, нажмите кнопку под постом в канале.")


async def start_captcha_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    giveaway_id: int,
) -> None:
    if message.from_user is None:
        return
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or giveaway.status != "published" or giveaway.group is None:
        await message.answer("Этот розыгрыш сейчас недоступен.")
        return

    condition_check = await check_participant_conditions(bot, giveaway, message.from_user.id)
    if not condition_check.ok:
        await message.answer(condition_check.user_message or "Условия участия не выполнены.")
        return

    participant, created = await register_participant_if_already_solved(
        session, giveaway_id, message.from_user.id
    )
    if participant is not None and not created:
        await message.answer("Ваше участие уже засчитано.")
        return

    challenge, image_bytes = await create_captcha_challenge(
        session, settings, giveaway.id, message.from_user.id
    )
    await session.commit()
    await state.set_state(CaptchaStates.answer)
    await state.update_data(challenge_id=challenge.id, giveaway_id=giveaway.id)
    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="captcha.png"),
        caption="Введите код с картинки. Капча действует несколько минут.",
    )


async def register_participant_if_already_solved(
    session: AsyncSession,
    giveaway_id: int,
    user_id: int,
) -> tuple[object | None, bool]:
    from sqlalchemy import select

    from tg_drop_bot.db.models import Participant

    result = await session.execute(
        select(Participant).where(
            Participant.giveaway_id == giveaway_id,
            Participant.user_id == user_id,
        )
    )
    participant = result.scalar_one_or_none()
    return participant, False


@router.message(CaptchaStates.answer)
async def captcha_answer(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if message.from_user is None or not message.text:
        await message.answer("Введите код текстом.")
        return
    data = await state.get_data()
    challenge = await session.get(CaptchaChallenge, data["challenge_id"])
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if challenge is None or giveaway is None or giveaway.status != "published":
        await state.clear()
        await message.answer("Проверка устарела. Нажмите кнопку участия еще раз.")
        return

    verification = await verify_captcha_answer(session, settings, challenge, message.text)
    if not verification.ok:
        await session.commit()
        if verification.reason == "expired":
            await state.clear()
            await message.answer("Капча истекла. Нажмите кнопку участия еще раз.")
        elif verification.reason == "cooldown":
            await state.clear()
            await message.answer("Слишком много попыток. Попробуйте позже.")
        elif verification.attempts_left > 0:
            await message.answer(f"Код неверный. Осталось попыток: {verification.attempts_left}.")
        else:
            await state.clear()
            await message.answer("Попытки закончились. Попробуйте позже.")
        return

    participant, created = await register_participant(session, giveaway, message.from_user)
    await session.commit()
    await state.clear()
    if created:
        await message.answer("Готово, участие засчитано.")
    else:
        await message.answer("Ваше участие уже было засчитано.")
