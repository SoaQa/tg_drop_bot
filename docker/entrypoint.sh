#!/usr/bin/env sh
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

alembic upgrade head
exec python -m tg_drop_bot
