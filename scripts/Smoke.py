"""

"""

import asyncio

from core.agent import apply_followup_answer, apply_peer_contribution, parse_design_record
from core.schema import LoggedEntry

TEXT = "Odometry has been installed, but encoder direction is wrong"
ANSWER = "flipped it in code instead of redoing the wiring, faster. 3m drive is off by about 2cm now"
PEER_TEXT = "we could invert the encoder sign in software instead of rewiring, easier"


def dump(record, label):
    print(f"── {label} " + "─" * (40 - len(label)))
    print(f"stage      : {record.stage.value}")
    print(f"subteam    : {record.subteam.value}")
    print(f"title      : {record.title}")
    print(f"summary    : {record.summary}")
    print(f"component  : {record.component}")
    print(f"problem    : {record.problem_statement}")
    print(f"alts       : {record.alternatives_considered}")
    print(f"rationale  : {record.rationale}")
    print(f"test       : {record.test_evidence}")
    print(f"missing    : {record.missing_fields}")
    print(f"confidence : {record.confidence}")
    print(f"Follow up  : {record.followup_question}")
    print()


async def main():
    print(f"Input: {TEXT}\n")
    record = await parse_design_record(TEXT)
    dump(record, "parsed")

    # Peer scenario first, and independent of whether the model asked a
    # follow-up question below — apply_peer_contribution gates on
    # missing_fields, not on followup_question, so it must not be skippable
    # by the same early return that guards the bot-Q&A scenario. Runs on a
    # fresh entry so the two scenarios cannot leak state into each other.
    entry = LoggedEntry(channel="smoke", raw_text=TEXT, record=record)
    merged = await apply_peer_contribution(entry, "bo", PEER_TEXT)
    print(f"Peer message: {PEER_TEXT}")
    print(f"peer contribution folded in: {merged is not entry}")
    dump(merged.record, "after peer message")

    if record.followup_question is None:
        print("No follow-up question — nothing to merge. (This is a valid outcome.)")
        return

    entry = LoggedEntry(
        channel="smoke", raw_text=TEXT, record=record
    ).mark_followup_asked(record.followup_question, "fake-message-id")
    print(f"awaiting_followup: {entry.awaiting_followup}")
    print(f"Reply: {ANSWER}\n")

    entry = await apply_followup_answer(entry, ANSWER)
    dump(entry.record, "after follow-up")
    print(f"awaiting_followup: {entry.awaiting_followup}")


if __name__ == "__main__":
    asyncio.run(main())
