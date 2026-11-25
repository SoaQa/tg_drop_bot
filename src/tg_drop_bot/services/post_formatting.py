from __future__ import annotations

import html
from html.parser import HTMLParser
from typing import Any

from aiogram.types import Message, MessageEntity
from aiogram.utils.text_decorations import html_decoration

from tg_drop_bot.db.models import Giveaway


def description_from_message(message: Message) -> tuple[str, list[dict[str, Any]]]:
    if message.text is not None:
        text, entities = message.text, message.entities
    else:
        text, entities = message.caption or "", message.caption_entities
    return text, [entity.model_dump(mode="json", exclude_none=True) for entity in entities or []]


def render_description(giveaway: Giveaway) -> str:
    entities = [
        MessageEntity.model_validate(value) for value in giveaway.post_text_entities or []
    ]
    # Aiogram handles UTF-16 offsets and nested entities. Escape attribute values
    # separately; its HTML renderer already escapes the visible text.
    for entity in entities:
        for field in ("url", "language", "custom_emoji_id"):
            value = getattr(entity, field, None)
            if value is not None:
                setattr(entity, field, html.escape(value, quote=True))
    return html_decoration.unparse(giveaway.post_text or "", entities)


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.units = 0

    def handle_data(self, data: str) -> None:
        self.units += len(data.encode("utf-16-le")) // 2


def telegram_text_length(rendered_html: str) -> int:
    parser = _VisibleText()
    parser.feed(rendered_html)
    parser.close()
    return parser.units
