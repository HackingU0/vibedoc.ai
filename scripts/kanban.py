"""storage -> board. `uv run python -m scripts.kanban > board.md`

The database read lives here, not in exporters/kanban.py, for the same reason
scripts/export.py exists: the exporter renders what it is handed, so narrowing
the board stays a caller-side query. `BOARD_DAYS` is that query — a board is
"what is on the go", and a season's worth of closed spans is a notebook, not a
board.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from core import storage
from exporters.kanban import render_board

load_dotenv()


async def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=int(os.getenv("BOARD_DAYS", "14")))
    try:
        entries = await storage.list_entries(since=since)
    finally:
        await storage.close()

    # Stored UTC, rendered in the team's own zone — CLAUDE.md §6 gotcha 8.
    print(render_board(entries, now=now, tz=ZoneInfo(os.getenv("TEAM_TZ", "UTC"))))


asyncio.run(main())
