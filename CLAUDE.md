# CLAUDE.md — DiscordFTCAgent

Project context for Claude Code. Read this before making changes.

---

## 1. What this is

An agent that captures an FTC robotics team's **engineering design process in
real time**, as it happens in their Discord, and turns scattered messages into
structured design records.

**The key framing — do not lose this:**

> This is a *design-process companion*, not a notebook generator.
> The engineering notebook is one **export format**, not the product.

Why it matters: a "notebook generator" is used once at the end of the season
(zero retention, dies with the deadline). A design-process companion is used at
every meeting. Teams cram their notebook the night before not because they're
lazy, but because **the process was never captured while it happened**. That is
the actual problem being solved.

**Corollary:** if the process is captured well, the notebook falls out for free.

---

## 2. Product goals

- Ship something real that a team actually uses — not a competition demo.
- First market: FTC teams (author is an FTC CV lead — insider access and
  distribution via r/FTC and team Discords).
- Later expansion (architecture should allow, MVP must not build): FRC/VEX,
  university capstone teams, Formula SAE.

**Success metric for v1:** the author's own team keeps dropping messages into
the channel for 4 consecutive weeks. Not accuracy, not feature count. If input
friction is too high, nothing else matters.

---

## 3. MVP scope

### In scope — three ways in, one record out

```
ambient   any message in the channel   ┐
/log      a deliberate write-up        ├→ classify into design-cycle stage
                                       │  → extract structured record
                                       │  → detect what's missing
                                       │  → ask a follow-up (or stay silent)
                                       │  → persist
reply     an answer to the bot's       ┘
          question                     → merge back, then maybe ask the next gap

                                       → export to notebook markdown
```

The three entry points are `core/agent.py`'s three public functions:
`parse_design_record` / `log_session` / `apply_followup_answer`. They share one
model and one schema and differ only in prompt — see §8.

The reply path is not optional polish. Without it the bot asks a good question,
someone answers it, the answer is parsed as a fresh junk record, and the hole it
was meant to close stays open. Every step works and the result is zero.

### Explicitly OUT of scope for v1

Do not build these unless asked directly:

- CAD integration
- Task management / kanban
- Parts inventory
- Match/competition data analysis
- Multi-team permissions or auth systems
- Web frontend
- Image understanding (see §6 gotcha 4)
- RAG over the FTC game manual — still out. Note this is *not* the same as the
  vector search in `core/storage.py`, which is over the team's own records
  ("did we ever try compliant wheels?") and is in.
- PDF export — markdown only for now; PDF needs a dependency
- Any agent framework beyond pydantic-ai (no LangGraph, no orchestration layer)

---

## 4. Architecture — the one rule that matters

> **`core/` does not know Discord exists, and does not know DeepSeek exists.**

Decision test for where code goes:

- Does this code know where the message came from? → `channels/`
- Does it not care? → `core/`

The Discord callback is allowed to do exactly two things: extract text, and send
a reply back. Every judgment call lives in `core/`. If you find yourself writing
an `if` inside `channels/discord_bot.py`, that logic probably belongs in `core/`.

This is what makes adding a web channel later a zero-change operation on `core/`.

### File structure

`(empty)` and `(todo)` mark what does not exist yet — the shape is the plan.

```
DiscordFTCAgent/
├── core/                      # the heart — channel-agnostic
│   ├── schema.py              # the contract: DesignRecord / LoggedEntry / FollowupPatch
│   ├── agent.py               # the brain: three entry points, one model
│   ├── triage.py              # is this worth a call at all?
│   ├── inbox.py               # burst coalescing
│   ├── followup.py            # merge gate + multi-round stop policy
│   ├── pipeline.py            # ingest policy — the only caller of all of core
│   ├── storage.py             # memory: postgres + pgvector
│   └── prompts/               # kept out of .py on purpose — see §8
│       ├── design_entry.md    # ambient
│       ├── session_log.md     # /log
│       └── followup_merge.md  # reply
│
├── channels/                  # entry points from the outside world
│   └── discord_bot.py         # ears and mouth only
│
├── exporters/                 # outputs — notebook is only the first one
│   └── notebook.py            # list[LoggedEntry] -> markdown, pure
│
├── tests/
│   ├── test_core.py           # pure-logic checks, no API, no DB
│   ├── samples.py             # 15 invented scoring messages — replace with real ones
│   └── conversations.py       # multi-message, multi-round fixtures
│
├── scripts/
│   ├── Smoke.py               # connectivity check: ambient + reply paths
│   ├── try_parse.py           # scoring harness over samples.py
│   ├── try_conversation.py    # rounds + nags
│   └── export.py              # (todo) storage -> notebook, ten lines
│
├── data/                      # gitignored
├── .env / .env.example
└── pyproject.toml             # uv-managed
```

`exporters/notebook.py` does **not** read storage. It takes the entries it is
given. The DB read belongs in `scripts/export.py`, for the same reason `core/`
does not know about Discord: "export only the intake thread" then becomes a
caller-side query rather than a change to the exporter.

`prompts/` is a separate directory because prompts get edited hundreds of times.
Keeping them as `.md` means git diffs are readable and no restart is needed.

`exporters/` exists as a directory even with one file in it, because the folder
structure should express the product's shape, not just today's code.

---

## 5. Tech stack (settled — do not re-litigate)

| Layer | Choice | Note |
|---|---|---|
| Language | Python 3.13 | |
| Package manager | uv | |
| LLM framework | pydantic-ai | Not LangGraph. This is a workflow, not a multi-step autonomous agent. |
| Model | DeepSeek (`deepseek-chat`) | Cost. Swappable — only `core/agent.py` knows. |
| Channel | discord.py | Capture where the team already lives |
| Storage | PostgreSQL + pgvector | Only `core/storage.py` knows. Supabase-compatible. |
| Embeddings | OpenAI `text-embedding-3-small` | **Optional.** DeepSeek has no embeddings endpoint — gotcha 6. |
| Driver | asyncpg | The one dependency added since the stack was settled |

### Correct model wiring

```python
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

_model = OpenAIChatModel(
    os.getenv("LLM_MODEL", "deepseek-chat"),
    provider=DeepSeekProvider(api_key=os.environ["LLM_API_KEY"]),
)
```

The env var is `LLM_API_KEY`, not `DEEPSEEK_API_KEY` — the whole point of
gotcha 5 is that getting this name wrong surfaces as a 401 nowhere near the
mistake. All three agents in `core/agent.py` share this one model instance.

---

## 6. Gotchas already hit — do not repeat these

**1. `OpenAIResponsesModel` is wrong for DeepSeek.**
`OpenAIChatModel` is the class for OpenAI-compatible providers. The Responses
API path produced confusing failures.

**2. Never hand-write `base_url`.**
`base_url` is a *base*, not a full endpoint. Passing
`https://api.deepseek.com/v1/chat/completions` made the SDK append its own path,
producing a nonexistent URL that returned **401**, which looked like an auth
problem for an hour. Use `DeepSeekProvider` — it sets the base URL itself.

**3. DeepSeek thinking mode conflicts with the default output mode.**
Error: `Thinking mode does not support this tool_choice`.
Cause: pydantic-ai's default **Tool Output** mode forces a tool call via
`tool_choice`; DeepSeek's thinking models reject that.
Fixes, in order of preference:
  a. use `deepseek-chat` (no thinking mode) — current choice
  b. `output_type=NativeOutput(DesignRecord)`
  c. `output_type=PromptedOutput(DesignRecord)` — most universal, slightly less reliable
  d. disable thinking via `openai_reasoning_effort`

**4. ⚠️ Images silently do not work through pydantic-ai's DeepSeek path.**
Image and document inputs are **replaced with placeholder text rather than
rejected** — no error is raised. The model sees nothing and confabulates. This
is why image handling is out of scope for v1. Do not add image input without
first verifying end to end that the model actually received the image.

**5. Use `os.environ[...]`, not `os.getenv(...)`, for the API key.**
A typo'd env var name with `getenv` returns `None` and surfaces as a 401 far
from the actual mistake.

**6. DeepSeek has no embeddings endpoint.**
Vector search needs a second vendor. That seam lives in `core/storage.py` and is
the only one outside `core/agent.py`. It fails soft: with no `EMBEDDING_API_KEY`
everything still works and `search()` returns `[]`. Persistence must never
depend on a vector store being reachable.

**7. ⚠️ Ephemeral Discord messages cannot be replied to.**
A slash command wants to answer ephemerally, but the whole follow-up round trip
is keyed on `message.reference` → `followup_message_id`. An ephemeral receipt
strands every answer. `/log` must post its receipt and its question as a normal
public message.

**8. Storing UTC and rendering UTC files evening work on the wrong day.**
FTC teams meet at night. 8pm Pacific is 03:00 the next day in UTC, and date is
what orders the notebook's threads. Store UTC, render in the team's zone —
`render_notebook(entries, tz=ZoneInfo(...))`, from `TEAM_TZ`.

---

## 7. The schema is the product's spine

`core/schema.py` defines `DesignRecord`. Design principles baked into it:

**Field descriptions ARE prompt.** Everything in `Field(description=...)` is
sent to the model. Write decision criteria, never tautologies.
`description="Category of the message"` is worthless.

**Three models, and the split between them is load-bearing:**

- `DesignRecord` — what the model produced. Judgments only.
- `LoggedEntry` — what actually happened: the record plus channel metadata
  (`created_at`, `author`, `channel_message_id`, `source`, follow-up state).
  Never shown to the model. A timestamp is a fact the channel knows, not
  something to extract — let the model near dates and it will invent them.
- `FollowupPatch` — what a reply adds and the next question it proposes.
  Deliberately *not* a `DesignRecord`, so the merge step cannot reach `stage`,
  `title` or `summary`; Python decides whether the proposed question is posted.

The merge gate is enforced in Python, not in the prompt: `apply_patch` writes
only fields in `PATCHABLE_FIELDS` **and** only those the record itself declared
missing. A casual reply can therefore never overwrite something the team already
said, however the model behaves.

**Two orthogonal axes, never merged:**
- `stage` — where in the design cycle (problem / ideation / decision / build /
  test / reflection / unknown). Drives the notebook export and gap detection.
- `subteam` — whose work it is (mechanical / software / electrical / drive /
  outreach / unknown). Drives filtering and routing.

Merging them (e.g. a single enum with `CAD`, `Code`, `Recap`, `Schedule`) makes
messages like "software team retro on odometry tuning" unclassifiable.

**Four field categories, in this order:**
1. Classification (`stage`, `subteam`) — first, so the model commits to a frame
2. Content (`title`, `summary`, `component`, `problem_statement`,
   `alternatives_considered`, `rationale`, `test_evidence`)
3. Self-assessment (`missing_fields`, `confidence`)
4. Action (`followup_question`) — **last**, so prior analysis is in context

**Hard rules:**
- Always keep an `UNKNOWN` escape hatch in every enum. Without it the model
  force-guesses and classifies "what time are we meeting?" as a design decision.
- Every uncertain field is `Optional[...] = None`. Required fields make the
  model fabricate — fatal here, since judges read this content.
- Lists use `default_factory=list`.
- Enums subclass `str`.
- `missing_fields` is restricted to four field names only, or follow-ups become
  nonsense ("what should this change be called?").
- `followup_question` must be explicitly permitted to return null. The model
  needs the *right to stay silent* or the bot gets muted by the team in a week.
- Follow-ups may run up to `FOLLOWUP_MAX_ROUNDS` rounds, and stop at the first
  of: nothing patchable left, a reply that did not answer, a round that filled
  nothing, the component thread already supplying the field, or the channel's
  open-question budget. Every gate is Python (`core/followup.py` and
  `core/pipeline.py`), not prompt.

---

## 8. Prompt design

Three prompts, one schema, one model. They differ because the author's *intent*
differs, which is not inferable from the text:

| Prompt | Path | What is different |
|---|---|---|
| `design_entry.md` | ambient | The baseline. Bias toward silence — nobody wrote this for the log. |
| `session_log.md` | `/log` | Do **not** take the latest stage (see below). Fill fields. Ask when something is missing. |
| `followup_merge.md` | reply | Report only what the reply adds. `answered: false` is a normal, frequent, correct outcome. |

The one that matters: `design_entry.md` says "if a message covers multiple
stages, take the latest." That is right for a fragment and wrong for a write-up
— a recap that walks problem → alternatives → decision → test gets stamped
`test`, and a full design cycle collapses into one point on the timeline.
Measured: the same recap yields `stage=test` on the ambient prompt and
`stage=decision` on `session_log.md`.

`design_entry.md` holds **cross-field global rules only**. Per-field
criteria belong in the schema descriptions, closer to where they apply.

Structure: Role / Task / Hard rules / Edge cases / Tone.

Non-negotiable rules already in it:
- **Never invent.** Null beats a plausible guess.
- **Never infer backwards.** A stated solution does not imply the problem.
  "Switched to a dual roller" does NOT license "the intake was jamming."
- Preserve the team's own numbers, units, part names, and jargon verbatim.
- Match the input's length.
- Follow-ups post publicly in a live channel — when in doubt, stay silent.

The tone section uses **good and bad example sentences, not adjectives**.
"Ask naturally" teaches the model nothing; four example questions do.

Every time the model misclassifies a recurring pattern, add a line to
**Edge cases** — that section grows from real failures, not from imagination.

Prompt language must match the team's actual Discord language. Do not mix.

---

## 9. Development workflow

The order matters and was chosen deliberately:

1. **`tests/samples.py` first** — the target is 15 real Discord messages with
   hand-written expected answers. The current fixture is invented and is only
   a smoke-level baseline; replace it before trusting scores. Samples come
   *before* the schema, so fields are derived from what messages actually
   contain rather than from imagination.
   Composition: 3 complete decisions, 4 result-without-reason, 3
   complaint-without-solution, 2 with test data, **3 pure chitchat**.
   The chitchat samples are the product's survival line.
2. `core/schema.py` — fields that appear ≥3 times across samples. Fewer fields
   beats more; eight always-null fields confuse the model and cost tokens.
3. `core/prompts/design_entry.md`
4. `scripts/Smoke.py` — one message, confirms the pipe is connected
5. `scripts/try_parse.py` — **the scoring loop, where ~80% of time goes**
6. `exporters/notebook.py` — **before Discord**, because seeing a real exported
   notebook reveals schema problems while they're still cheap to fix. It did,
   immediately: see §10.
7. `core/storage.py` — **after the exporter**, so the table is designed around
   the queries the exporter actually needs rather than the reverse
8. `channels/discord_bot.py` — last
9. Live in the team's real channel for a week

### The scoring loop

`try_parse.py` reports three **independent** metrics, because "70% correct"
gives no direction while three separate numbers do:

| Metric | Failure means | Fix location |
|---|---|---|
| Stage accuracy | classification criteria unclear | `Stage` enum description |
| Chitchat silence | model is over-eager | `followup_question` description + global rule |
| Missing detection | "missing" is ill-defined | `missing_fields` description |

**Discipline:**
- Change one thing per run.
- Log every version's score in `notes.md`.
- If the score drops, roll back — no "but this version is theoretically better."

**Ship gates:** chitchat silence must be **3/3 on the current fixture**
(non-negotiable). Stage accuracy ≥13/15. Follow-up quality is judged by reading
it aloud — it should sound like a teammate.

### The other tests

`python -m tests.test_core` — pure logic, no API, no DB, no framework. Covers
the merge and stop gates, per-thread gaps, the envelope, triage, coalescing, and
optional-embedding failure boundaries. Run it after touching `apply_patch`,
`exporters/`, or ingest policy. It says nothing about prompt quality — that is
what the scoring loops are for.

---

## 10. Current status

Working and verified locally:

- **`core/schema.py`** — `DesignRecord`, `LoggedEntry`, `FollowupPatch`, and the
  append-only `FollowupTurn` ledger.
- **`core/followup.py`** — `PATCHABLE_FIELDS`, merge gate, thread gaps, and the
  multi-round stop policy. Pure logic covered by `tests/test_core.py`.
- **`core/agent.py`** — all three entry points. API connectivity is fine; the
  §6 wiring works.
- **three prompts** in `core/prompts/`
- **`exporters/notebook.py`** — grouped by component, coverage table, gaps
  reported per thread. Pure function, ~175 lines.
- **`core/storage.py`** — postgres + pgvector. Tested against a real pgvector
  container: idempotent schema, upsert, follow-up lookup, redelivery dedup, all
  filters, vector search, and DB → notebook end to end.
- **`scripts/Smoke.py`** — runs the ambient and reply paths
- **`scripts/try_parse.py`** and **`scripts/try_conversation.py`** — scoring
  loops have been run against the invented fixtures; silence is 3/3 and nags 0.
- **`tests/test_core.py`** — offline logic checks. Green.
- **`.env.example`**

Written and checked offline, but **never run against real Discord**:

- **`core/pipeline.py`**, **`core/inbox.py`**, and **`core/triage.py`** — the
  ingest policy, burst coalescer, thread gate, and open-question budget. Their
  pieces have tests or real-DB checks, but timing and policy have not run under
  real Discord traffic.
- **`channels/discord_bot.py`** — `on_message` sends ambient bursts through the
  coalescer and routes replies to pipeline; `/log` sends deliberate text to the
  same pipeline. Imports and type-checks; everything about the live path —
  intents, command sync, modal timeout, reply resolution — remains untested.
- `DISCORD_CHANNELS` (env, comma-separated) limits which channels are parsed.
  Empty means every channel the bot can see, with at most one LLM call per
  ambient burst after triage.

Two findings worth keeping:

1. **The coverage table must compute gaps per *thread*, not per entry.**
   `missing_fields` is scoped to one message and exists to drive a follow-up.
   Judges read the thread. A design line whose problem, alternatives, rationale
   and results are spread across three entries is *complete* — summing per-entry
   gaps reported it as full of holes and would have sent the team chasing
   nothing. Found by looking at the first rendered notebook, exactly as §9
   predicted.
2. **The `/log` "ask more readily" rule is written but unproven.** On a recap
   missing its rationale, both prompts asked, in nearly the same words. Do not
   treat that inversion as verified until the scoring loop measures it with the
   two `source` values scored separately.

### Not done

- `tests/samples.py` and `tests/conversations.py` are invented. Replacing both
  with real team transcripts is still the blocker for a trustworthy baseline.
- `scripts/export.py` does not exist, so storage is not wired to notebook export.
- The bot has never connected to Discord

### Known defects

- `session_log.md` normalises the team's wording slightly ("adding compliant
  wheels" → "compliant wheels"), a mild violation of hard rule 3. Tunable, but
  not before real transcripts replace the invented baseline.

### First live run checklist

The seven constraints from the design are implemented; these are the ones that
can only fail at runtime:

1. Enable **Message Content Intent** in the Discord developer portal, or
   `on_message` sees empty strings
2. Set `DISCORD_CHANNELS` before pointing it at a busy server
3. `setup_hook` calls `storage.init_schema()` and `tree.sync()` — a global sync
   can take up to an hour to appear; use a guild-scoped sync while iterating
4. Confirm the `/log` receipt is **not** ephemeral and that replying to the
   bot's question actually lands in `find_by_open_followup`
5. `on_message` swallows and logs exceptions so one bad message cannot kill the
   bot — check the log after the first session rather than assuming silence
   means success
6. Tune the coalescer's 45-second quiet window from real conversation timing
7. Confirm the open-question budget of 2 is not too tight for a shared channel

## 11. Working agreements for Claude Code

- Do not add dependencies without asking. The stack is deliberately small.
  Added so far beyond §5: `asyncpg`.
- Do not expand scope. If a change touches anything in §3's out-of-scope list,
  stop and ask.
- Do not move logic into `channels/`.
- Do not make the schema "more complete." Fields are earned by appearing in real
  samples.
- When a prompt change is proposed, state which of the three metrics it targets.
- Do not let `channels/` or `exporters/` read from `core/storage.py`. Callers
  wire them together; the layers stay ignorant of each other.
- Enforce record-integrity rules in Python where you can, not in the prompt.
  `apply_patch` is the pattern.
- Prefer editing `prompts/*.md` over editing Python when the fix is behavioral.
