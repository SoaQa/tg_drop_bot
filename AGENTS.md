# Agent Development Rules

These rules apply to the whole repository.

## Core Workflow

- Read the existing code and docs before changing behavior.
- Keep changes scoped to the user request; do not mix unrelated refactors with feature work.
- Do not commit secrets, real bot tokens, production `.env` files, database dumps, or local caches.
- Prefer the existing stack and patterns: `aiogram 3`, async SQLAlchemy, Alembic, `uv`, Docker Compose, GitHub Actions.
- Use `uv` for dependency and command execution. Keep `uv.lock` in sync with `pyproject.toml`.

## Versioning

- The project version lives in both `pyproject.toml` and `src/tg_drop_bot/__init__.py`; keep them identical.
- Update the version whenever business logic changes.
- Use semantic versioning:
  - Patch: bug fixes, internal cleanups, docs/process changes, tests, CI tweaks, or non-behavioral Docker changes.
  - Minor: new bot/admin/user behavior, changed giveaway rules, changed captcha/rate-limit behavior, new config that affects runtime behavior, or database schema additions compatible with existing deployments.
  - Major: breaking config changes, incompatible database migrations, changed deployment contract, removed behavior, or changes that require manual production intervention.
- Release tags must match the project version exactly, for example version `0.2.0` must be released as tag `v0.2.0`.
- Do not create a release tag for documentation-only changes unless the user explicitly asks.

## Business Logic

- Treat these areas as business logic:
  - admin panel flows;
  - giveaway lifecycle and statuses;
  - participant registration;
  - captcha rules;
  - membership checks;
  - winner selection;
  - CSV export contents;
  - scheduler behavior;
  - rate limiting/debounce behavior.
- Any business logic change must include or update tests.
- Keep Telegram-specific code thin where possible; put testable behavior in `services`.

## Database And Migrations

- Every persistent schema change must include an Alembic migration.
- Do not edit already-released migrations unless the change has not been pushed or released.
- Prefer additive migrations for production safety.
- Keep model definitions and migrations consistent.
- Run `alembic upgrade head` against PostgreSQL before releasing schema changes.

## Required Checks

Before committing code changes, run:

```powershell
uv run ruff check .
uv run mypy src tests
uv run pytest
```

For database or deploy changes, also run:

```powershell
uv run alembic upgrade head
docker build -t tg-drop-bot:local .
```

For integration tests with PostgreSQL:

```powershell
docker compose up -d postgres redis
$env:TEST_DATABASE_URL='postgresql+asyncpg://tg_drop_bot:tg_drop_bot@localhost:5432/tg_drop_bot'
uv run pytest
```

## Docker And Deploy

- Local Compose is for dependencies and optional local bot execution.
- Production Compose must use the published GHCR image by default.
- Watchtower must only auto-update the bot service, not PostgreSQL.
- Do not change the production deploy contract without updating `README.md` and `.env.example`.

## GitHub Actions And Releases

- `main` must stay green in CI.
- Release images are built from `v*` tags by GitHub Actions.
- The release workflow publishes both the immutable version tag and `stable`.
- If a change should produce a new Docker image for production, bump the version, push `main`, then create and push the matching `vX.Y.Z` tag.
