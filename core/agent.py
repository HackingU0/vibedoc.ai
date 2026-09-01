import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic_ai import Agent, PromptedOutput
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
    os.getenv("LLM_MODEL", "deepseek-v4-flash"),
    provider=DeepSeekProvider(api_key=os.environ["LLM_API_KEY"]),
)

# deepseek-v4-flash is a thinking model: pydantic-ai's default Tool Output mode
# forces a tool call via tool_choice, which thinking models reject outright
# ("Thinking mode does not support this tool_choice" — CLAUDE.md §6 gotcha 3).
# PromptedOutput asks for the schema in the prompt and parses JSON back out,
# instead of forcing a tool call. NativeOutput (gotcha 3 fix (b)) was tried
# first and rejected immediately: pydantic-ai has no native-structured-output
# profile registered for this model id and raises `UserError: Native
# structured output is not supported by this model.` before any request goes
# out. PromptedOutput is fix (c) — most universal, slightly less reliable —
# and keeps reasoning on, which is the whole point of this model for long
# bursts and multi-person threads.
_agent = Agent(
    _model,
    output_type=PromptedOutput(DesignRecord),
    system_prompt=SYSTEM_PROMPT,
    retries=2,
)

_followup_agent = Agent(
    _model,
    output_type=PromptedOutput(FollowupPatch),
    system_prompt=FOLLOWUP_PROMPT,
    retries=2,
)

# Same schema, same model — a different prompt, because the author's intent is
# different. See prompts/session_log.md: a deliberate write-up inverts the bias
# toward silence and must not collapse to the latest stage mentioned.
_log_agent = Agent(
    _model,
    output_type=PromptedOutput(DesignRecord),
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


def _render_context(record: DesignRecord, asked: list[str] = ()) -> str:
    """The existing record, as the merge agent sees it."""
    lines = [
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
    if asked:
        lines += ["", "# Already asked in this thread — do not repeat these"]
        lines += [f"- {q}" for q in asked]
    return "\n".join(lines)


async def apply_followup_answer(
    entry: LoggedEntry, answer_text: str, at: Optional[datetime] = None
) -> LoggedEntry:
    """Fold a reply to the bot's question back into the entry it belongs to.

    This is the other half of asking. Without it the question gets answered, the
    answer gets parsed as a fresh junk record, and the original hole stays open.

    Always closes the live turn, even when the reply added nothing — a shrug is
    an outcome, and an empty `filled` is precisely the signal that stops the
    next round (core/followup.should_ask_again).

    The model's proposed next question rides back on
    `record.followup_question`. It is a proposal: core/pipeline decides whether
    it is ever posted.
    """
    if not entry.followups:
        return entry

    # Nothing left this reply could legally fill: don't spend a call.
    if not (set(entry.record.missing_fields) & set(PATCHABLE_FIELDS)):
        return entry.record_followup_answer(answer_text, [], at=at)

    prompt = "\n\n".join(
        [
            _render_context(entry.record, [t.question for t in entry.followups[:-1]]),
            f"# The question you asked\n{entry.followups[-1].question}",
            f"# The reply\n{answer_text}",
        ]
    )

    result = await _followup_agent.run(prompt)
    merged = apply_patch(entry.record, result.output)
    filled = sorted(set(entry.record.missing_fields) - set(merged.missing_fields))

    return entry.model_copy(update={"record": merged}).record_followup_answer(
        answer_text, filled, at=at
    )
