from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import cast

from aiogram import Bot
from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from tg_drop_bot.db.models import Giveaway, KnownChannel, Participant
from tg_drop_bot.services import giveaways
from tg_drop_bot.services.conditions import ConditionCheck
from tg_drop_bot.services.dates import utc_now
from tg_drop_bot.services.giveaways import (
    pick_winners,
    register_participant_after_condition_check,
    single_giveaway_or_none,
    validate_draft,
)


def test_validate_draft_reports_missing_fields() -> None:
    giveaway = Giveaway(creator_user_id=1)
    assert validate_draft(giveaway) == [
        "канал",
        "название",
        "описание",
        "условия",
        "количество победителей",
        "дедлайн",
    ]


def test_pick_winners_uses_available_participants_when_shortage() -> None:
    participants = [
        Participant(
            giveaway_id=1,
            user_id=1,
            joined_at=utc_now(),
            captcha_passed_at=utc_now(),
        ),
        Participant(
            giveaway_id=1,
            user_id=2,
            joined_at=utc_now() + timedelta(seconds=1),
            captcha_passed_at=utc_now() + timedelta(seconds=1),
        ),
    ]
    winners = pick_winners(participants, 5)
    assert sorted(participant.user_id for participant in winners) == [1, 2]


def test_single_giveaway_or_none_returns_only_unambiguous_giveaway() -> None:
    giveaway = Giveaway(id=7, creator_user_id=1)

    assert single_giveaway_or_none([giveaway]) is giveaway
    assert single_giveaway_or_none([]) is None
    assert (
        single_giveaway_or_none(
            [giveaway, Giveaway(id=8, creator_user_id=1)]
        )
        is None
    )


async def test_register_participant_after_condition_check_rejects_non_subscriber(
    monkeypatch,
) -> None:
    async def fake_check_participant_conditions(
        bot: Bot,
        giveaway: Giveaway,
        user_id: int,
    ) -> ConditionCheck:
        return ConditionCheck(
            ok=False,
            membership_status="left",
            user_message="Участвовать могут только подписчики канала розыгрыша.",
        )

    async def fake_register_participant(
        session: AsyncSession,
        giveaway: Giveaway,
        user: User,
    ) -> tuple[Participant, bool]:
        raise AssertionError("Неподписанного пользователя нельзя регистрировать")

    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        fake_check_participant_conditions,
    )
    monkeypatch.setattr(giveaways, "register_participant", fake_register_participant)

    result = await register_participant_after_condition_check(
        cast(AsyncSession, object()),
        cast(Bot, object()),
        Giveaway(id=1, creator_user_id=1),
        cast(User, SimpleNamespace(id=42)),
    )

    assert result.participant is None
    assert result.created is False
    assert result.condition_check.membership_status == "left"


async def test_register_participant_after_condition_check_keeps_membership_status(
    monkeypatch,
) -> None:
    participant = Participant(
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
        membership_status="member",
    )

    async def fake_check_participant_conditions(
        bot: Bot,
        giveaway: Giveaway,
        user_id: int,
    ) -> ConditionCheck:
        return ConditionCheck(ok=True, membership_status="administrator")

    async def fake_register_participant(
        session: AsyncSession,
        giveaway: Giveaway,
        user: User,
        *,
        received_at: datetime | None = None,
    ) -> tuple[Participant, bool]:
        assert received_at is None
        return participant, True

    monkeypatch.setattr(
        giveaways,
        "check_participant_conditions",
        fake_check_participant_conditions,
    )
    monkeypatch.setattr(giveaways, "register_participant", fake_register_participant)

    result = await register_participant_after_condition_check(
        cast(AsyncSession, object()),
        cast(Bot, object()),
        Giveaway(id=1, creator_user_id=1),
        cast(User, SimpleNamespace(id=42)),
    )

    assert result.participant is participant
    assert result.created is True
    assert participant.membership_status == "administrator"
    assert participant.membership_checked_at is not None


class FakeSession:
    def __init__(self, execute_result=None) -> None:
        self.added: list[object] = []
        self.log: list[str] = []
        self._execute_result = execute_result

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.log.append("commit")

    async def rollback(self) -> None:
        self.log.append("rollback")

    async def flush(self) -> None:
        self.log.append("flush")

    async def execute(self, query):
        self.log.append("execute")
        return self._execute_result


def make_channel() -> KnownChannel:
    return KnownChannel(
        id=1, telegram_chat_id=-1001234, title="Канал", username="channel", bot_is_admin=True
    )


def test_is_giveaway_open_requires_published_status_and_future_deadline() -> None:
    from tg_drop_bot.services.giveaways import is_giveaway_open

    open_giveaway = Giveaway(
        id=1,
        creator_user_id=1,
        status="published",
        deadline_at=utc_now() + timedelta(hours=1),
    )
    open_giveaway.channel = make_channel()
    assert is_giveaway_open(open_giveaway)

    overdue = Giveaway(
        id=2,
        creator_user_id=1,
        status="published",
        deadline_at=utc_now() - timedelta(seconds=1),
    )
    overdue.channel = make_channel()
    assert not is_giveaway_open(overdue)

    draft = Giveaway(
        id=3,
        creator_user_id=1,
        status="draft",
        deadline_at=utc_now() + timedelta(hours=1),
    )
    draft.channel = make_channel()
    assert not is_giveaway_open(draft)


async def test_finish_giveaway_returns_early_when_claim_lost(monkeypatch) -> None:
    from tg_drop_bot.config import Settings

    async def fake_transition(session, giveaway, *, expected_status, **values):
        return False

    async def must_not_run(*args, **kwargs):
        raise AssertionError("Side effects must not run without a successful claim")

    monkeypatch.setattr(giveaways, "try_status_transition", fake_transition)
    monkeypatch.setattr(giveaways, "announce_results_preparing", must_not_run)
    monkeypatch.setattr(giveaways, "draw_and_record_winners", must_not_run)
    monkeypatch.setattr(giveaways, "publish_giveaway_results", must_not_run)

    session = FakeSession()
    giveaway = Giveaway(id=1, creator_user_id=1, status="published", winners_count=1)
    giveaway.channel = make_channel()

    result = await giveaways.finish_giveaway(
        cast(AsyncSession, session),
        cast(Bot, object()),
        Settings(),
        giveaway,
        actor_user_id=None,
        manual=False,
    )

    assert result.finished is False
    assert result.winners == []
    assert "rollback" in session.log
    assert "commit" not in session.log


async def test_finish_giveaway_commits_claim_before_side_effects(monkeypatch) -> None:
    from tg_drop_bot.config import Settings

    order: list[str] = []

    async def fake_transition(session, giveaway, *, expected_status, **values):
        assert expected_status == "published"
        assert values["status"] == "finished"
        assert values["results_state"] == "pending"
        order.append("claim")
        return True

    async def fake_announce(session, bot, giveaway):
        order.append("announce_preparing")

    async def fake_draw(session, bot, giveaway):
        order.append("draw")
        return []

    async def fake_publish(session, bot, settings, giveaway, winners):
        order.append("publish_results")

    monkeypatch.setattr(giveaways, "try_status_transition", fake_transition)
    monkeypatch.setattr(giveaways, "announce_results_preparing", fake_announce)
    monkeypatch.setattr(giveaways, "draw_and_record_winners", fake_draw)
    monkeypatch.setattr(giveaways, "publish_giveaway_results", fake_publish)

    session = FakeSession()
    original_commit = session.commit

    async def tracked_commit() -> None:
        await original_commit()
        order.append("commit")

    session.commit = tracked_commit  # type: ignore[method-assign]

    giveaway = Giveaway(id=1, creator_user_id=1, status="published", winners_count=1)
    giveaway.channel = make_channel()

    result = await giveaways.finish_giveaway(
        cast(AsyncSession, session),
        cast(Bot, object()),
        Settings(),
        giveaway,
        actor_user_id=10,
        manual=True,
    )

    assert result.finished is True
    assert order == ["claim", "commit", "announce_preparing", "draw", "publish_results"]


async def test_draw_keeps_participants_when_membership_check_fails(monkeypatch) -> None:
    participant = Participant(
        id=5,
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )
    check_calls: list[int] = []

    async def fake_list_participants(session, giveaway_id):
        return [participant]

    async def fake_check(bot, giveaway, user_id):
        check_calls.append(user_id)
        return ConditionCheck(ok=False, membership_status="check_failed")

    async def fake_state_transition(session, giveaway, *, expected_state, new_state):
        giveaway.results_state = new_state
        return True

    monkeypatch.setattr(giveaways, "list_participants", fake_list_participants)
    monkeypatch.setattr(giveaways, "check_participant_conditions", fake_check)
    monkeypatch.setattr(giveaways, "try_results_state_transition", fake_state_transition)

    session = FakeSession()
    giveaway = Giveaway(
        id=1, creator_user_id=1, status="finished", winners_count=1, results_state="pending"
    )
    giveaway.channel = make_channel()

    selected = await giveaways.draw_and_record_winners(
        cast(AsyncSession, session), cast(Bot, object()), giveaway
    )

    assert selected == [participant]
    assert giveaway.results_state == "drawn"
    # Проверка повторяется один раз перед тем, как оставить участника в розыгрыше.
    assert check_calls == [42, 42]


async def test_draw_excludes_unsubscribed_participants(monkeypatch) -> None:
    participant = Participant(
        id=5,
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )

    async def fake_list_participants(session, giveaway_id):
        return [participant]

    async def fake_check(bot, giveaway, user_id):
        return ConditionCheck(ok=False, membership_status="left")

    async def fake_state_transition(session, giveaway, *, expected_state, new_state):
        giveaway.results_state = new_state
        return True

    monkeypatch.setattr(giveaways, "list_participants", fake_list_participants)
    monkeypatch.setattr(giveaways, "check_participant_conditions", fake_check)
    monkeypatch.setattr(giveaways, "try_results_state_transition", fake_state_transition)

    session = FakeSession()
    giveaway = Giveaway(
        id=1, creator_user_id=1, status="finished", winners_count=1, results_state="pending"
    )
    giveaway.channel = make_channel()

    selected = await giveaways.draw_and_record_winners(
        cast(AsyncSession, session), cast(Bot, object()), giveaway
    )

    assert selected == []
    assert participant.membership_status == "left"


async def test_draw_lost_race_returns_persisted_winners(monkeypatch) -> None:
    """Медленный live-финиш проигрывает гонку дозапуску: без второго тиража."""
    participant = Participant(
        id=5,
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )
    persisted_winner = Participant(
        id=6,
        giveaway_id=1,
        user_id=99,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )

    async def fake_list_participants(session, giveaway_id):
        return [participant]

    async def fake_check(bot, giveaway, user_id):
        return ConditionCheck(ok=True, membership_status="member")

    async def fake_state_transition(session, giveaway, *, expected_state, new_state):
        return False

    async def fake_list_winner_participants(session, giveaway_id):
        return [persisted_winner]

    monkeypatch.setattr(giveaways, "list_participants", fake_list_participants)
    monkeypatch.setattr(giveaways, "check_participant_conditions", fake_check)
    monkeypatch.setattr(giveaways, "try_results_state_transition", fake_state_transition)
    monkeypatch.setattr(giveaways, "list_winner_participants", fake_list_winner_participants)

    session = FakeSession()
    giveaway = Giveaway(
        id=1, creator_user_id=1, status="finished", winners_count=1, results_state="drawn"
    )
    giveaway.channel = make_channel()

    selected = await giveaways.draw_and_record_winners(
        cast(AsyncSession, session), cast(Bot, object()), giveaway
    )

    assert selected == [persisted_winner]
    assert "rollback" in session.log
    assert not any(isinstance(obj, giveaways.Winner) for obj in session.added)


async def test_resume_unfinished_results_retries_announcement(monkeypatch) -> None:
    from tg_drop_bot.config import Settings

    giveaway = Giveaway(
        id=1,
        creator_user_id=1,
        status="finished",
        results_state="drawn",
        winners_count=1,
        finished_at=utc_now(),
    )
    giveaway.channel = make_channel()

    class FakeScalars:
        def all(self):
            return [giveaway]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    participant = Participant(
        id=5,
        giveaway_id=1,
        user_id=42,
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )
    published: list[tuple[int, list[Participant]]] = []

    async def fake_list_winner_participants(session, giveaway_id):
        return [participant]

    async def fake_publish(session, bot, settings, giveaway, winners, **kwargs):
        # Дозапуск не должен спамить организатора при повторных неудачах.
        assert kwargs.get("notify_creator_on_failure") is False
        published.append((giveaway.id, winners))

    monkeypatch.setattr(giveaways, "list_winner_participants", fake_list_winner_participants)
    monkeypatch.setattr(giveaways, "publish_giveaway_results", fake_publish)

    session = FakeSession(execute_result=FakeResult())

    resumed = await giveaways.resume_unfinished_results(
        cast(AsyncSession, session), cast(Bot, object()), Settings()
    )

    assert resumed == [1]
    assert published == [(1, [participant])]


async def test_notify_creator_reports_announcement_failure() -> None:
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            sent.append((chat_id, text))

    giveaway = Giveaway(
        id=1,
        creator_user_id=77,
        status="finished",
        title="Приз",
        result_message_id=99,
    )
    giveaway.channel = make_channel()

    await giveaways.notify_creator_about_results(
        cast(Bot, FakeBot()), giveaway, [], announced=False
    )

    assert sent[0][0] == 77
    assert "не удалось" in sent[0][1].lower()
    assert "автоматически" in sent[0][1]


async def test_notify_creator_links_to_results_post() -> None:
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
            sent.append((chat_id, text))

    participant = Participant(
        id=5,
        giveaway_id=1,
        user_id=42,
        username="winner",
        joined_at=utc_now(),
        captcha_passed_at=utc_now(),
    )
    giveaway = Giveaway(
        id=1,
        creator_user_id=77,
        status="finished",
        title="Приз",
        result_message_id=99,
    )
    giveaway.channel = make_channel()

    await giveaways.notify_creator_about_results(
        cast(Bot, FakeBot()), giveaway, [participant], announced=True
    )

    assert "Победителей: 1." in sent[0][1]
    assert "https://t.me/channel/99" in sent[0][1]
