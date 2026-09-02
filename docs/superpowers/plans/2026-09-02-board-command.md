# `/board` Implementation Plan

> **For the engineer picking this up:** steps use checkbox (`- [ ]`) syntax.
> Tick each one as you go. There is no execution skill installed in this repo —
> work the tasks directly, in order, one commit per task.

**Goal:** put the board where the team already is. `exporters/kanban.py` and
`scripts/kanban.py` already render "who is on what, right now" as markdown, but
reading it means running a script at a terminal — which nobody does mid-build.
`/board` answers the same question inside Discord, in one ephemeral card.

**Architecture:** one function in `core/pipeline.py` (`board()`, the exact
shape of the existing `status()`), one renderer in `channels/discord_bot.py`,
one slash command. `core/progress.by_team_and_stage()` already does the
grouping and is shared with the markdown exporter unchanged. No model call, no
new query beyond one `list_entries`, no schema change.

**Tech Stack:** Python 3.13, uv, discord.py 2.7, pydantic-ai, PostgreSQL +
pgvector. **No new dependencies.**

**Spec:** this plan is its own spec. It leans on `docs/design/progress-tracker.md`
for span semantics and on `CLAUDE.md` §3/§4/§9/§11 throughout.

---

## Global Constraints

Copied from `CLAUDE.md`. Violating one is a rejected task, not a style note.

- **No new dependencies.**
- **`core/` does not know Discord exists.** Nothing from the `discord` package
  may be imported anywhere under `core/`. Core returns data; the channel
  renders it.
- **No judgment calls in `channels/`** (§4). Choosing an emoji for an open span
  is presentation. Deciding *which role names a team*, or *what counts as one
  span*, is not — that is `core/progress.py`, and it already exists.
- **`channels/` must not import `core/storage.py`** (§11). Everything the card
  needs arrives on the object `core/pipeline.py` hands back.
- **Still not task management** (§3). Every card is a `progress.Span`. Nothing
  on this board may be created, assigned, closed, reordered or dragged by a
  human. If a step in this plan starts to look like it needs a button, stop —
  that is the line, and it is the whole reason the columns are design-cycle
  stages instead of To Do / Doing / Done.
- No schema change, no prompt change, no new model call in this plan.

### On testing

`tests/test_core.py` is pure: no API, no database, no framework, and **no
`discord` import**. That is deliberate and this plan does not change it.

So each task splits:

| Half | Verified by |
|---|---|
| anything in `core/` | a unit test in `tests/test_core.py`, TDD, as usual |
| anything in `channels/` | the live checklist in the task, run against a real server |

`channels/discord_bot.py` has never had a unit test and does not get one here.
Do not skip the live checks because the unit tests pass — they cover different
halves.

---

## Why `/board` and not a web UI

The two were weighed. `/board` wins, and not because §3 lists "web frontend" as
out of scope — that rule is downstream of the reason, not the reason itself.

§2's success metric is that the author's own team keeps dropping messages into
the channel for four consecutive weeks. Everything in this product is bet on
**input friction being near zero**, because the team is already in Discord all
meeting.

A web UI is a *place you have to go*: another tab, a login, a link somebody has
to find again next week. The board's value is ambient awareness *during* a
build session — "the test column is still empty" is useful at 8pm on a
Tuesday, and useless if seeing it costs a context switch. Nobody opens a second
tab between two screws.

The secondary costs are real but they are not the argument: hosting, auth
(which §3 rules out separately), deploy, a mobile layout — all of it landing
before the bot has connected to Discord even once (§10).

And `/board` is not a detour away from a web UI. §4's promise is that adding a
channel later is a zero-change operation on `core/`. Building `pipeline.board()`
now means a web board, if it is ever wanted, is a rendering job and nothing
else.

---

## Design decisions

### The one hard problem: Discord does not render markdown tables

`exporters/kanban.py` emits a `| Team | problem | ... |` grid. Pasted into
Discord that is a wall of misaligned pipes. **The exporter's rendering cannot
be reused here at all.**

What *is* reused is the grouping — `progress.by_team_and_stage()` — which both
surfaces call and neither owns. Two surfaces, two shapes, one grouping
function. That split is the whole design, and it is the same reason
`exporters/notebook.py` does not read storage.

### Stage becomes the visible axis, team moves onto the card

A Discord embed can lay fields out as **rows** or as **columns**, never as a
2D grid. So one axis has to give:

| Shape | Structure | Cost |
|---|---|---|
| one field per team | teams are the skeleton, stage prefixes each card | on a single-team server the lane axis is degenerate — one field holding everything |
| **one inline field per stage** | stages are the skeleton, team suffixes each card | with several teams a column mixes them |

**Take stage-as-columns.** `inline=True` fields sit three across on desktop, so
six stages render as two rows of three — an actual column layout. On mobile
Discord collapses inline fields to a vertical stack, which degrades to stage
headings with cards underneath: still readable, still ordered by the design
cycle.

Team-as-lanes degrades worse where it matters most: the server this is being
built for has one team role (`5898 Andromeda`), so team lanes would render a
single field containing the entire board — a list with a heading.

When a second team appears, add `/board team:<name>` as a filter. Do not try to
render two axes in an embed.

### Empty columns are dropped from the card, but named in the footer

`exporters/kanban.py` deliberately keeps all six columns even when empty,
because a fixed shape is what makes a board scannable day to day and an empty
`test` column mid-season is the most useful thing it can say.

An embed cannot afford that — Discord rejects an empty field value, and three
`—` placeholders eat half the card. So the card **skips empty stages and names
them in the footer**: `nothing yet in: problem, reflection`. The signal
survives; the space does not.

This is the one place the two renderings deliberately disagree, and the reason
is the medium, not the data.

### Ephemeral, like `/status`

Gotcha 7 forces the `/log` receipt to be public: the follow-up round trip is
keyed on replying to it and **an ephemeral message cannot be replied to**.

`/board` starts no round trip — nobody answers a board — so the reason does not
apply, and posting the whole team's board because one person asked is exactly
the channel noise §8 warns about. Ephemeral.

### A seven-day window, not `BOARD_DAYS`

`scripts/kanban.py` uses `BOARD_DAYS` (default 14) because an export is
something you deliberately go and generate. `/board` answers "right now", and
`pipeline.STATUS_WINDOW` already settled that question for `/status` at seven
days for the same reason: a season-long window answers "what are you on" with
something from October.

Two constants, two surfaces, two genuine reasons. Do not merge them.

### No channel gate

`/log` refuses outside `DISCORD_CHANNELS` because it *captures*. `/board` only
reads what was already captured, exactly like `/status`, which has no gate
either. Do not add one.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `core/schema.py` | +`STAGE_ORDER` | owns design-cycle reading order — third consumer earns the move |
| `exporters/notebook.py` | import `STAGE_ORDER` from schema | unchanged behaviour, one less definition |
| `core/pipeline.py` | +`BOARD_WINDOW`, +`Board`, +`board()` | the board's policy: how far back, and grouped how |
| `channels/discord_bot.py` | +`_board_card()`, +`/board` | renders what pipeline hands back. No `if` about meaning |
| `tests/test_core.py` | +`test_board_command` | the core half |
| `CLAUDE.md`, `docs/running-the-bot.md` | status + live checklist | |

`exporters/kanban.py` and `scripts/kanban.py` are **not touched**.

---

## Task 1: `STAGE_ORDER` moves to `core/schema.py`

Reading order of the design cycle is currently defined in
`exporters/notebook.py` and imported from there by `exporters/kanban.py`. The
Discord card is the third consumer, and `channels/` importing `exporters/` to
get it would be a layering smell for a constant that is really schema
vocabulary.

- [ ] **Step 1: Move it**

  In `core/schema.py`, below the `Stage` enum:

  ```python
  # Reading order of the design cycle. Not the enum's declaration order by
  # accident — it IS the enum's order, and this constant exists so the two can
  # never drift and so UNKNOWN is excluded in exactly one place.
  STAGE_ORDER = [s for s in Stage if s is not Stage.UNKNOWN]
  ```

- [ ] **Step 2: Update the importers**

  `exporters/notebook.py`: delete its local `STAGE_ORDER`, import from
  `core.schema`. `exporters/kanban.py` currently imports it from
  `exporters.notebook` — repoint to `core.schema`.

- [ ] **Step 3: Runnable check**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: 21 `ok` lines, unchanged. `test_notebook` and `test_board` both
  assert on stage ordering already, so a wrong order fails here, not in review.

- [ ] **Step 4: Commit** — `refactor: STAGE_ORDER belongs to the schema, not the notebook`

---

## Task 2: `pipeline.board()`

Pure policy, no rendering. Mirrors `status()` line for line.

- [ ] **Step 1: Write the test first**

  In `tests/test_core.py`, `test_board_command`. Patch
  `storage.list_entries` with an `AsyncMock` the way `test_status` already
  does — no database.

  Assert:
  - the returned `lanes` match `by_team_and_stage(spans(...))` for the same
    entries (the function must not re-implement the grouping)
  - `list_entries` was called with `since` ≈ now − `BOARD_WINDOW` and the
    channel passed in
  - an entry older than the window does not appear
  - `Board.since` is what the footer will render

- [ ] **Step 2: Implement**

  In `core/pipeline.py`:

  ```python
  # How far back the board looks. Same reasoning as STATUS_WINDOW: a
  # season-long window answers "what is on the go" with something from October.
  # Deliberately NOT scripts/kanban.py's BOARD_DAYS — an export is something
  # you go and generate, a card answers "right now".
  BOARD_WINDOW = timedelta(days=7)


  @dataclass
  class Board:
      """Every open and recently-closed span, laned by team.

      Read-only and derived, exactly like Status: this creates nothing, assigns
      nothing and closes nothing. CLAUDE.md §3.
      """

      lanes: dict[str, dict[Stage, list[progress.Span]]]
      since: datetime


  async def board(*, channel: str) -> Board:
      """One query, no model call. The grouping is progress.py's, not ours."""
      now = datetime.now(timezone.utc)
      since = now - BOARD_WINDOW
      entries = await storage.list_entries(since=since, channel=channel)
      return Board(
          progress.by_team_and_stage(progress.spans(entries, now=now)), since
      )
  ```

  `Stage` needs adding to the `.schema` import line.

- [ ] **Step 3: Runnable check**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: 22 `ok` lines.

- [ ] **Step 4: Commit** — `feat: pipeline.board(), the board's policy in one query`

---

## Task 3: `_board_card()` and `/board`

- [ ] **Step 1: The card**

  In `channels/discord_bot.py`, beside `_status_card`:

  ```python
  # Cards shown per column before the rest are rolled into a "+N more" line.
  # Discord caps a field value at 1024 characters and the whole embed at 6000;
  # blowing either is an HTTP 400, not a graceful degrade.
  # ponytail: a flat cap, not a character budget. Ten cards is roughly 400
  # characters — measure before making it cleverer.
  BOARD_MAX_CARDS = 10


  def _board_card(board) -> discord.Embed:
      """The board as a Discord embed.

      Not the markdown grid: Discord does not render tables, and an embed lays
      fields out as rows or columns but never both. Stage is the visible axis
      because it is the one that carries information on a single-team server;
      the team rides along on each card. See the plan for the trade.
      """
  ```

  Shape:

  1. Invert `board.lanes` into `stage -> [(team, span)]`.
  2. For each stage in `STAGE_ORDER` **that has cards**, add an
     `inline=True` field: name is the stage, value is one line per card,
     live spans before quiet ones, most recently active first.
  3. Card line: `● intake — Eli · 5898 Andromeda`. Drop the team suffix
     entirely when the board has exactly one lane — on a single-team server it
     is the same eleven characters on every line, and §8's "match the input's
     length" instinct applies to a card too.
  4. Footer: `nothing yet in: problem, reflection · last 7 days`, with the
     window rendered from `board.since` as `<t:...:R>` (each viewer's own
     timezone — gotcha 8 is an export problem, not a Discord one).
  5. Empty board: a greyple embed, `"Nothing on the go"`, same shape
     `_status_card` already uses for its empty case.

  Colour: green when every column that has cards has at least one live span,
  orange otherwise. That is presentation over data core computed — allowed.

- [ ] **Step 2: The command**

  Beside `/status`, same ephemeral pattern:

  ```python
  @self.tree.command(name="board", description="Who is on what right now")
  async def _board(interaction: discord.Interaction):
      await interaction.response.defer(thinking=True, ephemeral=True)
      result = await pipeline.board(channel="discord")
      await interaction.followup.send(embed=_board_card(result), ephemeral=True)
  ```

  No `DISCORD_CHANNELS` gate — see Design decisions.

- [ ] **Step 3: Runnable check**

  ```bash
  uv run python -c "import channels.discord_bot"
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Imports and 22 `ok`. This proves nothing about the card's appearance — that
  is Step 4's job.

- [ ] **Step 4: Live check** (needs a real server; `DISCORD_GUILD_ID` set, or
  a global sync can take an hour to appear — §10 checklist item 3)

  - [ ] `/board` appears in the command list after sync
  - [ ] the card is visible only to you
  - [ ] columns read left to right in design-cycle order on desktop
  - [ ] the card is still legible on mobile (fields stack; that is expected)
  - [ ] a stage with no work is absent from the fields and present in the footer
  - [ ] with the channel's current data, the lanes match
        `uv run python -m scripts.kanban` — the two renderings must never
        disagree about *what* is on the board, only about how it looks
  - [ ] force the truncation path once (temporarily set `BOARD_MAX_CARDS = 1`)
        and confirm the `+N more` line, then set it back

- [ ] **Step 5: Commit** — `feat: /board, the work board where the team already is`

---

## Task 4: docs

- [ ] **Step 1:** `CLAUDE.md` §10 — move the board out of "no `/board` command
  yet" into the verified list, and record what the live run actually showed.
- [ ] **Step 2:** `docs/running-the-bot.md` §8 — `/board` beside
  `scripts/kanban.py`, and in §7 a step for reading the board mid-session.
- [ ] **Step 3:** `CLAUDE.md` §5 file tree — no new files, so only the
  `channels/discord_bot.py` and `core/pipeline.py` lines need a word.
- [ ] **Step 4: Commit** — `docs: record /board`

---

## Ship checklist

Nothing here touches a prompt, so §9's three metrics are not involved and no
scoring loop runs.

- [ ] 22 `ok` in `tests/test_core.py`
- [ ] `/board` and `scripts/kanban.py` agree on the same data
- [ ] the card is ephemeral
- [ ] nothing on the card can be clicked, dragged or closed

---

## Still not done after this plan

- **`/board team:<name>`** — no filter, because there is one team role. Add it
  when there are two, not before.
- **`TASK_IDLE_MINUTES` is still unmeasured** (§10 known defects). It decides
  `●` versus `○` on every card, so the board puts an uncalibrated number in
  front of the team. `/board` is the first surface where a wrong value is
  obvious at a glance — use it to settle the number against real channel
  history, which is what `notes.md` has been asking for.
- **Entries written before `author_roles` existed stay under `No tag`.**
  Nothing in this plan backfills them; from today's roster it would be a guess,
  and a wrong one for anyone who has changed teams or left.
- **The team-role heuristic is "name starts with a number"**, checked against
  exactly one real server. A role called "2nd place winner" would read as a
  team. Widen it after seeing it break, not before.
