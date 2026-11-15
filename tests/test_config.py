from tg_drop_bot.config import Settings


def test_default_user_rate_limit_is_spam_protection_not_normal_use_limit() -> None:
    settings = Settings()

    assert settings.rate_limit_user_updates == 50
    assert settings.rate_limit_user_window_seconds == 60


def test_default_captcha_cooldown_is_one_minute() -> None:
    settings = Settings()

    assert settings.captcha_cooldown_seconds == 60


def test_frog_game_supports_production_queue() -> None:
    settings = Settings(
        BOT_TOKEN="123:test", ADMIN_IDS="1", APP_SECRET_KEY="private-key",
        TELEGRAM_MODE="webhook", WEBHOOK_URL="https://example.test",
        WEBHOOK_SECRET="secret", FROG_GAME_ENABLED=True, PARTICIPATION_QUEUE_ENABLED=True,
    )
    settings.validate_runtime()
    assert settings.frog_game_path == "/game"
