# Conversational Capture Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Execute task-by-task; each task ends with a runnable check and a commit.

**Goal:** Make the agent survive how people actually talk — one thought split across four messages, and one hole that needs more than one question to close — while spending fewer LLM calls and asking fewer questions than it does today.

**Architecture:** Three additions, all in `core/`. A **coalescer** buffers a person's burst and hands the whole burst over as one unit. A **triage gate** decides in pure Python whether a burst is worth an LLM call at all. A **follow-up ledger** replaces the single-shot `followup_*` columns with a list of turns, so a conversation can run 2–3 rounds and stop the moment it stops earning its place. A new `core/pipeline.py` holds the policy that currently leaks into `channels/discord_bot.py`, which shrinks back to ears and mouth.

**Tech Stack:** Python 3.13, uv, pydantic-ai + DeepSeek (`deepseek-chat`), discord.py, PostgreSQL + pgvector via asyncpg. **No new dependencies.**

**Spec:** `CLAUDE.md` (§4 layering, §7 schema rules, §9 workflow, §11 working agreements), `notes.md` (scoring log), `spec.md` (current structure).

---

## Global Constraints

- **No new dependencies.** stdlib + what is already installed. (`CLAUDE.md` §11)
- **`core/` must not know Discord exists and must not know DeepSeek exists.** Only `core/agent.py` knows the model vendor; only `core/storage.py` knows Postgres. (§4, §5)
- **No `if` that is a judgment call inside `channels/`.** (§4)
- **`exporters/` and `channels/` must not import `core/storage.py`.** Callers wire them. `core/pipeline.py` is a core module and may import storage. (§11)
- **Record-integrity rules go in Python, not in prompts.** `apply_patch` is the pattern. (§11)
- **Every enum keeps an `UNKNOWN` escape hatch; every uncertain field is `Optional[...] = None`; lists use `default_factory=list`; enums subclass `str`.** (§7)
- **`missing_fields` stays restricted to exactly four names:** `problem_statement`, `alternatives_considered`, `rationale`, `test_evidence`. (§7)
- **Prompts stay in `core/prompts/*.md`, in English.** Prefer editing a prompt over editing Python when the fix is behavioral. (§8, §11)
- **One change per scoring run**, logged as a row in `notes.md`. If a score drops, roll back. (§9)
- **Ship gates:** chitchat silence non-negotiable at 3/3 on the current set (15/15 once real samples land); stage ≥ 13/15.
- Model env var is `LLM_API_KEY` (read with `os.environ[...]`, never `os.getenv`). Model id is `deepseek-chat`. (§5, §6.5)

---

## Design decisions this plan implements

These are the three questions the plan answers. Read this section before Task 0; every task below is downstream of it.

### Q1 — the content is spread across several messages

Two different problems wear this costume. Separate them, because they have different fixes and only one of them needs new code.

**(a) The burst — one thought, four messages, twenty seconds.**

```
alex  19:41  intake keeps jamming
alex  19:41  like when two blocks come in at the same time
alex  19:42  tried compliant wheels, didn't help much
alex  19:42  going dual roller, it fits the current mount
```

Today: four LLM calls, four fragment records, up to four follow-up questions
fired at one person. Every one of those numbers is wrong.

Fix: **debounce per (channel, author)**. Buffer, flush after `BURST_QUIET_SECONDS`
of silence from that person (default 45s), parse the joined text as one unit.
One call, one record, one question. This is the single largest cost reduction in
the plan and it needs no model change.

Cost of the delay: the bot's reply arrives ~45s later. In a live channel that
reads as *less* twitchy, not more. Anchor the entry to the **first** message id
(dedup key, stable across redelivery) and post the reply to the **last** one.

**(b) The thread — one design line, three meetings, three weeks.**

Do **not** merge these. `exporters/notebook.py::_gaps` already treats a component
thread as the unit and computes gaps across it (§10 finding 1) — that solved the
export side and this plan must not re-solve it.

What is *not* solved is the asking side: the bot asks "what problem was this
fixing?" about a message whose problem was stated last Tuesday. That is the most
annoying failure mode the product has, and it is a pure-Python gate, not an LLM
problem: before asking, look up the component thread and drop any question about
a field the thread already fills. Same rule as `_gaps` — so lift it into
`core/followup.py` and have the exporter call it too. One definition of "this
thread is missing X."

**Explicitly not built:** cross-burst record merging, an LLM pass that fuses two
records, Discord-thread awareness. All three are speculative and (b)'s gate plus
the exporter's per-thread grouping covers the observable symptom.

### Q2 — follow-ups should run multiple rounds, and stop when pointless

Today `LoggedEntry` carries four scalar `followup_*` columns and
`apply_patch` hard-sets `followup_question = None`. One question per record,
forever. That is a deliberate anti-nag design and it is over-tight: the common
real shape is *"why dual roller?" → "weight" → "how much lighter?"*, two rounds
to get a rationale a judge can read.

Replace the four scalars with `followups: list[FollowupTurn]`. Then multi-round
is a policy over that list, and the stop conditions become explicit and testable:

| Stop when | Why |
|---|---|
| `missing_fields ∩ PATCHABLE_FIELDS` is empty | Nothing left to ask. |
| The last reply had `answered=False` | A shrug is an answer. Re-asking is nagging. |
| The last round filled nothing | The question missed; a rephrase costs goodwill. |
| `len(followups) >= FOLLOWUP_MAX_ROUNDS` (default 3) | Hard ceiling. |
| The component thread already fills the field (Q1b) | Already known. |
| The channel already has `MAX_OPEN_QUESTIONS` unanswered | Budget (Q3). |

**The next question costs zero extra calls.** `FollowupPatch` gains one field at
the end — `next_question` — so the merge call, which already has the record, the
question asked and the reply in context, emits the next question in the same
response. A fourth prompt file would be more drift for no gain. Python gates the
model's suggestion: if policy says stop, the question is dropped regardless of
what the model returned.

### Q3 — how to make it cheaper (省力), for the team and for the wallet

Four levers, in descending value per line of code:

1. **Coalescing (Q1a).** A four-message burst is 1 call instead of 4. On a live
   meeting channel this is the dominant term.
2. **Triage before the call.** Most Discord traffic is `lol`, `omw`, `who's
   driving`, a link. A ~20-line pure-Python gate skips them entirely: no call,
   no row, no notebook footer noise. Conservative on purpose — a false negative
   loses real content, a false positive only costs one call — so anything long,
   numeric, or naming a robot part goes through. The keyword list is a
   **calibration knob**, not a finished artifact; it is scored offline against
   `tests/samples.py` for free.
3. **Question budget.** At most `MAX_OPEN_QUESTIONS` (default 2) unanswered
   questions per channel at a time. The bot asking six things during one meeting
   is how it gets muted in week one — which is exactly the §2 survival metric.
4. **The thread gate (Q1b).** Not asking a question whose answer is already in
   the log is the cheapest possible call: zero.

Levers considered and **skipped**: an end-of-meeting digest instead of live
questions (needs a scheduler; revisit after four weeks of live data), reaction-
based answers (can't fill a text field), a cheaper model for triage (Python is
cheaper than any model).

### What this changes about measurement

Coalescing and multi-round follow-ups are conversation-level behaviours;
`tests/samples.py` is single-message and cannot see them. `scripts/try_parse.py`
gains a triage line (free, offline). A new `tests/conversations.py` +
`scripts/try_conversation.py` measures the two things multi-round can get wrong:
**rounds used** and **nags** (a question asked after a non-answer, or after a
round that filled nothing). Nags must be 0.

**None of these numbers mean anything until Task 0 lands.** `notes.md` already
says the current baseline is untrustworthy for two reasons; Task 0 fixes one of
them (the model id) and every later task re-records against it.

---

## File Structure

**New:**

| File | Responsibility |
|---|---|
| `core/triage.py` | Pure. Is this text worth an LLM call? ~25 lines. |
| `core/inbox.py` | Generic quiet-window coalescer. Knows nothing about messages, only keys and items. ~50 lines. |
| `core/followup.py` | The follow-up conversation's rules: `apply_patch` (moved from `schema.py`), thread-gap computation, multi-round stop policy. ~80 lines. |
| `core/pipeline.py` | The one place ingest policy lives: dedup → triage → parse → persist → decide whether to ask. ~90 lines. |
| `tests/conversations.py` | Scripted multi-message, multi-round fixtures. |
| `scripts/try_conversation.py` | Scores rounds and nags. |

**Modified:**

| File | Change |
|---|---|
| `core/schema.py` | Add `FollowupTurn`; `LoggedEntry.followups` replaces four scalars; `FollowupPatch.next_question`; `apply_patch` moves out. |
| `core/storage.py` | `followups jsonb` + generated `open_followup_message_id`; new `find_by_open_followup`, `list_thread`, `count_open_followups`. |
| `core/agent.py` | `apply_followup_answer` becomes turn-aware; model id default. |
| `channels/discord_bot.py` | Wires the coalescer, calls `pipeline`, keeps no policy. |
| `exporters/notebook.py` | `_gaps` delegates to `core.followup.thread_gaps`. |
| `tests/test_core.py` | Cases for triage, coalescer, thread gate, round policy. |
| `scripts/try_parse.py` | Triage line. |
| `.env.example`, `notes.md`, `spec.md`, `CLAUDE.md` | Config, scores, structure diagram, docs. |

**Migration:** there is no production data — the bot has never connected to
Discord (§10). The follow-up column change is applied by dropping and recreating
the dev table, not by writing a migration. Task 5 says so explicitly.

---

## Task 0: Settle the model and re-record the baseline

Blocking. A score is bound to a model, and `notes.md` says the two rows on file
were taken against `deepseek-v4-flash-vision-exp` — a vision/experimental model
sitting next to gotchas 3 and 4. Nothing later in this plan can be judged until
this row exists.

**Files:**
- Modify: `core/agent.py:27`
- Modify: `.env` (not tracked), `.env.example`
- Modify: `notes.md`

**Interfaces:**
- Produces: a `notes.md` row every later task compares against.

- [x] **Step 1: Change the fallback default to the settled model**

`core/agent.py` line 27:

```python
_model = OpenAIChatModel(
    os.getenv("LLM_MODEL", "deepseek-chat"),
    provider=DeepSeekProvider(api_key=os.environ["LLM_API_KEY"]),
)
```

- [x] **Step 2: Point `.env` at the same model**

```bash
grep -n LLM_MODEL .env .env.example
# set both to: LLM_MODEL=deepseek-chat
```

- [x] **Step 3: Run the scoring loop**

Run: `uv run python -m scripts.try_parse`
Expected: three numbers print. Do not tune anything if they drop — record them.

- [x] **Step 4: Record the row in `notes.md`**

Append to the table:

```markdown
| 3 | 2026-09-01 | model settled to deepseek-chat (no prompt change) | deepseek-chat | ?/15 | ?/3 | ?/15 |
```

Replace `?` with the real numbers. If `silence` is below 3/3, **stop the plan
here** and fix the prompt before continuing — that gate is non-negotiable and
everything downstream assumes it holds.

- [x] **Step 5: Commit**

```bash
git add core/agent.py .env.example notes.md
git commit -m "fix: settle model on deepseek-chat and re-record baseline"
```

---

## Task 1: `core/triage.py` — skip what cannot be a design record

**Files:**
- Create: `core/triage.py`
- Modify: `tests/test_core.py`
- Modify: `scripts/try_parse.py`
- Modify: `notes.md`

**Interfaces:**
- Produces: `core.triage.worth_parsing(text: str) -> bool`, and
  `core.triage.KEYWORDS: frozenset[str]` (the calibration knob).

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py` (and add `from core import triage` and
`from tests.samples import SAMPLES` to the imports at the top):

```python
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
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run python -m tests.test_core`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.triage'`

- [x] **Step 3: Write the implementation**

Create `core/triage.py`:

```python
"""Cheap pre-filter: is this text worth an LLM call at all?

Most of what lands in a team Discord is "lol", "omw", a link, or a question
about snacks. Sending each one to the model costs a call, writes a row, and
inflates the notebook's "classified as unrelated" footer with noise that was
never a judgement call in the first place.

Deliberately conservative. A false negative silently loses real content — the
one failure mode this project cannot tolerate — while a false positive costs
one call and lands as stage=unknown anyway. So: anything long, anything with a
number in it, and anything naming a robot part goes through.

This runs on a coalesced burst (see core/inbox.py), not on single messages.
"it broke again" on its own is skipped; "it broke again" followed by "the arm
mount snapped" is one unit and passes.
"""

import re

# ponytail: hand-kept keyword list, and a calibration knob rather than a
# finished artifact — it is game- and team-specific and will rot between
# seasons. Retune it against tests/samples.py (test_triage guards the floor).
# Upgrade path if it starts costing recall: drop the list and gate on length
# alone, which is strictly safer and only slightly more expensive.
KEYWORDS = frozenset("""
    intake slide slides arm claw grabber odometry odo auto autonomous teleop
    opmode pid pathing vision april apriltag limelight camera
    motor encoder servo gear belt chain sprocket spool bearing bushing
    wheel wheels drivetrain chassis frame mount bracket plate standoff
    hang climb spec specimen sample basket bucket hook linkage fourbar
    cad print printed tolerance flex jam jammed stall slip torque rpm
    battery wiring hub expansion sensor limit switch
""".split())

_WORD = re.compile(r"[a-z]+")

# A burst this long is somebody explaining something. Let the model read it.
_LONG_ENOUGH = 80


def worth_parsing(text: str) -> bool:
    """False only when the text cannot plausibly contain a design record."""
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) >= _LONG_ENOUGH:
        return True
    if any(ch.isdigit() for ch in stripped):
        return True

    words = set(_WORD.findall(stripped.lower()))
    if words & KEYWORDS:
        return True
    return len(words) > 12
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.test_core`
Expected: `ok  test_triage` alongside the existing three.

If `test_triage`'s SAMPLES loop fails, the fix is **adding the missing word to
`KEYWORDS`**, never loosening the gate — that loop is the recall floor.

- [x] **Step 5: Report triage in the scoring loop**

In `scripts/try_parse.py`, before `results = await asyncio.gather(...)`, add:

```python
    from core import triage
    from core.schema import Stage

    skipped = [s for s in SAMPLES if not triage.worth_parsing(s.text)]
    wrongly = [s for s in skipped if s.stage is not Stage.UNKNOWN]
```

and in the printout block, above the `stage` line:

```python
    print(f"triage   skipped {len(skipped)}/{n} before any call "
          f"({len(wrongly)} of them real — must be 0)")
```

Every skipped sample is silence by definition, so also count it as such — change
the silence tally to start from the skipped chitchat:

```python
    silent_ok = sum(1 for s in skipped if s.silent)
```

and skip the already-counted ones in the results loop by guarding the
`if sample.silent:` branch with `if sample.silent and sample in skipped: continue`
placed just above it.

- [x] **Step 6: Run the scoring loop and record**

Run: `uv run python -m scripts.try_parse`
Expected: `wrongly` is 0. Calls made drop by the number of skipped samples.

Append to `notes.md`:

```markdown
| 4 | 2026-09-01 | added core/triage.py pre-filter (no prompt change) | deepseek-chat | ?/15 | ?/3 | ?/15 |
```

Add a line under the table noting how many of the 15 never reached the model.

- [x] **Step 7: Commit**

```bash
git add core/triage.py tests/test_core.py scripts/try_parse.py notes.md
git commit -m "feat: pre-filter messages that cannot contain a design record"
```

---

## Task 2: `core/inbox.py` — coalesce a burst into one unit

**Files:**
- Create: `core/inbox.py`
- Modify: `tests/test_core.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  ```python
  class Coalescer:
      def __init__(self, flush: Callable[[str, list], Awaitable[None]], *,
                   quiet: float = QUIET_SECONDS, max_items: int = MAX_ITEMS) -> None
      async def add(self, key: str, item: object) -> None
      async def drain(self) -> None
  ```
  Generic on purpose: `item` is whatever the channel puts in, `key` is whatever
  the channel considers "one person talking in one place". `core/` therefore
  stays ignorant of Discord.

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py` (add `import asyncio` and `from core.inbox import Coalescer`):

```python
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
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run python -m tests.test_core`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.inbox'`

- [x] **Step 3: Write the implementation**

Create `core/inbox.py`:

```python
"""Coalesce a burst of items under one key into a single unit of meaning.

People type the way they think: four lines in twenty seconds, one thought.

    19:41  intake keeps jamming
    19:41  like when two blocks come in at the same time
    19:42  tried compliant wheels, didn't help much
    19:42  going dual roller, it fits the current mount

Handled one at a time that is four model calls, four fragment records in the
notebook, and up to four follow-up questions aimed at one person. Handled as a
burst it is one call and one question.

Generic on purpose — `key` and `item` are opaque here. A channel decides that a
key is "this author in this channel"; core does not need to know that Discord
exists to buffer a list.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

# How long one person has to stay quiet before their burst is considered over.
# Tunable: too short splits a thought, too long makes the bot feel asleep.
QUIET_SECONDS = float(os.getenv("BURST_QUIET_SECONDS", "45"))

# A monologue longer than this is flushed without waiting. Bounds both memory
# and the worst-case prompt length.
MAX_ITEMS = int(os.getenv("BURST_MAX_ITEMS", "10"))

Flush = Callable[[str, list], Awaitable[None]]


class Coalescer:
    """Buffer items per key; flush after `quiet` seconds of silence."""

    def __init__(self, flush: Flush, *, quiet: float = QUIET_SECONDS,
                 max_items: int = MAX_ITEMS) -> None:
        self._flush = flush
        self._quiet = quiet
        self._max_items = max_items
        self._buffers: dict[str, list] = {}
        self._timers: dict[str, asyncio.Task] = {}

    async def add(self, key: str, item: object) -> None:
        buffer = self._buffers.setdefault(key, [])
        buffer.append(item)

        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()

        if len(buffer) >= self._max_items:
            await self._fire(key)
        else:
            self._timers[key] = asyncio.create_task(self._after_quiet(key))

    async def drain(self) -> None:
        """Flush everything now. Call on shutdown, or the last burst of the
        meeting — the one most likely to be the actual recap — is lost."""
        for key in list(self._buffers):
            timer = self._timers.pop(key, None)
            if timer is not None:
                timer.cancel()
            await self._fire(key)

    async def _after_quiet(self, key: str) -> None:
        try:
            await asyncio.sleep(self._quiet)
        except asyncio.CancelledError:
            return
        await self._fire(key)

    async def _fire(self, key: str) -> None:
        items = self._buffers.pop(key, [])
        self._timers.pop(key, None)
        if not items:
            return
        try:
            await self._flush(key, items)
        except Exception:
            # One bad burst must not take the timer machinery down with it.
            log.exception("flush failed for %s, dropping %d item(s)", key, len(items))
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.test_core`
Expected: `ok  test_coalescer`

- [x] **Step 5: Document the knobs**

Append to `.env.example`:

```bash
# How long one person must stay quiet before their burst is parsed as one unit.
BURST_QUIET_SECONDS=45
# Flush a monologue without waiting once it reaches this many messages.
BURST_MAX_ITEMS=10
```

- [x] **Step 6: Commit**

```bash
git add core/inbox.py tests/test_core.py .env.example
git commit -m "feat: coalesce a burst of messages into one unit"
```

---

## Task 3: `core/schema.py` — a follow-up ledger instead of four scalars

Pure data change. Behaviour comes in Tasks 4–6; this task keeps the tree green
by moving `apply_patch` and updating its callers.

**Files:**
- Modify: `core/schema.py`
- Create: `core/followup.py`
- Modify: `core/agent.py` (import site only)
- Modify: `tests/test_core.py` (import site + envelope test)

**Interfaces:**
- Produces:
  ```python
  class FollowupTurn(BaseModel):
      question: str
      message_id: Optional[str] = None
      asked_at: Optional[datetime] = None
      answer: Optional[str] = None
      answered_at: Optional[datetime] = None
      filled: list[str] = []          # field names this round actually closed

  LoggedEntry.followups: list[FollowupTurn]
  LoggedEntry.open_followup_message_id -> Optional[str]   # property
  LoggedEntry.awaiting_followup -> bool                   # property, unchanged meaning
  LoggedEntry.mark_followup_asked(question, message_id, at=None) -> LoggedEntry
  LoggedEntry.record_followup_answer(answer, filled, at=None) -> LoggedEntry

  FollowupPatch.next_question: Optional[str]

  core.followup.apply_patch(record, patch) -> DesignRecord   # moved from schema
  ```
- Removed: `LoggedEntry.followup_message_id`, `.followup_asked_at`,
  `.followup_answer`, `.followup_answered_at`, and `schema.apply_patch`.

- [x] **Step 1: Write the failing test**

First fix the one assertion in `test_patch_gate` that this task's behaviour
change invalidates. Dropping `apply_patch`'s `if not updates: return record`
early exit means an answered-but-empty patch now still clears the question it
answered — which is right (it was asked and answered; carrying it forward would
let it be re-asked) but no longer `== rec`. Replace:

```python
    assert _apply_patch(rec, FollowupPatch(answered=True)) == rec
```

with:

```python
    # A reply that answered but supplied nothing still closes the question it
    # answered — carrying it forward would let the bot re-ask it.
    empty = _apply_patch(rec, FollowupPatch(answered=True))
    assert empty.missing_fields == ["rationale"] and empty.followup_question is None
    # A patch may propose the next question; posting it is core/followup's call.
    assert _apply_patch(rec, FollowupPatch(answered=True, rationale="lighter",
                                           next_question="how much lighter?")
                        ).followup_question == "how much lighter?"
```

Then replace `test_envelope` in `tests/test_core.py` with:

```python
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
```

and change the import block at the top of `tests/test_core.py` to:

```python
from core.followup import apply_patch as _apply_patch
from core.schema import (
    DesignRecord,
    FollowupPatch,
    LoggedEntry,
    Stage,
    Subteam,
)
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run python -m tests.test_core`
Expected: FAIL — `ImportError: cannot import name 'apply_patch' from 'core.followup'`

- [x] **Step 3: Add `FollowupTurn` and rework `LoggedEntry`**

In `core/schema.py`, add above `LoggedEntry`:

```python
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
```

Then in `LoggedEntry`, delete the four `followup_*` fields, the
`awaiting_followup` property and `mark_followup_asked`, and replace them with:

```python
    # ── Follow-up lifecycle ──────────────────────────────────────────────────
    # Append-only. The last turn is the live one; everything before it is the
    # conversation that got the record this far.
    followups: list[FollowupTurn] = Field(default_factory=list)

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
```

- [x] **Step 4: Add `next_question` to `FollowupPatch`**

Append as the **last** field of `FollowupPatch` in `core/schema.py` (§7: action
fields go last, so the model's prior analysis is in context):

```python
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
```

Whether this question is actually asked is decided in Python
(`core/followup.py`), never by the model alone.

- [x] **Step 5: Move `apply_patch` into `core/followup.py`**

Create `core/followup.py` and move the existing `apply_patch` function and the
`PATCHABLE_FIELDS` tuple into it verbatim, with these two changes to
`apply_patch`'s body — carry the next question through instead of always
clearing it, and keep the no-op path honest:

```python
"""The follow-up conversation's rules — the merge gate and the stop policy.

Kept out of core/agent.py so pure callers (tests, the exporter) never need an
LLM_API_KEY just to import a merge rule, and out of core/schema.py so the schema
stays a contract rather than a rulebook.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Optional

from dotenv import load_dotenv

if TYPE_CHECKING:
    from .schema import DesignRecord, FollowupPatch, LoggedEntry

load_dotenv()

# ── The four fields a follow-up is allowed to fill ────────────────────────────
PATCHABLE_FIELDS = (
    "problem_statement",
    "alternatives_considered",
    "rationale",
    "test_evidence",
)

# Hard ceiling on rounds per record. Three is two more than the old design and
# still short enough that nobody feels interrogated.
MAX_ROUNDS = int(os.getenv("FOLLOWUP_MAX_ROUNDS", "3"))

# Unanswered questions allowed to be outstanding in one channel at once. The
# bot asking six things during one meeting is how it gets muted in week one.
MAX_OPEN_QUESTIONS = int(os.getenv("MAX_OPEN_QUESTIONS", "2"))


def apply_patch(record: "DesignRecord", patch: "FollowupPatch") -> "DesignRecord":
    """Merge a patch into a record — the gate, enforced in Python.

    Two guarantees the prompt alone could not give us:
      1. only PATCHABLE_FIELDS can change, so a casual reply can never quietly
         rewrite stage, title or summary;
      2. only fields the record itself declared missing can be written, so the
         reply cannot overwrite something the team already said.

    followup_question is carried forward as the patch's next_question. That is
    a *proposal*, not a decision — should_ask_again() below is what decides
    whether it is ever posted.
    """
    if not patch.answered:
        return record

    allowed = set(record.missing_fields) & set(PATCHABLE_FIELDS)
    updates: dict[str, object] = {}

    for name in PATCHABLE_FIELDS:
        if name not in allowed:
            continue
        value = getattr(patch, name)
        if not value:  # None, "", or [] — all mean "nothing supplied"
            continue
        updates[name] = value

    return record.model_copy(
        update={
            **updates,
            "missing_fields": [
                f for f in record.missing_fields if f not in updates
            ],
            "followup_question": patch.next_question,
        }
    )
```

Note the deleted `if not updates: return record` early exit — the record must
still pick up `next_question` when a reply answered but filled nothing, so the
policy below can see that and stop.

- [x] **Step 6: Update the two import sites**

In `core/agent.py`, change:

```python
from .followup import PATCHABLE_FIELDS, apply_patch
from .schema import DesignRecord, FollowupPatch, LoggedEntry
```

and delete `PATCHABLE_FIELDS` / `apply_patch` from the `.schema` import list.

- [x] **Step 7: Run the tests**

Run: `uv run python -m tests.test_core`
Expected: all pass. `test_patch_gate`'s last assertion —
`assert out.followup_question is None` — still holds, because a
`FollowupPatch` built without `next_question` defaults it to `None`.

- [x] **Step 8: Commit**

```bash
git add core/schema.py core/followup.py core/agent.py tests/test_core.py
git commit -m "refactor: follow-up ledger replaces the four scalar followup fields"
```

---

## Task 4: `core/followup.py` — thread gaps and the multi-round stop policy

The judgement layer. All pure, all testable without an API key or a database.

**Files:**
- Modify: `core/followup.py`
- Modify: `exporters/notebook.py`
- Modify: `tests/test_core.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: `PATCHABLE_FIELDS`, `MAX_ROUNDS` from Task 3.
- Produces:
  ```python
  thread_gaps(entries: list[LoggedEntry]) -> set[str]      # field names never filled
  open_gaps(record: DesignRecord, thread: list[LoggedEntry]) -> set[str]
  should_ask_again(entry: LoggedEntry) -> bool
  ```

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py` (add `from core import followup`):

```python
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
```

- [x] **Step 2: Run it to verify it fails**

Run: `uv run python -m tests.test_core`
Expected: FAIL — `AttributeError: module 'core.followup' has no attribute 'thread_gaps'`

- [x] **Step 3: Write the implementation**

Append to `core/followup.py`:

```python
def thread_gaps(entries: list["LoggedEntry"]) -> set[str]:
    """Which patchable fields no entry in this design thread ever fills.

    Deliberately NOT a sum of per-entry `missing_fields`. That field is scoped
    to one message and exists to drive one follow-up. A design line whose
    problem, alternatives, rationale and results are spread across three
    entries is complete — rolling up per-entry gaps reports it as full of
    holes and sends the team chasing nothing.

    One definition, two consumers: the notebook's coverage table renders it,
    and open_gaps() below uses it to keep the bot from asking about something
    the log already answered.
    """
    return {
        name
        for name in PATCHABLE_FIELDS
        if not any(getattr(e.record, name) for e in entries)
    }


def open_gaps(record: "DesignRecord", thread: list["LoggedEntry"]) -> set[str]:
    """What is still worth asking about: this message's holes, minus what the
    rest of its component thread already supplies.

    `record.missing_fields` is left alone on purpose — it is the truth about
    one message and the exporter reads it as such. This is a question filter,
    not a rewrite.
    """
    return set(record.missing_fields) & thread_gaps(thread)


def should_ask_again(entry: "LoggedEntry") -> bool:
    """Is another round still earning its place?

    Everything here is a stop condition. The default is silence; §8's rule that
    a follow-up posts publicly in a live channel does not stop applying just
    because the conversation is already underway.
    """
    turns = entry.followups
    if len(turns) >= MAX_ROUNDS:
        return False
    if not turns:
        return True

    last = turns[-1]
    if last.answered_at is None:
        return False        # still waiting on the current one
    if not last.filled:
        return False        # the reply added nothing: the question missed
    return bool(set(entry.record.missing_fields) & set(PATCHABLE_FIELDS))
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m tests.test_core`
Expected: `ok  test_thread_gaps`, `ok  test_should_ask_again`

- [x] **Step 5: Point the exporter at the shared definition**

In `exporters/notebook.py`, change the import line and `_gaps`:

```python
from core.followup import thread_gaps
from core.schema import LoggedEntry, Stage
```

```python
def _gaps(entries: list[LoggedEntry]) -> str:
    """What this thread never supplies anywhere.

    The rule lives in core.followup.thread_gaps because the follow-up policy
    needs exactly the same answer — a question about a field the thread already
    fills is the most annoying thing the bot can ask.
    """
    absent = thread_gaps(entries)
    labels = [label for name, label in SECTIONS if name in absent]
    return ", ".join(label.lower() for label in labels) if labels else "—"
```

`SECTIONS` and `PATCHABLE_FIELDS` list the same four field names in the same
order, so the rendered table is byte-identical to before — `test_notebook`
asserts the exact row strings and must still pass untouched.

- [x] **Step 6: Run the tests again**

Run: `uv run python -m tests.test_core`
Expected: `ok  test_notebook` unchanged. If the row strings shifted, `SECTIONS`
and `PATCHABLE_FIELDS` have drifted apart — reconcile them, do not edit the
assertions.

- [x] **Step 7: Document the knobs**

Append to `.env.example`:

```bash
# Hard ceiling on follow-up rounds per record.
FOLLOWUP_MAX_ROUNDS=3
# Unanswered questions allowed outstanding in one channel at once.
MAX_OPEN_QUESTIONS=2
```

- [x] **Step 8: Commit**

```bash
git add core/followup.py exporters/notebook.py tests/test_core.py .env.example
git commit -m "feat: thread-aware gap detection and a multi-round stop policy"
```

---

## Task 5: `core/storage.py` — persist the ledger, and the two new lookups

**Files:**
- Modify: `core/storage.py`

**Interfaces:**
- Consumes: `LoggedEntry.followups` (Task 3).
- Produces:
  ```python
  async def find_by_open_followup(message_id: str) -> Optional[LoggedEntry]
  async def list_thread(channel: str, component: Optional[str], *, limit: int = 20) -> list[LoggedEntry]
  async def count_open_followups(channel: str, *, since: datetime) -> int
  ```
- Removed: `find_by_followup_message_id` (replaced by `find_by_open_followup`).

**Migration:** none. The bot has never connected to Discord and there is no
production data (§10). Recreate the dev table:

```bash
psql "$DATABASE_URL" -c 'DROP TABLE IF EXISTS entries'
```

`init_schema()` rebuilds it on next start.

- [x] **Step 1: Replace the four follow-up columns in `SCHEMA_SQL`**

In `core/storage.py`, inside `CREATE TABLE IF NOT EXISTS entries`, delete:

```sql
    followup_message_id  text,
    followup_asked_at    timestamptz,
    followup_answer      text,
    followup_answered_at timestamptz,
```

and put in their place:

```sql
    -- The follow-up conversation, whole, in the order it happened. jsonb for
    -- the same reason `record` is jsonb: the shape is still moving, and a
    -- generated column can never drift out of sync with what it derives from.
    followups jsonb NOT NULL DEFAULT '[]'::jsonb,
```

Then, next to the other generated columns, add:

```sql
    -- The routing key: the message id a reply must target to count as an
    -- answer, and NULL once that question has been answered — otherwise later
    -- chatter in the thread would overwrite an answer that already landed.
    open_followup_message_id text GENERATED ALWAYS AS (
        CASE WHEN followups -> -1 ->> 'answered_at' IS NULL
             THEN followups -> -1 ->> 'message_id' END
    ) STORED,
```

Replace the follow-up index with:

```sql
-- The follow-up routing lookup: a channel resolves "this is a reply to message
-- N" into the entry whose question it answers. Hot path, one row. Doubles as
-- the index behind the per-channel open-question budget.
CREATE INDEX IF NOT EXISTS entries_open_followup_idx
    ON entries (open_followup_message_id)
    WHERE open_followup_message_id IS NOT NULL;
```

- [x] **Step 2: Update `_COLUMNS` and the row mapper**

```python
_COLUMNS = """entry_id, channel, source, channel_message_id, author, created_at,
              raw_text, record, followups"""
```

```python
def _to_entry(row: asyncpg.Record) -> LoggedEntry:
    data = dict(row)
    raw = data.pop("record")
    followups = data.pop("followups", None) or []
    return LoggedEntry(
        **data,
        record=DesignRecord.model_validate(
            json.loads(raw) if isinstance(raw, str) else raw
        ),
        followups=json.loads(followups) if isinstance(followups, str) else followups,
    )
```

- [x] **Step 3: Update `save()`**

Replace the INSERT with:

```python
    async with (await pool()).acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO entries ({_COLUMNS}, embedding)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::vector)
            ON CONFLICT (entry_id) DO UPDATE SET
                record     = EXCLUDED.record,
                followups  = EXCLUDED.followups,
                embedding  = COALESCE(EXCLUDED.embedding, entries.embedding)
            """,
            entry.entry_id, entry.channel, entry.source, entry.channel_message_id,
            entry.author, entry.created_at, entry.raw_text,
            entry.record.model_dump_json(),
            json.dumps([t.model_dump(mode="json") for t in entry.followups]),
            vector,
        )
    return entry
```

`mode="json"` is what turns the turn's datetimes into ISO strings the generated
column can read back.

- [x] **Step 4: Replace `find_by_followup_message_id` with `find_by_open_followup`**

```python
async def find_by_open_followup(message_id: str) -> Optional[LoggedEntry]:
    """The follow-up routing lookup.

    A channel resolves "this message replies to N" into the entry whose live
    question N is, and hands it to pipeline.handle_reply. No match means the
    reply is an ordinary message and goes down the ambient path instead.

    Scoped by the generated column to *unanswered* questions only, so once a
    reply has landed, later chatter in the same thread cannot overwrite it.
    """
    async with (await pool()).acquire() as conn:
        row = await conn.fetchrow(
            f"""SELECT {_COLUMNS} FROM entries
                WHERE open_followup_message_id = $1""",
            message_id,
        )
    return _to_entry(row) if row else None
```

- [x] **Step 5: Add the thread lookup and the budget count**

```python
async def list_thread(
    channel: str, component: Optional[str], *, limit: int = 20
) -> list[LoggedEntry]:
    """The recent entries in one component's design thread, oldest first.

    This is what stops the bot asking about a problem the team stated last
    Tuesday. Entries with no component share the "unfiled" bucket, which is
    loose enough that the caller should treat a hit there as weak evidence.
    """
    key = (component or "").strip().lower()
    async with (await pool()).acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT {_COLUMNS} FROM entries
                WHERE channel = $1 AND component_key = $2
                ORDER BY created_at DESC LIMIT $3""",
            channel, key, limit,
        )
    return [_to_entry(r) for r in reversed(rows)]


async def count_open_followups(channel: str, *, since: datetime) -> int:
    """How many questions this channel is still waiting on.

    The bot asking six things during one meeting is how it gets muted in week
    one, so this is a hard budget rather than a heuristic.
    """
    async with (await pool()).acquire() as conn:
        return await conn.fetchval(
            """SELECT count(*) FROM entries
               WHERE channel = $1 AND created_at >= $2
                 AND open_followup_message_id IS NOT NULL""",
            channel, since,
        )
```

- [x] **Step 6: Verify against a real database**

```bash
psql "$DATABASE_URL" -c 'DROP TABLE IF EXISTS entries'
uv run python - <<'PY'
import asyncio, datetime as dt
from core import storage
from core.schema import DesignRecord, LoggedEntry, Stage, Subteam

async def main():
    await storage.init_schema()
    rec = DesignRecord(stage=Stage.DECISION, subteam=Subteam.MECHANICAL,
                       title="dual roller", summary="went dual roller",
                       component="Intake", missing_fields=["rationale"],
                       confidence=0.8)
    e = LoggedEntry(channel="t", channel_message_id="1", raw_text="x", record=rec)
    await storage.save(e.mark_followup_asked("why?", "m1"))

    assert (await storage.find_by_open_followup("m1")).entry_id == e.entry_id
    assert await storage.count_open_followups(
        "t", since=dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)) == 1

    got = await storage.find_by_open_followup("m1")
    await storage.save(got.record_followup_answer("lighter", ["rationale"]))
    assert await storage.find_by_open_followup("m1") is None, "answered must stop routing"

    thread = await storage.list_thread("t", "intake")
    assert len(thread) == 1 and thread[0].followups[-1].filled == ["rationale"]
    print("storage ok")
    await storage.close()

asyncio.run(main())
PY
```

Expected: `storage ok`

- [x] **Step 7: Commit**

```bash
git add core/storage.py
git commit -m "feat: persist the follow-up ledger and add thread/budget lookups"
```

---

## Task 6: `core/agent.py` — a turn-aware merge

**Files:**
- Modify: `core/agent.py`
- Modify: `core/prompts/followup_merge.md`

**Interfaces:**
- Consumes: `LoggedEntry.record_followup_answer` (Task 3), `apply_patch` (Task 3).
- Produces: `apply_followup_answer(entry, answer_text, at=None) -> LoggedEntry`
  — same signature, new behaviour: it closes the live turn, records what the
  round filled, and leaves the model's proposed next question on
  `entry.record.followup_question` for the pipeline to gate.

- [x] **Step 1: Rewrite `apply_followup_answer`**

Replace the whole function in `core/agent.py`:

```python
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
            _render_context(entry.record),
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
```

The `entry.record.followup_question is None` guard is gone on purpose: the
question now lives on the turn, and `if not entry.followups` covers the "never
asked" case exactly.

- [x] **Step 2: Show the conversation so far in the merge context**

In `_render_context`, the merge agent only ever sees the current record. With
multiple rounds it should also see what has already been asked, or round three
re-asks round one's question in new words. Add a parameter:

```python
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
```

and pass it at the call site:

```python
            _render_context(entry.record, [t.question for t in entry.followups[:-1]]),
```

- [x] **Step 3: Teach the merge prompt about the next question**

Append to `core/prompts/followup_merge.md`:

```markdown
# Asking one more

After merging, you may propose ONE more question in `next_question`. It is a
proposal only — whether it is posted is decided outside this prompt.

Propose one only when all three hold:

1. The reply answered (`answered: true`). A person who deflected once will not
   thank you for a second try.
2. An important gap is genuinely still open — look at `missing_fields`.
3. The new question is about something different from every question in
   "Already asked in this thread".

Tone is unchanged: conversational, one thing, under 25 words.

- Good: "Nice — how much lighter did that end up being?"
- Good: "Did you get to test it, or is that next meeting?"
- Bad: "Could you also provide the rationale and alternatives considered?"
- Bad: re-asking the same thing with different words.

Return null otherwise. **Null is the normal outcome** — most replies close the
conversation, and a live channel is not a form.
```

- [x] **Step 4: Run the smoke check**

Run: `uv run python -m scripts.Smoke`
Expected: the ambient and reply paths both complete. `Smoke.py` constructs a
`LoggedEntry` and calls `mark_followup_asked` — update its call to the new
two-argument signature (`mark_followup_asked(question, message_id)`) if it
breaks, and nothing else.

- [x] **Step 5: Commit**

```bash
git add core/agent.py core/prompts/followup_merge.md scripts/Smoke.py
git commit -m "feat: multi-round merge, with the next question riding the same call"
```

---

## Task 7: `core/pipeline.py` — one place for ingest policy

Today `channels/discord_bot.py::_persist_and_reply` decides whether to ask. With
triage, thread gates, budgets and rounds that is four judgement calls sitting in
the channel layer, which §4 forbids. Move all of it here.

**Files:**
- Create: `core/pipeline.py`

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces:
  ```python
  @dataclass
  class Ingested:
      entry: LoggedEntry
      question: Optional[str]   # post it, then call mark_asked

  async def ingest(*, channel, author, created_at, raw_text,
                   channel_message_id=None, source="ambient") -> Optional[Ingested]
  async def handle_reply(*, open_message_id, raw_text, at) -> Optional[Ingested]
  async def mark_asked(entry, question, message_id, at=None) -> LoggedEntry
  ```

- [x] **Step 1: Write the file**

```python
"""The ingest pipeline: everything between "some text arrived" and "here is
what to say back".

This exists so channels/ can stay ears and mouth. Every branch below is a
judgement call — is this worth a call, is this a duplicate, is a question worth
asking right now — and §4 says judgement calls do not live in a channel
adapter. A channel calls ingest(), posts `question` if it is not None, and
calls mark_asked() with the id it got back. That is the whole contract, and it
is why adding a web channel later is a zero-change operation on core/.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from . import followup, storage, triage
from .agent import apply_followup_answer, log_session, parse_design_record
from .schema import LoggedEntry

log = logging.getLogger(__name__)

# The window the open-question budget looks back over. A season-long count
# would throttle the bot to silence by February.
BUDGET_WINDOW = timedelta(hours=12)


@dataclass
class Ingested:
    """What the channel needs to do next: nothing, or post one question."""

    entry: LoggedEntry
    question: Optional[str] = None


async def ingest(
    *,
    channel: str,
    author: Optional[str],
    created_at: datetime,
    raw_text: str,
    channel_message_id: Optional[str] = None,
    source: Literal["ambient", "log"] = "ambient",
) -> Optional[Ingested]:
    """Turn text into a persisted record. None means nothing was worth doing.

    `raw_text` is expected to be a whole burst already (core/inbox.py), not a
    single message — triage and the model both read better that way.
    """
    # Reconnects redeliver; don't pay for the same message twice.
    if channel_message_id and await storage.find_by_channel_message_id(
        channel, channel_message_id
    ):
        return None

    # A deliberate /log is never triaged away. Somebody typed it into a modal
    # on purpose; second-guessing that is how input friction starts.
    if source == "ambient" and not triage.worth_parsing(raw_text):
        return None

    record = await (log_session if source == "log" else parse_design_record)(raw_text)
    entry = LoggedEntry(
        channel=channel,
        source=source,
        channel_message_id=channel_message_id,
        author=author,
        created_at=created_at,
        raw_text=raw_text,
        record=record,
    )
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))


async def handle_reply(
    *, open_message_id: str, raw_text: str, at: datetime
) -> Optional[Ingested]:
    """Route a reply to the question it answers. None means it answered none."""
    entry = await storage.find_by_open_followup(open_message_id)
    if entry is None:
        return None

    entry = await apply_followup_answer(entry, raw_text, at=at)
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))


async def mark_asked(
    entry: LoggedEntry,
    question: str,
    message_id: str,
    at: Optional[datetime] = None,
) -> LoggedEntry:
    """Record that the question was actually posted, and under which id."""
    entry = entry.mark_followup_asked(question, message_id, at=at)
    await storage.save(entry)
    return entry


async def _question_for(entry: LoggedEntry) -> Optional[str]:
    """Should the bot say anything, and what?

    Five gates, cheapest first. The default is silence — §8's rule that a
    follow-up posts publicly in a live channel is the reason every one of these
    is a veto rather than a score.
    """
    question = entry.record.followup_question
    if not question:
        return None                          # the model chose silence
    if not followup.should_ask_again(entry):
        return None                          # rounds exhausted, or the last one missed

    thread = await storage.list_thread(entry.channel, entry.record.component)
    if not followup.open_gaps(entry.record, thread):
        return None                          # the thread already answers this

    open_count = await storage.count_open_followups(
        entry.channel, since=datetime.now(timezone.utc) - BUDGET_WINDOW
    )
    if open_count >= followup.MAX_OPEN_QUESTIONS:
        log.info("question budget reached for %s, staying quiet", entry.channel)
        return None

    return question
```

- [x] **Step 2: Verify it imports and type-checks**

Run: `uv run python -c "import core.pipeline; print('ok')"`
Expected: `ok`

- [x] **Step 3: Commit**

```bash
git add core/pipeline.py
git commit -m "feat: core/pipeline owns ingest policy so channels stay dumb"
```

---

## Task 8: `channels/discord_bot.py` — ears and mouth, and nothing else

**Files:**
- Modify: `channels/discord_bot.py`

**Interfaces:**
- Consumes: `core.pipeline`, `core.inbox.Coalescer`.
- No longer imports `core.agent` or `core.schema` at all; `core.storage` only
  for `init_schema`.

- [x] **Step 1: Rewrite the file**

```python
"""Discord channel — ears and mouth only. Every judgment lives in core/."""

import logging
import os

import discord
from discord import app_commands
from dotenv import load_dotenv

from core import pipeline, storage
from core.inbox import Coalescer

load_dotenv()
log = logging.getLogger(__name__)

# Empty = listen everywhere. Set it: one API call per burst still adds up.
CHANNELS = {c.strip() for c in os.getenv("DISCORD_CHANNELS", "").split(",") if c.strip()}


async def _say(result, send) -> None:
    """Post the question core decided on, and tell core which id it got."""
    if result is None or not result.question:
        return
    msg = await send(result.question)
    await pipeline.mark_asked(result.entry, result.question, str(msg.id), at=msg.created_at)


class LogModal(discord.ui.Modal, title="Log what you worked on"):
    # ponytail: single free-text box. Add per-field inputs only if the model
    # keeps mis-parsing recaps, which the scoring loop would show first.
    text = discord.ui.TextInput(label="What happened?", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)  # not ephemeral — gotcha 7
        result = await pipeline.ingest(
            channel="discord",
            source="log",
            author=interaction.user.display_name,
            created_at=interaction.created_at,
            raw_text=str(self.text),
        )
        await interaction.followup.send(_receipt(result))
        await _say(result, interaction.channel.send)


def _receipt(result) -> str:
    if result is None:
        return "Logged nothing — that didn't look like design work."
    record = result.entry.record
    lines = [f"**{record.title}**", f"`{record.stage.value}` · `{record.subteam.value}`"]
    if record.missing_fields:
        lines.append("still missing: " + ", ".join(record.missing_fields))
    return "\n".join(lines)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True, messages=True, message_content=True))
        self.tree = app_commands.CommandTree(self)
        # One thought is often four messages. Buffer per person per channel and
        # hand core the whole burst; see core/inbox.py.
        self.bursts = Coalescer(self._flush_burst)

    async def setup_hook(self):
        await storage.init_schema()

        @self.tree.command(name="log", description="Log work the team did offline")
        async def _log(interaction: discord.Interaction):
            if CHANNELS and str(interaction.channel_id) not in CHANNELS:
                await interaction.response.send_message(
                    "This bot isn't listening in this channel.", ephemeral=True
                )
                return
            await interaction.response.send_modal(LogModal())

        await self.tree.sync()

    async def close(self):
        # The last burst of a meeting is the one most likely to be the recap.
        await self.bursts.drain()
        await super().close()

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.content.strip():
            return
        if CHANNELS and str(message.channel.id) not in CHANNELS:
            return

        try:
            await self._handle(message)
        except Exception:
            log.exception("dropping message %s", message.id)

    async def _handle(self, message: discord.Message):
        # A reply to one of our questions is an answer, not a new record, and it
        # is never buffered — it is already a deliberate, complete thought.
        if message.reference and message.reference.message_id:
            result = await pipeline.handle_reply(
                open_message_id=str(message.reference.message_id),
                raw_text=message.content,
                at=message.created_at,
            )
            if result is not None:
                await _say(result, message.reply)
                return

        await self.bursts.add(f"{message.channel.id}:{message.author.id}", message)

    async def _flush_burst(self, key: str, messages: list[discord.Message]) -> None:
        """One person's burst, parsed as one unit.

        Anchored to the FIRST message: that id is the dedup key and it is stable
        across a reconnect that re-forms the same burst. The reply goes to the
        LAST one, which is where the conversation actually is.
        """
        first, last = messages[0], messages[-1]
        result = await pipeline.ingest(
            channel="discord",
            author=first.author.display_name,
            created_at=first.created_at,
            channel_message_id=str(first.id),
            raw_text="\n".join(m.content for m in messages),
        )
        await _say(result, last.reply)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    Bot().run(os.environ["DISCORD_TOKEN"])
```

- [x] **Step 2: Verify it imports**

Run: `uv run python -c "import channels.discord_bot; print('ok')"`
Expected: `ok`

- [x] **Step 3: Check the boundary held**

Run:

```bash
grep -nE "^(from|import) " channels/discord_bot.py
```

Expected: no `core.agent`, no `core.schema`. Only `core.pipeline`,
`core.storage` (for `init_schema`) and `core.inbox`.

- [x] **Step 4: Commit**

```bash
git add channels/discord_bot.py
git commit -m "refactor: discord bot delegates every decision to core/pipeline"
```

---

## Task 9: measure the conversation, not just the message

`tests/samples.py` is single-message and cannot see coalescing or rounds. This
adds the smallest fixture that can, and the two numbers that matter: **rounds
used** and **nags**.

**Files:**
- Create: `tests/conversations.py`
- Create: `scripts/try_conversation.py`
- Modify: `notes.md`

**Interfaces:**
- Consumes: `core.agent.parse_design_record`, `core.agent.apply_followup_answer`,
  `core.followup.should_ask_again`, `core.inbox.Coalescer`.
- Produces: `tests.conversations.CONVERSATIONS: list[Conversation]`

- [x] **Step 1: Write the fixture**

Create `tests/conversations.py`:

```python
"""Multi-message, multi-round fixtures for scripts/try_conversation.py.

⚠️  THESE ARE INVENTED, exactly like tests/samples.py. Same warning applies:
replace `burst` and `replies` with real transcripts from the team channel
before believing any number here. §9's rule that real messages come first is
not softer just because the unit got bigger.

What this measures that samples.py cannot:
  - a burst that must collapse into ONE record, not four
  - a follow-up that legitimately deserves a second round
  - a deflection that must end the conversation immediately
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Conversation:
    burst: list[str]
    # Replies, in order, as answers to whatever the bot asks. The run stops
    # early if the bot goes quiet before these run out.
    replies: list[str] = field(default_factory=list)
    # How many questions the bot is allowed to ask across the whole exchange.
    max_questions: int = 2
    # Fields that must be filled by the end, given these replies.
    want_filled: frozenset[str] = frozenset()


CONVERSATIONS = [
    # A burst that is one thought. One record, at most one question.
    Conversation(
        burst=[
            "intake keeps jamming",
            "like when two blocks come in at the same time",
            "tried compliant wheels, didn't help much",
            "going dual roller, it fits the current mount",
        ],
        replies=["haven't tested it yet, next meeting"],
        max_questions=2,
        want_filled=frozenset(),
    ),
    # Two productive rounds: the classic "why?" -> "weight" -> "how much?".
    Conversation(
        burst=["swapped the slide to 2 stage"],
        replies=["it was too heavy at 3 stage", "about 400g lighter"],
        max_questions=3,
        want_filled=frozenset({"rationale"}),
    ),
    # A deflection must end it. One question in, zero after the shrug.
    Conversation(
        burst=["redid the odometry pod mount"],
        replies=["idk ask sam", "lol"],
        max_questions=1,
        want_filled=frozenset(),
    ),
    # Chitchat: never a question, and triage should not even reach the model.
    Conversation(
        burst=["who's driving tmrw", "i can bring the cart"],
        replies=[],
        max_questions=0,
        want_filled=frozenset(),
    ),
]
```

- [x] **Step 2: Write the harness**

Create `scripts/try_conversation.py`:

```python
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
```

- [x] **Step 3: Run it**

Run: `uv run python -m scripts.try_conversation`
Expected: `nags 0`. Every conversation within its question budget.

If a conversation is LOUD, the fix is `followup_merge.md`'s "Asking one more"
section (state which metric you are targeting, per §11), not `MAX_ROUNDS` —
lowering the ceiling hides the behaviour instead of fixing it.

- [x] **Step 4: Run the whole suite**

```bash
uv run python -m tests.test_core
uv run python -m scripts.try_parse
uv run python -m scripts.try_conversation
```

Expected: all offline tests green, `silence` still at its gate, `nags 0`.

- [x] **Step 5: Record the run**

Append to `notes.md`:

```markdown
| 5 | 2026-09-01 | multi-round follow-ups + thread gate + question budget | deepseek-chat | ?/15 | ?/3 | ?/15 |

### Conversation-level (scripts/try_conversation.py, run 5)

| conversations | questions asked | nags | rounds used |
|---|---|---|---|
| 4 | ? | 0 | ? |

Still invented text, in the fixture as in samples.py. Replace both with real
transcripts before treating any of this as a baseline.
```

- [x] **Step 6: Commit**

```bash
git add tests/conversations.py scripts/try_conversation.py notes.md
git commit -m "test: score the conversation, not just the message"
```

---

## Task 10: update the docs so the next reader is not lied to

**Files:**
- Modify: `CLAUDE.md`
- Modify: `spec.md`

- [x] **Step 1: Update `CLAUDE.md` §4's file structure**

Add to the `core/` block:

```
│   ├── triage.py              # is this worth a call at all?
│   ├── inbox.py               # burst coalescing
│   ├── followup.py            # merge gate + multi-round stop policy
│   ├── pipeline.py            # ingest policy — the only caller of all of core
```

and add to `tests/` and `scripts/`:

```
│   ├── conversations.py       # multi-message, multi-round fixtures
│   └── try_conversation.py    # rounds + nags
```

- [x] **Step 2: Correct the statements this plan invalidated**

In §7, replace "`FollowupPatch` — what a reply adds" with a note that it also
proposes the next question, gated in Python. In §7's hard rules, the sentence
about one question per record no longer holds — replace with:

```markdown
- Follow-ups may run up to `FOLLOWUP_MAX_ROUNDS` rounds, and stop at the first
  of: nothing patchable left, a reply that did not answer, a round that filled
  nothing, the component thread already supplying the field, or the channel's
  open-question budget. Every one of those gates is Python
  (`core/followup.py`), not prompt.
```

In §10, move `channels/discord_bot.py`'s "never run against real Discord" note
forward unchanged — it is still true — and add `core/pipeline.py`,
`core/inbox.py` and `core/triage.py` to it: the coalescer's timing and the
budget have only ever been exercised by tests.

Delete the "known defect" about the `.env` model, which Task 0 fixed.

- [x] **Step 3: Update the diagram in `spec.md`**

Add `triage`, `inbox`, `followup` and `pipeline` to the core subgraph, and
redraw the channel edges: `discord_bot --> pipeline` only, with
`pipeline --> agent`, `pipeline --> storage`, `pipeline --> triage`,
`pipeline --> followup`, `discord_bot --> inbox`. The "no storage-to-export
caller" warning node stays — `scripts/export.py` is still absent.

- [x] **Step 4: Commit**

```bash
git add CLAUDE.md spec.md
git commit -m "docs: record the conversational-capture architecture"
```

---

## Still not done after this plan

Stated so the next reader does not mistake this for finished:

- **`tests/samples.py` and `tests/conversations.py` are both invented.** They
  remain the blocker §9 says they are. Every number in `notes.md` is provisional
  until real transcripts replace them.
- **`scripts/export.py` does not exist**, so nothing reads storage into the
  notebook. Ten lines, and worth writing before the first live week so the
  coverage table can actually be looked at.
- **The bot has still never connected to Discord.** The §10 first-live-run
  checklist applies unchanged, plus two new runtime-only risks: the coalescer's
  45-second window (too short splits a thought, too long feels asleep — tune it
  from the first real meeting), and the open-question budget (2 may be too tight
  for a channel with two subteams in it).
