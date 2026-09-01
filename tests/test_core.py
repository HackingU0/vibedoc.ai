"""Runnable checks for the logic that can silently go wrong.

No API calls, no database, no framework. `python -m tests.test_core`.

Not covered on purpose: prompt quality. That needs 15 real Discord messages in
tests/samples.py and the scoring loop in §9 — inventing the messages would
measure imagination, not the model.
"""

import asyncio
from datetime import datetime, timezone

from core import triage
from core.followup import apply_patch as _apply_patch
from core.inbox import Coalescer
from core.schema import (
    DesignRecord,
    FollowupPatch,
    LoggedEntry,
    Stage,
    Subteam,
)
from exporters.notebook import render_notebook
from tests.samples import SAMPLES


def R(**kw):
    base = dict(stage=Stage.BUILD, subteam=Subteam.MECHANICAL, title="t",
                summary="s", confidence=0.5)
    return DesignRecord(**{**base, **kw})


def E(day, record, **kw):
    return LoggedEntry(raw_text="x", record=record,
                       created_at=datetime(2025, 10, day, 3, 0, tzinfo=timezone.utc), **kw)


def test_patch_gate():
    rec = R(missing_fields=["rationale"], followup_question="why?")

    out = _apply_patch(rec, FollowupPatch(answered=True, rationale="lighter",
                                          test_evidence="9/10"))
    assert out.rationale == "lighter"
    assert out.test_evidence is None, "wrote a field the record never declared missing"
    assert out.stage is rec.stage and out.summary == rec.summary
    assert out.missing_fields == []
    assert out.followup_question is None, "one question per record"

    assert _apply_patch(rec, FollowupPatch(answered=False, rationale="x")) == rec
    # A reply that answered but supplied nothing still closes the question it
    # answered — carrying it forward would let the bot re-ask it.
    empty = _apply_patch(rec, FollowupPatch(answered=True))
    assert empty.missing_fields == ["rationale"] and empty.followup_question is None
    # A patch may propose the next question; posting it is core/followup's call.
    assert _apply_patch(rec, FollowupPatch(answered=True, rationale="lighter",
                                           next_question="how much lighter?")
                        ).followup_question == "how much lighter?"
    # Empty list is "nothing supplied", not "clear it".
    assert _apply_patch(R(missing_fields=["alternatives_considered"],
                          alternatives_considered=["a"]),
                        FollowupPatch(answered=True, alternatives_considered=[])
                        ).alternatives_considered == ["a"]


def test_notebook():
    # One thread whose four fields are spread across three entries: complete.
    entries = [
        E(12, R(component="intake", stage=Stage.PROBLEM, problem_statement="jams",
                missing_fields=["rationale"])),
        E(14, R(component="Intake", stage=Stage.DECISION,
                alternatives_considered=["compliant wheels"], rationale="fits")),
        E(16, R(component="intake", stage=Stage.TEST, test_evidence="9/10")),
        E(18, R(component="odometry", stage=Stage.BUILD, problem_statement="reversed")),
        E(20, R(stage=Stage.UNKNOWN, subteam=Subteam.UNKNOWN)),
    ]
    out = render_notebook(entries)

    assert "| intake | 3 | problem → decision → test | — |" in out, \
        "gaps must be per thread, not summed per entry"
    assert "| odometry | 1 | build | alternatives considered, why, results |" in out
    assert "1 message classified as unrelated" in out
    assert "Snacks" not in out and out.count("\n## ") == 3  # Coverage + 2 threads
    assert "_4 entries" in out

    assert "No design records yet." in render_notebook([])
    # Casing folds for grouping, the team's own spelling survives in the record.
    assert entries[1].record.component == "Intake"


def test_envelope():
    e = E(12, R())
    assert not e.awaiting_followup and e.open_followup_message_id is None
    assert e.source == "ambient" and e.entry_id != E(13, R()).entry_id

    at = datetime(2025, 10, 12, tzinfo=timezone.utc)
    asked = e.mark_followup_asked("why dual roller?", "m1", at=at)
    assert asked.awaiting_followup
    assert asked.open_followup_message_id == "m1"
    assert asked.followups[-1].question == "why dual roller?"
    assert asked.followups[-1].asked_at.day == 12

    answered = asked.record_followup_answer("it's lighter", ["rationale"], at=at)
    assert not answered.awaiting_followup
    # An answered question must stop routing replies, or later chatter in the
    # thread overwrites the answer that already landed.
    assert answered.open_followup_message_id is None
    assert answered.followups[-1].filled == ["rationale"]

    # A second round appends; it does not overwrite round one.
    round2 = answered.mark_followup_asked("how much lighter?", "m2", at=at)
    assert len(round2.followups) == 2
    assert round2.open_followup_message_id == "m2"
    assert round2.followups[0].answer == "it's lighter"


def test_triage():
    # Cannot be a design record: skip before spending a call.
    for junk in ["lol", "omw", "👍", "who's driving tmrw",
                 "https://youtu.be/dQw4w9WgXcQ", "   "]:
        assert not triage.worth_parsing(junk), junk

    # Anything naming a part, carrying a number, or long enough goes through.
    for real in ["the arm keeps shaking",
                 "3m run, 2cm error",
                 "intake kept jamming when two samples came in at once and we "
                 "ended up going dual roller"]:
        assert triage.worth_parsing(real), real

    # The gate must never swallow a scoring sample that has a real stage.
    # A false negative loses content silently; a false positive costs one call.
    for s in SAMPLES:
        if s.stage is not Stage.UNKNOWN:
            assert triage.worth_parsing(s.text), f"gate swallowed a real one: {s.text[:50]}"


def test_silence_normalization():
    assert R(followup_question="").followup_question is None
    assert R(followup_question="null").followup_question is None


def test_coalescer():
    async def scenario():
        flushed: list[tuple[str, list]] = []

        async def flush(key, items):
            flushed.append((key, items))

        c = Coalescer(flush, quiet=0.02, max_items=3)

        # Three messages inside the quiet window are one unit.
        for text in ["intake keeps jamming", "when two come in at once", "going dual roller"]:
            await c.add("chan:alex", text)
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        assert flushed == [("chan:alex", ["intake keeps jamming",
                                          "when two come in at once",
                                          "going dual roller"])], flushed

        # Two people talking at once do not get mixed together.
        flushed.clear()
        await c.add("chan:alex", "a1")
        await c.add("chan:sam", "s1")
        await asyncio.sleep(0.05)
        assert sorted(flushed) == [("chan:alex", ["a1"]), ("chan:sam", ["s1"])], flushed

        # max_items fires immediately rather than buffering a monologue forever.
        flushed.clear()
        for i in range(3):
            await c.add("chan:sam", i)
        assert flushed == [("chan:sam", [0, 1, 2])], flushed

        # A shutdown must not eat the meeting's last burst.
        flushed.clear()
        await c.add("chan:alex", "last thing")
        await c.drain()
        assert flushed == [("chan:alex", ["last thing"])], flushed

        # A raising flush must not kill the timer task silently.
        flushed.clear()
        async def boom(key, items):
            raise RuntimeError("downstream exploded")
        c2 = Coalescer(boom, quiet=0.01)
        await c2.add("k", "x")
        await asyncio.sleep(0.05)
        await c2.add("k", "y")          # still alive
        await c2.drain()

    asyncio.run(scenario())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
