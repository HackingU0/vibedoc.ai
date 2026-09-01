"""Coalesce a burst of items under one key into a single unit of meaning.

People type the way they think: four lines in twenty seconds, one thought.

    19:41  intake keeps jamming
    19:41  like when two blocks come in at the same time
    19:42  tried compliant wheels, didn't help much
    19:42  going dual roller, it fits the current mount

Handled one at a time that is four model calls, four fragment records in the
notebook, and up to four follow-up questions aimed at one person. Handled as a
burst it is one call and one question.

Generic on purpose — `key` and `item` are opaque here. A channel decides that a
key is "this author in this channel"; core does not need to know that Discord
exists to buffer a list.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# How long one person has to stay quiet before their burst is considered over.
# Tunable: too short splits a thought, too long makes the bot feel asleep.
QUIET_SECONDS = float(os.getenv("BURST_QUIET_SECONDS", "45"))

# A monologue longer than this is flushed without waiting. Bounds both memory
# and the worst-case prompt length.
MAX_ITEMS = int(os.getenv("BURST_MAX_ITEMS", "10"))

Flush = Callable[[str, list], Awaitable[None]]


class Coalescer:
    """Buffer items per key; flush after `quiet` seconds of silence."""

    def __init__(self, flush: Flush, *, quiet: float = QUIET_SECONDS,
                 max_items: int = MAX_ITEMS) -> None:
        self._flush = flush
        self._quiet = quiet
        self._max_items = max_items
        self._buffers: dict[str, list] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, key: str, item: object) -> None:
        buffer = self._buffers.setdefault(key, [])
        buffer.append(item)

        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

        if len(buffer) >= self._max_items:
            await self._fire(key)
        else:
            self._timers[key] = asyncio.create_task(self._after_quiet(key))

    async def drain(self) -> None:
        """Flush everything now. Call on shutdown, or the last burst of the
        meeting — the one most likely to be the actual recap — is lost."""
        for key in list(self._buffers):
            timer = self._timers.pop(key, None)
            if timer is not None:
                timer.cancel()
            await self._fire(key)

    async def _after_quiet(self, key: str) -> None:
        try:
            await asyncio.sleep(self._quiet)
        except asyncio.CancelledError:
            return
        await self._fire(key)

    async def _fire(self, key: str) -> None:
        items = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not items:
            return
        try:
            await self._flush(key, items)
        except Exception:
            # One bad burst must not take the timer machinery down with it.
            log.exception("flush failed for %s, dropping %d item(s)", key, len(items))
