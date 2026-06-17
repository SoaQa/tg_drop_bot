from __future__ import annotations

from tg_drop_bot.config import Settings
from tg_drop_bot.db.models import Giveaway


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_id_set


def can_manage_giveaway(user_id: int | None, giveaway: Giveaway, settings: Settings) -> bool:
    if user_id is None or not is_admin(user_id, settings):
        return False
    if giveaway.creator_user_id in settings.admin_id_set:
        return giveaway.creator_user_id == user_id
    return True
