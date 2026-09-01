"""Runnable checks for the logic that can silently go wrong.

No API calls, no database, no framework. `python -m tests.test_core`.

Not covered on purpose: prompt quality. That needs 15 real Discord messages in
tests/samples.py and the scoring loop in §9 — inventing the messages would
measure imagination, not the model.
"""

from datetime import datetime, timezone

from core.agent import _apply_patch
from core.schema import DesignRecord, FollowupPatch, LoggedEntry, Stage, Subteam
from exporters.notebook import render_notebook


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
    assert _apply_patch(rec, FollowupPatch(answered=True)) == rec
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
    assert not e.awaiting_followup
    asked = e.mark_followup_asked("m1", at=datetime(2025, 10, 12, tzinfo=timezone.utc))
    assert asked.awaiting_followup and asked.followup_asked_at.day == 12
    assert e.source == "ambient" and e.entry_id != E(13, R()).entry_id


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
