from __future__ import annotations

# mypy: disable-error-code="union-attr,index"
from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.bot.keyboards import (
    CHANNEL_REQUEST_ID,
    admin_main_keyboard,
    channel_request_keyboard,
    channels_keyboard,
    confirm_keyboard,
    draft_conditions_keyboard,
    draft_image_keyboard,
    draft_preview_keyboard,
    giveaway_card_keyboard,
    giveaway_list_keyboard,
)
from tg_drop_bot.bot.states import DraftStates, EditStates
from tg_drop_bot.config import Settings
from tg_drop_bot.services.access import can_manage_giveaway, is_admin
from tg_drop_bot.services.conditions import CHANNEL_SUBSCRIPTION_CONDITION, condition_label
from tg_drop_bot.services.dates import parse_admin_deadline
from tg_drop_bot.services.giveaways import (
    add_audit,
    build_csv_file,
    cancel_giveaway,
    count_participants,
    create_draft,
    edit_published_message,
    finish_giveaway,
    get_giveaway,
    list_available_channels,
    list_giveaways_by_status,
    publish_giveaway,
    replace_published_image,
    upsert_known_channel,
    validate_draft,
)
from tg_drop_bot.services.rendering import render_giveaway_card, render_giveaway_post

router = Router(name="admin")


def parse_edit_callback_data(data: str | None) -> tuple[str, int] | None:
    if data is None:
        return None
    parts = data.split(":")
    if (
        len(parts) != 3
        or parts[0] != "giveaway"
        or not parts[1].startswith("edit_")
        or not parts[2].isdigit()
    ):
        return None
    return parts[1].removeprefix("edit_"), int(parts[2])


def admin_only(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id, settings)


@router.message(StateFilter(None), F.chat.type == "private", F.text == "Создать розыгрыш")
async def create_giveaway(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not admin_only(message, settings):
        return
    channels = await list_available_channels(session)
    if not channels:
        await message.answer(
            "Пока нет доступных каналов. Нажмите «Выбрать канал» и выберите канал, "
            "где бот добавлен администратором.",
            reply_markup=channel_request_keyboard(),
        )
        return
    draft = await create_draft(session, message.from_user.id)
    await session.commit()
    await state.set_state(DraftStates.choosing_channel)
    await state.update_data(giveaway_id=draft.id)
    await message.answer("Выберите канал для розыгрыша.", reply_markup=channels_keyboard(channels))


@router.callback_query(DraftStates.choosing_channel, F.data.startswith("draft:channel:"))
async def draft_channel_selected(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    channel_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    giveaway.channel_id = channel_id
    await session.commit()
    await state.set_state(DraftStates.title)
    await callback.message.answer("Введите название розыгрыша.")
    await callback.answer()


@router.message(DraftStates.title)
async def draft_title(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None or not message.text:
        await message.answer("Введите название сообщением.")
        return
    giveaway.title = message.text.strip()
    await session.commit()
    await state.set_state(DraftStates.post_text)
    await message.answer("Введите описание розыгрыша.")


@router.message(DraftStates.post_text)
async def draft_post_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None or not message.text:
        await message.answer("Введите описание сообщением.")
        return
    giveaway.post_text = message.text.strip()
    await session.commit()
    await state.set_state(DraftStates.terms_text)
    await message.answer("Выберите условие участия.", reply_markup=draft_conditions_keyboard())


@router.message(DraftStates.terms_text)
async def draft_terms_text(message: Message) -> None:
    await message.answer("Выберите условие кнопкой.", reply_markup=draft_conditions_keyboard())


@router.callback_query(
    DraftStates.terms_text,
    F.data == f"draft:condition:{CHANNEL_SUBSCRIPTION_CONDITION}",
)
async def draft_condition_selected(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    giveaway.terms_text = condition_label(CHANNEL_SUBSCRIPTION_CONDITION)
    await session.commit()
    await state.set_state(DraftStates.winners_count)
    await callback.message.answer("Сколько победителей выбрать?")
    await callback.answer()


@router.callback_query(DraftStates.image, F.data == "draft:image:skip")
async def draft_image_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DraftStates.deadline)
    await callback.message.answer(
        "Введите дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 25.06.2026 18:30."
    )
    await callback.answer()


@router.message(DraftStates.image)
async def draft_image(message: Message, state: FSMContext, session: AsyncSession) -> None:
    if not message.photo:
        await message.answer("Отправьте изображение или нажмите «Пропустить».")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await message.answer("Черновик не найден.")
        return
    giveaway.image_file_id = message.photo[-1].file_id
    await session.commit()
    await state.set_state(DraftStates.deadline)
    await message.answer(
        "Введите дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 25.06.2026 18:30."
    )


@router.message(DraftStates.winners_count)
async def draft_winners_count(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        count = int((message.text or "").strip())
        if count <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число.")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await message.answer("Черновик не найден.")
        return
    giveaway.winners_count = count
    await session.commit()
    await state.set_state(DraftStates.image)
    await message.answer(
        "Отправьте картинку для поста или пропустите шаг.", reply_markup=draft_image_keyboard()
    )


@router.message(DraftStates.deadline)
async def draft_deadline(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    try:
        deadline = parse_admin_deadline(message.text or "", settings.timezone)
    except ValueError:
        await message.answer("Дата не распознана. Пример: 25.06.2026 18:30.")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await message.answer("Черновик не найден.")
        return
    giveaway.deadline_at = deadline
    await session.commit()
    await state.clear()
    await show_draft_preview(message, giveaway, settings)


async def show_draft_preview(message: Message, giveaway, settings: Settings) -> None:
    text = render_giveaway_post(giveaway, settings)
    missing = validate_draft(giveaway)
    if missing:
        text += "\n\nНе заполнено: " + ", ".join(missing)
    if giveaway.image_file_id:
        await message.answer_photo(
            giveaway.image_file_id,
            caption=text,
            reply_markup=draft_preview_keyboard(giveaway.id),
        )
    else:
        await message.answer(text, reply_markup=draft_preview_keyboard(giveaway.id))


@router.callback_query(F.data.startswith("draft:publish:"))
async def draft_publish(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    if callback.from_user is None or not is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Розыгрыш не найден или нет доступа.", show_alert=True)
        return
    missing = validate_draft(giveaway)
    if missing:
        await callback.answer("Черновик не заполнен: " + ", ".join(missing), show_alert=True)
        return
    await publish_giveaway(session, bot, settings, giveaway, callback.from_user.id)
    await session.commit()
    await callback.message.answer("Розыгрыш опубликован.", reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("draft:delete:"))
async def draft_delete(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    giveaway.status = "cancelled"
    await add_audit(
        session,
        "giveaway.draft_deleted",
        giveaway_id=giveaway.id,
        actor_user_id=callback.from_user.id,
    )
    await session.commit()
    await callback.message.answer("Черновик удален.", reply_markup=admin_main_keyboard())
    await callback.answer()


@router.message(
    StateFilter(None),
    F.chat.type == "private",
    F.text.in_({"Активные", "Черновики", "Завершенные", "Отмененные"}),
)
async def list_giveaways(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not admin_only(message, settings):
        return
    status_by_text = {
        "Активные": "published",
        "Черновики": "draft",
        "Завершенные": "finished",
        "Отмененные": "cancelled",
    }
    status = status_by_text[message.text]
    giveaways = await list_giveaways_by_status(
        session,
        status,
        admin_user_id=message.from_user.id,
        current_admin_ids=settings.admin_id_set,
    )
    if not giveaways:
        await message.answer("Ничего не найдено.", reply_markup=admin_main_keyboard())
        return
    await message.answer("Выберите розыгрыш.", reply_markup=giveaway_list_keyboard(giveaways))


@router.message(StateFilter(None), F.chat.type == "private", F.text == "Каналы")
async def show_channels(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not admin_only(message, settings):
        return
    channels = await list_available_channels(session)
    if not channels:
        await message.answer(
            "Доступных каналов пока нет. Нажмите «Выбрать канал» и выберите канал, "
            "где бот добавлен администратором.",
            reply_markup=channel_request_keyboard(),
        )
        return
    text = "\n".join(f"- {channel.title} ({channel.telegram_chat_id})" for channel in channels)
    await message.answer(
        text + "\n\nЧтобы добавить еще один канал, нажмите «Выбрать канал».",
        reply_markup=channel_request_keyboard(),
    )


@router.message(StateFilter(None), F.chat.type == "private", F.chat_shared)
async def register_shared_channel(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
) -> None:
    if not admin_only(message, settings) or message.chat_shared is None:
        return
    if message.chat_shared.request_id != CHANNEL_REQUEST_ID:
        return

    chat_id = message.chat_shared.chat_id
    try:
        chat = await bot.get_chat(chat_id)
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
    except TelegramAPIError:
        await message.answer(
            "Не удалось проверить канал. Убедитесь, что бот добавлен в канал "
            "администратором, и попробуйте еще раз.",
            reply_markup=channel_request_keyboard(),
        )
        return

    if chat.type != "channel":
        await message.answer(
            "Можно добавлять только каналы.", reply_markup=channel_request_keyboard()
        )
        return

    bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
    if not bot_is_admin:
        await message.answer(
            "Бот найден в канале, но не является администратором. "
            "Выдайте ему права администратора и выберите канал еще раз.",
            reply_markup=channel_request_keyboard(),
        )
        return

    channel = await upsert_known_channel(
        session,
        telegram_chat_id=chat.id,
        title=chat.title or message.chat_shared.title or str(chat.id),
        username=chat.username or message.chat_shared.username,
        is_active=True,
        bot_is_admin=True,
    )
    await session.commit()
    await message.answer(
        f"Канал добавлен: {channel.title}. Теперь его можно выбрать при создании розыгрыша.",
        reply_markup=admin_main_keyboard(),
    )


@router.callback_query(F.data.startswith("giveaway:view:"))
async def view_giveaway(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    participants_count = await count_participants(session, giveaway.id)
    await callback.message.answer(
        render_giveaway_card(giveaway, settings, participants_count),
        reply_markup=giveaway_card_keyboard(giveaway),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("giveaway:csv:"))
async def export_csv(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    csv_file = await build_csv_file(session, giveaway.id)
    await callback.message.answer_document(csv_file)
    await callback.answer()


@router.callback_query(F.data.startswith("giveaway:edit_"))
async def edit_field_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    parsed = parse_edit_callback_data(callback.data)
    if parsed is None:
        await callback.answer("Действие устарело. Откройте розыгрыш заново.", show_alert=True)
        return
    field, giveaway_id = parsed
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.update_data(giveaway_id=giveaway.id)
    prompts = {
        "title": ("Введите новое название розыгрыша.", EditStates.title),
        "text": ("Введите новое описание розыгрыша.", EditStates.text),
        "terms": ("Выберите новое условие участия.", EditStates.terms),
        "deadline": ("Введите новый дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ.", EditStates.deadline),
        "winners": ("Введите новое количество победителей.", EditStates.winners),
        "image": ("Отправьте новую картинку.", EditStates.image),
    }
    if field == "image" and not giveaway.image_file_id:
        await callback.answer(
            "В v1 можно только заменить уже опубликованную картинку.", show_alert=True
        )
        return
    prompt_config = prompts.get(field)
    if prompt_config is None:
        await callback.answer("Действие устарело. Откройте розыгрыш заново.", show_alert=True)
        return
    prompt, next_state = prompt_config
    await state.set_state(next_state)
    if next_state == EditStates.terms:
        await callback.message.answer(prompt, reply_markup=draft_conditions_keyboard())
    else:
        await callback.message.answer(prompt)
    await callback.answer()


@router.message(EditStates.title)
async def edit_title(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    await update_text_field(message, state, session, settings, bot, "title", message.text or "")


@router.message(EditStates.text)
async def edit_text(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    await update_text_field(message, state, session, settings, bot, "post_text", message.text or "")


@router.message(EditStates.terms)
async def edit_terms(
    message: Message,
) -> None:
    await message.answer("Выберите условие кнопкой.", reply_markup=draft_conditions_keyboard())


@router.callback_query(
    EditStates.terms,
    F.data == f"draft:condition:{CHANNEL_SUBSCRIPTION_CONDITION}",
)
async def edit_terms_selected(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    if not isinstance(callback.message, Message):
        await callback.answer("Сообщение не найдено.", show_alert=True)
        return
    await update_text_field(
        callback.message,
        state,
        session,
        settings,
        bot,
        "terms_text",
        condition_label(CHANNEL_SUBSCRIPTION_CONDITION),
        actor_user_id=callback.from_user.id,
    )
    await callback.answer()


async def update_text_field(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    field: str,
    value: str,
    *,
    actor_user_id: int | None = None,
) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if actor_user_id is None:
        if message.from_user is None:
            await message.answer("Нет доступа.")
            return
        manager_user_id = message.from_user.id
    else:
        manager_user_id = actor_user_id
    if (
        giveaway is None
        or not can_manage_giveaway(manager_user_id, giveaway, settings)
    ):
        await message.answer("Нет доступа.")
        return
    setattr(giveaway, field, value.strip())
    participants_count = await count_participants(session, giveaway.id)
    await edit_published_message(
        bot,
        settings,
        giveaway,
        participants_count=participants_count,
    )
    await add_audit(
        session,
        f"giveaway.edited.{field}",
        giveaway_id=giveaway.id,
        actor_user_id=manager_user_id,
    )
    await session.commit()
    await state.clear()
    await message.answer("Обновлено.", reply_markup=admin_main_keyboard())


@router.message(EditStates.deadline)
async def edit_deadline(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    try:
        deadline = parse_admin_deadline(message.text or "", settings.timezone)
    except ValueError:
        await message.answer("Дата не распознана. Пример: 25.06.2026 18:30.")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if (
        giveaway is None
        or message.from_user is None
        or not can_manage_giveaway(message.from_user.id, giveaway, settings)
    ):
        await message.answer("Нет доступа.")
        return
    giveaway.deadline_at = deadline
    participants_count = await count_participants(session, giveaway.id)
    await edit_published_message(
        bot,
        settings,
        giveaway,
        participants_count=participants_count,
    )
    await add_audit(
        session,
        "giveaway.edited.deadline",
        giveaway_id=giveaway.id,
        actor_user_id=message.from_user.id,
    )
    await session.commit()
    await state.clear()
    await message.answer("Дедлайн обновлен.", reply_markup=admin_main_keyboard())


@router.message(EditStates.winners)
async def edit_winners(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    try:
        count = int((message.text or "").strip())
        if count <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число.")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if (
        giveaway is None
        or message.from_user is None
        or not can_manage_giveaway(message.from_user.id, giveaway, settings)
    ):
        await message.answer("Нет доступа.")
        return
    giveaway.winners_count = count
    participants_count = await count_participants(session, giveaway.id)
    await edit_published_message(
        bot,
        settings,
        giveaway,
        participants_count=participants_count,
    )
    await add_audit(
        session,
        "giveaway.edited.winners_count",
        giveaway_id=giveaway.id,
        actor_user_id=message.from_user.id,
    )
    await session.commit()
    await state.clear()
    await message.answer("Количество победителей обновлено.", reply_markup=admin_main_keyboard())


@router.message(EditStates.image)
async def edit_image(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    if not message.photo:
        await message.answer("Отправьте изображение.")
        return
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if (
        giveaway is None
        or message.from_user is None
        or not can_manage_giveaway(message.from_user.id, giveaway, settings)
    ):
        await message.answer("Нет доступа.")
        return
    participants_count = await count_participants(session, giveaway.id)
    await replace_published_image(
        bot,
        settings,
        giveaway,
        message.photo[-1].file_id,
        participants_count=participants_count,
    )
    await add_audit(
        session,
        "giveaway.edited.image",
        giveaway_id=giveaway.id,
        actor_user_id=message.from_user.id,
    )
    await session.commit()
    await state.clear()
    await message.answer("Картинка заменена.", reply_markup=admin_main_keyboard())


@router.callback_query(F.data.startswith("giveaway:cancel:"))
async def cancel_start(callback: CallbackQuery) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    await callback.message.answer(
        "Отменить розыгрыш?", reply_markup=confirm_keyboard("cancel", giveaway_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("giveaway:finish:"))
async def finish_start(callback: CallbackQuery) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    await callback.message.answer(
        "Завершить розыгрыш сейчас?", reply_markup=confirm_keyboard("finish", giveaway_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("giveaway:cancel_confirm:"))
async def cancel_confirm(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await cancel_giveaway(session, bot, settings, giveaway, callback.from_user.id)
    await session.commit()
    await callback.message.answer("Розыгрыш отменен.", reply_markup=admin_main_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("giveaway:finish_confirm:"))
async def finish_confirm(
    callback: CallbackQuery, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    giveaway_id = int(callback.data.split(":")[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await finish_giveaway(
        session,
        bot,
        settings,
        giveaway,
        actor_user_id=callback.from_user.id,
        manual=True,
    )
    await session.commit()
    await callback.message.answer("Розыгрыш завершен.", reply_markup=admin_main_keyboard())
    await callback.answer()
