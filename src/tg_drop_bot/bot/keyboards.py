from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from tg_drop_bot.db.models import Giveaway, KnownChannel
from tg_drop_bot.services.conditions import CHANNEL_SUBSCRIPTION_CONDITION
from tg_drop_bot.services.rendering import giveaway_status_label

CHANNEL_REQUEST_ID = 1


def admin_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Создать розыгрыш")],
            [KeyboardButton(text="Активные"), KeyboardButton(text="Черновики")],
            [KeyboardButton(text="Завершенные"), KeyboardButton(text="Отмененные")],
            [KeyboardButton(text="Каналы")],
        ],
        resize_keyboard=True,
    )


def channel_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="Выбрать канал",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=CHANNEL_REQUEST_ID,
                        chat_is_channel=True,
                        bot_is_member=True,
                        request_title=True,
                        request_username=True,
                    ),
                )
            ],
            [KeyboardButton(text="Создать розыгрыш"), KeyboardButton(text="Каналы")],
        ],
        resize_keyboard=True,
    )


def channels_keyboard(channels: list[KnownChannel]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for channel in channels:
        builder.button(text=channel.title, callback_data=f"draft:channel:{channel.id}")
    builder.adjust(1)
    return builder.as_markup()


def draft_image_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить", callback_data="draft:image:skip")]
        ]
    )


def draft_conditions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Подписчики канала",
                    callback_data=f"draft:condition:{CHANNEL_SUBSCRIPTION_CONDITION}",
                )
            ]
        ]
    )


def draft_preview_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Опубликовать", callback_data=f"draft:publish:{giveaway_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Удалить черновик", callback_data=f"draft:delete:{giveaway_id}"
                )
            ],
        ]
    )


def giveaway_list_keyboard(giveaways: list[Giveaway]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for giveaway in giveaways:
        title = giveaway.title or "без названия"
        label = f"#{giveaway.id} - {title} - {giveaway_status_label(giveaway.status)}"
        builder.button(text=label, callback_data=f"giveaway:view:{giveaway.id}")
    builder.adjust(1)
    return builder.as_markup()


def giveaway_card_keyboard(giveaway: Giveaway) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="Список участников", callback_data=f"giveaway:csv:{giveaway.id}"
            ),
            InlineKeyboardButton(
                text="Название", callback_data=f"giveaway:edit_title:{giveaway.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Описание", callback_data=f"giveaway:edit_text:{giveaway.id}"
            ),
            InlineKeyboardButton(
                text="Условие участия", callback_data=f"giveaway:edit_terms:{giveaway.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Кол-во победителей", callback_data=f"giveaway:edit_winners:{giveaway.id}"
            ),
            InlineKeyboardButton(
                text="Дата завершения", callback_data=f"giveaway:edit_deadline:{giveaway.id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="Изменить картинку", callback_data=f"giveaway:edit_image:{giveaway.id}"
            ),
        ],
    ]
    if giveaway.status == "published":
        rows.append(
            [
                InlineKeyboardButton(
                    text="Отменить розыгрыш", callback_data=f"giveaway:cancel:{giveaway.id}"
                ),
                InlineKeyboardButton(
                    text="Завершить сейчас", callback_data=f"giveaway:finish:{giveaway.id}"
                ),
            ]
        )
    elif giveaway.status == "draft":
        rows.append(
            [
                InlineKeyboardButton(
                    text="Удалить черновик", callback_data=f"draft:delete:{giveaway.id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def active_giveaways_keyboard(giveaways: list[Giveaway]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for giveaway in giveaways:
        title = giveaway.title or f"Розыгрыш #{giveaway.id}"
        label = f"Участвовать: {title}"
        if giveaway.channel is not None:
            label = f"{label} - {giveaway.channel.title}"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"participate:{giveaway.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def captcha_keyboard(challenge_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Обновить капчу",
                    callback_data=f"captcha:refresh:{challenge_id}",
                )
            ]
        ]
    )


def confirm_keyboard(action: str, giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, подтверждаю", callback_data=f"giveaway:{action}_confirm:{giveaway_id}"
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data=f"giveaway:view:{giveaway_id}")],
        ]
    )


def participation_keyboard(
    giveaway_id: int, bot_username: str | None = None
) -> InlineKeyboardMarkup:
    if bot_username:
        button = InlineKeyboardButton(
            text="Участвовать",
            url=f"https://t.me/{bot_username}?start=giveaway_{giveaway_id}",
        )
    else:
        button = InlineKeyboardButton(
            text="Участвовать", callback_data=f"participate:{giveaway_id}"
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
