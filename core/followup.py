"""The follow-up conversation's rules — the merge gate and the stop policy.

Kept out of core/agent.py so pure callers (tests, the exporter) never need an
LLM_API_KEY just to import a merge rule, and out of core/schema.py so the schema
stays a contract rather than a rulebook.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from .schema import DesignRecord, FollowupPatch, LoggedEntry

load_dotenv()

# ── The four fields a follow-up is allowed to fill ────────────────────────────
PATCHABLE_FIELDS = (
    "problem_statement",
    "alternatives_considered",
    "rationale",
    "test_evidence",
)

# Hard ceiling on rounds per record. Three is two more than the old design and
# still short enough that nobody feels interrogated.
MAX_ROUNDS = int(os.getenv("FOLLOWUP_MAX_ROUNDS", "3"))

# Unanswered questions allowed to be outstanding in one channel at once. The
# bot asking six things during one meeting is how it gets muted in week one.
MAX_OPEN_QUESTIONS = int(os.getenv("MAX_OPEN_QUESTIONS", "2"))


def apply_patch(record: "DesignRecord", patch: "FollowupPatch") -> "DesignRecord":
    """Merge a patch into a record — the gate, enforced in Python.

    Two guarantees the prompt alone could not give us:
      1. only PATCHABLE_FIELDS can change, so a casual reply can never quietly
         rewrite stage, title or summary;
      2. only fields the record itself declared missing can be written, so the
         reply cannot overwrite something the team already said.

    followup_question is carried forward as the patch's next_question. That is
    a *proposal*, not a decision — should_ask_again() below is what decides
    whether it is ever posted.
    """
    if not patch.answered:
        return record

    allowed = set(record.missing_fields) & set(PATCHABLE_FIELDS)
    updates: dict[str, object] = {}

    for name in PATCHABLE_FIELDS:
        if name not in allowed:
            continue
        value = getattr(patch, name)
        if not value:  # None, "", or [] — all mean "nothing supplied"
            continue
        updates[name] = value

    return record.model_copy(
        update={
            **updates,
            "missing_fields": [
                f for f in record.missing_fields if f not in updates
            ],
            "followup_question": patch.next_question,
        }
    )


def thread_gaps(entries: list["LoggedEntry"]) -> set[str]:
    """Which patchable fields no entry in this design thread ever fills.

    Deliberately NOT a sum of per-entry `missing_fields`. That field is scoped
    to one message and exists to drive one follow-up. A design line whose
    problem, alternatives, rationale and results are spread across three
    entries is complete — rolling up per-entry gaps reports it as full of
    holes and sends the team chasing nothing.

    One definition, two consumers: the notebook's coverage table renders it,
    and open_gaps() below uses it to keep the bot from asking about something
    the log already answered.
    """
    return {
        name
        for name in PATCHABLE_FIELDS
        if not any(getattr(e.record, name) for e in entries)
    }


def open_gaps(record: "DesignRecord", thread: list["LoggedEntry"]) -> set[str]:
    """What is still worth asking about: this message's holes, minus what the
    rest of its component thread already supplies.

    `record.missing_fields` is left alone on purpose — it is the truth about
    one message and the exporter reads it as such. This is a question filter,
    not a rewrite.
    """
    return set(record.missing_fields) & thread_gaps(thread)


def should_ask_again(entry: "LoggedEntry") -> bool:
    """Is another round still earning its place?

    Everything here is a stop condition. The default is silence; §8's rule that
    a follow-up posts publicly in a live channel does not stop applying just
    because the conversation is already underway.
    """
    turns = entry.followups
    if len(turns) >= MAX_ROUNDS:
        return False
    if not turns:
        return True

    last = turns[-1]
    if last.answered_at is None:
        return False        # still waiting on the current one
    if not last.filled:
        return False        # the reply added nothing: the question missed
    return bool(set(entry.record.missing_fields) & set(PATCHABLE_FIELDS))
