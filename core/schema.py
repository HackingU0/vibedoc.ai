from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

class Stage(str, Enum):
    PROBLEM = "problem"
    IDEATION = "ideation"
    DECISION = "decision"
    BUILD = "build"
    TEST = "test"
    REFLECTION = "reflection"
    UNKNOWN = "unknown"u

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
            "- decision: Explicitly states 'we decided to / ended up choosing' a specific solution\n"
            "- build: Action has been taken (e.g., modified, built, assembled/installed)\n"
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
        description="The problem intended to be solved by this change. Return null if not explicitly stated; do not reverse-engineer from the solution.",
    )

    alternatives_considered: list[str] = Field(
        default_factory=list,
        description=(
            "Alternative solutions mentioned in the source text that were not ultimately adopted. "
            "This is highly valued by judges. Return an empty list if none are mentioned."
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


    missing_fields: list[str] = Field(
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
        if isinstance(value, str) and value.strip().lower() in {"", "null"}:
            return None
        return value


# ── The four fields a follow-up is allowed to fill ────────────────────────────
# Single source of truth: the gate in agent._apply_patch enforces this list in
# Python, so a follow-up can never rewrite stage / summary / title even if the
# model tries to.
PATCHABLE_FIELDS = (
    "problem_statement",
    "alternatives_considered",
    "rationale",
    "test_evidence",
)


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
    # followup_message_id is set once the bot has posted its question. It is the
    # hook a channel uses to recognise a reply as an answer, and it doubles as
    # the "already asked" flag — one question per record, never a second round.
    followup_message_id: Optional[str] = None
    followup_asked_at: Optional[datetime] = None
    followup_answer: Optional[str] = None
    followup_answered_at: Optional[datetime] = None

    @property
    def awaiting_followup(self) -> bool:
        """Asked, not yet answered. A channel routes replies only to these."""
        return (
            self.followup_message_id is not None
            and self.followup_answered_at is None
        )

    def mark_followup_asked(
        self, message_id: str, at: Optional[datetime] = None
    ) -> "LoggedEntry":
        """Record that the bot posted its question, and under which message id.

        `at` takes the channel's own event time when it has one; it falls back to
        now so scripts and tests do not have to care.
        """
        return self.model_copy(
            update={
                "followup_message_id": message_id,
                "followup_asked_at": at or datetime.now(timezone.utc),
            }
        )
