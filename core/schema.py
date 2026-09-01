from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def _normalize_silence(value):
    """DeepSeek sometimes emits the literal string "null" instead of JSON
    null for an "optional, silence-preferred" field. Shared by every field
    where a model literally saying "null" must read as staying silent, not as
    a question titled "null" — see DesignRecord.followup_question and
    FollowupPatch.next_question below."""
    if isinstance(value, str) and value.strip().lower() in {"", "null"}:
        return None
    return value


class Stage(str, Enum):
    PROBLEM = "problem"
    IDEATION = "ideation"
    DECISION = "decision"
    BUILD = "build"
    TEST = "test"
    REFLECTION = "reflection"
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value):
        if value == "idea":
            return cls.IDEATION
        return None

class Subteam(str, Enum):
    MECHANICAL = "mechanical"
    SOFTWARE = "software"
    ELECTRICAL = "electrical"
    DRIVE = "drive"
    OUTREACH = "outreach"
    UNKNOWN = "unknown"

class DesignRecord(BaseModel):
    stage: Stage = Field(
        description=(
            "Which stage of the engineering design cycle this message belongs to. Criteria:\n"
            "- problem: Describes a failure, dissatisfaction, or need, with no solution proposed yet\n"
            "- ideation: Discussing multiple possible solutions, weighing trade-offs, nothing finalized yet\n"
            "- decision: A change is announced together with a reason for it — a trade-off, a "
            "comparison, a 'since/because' — even in present tense with no 'decided to' phrase. "
            "The reasoning is what marks it a decision, not the tense: 'dropping road runner "
            "for pedro pathing, RR tuning ate a meeting every week' is a decision.\n"
            "- build: A change is reported with NO reasoning attached anywhere in the text, or "
            "described as physical/software action taken ('installed', 'swapped', 'moved', "
            "'rewrote') with nothing explaining why: 'we're doing 4 wheels now instead of 6' "
            "is a build, not a decision — nothing says why.\n"
            "- test: Involves testing actions or quantitative results (e.g., error margin, success rate, time elapsed)\n"
            "- reflection: Reviewing whether a change was effective, summarizing takeaways/lessons learned\n"
            "- unknown: Scheduling, ordering takeout, small talk, or other topics unrelated to robot design\n"
            "If a message covers multiple stages simultaneously, select the latest stage."
        )
    )
    subteam: Subteam = Field(
        description=(
            "Categorize by content: mentions structure/mechanism/3D printing/CAD → mechanical; "
            "mentions code/auto/OpMode/pathing/vision → software; "
            "mentions wiring/motors/encoders/sensors → electrical; "
            "mentions teleop/driver practice/match strategy → drive; "
            "mentions sponsorship/outreach/events → outreach; if uncertain, return unknown."
        )
    )

    # ── Content: Extract only what is present in the source text ─────────────────────
    title: str = Field(
        description="A neutral summary under 15 words, styled like a note title. No opinions or exclamations."
    )

    summary: str = Field(
        description=(
            "Recount what happened using a factual tone. Only use information explicitly present in the original text; "
            "do not infer, extrapolate, or embellish. Keep it very short if the source text is short."
        )
    )

    component: Optional[str] = Field(
        default=None,
        description=(
            "The specific mechanism or module name involved, such as intake, slide, odometry, "
            "arm, claw, autonomous. Return null if not mentioned in the source text."
        ),
    )

    problem_statement: Optional[str] = Field(
        default=None,
        description=(
            "The problem intended to be solved by this change. Return null if not explicitly "
            "stated; do not reverse-engineer from the solution. A bare label pointing at an "
            "issue ('the slide flex thing', 'that jamming issue') without describing what "
            "actually went wrong is not an explicit problem statement — it names a topic, not "
            "a problem. Only fill this when the text says what failed, what broke, or what was "
            "unsatisfactory."
        ),
    )

    alternatives_considered: list[str] = Field(
        default_factory=list,
        description=(
            "Prior parts or approaches the text names alongside a stated reason for the change — "
            "a trade-off, a comparison, a 'since/because' — whether that reason explains what was "
            "adopted or what was rejected; the two are usually the same sentence. This is highly "
            "valued by judges. Naming what was replaced is not enough BY ITSELF: 'swapped the "
            "435s for 1150s' names the 435s but the message gives no reason anywhere, so this "
            "stays empty. 'ended up going dual roller since it fits the current frame' and "
            "'dropping road runner, RR tuning ate a meeting every week' both give a reason, so "
            "the named prior option(s) count. Return an empty list only when a change is "
            "reported with no reasoning attached anywhere in the text."
        ),
    )

    rationale: Optional[str] = Field(
        default=None,
        description=(
            "The reasoning behind the choice: trade-offs regarding weight, space, cost, reliability, time, etc. "
            "Fill only when explicitly stated in the source text."
        ),
    )

    test_evidence: Optional[str] = Field(
        default=None,
        description=(
            "Any test or validation results, preserving original numbers and units where possible "
            "(e.g., '2cm error over 3m', '8 out of 10 successful'). Return null if none."
        ),
    )


    missing_fields: list[Literal[
        "problem_statement", "alternatives_considered", "rationale", "test_evidence"
    ]] = Field(
        default_factory=list,
        description=(
            "Which of the key fields above are missing from the source text, listed using their exact English field names. "
            "Only consider these four: problem_statement / alternatives_considered / rationale / test_evidence. "
            "Return an empty list if stage is unknown."
        ),
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score for the stage determination. Assign a low score if the message is ambiguous; do not inflate.",
    )

    # ── Action: Keep at the end ─────────────────────────
    followup_question: Optional[str] = Field(
        default=None,
        description=(
            "A follow-up question targeting the single most important item in missing_fields. "
            "Rules: ask only one question at a time; keep it conversational, like a teammate casually asking; under 30 words; "
            "avoid formal phrases like 'please provide'. "
            "Return null if stage is unknown, missing_fields is empty, or the record is too trivial to follow up on."
        ),
    )

    @field_validator("followup_question", mode="before")
    @classmethod
    def normalize_silence(cls, value):
        return _normalize_silence(value)

    @field_validator("missing_fields", mode="before")
    @classmethod
    def ignore_legacy_missing_fields(cls, value):
        if isinstance(value, list):
            allowed = {
                "problem_statement", "alternatives_considered", "rationale", "test_evidence"
            }
            return [field for field in value if field in allowed]
        return value


class FollowupPatch(BaseModel):
    """What a reply to the bot's follow-up question adds to an existing record.

    Deliberately NOT a DesignRecord: the merge step must not be able to touch
    stage, summary, title or confidence. null means "the reply says nothing
    about this field", never "clear it".
    """

    answered: bool = Field(
        description=(
            "Whether the reply actually answers the question that was asked. "
            "False for deflections, jokes, 'idk', 'later', or an unrelated topic. "
            "When false, every field below must be null."
        )
    )

    problem_statement: Optional[str] = Field(
        default=None,
        description="The problem this change was meant to solve, if the reply states it. Null otherwise.",
    )

    alternatives_considered: Optional[list[str]] = Field(
        default=None,
        description=(
            "Alternatives the reply names as considered but not adopted. "
            "Null if the reply names none — do not return an empty list."
        ),
    )

    rationale: Optional[str] = Field(
        default=None,
        description=(
            "The reasoning the reply gives for the choice: weight, space, cost, "
            "reliability, time. Null if the reply gives no reason."
        ),
    )

    test_evidence: Optional[str] = Field(
        default=None,
        description=(
            "Test or validation results in the reply, keeping the original numbers "
            "and units verbatim. Null if none."
        ),
    )

    # ── Action: keep at the end ──────────────────────────────────────────────
    next_question: Optional[str] = Field(
        default=None,
        description=(
            "One more question, only if a genuinely important gap is still open "
            "after this reply and the reply showed the person is willing to "
            "answer. Same tone rules as the first question: conversational, "
            "under 25 words, one thing only. "
            "Return null if the reply did not answer, if it answered everything "
            "that mattered, or if asking again would feel like nagging. "
            "Null is the normal outcome."
        ),
    )

    @field_validator("next_question", mode="before")
    @classmethod
    def normalize_silence(cls, value):
        return _normalize_silence(value)


class FollowupTurn(BaseModel):
    """One round of the follow-up conversation: a question and its answer.

    The list of these replaces the four scalar followup_* fields this envelope
    used to carry. That shape allowed exactly one question per record forever;
    the real shape of getting a rationale out of a teammate is two rounds
    ("why dual roller?" / "weight" / "how much lighter?").

    `filled` is the stop signal, and the reason a turn records more than the
    text: a round that closed nothing means the question missed, and rephrasing
    it costs goodwill the bot does not have. See core/followup.py.
    """

    question: str
    # Set once the channel has actually posted it. It is the hook a channel uses
    # to recognise a reply, so it is a channel fact, never a model output.
    message_id: Optional[str] = None
    asked_at: Optional[datetime] = None
    answer: Optional[str] = None
    answered_at: Optional[datetime] = None
    # Which of PATCHABLE_FIELDS this round actually closed. Empty means the
    # reply arrived and added nothing.
    filled: list[str] = Field(default_factory=list)


class Contribution(BaseModel):
    """One other person's message, folded into someone else's record.

    The peer analogue of FollowupTurn: same append-only, same `filled` stop
    signal, but for a person who joined the thread unprompted rather than
    answered a question the bot asked. No `message_id`/`asked_at` — nothing
    was asked — and no separate answered_at, because a contribution is one
    event, not a question-and-wait. See core/pipeline._find_open_peer_thread
    and core/agent.apply_peer_contribution.
    """

    author: Optional[str] = None
    raw_text: str
    at: datetime
    filled: list[str] = Field(default_factory=list)


class LoggedEntry(BaseModel):
    """A DesignRecord plus the channel metadata around it.

    The split is the point: DesignRecord is what the model produced, LoggedEntry
    is what actually happened. Nothing here is ever shown to the model as a
    field to fill — timestamps and message ids are facts the channel knows, not
    judgments, and letting the model near them invites fabricated dates.

    `channel` is a plain string label, so core still knows nothing about Discord.
    """

    entry_id: str = Field(default_factory=lambda: uuid4().hex)
    channel: str = "unknown"
    # "ambient": overheard in the channel. "log": someone ran /log on purpose.
    # Deliberate entries are more complete and more trustworthy — the exporter
    # should prefer them, and prompt scores should be read separately per source.
    source: Literal["ambient", "log"] = "ambient"
    channel_message_id: Optional[str] = None
    author: Optional[str] = None
    # When the work happened, as the channel knows it. Defaults to now for
    # hand-built and script-driven entries; a channel should pass the real
    # event time instead (Discord's message.created_at is already UTC-aware).
    # Never a model output — an inferred date is a fabricated date.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_text: str

    record: DesignRecord

    # ── Follow-up lifecycle ──────────────────────────────────────────────────
    # Append-only. The last turn is the live one; everything before it is the
    # conversation that got the record this far.
    followups: list[FollowupTurn] = Field(default_factory=list)

    # ── Peer lifecycle ──────────────────────────────────────────────────────
    # Append-only, like followups, but populated when someone OTHER than
    # `author` adds to this thread without being asked — see
    # core/pipeline._find_open_peer_thread.
    contributions: list[Contribution] = Field(default_factory=list)

    @property
    def open_followup_message_id(self) -> Optional[str]:
        """The message id a reply must target to count as an answer.

        None once the last question has been answered — otherwise later chatter
        in the same thread would overwrite an answer that already landed.
        """
        if self.followups and self.followups[-1].answered_at is None:
            return self.followups[-1].message_id
        return None

    @property
    def awaiting_followup(self) -> bool:
        """Asked, not yet answered. A channel routes replies only to these."""
        return self.open_followup_message_id is not None

    def mark_followup_asked(
        self, question: str, message_id: str, at: Optional[datetime] = None
    ) -> "LoggedEntry":
        """Append a round. `at` takes the channel's own event time when it has
        one; it falls back to now so scripts and tests need not care."""
        turn = FollowupTurn(
            question=question,
            message_id=message_id,
            asked_at=at or datetime.now(timezone.utc),
        )
        return self.model_copy(update={"followups": [*self.followups, turn]})

    def record_followup_answer(
        self, answer: str, filled: list[str], at: Optional[datetime] = None
    ) -> "LoggedEntry":
        """Close the live round. Always called when a reply arrives, even when
        the reply added nothing — a shrug is an outcome, and it is the signal
        that stops the next round."""
        if not self.followups:
            return self
        closed = self.followups[-1].model_copy(
            update={
                "answer": answer,
                "answered_at": at or datetime.now(timezone.utc),
                "filled": list(filled),
            }
        )
        return self.model_copy(update={"followups": [*self.followups[:-1], closed]})
