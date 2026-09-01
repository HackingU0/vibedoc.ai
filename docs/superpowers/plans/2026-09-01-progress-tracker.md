# Progress Tracker Implementation Plan

> **For the engineer picking this up:** steps use checkbox (`- [ ]`) syntax.
> Tick each one as you go. There is no execution skill installed in this repo —
> work the tasks directly, in order, one commit per task.

**Goal:** give the agent a per-person, time-ordered view of what the team is
working on — who is on what, when it started, when it stopped — derived from
entries already captured, at zero additional model cost.

**Architecture:** one new pure module, `core/progress.py`, that segments
`list[LoggedEntry]` into work **spans** by `(author, component)` using nothing
but timestamps. No new table, no new model call, no background job. Two
consumers: a follow-up gate in `core/pipeline.py` ("one question per task") and
a session timeline in `exporters/notebook.py`.

**Tech Stack:** Python 3.13, uv, pydantic-ai, PostgreSQL + pgvector (untouched
by this plan), discord.py (untouched by this plan). **No new dependencies.**

**Spec:** `docs/design/progress-tracker.md` — read it before Task 1. It carries
the reasoning this plan only executes, including the scope line against
CLAUDE.md §3's kanban exclusion.

---

## Global Constraints

Every task's requirements implicitly include these. They are copied from
`CLAUDE.md`; violating one is a rejected task, not a style note.

- **No new dependencies.** The stack is deliberately small (§11).
- **`core/` does not know Discord exists, and does not know DeepSeek exists** (§4).
- **No judgment calls in `channels/`.** If you write an `if` in
  `channels/discord_bot.py`, it belongs in `core/` (§4). *This plan touches no
  channel file at all.*
- **`exporters/` and `channels/` must not import `core/storage.py`** (§11).
  Callers wire them together; the layers stay ignorant of each other.
- **Enforce integrity rules in Python, not in the prompt** (§11). This plan adds
  no prompt text and changes no prompt file.
- **Do not make the schema "more complete."** No field is added to
  `DesignRecord`, `LoggedEntry`, or `FollowupPatch` by this plan.
- **Enums subclass `str`; every uncertain field is `Optional[...] = None`;
  lists use `default_factory=list`** (§7).
- Timestamps are facts the channel supplies, never model output (§7). Nothing
  here goes near the model.
- Tests in `tests/test_core.py` are **pure**: no API key, no database, no
  framework. Run with `uv run python -m tests.test_core`.

### Scope guard

`CLAUDE.md` §3 puts **task management / kanban** out of scope. This plan builds
a *derived, read-only* view and stays on the right side of that line. Do not,
while implementing it, add: a `spans` table, a `/task` command, assignment,
owners, due dates, a "mark done" action, or any way for a human to edit a span.
If one of those seems necessary, stop and ask — it is a different feature.

---

## Two corrections this plan makes to the design doc

Both were found by reading the existing code and both make the change smaller.
The design doc is otherwise implemented as written.

**1. No storage change is needed.** The design doc proposed adding a `channel`
filter to `storage.list_entries`. It turns out `pipeline._question_for` already
loads exactly the rows the span gate needs:

```python
thread = await storage.list_thread(entry.channel, entry.record.component)
```

That is channel-scoped, component-scoped, and recent. The gate reuses it.
**Zero new queries, zero storage edits.** Drop the `list_entries` change.

**2. No new scoring harness, and the gate excludes the entry's own rounds.**
The design doc proposed a "questions per span" metric in
`scripts/try_conversation.py`. Two reasons not to:

- The gate is a **deterministic veto applied after the model already produced a
  question**. No model behaviour is involved, so a scoring run would spend API
  calls to measure nothing the unit test does not already pin down.
- `tests/conversations.py` fixtures are one burst → one entry. A span over a
  single entry is trivially of size one, so the metric would read 1.0 by
  construction and mean nothing.

Whether the gate makes the bot *too* quiet is a real question, but it can only
be answered against real channel history — the same thing `notes.md` already
says about `samples.py`. Record it as an open question; do not fake it.

The second half matters for correctness. **Depth and breadth are different
gates:**

| Gate | Owns | Question it answers |
|---|---|---|
| `followup.should_ask_again` | depth | may *this record* have another round? |
| the new span gate | breadth | has this person's *current task* already been interrupted? |

If the span gate counted the entry's own `followups`, it would kill the
multi-round follow-up feature outright — in `pipeline.handle_reply` the entry
always has at least one turn by definition. **The gate must exclude the entry
being decided about.** Task 2's test pins this.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `core/progress.py` | **Create** (~75 lines) | `Span`, `spans()`, `current()`. Pure segmentation. Imports only `schema`. |
| `core/pipeline.py` | Modify (~18 lines) | Gains a sixth gate in `_question_for`: one question per task. |
| `exporters/notebook.py` | Modify (~25 lines) | Gains a `## Sessions` timeline between Coverage and the component threads. |
| `tests/test_core.py` | Modify (~70 lines) | Gains `test_spans`, `test_span_gate`, `test_notebook_timeline`. |
| `CLAUDE.md` | Modify | §3 scope line clarified, §4 file tree updated, §10 status updated. |
| `spec.md` | Modify | Diagram gains the `progress` node. |
| `docs/design/progress-tracker.md` | Modify | Status flipped from "design only". |

Nothing else is touched. In particular: **no change to `core/schema.py`,
`core/storage.py`, `core/agent.py`, `core/followup.py`, `core/triage.py`,
`core/inbox.py`, `channels/`, or any file in `core/prompts/`.**

---

## Task 1: `core/progress.py` — segment entries into spans

Lands the segmenter and nothing else. **No behaviour changes anywhere in the
product after this task** — that is deliberate, so the segmenter can be
eyeballed against real history before anything depends on it.

**Files:**
- Create: `core/progress.py`
- Test: `tests/test_core.py` (add `test_spans`)

**Interfaces:**
- Consumes: `core.schema.LoggedEntry`, `core.schema.Stage`
- Produces, for Tasks 2 and 3:
  - `Span` — frozen dataclass with `author: Optional[str]`,
    `component: Optional[str]`, `started_at: datetime`, `last_at: datetime`,
    `ended_at: Optional[datetime]`, `entry_ids: list[str]`,
    `stages: list[Stage]`, and a property `is_open: bool`
  - `spans(entries: list[LoggedEntry], *, now: datetime, idle: Optional[timedelta] = None) -> list[Span]`
  - `current(entries: list[LoggedEntry], *, author: Optional[str], now: datetime, idle: Optional[timedelta] = None) -> Optional[Span]`
  - `IDLE: timedelta` — module default, from `TASK_IDLE_MINUTES`

- [x] **Step 1: Write the failing test**

Add to `tests/test_core.py`. First extend the existing datetime import at the
top of the file from

```python
from datetime import datetime, timezone
```

to

```python
from datetime import datetime, timedelta, timezone
```

and add `current` / `spans` to the imports:

```python
from core.progress import current, spans
```

Then append this test:

```python
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
    assert got[0].stages == [Stage.BUILD, Stage.BUILD]

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
```

- [x] **Step 2: Run the test and watch it fail**

```bash
uv run python -m tests.test_core
```

Expected: `ModuleNotFoundError: No module named 'core.progress'`.

- [x] **Step 3: Write `core/progress.py`**

```python
"""Who is working on what, and when — derived, never stored.

A span is one person, one component, a start and an end. Everything it needs is
already on the entry (author, created_at, record.component), so this module
costs no model call, no table and no background job.

The idea worth keeping: **segmentation is arithmetic, naming is judgement, and
only the arithmetic is needed here.** Asking the model "what task is this?"
would cost a call per message and produce labels that drift across synonyms
("intake fix" / "fixing intake" / "intake"); the component the model already
extracted is a stabler name than anything a second call would invent. It also
keeps §7's rule intact — a start time is a fact the channel knows, exactly like
a timestamp, and facts do not go near the model.

This is NOT task management. Nothing here can be created, assigned, or closed
by a human; a span exists because people talked and stops because they stopped.
See docs/design/progress-tracker.md §2 for the line against CLAUDE.md §3.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

from .schema import Stage

if TYPE_CHECKING:
    from .schema import LoggedEntry

load_dotenv()

# How long a person's work on one component goes quiet before the span is
# considered over. UNMEASURED: 60 is a starting point, not a finding. Tune it
# by running the segmenter over a month of real channel history and checking
# the spans against what people remember doing — never against invented text,
# which is the trap notes.md already documents for samples.py.
IDLE = timedelta(minutes=int(os.getenv("TASK_IDLE_MINUTES", "60")))


@dataclass(frozen=True)
class Span:
    """One person's run of work on one component.

    A dataclass rather than a BaseModel on purpose: a span is never persisted,
    never serialised to the model, and never read back from the database. It is
    a view, computed on demand, and so it can never drift out of sync with the
    entries it comes from.

    `last_at` is the last observed activity and is always honest. `ended_at` is
    None while the span is live and equals `last_at` once closed — never the
    moment the closure was noticed, which would inflate every task by a full
    idle window.
    """

    author: Optional[str]
    component: Optional[str]
    started_at: datetime
    last_at: datetime
    ended_at: Optional[datetime]
    entry_ids: list[str] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


def _key(entry: "LoggedEntry") -> tuple[Optional[str], str]:
    """Author plus the folded component.

    The fold is character-for-character the one core/storage.py applies to its
    `component_key` generated column, so a span and a design thread can never
    disagree about whether "Intake" and "intake" are the same work.
    """
    return entry.author, (entry.record.component or "").strip().lower()


def spans(
    entries: list["LoggedEntry"],
    *,
    now: datetime,
    idle: Optional[timedelta] = None,
) -> list[Span]:
    """Segment entries into work spans, oldest first.

    Pure: a function of (entries, now, idle) and nothing else. No clock read and
    no I/O, which is what lets the whole feature be tested without a database or
    an API key.

    Spans may overlap. Someone mentioning the slide in the middle of an intake
    session has two genuinely live pieces of work, and closing the first at the
    interruption would be a lie about when the intake work ended.
    """
    idle = idle or IDLE
    runs: dict[tuple[Optional[str], str], list[list["LoggedEntry"]]] = {}

    for entry in sorted(entries, key=lambda e: e.created_at):
        if entry.record.stage is Stage.UNKNOWN:
            continue  # chitchat must never extend, or bridge, a task
        bucket = runs.setdefault(_key(entry), [])
        if not bucket or entry.created_at - bucket[-1][-1].created_at > idle:
            bucket.append([entry])
        else:
            bucket[-1].append(entry)

    out = []
    for buckets in runs.values():
        for group in buckets:
            last_at = group[-1].created_at
            out.append(
                Span(
                    author=group[0].author,
                    # The first entry's spelling, so the team's own
                    # capitalisation survives the grouping.
                    component=(group[0].record.component or "").strip() or None,
                    started_at=group[0].created_at,
                    last_at=last_at,
                    ended_at=last_at if now - last_at > idle else None,
                    entry_ids=[e.entry_id for e in group],
                    stages=[e.record.stage for e in group],
                )
            )
    return sorted(out, key=lambda s: s.started_at)


def current(
    entries: list["LoggedEntry"],
    *,
    author: Optional[str],
    now: datetime,
    idle: Optional[timedelta] = None,
) -> Optional[Span]:
    """What this person is on right now, or None if they are not on anything.

    Since spans overlap, "right now" can have more than one honest answer; this
    returns the most recently active live one. A caller that needs every open
    span should call spans() and filter.
    """
    live = [
        s
        for s in spans(entries, now=now, idle=idle)
        if s.author == author and s.is_open
    ]
    return max(live, key=lambda s: s.last_at) if live else None
```

- [x] **Step 4: Run the test and watch it pass**

```bash
uv run python -m tests.test_core
```

Expected: every existing test still `ok`, plus `ok  test_spans`.

- [x] **Step 5: Commit**

```bash
git add core/progress.py tests/test_core.py
git commit -m "feat: derive per-person work spans from entries already captured"
```

---

## Task 2: one question per task

Adds the sixth gate to `_question_for`. This is the reason the feature exists.

**Files:**
- Modify: `core/pipeline.py` (import block, `_question_for`, one new helper)
- Test: `tests/test_core.py` (add `test_span_gate`)

**Interfaces:**
- Consumes: `core.progress.spans` from Task 1
- Produces: `pipeline._span_is_busy(entry: LoggedEntry, thread: list[LoggedEntry]) -> bool`

- [x] **Step 1: Write the failing test**

Add `FollowupTurn` to the `core.schema` import in `tests/test_core.py`, so the
block reads:

```python
from core.schema import (
    DesignRecord,
    FollowupPatch,
    FollowupTurn,
    LoggedEntry,
    Stage,
    Subteam,
)
```

Then append:

```python
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
```

- [x] **Step 2: Run the test and watch it fail**

```bash
uv run python -m tests.test_core
```

Expected: `ImportError: cannot import name '_span_is_busy' from 'core.pipeline'`.

- [x] **Step 3: Add the gate to `core/pipeline.py`**

Extend the relative import on line 19 from

```python
from . import followup, storage, triage
```

to

```python
from . import followup, progress, storage, triage
```

Append this helper to the end of the file:

```python
def _span_is_busy(entry: LoggedEntry, thread: list[LoggedEntry]) -> bool:
    """Has this person's current task already been interrupted once?

    Breadth, not depth. followup.should_ask_again() decides whether *this
    record* has earned another round; this decides whether the *task* it
    belongs to has already cost the team one interruption. One question per
    task is the first budget in this codebase that means something — "two per
    twelve hours" is a guess, "don't interrupt the same job twice" is a rule a
    teammate would actually follow.

    The entry being decided about is excluded on purpose. In handle_reply it
    always carries a turn already, so counting its own rounds here would gate
    the multi-round follow-up out of existence.

    `now` is the entry's own timestamp rather than wall-clock time: the
    decision is being made at the moment the entry arrived, and reading the
    clock here would make the same inputs answer differently in a test than in
    production.
    """
    for span in progress.spans(thread, now=entry.created_at):
        if entry.entry_id not in span.entry_ids:
            continue
        others = set(span.entry_ids) - {entry.entry_id}
        return any(e.entry_id in others and e.followups for e in thread)
    return False
```

Then wire it into `_question_for`, immediately after the thread-gap check and
before the budget check — it is pure Python over rows already in memory, so it
belongs ahead of anything that costs a round trip:

```python
    thread = await storage.list_thread(entry.channel, entry.record.component)
    if not followup.open_gaps(entry.record, thread):
        return None                          # the thread already answers this
    if _span_is_busy(entry, thread):
        return None                          # already interrupted this task once

    open_count = await storage.count_open_followups(
```

Finally update the docstring of `_question_for`, which currently says "Five
gates, cheapest first":

```python
    """Should the bot say anything, and what?

    Six gates, cheapest first. The default is silence — §8's rule that a
    follow-up posts publicly in a live channel is the reason every one of these
    is a veto rather than a score.
    """
```

- [x] **Step 4: Run the test and watch it pass**

```bash
uv run python -m tests.test_core
```

Expected: everything `ok`, including `ok  test_span_gate`.

- [x] **Step 5: Confirm the conversation harness did not regress**

The span gate is not exercised by this harness (its fixtures are one burst
each), so this run is a check that nothing *broke*, not a measurement.

```bash
uv run python -m scripts.try_conversation
```

Expected: `nags 0`, and the same 3 questions across 4 conversations that
`notes.md` run 10 records. If the numbers moved, the gate is reaching further
than intended — investigate before committing.

- [x] **Step 6: Commit**

```bash
git add core/pipeline.py tests/test_core.py
git commit -m "feat: at most one follow-up question per task"
```

---

## Task 3: the notebook's session timeline

**Files:**
- Modify: `exporters/notebook.py`
- Test: `tests/test_core.py` (add `test_notebook_timeline`)

**Interfaces:**
- Consumes: `core.progress.spans` from Task 1
- Produces: nothing other tasks depend on

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py`:

```python
def test_notebook_timeline():
    base = datetime(2025, 10, 7, 19, 0, tzinfo=timezone.utc)

    def at(minutes, stage):
        return LoggedEntry(
            raw_text="x",
            author="ann",
            created_at=base + timedelta(minutes=minutes),
            record=R(component="intake", stage=stage),
        )

    md = render_notebook([at(0, Stage.PROBLEM), at(20, Stage.BUILD)])

    assert "## Sessions" in md
    assert "| Oct 07 | ann | intake | 19:00–19:20 | problem → build |" in md
    # A window, never a duration. A span is the stretch the work was TALKED
    # about; someone can machine a part for two hours in silence. Printing
    # "20 min" would be a timesheet built on chat noise.
    assert "20 min" not in md and "0:20" not in md
```

- [x] **Step 2: Run the test and watch it fail**

```bash
uv run python -m tests.test_core
```

Expected: `AssertionError` on `"## Sessions" in md`.

- [x] **Step 3: Add the timeline to `exporters/notebook.py`**

Extend the `core` imports at the top of the file from

```python
from core.followup import thread_gaps
from core.schema import LoggedEntry, Stage
```

to

```python
from core.followup import thread_gaps
from core.progress import spans
from core.schema import LoggedEntry, Stage
```

Add this function immediately after `_gaps`:

```python
def _timeline(entries: list[LoggedEntry], tz: tzinfo | None = None) -> list[str]:
    """A day-by-day view of who worked on what, and when.

    The coverage table answers "is this design thread complete". This answers
    "what happened at Tuesday's meeting" — the other half of what a judge flips
    through a notebook looking for, and the half a component-grouped export
    structurally cannot show.

    Times, never durations. A span is the window in which the work was talked
    about, not hours worked: someone can machine a part for two hours in
    silence and leave a one-minute span behind. Rendering "1h51m" here, or
    summing spans into a season total, would be a confidently wrong number in
    front of a judge. See docs/design/progress-tracker.md §7.
    """
    # `now` only decides whether a span is open, which this table never prints.
    runs = spans(entries, now=max(e.created_at for e in entries))
    if not runs:
        return []

    out = [
        "## Sessions",
        "",
        "| Day | Who | Component | Active | Stages |",
        "|---|---|---|---|---|",
    ]
    for span in runs:
        start = span.started_at.astimezone(tz) if tz else span.started_at
        end = span.last_at.astimezone(tz) if tz else span.last_at
        window = start.strftime("%H:%M")
        if span.last_at != span.started_at:
            window += f"–{end.strftime('%H:%M')}"
        # dict.fromkeys dedupes while keeping the order it happened in.
        arc = " → ".join(dict.fromkeys(s.value for s in span.stages))
        out.append(
            f"| {_date(span.started_at, tz)} | {span.author or '—'} "
            f"| {span.component or UNFILED} | {window} | {arc} |"
        )
    out.append("")
    return out
```

Then call it in `render_notebook`. Find the end of the coverage table:

```python
    for name, group in threads.items():
        out.append(f"| {name} | {len(group)} | {_arc(group)} | {_gaps(group)} |")

    out.append("")
```

and add one line after it:

```python
    for name, group in threads.items():
        out.append(f"| {name} | {len(group)} | {_arc(group)} | {_gaps(group)} |")

    out.append("")
    out += _timeline(logged, tz)
```

- [x] **Step 4: Run the test and watch it pass**

```bash
uv run python -m tests.test_core
```

Expected: everything `ok`, including `ok  test_notebook_timeline`. `test_notebook`
must still pass untouched — if it fails, the timeline was inserted in the wrong
place.

- [x] **Step 5: Commit**

```bash
git add exporters/notebook.py tests/test_core.py
git commit -m "feat: notebook renders a session timeline alongside the coverage table"
```

---

## Task 4: update the docs so the next reader is not lied to

`CLAUDE.md` currently rules out task management in a sentence that, read
literally, also rules out `core/progress.py`. Leaving that is how a future
reader deletes this feature as out of scope.

**Files:**
- Modify: `CLAUDE.md` (§3, §4, §10)
- Modify: `spec.md`
- Modify: `docs/design/progress-tracker.md`

- [x] **Step 1: Clarify the scope line in `CLAUDE.md` §3**

In the "Explicitly OUT of scope for v1" list, replace

```markdown
- Task management / kanban
```

with

```markdown
- Task management / kanban — still out. Note this is *not* the same as the work
  spans in `core/progress.py`, which are *derived* from captured entries
  (who was on what, when) and are read-only. Out means: creating a task by
  command, assignment, owners, due dates, "mark done". Nothing a human can edit.
```

This mirrors the wording already used for the RAG exclusion two lines below,
which draws the same kind of line for `core/storage.py`'s vector search.

- [x] **Step 2: Add `core/progress.py` to the §4 file tree**

In the tree, after the `core/followup.py` line, add:

```
│   ├── progress.py            # who is on what, derived — see docs/design/
```

- [x] **Step 3: Update §10 status**

In the "Working and verified end to end" list, add:

```markdown
- **`core/progress.py`** — per-person work spans, derived from entries with no
  extra model call. Two consumers: `pipeline._question_for`'s one-question-
  per-task gate and the notebook's session timeline. Pure and unit-tested.
```

In "Known defects", add:

```markdown
- `TASK_IDLE_MINUTES` (default 60) has never been measured against real channel
  history. Too small fragments one task into five spans; too large collapses a
  whole meeting into one. Settle it the way §9 settles everything else — with
  real messages, not invented ones.
```

- [x] **Step 4: Add the node to `spec.md`'s diagram**

Inside the `core_layer` subgraph, after the `followup` node, add:

```
        progress["core/progress.py<br/>Work spans, derived"]
```

and add the two edges that show its consumers, next to the existing edge
definitions:

```
    pipeline --> progress
    notebook --> progress
```

- [x] **Step 5: Flip the design doc's status**

In `docs/design/progress-tracker.md`, replace

```markdown
**Status:** design only. Nothing is built. This document argues for a shape;
the implementation plan comes after it is agreed.
```

with

```markdown
**Status:** implemented. `core/progress.py` plus the two consumers named in §9
are in the tree; the open questions in §12 are still open. Built by
`docs/superpowers/plans/2026-09-01-progress-tracker.md`, which corrects two
details of this document — no storage change was needed, and the follow-up gate
carries no scoring harness because it involves no model behaviour.
```

- [x] **Step 6: Run everything once more, then commit**

```bash
uv run python -m tests.test_core
```

```bash
git add CLAUDE.md spec.md docs/design/progress-tracker.md
git commit -m "docs: record the progress tracker and redraw the kanban scope line"
```

---

## Self-review notes

Checked against `docs/design/progress-tracker.md`:

- §5 shape → Task 1, verbatim.
- §6 segmentation rules, all five → Task 1, one test case each.
- §6 "spans may overlap" → Task 1 test, the component-switch case.
- §6 "closing is lazy" → satisfied by construction: `spans()` takes `now`, and
  no scheduler exists anywhere in the plan.
- §7 "not hours worked" → Task 3, enforced by an assertion, not just a comment.
- §8 file table → matches the File Structure section above, minus the
  `storage.py` row, which the corrections section explains away.
- §9 phase 2 gate → Task 2. Phase 2's scoring metric is deliberately dropped;
  reason in the corrections section.
- §9 phase 3 timeline → Task 3.
- §10 config → Task 1's `IDLE`, with the unmeasured warning carried into both
  the code comment and `CLAUDE.md` §10.
- §11 tests, all eight rows → Task 1's `test_spans` covers rows 1–8.
- §12 open questions → carried into `CLAUDE.md` as a known defect (idle
  default) and left open elsewhere, which is the honest outcome.
- §13 "deliberately not doing" → no table, no summary call, no sweeper, no
  duration arithmetic appears anywhere in this plan.

Type consistency: `Span`, `spans()`, `current()` and `_span_is_busy()` carry the
same signatures in the Interfaces blocks, the implementation steps, and the
tests. `spans()` is called with `now=` in all three call sites (test, gate,
notebook).

---

## Still not done after this plan

Unchanged by this work, listed so nobody reads a green test run as more than it
is:

- `TASK_IDLE_MINUTES = 60` is an unmeasured guess, and the only way to settle it
  is a month of real channel history.
- Whether the one-question-per-task gate makes the bot *too* quiet cannot be
  answered by any fixture in this repo. It needs the same real messages
  `tests/samples.py` has been waiting for since §9 was written.
- `tests/samples.py` and `tests/conversations.py` are still invented.
- `scripts/export.py` still does not exist, so nothing renders a notebook from
  the database — including the new timeline.
- The bot has still never connected to a real Discord server.
