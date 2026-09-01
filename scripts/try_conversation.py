"""Conversation-level scoring. `uv run python -m scripts.try_conversation`

samples.py scores one message at a time and therefore cannot see the two things
multi-round follow-ups get wrong:

    questions -> is the bot talking too much?     -> followup_merge.md + MAX_ROUNDS
    nags      -> did it ask after a dead end?     -> should_ask_again (must be 0)

Runs core's real policy, with storage stubbed out — the thread gate and the
channel budget need a database and are covered by the storage check in Task 5.
"""

import asyncio
from datetime import datetime, timezone

from core import followup, triage
from core.agent import apply_followup_answer, parse_design_record
from core.schema import LoggedEntry, Stage
from tests.conversations import CONVERSATIONS


async def run_one(convo):
    text = "\n".join(convo.burst)
    if not triage.worth_parsing(text):
        return convo, 0, 0, set(), "triaged out before any call"

    record = await parse_design_record(text)
    entry = LoggedEntry(raw_text=text, record=record,
                        created_at=datetime.now(timezone.utc))

    asked = nags = 0
    for reply in convo.replies:
        if not entry.record.followup_question or not followup.should_ask_again(entry):
            break
        # A question after a round that closed nothing is the nag we are hunting.
        if entry.followups and not entry.followups[-1].filled:
            nags += 1
        entry = await pipeline_ask(entry, asked)
        asked += 1
        entry = await apply_followup_answer(entry, reply)

    filled = {f for turn in entry.followups for f in turn.filled}
    return convo, asked, nags, filled, None


async def pipeline_ask(entry, n):
    """Stand in for the channel posting the question."""
    return entry.mark_followup_asked(entry.record.followup_question, f"m{n}")


async def main():
    results = await asyncio.gather(*(run_one(c) for c in CONVERSATIONS))

    total_nags = 0
    fails = []
    for convo, asked, nags, filled, note in results:
        head = convo.burst[0][:44]
        total_nags += nags
        if asked > convo.max_questions:
            fails.append(f"  LOUD   {head}\n         asked {asked}, budget {convo.max_questions}")
        if nags:
            fails.append(f"  NAG    {head}\n         {nags} question(s) after a dead end")
        if not convo.want_filled <= filled:
            fails.append(f"  thin   {head}\n         wanted {sorted(convo.want_filled)}, filled {sorted(filled)}")
        print(f"  {head:<46} asked={asked} filled={sorted(filled)}"
              + (f"  [{note}]" if note else ""))

    print(f"\nnags {total_nags}   (gate 0, non-negotiable)")
    if fails:
        print("\n" + "\n".join(fails))


asyncio.run(main())
