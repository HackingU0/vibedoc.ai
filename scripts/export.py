"""storage -> notebook. `uv run python -m scripts.export > notebook.md`

The database read lives here rather than in exporters/notebook.py on purpose.
The exporter renders the entries it is handed and nothing else, so "export only
the intake thread" or "export only what software did" stays a caller-side query
instead of becoming a pile of flags inside the exporter — the same reason core/
does not know Discord exists. To narrow an export, pass list_entries() the
filters it already has: since, until, subteam, source, include_unknown.
"""

import asyncio
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from core import storage
from exporters.notebook import render_notebook

load_dotenv()


async def main():
    try:
        entries = await storage.list_entries()
    finally:
        await storage.close()

    # Stored UTC, rendered in the team's own zone. FTC teams meet at night, and
    # 8pm Pacific is 03:00 the next day in UTC — without this, half the season's
    # evening work files under the wrong date, and date is what orders the
    # threads. CLAUDE.md §6 gotcha 8.
    print(render_notebook(entries, tz=ZoneInfo(os.getenv("TEAM_TZ", "UTC"))))


asyncio.run(main())
