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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from .schema import Stage, thread_key

if TYPE_CHECKING:
    from .schema import LoggedEntry

load_dotenv()

# How long a person's work on one component goes quiet before the span is
# considered over. UNMEASURED: 60 is a starting point, not a finding. Tune it
# by running the segmenter over a month of real channel history and checking
# the spans against what people remember doing — never against invented text,
# which is the trap notes.md already documents for samples.py.
IDLE = timedelta(minutes=int(os.getenv("TASK_IDLE_MINUTES", "60")))


# Authors with no team label land here. A bin, not a team — it sorts last.
# Deliberately not `Unfiled`: that bin already names a missing component, and
# a board cell reading "Unfiled · Unfiled"
# hides which of the two is actually missing. This one names the fix.
UNTAGGED = "No tag"

# What a team label looks like: a leading number. FTC teams are numbered, and
# a server's team role is written "5898 Andromeda" or "5898" while its other
# roles ("Team Member", "Mentor") are not. Leading digits are the whole test.
# ponytail: a regex, not a roster table. If a server ever names a role
# "2nd place winner" this mislabels it; add a TEAM_ROLE_PATTERN env var then,
# not before.
_TEAM_ROLE = re.compile(r"^\d+\b")


def team(roles: list[str]) -> str:
    """Which of an author's role labels names their team.

    The channel reports the labels; this decides what they mean. That split is
    §4's rule — `channels/` may not contain an `if`, and "is this role a team"
    is exactly the kind of `if` that would otherwise end up there.

    The role's own text is returned whole, not just the number: "5898 Andromeda"
    is what the team calls itself, and §8's rule against normalising the team's
    own wording applies to a lane header as much as to a summary.
    """
    return next((r for r in roles if _TEAM_ROLE.match(r.strip())), UNTAGGED)


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
    # Resolved from the role labels captured on the entries, never from a live
    # roster — so a board rendered next season still shows who was on which
    # team back then, including people who have left.
    team: str = UNTAGGED


    @property
    def is_open(self) -> bool:
        return self.ended_at is None


def _key(entry: "LoggedEntry") -> tuple[Optional[str], str]:
    """Author plus the folded component."""
    return entry.author, thread_key(entry.record.component)


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
                    team=team(group[0].author_roles),
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


def by_team_and_stage(runs: list[Span]) -> dict[str, dict[Stage, list[Span]]]:
    """Group spans into board cells: team lane x design-cycle column.

    A span's column is its **last** stage — the same "if it covers several
    stages, take the latest" rule design_entry.md applies within one message,
    for the same reason: where work has got to is where it belongs on a board.

    Still not task management. Every cell holds spans, so nothing on the board
    was created by a person, is assigned to anyone, or can be moved by hand.
    Cards appear because people talked and grey out because they stopped.
    """
    board: dict[str, dict[Stage, list[Span]]] = {}
    for span in runs:
        lane = board.setdefault(span.team, {})
        lane.setdefault(span.stages[-1], []).append(span)
    return {k: board[k] for k in sorted(board, key=lambda t: (t == UNTAGGED, t))}
