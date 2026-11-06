from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message

from tg_drop_bot.bot import admin
from tg_drop_bot.bot.keyboards import draft_preview_keyboard, giveaway_card_keyboard
from tg_drop_bot.bot.states import DraftStates
from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway, KnownChannel
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import PHOTO_CAPTION_LIMIT, giveaway_post_length_error
from tg_drop_bot.services.rendering import render_giveaway_post


@pytest.fixture
def draft() -> Giveaway:
    return Giveaway(
        id=5,
        creator_user_id=1,
        status="draft",
        title="Роутер",
        post_text="Актуальное описание",
        terms_text="Подписка на канал",
        channel_id=3,
        channel=KnownChannel(id=3, telegram_chat_id=-100123, title="Мой канал"),
        winners_count=1,
        deadline_at=utc_now() + timedelta(days=1),
    )


def callback(user_id: int = 1):
    return SimpleNamespace(
        data="draft:preview:5",
        from_user=SimpleNamespace(id=user_id),
        message=AsyncMock(
            spec=Message,
            answer=AsyncMock(),
            answer_photo=AsyncMock(),
            edit_reply_markup=AsyncMock(),
        ),
        answer=AsyncMock(),
    )


def callback_data(keyboard) -> list[str | None]:
    return [button.callback_data for row in keyboard.inline_keyboard for button in row]


@pytest.mark.parametrize("status", ["draft", "published", "finished", "cancelled"])
def test_launch_entry_is_only_available_for_drafts(draft, status) -> None:
    draft.status = status
    actions = callback_data(giveaway_card_keyboard(draft))
    assert ("draft:preview:5" in actions) == (status == "draft")
    assert "draft:publish:5" not in actions


def test_launch_menu_has_return_to_settings_and_requires_complete_draft() -> None:
    assert "giveaway:view:5" in callback_data(draft_preview_keyboard(5))
    assert "draft:publish:5" in callback_data(draft_preview_keyboard(5))
    assert "draft:publish:5" not in callback_data(draft_preview_keyboard(5, can_publish=False))


@pytest.mark.parametrize("with_photo", [False, True])
async def test_preview_uses_saved_values_without_publishing(monkeypatch, draft, with_photo) -> None:
    draft.image_file_id = "photo-id" if with_photo else None
    cb = callback()
    state = AsyncMock()
    session = AsyncMock()
    fetch = AsyncMock(return_value=draft)
    publish = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", fetch)
    monkeypatch.setattr(admin, "publish_giveaway", publish)

    await admin.draft_preview(cb, state, session, Settings(ADMIN_IDS="1"))

    fetch.assert_awaited_once_with(session, 5)
    state.clear.assert_awaited_once()
    publish.assert_not_awaited()
    session.commit.assert_not_awaited()
    if with_photo:
        preview = cb.message.answer_photo.call_args.kwargs["caption"]
    else:
        preview = cb.message.answer.call_args_list[0].args[0]
    assert "Актуальное описание" in preview
    menu = cb.message.answer.call_args
    assert "Мой канал" in menu.args[0]
    assert "draft:publish:5" in callback_data(menu.kwargs["reply_markup"])


async def test_incomplete_preview_explains_missing_fields(draft) -> None:
    draft.terms_text = None
    message = AsyncMock()
    await admin.show_draft_preview(message, draft, Settings())
    menu = message.answer.call_args
    assert "условия" in menu.args[0]
    assert "draft:publish:5" not in callback_data(menu.kwargs["reply_markup"])
    assert "giveaway:view:5" in callback_data(menu.kwargs["reply_markup"])


def test_photo_caption_reserves_room_for_participant_count_and_deadline(draft) -> None:
    settings = Settings()
    draft.image_file_id = "photo-id"
    draft.post_text = "а"
    size = len(render_giveaway_post(draft, settings, participants_count=2**63 - 1))
    draft.post_text += "а" * (PHOTO_CAPTION_LIMIT - size)
    assert giveaway_post_length_error(draft, settings) is None
    deadline = draft.deadline_at
    draft.deadline_at = None
    assert giveaway_post_length_error(draft, settings) is None
    draft.deadline_at = deadline
    draft.post_text += "а"
    assert len(render_giveaway_post(draft, settings, participants_count=0)) < PHOTO_CAPTION_LIMIT
    assert giveaway_post_length_error(draft, settings)
    draft.deadline_at = None
    assert giveaway_post_length_error(draft, settings)
    draft.image_file_id = None
    assert giveaway_post_length_error(draft, settings) is None


async def test_long_photo_caption_blocks_preview_and_publish_button(draft) -> None:
    draft.image_file_id = "photo-id"
    draft.post_text = "а" * PHOTO_CAPTION_LIMIT
    message = AsyncMock()

    await admin.show_draft_preview(message, draft, Settings())

    message.answer_photo.assert_not_awaited()
    message.answer.assert_awaited_once()
    menu = message.answer.call_args
    assert "Подпись к картинке слишком длинная" in menu.args[0]
    assert "draft:publish:5" not in callback_data(menu.kwargs["reply_markup"])


@pytest.mark.parametrize("too_long", [False, True])
async def test_draft_image_checks_caption_before_saving(monkeypatch, draft, too_long) -> None:
    if too_long:
        draft.post_text = "а" * PHOTO_CAPTION_LIMIT
    message = AsyncMock(photo=[SimpleNamespace(file_id="photo-id")])
    state = AsyncMock(get_data=AsyncMock(return_value={"giveaway_id": 5}))
    session = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))

    await admin.draft_image(message, state, session, Settings())

    if too_long:
        assert draft.image_file_id is None
        session.commit.assert_not_awaited()
        state.set_state.assert_not_awaited()
        buttons = callback_data(message.answer.call_args.kwargs["reply_markup"])
        assert "draft:image:edit_description" in buttons
        assert "draft:image:skip" in buttons
    else:
        assert draft.image_file_id == "photo-id"
        session.commit.assert_awaited_once()
        state.set_state.assert_awaited_once_with(DraftStates.deadline)


@pytest.mark.parametrize("status", ["draft", "published"])
async def test_long_photo_caption_is_not_saved_on_edit(monkeypatch, draft, status) -> None:
    draft.status = status
    draft.image_file_id = "photo-id"
    message = AsyncMock(
        from_user=SimpleNamespace(id=1), text="а" * PHOTO_CAPTION_LIMIT
    )
    state = AsyncMock(get_data=AsyncMock(return_value={"giveaway_id": 5}))
    session = AsyncMock()
    update_post = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))
    monkeypatch.setattr(admin, "count_participants", AsyncMock(return_value=1))
    monkeypatch.setattr(admin, "edit_published_message", update_post)

    await admin.edit_text(message, state, session, Settings(ADMIN_IDS="1"), AsyncMock())

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    update_post.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert "Подпись к картинке слишком длинная" in message.answer.call_args.args[0]


@pytest.mark.parametrize("status", ["published", "finished", "cancelled"])
async def test_stale_preview_is_rejected(monkeypatch, draft, status) -> None:
    draft.status = status
    cb = callback()
    state = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))
    await admin.draft_preview(cb, state, AsyncMock(), Settings(ADMIN_IDS="1"))
    cb.message.answer.assert_not_awaited()
    state.clear.assert_not_awaited()
    assert cb.answer.call_args.kwargs["show_alert"]


@pytest.mark.parametrize("user_id", [2, 99])
async def test_preview_rejects_other_owner_or_non_admin(monkeypatch, draft, user_id) -> None:
    cb = callback(user_id)
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))
    await admin.draft_preview(cb, AsyncMock(), AsyncMock(), Settings(ADMIN_IDS="1,2"))
    cb.message.answer.assert_not_awaited()
    assert cb.answer.call_args.args[0] == "Нет доступа."


async def test_publish_reloads_draft_then_removes_confirmation(monkeypatch, draft) -> None:
    cb = callback()
    cb.data = "draft:publish:5"
    session = AsyncMock()
    bot = AsyncMock()
    settings = Settings(ADMIN_IDS="1")
    fetch = AsyncMock(return_value=draft)
    publish = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", fetch)
    monkeypatch.setattr(admin, "publish_giveaway", publish)

    await admin.draft_publish(cb, session, settings, bot)

    fetch.assert_awaited_once_with(session, 5)
    publish.assert_awaited_once_with(session, bot, settings, draft, 1)
    cb.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.parametrize("status", ["published", "finished", "cancelled"])
async def test_old_publish_buttons_cannot_republish(monkeypatch, draft, status) -> None:
    draft.status = status
    cb = callback()
    cb.data = "draft:publish:5"
    publish = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))
    monkeypatch.setattr(admin, "publish_giveaway", publish)
    await admin.draft_publish(cb, AsyncMock(), Settings(ADMIN_IDS="1"), AsyncMock())
    publish.assert_not_awaited()
    assert cb.answer.call_args.kwargs["show_alert"]


async def test_publish_revalidates_settings_changed_since_preview(monkeypatch, draft) -> None:
    draft.terms_text = None
    cb = callback()
    cb.data = "draft:publish:5"
    publish = AsyncMock()
    monkeypatch.setattr(admin, "get_giveaway", AsyncMock(return_value=draft))
    monkeypatch.setattr(admin, "publish_giveaway", publish)
    await admin.draft_publish(cb, AsyncMock(), Settings(ADMIN_IDS="1"), AsyncMock())
    publish.assert_not_awaited()
    assert "условия" in cb.answer.call_args.args[0]
