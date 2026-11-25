from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message, MessageEntity

from tg_drop_bot.bot import admin
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.giveaways import giveaway_post_length_error
from tg_drop_bot.services.post_formatting import (
    description_from_message,
    render_description,
    telegram_text_length,
)
from tg_drop_bot.services.rendering import render_giveaway_card, render_giveaway_post


def message(text: str, entities: list[MessageEntity], *, caption: bool = False) -> Message:
    return Message.model_validate({
        "message_id": 1, "date": 1,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "Администратор"},
        "caption" if caption else "text": text,
        "caption_entities" if caption else "entities": entities,
    })


@pytest.mark.parametrize("caption", [False, True])
def test_native_entities_preserve_nested_formatting_and_emoji_offsets(caption) -> None:
    original = "  😀 ссылка и секрет  "
    entities = [
        MessageEntity(type="bold", offset=2, length=9),
        MessageEntity(type="text_link", offset=5, length=6, url="https://example.com/?a=1&b=2"),
        MessageEntity(type="spoiler", offset=14, length=6),
    ]
    text, saved = description_from_message(message(original, entities, caption=caption))
    giveaway = Giveaway(post_text=text, post_text_entities=saved)
    rendered = render_description(giveaway)
    assert text == original
    assert '<b>😀 <a href="https://example.com/?a=1&amp;b=2">ссылка</a></b>' in rendered
    assert "<tg-spoiler>секрет</tg-spoiler>" in rendered
    assert telegram_text_length(rendered) == len(original.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("bold", "<b>Текст</b>"),
        ("italic", "<i>Текст</i>"),
        ("underline", "<u>Текст</u>"),
        ("strikethrough", "<s>Текст</s>"),
        ("spoiler", "<tg-spoiler>Текст</tg-spoiler>"),
        ("code", "<code>Текст</code>"),
        ("pre", "<pre>Текст</pre>"),
        ("blockquote", "<blockquote>Текст</blockquote>"),
        ("expandable_blockquote", "<blockquote expandable>Текст</blockquote>"),
    ],
)
def test_supported_native_formats(kind, expected) -> None:
    text, saved = description_from_message(message("Текст", [
        MessageEntity(type=kind, offset=0, length=5)
    ]))
    assert render_description(Giveaway(post_text=text, post_text_entities=saved)) == expected


@pytest.mark.parametrize("status", ["draft", "published", "finished", "cancelled"])
def test_description_formatting_survives_every_post_state_and_card(status) -> None:
    text, entities = description_from_message(message("ссылка", [
        MessageEntity(type="text_link", offset=0, length=6, url="https://example.com")
    ]))
    giveaway = Giveaway(id=1, title="Приз", status=status,
                        post_text=text, post_text_entities=entities)
    link = '<a href="https://example.com">ссылка</a>'
    assert link in render_giveaway_post(giveaway, Settings(), participants_count=100)
    assert link in render_giveaway_card(giveaway, Settings(), 100)
    giveaway.intake_closed_at = giveaway.deadline_at
    assert link in render_giveaway_post(giveaway, Settings(), closed=True)


def test_plain_text_cannot_inject_html_and_legacy_text_still_renders() -> None:
    giveaway = Giveaway(post_text="<b>не HTML</b> & текст")
    assert render_description(giveaway) == "&lt;b&gt;не HTML&lt;/b&gt; &amp; текст"
    giveaway.post_text_entities = []
    assert "<b>" not in render_description(giveaway)


def test_link_attributes_cannot_escape_html() -> None:
    text, saved = description_from_message(message("ссылка", [
        MessageEntity(type="text_link", offset=0, length=6, url='https://example.com/?q="x"&n=1')
    ]))
    assert render_description(Giveaway(post_text=text, post_text_entities=saved)) == (
        '<a href="https://example.com/?q=&quot;x&quot;&amp;n=1">ссылка</a>'
    )


def test_caption_limit_counts_visible_text_not_link_url_or_markup() -> None:
    text, saved = description_from_message(message("Купить", [
        MessageEntity(
            type="text_link", offset=0, length=6, url="https://example.com/" + "a" * 2000
        )
    ]))
    giveaway = Giveaway(
        title="Приз", post_text=text, post_text_entities=saved,
        image_file_id="photo", status="draft", winners_count=1,
    )
    assert len(render_giveaway_post(giveaway, Settings())) > 1024
    assert giveaway_post_length_error(giveaway, Settings()) is None
    giveaway.post_text = "a" * 1025
    giveaway.post_text_entities = []
    assert giveaway_post_length_error(giveaway, Settings()) is not None


def test_formatted_description_equal_to_title_is_not_dropped() -> None:
    giveaway = Giveaway(
        title="Приз", post_text="Приз",
        post_text_entities=[{"type": "strikethrough", "offset": 0, "length": 4}],
    )
    assert "<s>Приз</s>" in render_giveaway_post(giveaway, Settings())


@pytest.mark.parametrize("creation", [False, True])
async def test_admin_saves_entities_and_plain_edit_clears_them(monkeypatch, creation) -> None:
    giveaway = Giveaway(id=1, creator_user_id=1, status="draft", title="Приз",
                        post_text="Старый текст", post_text_entities=[{
                            "type": "bold", "offset": 0, "length": 5,
                        }])
    session = AsyncMock()
    state = AsyncMock(get_data=AsyncMock(return_value={"giveaway_id": 1}))
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=giveaway))
    monkeypatch.setattr(admin, "count_participants", AsyncMock(return_value=0))
    edit_post = AsyncMock()
    monkeypatch.setattr(admin, "edit_published_message", edit_post)
    monkeypatch.setattr(admin, "add_audit", AsyncMock())
    settings = Settings(ADMIN_IDS="1")
    for entities, expected in [
        ([MessageEntity(type="strikethrough", offset=0, length=5)], "<s>Текст</s>"),
        ([], "Текст"),
    ]:
        source = message("Текст", entities)
        incoming = SimpleNamespace(
            text=source.text, entities=source.entities, from_user=source.from_user,
            answer=AsyncMock(),
        )
        if creation:
            await admin.draft_post_text(incoming, state, session, settings)
        else:
            await admin.edit_text(incoming, state, session, settings, AsyncMock())
        assert render_description(giveaway) == expected
    assert session.commit.await_count == 2
