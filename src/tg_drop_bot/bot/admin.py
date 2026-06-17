from __future__ import annotations

# mypy: disable-error-code="union-attr,index"
from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.bot.keyboards import (
    admin_main_keyboard,
    confirm_keyboard,
    draft_image_keyboard,
    draft_preview_keyboard,
    giveaway_card_keyboard,
    giveaway_list_keyboard,
    groups_keyboard,
)
from tg_drop_bot.bot.states import DraftStates, EditStates
from tg_drop_bot.config import Settings
from tg_drop_bot.services.access import can_manage_giveaway, is_admin
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
    list_available_groups,
    list_giveaways_by_status,
    publish_giveaway,
    replace_published_image,
    validate_draft,
)
from tg_drop_bot.services.rendering import render_giveaway_card, render_giveaway_post

router = Router(name="admin")


def admin_only(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id, settings)


@router.message(StateFilter(None), F.chat.type == "private", F.text == "Создать розыгрыш")
async def create_giveaway(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not admin_only(message, settings):
        return
    groups = await list_available_groups(session)
    if not groups:
        await message.answer(
            "Пока нет доступных групп. Добавьте бота в группу админом и повторите попытку.",
            reply_markup=admin_main_keyboard(),
        )
        return
    draft = await create_draft(session, message.from_user.id)
    await session.commit()
    await state.set_state(DraftStates.choosing_group)
    await state.update_data(giveaway_id=draft.id)
    await message.answer("Выберите группу для розыгрыша.", reply_markup=groups_keyboard(groups))


@router.callback_query(DraftStates.choosing_group, F.data.startswith("draft:group:"))
async def draft_group_selected(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    group_id = int(callback.data.split(":")[2])
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None:
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    giveaway.group_id = group_id
    await session.commit()
    await state.set_state(DraftStates.post_text)
    await callback.message.answer("Введите текст поста розыгрыша.")
    await callback.answer()


@router.message(DraftStates.post_text)
async def draft_post_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None or not message.text:
        await message.answer("Введите текст сообщением.")
        return
    giveaway.post_text = message.text.strip()
    await session.commit()
    await state.set_state(DraftStates.terms_text)
    await message.answer("Введите условия участия.")


@router.message(DraftStates.terms_text)
async def draft_terms_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if giveaway is None or not message.text:
        await message.answer("Введите условия текстом.")
        return
    giveaway.terms_text = message.text.strip()
    await session.commit()
    await state.set_state(DraftStates.image)
    await message.answer(
        "Отправьте картинку для поста или пропустите шаг.", reply_markup=draft_image_keyboard()
    )


@router.callback_query(DraftStates.image, F.data == "draft:image:skip")
async def draft_image_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(DraftStates.winners_count)
    await callback.message.answer("Сколько победителей выбрать?")
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
    await state.set_state(DraftStates.winners_count)
    await message.answer("Сколько победителей выбрать?")


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
    await state.set_state(DraftStates.deadline)
    await message.answer("Введите дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ, например 25.06.2026 18:30.")


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


@router.message(StateFilter(None), F.chat.type == "private", F.text == "Группы")
async def show_groups(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not admin_only(message, settings):
        return
    groups = await list_available_groups(session)
    if not groups:
        await message.answer("Доступных групп пока нет.")
        return
    text = "\n".join(f"- {group.title} ({group.telegram_chat_id})" for group in groups)
    await message.answer(text)


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
    parts = callback.data.split(":")
    field = parts[1].removeprefix("edit_")
    giveaway_id = int(parts[2])
    giveaway = await get_giveaway(session, giveaway_id)
    if giveaway is None or not can_manage_giveaway(callback.from_user.id, giveaway, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.update_data(giveaway_id=giveaway.id)
    prompts = {
        "text": ("Введите новый текст поста.", EditStates.text),
        "terms": ("Введите новые условия.", EditStates.terms),
        "deadline": ("Введите новый дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ.", EditStates.deadline),
        "winners": ("Введите новое количество победителей.", EditStates.winners),
        "image": ("Отправьте новую картинку.", EditStates.image),
    }
    if field == "image" and not giveaway.image_file_id:
        await callback.answer(
            "В v1 можно только заменить уже опубликованную картинку.", show_alert=True
        )
        return
    prompt, next_state = prompts[field]
    await state.set_state(next_state)
    await callback.message.answer(prompt)
    await callback.answer()


@router.message(EditStates.text)
async def edit_text(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    await update_text_field(message, state, session, settings, bot, "post_text", message.text or "")


@router.message(EditStates.terms)
async def edit_terms(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot
) -> None:
    await update_text_field(
        message, state, session, settings, bot, "terms_text", message.text or ""
    )


async def update_text_field(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    field: str,
    value: str,
) -> None:
    data = await state.get_data()
    giveaway = await get_giveaway(session, data["giveaway_id"])
    if (
        giveaway is None
        or message.from_user is None
        or not can_manage_giveaway(message.from_user.id, giveaway, settings)
    ):
        await message.answer("Нет доступа.")
        return
    setattr(giveaway, field, value.strip())
    await edit_published_message(bot, settings, giveaway)
    await add_audit(
        session,
        f"giveaway.edited.{field}",
        giveaway_id=giveaway.id,
        actor_user_id=message.from_user.id,
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
    await edit_published_message(bot, settings, giveaway)
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
    await edit_published_message(bot, settings, giveaway)
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
    await replace_published_image(bot, settings, giveaway, message.photo[-1].file_id)
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
