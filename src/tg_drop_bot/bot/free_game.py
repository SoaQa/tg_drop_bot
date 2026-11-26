from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.config import Settings

router = Router(name="free_game")
top_router = Router(name="hard_top")


@top_router.message(Command("top", "Top"))
async def show_top(
    message: Message, session: AsyncSession, settings: Settings | None = None,
) -> None:
    from tg_drop_bot.services.hard_game import leaderboard_text

    await message.answer(await leaderboard_text(
        session, message.chat.id if message.chat.type != 'private' else None,
        mode_selection=bool(settings and settings.frog_mode_selection_enabled),
    ))


@router.message(Command("game"))
@router.message(CommandStart(deep_link=True, magic=F.args == "game"))
async def open_free_game(message: Message, settings: Settings, bot: Bot) -> None:
    if message.chat.type == "private":
        button = InlineKeyboardButton(
            text="Играть", web_app=WebAppInfo(
                url=f"{settings.webhook_url}{settings.free_game_path}/",
            ),
        )
    else:
        me = await bot.get_me()
        button = InlineKeyboardButton(text="Играть", url=f"https://t.me/{me.username}?start=game")
    await message.answer(
        "Помогите лягушке перейти дорогу! Тап или клик делает прыжок вперёд. "
        "Можно играть сколько угодно, это отдельная мини-игра.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]),
    )
