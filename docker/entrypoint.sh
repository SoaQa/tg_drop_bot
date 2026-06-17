#!/usr/bin/env sh
set -e

alembic upgrade head
exec python -m tg_drop_bot
