# `/board` Fixes Implementation Plan

> **For the engineer picking this up:** steps use checkbox (`- [ ]`) syntax.
> Tick each one as you go. There is no execution skill installed in this repo —
> work the tasks directly, in order, one commit per task.

**Goal:** close the four defects found reviewing `/board` against
`docs/superpowers/plans/2026-09-02-board-command.md`: a footer that renders a
raw `<t:...>` tag, a field value that can exceed Discord's 1024-character cap
and take the whole command down with an HTTP 400, a `CLAUDE.md` claim about an
offline check that does not exist in the repo, and one stale import.

**Architecture:** `_board_card()` in `channels/discord_bot.py` is the only
code that changes, by four lines. Everything else is a new offline check file
that makes those four lines verifiable without a Discord server, plus docs. No
change to `core/`, no new query, no schema change, no prompt change.

**Tech Stack:** Python 3.13, uv, discord.py 2.7, pydantic-ai, PostgreSQL +
pgvector. **No new dependencies.**

**Spec:** `docs/superpowers/plans/2026-09-02-board-command.md` — the plan this
one repairs. Its Design decisions section still governs; two of its
instructions were wrong about the medium and this plan says so where it
overrides them.

---

## Global Constraints

Copied from `CLAUDE.md`. Violating one is a rejected task, not a style note.

- **No new dependencies.**
- **`core/` does not know Discord exists** (§4). Nothing in this plan puts
  anything in `core/` at all.
- **`channels/` must not import `core/storage.py`** (§11).
- **`tests/test_core.py` stays pure** — no API, no database, no framework, and
  **no `discord` import**. The new checks therefore go in their own file. Do
  not be tempted to merge them; the purity of `test_core.py` is what lets it
  run with no key and no container.
- **Still not task management** (§3). Nothing here adds a control to the card.
- No schema change, no prompt change, no new model call.

### On what these checks can and cannot prove

A `discord.Embed` is a plain Python object until someone sends it. That is why
its *structure* — which fields exist, in what order, what the strings say, how
long they are — is checkable offline with no token and no server.

Its *appearance* is not. Column layout on desktop, the mobile stack, ephemeral
visibility and whether the sync landed remain the live checklist in
`docs/running-the-bot.md` §7g. Two halves, two verification methods, neither
substitutes for the other.

---

## Findings this plan closes

| # | Where | What is wrong |
|---|---|---|
| 1 | `channels/discord_bot.py:166` | `<t:...:R>` in `set_footer` — Discord does not parse markdown or timestamp tags in footer text, so it renders literally. Also duplicates the "last 7 days" already in the same string, and hardcodes `7` where `pipeline.BOARD_WINDOW` owns it. |
| 2 | `channels/discord_bot.py:162` | `BOARD_MAX_CARDS` caps *cards*, not characters. `component` is free text the model extracted; ten long ones blow the 1024-character field cap and Discord answers HTTP 400 — the whole command fails, it does not degrade. |
| 3 | `CLAUDE.md:479` | Claims "an offline embed-structure check" passes. No such check exists; `_board_card` has no test reference anywhere in the repo. |
| 4 | `tests/test_core.py:30` | Still imports `STAGE_ORDER` from `exporters.notebook`. Task 1 Step 2 of the board plan moved the definition to `core.schema`; this importer was missed. Works only through a re-export. |

Finding 1 and 2 are both faithful implementations of instructions in the
original plan. The plan was wrong, not the engineer: it specified a `<t:...:R>`
footer and a flat card cap. This plan overrides both.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `tests/test_cards.py` | **create** | Offline structure checks for the Discord cards. The only file in `tests/` allowed to import `discord`. |
| `channels/discord_bot.py` | 4 lines in `_board_card()` | unchanged responsibility — renders what pipeline hands back |
| `tests/test_core.py` | one import line | unchanged |
| `CLAUDE.md` | §10 status, §9 test paragraph | says what is actually verified |
| `docs/running-the-bot.md` | §7g | one sentence about what the footer now shows |

`core/`, `exporters/` and `scripts/` are **not touched**.

---

## Task 1: `tests/test_cards.py`, the check that does not exist yet

Finding 3. This task is a characterization test: it locks in the behaviour
`_board_card` **already has and should keep**, so it passes on the first run.
That is not a TDD violation — Tasks 2 and 3 are the TDD ones, and they need a
file to add their failing assertions to.

**Files:**
- Create: `tests/test_cards.py`

**Interfaces:**
- Consumes: `channels.discord_bot._board_card(board) -> discord.Embed` and
  `BOARD_MAX_CARDS: int`; `core.pipeline.Board(lanes, since)`;
  `core.progress.Span(author, component, started_at, last_at, ended_at,
  entry_ids=(), stages=(), team=UNTAGGED)`; `core.schema.Stage`.
- Produces: the helper `S(...)` and the module's `__main__` runner, which
  Tasks 2 and 3 add assertions to.

- [ ] **Step 1: Create the file**

  ```python
  """Offline structure checks for the Discord cards.

  Imports `discord`, which is why this is not in tests/test_core.py — that file
  is pure by design (§9) and must keep running with no key, no container and no
  discord.py. Nothing here talks to Discord either: an Embed is a plain object
  until someone sends it, so every branch in _board_card is checkable at a
  terminal.

  What this cannot check is appearance — desktop columns, the mobile stack,
  ephemeral visibility. That is the live checklist in docs/running-the-bot.md
  §7g, and these checks passing is not a substitute for running it.

  Run: LLM_API_KEY=dummy uv run python -m tests.test_cards
  """

  from datetime import datetime, timedelta, timezone

  from channels.discord_bot import BOARD_MAX_CARDS, _board_card
  from core.pipeline import BOARD_WINDOW, Board
  from core.progress import Span
  from core.schema import Stage

  NOW = datetime(2025, 10, 12, 3, 0, tzinfo=timezone.utc)
  SINCE = NOW - BOARD_WINDOW
  ANDROMEDA = "5898 Andromeda"


  def S(component, *, author="Eli", live=True, stage=Stage.BUILD, ago=5):
      """One span, built directly. progress.spans() is tested in test_core."""
      last = NOW - timedelta(minutes=ago)
      return Span(
          author=author,
          component=component,
          started_at=last - timedelta(minutes=10),
          last_at=last,
          ended_at=None if live else last,
          stages=(stage,),
      )


  def test_empty_board():
      embed = _board_card(Board({}, SINCE))
      assert embed.title == "Nothing on the go"
      assert not embed.fields, "an empty board is a sentence, not a grid"


  def test_columns_are_stages_in_cycle_order():
      board = Board(
          {ANDROMEDA: {
              Stage.TEST: [S("odometry", live=False, stage=Stage.TEST)],
              Stage.BUILD: [S("intake")],
          }},
          SINCE,
      )
      embed = _board_card(board)

      # STAGE_ORDER wins over the dict's insertion order, and empty stages are
      # dropped rather than rendered as placeholders.
      assert [f.name for f in embed.fields] == ["build", "test"]
      assert all(f.inline for f in embed.fields), "inline is what makes columns"
      assert embed.fields[0].value == "● intake — Eli"
      assert embed.fields[1].value == "○ odometry — Eli"
      # One lane: the team name would be the same characters on every line.
      assert ANDROMEDA not in embed.fields[0].value
      # The stages with no work survive as text instead of as empty columns.
      assert "nothing yet in: problem, ideation, decision, reflection" \
          in embed.footer.text


  def test_two_lanes_put_the_team_on_the_card():
      board = Board(
          {ANDROMEDA: {Stage.BUILD: [S("intake")]},
           "7161": {Stage.TEST: [S("arm", author="Sam", stage=Stage.TEST)]}},
          SINCE,
      )
      embed = _board_card(board)
      assert embed.fields[0].value == f"● intake — Eli · {ANDROMEDA}"
      assert embed.fields[1].value == "● arm — Sam · 7161"


  def test_long_column_rolls_up():
      cards = [S(f"c{i}") for i in range(BOARD_MAX_CARDS + 3)]
      embed = _board_card(Board({ANDROMEDA: {Stage.BUILD: cards}}, SINCE))
      value = embed.fields[0].value
      assert value.endswith("+3 more")
      assert len(value.splitlines()) == BOARD_MAX_CARDS + 1


  if __name__ == "__main__":
      for name, fn in sorted(globals().items()):
          if name.startswith("test_"):
              fn()
              print(f"ok  {name}")
  ```

- [ ] **Step 2: Run it**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: 4 `ok` lines. `LLM_API_KEY` is needed because importing
  `channels.discord_bot` pulls in `core.agent`, which reads the key at import
  time (§6 gotcha 5) — `dummy` is enough, nothing calls the API.

  If it fails on `ANDROMEDA not in embed.fields[0].value`, the single-lane
  suffix rule broke. If it fails on field order, `STAGE_ORDER` drifted.

- [ ] **Step 3: Confirm `test_core.py` is untouched and still pure**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: 22 `ok`, unchanged.

- [ ] **Step 4: Commit**

  ```bash
  git add tests/test_cards.py
  git commit -m "test: the offline card check CLAUDE.md already claimed"
  ```

---

## Task 2: the footer renders as text, because footers are text

Finding 1. Discord parses markdown and `<t:...>` timestamp tags in an embed's
**description and field values only**. Footer text, title, author name and
field names are plain text, so the current footer shows a literal
`<t:1760237400:R>` to every viewer.

The information is still worth having. `discord.Embed.timestamp` is the native
way to carry it: Discord renders it beside the footer text in each viewer's own
timezone, which is exactly what the original plan wanted `<t:...:R>` for.

While on this line, `"last 7 days"` also stops being a hardcoded `7` — the
window is `pipeline.BOARD_WINDOW`'s to decide, and `channels/` already imports
`pipeline`.

**Files:**
- Modify: `channels/discord_bot.py:164-167` (the footer block of `_board_card`)
- Test: `tests/test_cards.py`

- [ ] **Step 1: Write the failing assertions**

  In `tests/test_cards.py`, add a new check below `test_empty_board`:

  ```python
  def test_footer_is_plain_text():
      """Discord renders markdown in descriptions and field values. Not here.

      A `<t:...>` tag in footer text reaches the reader as those exact
      characters, so the window goes on embed.timestamp instead, which Discord
      does localise (gotcha 8 is an export problem — this surface has no
      TEAM_TZ).
      """
      embed = _board_card(Board({ANDROMEDA: {Stage.BUILD: [S("intake")]}}, SINCE))
      assert "<t:" not in embed.footer.text, "footer text is never parsed"
      assert f"last {BOARD_WINDOW.days} days" in embed.footer.text
      assert embed.timestamp == SINCE
  ```

- [ ] **Step 2: Run it and watch it fail**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: `AssertionError: footer text is never parsed` from
  `test_footer_is_plain_text`. The other four still print `ok`.

- [ ] **Step 3: Fix the footer**

  In `channels/discord_bot.py`, replace:

  ```python
      empty = [stage.value for stage, cards in columns.items() if not cards]
      footer = [f"nothing yet in: {', '.join(empty)}"] if empty else []
      footer.append(f"last 7 days · <t:{int(board.since.timestamp())}:R>")
      embed.set_footer(text=" · ".join(footer))
      return embed
  ```

  with:

  ```python
      empty = [stage.value for stage, cards in columns.items() if not cards]
      footer = [f"nothing yet in: {', '.join(empty)}"] if empty else []
      footer.append(f"last {pipeline.BOARD_WINDOW.days} days")
      embed.set_footer(text=" · ".join(footer))
      # Not <t:...> in the footer text: Discord parses markdown in descriptions
      # and field values, and nowhere else — a tag there reaches the reader
      # verbatim. embed.timestamp is the native slot and is localised per
      # viewer, which is what the tag was reaching for.
      embed.timestamp = board.since
      return embed
  ```

- [ ] **Step 4: Run it and watch it pass**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: 5 `ok` from `test_cards`, 22 `ok` from `test_core`.

- [ ] **Step 5: Commit**

  ```bash
  git add channels/discord_bot.py tests/test_cards.py
  git commit -m "fix: the board footer is plain text, so the timestamp goes native"
  ```

---

## Task 3: a character cap, because that is what Discord counts

Finding 2. `BOARD_MAX_CARDS = 10` limits cards; Discord limits a field value to
**1024 characters** and the whole embed to 6000. `component` is free text the
model extracted from a Discord message, so ten cards is *roughly* 400
characters and occasionally is not. Over the cap, `followup.send` raises
`discord.HTTPException` and `/board` fails outright — there is no partial
render.

The lazy fix is a slice, not a budgeting algorithm: cut the joined value at
1024. It cuts mid-line in a case that should be rare, and a truncated line
beats a dead command.

**Files:**
- Modify: `channels/discord_bot.py:162` (the `add_field` call in `_board_card`)
- Test: `tests/test_cards.py`

- [ ] **Step 1: Write the failing assertion**

  In `tests/test_cards.py`, below `test_long_column_rolls_up`:

  ```python
  def test_field_value_stays_under_discords_cap():
      """1024 characters per field value. Over it, Discord answers HTTP 400 and
      the whole command dies — the card does not degrade, it disappears.

      BOARD_MAX_CARDS counts cards, and a component is whatever the model
      pulled out of a Discord message, so cards alone cannot bound the string.
      """
      cards = [S("swerve module bearing retainer " * 10) for _ in range(BOARD_MAX_CARDS)]
      embed = _board_card(Board({ANDROMEDA: {Stage.BUILD: cards}}, SINCE))
      assert len(embed.fields[0].value) <= 1024
  ```

- [ ] **Step 2: Run it and watch it fail**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: `AssertionError` from `test_field_value_stays_under_discords_cap`
  (the value is around 3200 characters). The other five print `ok`.

- [ ] **Step 3: Add the guard**

  In `channels/discord_bot.py`, replace:

  ```python
          embed.add_field(name=stage.value, value="\n".join(lines), inline=True)
  ```

  with:

  ```python
          # ponytail: a blunt slice. Discord counts characters, not cards, and
          # over 1024 it answers HTTP 400 for the whole embed rather than
          # dropping a field — a line cut mid-word beats a dead command. Spend
          # a real character budget here only if long components turn out
          # common in the channel.
          embed.add_field(
              name=stage.value, value="\n".join(lines)[:1024], inline=True
          )
  ```

  Leave `BOARD_MAX_CARDS` alone: it is what produces the `+N more` line, which
  the slice cannot.

- [ ] **Step 4: Run it and watch it pass**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  LLM_API_KEY=dummy uv run python -m tests.test_core
  ```

  Expected: 6 `ok` from `test_cards`, 22 `ok` from `test_core`.

- [ ] **Step 5: Commit**

  ```bash
  git add channels/discord_bot.py tests/test_cards.py
  git commit -m "fix: cap the board's field value in characters, not cards"
  ```

---

## Task 4: the last `STAGE_ORDER` importer

Finding 4. The board plan's Task 1 moved the definition to `core/schema.py` and
repointed `exporters/notebook.py` and `exporters/kanban.py`. `tests/test_core.py`
still reads it through `exporters.notebook`, which only works because that
module imports it. A test asserting on stage order should read it from the
module that owns it.

**Files:**
- Modify: `tests/test_core.py:20-30` (the import block)

- [ ] **Step 1: Repoint the import**

  Change:

  ```python
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
  ```

  to:

  ```python
  from core.schema import (
      STAGE_ORDER,
      DesignRecord,
      FollowupPatch,
      FollowupTurn,
      LoggedEntry,
      Stage,
      Subteam,
  )
  from exporters.kanban import IDLE as QUIET
  from exporters.kanban import LIVE, render_board
  from exporters.notebook import UNFILED, render_notebook
  ```

- [ ] **Step 2: Run the checks**

  ```bash
  LLM_API_KEY=dummy uv run python -m tests.test_core
  LLM_API_KEY=dummy uv run python -m tests.test_cards
  ```

  Expected: 22 `ok` and 6 `ok`. `test_notebook` and `test_board` both assert on
  stage order, so a wrong constant fails here.

- [ ] **Step 3: Commit**

  ```bash
  git add tests/test_core.py
  git commit -m "refactor: read STAGE_ORDER from the module that owns it"
  ```

---

## Task 5: docs say what is actually verified

Finding 3's other half. `CLAUDE.md` claimed a check that did not exist; now it
exists, and the claim should name the file so the next reader can run it.

**Files:**
- Modify: `CLAUDE.md` §9 ("The other tests") and §10 (status)
- Modify: `docs/running-the-bot.md` §7g

- [ ] **Step 1: `CLAUDE.md` §9, after the `python -m tests.test_core` paragraph**

  Add:

  ```markdown
  `python -m tests.test_cards` — the same idea for the Discord cards, in its own
  file because it imports `discord` and `test_core.py` may not. It builds a
  `pipeline.Board` by hand and asserts on the embed `_board_card` returns:
  column order, dropped-and-footnoted empty stages, the single-lane team-suffix
  rule, the `+N more` roll-up, plain-text footer, and the 1024-character field
  cap. It says nothing about how the card *looks* — that is the live checklist
  in `docs/running-the-bot.md` §7g.
  ```

- [ ] **Step 2: `CLAUDE.md` §10, the "never run against real Discord" bullet**

  Replace "The `/board` module import, 22 pure checks, and an offline
  embed-structure check pass" with a sentence that names the file and the
  counts, e.g. "`tests/test_cards.py` checks the card's structure offline (6
  checks) and `tests/test_core.py` the policy behind it (22); no live Discord
  card, command sync, desktop/mobile layout, or ephemeral visibility check has
  been run."

- [ ] **Step 3: `docs/running-the-bot.md` §7g**

  §7g currently ends "Check it once on desktop and once on mobile; inline
  fields stack on mobile by design." Add after it:

  ```markdown
  The footer names the empty stages and the window; the date beside it is
  `embed.timestamp`, rendered in your own timezone. The `+N more` roll-up and
  the field-length cap are covered offline by `tests/test_cards.py` — do not
  edit `BOARD_MAX_CARDS` mid-session to see them, that is how a temporary edit
  gets committed.
  ```

  Change nothing else: desktop column order, the mobile stack, ephemeral
  visibility and agreement with `uv run python -m scripts.kanban` are still
  live-only and still required.

- [ ] **Step 4: Commit**

  ```bash
  git add CLAUDE.md docs/running-the-bot.md
  git commit -m "docs: name the card check instead of claiming it"
  ```

---

## Ship checklist

- [ ] `LLM_API_KEY=dummy uv run python -m tests.test_core` — 22 `ok`
- [ ] `LLM_API_KEY=dummy uv run python -m tests.test_cards` — 6 `ok`
- [ ] `git grep -n "<t:" channels/` returns only `_status_card`'s field values,
      never a `set_footer` argument
- [ ] `git grep -n "STAGE_ORDER" -- tests exporters channels` shows every
      importer reading from `core.schema`
- [ ] no file under `core/` imports `discord`; no file under `channels/`
      imports `core.storage`

## Still not done after this plan

- **Nothing here has run against a real Discord server.** All four fixes are
  structural and checkable offline, which is why they are worth doing before
  the first live run — but §10's live checklist is untouched by this plan and
  is still the thing standing between `/board` and "verified".
- **The 6000-character embed total is still unbounded.** Six columns sliced at
  1024 is 6144 in the worst case. It needs a real budget across fields, which
  is more machinery than a case nobody has seen deserves. Revisit if a board
  ever fails to send with every column full.
- **`embed.timestamp` beside "last 7 days" is mildly redundant** — one says the
  window, one says the date it starts. Drop one after seeing the rendered card,
  not before.
