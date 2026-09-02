# Global Awareness Implementation Plan

> **For the engineer picking this up:** use the repository-local
> `planning-with-files` skill, tick each checkbox as you go, and work the tasks
> directly in order, one commit per task. The execution skills normally paired
> with `writing-plans` are not installed in this repo.

**Goal:** the bot currently knows one message and its component thread. After
this plan it knows three more things, each on its own surface: **the whole
season** (`/digest` — which design lines are one field from judge-ready, which
stalled, which decided something and never tested it), **its own archive**
(`/ask` — semantic search over the team's own records, plus a "we did this
before" line on `/log` receipts), and **when a meeting ended** (an opt-in
session recap it posts by itself).

**Architecture:** one new pure module, `core/digest.py`, which derives
per-thread health from entries the way `core/progress.py` derives per-person
spans — no model call, no query, no clock read. `core/pipeline.py` gains three
read functions (`digest`, `recall`, `session_recap`) shaped exactly like the
existing `status()` and `board()`. `channels/discord_bot.py` gains three
renderers, two commands and one idle timer that reuses `core/inbox.Coalescer`
unchanged. **`storage.search()` — built, tested, and called by nothing since
the day it was written — finally gets a caller.**

**Tech Stack:** Python 3.13, uv, discord.py 2.7, pydantic-ai, PostgreSQL +
pgvector. **No new dependencies.** One optional vendor already in the stack
(OpenAI embeddings, §5) becomes user-visible for the first time.

**Spec:** this plan is its own spec. It leans on `CLAUDE.md` §3 (scope), §4
(layering), §7 (schema), §8 (silence), §9 (test discipline) and §11 throughout,
and on `docs/design/progress-tracker.md` for span semantics.

---

## Global Constraints

Copied from `CLAUDE.md`. Violating one is a rejected task, not a style note.

- **No new dependencies.**
- **`core/` does not know Discord exists** (§4). Nothing from the `discord`
  package may be imported under `core/`.
- **`channels/` must not import `core/storage.py`** (§11). Everything a card
  needs arrives on the object `core/pipeline.py` hands back.
- **No judgment calls in `channels/`** (§4). Choosing an emoji is presentation.
  Deciding which threads are worth showing, or whether the bot should speak at
  all tonight, is not.
- **Not task management** (§3). Nothing here creates, assigns, closes or
  reorders anything. Every line on every new surface is derived from captured
  entries and a human can edit none of it.
- **Not an agent framework** (§5). No tool loop, no orchestration layer, no
  LangGraph. Every new code path is one query and at most one model call, and
  the three new pipeline functions make **zero** model calls between them.
- **Not RAG over the game manual** (§3). `/ask` searches the team's own records
  and nothing else — the seam CLAUDE.md explicitly rules *in*.
- **No schema change to `DesignRecord`** and **no prompt change**. §9's three
  metrics are therefore untouched and no scoring loop runs in this plan.
- The **only** new persistence is nothing. No table, no column, no migration.

### On testing

Two files, two halves, established by the previous plan:

| Half | Verified by |
|---|---|
| anything in `core/` | a unit test in `tests/test_core.py` — pure, no API, no DB, no `discord` import |
| any Discord card | a structure check in `tests/test_cards.py` — imports `discord`, builds the object by hand, asserts on the embed |
| anything about *appearance* or live timing | the checklist in `docs/running-the-bot.md`, on a real server |

Counts at the start of this plan: `test_core` 22, `test_cards` 6. Each task
below states the count it should leave behind.

---

## What "global awareness" means here, and what it does not

The request was "让 bot 对全局都有掌控性". Four readings were weighed; three are
built here and the fourth is deliberately refused.

| Reading | Verdict |
|---|---|
| **Memory** — the bot can consult everything the team ever logged | **Build.** `storage.search()` already exists and has no caller. |
| **Cross-thread state** — the bot knows the season's shape, not just this message | **Build.** `/digest`, pure arithmetic over `thread_gaps` and stages. |
| **Initiative** — the bot decides when to speak, unprompted | **Build, opt-in and off by default.** One recap per meeting, gated in Python. |
| **Autonomy** — a tool loop that plans multi-step actions | **Refuse.** §5 settled this: "this is a workflow, not a multi-step autonomous agent". Nothing in the request needs a loop — each of the three above is one query and a pure function. Adding an orchestration layer would buy nothing except a second way for the bot to be wrong in public. |

The line between the third and the fourth is worth stating plainly, because it
is the one that will be under pressure later: **the bot may decide *whether* to
speak, never *what to do*.** Every trigger in this plan is a Python gate over
derived data, auditable by reading forty lines. That is what keeps a
design-process companion from turning into an assistant nobody trusts.

---

## Design decisions

### `/digest` is arithmetic, not a summary

The obvious build is "send the season to the model and ask for a summary".
It is rejected: it costs a call per invocation, it can hallucinate a design
decision that was never made — the one failure mode §8's "Never invent" exists
to prevent — and it answers in prose that reads differently every time.

Everything the digest says is already computable: `followup.thread_gaps()`
knows what a thread lacks, `Stage` knows how far it got, `created_at` knows
when it stopped. Three buckets over those three facts answer the actual
question ("what does the season still need?") with numbers a judge could check.

So `/digest` makes **no model call at all**. If a phrased narrative is ever
wanted, it is a second surface over the same data, not a rewrite of this one.

### Three buckets, no score

A ranked list needs weights, and weights invented at a desk are a fudge that
looks like rigour. Three named buckets need none:

| Bucket | Rule | The question it answers |
|---|---|---|
| `almost` | exactly one patchable field missing | "what is 30 seconds from judge-ready?" |
| `untested` | reached decision or build, never reached `test`, no `test_evidence` | "what did we decide and never verify?" — the classic FTC notebook hole |
| `stale` | has gaps, untouched for `STALE_AFTER` | "what did we abandon without noticing?" |

**First bucket wins**, in that order, so a thread never appears twice and the
label it gets is the most actionable one. Threads with no gaps are counted and
not listed — a complete thread needs nothing from anyone.

A thread can also match **no** bucket: several holes, no decision yet, touched
this week. That is work in progress, and it is deliberately silent — nagging a
two-day-old ideation thread about its missing rationale is the follow-up
machinery's job (`core/followup.py`), one question at a time, in the thread.
The digest is for what fell through *that* net. The card's counts therefore do
not add up to `total` on purpose, and the description says so.

### `/ask` shows what it found, and does not answer

Same reasoning one rung further. `storage.search()` returns ranked records with
titles, summaries, dates and authors — that *is* the answer to "did we ever try
compliant wheels?". Rendering the hits costs one embedding call; synthesising
them into a paragraph costs a model call and adds a way to be confidently
wrong about the team's own history.

The **score is shown, not filtered**. A floor (`> 0.4`) is exactly the kind of
number this codebase refuses to invent — `TASK_IDLE_MINUTES` is already one
unmeasured constant too many (§10). Show the number, let the reader judge, and
set a floor once real queries exist.

### Retrieval reaches `/log` receipts, and stays out of the prompt

The tempting build is feeding retrieved history into `parse_design_record` so
the classifier "knows the season". Not in this plan, for two reasons:

1. It is a **prompt change**, and §9's discipline is one change per run,
   measured. `tests/samples.py` is still invented (§10), so the scoring loop
   cannot currently tell whether retrieval helped or hurt. Making an unmeasured
   prompt change to the one path every ambient message goes through is how the
   three metrics quietly rot.
2. Retrieved text in the context of a "never invent" prompt is a
   confabulation risk aimed at the exact field it would most damage — the model
   copying a *past* rationale into a *present* record is indistinguishable, in
   the notebook, from the team having said it.

What ships instead is the same information pointed at the human: a **Related
earlier** field on the `/log` receipt. One search call, on the deliberate,
low-volume path only — never on ambient bursts, which would put a vendor call
in front of every message in the channel.

### The session recap is opt-in, and off by default

This is the only thing in the plan that makes the bot speak without being
spoken to, and §2's success metric is retention: a bot that posts a wall of
text every night gets muted in week one, and a muted bot captures nothing.

So: `SESSION_RECAP=off` by default, three Python gates before a word is
posted (a real session, something actually missing, and one recap per meeting),
and the recap is short enough to skip. Turn it on in the second live week,
after the capture path itself is trusted — `docs/running-the-bot.md` says so.

Silence when everything is complete is not a bug. "All good tonight" is noise;
`/digest` is there for whoever wants to check.

### The idle timer is a `Coalescer`

"The meeting ended" is "this channel has been quiet for 90 minutes", which is
precisely what `core/inbox.Coalescer` already does — buffer per key, reset the
timer on every arrival, fire after N seconds of silence, and swallow flush
exceptions so one bad recap cannot take the bot down. A hand-rolled
`asyncio.sleep` loop would re-implement all four and get one of them wrong.

Two instances, two windows, one class: 45 seconds per author for bursts, 90
minutes per channel for sessions. The session one is **not** drained on
shutdown — a restart is not the end of a meeting, and claiming it is would post
a recap in the middle of a build.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `core/schema.py` | +`thread_key()`, +`UNFILED` | owns thread vocabulary — the fold and the bin name, defined once |
| `core/progress.py`, `core/pipeline.py`, `exporters/notebook.py`, `exporters/kanban.py`, `channels/discord_bot.py` | import them | one definition instead of five |
| `core/digest.py` | **create** (~90 lines) | per-thread health, pure. The `progress.py` of the other axis |
| `core/pipeline.py` | +`digest()`, +`recall()`, +`session_recap()`, +`Recall`, +`Recap`, +`RECAP_ENABLED` | the policy for all three: how far back, what counts, when to stay quiet |
| `core/storage.py` | +`embeddings_enabled()` | says whether search can work at all, without a vendor call |
| `channels/discord_bot.py` | +3 renderers, +2 commands, +1 timer | renders what pipeline hands back |
| `tests/test_core.py` | +6 checks | the core half |
| `tests/test_cards.py` | +4 checks | the card half |
| `.env.example`, `CLAUDE.md`, `docs/running-the-bot.md` | new knobs, status, live checklist | |

`exporters/` gains nothing and loses a duplicate constant. `scripts/` is not
touched. No file is deleted.

---

## Task 1: thread vocabulary moves to `core/schema.py`

The component fold — `(component or "").strip().lower()` — exists in
`core/progress.py`, `core/pipeline.py`, `exporters/notebook.py`,
`core/storage.py`'s `list_thread()` and, as a generated column, in the same
file's SQL. `core/digest.py` would be the sixth. `"Unfiled"` is spelled out in `exporters/notebook.py` and again as a
literal in two cards.

Same argument as `STAGE_ORDER` last week: the third consumer earns the move.

**Files:**
- Modify: `core/schema.py`, `core/progress.py`, `core/pipeline.py`,
  `exporters/notebook.py`, `exporters/kanban.py`, `channels/discord_bot.py`,
  `tests/test_core.py`

**Interfaces:**
- Produces: `core.schema.thread_key(component: Optional[str]) -> str` and
  `core.schema.UNFILED: str`. Every later task uses both.

- [x] **Step 1: Write the failing test**

  In `tests/test_core.py`, beside the other pure checks:

  ```python
  def test_thread_key_folds_the_way_storage_does():
      from core.schema import UNFILED, thread_key

      # Character for character the fold core/storage.py's component_key
      # generated column applies, or a span and a design thread can disagree
      # about whether "Intake" and "intake" are the same work.
      assert thread_key("Intake") == thread_key("  intake ") == "intake"
      # Empty stays empty. The "Unfiled" bin is the notebook's display choice,
      # not the key's — collapsing them here would silently merge every
      # component-less entry into one thread everywhere else.
      assert thread_key(None) == thread_key("   ") == ""
      assert UNFILED == "Unfiled"
  ```

- [x] **Step 2: Run it and watch it fail**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: `ImportError: cannot import name 'thread_key' from 'core.schema'`.

- [x] **Step 3: Add both to `core/schema.py`**

  Below `STAGE_ORDER`:

  ```python
  # The bin for entries with no component. A display name, not a key — see
  # thread_key below.
  UNFILED = "Unfiled"


  def thread_key(component: Optional[str]) -> str:
      """Fold a component name into a design-thread key.

      Character for character the fold core/storage.py's `component_key`
      generated column applies in SQL. The two must never drift: a span, a
      design thread and a database lookup all have to agree that "Intake" and
      "intake" are the same work. That is why this lives in the schema rather
      than in whichever module happened to need it first.

      Returns "" for a missing component, deliberately. Whether that empty key
      is displayed as "Unfiled" is the renderer's call, and a renderer that
      folded it into the key would merge every component-less entry in the
      season into a single thread.
      """
      return (component or "").strip().lower()
  ```

  `Optional` is already imported in `schema.py`.

- [x] **Step 4: Repoint every caller**

  1. `core/progress.py` — `_key()` becomes
     `return entry.author, thread_key(entry.record.component)`, importing
     `thread_key` from `.schema`. Delete the fold comment there; it now lives
     on the function.
  2. `core/pipeline.py` — in `status()`, `key = thread_key(span.component)` and
     the list comprehension becomes
     `if thread_key(e.record.component) == key`.
  3. `exporters/notebook.py` — delete `UNFILED = "Unfiled"` and the body of
     `_thread_key`; import both from `core.schema` and make it
     `return thread_key(entry.record.component) or UNFILED.lower()`.
     **Keep the `or UNFILED.lower()`** — the notebook's threads dict is keyed
     by display bin, and dropping it would move every unfiled entry into a
     thread named `""`.
  4. `exporters/kanban.py` — `from exporters.notebook import UNFILED` becomes
     `from core.schema import UNFILED`.
  5. `channels/discord_bot.py` — the two `or 'Unfiled'` literals in
     `_status_card` and `_board_card` become `or UNFILED`, imported from
     `core.schema` on the existing import line.
  6. `core/storage.py` — `list_thread()` has its own
     `key = (component or "").strip().lower()`; make it
     `key = thread_key(component)`. The SQL generated column
     (`lower(btrim(coalesce(...)))`) stays as it is — it is the same fold in
     the database's own words, and the docstring on `thread_key` now names it
     so the two are checked against each other by a reader, not by luck.
  7. `tests/test_core.py` — `from exporters.notebook import UNFILED,
     render_notebook` becomes `from exporters.notebook import render_notebook`,
     and `UNFILED` joins the `core.schema` import.

- [x] **Step 5: Run everything**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: **23** `ok` and **6** `ok`. `test_notebook`, `test_spans`,
  `test_status` and `test_board` all exercise the fold, so a wrong one fails
  here rather than in a rendered notebook.

  ```bash
  git grep -n '"Unfiled"' -- core channels exporters
  ```

  Expected: one hit, the definition in `core/schema.py`.

- [x] **Step 6: Commit**

  ```bash
  git add core/schema.py core/progress.py core/pipeline.py core/storage.py \
    exporters/notebook.py exporters/kanban.py channels/discord_bot.py \
    tests/test_core.py
  git commit -m "refactor: the thread fold and the Unfiled bin belong to the schema"
  ```

---

## Task 2: `core/digest.py`, per-thread health

Pure derivation, no I/O. The `core/progress.py` of the component axis.

**Files:**
- Create: `core/digest.py`
- Test: `tests/test_core.py`

**Interfaces:**
- Consumes: `core.followup.thread_gaps`, `core.schema.STAGE_ORDER`,
  `thread_key`, `UNFILED`, `LoggedEntry`, `Stage`.
- Produces:
  - `Thread(component: str, entries: int, authors: tuple[str, ...],
    stages: tuple[Stage, ...], gaps: frozenset[str], last_at: datetime)`
  - `threads(entries: list[LoggedEntry]) -> list[Thread]`
  - `Digest(almost: list[Thread], untested: list[Thread], stale: list[Thread],
    total: int, complete: int)`
  - `summarise(entries: list[LoggedEntry], *, now: datetime) -> Digest`
  - `STALE_AFTER: timedelta`

- [x] **Step 1: Write the failing test**

  In `tests/test_core.py`:

  ```python
  def test_digest_buckets():
      from core.digest import STALE_AFTER, summarise, threads

      now = datetime(2025, 10, 12, 3, 0, tzinfo=timezone.utc)

      def D(component, stage, ago_days, **fields):
          return LoggedEntry(
              raw_text="x", author="Eli",
              created_at=now - timedelta(days=ago_days),
              record=R(stage=stage, component=component, **fields),
          )

      entries = [
          # one field short: problem, alternatives and rationale are in, no test
          D("intake", Stage.PROBLEM, 3, problem_statement="jams on two blocks"),
          D("Intake", Stage.DECISION, 2, alternatives_considered=["compliant"],
            rationale="fits the current mount"),
          # decided and built, nothing else — untested, and more than one gap
          D("slide", Stage.BUILD, 4),
          # A test-stage note with no recorded result is incomplete, but the
          # digest must not claim that testing never happened.
          D("arm", Stage.TEST, 4),
          # abandoned: gaps, and nothing since well before the stale window
          D("claw", Stage.IDEATION, STALE_AFTER.days + 5),
          # complete: every patchable field filled somewhere in the thread
          D("odometry", Stage.TEST, 1, problem_statement="drifts",
            alternatives_considered=["two pods"], rationale="cheaper",
            test_evidence="0.4in over 10ft"),
      ]

      # "Intake" and "intake" are one thread, named by the first entry's own
      # capitalisation. Six entries, five threads.
      names = [t.component for t in threads(entries)]
      assert "intake" in names and "Intake" not in names and len(names) == 5

      got = summarise(entries, now=now)
      assert [t.component for t in got.almost] == ["intake"]
      assert [t.component for t in got.untested] == ["slide"]
      assert [t.component for t in got.stale] == ["claw"]
      assert got.total == 5 and got.complete == 1
      # A complete thread is counted and never listed.
      listed = {t.component for t in [*got.almost, *got.untested, *got.stale]}
      assert "odometry" not in listed
      assert "arm" not in listed, "a test-stage thread was tested"
      # First bucket wins: no thread is reported twice.
      assert len(listed) == 3
      # Stages come back in design-cycle order, not arrival order.
      assert dict((t.component, t.stages) for t in threads(entries))["intake"] \
          == (Stage.PROBLEM, Stage.DECISION)
  ```

- [x] **Step 2: Run it and watch it fail**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: `ModuleNotFoundError: No module named 'core.digest'`.

- [x] **Step 3: Write `core/digest.py`**

  ```python
  """Design-thread health: what the season still needs, thread by thread.

  /board answers "who is on what right now" over spans (core/progress.py).
  This answers "what is still missing" over component threads — the other axis,
  and the one a team only asks the night before the deadline, which is exactly
  too late. Capturing the process is worth nothing if nobody can see the holes
  while there is still time to fill them.

  Pure and derived, like core/progress.py: no model call, no query, no clock
  read (`now` is passed in). Everything here is arithmetic over entries that
  already exist, which is what makes the whole module testable with no database
  and no API key.

  Still not task management (CLAUDE.md §3): a thread appears because people
  talked about a component and moves between buckets because they did or did
  not keep talking. Nothing here can be created, assigned or closed by hand.
  """

  from __future__ import annotations

  import os
  from dataclasses import dataclass
  from datetime import datetime, timedelta

  from dotenv import load_dotenv

  from .followup import thread_gaps
  from .schema import STAGE_ORDER, UNFILED, LoggedEntry, Stage, thread_key

  load_dotenv()

  # How long a thread with holes goes untouched before the digest calls it
  # abandoned. UNMEASURED, exactly like TASK_IDLE_MINUTES (§10): a build season
  # has quiet weeks — competition, exams, parts on order — and ten days is a
  # guess until real channel history says otherwise. Too small and the digest
  # cries wolf every holiday; too large and nothing is ever reported abandoned.
  STALE_AFTER = timedelta(days=int(os.getenv("DIGEST_STALE_DAYS", "10")))


  @dataclass(frozen=True)
  class Thread:
      """One component's design line, rolled up.

      `gaps` is followup.thread_gaps() — the WHOLE thread's holes, never a sum
      of per-entry `missing_fields`. That distinction is §10's first finding and
      the reason this module reuses that function instead of counting for
      itself: a thread whose problem, alternatives and rationale arrived in
      three separate messages is complete, and reporting it as broken sends the
      team chasing nothing.
      """

      component: str
      entries: int
      authors: tuple[str, ...]
      stages: tuple[Stage, ...]
      gaps: frozenset[str]
      last_at: datetime


  def threads(entries: list[LoggedEntry]) -> list[Thread]:
      """Roll entries up per component, oldest thread first.

      The display name is the first entry's own capitalisation, so "Intake" and
      "intake" become one thread without normalising the team's wording away —
      the same rule exporters/notebook.py applies, for the same §8 reason.
      """
      buckets: dict[str, list[LoggedEntry]] = {}
      names: dict[str, str] = {}
      for entry in sorted(entries, key=lambda e: e.created_at):
          key = thread_key(entry.record.component)
          buckets.setdefault(key, []).append(entry)
          names.setdefault(key, (entry.record.component or UNFILED).strip() or UNFILED)

      out = []
      for key, bucket in buckets.items():
          seen = {e.record.stage for e in bucket}
          out.append(
              Thread(
                  component=names[key],
                  entries=len(bucket),
                  authors=tuple(sorted({e.author for e in bucket if e.author})),
                  stages=tuple(s for s in STAGE_ORDER if s in seen),
                  gaps=frozenset(thread_gaps(bucket)),
                  last_at=max(e.created_at for e in bucket),
              )
          )
      return out


  @dataclass(frozen=True)
  class Digest:
      """The season in three buckets and two counts.

      No score and no ranking: weights invented at a desk are a fudge that looks
      like rigour. Each bucket answers one concrete question, and a thread lands
      in the FIRST one it matches — most actionable first — so nothing is
      reported twice and the label a thread gets is the one worth acting on.
      """

      almost: list[Thread]     # one field short of judge-ready
      untested: list[Thread]   # decided or built, never verified
      stale: list[Thread]      # has holes, nobody has touched it in STALE_AFTER
      total: int               # every thread seen, complete ones included
      complete: int            # threads with no gaps at all


  def summarise(entries: list[LoggedEntry], *, now: datetime) -> Digest:
      """Bucket every thread. Complete threads are counted, never listed."""
      almost: list[Thread] = []
      untested: list[Thread] = []
      stale: list[Thread] = []
      seen = threads(entries)

      for t in seen:
          if not t.gaps:
              continue
          if len(t.gaps) == 1:
              almost.append(t)
          elif Stage.TEST not in t.stages and "test_evidence" in t.gaps and (
              {Stage.DECISION, Stage.BUILD} & set(t.stages)
          ):
              untested.append(t)
          elif now - t.last_at >= STALE_AFTER:
              stale.append(t)

      # Recent first where the team still remembers the work; oldest first for
      # stale, where the point is which one has been rotting longest.
      almost.sort(key=lambda t: t.last_at, reverse=True)
      untested.sort(key=lambda t: t.last_at, reverse=True)
      stale.sort(key=lambda t: t.last_at)

      return Digest(
          almost=almost,
          untested=untested,
          stale=stale,
          total=len(seen),
          complete=sum(1 for t in seen if not t.gaps),
      )
  ```

- [x] **Step 4: Run it and watch it pass**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: **24** `ok`.

- [x] **Step 5: Commit**

  ```bash
  git add core/digest.py tests/test_core.py
  git commit -m "feat: core/digest.py, the season's holes as arithmetic"
  ```

---

## Task 3: `pipeline.digest()` and `/digest`

**Files:**
- Modify: `core/pipeline.py`, `channels/discord_bot.py`
- Test: `tests/test_core.py`, `tests/test_cards.py`

**Interfaces:**
- Consumes: Task 2's `summarise`, `Digest`, `Thread`.
- Produces: `pipeline.digest(*, channel: str) -> Digest` and
  `discord_bot._digest_card(digest) -> discord.Embed`.

- [x] **Step 1: Write the failing core test**

  In `tests/test_core.py`:

  ```python
  def test_digest_reads_the_whole_season():
      from unittest.mock import AsyncMock

      from core.pipeline import digest

      now = datetime.now(timezone.utc)
      rows = [
          LoggedEntry(raw_text="x", author="Eli", created_at=now - timedelta(days=200),
                      record=R(component="intake", stage=Stage.BUILD)),
      ]
      list_entries = AsyncMock(return_value=rows)
      with patch.object(storage, "list_entries", new=list_entries):
          got = asyncio.run(digest(channel="discord"))

      _, kwargs = list_entries.await_args
      # No `since`: the digest is the one surface that deliberately looks at
      # everything. A window here would answer "what does the season need"
      # with only this month.
      assert kwargs == {"channel": "discord"}
      assert got.total == 1
      assert [t.component for t in got.almost + got.untested + got.stale]
  ```

- [x] **Step 2: Run it and watch it fail**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core`
  Expected: `ImportError: cannot import name 'digest' from 'core.pipeline'`.

- [x] **Step 3: Implement `pipeline.digest()`**

  In `core/pipeline.py`, add to the `.schema` import line what it needs, add
  `from .digest import Digest, summarise` beside the other core imports, and
  below `board()`:

  ```python
  async def digest(*, channel: str) -> Digest:
      """What the season still needs. One query, no model call.

      Deliberately unwindowed, unlike status() and board(). Those answer "right
      now" and a season-long window would answer them with something from
      October; this one asks what is still missing, and a thread abandoned in
      October is the single most useful thing it can report.
      """
      entries = await storage.list_entries(channel=channel)
      return summarise(entries, now=datetime.now(timezone.utc))
  ```

- [x] **Step 4: Run it and watch it pass**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core` → **25** `ok`.

- [x] **Step 5: Write the failing card check**

  In `tests/test_cards.py`:

  ```python
  def test_digest_card_lists_buckets_and_counts_the_rest():
      from core.digest import Digest, Thread
      from channels.discord_bot import _digest_card

      def T(component, gaps, ago_days=1):
          return Thread(
              component=component, entries=2, authors=("Eli",),
              stages=(Stage.BUILD,), gaps=frozenset(gaps),
              last_at=NOW - timedelta(days=ago_days),
          )

      empty = _digest_card(Digest([], [], [], total=3, complete=3))
      assert "Nothing missing" in empty.title
      assert "3" in empty.description, "a complete season still reports its size"

      card = _digest_card(Digest(
          almost=[T("intake", ["test_evidence"])],
          untested=[T("slide", ["rationale", "test_evidence"])],
          stale=[],
          total=6, complete=2,
      ))
      names = [f.name for f in card.fields]
      assert "One field from done" in names[0]
      assert "Decided, never tested" in names[1]
      # An empty bucket is absent, exactly like an empty column on /board.
      assert len(names) == 2
      assert "intake" in card.fields[0].value
      assert "test_evidence" not in card.fields[0].value, "field names are jargon"
      assert "Results" in card.fields[0].value, "say it the way a person would"
      assert all(len(f.value) <= 1024 for f in card.fields)
  ```

- [x] **Step 6: Implement the card and the command**

  In `channels/discord_bot.py`, beside `_board_card`. Note it reuses the
  existing `COVERAGE` table for field labels — the notebook, the `/log` receipt
  and this card must never call the same hole by three different names:

  ```python
  # Threads listed per bucket before the rest become a "+N more" line. Same
  # 1024-character ceiling as the board, same blunt slice — see _board_card.
  DIGEST_MAX_THREADS = 8

  BUCKETS = (
      ("almost", "One field from done"),
      ("untested", "Decided, never tested"),
      ("stale", "Nobody has touched these"),
  )


  def _needs(gaps) -> str:
      """Gaps in the team's words, not the schema's field names."""
      return ", ".join(label for name, label in COVERAGE if name in gaps) or "—"


  def _digest_card(digest) -> discord.Embed:
      """The season's holes. No model call — every line is arithmetic.

      Buckets, not a ranked list: see the plan. An empty bucket is dropped the
      way /board drops an empty stage, because an embed cannot afford three
      placeholder fields.
      """
      total = (
          f"{digest.total} design thread{'' if digest.total == 1 else 's'} · "
          f"{digest.complete} complete"
      )
      if not (digest.almost or digest.untested or digest.stale):
          return discord.Embed(
              title="Nothing missing",
              description=total,
              colour=discord.Colour.green(),
          )

      embed = discord.Embed(
          title="What the season still needs",
          description=total,
          colour=discord.Colour.orange(),
      )
      for attr, label in BUCKETS:
          rows = getattr(digest, attr)
          if not rows:
              continue
          lines = [
              f"**{t.component}** — needs {_needs(t.gaps)}"
              for t in rows[:DIGEST_MAX_THREADS]
          ]
          if len(rows) > DIGEST_MAX_THREADS:
              lines.append(f"+{len(rows) - DIGEST_MAX_THREADS} more")
          embed.add_field(
              name=f"{label} ({len(rows)})",
              value="\n".join(lines)[:1024],
              inline=False,
          )
      return embed
  ```

  And the command, beside `/board`:

  ```python
  @self.tree.command(name="digest", description="What the season still needs")
  async def _digest(interaction: discord.Interaction):
      await interaction.response.defer(thinking=True, ephemeral=True)
      result = await pipeline.digest(channel="discord")
      await interaction.followup.send(embed=_digest_card(result), ephemeral=True)
  ```

  Ephemeral for the same reason `/status` and `/board` are: it starts no
  round trip, and posting the season's holes to the whole channel because one
  person asked is the noise §8 warns about.

- [x] **Step 7: Run both suites**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: **25** `ok` and **7** `ok`.

- [x] **Step 8: Commit**

  ```bash
  git add core/pipeline.py channels/discord_bot.py tests/test_core.py tests/test_cards.py
  git commit -m "feat: /digest, the season's holes where the team can see them"
  ```

---

## Task 4: `pipeline.recall()` and `/ask` — the archive gets a caller

`core/storage.py:381` `search()` has been written, documented and tested since
the storage layer landed, and **nothing has ever called it**. This task is the
caller.

**Files:**
- Modify: `core/storage.py` (+`embeddings_enabled`), `core/pipeline.py`,
  `channels/discord_bot.py`
- Test: `tests/test_core.py`, `tests/test_cards.py`

**Interfaces:**
- Produces: `storage.embeddings_enabled() -> bool`,
  `pipeline.Recall(query: str, hits: list[tuple[LoggedEntry, float]],
  enabled: bool)`, `pipeline.recall(*, query: str, limit: int = 5) -> Recall`,
  `discord_bot._recall_card(recall) -> discord.Embed`.
- Discord guard: query titles and field names are capped at 256 characters;
  each of the at most five result fields is capped at 800 characters. That
  keeps the complete embed below Discord's 6000-character ceiling without a
  general-purpose budget allocator.

- [x] **Step 1: Write the failing core test**

  In `tests/test_core.py`:

  ```python
  def test_recall_reports_disabled_embeddings_as_a_state():
      from unittest.mock import AsyncMock

      from core.pipeline import recall

      hit = LoggedEntry(raw_text="x", author="Eli",
                        record=R(component="intake", title="dual roller"))

      # Embeddings configured and a match found.
      with patch.object(storage, "embeddings_enabled", return_value=True), \
           patch.object(storage, "search", new=AsyncMock(return_value=[(hit, 0.82)])):
          got = asyncio.run(recall(query="compliant wheels"))
      assert got.enabled and got.query == "compliant wheels"
      assert got.hits[0][1] == 0.82

      # No embedding key: search() returns [] and so would a genuine miss. The
      # card has to tell "we never tried that" from "search is switched off",
      # so the state travels on the result rather than being inferred from an
      # empty list.
      with patch.object(storage, "embeddings_enabled", return_value=False), \
           patch.object(storage, "search", new=AsyncMock(return_value=[])):
          got = asyncio.run(recall(query="compliant wheels"))
      assert got.hits == [] and not got.enabled
  ```

- [x] **Step 2: Run it and watch it fail**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core`
  Expected: `ImportError: cannot import name 'recall' from 'core.pipeline'`.

- [x] **Step 3: Implement**

  In `core/storage.py`, beside `embed()`:

  ```python
  def embeddings_enabled() -> bool:
      """Whether semantic search can work at all, without spending a call.

      search() returns [] both when nothing matches and when no embedding
      vendor is configured (§6 gotcha 6). Those are different answers to a
      person's question, so a caller that shows the result to a human needs to
      tell them apart.
      """
      return bool(os.getenv("EMBEDDING_API_KEY"))
  ```

  In `core/pipeline.py`:

  ```python
  @dataclass
  class Recall:
      """Search over the team's own records. Never the game manual (§3)."""

      query: str
      hits: list[tuple[LoggedEntry, float]]
      enabled: bool


  async def recall(*, query: str, limit: int = 5) -> Recall:
      """Answer "did we ever try this?" out of the team's own notebook.

      No model call: storage.search() already returns ranked records with
      titles, summaries, dates and authors, and that IS the answer. Asking the
      model to phrase it adds a way to be confidently wrong about the team's
      own history.

      The score rides along unfiltered. A relevance floor is exactly the kind
      of number this codebase refuses to invent (§10 already carries one
      unmeasured constant); show it, and set a floor once real queries exist.
      """
      return Recall(
          query=query,
          hits=await storage.search(query, limit=limit),
          enabled=storage.embeddings_enabled(),
      )
  ```

- [x] **Step 4: Run it and watch it pass**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core` → **26** `ok`.

- [x] **Step 5: Write the failing card check**

  In `tests/test_cards.py`:

  ```python
  def test_recall_card_separates_off_from_empty():
      from core.pipeline import Recall
      from channels.discord_bot import _recall_card
      from core.schema import DesignRecord, Stage as St, Subteam
      from core.schema import LoggedEntry as LE

      off = _recall_card(Recall("compliant wheels", [], enabled=False))
      assert "not configured" in off.description.lower()

      miss = _recall_card(Recall("compliant wheels", [], enabled=True))
      assert "nothing" in miss.description.lower()
      assert "not configured" not in miss.description.lower()

      hit = LE(
          raw_text="x", author="Eli", created_at=NOW,
          record=DesignRecord(stage=St.DECISION, subteam=Subteam.MECHANICAL,
                              title="dual roller intake", summary="s",
                              component="intake", confidence=0.5),
      )
      found = _recall_card(Recall("compliant wheels", [(hit, 0.82)], enabled=True))
      assert "dual roller intake" in found.fields[0].value
      assert "0.82" in found.fields[0].name, "the score is shown, not filtered"
      assert all(len(f.value) <= 1024 for f in found.fields)

      long_record = hit.record.model_copy(update={
          "component": "component " * 100,
          "title": "title " * 100,
          "summary": "summary " * 1000,
      })
      long_hit = hit.model_copy(update={"record": long_record})
      bounded = _recall_card(Recall("query " * 100, [(long_hit, 0.82)] * 5,
                                    enabled=True))
      assert len(bounded.title) <= 256
      assert all(len(f.name) <= 256 and len(f.value) <= 800
                 for f in bounded.fields)
      assert len(bounded) <= 6000
  ```

- [x] **Step 6: Implement the card and the command**

  ```python
  RECALL_FIELD_CHARS = 800


  def _recall_card(recall) -> discord.Embed:
      """Search hits, as found. No synthesis — see the plan."""
      if not recall.hits:
          why = (
              "Nothing in the notebook matches that yet."
              if recall.enabled
              else "Search is not configured — set EMBEDDING_API_KEY to switch "
                   "it on. Everything else keeps working without it."
          )
          return discord.Embed(
              title=recall.query[:256], description=why,
              colour=discord.Colour.greyple(),
          )

      embed = discord.Embed(
          title=recall.query[:256],
          description=f"{len(recall.hits)} from the team's own records",
          colour=discord.Colour.blurple(),
      )
      for entry, score in recall.hits:
          when = f"<t:{int(entry.created_at.timestamp())}:D>"
          body = " · ".join(
              p for p in (entry.record.summary, entry.author) if p
          )
          embed.add_field(
              name=f"{entry.record.component or UNFILED} · {score:.2f}"[:256],
              value=f"**{entry.record.title}**\n{when} · {body}"[:RECALL_FIELD_CHARS],
              inline=False,
          )
      return embed
  ```

  ```python
  @self.tree.command(name="ask", description="Did we ever try this before?")
  @app_commands.describe(query="What to look for in the team's own records")
  async def _ask(interaction: discord.Interaction, query: str):
      await interaction.response.defer(thinking=True, ephemeral=True)
      await interaction.followup.send(
          embed=_recall_card(await pipeline.recall(query=query)), ephemeral=True
      )
  ```

- [x] **Step 7: Run both suites**

  Expected: **26** `ok` and **8** `ok`.

- [x] **Step 8: Commit**

  ```bash
  git add core/storage.py core/pipeline.py channels/discord_bot.py tests/test_core.py tests/test_cards.py
  git commit -m "feat: /ask, the archive the bot has never once consulted"
  ```

---

## Task 5: "Related earlier" on the `/log` receipt

Retrieval the bot does without being asked — pointed at the human, not at the
prompt. See Design decisions for why this does not touch `parse_design_record`.

**Files:**
- Modify: `core/pipeline.py` (`Ingested`, `ingest`), `channels/discord_bot.py`
  (`_card`)
- Test: `tests/test_core.py`, `tests/test_cards.py`

Not in `_respond()`: that helper is shared with `handle_reply()`, and a reply
never renders the receipt card — so the search would be paid on every
follow-up round to a `/log` entry and thrown away. The call goes in `ingest()`,
on the one path that produces a fresh entry, and the unit under test is
`_prior_work()` itself.

- [x] **Step 1: Write the failing test**

  In `tests/test_core.py`:

  ```python
  def test_prior_work_is_log_only_and_skips_the_current_thread():
      from unittest.mock import AsyncMock

      from core.pipeline import _prior_work

      old = LoggedEntry(raw_text="x", author="Kim",
                        record=R(component="wheels", title="tried compliant"))
      same_thread = LoggedEntry(raw_text="x", author="Eli",
                                record=R(component="Intake", title="earlier intake note"))
      entry = LoggedEntry(raw_text="x", author="Eli", source="log",
                          record=R(component="intake", title="dual roller"))

      search = AsyncMock(return_value=[(entry, 1.0), (same_thread, 0.9), (old, 0.8)])
      with patch.object(storage, "search", new=search):
          got = asyncio.run(_prior_work(entry))

      # Itself is excluded (it was just saved and embedded, so it is the top
      # hit), and so is its own thread — the receipt's coverage line already
      # speaks for that. "Intake" vs "intake" is the same thread.
      assert [e.entry_id for e in got] == [old.entry_id]

      # Ambient bursts never pay for a search: one vendor call per message in a
      # live channel is exactly the friction §2 says kills the product.
      search.reset_mock()
      with patch.object(storage, "search", new=search):
          got = asyncio.run(_prior_work(entry.model_copy(update={"source": "ambient"})))
      assert got == [] and not search.await_count
  ```

- [x] **Step 2: Run it and watch it fail**

  Expected: `ImportError: cannot import name '_prior_work' from 'core.pipeline'`.

- [x] **Step 3: Implement**

  In `core/pipeline.py`, add the field to `Ingested`:

  ```python
      # Earlier records that look like this one, for the /log receipt only.
      # Filled by ingest(), never by _respond(): a reply round never renders
      # the receipt, so it must never pay for the search.
      related: list[LoggedEntry] = field(default_factory=list)
  ```

  (`field` joins the existing `from dataclasses import dataclass` import.)

  At the end of `ingest()`, the last three lines become:

  ```python
      await storage.save(entry)
      result = await _respond(entry)
      result.related = await _prior_work(entry)
      return result
  ```

  The two peer-merge early returns above it are untouched: they are ambient
  paths, and `_prior_work` would return `[]` for them anyway.

  Then, below `_respond`:

  ```python
  async def _prior_work(entry: LoggedEntry, limit: int = 2) -> list[LoggedEntry]:
      """Earlier work that resembles this one, for a deliberate write-up.

      Only on the /log path. Ambient bursts are the high-volume path and an
      embedding call per burst puts a second vendor in front of every message
      in the channel — §2's input friction, paid in latency.

      The current thread is excluded: it is not prior work, the receipt's
      coverage line already reports it, and repeating it there would waste the
      two lines this field gets.
      """
      if entry.source != "log" or not entry.record.title:
          return []

      key = thread_key(entry.record.component)
      # ponytail: over-fetch three so self/same-thread hits do not consume the
      # two display slots. Add storage-side exclusions only if dense histories
      # make relevant cross-thread results disappear.
      hits = await storage.search(entry.record.title, limit=limit + 3)
      return [
          e for e, _ in hits
          if e.entry_id != entry.entry_id and thread_key(e.record.component) != key
      ][:limit]
  ```

  `thread_key` joins the `.schema` import line.

- [x] **Step 4: Run it and watch it pass**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core` → **27** `ok`.

- [x] **Step 5: Render it on the receipt**

  First add the card regression check in `tests/test_cards.py`:

  ```python
  def test_log_card_lists_related_work():
      from channels.discord_bot import _card
      from core.pipeline import Ingested
      from core.schema import DesignRecord, LoggedEntry, Subteam

      def entry(component, title):
          return LoggedEntry(
              raw_text="x", author="Eli", created_at=NOW,
              record=DesignRecord(
                  stage=Stage.BUILD, subteam=Subteam.MECHANICAL,
                  title=title, summary="s", component=component,
                  confidence=0.5,
              ),
          )

      current = entry("intake", "dual roller intake")
      related = entry("wheels", "tried compliant wheels")
      card = _card(Ingested(current, related=[related]))
      field = next(f for f in card.fields if f.name == "Related earlier")
      assert "wheels" in field.value and "tried compliant wheels" in field.value
      assert len(field.value) <= 1024
      assert "Related earlier" not in {
          f.name for f in _card(Ingested(current)).fields
      }
  ```

  In `channels/discord_bot.py`'s `_card`, before the coverage field:

  ```python
      if result.related:
          embed.add_field(
              name="Related earlier",
              value="\n".join(
                  f"**{e.record.component or UNFILED}** — {e.record.title}"
                  for e in result.related
              )[:1024],
              inline=False,
          )
  ```

- [x] **Step 6: Run both suites and commit**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  git add core/pipeline.py channels/discord_bot.py tests/test_core.py tests/test_cards.py
  git commit -m "feat: a /log receipt remembers what the team already tried"
  ```

  Expected: **27** `ok` and **9** `ok`.

---

## Task 6: the session recap — the bot speaks first, once, opt-in

**Files:**
- Modify: `core/pipeline.py`, `channels/discord_bot.py`, `.env.example`
- Test: `tests/test_core.py`, `tests/test_cards.py`

**Interfaces:**
- Produces: `pipeline.RECAP_ENABLED: bool`,
  `pipeline.Recap(entries: int, threads: list[Thread])`,
  `pipeline.session_recap(*, channel: str, since: datetime) -> Optional[Recap]`,
  `discord_bot._recap_card(recap) -> discord.Embed`.

- [x] **Step 1: Write the failing core test**

  In `tests/test_core.py`:

  ```python
  def test_session_recap_stays_quiet_unless_it_has_something():
      from unittest.mock import AsyncMock

      from core import pipeline as P

      now = datetime.now(timezone.utc)
      since = now - timedelta(hours=3)

      def L(component, **fields):
          return LoggedEntry(raw_text="x", author="Eli", created_at=now,
                             record=R(component=component, **fields))

      full = dict(problem_statement="p", alternatives_considered=["a"],
                  rationale="r", test_evidence="t")
      holes = [L("intake"), L("intake"), L("slide")]
      complete = [L("odometry", **full), L("odometry", **full)]

      def run(rows, enabled=True):
          with patch.object(P, "RECAP_ENABLED", enabled), \
               patch.object(storage, "list_entries", new=AsyncMock(return_value=rows)):
              return asyncio.run(P.session_recap(channel="discord", since=since))

      # Something is missing and a real session happened: speak.
      got = run(holes)
      assert got and got.entries == 3
      assert {t.component for t in got.threads} == {"intake", "slide"}

      # Off by default is a switch, not a suggestion.
      assert run(holes, enabled=False) is None
      # One message is not a meeting.
      assert run(holes[:1]) is None
      # Nothing missing: "all good" is noise (§8). Silence.
      assert run(complete) is None
  ```

- [x] **Step 2: Run it and watch it fail**

  Expected: `AttributeError: module 'core.pipeline' has no attribute
  'session_recap'`.

- [x] **Step 3: Implement**

  In `core/pipeline.py`, import `os` and explicitly initialize dotenv in this
  module (`from dotenv import load_dotenv`; `load_dotenv()`) before reading the
  new switch. Do not rely on an imported module's initialization side effect.
  Then add:

  ```python
  # Whether the bot may post a recap when a meeting goes quiet. OFF by default,
  # and that is a product decision, not caution for its own sake: §2's success
  # metric is that the team keeps dropping messages in for four weeks, and the
  # fastest way to lose that is a bot that posts a wall of text every night.
  # Turn it on in the second live week, once capture itself is trusted.
  RECAP_ENABLED = os.getenv("SESSION_RECAP", "off").strip().lower() in {"1", "on", "true"}

  # A session shorter than this is somebody thinking out loud, not a meeting.
  RECAP_MIN_ENTRIES = 2


  @dataclass
  class Recap:
      """What tonight left open. Nothing to act on means nothing is posted."""

      entries: int
      threads: list[Thread]


  async def session_recap(*, channel: str, since: datetime) -> Optional[Recap]:
      """The one place in this codebase where the bot speaks unprompted.

      Three gates, and None means silence:
      the switch is off, the session was too small to be a meeting, or every
      thread touched tonight is already complete. "All good tonight" is noise,
      and a channel that learns to skip the bot's messages stops reading the
      questions too (§8).
      """
      if not RECAP_ENABLED:
          return None

      entries = await storage.list_entries(since=since, channel=channel)
      if len(entries) < RECAP_MIN_ENTRIES:
          return None

      open_threads = [t for t in digest_threads(entries) if t.gaps]
      if not open_threads:
          return None
      return Recap(entries=len(entries), threads=open_threads)
  ```

  `Optional` is already imported. Add
  `from .digest import Digest, Thread, summarise, threads as digest_threads`
  to the import added in Task 3.

- [x] **Step 4: Run it and watch it pass**

  Run: `LLM_API_KEY=dummy uv run python -m tests.test_core` → **28** `ok`.

- [x] **Step 5: The card**

  In `tests/test_cards.py`:

  ```python
  def test_recap_card_is_short():
      from core.digest import Thread
      from core.pipeline import Recap
      from channels.discord_bot import _recap_card

      recap = Recap(entries=7, threads=[
          Thread("intake", 3, ("Eli",), (Stage.BUILD,),
                 frozenset({"test_evidence"}), NOW),
          Thread("slide", 2, ("Kim",), (Stage.DECISION,),
                 frozenset({"rationale", "test_evidence"}), NOW),
      ])
      card = _recap_card(recap)
      assert "7" in card.description
      assert len(card.fields) == 1, "one field: a recap is a note, not a report"
      assert "intake" in card.fields[0].value and "slide" in card.fields[0].value
      assert len(card.fields[0].value) <= 1024
  ```

  In `channels/discord_bot.py`:

  ```python
  # Threads named in a recap before it stops listing. A recap is a note left on
  # the door, not a report — if tonight touched more than this, the number is
  # the message.
  RECAP_MAX_THREADS = 5


  def _recap_card(recap) -> discord.Embed:
      """Tonight, and what it left open. Posted publicly — it is for the team."""
      embed = discord.Embed(
          title="Tonight's session",
          description=f"{recap.entries} entries captured · "
                      f"{len(recap.threads)} thread"
                      f"{'' if len(recap.threads) == 1 else 's'} still open",
          colour=discord.Colour.blurple(),
      )
      lines = [
          f"**{t.component}** — needs {_needs(t.gaps)}"
          for t in recap.threads[:RECAP_MAX_THREADS]
      ]
      if len(recap.threads) > RECAP_MAX_THREADS:
          lines.append(f"+{len(recap.threads) - RECAP_MAX_THREADS} more")
      embed.add_field(name="Still missing", value="\n".join(lines)[:1024],
                      inline=False)
      embed.set_footer(text="/digest for the whole season")
      return embed
  ```

- [x] **Step 6: The timer**

  In `channels/discord_bot.py`:

  ```python
  # How long a channel must go quiet before the meeting is treated as over.
  SESSION_IDLE_SECONDS = float(os.getenv("SESSION_IDLE_MINUTES", "90")) * 60
  ```

  In `Bot.__init__`, beside `self.bursts`:

  ```python
      # "The meeting ended" is "this channel went quiet for 90 minutes", which
      # is exactly what the burst coalescer already does — buffer per key, reset
      # on every arrival, fire after silence, and swallow flush errors so one
      # bad recap cannot kill the bot. Two instances, two windows, one class.
      # ponytail: max_items is set out of reach because this timer never wants
      # a size-triggered flush; if a meeting really produces 100k messages, a
      # recap is the least of it.
      self.sessions = Coalescer(
          self._session_ended, quiet=SESSION_IDLE_SECONDS, max_items=10**6
      )
  ```

  In `_handle`, as its **first** line — before the reply branch, because a
  reply to the bot's question is activity too and must reset the timer:

  ```python
          await self.sessions.add(str(message.channel.id), message.created_at)
  ```

  And the flush:

  ```python
      async def _session_ended(self, key: str, stamps: list[datetime]) -> None:
          """The channel has been quiet long enough to call the meeting over.

          Core decides whether there is anything worth saying; None is the
          normal answer, including whenever SESSION_RECAP is off.
          """
          recap = await pipeline.session_recap(channel="discord", since=min(stamps))
          if recap is None:
              return
          channel = self.get_channel(int(key))
          if channel is not None:
              await channel.send(embed=_recap_card(recap))
  ```

  `close()` is **not** changed: `self.bursts.drain()` stays, `self.sessions` is
  deliberately never drained. A restart is not the end of a meeting, and
  posting a recap because the process is going down would be a lie about what
  happened.

  `datetime` joins the imports at the top of the file.

- [x] **Step 7: `.env.example`**

  Under the Discord section:

  ```bash
  # Post a short recap when the channel goes quiet for SESSION_IDLE_MINUTES.
  # OFF by default: the bot speaking unprompted is the fastest way to get
  # muted, so switch it on only once the capture path is trusted.
  SESSION_RECAP=off
  SESSION_IDLE_MINUTES=90
  ```

  And under a new digest section:

  ```bash
  # ── Digest (core/digest.py) ───────────────────────────────────────────────────
  # Days a thread with holes may go untouched before /digest calls it stale.
  # UNMEASURED, like TASK_IDLE_MINUTES — a season has quiet weeks.
  DIGEST_STALE_DAYS=10
  ```

- [x] **Step 8: Run both suites and commit**

  Expected: **28** `ok` and **10** `ok`.

  ```bash
  git add core/pipeline.py channels/discord_bot.py tests/test_core.py tests/test_cards.py .env.example
  git commit -m "feat: an opt-in session recap, the one time the bot speaks first"
  ```

---

## Task 7: docs

- [x] **Step 1: `CLAUDE.md` §3** — under "In scope", the three ways in are now
  three ways in and **five** ways out (`/status`, `/board`, `/digest`, `/ask`,
  notebook). Add one line noting that `/ask` is the vector-search seam the
  section already rules in, and that the season digest is derived, never
  authored — the same side of the task-management line as `progress.py`.

- [x] **Step 2: `CLAUDE.md` §4 file tree** — add `core/digest.py` with a
  one-line description beside `core/progress.py`.

- [x] **Step 3: `CLAUDE.md` §10** — move `/digest`, `/ask`, the `/log`
  "Related earlier" field and the session recap into "written and checked
  offline, never run against real Discord", with the check counts (28 / 10).
  Add to **Known defects**: `DIGEST_STALE_DAYS` is unmeasured for the same
  reason `TASK_IDLE_MINUTES` is, and `/ask` returns nothing at all until
  `EMBEDDING_API_KEY` is set — which no live run has ever exercised, since the
  bot has never connected.

- [x] **Step 4: `docs/running-the-bot.md`** — §7 gains `h. /digest`, `i. /ask`
  and `j. the session recap`, and §9's checklist gains four lines:

  ```markdown
  - [ ] `/digest` named the same holes the notebook's coverage table shows
  - [ ] `/ask` found something you know is in the notebook (needs EMBEDDING_API_KEY)
  - [ ] a `/log` receipt showed "Related earlier" pointing at a different component
  - [ ] with SESSION_RECAP=on, exactly one recap appeared after the meeting — and
        none at all when nothing was missing
  ```

- [x] **Step 5: Commit**

  ```bash
  git add CLAUDE.md docs/running-the-bot.md
  git commit -m "docs: record the three global-awareness surfaces"
  ```

---

## Ship checklist

- [ ] `LLM_API_KEY=dummy uv run python -m tests.test_core` — 28 `ok`
- [ ] `LLM_API_KEY=dummy uv run python -m tests.test_cards` — 10 `ok`
- [ ] `git grep -n "import discord" -- core/ exporters/` — no hits
- [ ] `git grep -n "storage" -- channels/` — comments only
- [ ] `git grep -n '"Unfiled"' -- core channels exporters` — one hit, the schema
- [ ] no new dependency in `pyproject.toml`
- [ ] no prompt file changed: `git diff --stat core/prompts/` is empty
- [ ] nothing on any new card can be clicked, dragged or closed
- [ ] `SESSION_RECAP` unset ⇒ the bot posts nothing it was not asked for

## Still not done after this plan

- **Retrieval still does not reach the classifier.** That is a prompt change
  and §9 wants it measured; it cannot be measured until `tests/samples.py`
  holds real messages. This remains the single highest-value change blocked on
  the same blocker everything else is.
- **`/ask` has no relevance floor** — every hit is shown with its score. Set a
  floor after watching real queries, not before.
- **`DIGEST_STALE_DAYS` and `SESSION_IDLE_MINUTES` are guesses**, in the same
  bucket as `TASK_IDLE_MINUTES`. `/digest` is where a wrong stale window is
  obvious at a glance; use it to settle the number against real history.
- **No `/digest team:<name>` or per-subteam filter.** One team role exists.
  Same rule as `/board team:` — add it when there are two.
- **The recap cannot tell a meeting from a long afternoon.** It fires on
  channel silence, so an all-day build with a lunch break produces two. Watch
  it before adding a clock.
- **`LoggedEntry.channel` is the label `"discord"`, not a channel id.** So
  `/status`, `/board`, `/digest` and the recap all read every listened-to
  channel at once — inherited, not new. With two channels in
  `DISCORD_CHANNELS`, each gets its own idle timer and its own recap of the
  *same* entries. One listened channel is the assumption today; storing the
  channel id is a schema change and belongs in its own plan when a second one
  appears.
