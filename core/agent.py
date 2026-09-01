import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

from .followup import PATCHABLE_FIELDS, apply_patch
from .schema import DesignRecord, FollowupPatch, LoggedEntry

load_dotenv()

_PROMPTS = Path(__file__).parent / "prompts"
SYSTEM_PROMPT = (_PROMPTS / "design_entry.md").read_text(encoding="utf-8")
FOLLOWUP_PROMPT = (_PROMPTS / "followup_merge.md").read_text(encoding="utf-8")
SESSION_LOG_PROMPT = (_PROMPTS / "session_log.md").read_text(encoding="utf-8")

_model = OpenAIChatModel(
    os.getenv("LLM_MODEL", "deepseek-chat"),
    provider=DeepSeekProvider(api_key=os.environ["LLM_API_KEY"]),
)

_agent = Agent(
    _model,
    output_type=DesignRecord,
    system_prompt=SYSTEM_PROMPT,
    retries=2,
)

_followup_agent = Agent(
    _model,
    output_type=FollowupPatch,
    system_prompt=FOLLOWUP_PROMPT,
    retries=2,
)

# Same schema, same model — a different prompt, because the author's intent is
# different. See prompts/session_log.md: a deliberate write-up inverts the bias
# toward silence and must not collapse to the latest stage mentioned.
_log_agent = Agent(
    _model,
    output_type=DesignRecord,
    system_prompt=SESSION_LOG_PROMPT,
    retries=2,
)


async def parse_design_record(raw_text: str) -> DesignRecord:
    """Ambient path: something said in the channel, not written for the log."""
    result = await _agent.run(raw_text)
    return result.output


async def log_session(raw_text: str) -> DesignRecord:
    """Deliberate path: someone ran /log to write up work done offline.

    The caller is responsible for setting source="log" on the LoggedEntry, and
    for posting the receipt as a normal message rather than an ephemeral one —
    ephemeral messages cannot be replied to, which would strand the follow-up.
    """
    result = await _log_agent.run(raw_text)
    return result.output


def _render_context(record: DesignRecord) -> str:
    """The existing record, as the merge agent sees it."""
    return "\n".join(
        [
            "# Existing record",
            f"stage: {record.stage.value}",
            f"subteam: {record.subteam.value}",
            f"title: {record.title}",
            f"summary: {record.summary}",
            f"component: {record.component}",
            f"problem_statement: {record.problem_statement}",
            f"alternatives_considered: {record.alternatives_considered}",
            f"rationale: {record.rationale}",
            f"test_evidence: {record.test_evidence}",
            f"missing_fields: {record.missing_fields}",
        ]
    )


async def apply_followup_answer(
    entry: LoggedEntry, answer_text: str, at: Optional[datetime] = None
) -> LoggedEntry:
    """Fold a reply to the bot's follow-up back into the entry it belongs to.

    This is the other half of asking. Without it the question gets answered, the
    answer gets parsed as a fresh junk record, and the original hole stays open.

    Returns the updated entry. Always records that a reply arrived, even when
    the reply turned out to add nothing — a shrug is an outcome, and re-asking
    is not on the table.
    """
    # `at` is the reply's own timestamp when the channel has one.
    stamp = {
        "followup_answer": answer_text,
        "followup_answered_at": at or datetime.now(timezone.utc),
    }

    # Nothing was asked, or nothing is left to fill: don't spend a call.
    if entry.record.followup_question is None or not (
        set(entry.record.missing_fields) & set(PATCHABLE_FIELDS)
    ):
        return entry.model_copy(update=stamp)

    prompt = "\n\n".join(
        [
            _render_context(entry.record),
            f"# The question you asked\n{entry.record.followup_question}",
            f"# The reply\n{answer_text}",
        ]
    )

    result = await _followup_agent.run(prompt)
    return entry.model_copy(
        update={**stamp, "record": apply_patch(entry.record, result.output)}
    )
