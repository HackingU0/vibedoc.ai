"""Runnable checks for the logic that can silently go wrong.

No API calls, no database, no framework. `python -m tests.test_core`.

Not covered on purpose: prompt quality. That needs 15 real Discord messages in
tests/samples.py and the scoring loop in §9 — inventing the messages would
measure imagination, not the model.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import followup, storage, triage
from core.followup import apply_patch as _apply_patch
from core.inbox import Coalescer
from core.progress import UNTAGGED, by_team_and_stage, current, spans
from core.schema import (
    DesignRecord,
    FollowupPatch,
    FollowupTurn,
    LoggedEntry,
    Stage,
    Subteam,
)
from exporters.kanban import IDLE as QUIET
from exporters.kanban import LIVE, render_board
from exporters.notebook import STAGE_ORDER, UNFILED, render_notebook
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
    assert "Snacks" not in out and out.count("\n## ") == 4  # Coverage + Sessions + 2 threads
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

    # A live run had DeepSeek emit the literal string "null" for
    # FollowupPatch.next_question, which had no normalizer of its own. It
    # merged straight through apply_patch (which uses model_copy and so never
    # re-validates) and got posted to the channel as a question reading "null".
    patch = FollowupPatch(answered=True, rationale="lighter", next_question="null")
    assert patch.next_question is None
    merged = _apply_patch(R(missing_fields=["rationale"]), patch)
    assert merged.followup_question is None


def test_legacy_missing_fields():
    record = R(missing_fields=["rationale"]).model_dump(mode="json")
    record["stage"] = "idea"
    record["missing_fields"] = ["rationale", "title", "confidence"]
    entry = storage._to_entry({
        "entry_id": "legacy",
        "channel": "discord",
        "source": "ambient",
        "channel_message_id": "1",
        "author": "alex",
        "created_at": datetime(2025, 10, 12, tzinfo=timezone.utc),
        "raw_text": "x",
        "record": json.dumps(record),
        "followups": "[]",
    })
    assert entry.record.stage is Stage.IDEATION
    assert entry.record.missing_fields == ["rationale"]


def test_search_embedding_failure_is_soft():
    class BrokenEmbeddings:
        async def create(self, **kwargs):
            raise RuntimeError("embedding API is down")

    class BrokenEmbedder:
        embeddings = BrokenEmbeddings()

    async def scenario():
        with patch.dict(os.environ, {"EMBEDDING_API_KEY": "test"}), \
                patch.object(storage, "_embedder", BrokenEmbedder()):
            assert await storage.search("compliant wheels") == []

    asyncio.run(scenario())


def test_save_does_not_hide_embedding_text_bugs():
    async def scenario():
        with patch.object(storage, "_embed_text", side_effect=AttributeError("bug")):
            try:
                await storage.save(E(12, R()))
            except AttributeError as exc:
                assert str(exc) == "bug"
            else:
                assert False, "local embedding-text bugs must propagate"

    asyncio.run(scenario())


def test_thread_gaps():
    thread = [
        E(12, R(component="intake", problem_statement="jams")),
        E(14, R(component="intake", rationale="fits the mount")),
    ]
    assert followup.thread_gaps(thread) == {"alternatives_considered", "test_evidence"}
    assert followup.thread_gaps([]) == set(followup.PATCHABLE_FIELDS)

    # The point of the gate: this message is missing a problem statement, but
    # the thread stated it two entries ago. Asking again is the most annoying
    # thing the bot can do.
    rec = R(component="intake", missing_fields=["problem_statement", "test_evidence"])
    assert followup.open_gaps(rec, thread) == {"test_evidence"}
    assert followup.open_gaps(R(missing_fields=[]), thread) == set()


def test_should_ask_again():
    asked = E(12, R(missing_fields=["rationale"], followup_question="why?")) \
        .mark_followup_asked("why?", "m1")

    # Never asked yet -> ask.
    assert followup.should_ask_again(E(12, R(missing_fields=["rationale"])))
    # Asked, still waiting -> do not pile on.
    assert not followup.should_ask_again(asked)
    # Answered and productive, gap remains -> ask again.
    productive = asked.record_followup_answer("lighter", ["rationale"])
    productive = productive.model_copy(
        update={"record": R(missing_fields=["test_evidence"], followup_question="numbers?")})
    assert followup.should_ask_again(productive)
    # Answered but filled nothing -> the question missed. Stop.
    assert not followup.should_ask_again(
        asked.record_followup_answer("idk man", []))
    # Nothing left to fill -> stop.
    assert not followup.should_ask_again(
        asked.record_followup_answer("lighter", ["rationale"]).model_copy(
            update={"record": R(missing_fields=[])}))
    # Hard ceiling.
    maxed = E(12, R(missing_fields=["rationale"]))
    for i in range(followup.MAX_ROUNDS):
        maxed = maxed.mark_followup_asked(f"q{i}", f"m{i}").record_followup_answer("a", ["rationale"])
        maxed = maxed.model_copy(update={"record": R(missing_fields=["rationale"])})
    assert not followup.should_ask_again(maxed)


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


def test_spans():
    base = datetime(2025, 10, 7, 19, 0, tzinfo=timezone.utc)
    idle = timedelta(minutes=60)
    soon = base + timedelta(minutes=30)

    def at(minutes, *, author="ann", component="intake", stage=Stage.BUILD):
        return LoggedEntry(
            raw_text="x",
            author=author,
            created_at=base + timedelta(minutes=minutes),
            record=R(component=component, stage=stage),
        )

    # Ten minutes apart is one continuous piece of work.
    got = spans([at(0), at(10)], now=soon, idle=idle)
    assert len(got) == 1
    assert got[0].started_at == base
    assert got[0].last_at == base + timedelta(minutes=10)
    assert got[0].is_open, "last activity is well inside the idle window"
    assert got[0].stages == (Stage.BUILD, Stage.BUILD)
    hash(got[0])

    # Three hours apart is two separate pieces of work.
    got = spans([at(0), at(180)], now=base + timedelta(minutes=200), idle=idle)
    assert len(got) == 2

    # A closed span ends at its last activity, NOT at `now` — otherwise every
    # task silently absorbs a full idle window.
    got = spans([at(0)], now=base + timedelta(hours=5), idle=idle)
    assert got[0].ended_at == base and not got[0].is_open

    # A component switch must not truncate the span it interrupted: the intake
    # work is plainly still running at minute 20.
    got = spans([at(0), at(2, component="slide"), at(20)], now=soon, idle=idle)
    by_component = {s.component: s for s in got}
    assert by_component["intake"].last_at == base + timedelta(minutes=20)
    assert by_component["slide"].last_at == base + timedelta(minutes=2)

    # Two people never share a span.
    got = spans([at(0), at(5, author="bo")], now=soon, idle=idle)
    assert len(got) == 2 and {s.author for s in got} == {"ann", "bo"}

    # Chitchat must not bridge two spans. Without the unknown-stage filter the
    # 0/30/70 chain is one span; with it, 0 and 70 are 70 minutes apart.
    got = spans(
        [at(0), at(30, stage=Stage.UNKNOWN), at(70)],
        now=base + timedelta(minutes=200),
        idle=idle,
    )
    assert len(got) == 2, "an unknown-stage message extended a task"

    # Unfiled work is its own bucket, never folded into a named component.
    got = spans([at(0), at(5, component=None)], now=soon, idle=idle)
    assert len(got) == 2 and {s.component for s in got} == {"intake", None}

    # current() reports the thread they came back to, and only while it is live.
    entries = [at(0), at(2, component="slide"), at(20)]
    assert current(entries, author="ann", now=soon, idle=idle).component == "intake"
    assert current(entries, author="bo", now=soon, idle=idle) is None
    assert current(
        entries, author="ann", now=base + timedelta(hours=9), idle=idle
    ) is None


def test_span_gate():
    from core.pipeline import _span_is_busy

    base = datetime(2025, 10, 7, 19, 0, tzinfo=timezone.utc)
    asked = FollowupTurn(question="why?", message_id="m1", asked_at=base)

    def at(minutes, *, author="ann", turns=()):
        return LoggedEntry(
            raw_text="x",
            author=author,
            created_at=base + timedelta(minutes=minutes),
            record=R(component="intake", missing_fields=["rationale"]),
            followups=list(turns),
        )

    fresh = at(20)

    # A question is already outstanding on this task: stay quiet.
    assert _span_is_busy(fresh, [at(0, turns=[asked]), fresh])

    # Nothing asked on this task yet: the gate does not object.
    assert not _span_is_busy(fresh, [at(0), fresh])

    # DEPTH IS NOT BREADTH. The entry's own rounds must be invisible here, or
    # handle_reply — where the entry always has a turn — could never ask again
    # and the whole multi-round feature dies.
    own = at(20, turns=[asked])
    assert not _span_is_busy(own, [at(0), own])

    # Someone else's interruption is not this person's interruption.
    assert not _span_is_busy(fresh, [at(0, author="bo", turns=[asked]), fresh])

    # Yesterday's question on the same component was a different task.
    assert not _span_is_busy(fresh, [at(-1440, turns=[asked]), fresh])


def test_notebook_timeline():
    base = datetime(2025, 10, 7, 19, 0, tzinfo=timezone.utc)

    def at(minutes, stage):
        return LoggedEntry(
            raw_text="x",
            author="ann",
            created_at=base + timedelta(minutes=minutes),
            record=R(component="intake", stage=stage),
        )

    md = render_notebook([
        at(0, Stage.PROBLEM),
        at(20, Stage.BUILD),
        at(24 * 60, Stage.TEST),
    ])

    assert "## Sessions" in md
    assert "### Oct 07" in md and "### Oct 08" in md
    assert md.count("| Who | Component | Active | Stages |") == 2
    assert "| ann | intake | 19:00–19:20 | problem → build |" in md
    # A window, never a duration. A span is the stretch the work was TALKED
    # about; someone can machine a part for two hours in silence. Printing
    # "20 min" would be a timesheet built on chat noise.
    assert "20 min" not in md and "0:20" not in md

    late = base.replace(hour=23, minute=40)
    midnight = render_notebook([
        LoggedEntry(
            raw_text="x",
            author="ann",
            created_at=late,
            record=R(component="intake", stage=Stage.PROBLEM),
        ),
        LoggedEntry(
            raw_text="x",
            author="ann",
            created_at=late + timedelta(minutes=40),
            record=R(component="intake", stage=Stage.BUILD),
        ),
    ])
    assert "| ann | intake | 23:40–Oct 08 00:20 | problem → build |" in midnight


def test_author_question_gate():
    from core.pipeline import _question_for

    entry = LoggedEntry(
        raw_text="x",
        author="ann",
        created_at=datetime(2025, 10, 7, 19, 5, tzinfo=timezone.utc),
        record=R(
            component="slide",
            missing_fields=["rationale"],
            followup_question="why?",
        ),
    )
    calls = []

    async def count_open(channel, *, since, author=None):
        calls.append(author)
        return int(author == "ann")

    async def scenario():
        with patch.object(storage, "count_open_followups", new=count_open):
            assert await _question_for(entry, [entry]) is None

    asyncio.run(scenario())
    assert calls == ["ann"]


def test_respond_reports_thread_gaps():
    from core.pipeline import _respond

    # The thread supplies a problem and a rationale across two entries. The
    # entry in hand supplies neither. The receipt must report what the THREAD
    # lacks, not what this one message lacks — otherwise a complete design
    # line renders as full of holes (CLAUDE.md §10).
    older = E(7, R(component="intake", problem_statement="jams on doubles"))
    entry = E(7, R(component="intake", rationale="the mount already fits",
                   missing_fields=["problem_statement"]))

    async def list_thread(channel, component):
        return [older, entry]

    async def count_open(channel, *, since, author=None):
        return 0

    async def scenario():
        with patch.object(storage, "list_thread", new=list_thread), \
                patch.object(storage, "count_open_followups", new=count_open):
            return await _respond(entry)

    got = asyncio.run(scenario())
    assert got.gaps == frozenset({"alternatives_considered", "test_evidence"})
    assert got.entry is entry
    assert got.question is None, "the record carries no followup_question to post"


def test_status():
    from core.pipeline import status

    now = datetime.now(timezone.utc)

    def at(minutes_ago, **kw):
        return LoggedEntry(
            raw_text="x",
            author=kw.pop("author", "ann"),
            created_at=now - timedelta(minutes=minutes_ago),
            record=R(**kw),
        )

    rows = [
        at(30, component="intake", problem_statement="jams on doubles"),
        at(10, component="intake", rationale="the mount already fits"),
        at(5, component="slide", author="bo"),
    ]

    async def list_entries(*, since=None, channel=None, **kw):
        assert channel == "discord", "status must scope to the channel it was asked in"
        return rows

    async def scenario(author):
        with patch.object(storage, "list_entries", new=list_entries):
            return await status(channel="discord", author=author)

    got = asyncio.run(scenario("ann"))
    assert got.span is not None and got.span.component == "intake"
    assert got.entries == 2, "counts the component thread, not the whole channel"
    # Thread-level again: problem and rationale are supplied across two rows.
    assert got.gaps == frozenset({"alternatives_considered", "test_evidence"})

    # Someone with nothing recent gets an empty answer, not a crash.
    assert asyncio.run(scenario("nobody")).span is None


def test_contribution_roundtrip():
    from core.schema import Contribution

    entry = E(7, R(missing_fields=["rationale"]))
    updated = entry.model_copy(update={
        "contributions": [
            *entry.contributions,
            Contribution(author="bo", raw_text="try dual roller",
                         at=entry.created_at, filled=["rationale"]),
        ]
    })
    assert updated.contributions[0].author == "bo"
    assert updated.contributions[0].filled == ["rationale"]
    # Round-trips through JSON the same way followups already does — this is
    # what storage.py's jsonb column will actually store and reload.
    data = updated.model_dump(mode="json")
    assert LoggedEntry.model_validate(data).contributions[0].author == "bo"


def test_explicit_peer_merge_routing():
    from core import pipeline
    from unittest.mock import AsyncMock

    target = E(7, R(missing_fields=["rationale"]),
               author="ann", channel_message_id="m1")
    merged = target.model_copy(update={"record": R(rationale="lighter")})

    calls = []

    async def find_by_channel_message_id(channel, message_id):
        calls.append(message_id)
        return target if message_id == "m1" else None

    async def save(entry, **kw):
        return entry

    async def scenario(reply_to, author, patched_merge, patched_parse=None):
        with patch.object(storage, "find_by_channel_message_id",
                           new=find_by_channel_message_id), \
             patch.object(storage, "save", new=save), \
             patch.object(storage, "list_thread", new=AsyncMock(return_value=[])), \
             patch.object(storage, "count_open_followups", new=AsyncMock(return_value=0)), \
             patch.object(pipeline, "apply_peer_contribution", new=patched_merge), \
             patch.object(pipeline, "parse_design_record",
                           new=patched_parse or AsyncMock(side_effect=AssertionError(
                               "explicit trigger must not parse — the target is already known"))):
            return await pipeline.ingest(
                channel="discord", author=author,
                created_at=target.created_at, raw_text="try dual roller",
                reply_to_message_id=reply_to,
            )

    # A reply to a real entry, from someone else: merges, never parses.
    result = asyncio.run(scenario("m1", "bo", AsyncMock(return_value=merged)))
    assert result.entry is merged
    assert calls == ["m1"]

    # Replying to your OWN earlier message is not a peer joining — falls
    # through and parses normally instead.
    async def must_not_merge(*a, **kw):
        raise AssertionError("should not attempt a merge against your own entry")
    asyncio.run(scenario("m1", "ann", must_not_merge, AsyncMock(return_value=R())))

    # A reply to a message that isn't tracked at all: falls through and
    # parses normally, no crash.
    asyncio.run(scenario("nonexistent", "bo", must_not_merge, AsyncMock(return_value=R())))


def test_implicit_peer_merge_routing():
    from core import pipeline
    from unittest.mock import AsyncMock

    now = datetime.now(timezone.utc)
    ann_recent = LoggedEntry(
        raw_text="x", author="ann", created_at=now - timedelta(minutes=5),
        record=R(component="intake", missing_fields=["rationale"]),
    )
    ann_stale = LoggedEntry(
        raw_text="x", author="ann", created_at=now - timedelta(hours=3),
        record=R(component="intake", missing_fields=["rationale"]),
    )

    async def scenario(thread, author, component, patched_merge):
        with patch.object(storage, "list_thread",
                           new=AsyncMock(return_value=thread)), \
             patch.object(storage, "count_open_followups", new=AsyncMock(return_value=0)), \
             patch.object(storage, "save", new=AsyncMock(side_effect=lambda e, **kw: e)), \
             patch.object(pipeline, "apply_peer_contribution", new=patched_merge), \
             patch.object(pipeline, "parse_design_record",
                           new=AsyncMock(return_value=R(component=component))):
            return await pipeline.ingest(
                channel="discord", author=author, created_at=now,
                raw_text="try dual roller",
            )

    # ann's span on intake is still live (5 min ago, default 60-min idle):
    # bo's message about intake should merge into it, not become its own row.
    merged = ann_recent.model_copy(update={"record": R(component="intake", rationale="x")})
    result = asyncio.run(scenario([ann_recent], "bo", "intake", AsyncMock(return_value=merged)))
    assert result.entry is merged

    # ann's span went stale hours ago: too old to call this "joining" her
    # work — falls through and creates bo's own entry instead.
    async def must_not_merge(*a, **kw):
        raise AssertionError("a stale span must not be treated as still live")
    result = asyncio.run(scenario([ann_stale], "bo", "intake", must_not_merge))
    assert result.entry.author == "bo" and result.entry.record.component == "intake"

    # Same person posting again is not a peer joining themselves.
    result = asyncio.run(scenario([ann_recent], "ann", "intake", must_not_merge))
    assert result.entry.author == "ann"



def test_board():
    now = datetime(2025, 10, 12, 3, 0, tzinfo=timezone.utc)

    def B(author, roles, component, stage, ago):
        return LoggedEntry(
            raw_text="x", author=author, author_roles=roles,
            created_at=now - timedelta(minutes=ago),
            record=R(stage=stage, component=component),
        )

    ANDROMEDA = "5898 Andromeda"
    entries = [
        B("Eli", ["@everyone", ANDROMEDA, "Team Member"], "intake", Stage.BUILD, 5),
        B("Kim", [ANDROMEDA, "Mentor"], "odometry", Stage.TEST, 600),
        B("Sam", ["7161"], "arm", Stage.PROBLEM, 5),       # bare number is a team
        B("Alex", ["Team Member"], "slide", Stage.BUILD, 5),  # no team role
        B("Jo", [], "claw", Stage.BUILD, 5),               # no roles at all
    ]

    board = by_team_and_stage(spans(entries, now=now))
    assert list(board) == [ANDROMEDA, "7161", UNTAGGED], "lane order, untagged last"
    # The team role wins over the non-team ones regardless of position, and its
    # own wording survives whole — not just the number.
    assert board[ANDROMEDA].keys() == {Stage.BUILD, Stage.TEST}
    assert len(board[UNTAGGED][Stage.BUILD]) == 2, "Alex and Jo share the bin"
    # A span sits in its LAST stage's column, not its first.
    assert board[ANDROMEDA][Stage.TEST][0].component == "odometry"
    # 600 minutes quiet against the default 60-minute window.
    assert not board[ANDROMEDA][Stage.TEST][0].is_open
    assert board[ANDROMEDA][Stage.BUILD][0].is_open

    text = render_board(entries, now=now)
    head, *rows = [l for l in text.splitlines() if l.startswith("|")]
    assert head.count("|") == len(STAGE_ORDER) + 2, "column count drifted"
    assert all(r.count("|") == head.count("|") for r in rows), "ragged row"
    assert f"{LIVE} intake" in text and f"{QUIET} odometry" in text
    # The team's own wording, whole — the lane is not reduced to "5898".
    assert f"**{ANDROMEDA}**" in text
    assert "\u00b7 Eli" in text and "\u00b7 Alex" in text
    assert "2 teams" in text, "the untagged bin is not a team"
    # The lane bin and the no-component bin must not share a word.
    assert UNTAGGED != UNFILED

    assert "Nothing on the go" in render_board([], now=now)

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")


