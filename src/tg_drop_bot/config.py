from __future__ import annotations

from functools import cached_property
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    bot_token: str = Field(default="", alias="BOT_TOKEN")
    admin_ids: str = Field(default="", alias="ADMIN_IDS")
    database_url: str = Field(
        default="postgresql+asyncpg://tg_drop_bot:tg_drop_bot@localhost:5432/tg_drop_bot",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    app_env: str = Field(default="local", alias="APP_ENV")
    app_timezone: str = Field(default="Europe/Moscow", alias="APP_TIMEZONE")
    app_secret_key: str = Field(default="dev-change-me", alias="APP_SECRET_KEY")

    telegram_mode: Literal["polling", "webhook"] = Field(default="polling", alias="TELEGRAM_MODE")
    webhook_url: str = Field(default="", alias="WEBHOOK_URL")
    webhook_path: str = Field(default="/telegram/webhook", alias="WEBHOOK_PATH")
    webhook_secret: str = Field(default="", alias="WEBHOOK_SECRET")
    webapp_host: str = Field(default="0.0.0.0", alias="WEBAPP_HOST")
    webapp_port: int = Field(default=8080, alias="WEBAPP_PORT")

    captcha_length: int = Field(default=5, alias="CAPTCHA_LENGTH")
    captcha_ttl_seconds: int = Field(default=300, alias="CAPTCHA_TTL_SECONDS")
    captcha_max_attempts: int = Field(default=3, alias="CAPTCHA_MAX_ATTEMPTS")
    captcha_cooldown_seconds: int = Field(default=300, alias="CAPTCHA_COOLDOWN_SECONDS")

    scheduler_interval_seconds: int = Field(default=30, alias="SCHEDULER_INTERVAL_SECONDS")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_user_updates: int = Field(default=8, alias="RATE_LIMIT_USER_UPDATES")
    rate_limit_user_window_seconds: int = Field(default=3, alias="RATE_LIMIT_USER_WINDOW_SECONDS")
    rate_limit_participate_seconds: int = Field(default=5, alias="RATE_LIMIT_PARTICIPATE_SECONDS")

    @cached_property
    def admin_id_set(self) -> set[int]:
        values: set[int] = set()
        for item in self.admin_ids.replace(";", ",").split(","):
            item = item.strip()
            if item:
                values.add(int(item))
        return values

    @cached_property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def full_webhook_url(self) -> str:
        return f"{self.webhook_url.rstrip('/')}{self.webhook_path}"

    def validate_runtime(self) -> None:
        if not self.bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        if not self.admin_id_set:
            raise RuntimeError("ADMIN_IDS must contain at least one Telegram user ID")
        if self.telegram_mode == "webhook":
            if not self.webhook_url:
                raise RuntimeError("WEBHOOK_URL is required in webhook mode")
            if not self.webhook_secret:
                raise RuntimeError("WEBHOOK_SECRET is required in webhook mode")
