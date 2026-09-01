"""Who is working on what, and when — derived, never stored.

A span is one person, one component, a start and an end. Everything it needs is
already on the entry (author, created_at, record.component), so this module
costs no model call, no table and no background job.

The idea worth keeping: **segmentation is arithmetic, naming is judgement, and
only the arithmetic is needed here.** Asking the model "what task is this?"
would cost a call per message and produce labels that drift across synonyms
("intake fix" / "fixing intake" / "intake"); the component the model already
extracted is a stabler name than anything a second call would invent. It also
keeps §7's rule intact — a start time is a fact the channel knows, exactly like
a timestamp, and facts do not go near the model.

This is NOT task management. Nothing here can be created, assigned, or closed
by a human; a span exists because people talked and stops because they stopped.
See docs/design/progress-tracker.md §2 for the line against CLAUDE.md §3.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from .schema import Stage

if TYPE_CHECKING:
    from .schema import LoggedEntry

load_dotenv()

# How long a person's work on one component goes quiet before the span is
# considered over. UNMEASURED: 60 is a starting point, not a finding. Tune it
# by running the segmenter over a month of real channel history and checking
# the spans against what people remember doing — never against invented text,
# which is the trap notes.md already documents for samples.py.
IDLE = timedelta(minutes=int(os.getenv("TASK_IDLE_MINUTES", "60")))


@dataclass(frozen=True)
class Span:
    """One person's run of work on one component.

    A dataclass rather than a BaseModel on purpose: a span is never persisted,
    never serialised to the model, and never read back from the database. It is
    a view, computed on demand, and so it can never drift out of sync with the
    entries it comes from.

    `last_at` is the last observed activity and is always honest. `ended_at` is
    None while the span is live and equals `last_at` once closed — never the
    moment the closure was noticed, which would inflate every task by a full
    idle window.
    """

    author: Optional[str]
    component: Optional[str]
    started_at: datetime
    last_at: datetime
    ended_at: Optional[datetime]
    entry_ids: tuple[str, ...] = ()
    stages: tuple[Stage, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


def _key(entry: "LoggedEntry") -> tuple[Optional[str], str]:
    """Author plus the folded component.

    The fold is character-for-character the one core/storage.py applies to its
    `component_key` generated column, so a span and a design thread can never
    disagree about whether "Intake" and "intake" are the same work.
    """
    return entry.author, (entry.record.component or "").strip().lower()


def spans(
    entries: list["LoggedEntry"],
    *,
    now: datetime,
    idle: Optional[timedelta] = None,
) -> list[Span]:
    """Segment entries into work spans, oldest first.

    Pure: a function of (entries, now, idle) and nothing else. No clock read and
    no I/O, which is what lets the whole feature be tested without a database or
    an API key.

    Spans may overlap. Someone mentioning the slide in the middle of an intake
    session has two genuinely live pieces of work, and closing the first at the
    interruption would be a lie about when the intake work ended.
    """
    idle = IDLE if idle is None else idle
    runs: dict[tuple[Optional[str], str], list[list["LoggedEntry"]]] = {}

    for entry in sorted(entries, key=lambda e: e.created_at):
        if entry.record.stage is Stage.UNKNOWN:
            continue  # chitchat must never extend, or bridge, a task
        bucket = runs.setdefault(_key(entry), [])
        if not bucket or entry.created_at - bucket[-1][-1].created_at > idle:
            bucket.append([entry])
        else:
            bucket[-1].append(entry)

    out = []
    for buckets in runs.values():
        for group in buckets:
            last_at = group[-1].created_at
            out.append(
                Span(
                    author=group[0].author,
                    # The first entry's spelling, so the team's own
                    # capitalisation survives the grouping.
                    component=(group[0].record.component or "").strip() or None,
                    started_at=group[0].created_at,
                    last_at=last_at,
                    ended_at=last_at if now - last_at > idle else None,
                    entry_ids=tuple(e.entry_id for e in group),
                    stages=tuple(e.record.stage for e in group),
                )
            )
    return sorted(out, key=lambda s: s.started_at)


def current(
    entries: list["LoggedEntry"],
    *,
    author: Optional[str],
    now: datetime,
    idle: Optional[timedelta] = None,
) -> Optional[Span]:
    """What this person is on right now, or None if they are not on anything.

    Since spans overlap, "right now" can have more than one honest answer; this
    returns the most recently active live one. A caller that needs every open
    span should call spans() and filter.
    """
    live = [
        s
        for s in spans(entries, now=now, idle=idle)
        if s.author == author and s.is_open
    ]
    return max(live, key=lambda s: s.last_at) if live else None
