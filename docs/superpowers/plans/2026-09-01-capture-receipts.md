# Capture Receipts Implementation Plan

> **For the engineer picking this up:** steps use checkbox (`- [ ]`) syntax.
> Tick each one as you go. There is no execution skill installed in this repo —
> work the tasks directly, in order, one commit per task.

**Goal:** make the bot's capture visible without making it talk more. Today an
ambient message produces either a question or total silence, and silence is
indistinguishable from a crash — which is exactly what happened on the first
live run (see §"What the live run showed" below).

**Architecture:** three tiers of feedback, ordered by how much channel noise
they cost. Tier 1 is an emoji reaction (zero noise, the common case). Tier 2 is
a rich embed on `/log`, which already posts a receipt. Tier 3 is `/status`, an
ephemeral read that costs the channel nothing. No tier adds a message the bot
does not already send.

**Tech Stack:** Python 3.13, uv, discord.py 2.7, pydantic-ai, PostgreSQL +
pgvector. **No new dependencies.**

**Spec:** this plan is its own spec. It leans on
`docs/design/progress-tracker.md` for Tier 3's span semantics and on
`CLAUDE.md` §4/§7/§8 throughout.

---

## Global Constraints

Copied from `CLAUDE.md`. Violating one is a rejected task, not a style note.

- **No new dependencies.**
- **`core/` does not know Discord exists.** No `discord.Embed` — nothing from
  the `discord` package — may be imported anywhere under `core/`. Core returns
  data; the channel renders it.
- **No judgment calls in `channels/`** (§4). Choosing a colour from a set of
  gaps core computed is presentation. Deciding *what* the gaps are is not, and
  belongs in `core/`.
- **`channels/` must not import `core/storage.py`** (§11). Everything the
  channel needs arrives on the object `core/pipeline.py` hands back.
- **The receipt must never take the record down with it.** A missing reaction
  permission, a deleted message, a failed embed — none of these may raise past
  the capture. Same rule `storage.save()` already applies to embeddings: the
  nice-to-have fails soft, persistence does not.
- **Coverage is per *thread*, never per entry** (§10 finding 1). A design line
  whose problem, alternatives, rationale and results are spread over three
  entries is complete. `record.missing_fields` is scoped to one message and
  exists to drive one follow-up — rendering it as "what's missing" reports a
  complete thread as full of holes and sends the team chasing nothing.
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
Building a discord.py mock harness would be more test infrastructure than the
110 lines it would cover. Do not skip the live checks because the unit tests
pass — they cover different halves.

---

## What the live run showed

Grounding, so the design is not argued from imagination. From the first real
Discord session (2026-09-01):

- Ambient message captured, question asked, answer merged. All three paths work.
- The user replied "Not too sure. Testing needed". `filled` came back empty, so
  `should_ask_again()` correctly stopped and the bot said nothing.
- **The user read that silence as a failure and asked what broke.** Nothing had.
  The record was saved, the turn was closed, the DB was correct.

That is the whole problem this plan solves. The bot was right to stay quiet and
wrong to be invisible, and those are separable.

---

## Design decisions

### Why a reaction, and not a card, on the ambient path

A card per captured message means the bot posts on every design message in the
channel. A build session is dozens of them. §7 is explicit that this is how the
bot gets muted in week one, and §2's success metric — the team still dropping
messages in after four weeks — dies with it.

A reaction is acknowledgement without a turn: no new message, no push to the
channel, no ping, no interruption of whatever the team was saying. Absence of a
reaction is also information — chitchat gets none, which is how you see triage
working at a glance.

### Why the follow-up question does NOT get a card

§8 says the follow-up should read like a teammate, and the tone section is
built from example sentences precisely because that is hard to get right.
Wrapping "why the dual roller?" in a titled embed with fields turns a question
into a form. `/log` is different: someone deliberately opened a modal and typed
a write-up, and a structured receipt matches that intent.

### Why `/status` is ephemeral and `/log` is not

Gotcha 7 says a `/log` receipt must be public, because the whole follow-up
round trip is keyed on replying to it and **an ephemeral message cannot be
replied to**. `/status` starts no round trip — nobody answers a status card —
so the reason does not apply, and posting one person's summary to the whole
channel is pure noise. This is the one place ephemeral is the right answer.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `channels/discord_bot.py` | Modify (~70 lines) | Adds `_receipt_reaction`, `_card`, `_status_card`, `/status`. Deletes `_receipt`. Still no judgment calls. |
| `core/pipeline.py` | Modify (~45 lines) | `Ingested` gains `gaps`. New `_respond` collapses two thread reads into one. New `Status` + `status()`. |
| `core/storage.py` | Modify (2 lines) | `list_entries` gains a `channel` filter. |
| `tests/test_core.py` | Modify (~60 lines) | `test_respond_reports_thread_gaps`, `test_status`. |
| `CLAUDE.md` | Modify | §10 status. |
| `docs/running-the-bot.md` | Modify | Reaction and `/status` in the test script; new permission integer. |

No change to `core/schema.py`, `core/agent.py`, `core/followup.py`,
`core/progress.py`, `core/triage.py`, `core/inbox.py`, `exporters/`, or any
prompt.

---

## Task 1: Tier 1 — a reaction says "this is in the notebook"

Smallest tier, largest payoff, and it touches `core/` not at all.

**Files:**
- Modify: `channels/discord_bot.py`

**Interfaces:**
- Consumes: `pipeline.ingest` / `pipeline.handle_reply` returning
  `Optional[Ingested]` — unchanged
- Produces: `_receipt_reaction(message: discord.Message) -> None`

- [ ] **Step 1: Grant the permission**

The bot is already in the server, so no re-invite is needed. Server Settings →
Roles → `doc.bot` → Permissions → enable **Add Reactions**. Save.

(If you would rather re-invite: `permissions=85056` on
`https://discord.com/oauth2/authorize`. That value also covers Task 2's
Embed Links, so doing it once here saves a round trip.)

- [x] **Step 2: Add the reaction helper**

In `channels/discord_bot.py`, after the `CHANNELS` / `GUILD_ID` block:

```python
# The capture receipt. A reaction rather than a message on purpose: it says
# "this is in the notebook" without spending a turn in the channel. §8's rule
# is that the bot posts publicly and should stay quiet when in doubt — a
# reaction is how you acknowledge without talking. Absence of one is also
# information: chitchat never gets it, so triage is visible at a glance.
CAPTURED = "📓"


async def _receipt_reaction(message: discord.Message) -> None:
    """Mark a message as captured. Never allowed to break the capture itself."""
    try:
        await message.add_reaction(CAPTURED)
    except discord.HTTPException:
        # Missing Add Reactions, or the message was deleted mid-flush. The
        # record is already saved and a receipt is cosmetic — same rule
        # storage.save() applies to embeddings, where the optional half is
        # never allowed to take the durable half down with it.
        log.warning("could not add the capture reaction", exc_info=True)
```

`discord.Forbidden` and `discord.NotFound` both subclass
`discord.HTTPException`, so one clause covers the permission case and the
deleted-message race.

- [x] **Step 3: React on the ambient path**

In `Bot._flush_burst`, replace

```python
        result = await pipeline.ingest(
            channel="discord",
            author=first.author.display_name,
            created_at=first.created_at,
            channel_message_id=str(first.id),
            raw_text="\n".join(m.content for m in messages),
        )
        await _say(result, last.reply)
```

with

```python
        result = await pipeline.ingest(
            channel="discord",
            author=first.author.display_name,
            created_at=first.created_at,
            channel_message_id=str(first.id),
            raw_text="\n".join(m.content for m in messages),
        )
        if result is not None:
            await _receipt_reaction(last)
        await _say(result, last.reply)
```

The reaction goes on the **last** message of the burst, the same one `_say`
replies to, so everything the bot does about a burst lands in one place.

- [x] **Step 4: React on the reply path**

This is the case that actually bit the first live run — a deflection is
processed correctly and looks like a crash. In `Bot._handle`, replace

```python
            if result is not None:
                await _say(result, message.reply)
                return
```

with

```python
            if result is not None:
                # A reply that filled nothing ends the exchange in silence, by
                # design. Without this the person cannot tell "merged, nothing
                # to add" from "the bot fell over" — which is exactly how the
                # first live run read.
                await _receipt_reaction(message)
                await _say(result, message.reply)
                return
```

- [ ] **Step 5: Live check**

Restart the bot:

```bash
uv run python -m channels.discord_bot
```

In the test server, confirm all four:

| Do | Expect |
|---|---|
| post a design message, wait ~45s | 📓 appears on it |
| post `who's driving tomorrow` | **no** reaction, no reply, and no new `POST .../chat/completions` line in the log |
| reply to one of the bot's questions | 📓 appears on your reply, whether or not it asks again |
| revoke Add Reactions, post again | one `could not add the capture reaction` warning; the record still saves |

That last row is the one worth actually doing — it is the only proof the soft
failure works, and it takes ten seconds to toggle back.

- [x] **Step 6: Commit**

```bash
git add channels/discord_bot.py
git commit -m "feat: acknowledge a captured message with a reaction, not a post"
```

---

## Task 2: Tier 2 — a real card for `/log`

**Files:**
- Modify: `core/pipeline.py`
- Modify: `channels/discord_bot.py`
- Test: `tests/test_core.py` (add `test_respond_reports_thread_gaps`)

**Interfaces:**
- Produces, for Task 3 and for the channel:
  - `Ingested.gaps: frozenset[str]` — what the whole design thread still lacks
  - `pipeline._respond(entry: LoggedEntry) -> Ingested`
  - `pipeline._question_for(entry: LoggedEntry, thread: list[LoggedEntry]) -> Optional[str]`
    — **signature change**: it no longer loads the thread itself
  - `channels.discord_bot._card(result: Ingested) -> discord.Embed`

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py`:

```python
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
```

- [x] **Step 2: Run it and watch it fail**

```bash
uv run python -m tests.test_core
```

Expected: `ImportError: cannot import name '_respond' from 'core.pipeline'`.

- [x] **Step 3: Plumb the gaps through `core/pipeline.py`**

Extend `Ingested`:

```python
@dataclass
class Ingested:
    """What the channel needs to do next: nothing, or post one question."""

    entry: LoggedEntry
    question: Optional[str] = None
    # What the whole design thread still lacks — NOT this entry's
    # missing_fields. A thread whose problem, alternatives, rationale and
    # results are spread over three entries is complete; rendering per-entry
    # holes reports it as broken and sends the team chasing nothing (§10).
    gaps: frozenset[str] = frozenset()
```

Add `_respond` immediately after `Ingested`:

```python
async def _respond(entry: LoggedEntry) -> Ingested:
    """One load of the design thread, two answers out of it.

    The thread read is the expensive part and both callers need it — the
    question decision to avoid asking what the log already answers, and the
    receipt to show coverage. Loading it once here is why adding the receipt
    costs no extra query.
    """
    thread = await storage.list_thread(entry.channel, entry.record.component)
    return Ingested(
        entry=entry,
        question=await _question_for(entry, thread),
        gaps=frozenset(followup.thread_gaps(thread)),
    )
```

In `ingest`, replace

```python
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))
```

with

```python
    await storage.save(entry)
    return await _respond(entry)
```

In `handle_reply`, replace

```python
    await storage.save(entry)
    return Ingested(entry, await _question_for(entry))
```

with

```python
    await storage.save(entry)
    return await _respond(entry)
```

Finally change `_question_for` to take the thread instead of loading it. Its
signature becomes

```python
async def _question_for(
    entry: LoggedEntry, thread: list[LoggedEntry]
) -> Optional[str]:
```

and the line

```python
    thread = await storage.list_thread(entry.channel, entry.record.component)
    if not followup.open_gaps(entry.record, thread):
```

becomes

```python
    if not followup.open_gaps(entry.record, thread):
```

- [x] **Step 4: Fix the test the signature change breaks**

`test_author_question_gate` calls `_question_for(entry)` and patches
`storage.list_thread`. Update it to pass the thread directly — it is simpler
now, which is a small argument that the refactor was right:

```python
    async def scenario():
        with patch.object(storage, "count_open_followups", new=count_open):
            assert await _question_for(entry, [entry]) is None
```

and delete the now-unused `list_thread` stub and its `patch.object` from that
test.

- [x] **Step 5: Run the tests**

```bash
uv run python -m tests.test_core
```

Expected: everything `ok`, including `ok  test_respond_reports_thread_gaps`.

- [x] **Step 6: Render the card in `channels/discord_bot.py`**

Add near the top, after `CAPTURED`:

```python
# The four fields a design record is judged on, in reading order. Matches
# exporters/notebook.py's SECTIONS — the notebook and the receipt must never
# disagree about what "complete" means.
COVERAGE = [
    ("problem_statement", "Problem"),
    ("alternatives_considered", "Alternatives"),
    ("rationale", "Why"),
    ("test_evidence", "Results"),
]


def _card(result) -> discord.Embed:
    """The /log receipt, as a card.

    Coverage is the thread's, not this entry's: core already made that
    distinction in `result.gaps`, and this only renders it.
    """
    record = result.entry.record
    embed = discord.Embed(
        title=record.title,
        description=record.summary,
        colour=discord.Colour.orange() if result.gaps else discord.Colour.green(),
    )
    embed.add_field(name="Stage", value=record.stage.value)
    embed.add_field(name="Subteam", value=record.subteam.value)
    embed.add_field(name="Component", value=record.component or "—")
    embed.add_field(
        name="This design thread so far",
        value=" · ".join(
            f"{'✗' if name in result.gaps else '✓'} {label}"
            for name, label in COVERAGE
        ),
        inline=False,
    )
    return embed
```

Replace `LogModal.on_submit`'s tail. It currently reads

```python
        await interaction.followup.send(_receipt(result))
        await _say(result, interaction.channel.send)
```

Make it

```python
        if result is None:
            await interaction.followup.send(
                "Logged nothing — that didn't look like design work."
            )
            return
        await interaction.followup.send(embed=_card(result))
        await _say(result, interaction.channel.send)
```

The receipt still goes out **before** `_say`. That order is not cosmetic: a
failure inside `_say` used to leave the deferred interaction unresolved and the
person with no receipt at all, which a code review caught earlier. Do not swap
it back.

Delete the now-unused `_receipt` function.

- [ ] **Step 7: Live check**

Grant **Embed Links** (Server Settings → Roles → `doc.bot`) if Task 1 step 1
did not already. Restart the bot, then:

| Do | Expect |
|---|---|
| `/log` a recap with a rationale but no test data | a card, **public**, orange, with `✓ Why` and `✗ Results` |
| `/log` into a component the channel already has a full thread for | `✓` on fields *this* write-up never mentioned — that is the thread rule working |
| check the receipt | it is a normal message, not "only you can see this" (gotcha 7) |

- [x] **Step 8: Commit**

```bash
git add core/pipeline.py channels/discord_bot.py tests/test_core.py
git commit -m "feat: /log receipt shows the design thread's coverage as a card"
```

---

## Task 3: Tier 3 — `/status`

**Files:**
- Modify: `core/storage.py`
- Modify: `core/pipeline.py`
- Modify: `channels/discord_bot.py`
- Test: `tests/test_core.py` (add `test_status`)

**Interfaces:**
- Consumes: `core.progress.current`, `core.followup.thread_gaps`
- Produces:
  - `storage.list_entries(..., channel: Optional[str] = None)`
  - `pipeline.Status` — dataclass with `span: Optional[progress.Span]`,
    `gaps: frozenset[str]`, `entries: int`
  - `pipeline.status(*, channel: str, author: Optional[str]) -> Status`

- [x] **Step 1: Write the failing test**

Append to `tests/test_core.py`:

```python
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
```

- [x] **Step 2: Run it and watch it fail**

```bash
uv run python -m tests.test_core
```

Expected: `ImportError: cannot import name 'status' from 'core.pipeline'`.

- [x] **Step 3: Add the channel filter to `core/storage.py`**

In `list_entries`, add to the keyword arguments, after `until`:

```python
    channel: Optional[str] = None,
```

and add to the clause block, after the `until` clause:

```python
    if channel is not None:
        add("channel = ${n}", channel)
```

- [x] **Step 4: Add `Status` and `status()` to `core/pipeline.py`**

Import `progress` — the module already imports it for `_span_is_busy`, so no
import change is needed. Add next to `BUDGET_WINDOW`:

```python
# How far back /status looks. A season-long window would answer "what are you
# on right now" with something from October.
STATUS_WINDOW = timedelta(days=7)
```

Add after `Ingested`:

```python
@dataclass
class Status:
    """What one person is on right now, and how complete that thread is.

    Read-only and derived: this creates nothing, assigns nothing, and closes
    nothing. CLAUDE.md §3 rules out task management, and the line that keeps
    this on the right side of it is that a human can never edit any of it.
    """

    span: Optional["progress.Span"]
    gaps: frozenset[str] = frozenset()
    entries: int = 0
```

Add at the end of the file:

```python
async def status(*, channel: str, author: Optional[str]) -> Status:
    """Answer "what am I on, and what is this thread still missing".

    Costs one query and no model call — the span is arithmetic over rows that
    already exist (core/progress.py) and the gaps are the same rule the
    notebook's coverage table renders.
    """
    now = datetime.now(timezone.utc)
    entries = await storage.list_entries(since=now - STATUS_WINDOW, channel=channel)

    span = progress.current(entries, author=author, now=now)
    if span is None:
        return Status(None)

    # Gaps are the whole component thread's, including other people's entries:
    # "is this design line judge-ready" is a question about the line, not about
    # who typed which part of it.
    key = (span.component or "").strip().lower()
    thread = [
        e for e in entries
        if (e.record.component or "").strip().lower() == key
    ]
    return Status(span, frozenset(followup.thread_gaps(thread)), len(thread))
```

- [x] **Step 5: Run the tests**

```bash
uv run python -m tests.test_core
```

Expected: everything `ok`, including `ok  test_status`.

- [x] **Step 6: Add the command in `channels/discord_bot.py`**

Add the card renderer next to `_card`:

```python
def _status_card(result) -> discord.Embed:
    if result.span is None:
        return discord.Embed(
            title="Nothing on the go",
            description="No design work logged for you in the last week.",
            colour=discord.Colour.greyple(),
        )

    span = result.span
    embed = discord.Embed(
        title=span.component or "Unfiled",
        description=f"{result.entries} entr{'y' if result.entries == 1 else 'ies'} "
                    f"in this design thread",
        colour=discord.Colour.orange() if result.gaps else discord.Colour.green(),
    )
    # Discord renders <t:...> in each viewer's own timezone, which sidesteps
    # TEAM_TZ entirely for this surface (gotcha 8 is an export problem).
    embed.add_field(
        name="Active",
        value=f"<t:{int(span.started_at.timestamp())}:t>"
              f" – <t:{int(span.last_at.timestamp())}:t>",
        inline=False,
    )
    embed.add_field(
        name="Stages",
        value=" → ".join(dict.fromkeys(s.value for s in span.stages)),
        inline=False,
    )
    embed.add_field(
        name="Still missing",
        value=", ".join(label for name, label in COVERAGE if name in result.gaps)
              or "nothing — this thread is complete",
        inline=False,
    )
    return embed
```

Register the command inside `setup_hook`, next to `/log` and **before** the
`self.tree.sync()` block:

```python
        @self.tree.command(name="status", description="What you're working on right now")
        async def _status(interaction: discord.Interaction):
            # Ephemeral is right here and wrong for /log. Gotcha 7 makes a /log
            # receipt public because the follow-up round trip is keyed on
            # replying to it; a status card starts no round trip, and posting
            # one person's summary to the whole channel is pure noise.
            await interaction.response.defer(thinking=True, ephemeral=True)
            result = await pipeline.status(
                channel="discord", author=interaction.user.display_name
            )
            await interaction.followup.send(embed=_status_card(result), ephemeral=True)
```

- [ ] **Step 7: Live check**

Restart the bot. `DISCORD_GUILD_ID` is set, so the new command appears
immediately.

| Do | Expect |
|---|---|
| `/status` after logging some intake work | a card naming `intake`, only visible to you |
| `/status` from an account with nothing logged | "Nothing on the go", no crash |
| `/status` right after `/log` | the same coverage marks the `/log` card showed |

- [x] **Step 8: Commit**

```bash
git add core/storage.py core/pipeline.py channels/discord_bot.py tests/test_core.py
git commit -m "feat: /status answers what you are on without posting to the channel"
```

---

## Task 4: docs

- [x] **Step 1: Update `CLAUDE.md` §10**

In "Working and verified end to end", add:

```markdown
- **Capture receipts** — a 📓 reaction acknowledges every captured message and
  every merged reply without posting; `/log` returns an embed card showing the
  *thread's* coverage; `/status` answers "what am I on" ephemerally. No tier
  adds a message the bot did not already send.
```

- [x] **Step 2: Update `docs/running-the-bot.md`**

In §2 step 4, change the permission list to add **Add Reactions** and
**Embed Links**, and the permission integer to `85056`.

In §7, add the reaction to each expectation — test (a) should now say a 📓
appears, and test (d) should say chitchat gets *no* reaction. Add `/status` as
test (f).

In §9's success checklist, add:

```markdown
- [ ] a captured message got a 📓 and chitchat did not
- [ ] `/status` returned a card only you could see
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/running-the-bot.md
git commit -m "docs: record the three receipt tiers"
```

---

## Self-review notes

- Tier 1 → Task 1, Tier 2 → Task 2, Tier 3 → Task 3. All three covered.
- No `discord` import appears in any `core/` snippet above; no `core.storage`
  import appears in any `channels/` snippet. Layering holds.
- `COVERAGE` in `channels/` mirrors `SECTIONS` in `exporters/notebook.py`. They
  are duplicated rather than shared because the exporter must not import a
  channel and the channel must not import an exporter; the names differ
  ("Alternatives" vs "Alternatives considered") because an embed field is
  narrower than a markdown heading. If a fifth patchable field is ever added,
  both lists need it — and so does `core.followup.PATCHABLE_FIELDS`, which is
  the one that actually decides.
- Type consistency: `Ingested.gaps` and `Status.gaps` are both
  `frozenset[str]`; `_card` and `_status_card` both read `result.gaps` with
  `name in result.gaps`; `_question_for`'s new two-argument signature is
  updated at both call sites (`_respond`) and in the one test that calls it.
- `_respond` is used by both `ingest` and `handle_reply`, so the reply path
  gets `gaps` too — unused today, but Tier 1's reaction is what that path
  renders, and leaving the field unpopulated there would be a trap.

---

## Still not done after this plan

- The bot still says nothing when it captures something and has no question —
  by design. The reaction is the whole acknowledgement.
- No digest at the end of a session. `core/progress.py` knows when a span
  closes, but acting on that instant needs a timer, and
  `docs/design/progress-tracker.md` §6 argues against one until lazy closing is
  proven insufficient.
- `tests/samples.py` and `tests/conversations.py` are still invented; nothing
  here changes what the model does, so no scoring run is warranted.
