from __future__ import annotations

import asyncio

from tg_drop_bot.bot.app import run_bot


def run() -> None:
    asyncio.run(run_bot())
