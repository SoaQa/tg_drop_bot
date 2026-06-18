from tg_drop_bot.config import Settings


def test_default_user_rate_limit_is_spam_protection_not_normal_use_limit() -> None:
    settings = Settings()

    assert settings.rate_limit_user_updates == 50
    assert settings.rate_limit_user_window_seconds == 60
