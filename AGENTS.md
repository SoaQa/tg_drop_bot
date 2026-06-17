# Правила разработки для агентов

Эти правила действуют для всего репозитория.

## Язык

- По умолчанию общайся с владельцем проекта на русском языке.
- Весь текст, который видят пользователи, участники, админы или операторы бота, должен быть на русском.
- Это касается сообщений Telegram-бота, кнопок, CSV-выгрузок, README, `.env.example` комментариев и production-инструкций.
- Английский допустим для внутренних идентификаторов, имен переменных, enum-значений, callback data, названий пакетов, логов и технических API-контрактов.
- Если добавляешь новое user-facing поле или статус, сразу добавляй русскую человекочитаемую подпись.

## Рабочий процесс

- Перед изменениями прочитай существующий код и документацию.
- Держи изменения в рамках запроса пользователя; не смешивай фичи, багфиксы и unrelated refactor.
- Не коммить секреты, реальные bot tokens, production `.env`, дампы БД и локальные кэши.
- Используй текущий стек и паттерны проекта: `aiogram 3`, async SQLAlchemy, Alembic, `uv`, Docker Compose, GitHub Actions.
- Для зависимостей и команд используй `uv`. Если меняешь `pyproject.toml`, обновляй `uv.lock`.

## Версионирование

- Версия проекта хранится в `pyproject.toml` и `src/tg_drop_bot/__init__.py`; значения должны совпадать.
- Обновляй версию при любом изменении бизнес-логики или user-facing поведения.
- Используй semantic versioning:
  - Patch: багфиксы, внутренние чистки, документация, правила процесса, тесты, CI, не поведенческие Docker-правки.
  - Minor: новое поведение бота/админки/участников, изменение правил розыгрыша, captcha/rate-limit поведения, новые runtime-настройки, совместимые добавления в БД, изменения user-facing текстов.
  - Major: breaking config changes, несовместимые миграции, изменение deploy-контракта, удаление поведения или изменения с ручным production-вмешательством.
- Release tag должен точно соответствовать версии проекта: версия `0.2.0` выпускается тегом `v0.2.0`.
- Не создавай release tag для документации или process-only изменений, если пользователь явно не попросил.

## Бизнес-логика

- Считай бизнес-логикой:
  - сценарии админки;
  - жизненный цикл и статусы розыгрыша;
  - регистрацию участников;
  - правила капчи;
  - проверки членства;
  - выбор победителей;
  - содержимое CSV;
  - scheduler;
  - rate limiting/debounce;
  - все user-facing тексты и подписи.
- Любое изменение бизнес-логики должно включать новые или обновленные тесты.
- Telegram-specific код держи тонким, а тестируемую логику выноси в `services`.

## База данных и миграции

- Любое изменение persistent schema должно сопровождаться Alembic migration.
- Не редактируй уже выпущенные миграции, если изменение было запушено или выпущено релизом.
- Для production-безопасности предпочитай additive migrations.
- Держи модели SQLAlchemy и миграции синхронными.
- Перед релизом schema changes запускай `alembic upgrade head` на PostgreSQL.

## Обязательные проверки

Перед коммитом code changes запускай:

```powershell
uv run ruff check .
uv run mypy src tests
uv run pytest
```

Для изменений БД или deploy также запускай:

```powershell
uv run alembic upgrade head
docker build -t tg-drop-bot:local .
```

Для integration tests с PostgreSQL:

```powershell
docker compose up -d postgres redis
$env:TEST_DATABASE_URL='postgresql+asyncpg://tg_drop_bot:tg_drop_bot@localhost:5432/tg_drop_bot'
uv run pytest
```

## Docker и deploy

- Local Compose используется для зависимостей и опционального локального запуска бота.
- Production Compose по умолчанию должен использовать опубликованный GHCR image.
- Watchtower должен автообновлять только bot service, не PostgreSQL.
- Если меняешь production deploy contract, обновляй `README.md` и `.env.example`.

## GitHub Actions и релизы

- `main` должен оставаться зеленым в CI.
- Release images собираются GitHub Actions по тегам `v*`.
- Release workflow публикует immutable version tag и mutable tag `stable`.
- Если изменение должно попасть в production Docker image, обнови версию, запушь `main`, затем создай и запушь matching tag `vX.Y.Z`.
