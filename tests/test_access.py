from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway
from tg_drop_bot.services.access import can_manage_giveaway, is_admin


def test_admin_ids_are_parsed() -> None:
    settings = Settings(ADMIN_IDS="10, 20;30")
    assert settings.admin_id_set == {10, 20, 30}
    assert is_admin(20, settings)
    assert not is_admin(40, settings)


def test_author_controls_own_giveaway() -> None:
    settings = Settings(ADMIN_IDS="10,20")
    giveaway = Giveaway(id=1, creator_user_id=10)
    assert can_manage_giveaway(10, giveaway, settings)
    assert not can_manage_giveaway(20, giveaway, settings)


def test_orphaned_giveaway_can_be_managed_by_any_current_admin() -> None:
    settings = Settings(ADMIN_IDS="20,30")
    giveaway = Giveaway(id=1, creator_user_id=10)
    assert can_manage_giveaway(20, giveaway, settings)
    assert can_manage_giveaway(30, giveaway, settings)
