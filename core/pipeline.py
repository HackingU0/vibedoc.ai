"""The ingest pipeline: everything between "some text arrived" and "here is
what to say back".

This exists so channels/ can stay ears and mouth. Every branch below is a
judgement call — is this worth a call, is this a duplicate, is a question worth
asking right now — and §4 says judgement calls do not live in a channel
adapter. A channel calls ingest(), posts `question` if it is not None, and
calls mark_asked() with the id it got back. That is the whole contract, and it
is why adding a web channel later is a zero-change operation on core/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from . import followup, progress, storage, triage
from .agent import apply_followup_answer, apply_peer_contribution, log_session, parse_design_record
from .schema import LoggedEntry, Stage

log = logging.getLogger(__name__)

# The window the open-question budget looks back over. A season-long count
# would throttle the bot to silence by February.
BUDGET_WINDOW = timedelta(hours=12)

# How far back /status looks. A season-long window would answer "what are you
# on right now" with something from October.
STATUS_WINDOW = timedelta(days=7)

# How far back the board looks. Same reasoning as STATUS_WINDOW: a
# season-long window answers "what is on the go" with something from October.
# Deliberately NOT scripts/kanban.py's BOARD_DAYS — an export is something
# you go and generate, a card answers "right now".
BOARD_WINDOW = timedelta(days=7)


@dataclass
class Ingested:
    """What the channel needs to do next: nothing, or post one question."""

    entry: LoggedEntry
    question: Optional[str] = None
    # What the whole design thread still lacks — NOT this entry's
    # missing_fields. A thread whose problem, alternatives, rationale and
    # results are spread over three entries is complete; rendering per-entry
    # holes reports it as broken and sends the team chasing nothing (§10).
    gaps: frozenset[str] = frozenset()


@dataclass
class Status:
    """What one person is on right now, and how complete that thread is.

    Read-only and derived: this creates nothing, assigns nothing, and closes
    nothing. CLAUDE.md §3 rules out task management, and the line that keeps
    this on the right side of it is that a human can never edit any of it.
    """

    span: Optional["progress.Span"]
    gaps: frozenset[str] = frozenset()
    entries: int = 0


@dataclass
class Board:
    """Every open and recently-closed span, laned by team.

    Read-only and derived, exactly like Status: this creates nothing, assigns
    nothing and closes nothing. CLAUDE.md §3.
    """

    lanes: dict[str, dict[Stage, list[progress.Span]]]
    since: datetime


async def init_schema() -> None:
    """Initialize persistence without exposing storage to channel adapters."""
    await storage.init_schema()


async def _respond(entry: LoggedEntry) -> Ingested:
    """One load of the design thread, two answers out of it.

    The thread read is the expensive part and both callers need it — the
    question decision to avoid asking what the log already answers, and the
    receipt to show coverage. Loading it once here is why adding the receipt
    costs no extra query.
    """
    thread = await storage.list_thread(entry.channel, entry.record.component)
    return Ingested(
        entry=entry,
        question=await _question_for(entry, thread),
        gaps=frozenset(followup.thread_gaps(thread)),
    )


async def ingest(
    *,
    channel: str,
    author: Optional[str],
    created_at: datetime,
    raw_text: str,
    author_roles: Optional[list[str]] = None,
    channel_message_id: Optional[str] = None,
    source: Literal["ambient", "log"] = "ambient",
    reply_to_message_id: Optional[str] = None,
) -> Optional[Ingested]:
    """Turn text into a persisted record. None means nothing was worth doing.

    `raw_text` is expected to be a whole burst already (core/inbox.py), not a
    single message — triage and the model both read better that way.

    `author_roles` is whatever role labels the channel hangs on the author,
    verbatim. Passed through untouched and stored on the entry — the judgment
    about which of them names a team is progress.team()'s, not a channel's.

    `reply_to_message_id` is a Discord Reply target that did NOT resolve to an
    open bot-question (channels/discord_bot.py already tried that path and
    fell through here) — it is checked against the notebook as a peer-merge
    candidate before this message is treated as a topic of its own.
    """
    # Reconnects redeliver; don't pay for the same message twice.
    if channel_message_id and await storage.find_by_channel_message_id(
        channel, channel_message_id
    ):
        return None

    # A deliberate /log is never triaged away. Somebody typed it into a modal
    # on purpose; second-guessing that is how input friction starts.
    if source == "ambient" and not triage.worth_parsing(raw_text):
        return None

    # Explicit peer signal: a Reply that points at a message already in the
    # notebook, from someone other than that entry's author. Cheaper than the
    # implicit path below — the target is already named, so there is nothing
    # to parse until we know the merge did not apply.
    if source == "ambient" and reply_to_message_id and author:
        target = await storage.find_by_channel_message_id(
            channel, reply_to_message_id
        )
        if target and target.author != author:
            merged = await apply_peer_contribution(
                target, author, raw_text, at=created_at
            )
            if merged is not target:
                await storage.save(merged)
                return await _respond(merged)

    record = await (log_session if source == "log" else parse_design_record)(raw_text)

    # Implicit peer signal: nobody replied to anybody, but someone else's work
    # on this exact component has not gone idle. A new message about it is
    # more likely joining that work than starting its own.
    if source == "ambient" and author and record.component:
        target = await _find_open_peer_thread(
            channel, record.component, author, created_at
        )
        if target:
            merged = await apply_peer_contribution(
                target, author, raw_text, at=created_at
            )
            if merged is not target:
                await storage.save(merged)
                return await _respond(merged)

    entry = LoggedEntry(
        channel=channel,
        source=source,
        channel_message_id=channel_message_id,
        author=author,
        author_roles=author_roles or [],
        created_at=created_at,
        raw_text=raw_text,
        record=record,
    )
    await storage.save(entry)
    return await _respond(entry)


async def handle_reply(
    *, open_message_id: str, raw_text: str, at: datetime
) -> Optional[Ingested]:
    """Route a reply to the question it answers. None means it answered none."""
    entry = await storage.find_by_open_followup(open_message_id)
    if entry is None:
        return None

    entry = await apply_followup_answer(entry, raw_text, at=at)
    await storage.save(entry)
    return await _respond(entry)


async def mark_asked(
    entry: LoggedEntry,
    question: str,
    message_id: str,
    at: Optional[datetime] = None,
) -> LoggedEntry:
    """Record that the question was actually posted, and under which id."""
    entry = entry.mark_followup_asked(question, message_id, at=at)
    await storage.save(entry)
    return entry


async def _question_for(
    entry: LoggedEntry, thread: list[LoggedEntry]
) -> Optional[str]:
    """Should the bot say anything, and what?

    Seven gates, cheapest first. The default is silence — §8's rule that a
    follow-up posts publicly in a live channel is the reason every one of these
    is a veto rather than a score.
    """
    question = entry.record.followup_question
    if not question:
        return None                          # the model chose silence
    if not followup.should_ask_again(entry):
        return None                          # rounds exhausted, or the last one missed

    if not followup.open_gaps(entry.record, thread):
        return None                          # the thread already answers this
    if _span_is_busy(entry, thread):
        return None                          # already interrupted this task once

    since = datetime.now(timezone.utc) - BUDGET_WINDOW
    if entry.author and await storage.count_open_followups(
        entry.channel, since=since, author=entry.author
    ):
        return None                          # this person already has a question

    open_count = await storage.count_open_followups(
        entry.channel, since=since
    )
    if open_count >= followup.MAX_OPEN_QUESTIONS:
        log.info("question budget reached for %s, staying quiet", entry.channel)
        return None

    return question


def _span_is_busy(entry: LoggedEntry, thread: list[LoggedEntry]) -> bool:
    """Has this person's current task already been interrupted once?

    Breadth, not depth. followup.should_ask_again() decides whether *this
    record* has earned another round; this decides whether the *task* it
    belongs to has already cost the team one interruption. One question per
    task is the first budget in this codebase that means something — "two per
    twelve hours" is a guess, "don't interrupt the same job twice" is a rule a
    teammate would actually follow.

    The entry being decided about is excluded on purpose. In handle_reply it
    always carries a turn already, so counting its own rounds here would gate
    the multi-round follow-up out of existence.

    `now` is the entry's own timestamp rather than wall-clock time: the
    decision is being made at the moment the entry arrived, and reading the
    clock here would make the same inputs answer differently in a test than in
    production.
    """
    for span in progress.spans(thread, now=entry.created_at):
        if entry.entry_id not in span.entry_ids:
            continue
        others = set(span.entry_ids) - {entry.entry_id}
        return any(e.entry_id in others and e.followups for e in thread)
    return False


async def _find_open_peer_thread(
    channel: str, component: str, author: str, now: datetime
) -> Optional[LoggedEntry]:
    """Is someone ELSE still mid-task on this component right now?

    Reuses the exact "still live" test _span_is_busy already relies on — no
    new time constant, no second guess about what "recent" means alongside
    TASK_IDLE_MINUTES. If more than one other author has an open span here,
    the most recently active one wins; ties are not expected to matter enough
    to resolve deterministically beyond that.
    """
    thread = await storage.list_thread(channel, component)
    candidates = [
        s for s in progress.spans(thread, now=now)
        if s.author != author and s.is_open
    ]
    if not candidates:
        return None
    target_span = max(candidates, key=lambda s: s.last_at)
    return next(
        (e for e in reversed(thread) if e.entry_id in target_span.entry_ids),
        None,
    )


async def status(*, channel: str, author: Optional[str]) -> Status:
    """Answer "what am I on, and what is this thread still missing".

    Costs one query and no model call — the span is arithmetic over rows that
    already exist (core/progress.py) and the gaps are the same rule the
    notebook's coverage table renders.
    """
    now = datetime.now(timezone.utc)
    entries = await storage.list_entries(since=now - STATUS_WINDOW, channel=channel)

    span = progress.current(entries, author=author, now=now)
    if span is None:
        return Status(None)

    # Gaps are the whole component thread's, including other people's entries:
    # "is this design line judge-ready" is a question about the line, not about
    # who typed which part of it.
    key = (span.component or "").strip().lower()
    thread = [
        e for e in entries
        if (e.record.component or "").strip().lower() == key
    ]
    return Status(span, frozenset(followup.thread_gaps(thread)), len(thread))


async def board(*, channel: str) -> Board:
    """One query, no model call. The grouping is progress.py's, not ours."""
    now = datetime.now(timezone.utc)
    since = now - BOARD_WINDOW
    entries = await storage.list_entries(since=since, channel=channel)
    return Board(
        progress.by_team_and_stage(progress.spans(entries, now=now)), since
    )
