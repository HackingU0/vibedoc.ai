"""Pure, derived health of component threads across the season."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from dotenv import load_dotenv

from .followup import thread_gaps
from .schema import STAGE_ORDER, UNFILED, LoggedEntry, Stage, thread_key

load_dotenv()

# ponytail: ten days is unmeasured; tune it against real season history if the
# stale bucket cries wolf or misses abandoned work.
STALE_AFTER = timedelta(days=int(os.getenv("DIGEST_STALE_DAYS", "10")))


@dataclass(frozen=True)
class Thread:
    """One component's design line, rolled up.

    `gaps` is the WHOLE thread's, never a sum of per-entry `missing_fields`:
    a line whose problem, alternatives and rationale arrived in three separate
    messages is complete, and reporting it as broken sends the team chasing
    nothing (§10, found by reading the first rendered notebook).
    """

    component: str
    entries: int
    authors: tuple[str, ...]
    stages: tuple[Stage, ...]
    gaps: frozenset[str]
    last_at: datetime


def threads(entries: list[LoggedEntry]) -> list[Thread]:
    """Roll entries up by folded component, oldest thread first."""
    buckets: dict[str, list[LoggedEntry]] = {}
    names: dict[str, str] = {}
    for entry in sorted(entries, key=lambda e: e.created_at):
        key = thread_key(entry.record.component)
        buckets.setdefault(key, []).append(entry)
        names.setdefault(key, (entry.record.component or UNFILED).strip() or UNFILED)

    out = []
    for key, bucket in buckets.items():
        seen = {entry.record.stage for entry in bucket}
        out.append(Thread(
            component=names[key],
            entries=len(bucket),
            authors=tuple(sorted({entry.author for entry in bucket if entry.author})),
            stages=tuple(stage for stage in STAGE_ORDER if stage in seen),
            gaps=frozenset(thread_gaps(bucket)),
            last_at=max(entry.created_at for entry in bucket),
        ))
    return out


@dataclass(frozen=True)
class Digest:
    almost: list[Thread]
    untested: list[Thread]
    stale: list[Thread]
    total: int
    complete: int


def summarise(entries: list[LoggedEntry], *, now: datetime) -> Digest:
    """Bucket incomplete threads once, with the most actionable bucket first."""
    almost: list[Thread] = []
    untested: list[Thread] = []
    stale: list[Thread] = []
    rolled = threads(entries)

    for thread in rolled:
        if not thread.gaps:
            continue
        if len(thread.gaps) == 1:
            almost.append(thread)
        elif "test_evidence" in thread.gaps and (
            {Stage.DECISION, Stage.BUILD, Stage.TEST} & set(thread.stages)
        ):
            # Reaching the test stage does NOT exempt a thread: "we ran it and
            # nobody wrote the numbers down" is the hole judges punish, and
            # gating on the stage made it invisible in every bucket.
            untested.append(thread)
        elif now - thread.last_at >= STALE_AFTER:
            stale.append(thread)

    almost.sort(key=lambda thread: thread.last_at, reverse=True)
    untested.sort(key=lambda thread: thread.last_at, reverse=True)
    stale.sort(key=lambda thread: thread.last_at)
    return Digest(
        almost=almost,
        untested=untested,
        stale=stale,
        total=len(rolled),
        complete=sum(not thread.gaps for thread in rolled),
    )
