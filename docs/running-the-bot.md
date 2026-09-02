# Running the bot in a real Discord server

The first live run. Everything in `CLAUDE.md` §10 is verified except the parts
that can only fail at runtime — this walks those.

**Do the whole thing in a throwaway server you own, not the team's channel.**
The bot posts publicly, and the first run is where you find out whether it
posts too much.

---

## 0. What you have to do yourself

Three things nothing in this repo can do for you:

- create a Discord application and copy its bot token
- turn on the **Message Content Intent** (step 2 — without it the bot reads
  every message as an empty string and does nothing, silently)
- decide which channel it listens in

Everything else is copy-paste.

---

## 1. Database

The bot calls `storage.init_schema()` on startup, which needs `CREATE
EXTENSION vector`. The pgvector image ships it:

```bash
docker run -d --name ftc-pg \
  -e POSTGRES_PASSWORD=ftc \
  -e POSTGRES_DB=ftcagent \
  -p 5432:5432 \
  pgvector/pgvector:pg17
```

Check it came up:

```bash
docker exec ftc-pg pg_isready -U postgres
```

Expected: `accepting connections`.

The schema creates itself on first boot. To wipe and start over between runs:

```bash
docker exec ftc-pg psql -U postgres -d ftcagent -c "DROP TABLE IF EXISTS entries;"
```

---

## 2. Discord application

1. <https://discord.com/developers/applications> → **New Application**
2. **Bot** tab → **Reset Token** → copy it. This is the `DISCORD_TOKEN`.
   Treat it like a password; it goes in `.env`, which is gitignored.
3. Still on the **Bot** tab, scroll to **Privileged Gateway Intents** and turn
   on **MESSAGE CONTENT INTENT**. Save.

   > This is the one that silently breaks everything. Without it `on_message`
   > fires with `message.content == ""`, the guard on line 84 drops it, and the
   > bot looks alive and does nothing. `CLAUDE.md` §10 lists it first for a
   > reason.

4. **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: **View Channels**, **Send Messages**,
     **Read Message History**, **Add Reactions**, **Embed Links**

   The resulting permissions integer is `85056`.

   `Read Message History` is not optional — the reply path resolves
   `message.reference`, and `message.reply()` needs it.

5. Open the generated URL, invite the bot to your test server.

6. Get the channel id: Discord **Settings → Advanced → Developer Mode** on,
   then right-click the channel → **Copy Channel ID**.

---

## 3. `.env`

Add to the existing `.env` (which currently has only the three `LLM_*` keys):

```bash
DATABASE_URL=postgresql://postgres:ftc@localhost:5432/ftcagent
DISCORD_TOKEN=<the token from step 2>
DISCORD_CHANNELS=<the channel id from step 2>
TEAM_TZ=America/Los_Angeles
```

**Set `DISCORD_CHANNELS`.** Empty means every channel the bot can see, and
every burst in every channel is an LLM call.

Optional, all have working defaults — see `.env.example` for the full list:

| Var | Default | What it does |
|---|---|---|
| `BURST_QUIET_SECONDS` | 45 | how long one person must go quiet before their messages are parsed as one unit |
| `BURST_MAX_ITEMS` | 10 | flush a monologue early at this many messages |
| `FOLLOWUP_MAX_ROUNDS` | 3 | hard ceiling on follow-up rounds per record |
| `MAX_OPEN_QUESTIONS` | 2 | unanswered questions allowed in one channel at once |
| `TASK_IDLE_MINUTES` | 60 | quiet gap that ends a work span (`core/progress.py`) |

> `TASK_IDLE_MINUTES` is missing from `.env.example` — the progress-tracker
> work added the knob and never listed it. The default applies either way.

---

## 4. Verify before touching Discord

Cheapest failures first.

```bash
uv run python -m tests.test_core
```

Expected: 15 lines of `ok`. Three tracebacks scroll past on the way —
`downstream exploded` and `embedding API is down` are deliberately raised by
tests that check those failures are handled. Only the `ok` lines matter.

```bash
uv run python -m scripts.Smoke
```

Expected: two parsed records printed. This proves the DeepSeek key works and
the ambient + reply paths run. **It costs two API calls.** If it 401s, re-read
`CLAUDE.md` §6 gotchas 2 and 5 before touching anything else — the cause is
almost never the key itself.

---

## 5. Slash-command sync

`setup_hook` calls `self.tree.sync()`, which is a **global** sync. Discord can
take up to an hour to show a globally-synced command. For a test run that is
unusable.

Before the first run, edit `channels/discord_bot.py:76` from

```python
        await self.tree.sync()
```

to

```python
        guild = discord.Object(id=<your test server id>)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
```

Guild-scoped syncs are instant. Revert it before the bot goes near the real
team server. (Server id: right-click the server icon → Copy Server ID.)

If you would rather not edit code, skip `/log` on this run and test the ambient
and reply paths only — they need no sync.

---

## 6. Run it

```bash
uv run python -m channels.discord_bot
```

Expected on startup: discord.py's own `logging in`/`connected` lines, then
nothing. Nothing is correct. **Leave this terminal visible** — `on_message`
swallows exceptions and logs them (`dropping message <id>`), so a bug shows up
here and nowhere else. Silence in the channel is not evidence of success
(`CLAUDE.md` §10, item 5).

Stop with Ctrl-C. `Bot.close()` drains any buffered burst first, so the last
thing said before you quit still gets parsed.

---

## 7. What to actually type in the channel

Six paths. Do them in this order.

### a. One design message → one record

```
intake keeps jamming when two blocks come in at once
```

**Then wait 45 seconds.** Nothing happens before that — the burst coalescer is
holding the message to see whether you are still typing. This is the single
most likely thing to make you think the bot is broken.

Expect: a 📓 reaction, then either a reply asking one question or silence. Both
are valid; the model is allowed to stay quiet and `_question_for` has seven
separate vetoes.

### b. Four messages → still one record

```
ok so the slide
it was flexing at full extension
added a third stage support
should be stiffer now
```

Send them within a few seconds of each other, then wait 45s. Expect one 📓 on
the last message and **one** reply at most, not four. Afterwards there should
be exactly one new row in the database, with all four lines joined in
`raw_text`.

### c. Reply to the bot's question

Use Discord's actual **Reply** function on the bot's message, not a fresh
message that starts with `@bot`. The whole round trip is keyed on
`message.reference`.

Expect: a 📓 appears on the reply and the answer merges into the existing
record — no new row appears. It may ask one more question (up to
`FOLLOWUP_MAX_ROUNDS`), or go quiet.

### d. Chitchat → complete silence, and no API call

```
who's driving tomorrow
```

Expect: no reaction and no reply, ever. This one is free — `core/triage.py`
filters it before any model call. **This is the gate that matters most.** If
the bot answers chitchat in a real team channel it gets muted within a week.

### e. `/log` (only if you did step 5)

Run `/log`, type a recap into the modal, submit.

Expect: a **public** embed card showing the design thread's coverage, not an
ephemeral "only you can see this". Ephemeral messages cannot be replied to,
which would strand every follow-up (`CLAUDE.md` §6 gotcha 7). If the receipt is
ephemeral, that is a bug — stop and report it.

### f. `/status`

Run `/status` after logging some work, then run it from an account with no work
in the last week.

Expect: the first card names the current component and shows the same thread
coverage as `/log`; the second says "Nothing on the go". Both are visible only
to the person who ran the command.

---

## 8. Look at what it captured

Two renderers read the database. Start with the board — it is one screen, and
it tells you immediately whether `author` tags and `component` came out usable:

```bash
uv run python -m scripts.kanban > board.md
uv run python -m scripts.export > notebook.md
```

`board.md` is a markdown table; open it in an editor with a markdown preview,
or paste it into any GitHub comment box. In a terminal the pipes do not line up
and it is much harder to read than it looks.

On the board, check:

1. **Lanes are team roles, not people.** Everything under `No tag` means those
   members have no role whose name starts with a number ("5898 Andromeda").
   That is a server-side fix — give them the role — not a code one, and it only
   applies from the next message on: roles are captured per entry, never
   backfilled.
2. **Cards sit in the stage you expect.** A card's column is its span's *last*
   stage.
3. **`●` vs `○` matches what actually happened.** Everything from tonight's
   session showing `○` means `TASK_IDLE_MINUTES` is too small — this is the
   first real chance to measure it, so write the number down.

Then read the table directly:

```bash
docker exec ftc-pg psql -U postgres -d ftcagent -c "
SELECT created_at, author, source,
       record->>'stage'     AS stage,
       record->>'component' AS component,
       record->>'title'     AS title,
       jsonb_array_length(followups) AS rounds
FROM entries ORDER BY created_at;"
```

What to check, in order of how much it tells you:

1. **One row per burst**, not one per message. If test (b) made four rows, the
   coalescer is not doing its job.
2. **No row for the chitchat.** If there is one, triage let it through and it
   cost an API call.
3. `stage` and `component` look right. These drive the notebook's grouping;
   everything downstream is built on them.
4. `rounds` matches how many questions you actually saw.

To read the full record of one entry:

```bash
docker exec ftc-pg psql -U postgres -d ftcagent -c \
  "SELECT jsonb_pretty(record) FROM entries ORDER BY created_at DESC LIMIT 1;"
```

---

## 9. What a successful first run looks like

Not "the records were accurate" — that needs the scoring loop and real
messages, and `notes.md` is clear that no trustworthy baseline exists yet.

For this run, success is narrower:

- [ ] the bot connected and stayed up
- [ ] a message produced exactly one row
- [ ] a four-message burst produced exactly one row
- [ ] chitchat produced no row and no reply
- [ ] a Discord **Reply** merged into the existing row instead of making a new one
- [ ] `/log`'s receipt was public
- [ ] a captured message got a 📓 and chitchat did not
- [ ] `/status` returned a card only you could see
- [ ] the terminal shows no `dropping message` lines

Anything failing above is a runtime bug in `channels/discord_bot.py`, which has
never run before this. Anything about *what the model said* is a prompt
question — write it in `notes.md`, do not fix it during the run.

---

## 10. When it misbehaves

| Symptom | Cause |
|---|---|
| Bot online, ignores everything | Message Content Intent off (step 2.3) |
| Bot ignores one channel | `DISCORD_CHANNELS` has the wrong id |
| Nothing happens for 45s | Working as designed — the burst window |
| `/log` not in the command list | Global sync pending; do step 5 |
| Replying to the bot creates a new record | Not a real Discord Reply, or the question's `message_id` was never saved — check for a `dropping message` line at the moment it asked |
| `401` from DeepSeek | `CLAUDE.md` §6 gotchas 2 and 5. Not the key. |
| `CREATE EXTENSION` denied | Not the pgvector image, or not the `postgres` superuser |
| Bot never asks anything | Possibly correct. Seven vetoes in `_question_for`; `MAX_OPEN_QUESTIONS=2` and the per-author gate both suppress in a quiet test server. |
| Bot asks about the same thing twice | Real bug — the span gate should stop it. Capture both messages. |

---

## 11. After the run

Write what happened in `notes.md`. Not scores — the samples are still invented
and §9's discipline says a score against invented text measures imagination.
Write the runtime facts: what broke, what the 45-second wait felt like, whether
a question landed at a moment that felt natural or intrusive.

That last one is the only thing this run can measure that nothing else can, and
it is the metric the product actually lives or dies on.
