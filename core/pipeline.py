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

from . import followup, storage, triage
from .agent import apply_followup_answer, log_session, parse_design_record
from .schema import LoggedEntry

log = logging.getLogger(__name__)

# The window the open-question budget looks back over. A season-long count
# would throttle the bot to silence by February.
BUDGET_WINDOW = timedelta(hours=12)


@dataclass
class Ingested:
    """What the channel needs to do next: nothing, or post one question."""

    entry: LoggedEntry
    question: Optional[str] = None


async def ingest(
    *,
    channel: str,
    author: Optional[str],
    created_at: datetime,
    raw_text: str,
    channel_message_id: Optional[str] = None,
    source: Literal["ambient", "log"] = "ambient",
) -> Optional[Ingested]:
    """Turn text into a persisted record. None means nothing was worth doing.

    `raw_text` is expected to be a whole burst already (core/inbox.py), not a
    single message — triage and the model both read better that way.
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

    record = await (log_session if source == "log" else parse_design_record)(raw_text)
    entry = LoggedEntry(
        channel=channel,
        source=source,
        channel_message_id=channel_message_id,
        author=author,
        created_at=created_at,
        raw_text=raw_text,
        record=record,
    )
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))


async def handle_reply(
    *, open_message_id: str, raw_text: str, at: datetime
) -> Optional[Ingested]:
    """Route a reply to the question it answers. None means it answered none."""
    entry = await storage.find_by_open_followup(open_message_id)
    if entry is None:
        return None

    entry = await apply_followup_answer(entry, raw_text, at=at)
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))


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


async def _question_for(entry: LoggedEntry) -> Optional[str]:
    """Should the bot say anything, and what?

    Five gates, cheapest first. The default is silence — §8's rule that a
    follow-up posts publicly in a live channel is the reason every one of these
    is a veto rather than a score.
    """
    question = entry.record.followup_question
    if not question:
        return None                          # the model chose silence
    if not followup.should_ask_again(entry):
        return None                          # rounds exhausted, or the last one missed

    thread = await storage.list_thread(entry.channel, entry.record.component)
    if not followup.open_gaps(entry.record, thread):
        return None                          # the thread already answers this

    open_count = await storage.count_open_followups(
        entry.channel, since=datetime.now(timezone.utc) - BUDGET_WINDOW
    )
    if open_count >= followup.MAX_OPEN_QUESTIONS:
        log.info("question budget reached for %s, staying quiet", entry.channel)
        return None

    return question
