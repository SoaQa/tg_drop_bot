# tg_drop_bot

Telegram-бот для розыгрышей в каналах: админка, капча для участников,
PostgreSQL, Docker-деплой и публикация релизных образов через GitHub Actions.

## Возможности

- Админка в личке бота для пользователей из `ADMIN_IDS`.
- Создание розыгрыша пошаговым мастером: канал, название, описание, условия, картинка,
  количество победителей, дедлайн.
- Автоматический список каналов, где бот добавлен администратором.
- Участие через кнопку под постом и deep link в личку бота.
- Капча с изображением на каждый розыгрыш через пакет `captcha`.
- Мягкий Redis rate limit/debounce от спама сообщениями и кнопкой участия.
- Проверка подписки на канал при участии и перед выбором победителей.
- Счетчик участников в посте канала с фоновым обновлением по расписанию.
- Выгрузка списка участников.
- Автоматическое завершение по дедлайну и ручное досрочное завершение.
- Docker-first локальный и продакшен-запуск.

## Локальная разработка

Требуется Docker и `uv`.

```powershell
uv venv --python 3.12
uv sync --dev
Copy-Item .env.example .env
docker compose up -d postgres
docker compose up -d redis
uv run alembic upgrade head
uv run python -m tg_drop_bot
```

Для локального теста используйте `TELEGRAM_MODE=polling`.

Бота нужно добавить администратором в тестовый канал. После этого канал появится
в меню админки. Если канал не появился автоматически, откройте в админке пункт
`Каналы` и нажмите `Выбрать канал`.

## Переменные окружения

Создайте `.env` из `.env.example` и заполните:

- `BOT_TOKEN` - токен от BotFather.
- `ADMIN_IDS` - Telegram user ID админов через запятую.
- `DATABASE_URL` - async SQLAlchemy URL PostgreSQL.
- `REDIS_URL` - Redis URL для мягкого rate limit/debounce.
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` - настройки PostgreSQL
  для Docker Compose.
- `APP_SECRET_KEY` - длинная случайная строка для хеширования CAPTCHA.
- `TELEGRAM_MODE` - `polling` локально, `webhook` на продакшене.
- `WEBHOOK_URL`, `WEBHOOK_PATH`, `WEBHOOK_SECRET` - настройки webhook.
  В продакшене `WEBHOOK_URL` также используется Caddy для выпуска TLS-сертификата.

Rate limit не жесткий: по умолчанию допускает несколько апдейтов за короткое
окно и отдельно дебаунсит кнопку участия. Если Redis временно недоступен, бот не
падает и пропускает апдейты без ограничения.

Даты в админке вводятся в формате `ДД.ММ.ГГГГ ЧЧ:ММ`; таймзона задается через
`APP_TIMEZONE`, по умолчанию `Europe/Moscow`.

## Продакшен-деплой

Compose для продакшена использует готовый образ и сам поднимает Caddy для HTTPS.
На новом сервере достаточно положить файлы проекта, создать `.env` и запустить
compose:

```bash
cp .env.example .env
# заполните BOT_TOKEN, ADMIN_IDS, APP_SECRET_KEY, POSTGRES_PASSWORD и webhook-поля
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Минимальные значения для продакшена:

```dotenv
BOT_TOKEN=123456:replace-me
ADMIN_IDS=123456789
POSTGRES_PASSWORD=replace-with-long-random-password
APP_ENV=production
APP_SECRET_KEY=replace-with-long-random-secret
TELEGRAM_MODE=webhook
WEBHOOK_URL=https://bot.example.com
WEBHOOK_PATH=/telegram/webhook
WEBHOOK_SECRET=replace-with-long-random-secret
```

Caddy публикует порты `80` и `443`, автоматически получает сертификат Let's
Encrypt и проксирует Telegram webhook во внутренний контейнер бота. Порт бота
наружу не публикуется.

По умолчанию используется `ghcr.io/soaqa/tg_drop_bot:stable`. Для rollback
закрепите конкретный тег:

```bash
IMAGE_TAG=v0.6.0 docker compose -f docker-compose.prod.yml up -d
```

Watchtower включен в `docker-compose.prod.yml`, но работает только с контейнерами,
у которых есть label `com.centurylinklabs.watchtower.enable=true`. PostgreSQL он
не обновляет.

## GitHub Actions и GHCR

`CI` запускается на push и pull request в `main`:

- поднимает PostgreSQL как GitHub Actions service container;
- устанавливает зависимости через `uv`;
- запускает `ruff`, `mypy`, миграции Alembic и `pytest`;
- собирает Docker image без публикации.

`Release` запускается на git tags `v*`:

- повторяет проверки;
- собирает Docker image;
- публикует теги `vX.Y.Z` и `stable` в GitHub Container Registry:
  `ghcr.io/soaqa/tg_drop_bot`.

Чтобы package был связан с репозиторием, workflow добавляет OCI label
`org.opencontainers.image.source=https://github.com/SoaQa/tg_drop_bot`.

Если репозиторий публичный, GHCR package можно сделать публичным в GitHub UI:
`Packages -> tg_drop_bot -> Package settings -> Change visibility`.

## Проверки

```powershell
uv run ruff check .
uv run mypy src tests
uv run pytest
docker build -t tg-drop-bot:local .
```
