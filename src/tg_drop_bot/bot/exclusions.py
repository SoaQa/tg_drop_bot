from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.bot.keyboards import parse_callback_int as parse_callback_integer
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import ChannelExclusion, KnownChannel, Participant
from tg_drop_bot.services.access import can_manage_giveaway, is_admin
from tg_drop_bot.services.giveaways import get_giveaway
from tg_drop_bot.services.membership import prohibit_participation, revoke_channel_exclusion

router = Router(name="exclusions")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
PAGE_SIZE = 20


def parse_callback_int(data: str | None, index: int) -> int | None:
    value = parse_callback_integer(data, index)
    return value if value is not None and value <= 2_147_483_647 else None


class ExclusionSearch(StatesGroup):
    query = State()
    prohibit_id = State()
    confirm_prohibition = State()


@router.message(StateFilter(None), F.text == "Исключения")
async def exclusions_menu(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    channels = (await session.scalars(select(KnownChannel).order_by(KnownChannel.title))).all()
    keyboard = InlineKeyboardBuilder()
    for channel in channels:
        keyboard.button(text=channel.title, callback_data=f"anti:list:{channel.id}:0")
    keyboard.adjust(1)
    await message.answer(
        "Выберите канал." if channels else "Каналов пока нет.", reply_markup=keyboard.as_markup()
    )


async def show_exclusions(
    message: Message,
    session: AsyncSession,
    channel_id: int,
    page: int,
    user_ids: list[int] | None = None,
) -> None:
    channel = await session.get(KnownChannel, channel_id)
    if channel is None:
        await message.answer("Канал не найден.")
        return
    query = select(ChannelExclusion).where(
        ChannelExclusion.channel_id == channel_id, ChannelExclusion.revoked_at.is_(None)
    )
    if user_ids is not None:
        query = query.where(ChannelExclusion.user_id.in_(user_ids))
    entries = (
        await session.scalars(
            query.order_by(ChannelExclusion.id.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
        )
    ).all()
    await message.answer(f"Исключения: {html.escape(channel.title)}. Страница {page + 1}.")
    for entry in entries[:PAGE_SIZE]:
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="Снять исключение", callback_data=f"anti:revoke:{entry.id}")
        await message.answer(
            f"Telegram ID: <code>{entry.user_id}</code>\n"
            f"Причина: {html.escape(entry.reason)}\n"
            f"Добавлено: {entry.created_at.isoformat()}",
            reply_markup=keyboard.as_markup(),
        )
    if not entries:
        await message.answer("Действующих исключений не найдено.")
    keyboard = InlineKeyboardBuilder()
    if page and user_ids is None:
        keyboard.button(text="Назад", callback_data=f"anti:list:{channel_id}:{page - 1}")
    if len(entries) > PAGE_SIZE and user_ids is None:
        keyboard.button(text="Далее", callback_data=f"anti:list:{channel_id}:{page + 1}")
    keyboard.button(text="Поиск по ID или имени", callback_data=f"anti:search:{channel_id}")
    keyboard.button(text="Запретить участие", callback_data=f"anti:prohibit:{channel_id}")
    await message.answer("Действия", reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("anti:list:"))
async def exclusions_list(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not is_admin(callback.from_user.id, settings) or not isinstance(callback.message, Message):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channel_id, page = parse_callback_int(callback.data, 2), parse_callback_int(callback.data, 3)
    if channel_id is None or page is None:
        await callback.answer("Кнопка устарела.")
        return
    await callback.answer()
    await show_exclusions(callback.message, session, channel_id, page)


@router.callback_query(F.data.startswith("anti:search:"))
async def search_start(callback: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not is_admin(callback.from_user.id, settings) or not isinstance(callback.message, Message):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channel_id = parse_callback_int(callback.data, 2)
    if channel_id is None:
        await callback.answer("Кнопка устарела.")
        return
    await state.set_state(ExclusionSearch.query)
    await state.update_data(exclusion_channel_id=channel_id)
    await callback.answer()
    await callback.message.answer("Введите Telegram ID или @имя пользователя.")


@router.message(ExclusionSearch.query)
async def search_submit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    query = (message.text or "").strip().lstrip("@")
    if not query:
        await message.answer("Введите Telegram ID или @имя пользователя.")
        return
    data = await state.get_data()
    if query.isascii() and query.isdigit():
        ids = [int(query)] if len(query) <= 18 else []
    else:
        ids = list(
            (
                await session.scalars(
                    select(Participant.user_id)
                    .where(func.lower(Participant.username) == query.lower())
                    .distinct()
                )
            ).all()
        )
    await state.clear()
    await show_exclusions(message, session, data["exclusion_channel_id"], 0, ids)


@router.callback_query(F.data.startswith("anti:prohibit:"))
async def prohibit_start(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings) or not isinstance(callback.message, Message):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    channel_id = parse_callback_int(callback.data, 2)
    if channel_id is None or await session.get(KnownChannel, channel_id) is None:
        await callback.answer("Канал не найден.", show_alert=True)
        return
    await state.set_state(ExclusionSearch.prohibit_id)
    await state.update_data(prohibit_channel_id=channel_id)
    await callback.answer()
    await callback.message.answer("Введите Telegram ID пользователя, которому запрещаете участие.")


@router.message(ExclusionSearch.prohibit_id)
async def prohibit_preview(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    value = (message.text or "").strip()
    if not (value.isascii() and value.isdecimal() and len(value) <= 16):
        await message.answer("Введите числовой Telegram ID.")
        return
    user_id = int(value)
    if not 0 < user_id < 2**52:
        await message.answer("Введите корректный Telegram ID.")
        return
    data = await state.get_data()
    channel_id = data.get("prohibit_channel_id")
    if channel_id is None:
        await state.clear()
        await message.answer("Выберите канал заново.")
        return
    await state.set_state(ExclusionSearch.confirm_prohibition)
    await state.update_data(prohibit_user_id=user_id)
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Да, запретить", callback_data=f"anti:block:{channel_id}:{user_id}")
    keyboard.button(text="Отмена", callback_data="anti:block_cancel")
    await message.answer(
        f"Запретить участие пользователю <code>{user_id}</code> в выбранном канале?",
        reply_markup=keyboard.as_markup(),
    )


@router.callback_query(ExclusionSearch.confirm_prohibition, F.data == "anti:block_cancel")
async def prohibit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Действие отменено.")


@router.callback_query(ExclusionSearch.confirm_prohibition, F.data.startswith("anti:block:"))
async def prohibit_confirm(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    data = await state.get_data()
    channel_id, user_id = data.get("prohibit_channel_id"), data.get("prohibit_user_id")
    if (
        channel_id is None
        or user_id is None
        or callback.data != f"anti:block:{channel_id}:{user_id}"
    ):
        await callback.answer("Кнопка устарела.")
        return
    changed = await prohibit_participation(session, channel_id, user_id, callback.from_user.id)
    await session.commit()
    await state.clear()
    await callback.answer(
        "Участие запрещено." if changed else "Запрет уже действует.", show_alert=True
    )


@router.callback_query(F.data.startswith("anti:revoke:"))
async def revoke_start(
    callback: CallbackQuery,
    settings: Settings,
    session: AsyncSession,
) -> None:
    if not is_admin(callback.from_user.id, settings) or not isinstance(callback.message, Message):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    entry_id = parse_callback_int(callback.data, 2)
    entry = await session.get(ChannelExclusion, entry_id) if entry_id is not None else None
    if entry is None or entry.revoked_at is not None:
        await callback.answer("Исключение уже снято или не найдено.")
        return
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="Да, снять", callback_data=f"anti:confirm:{entry.id}")
    keyboard.button(text="Назад", callback_data=f"anti:list:{entry.channel_id}:0")
    await callback.answer()
    await callback.message.answer(
        f"Снять исключение для {entry.user_id}?", reply_markup=keyboard.as_markup()
    )


@router.callback_query(F.data.startswith("anti:confirm:"))
async def revoke_confirm(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    if not is_admin(callback.from_user.id, settings):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    entry_id = parse_callback_int(callback.data, 2)
    changed = entry_id is not None and await revoke_channel_exclusion(
        session, entry_id, callback.from_user.id
    )
    await session.commit()
    await callback.answer(
        "Исключение снято." if changed else "Исключение уже снято.", show_alert=True
    )


@router.callback_query(F.data.startswith("anti:giveaway:"))
async def giveaway_protection(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
) -> None:
    giveaway_id = parse_callback_int(callback.data, 2)
    giveaway = await get_giveaway(session, giveaway_id) if giveaway_id is not None else None
    if (
        giveaway is None
        or not can_manage_giveaway(callback.from_user.id, giveaway, settings)
        or not isinstance(callback.message, Message)
    ):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    rows = (
        await session.execute(
            select(
                Participant.selection_weight, Participant.draw_excluded, func.count(Participant.id)
            )
            .where(Participant.giveaway_id == giveaway.id)
            .group_by(Participant.selection_weight, Participant.draw_excluded)
        )
    ).all()
    weight_rule = (
        "Вес новых подписчиков: 0,7. Остальных: 1."
        if giveaway.anti_abuse_enabled
        else "Равные шансы: вес всех участников 1."
    )
    lines = ["Защита розыгрыша", weight_rule]
    for weight, excluded, count in rows:
        lines.append(f"Вес {weight}, {'исключены' if excluded else 'участники'}: {count}")
    channel_exclusions = await session.scalar(
        select(func.count(ChannelExclusion.id)).where(
            ChannelExclusion.channel_id == giveaway.channel_id,
            ChannelExclusion.revoked_at.is_(None),
        )
    )
    lines.append(f"Действующих постоянных исключений в канале: {channel_exclusions or 0}")
    lines.append(
        "Показаны сохранённые значения. Окончательный допуск определяется при подведении итогов."
    )
    await callback.answer()
    await callback.message.answer("\n".join(lines))
