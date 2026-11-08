from __future__ import annotations

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.bot.keyboards import (
    active_giveaways_keyboard,
    admin_main_keyboard,
    captcha_keyboard,
    parse_callback_int,
)
from tg_drop_bot.bot.states import CaptchaStates
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import CaptchaChallenge, Participant
from tg_drop_bot.services.access import is_admin
from tg_drop_bot.services.captcha import (
    captcha_response_ms,
    create_captcha_challenge,
    find_challenge_matching_answer,
    get_blocking_captcha_challenge,
    get_pending_captcha_challenges,
    looks_like_captcha_answer,
    verify_captcha_answer,
)
from tg_drop_bot.services.conditions import check_participant_conditions
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import (
    get_giveaway,
    is_giveaway_open,
    list_published_giveaways,
    register_participant_after_condition_check,
    single_giveaway_or_none,
)
from tg_drop_bot.services.membership import PARTICIPATION_DENIED, get_active_exclusion

router = Router(name="participant")

GIVEAWAY_PAYLOAD_RE = re.compile(r"^giveaway_(\d+)$")


def captcha_cooldown_notice(settings: Settings) -> str:
    return f"Новую капчу можно получить через {settings.captcha_cooldown_seconds} сек."


@router.callback_query(F.data.startswith("participate:"))
async def participate_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    giveaway_id = parse_callback_int(callback.data, 1)
    if giveaway_id is None:
        await callback.answer("Кнопка устарела.", show_alert=True)
        return
    if isinstance(callback.message, Message) and callback.message.chat.type == "private":
        await callback.answer()
        await state.clear()
        await start_captcha_flow(
            callback.message,
            state,
            session,
            settings,
            bot,
            giveaway_id,
            user=callback.from_user,
        )
        return

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
        giveaways = await list_published_giveaways(session)
        single_giveaway = single_giveaway_or_none(giveaways)
        if single_giveaway is not None:
            await start_captcha_flow(message, state, session, settings, bot, single_giveaway.id)
            return
        if giveaways:
            await message.answer(
                "Выберите розыгрыш, в котором хотите участвовать.",
                reply_markup=active_giveaways_keyboard(giveaways),
            )
            return
        await message.answer("Чтобы участвовать в розыгрыше, нажмите кнопку под постом в канале.")


async def start_captcha_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    giveaway_id: int,
    *,
    user: User | None = None,
) -> None:
    participant_user = user or message.from_user
    if participant_user is None:
        return
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or giveaway.status != "published" or giveaway.channel is None:
        await message.answer("Этот розыгрыш сейчас недоступен.")
        return
    if not is_giveaway_open(giveaway):
        await message.answer("Прием заявок в этом розыгрыше уже закрыт, подводим итоги.")
        return

    if giveaway.channel_id is not None and await get_active_exclusion(
        session, giveaway.channel_id, participant_user.id
    ):
        await message.answer(PARTICIPATION_DENIED)
        return

    condition_check = await check_participant_conditions(bot, giveaway, participant_user.id)
    if not condition_check.ok:
        await message.answer(condition_check.user_message or "Условия участия не выполнены.")
        return

    participant = await get_existing_participant(session, giveaway_id, participant_user.id)
    if participant is not None:
        await message.answer("Ваше участие уже засчитано.")
        return

    blocking_challenge = await get_blocking_captcha_challenge(
        session, giveaway_id, participant_user.id
    )
    if blocking_challenge is not None:
        if blocking_challenge.status == "failed":
            await message.answer("Слишком много попыток. " + captcha_cooldown_notice(settings))
        else:
            await message.answer(
                "Капча уже отправлена. Введите код из последнего сообщения с картинкой "
                "или обновите капчу.",
                reply_markup=captcha_keyboard(blocking_challenge.id),
            )
        return

    challenge, image_bytes = await create_captcha_challenge(
        session, settings, giveaway.id, participant_user.id
    )
    await session.commit()
    await send_captcha_challenge(message, state, session, challenge, image_bytes)


@router.callback_query(F.data.startswith("captcha:refresh:"))
async def refresh_captcha_callback(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer("Капча устарела.", show_alert=True)
        return

    challenge_id = parse_callback_int(callback.data, 2)
    if challenge_id is None:
        await callback.answer("Капча устарела.", show_alert=True)
        return
    challenge = await session.scalar(
        select(CaptchaChallenge).where(CaptchaChallenge.id == challenge_id).with_for_update()
    )
    if (
        challenge is None
        or challenge.user_id != callback.from_user.id
        or challenge.status != "pending"
    ):
        await callback.answer("Капча устарела. Нажмите кнопку участия еще раз.", show_alert=True)
        return

    giveaway = await get_giveaway(session, challenge.giveaway_id)
    if giveaway is None or not is_giveaway_open(giveaway):
        await callback.answer("Розыгрыш сейчас недоступен.", show_alert=True)
        return

    if giveaway.channel_id is not None and await get_active_exclusion(
        session, giveaway.channel_id, callback.from_user.id
    ):
        await callback.answer()
        await callback.message.answer(PARTICIPATION_DENIED)
        return

    condition_check = await check_participant_conditions(bot, giveaway, callback.from_user.id)
    if not condition_check.ok:
        await callback.answer()
        await callback.message.answer(
            condition_check.user_message or "Условия участия не выполнены."
        )
        return

    await session.delete(challenge)
    new_challenge, image_bytes = await create_captcha_challenge(
        session,
        settings,
        challenge.giveaway_id,
        challenge.user_id,
    )
    await session.commit()
    await callback.answer("Капча обновлена.")
    await send_captcha_challenge(
        callback.message,
        state,
        session,
        new_challenge,
        image_bytes,
        caption="Введите код с новой картинки. Капча действует несколько минут.",
    )


async def send_captcha_challenge(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    challenge: CaptchaChallenge,
    image_bytes: bytes,
    *,
    caption: str = "Введите код с картинки. Капча действует несколько минут.",
) -> None:
    await state.set_state(CaptchaStates.answer)
    await state.update_data(challenge_id=challenge.id, giveaway_id=challenge.giveaway_id)
    await message.answer_photo(
        BufferedInputFile(image_bytes, filename="captcha.png"),
        caption=caption,
        reply_markup=captcha_keyboard(challenge.id),
    )
    challenge.captcha_sent_at = utc_now()
    await session.commit()


async def get_existing_participant(
    session: AsyncSession,
    giveaway_id: int,
    user_id: int,
) -> Participant | None:
    from sqlalchemy import select

    result = await session.execute(
        select(Participant).where(
            Participant.giveaway_id == giveaway_id,
            Participant.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


@router.message(CaptchaStates.answer)
async def captcha_answer(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    received_at = utc_now()
    if message.from_user is None or not message.text:
        await message.answer("Введите код текстом.")
        return
    data = await state.get_data()
    challenge_id = data.get("challenge_id")
    if challenge_id is None:
        challenge = await find_stateless_challenge(
            session, settings, message.from_user.id, message.text
        )
    else:
        challenge = await session.get(CaptchaChallenge, challenge_id)
    await process_captcha_answer(message, state, session, settings, bot, challenge, received_at)


@router.message(StateFilter(None), F.chat.type == "private", F.text.regexp(r"^[A-Za-z0-9]{3,16}$"))
async def captcha_answer_without_state(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    received_at = utc_now()
    if message.from_user is None or not message.text:
        return
    if not looks_like_captcha_answer(message.text):
        return
    challenge = await find_stateless_challenge(
        session, settings, message.from_user.id, message.text
    )
    if challenge is None:
        return
    await process_captcha_answer(message, state, session, settings, bot, challenge, received_at)


async def find_stateless_challenge(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    answer_text: str,
) -> CaptchaChallenge | None:
    """Resolve which pending captcha the text answers when FSM state is lost.

    Prefers the challenge whose answer actually matches (the user may hold
    captchas for several giveaways), falling back to the most recent one.
    """
    challenges = await get_pending_captcha_challenges(session, user_id)
    if not challenges:
        return None
    matched = find_challenge_matching_answer(challenges, answer_text, settings)
    return matched or challenges[0]


async def process_captcha_answer(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    challenge: CaptchaChallenge | None,
    received_at: datetime,
) -> None:
    if message.from_user is None or not message.text:
        await message.answer("Введите код текстом.")
        return
    if challenge is not None:
        challenge = await session.scalar(
            select(CaptchaChallenge)
            .where(CaptchaChallenge.id == challenge.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    giveaway = await get_giveaway(session, challenge.giveaway_id) if challenge else None
    if (
        challenge is None
        or challenge.user_id != message.from_user.id
        or giveaway is None
        or giveaway.status != "published"
    ):
        await state.clear()
        await message.answer("Проверка устарела. Нажмите кнопку участия еще раз.")
        return
    if not is_giveaway_open(giveaway):
        await state.clear()
        await message.answer("Прием заявок в этом розыгрыше уже закрыт, подводим итоги.")
        return

    if giveaway.channel_id is not None and await get_active_exclusion(
        session, giveaway.channel_id, message.from_user.id
    ):
        await state.clear()
        await message.answer(PARTICIPATION_DENIED)
        return
    response_ms = captcha_response_ms(challenge.captcha_sent_at, received_at)
    verification = await verify_captcha_answer(session, settings, challenge, message.text)
    if not verification.ok:
        await session.commit()
        if verification.reason == "expired":
            await state.clear()
            await message.answer("Капча истекла. Нажмите кнопку участия еще раз.")
        elif verification.reason == "cooldown":
            await state.clear()
            await message.answer("Слишком много попыток. " + captcha_cooldown_notice(settings))
        elif verification.attempts_left > 0:
            await message.answer(f"Код неверный. Осталось попыток: {verification.attempts_left}.")
        else:
            await state.clear()
            await message.answer("Попытки закончились. " + captcha_cooldown_notice(settings))
        return

    registration = await register_participant_after_condition_check(
        session,
        bot,
        giveaway,
        message.from_user,
    )
    if (
        verification.reason == "ok"
        and registration.created
        and registration.participant is not None
    ):
        registration.participant.captcha_response_ms = response_ms
    await session.commit()
    await state.clear()
    if registration.participant is None:
        await message.answer(
            registration.condition_check.user_message or "Условия участия не выполнены."
        )
    elif registration.created:
        await message.answer("Готово, участие засчитано.")
    else:
        await message.answer("Ваше участие уже было засчитано.")
