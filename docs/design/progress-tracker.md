# Progress tracker — design

**Status:** implemented. `core/progress.py` plus the two consumers named in §9
are in the tree; the open questions in §12 are still open. Built by
`docs/superpowers/plans/2026-09-01-progress-tracker.md`, which corrects two
details of this document — no span-specific storage read was needed, and the
follow-up gate carries no scoring harness because it involves no model
behaviour. A later review added author scoping to the existing open-follow-up
count; no table or span persistence was added.

**One sentence:** give the agent a per-person, time-ordered view of what the
team is working on — derived entirely from entries it has already captured, at
zero additional model cost.

---

## 1. The question this answers

Today every `LoggedEntry` is a point in time. The agent can say "this message
was about the intake" but not:

> Who is working on what right now, when did they start, and have they stopped?

That is the whole feature. A **span** is the unit: one person, one component,
a start, and an end.

---

## 2. What this is NOT

CLAUDE.md §3 puts **task management / kanban** out of scope. This design stays
on the right side of that line, and the line is worth writing down because the
two features look similar from a distance:

| In scope (this doc) | Out of scope (still §3's kanban) |
|---|---|
| Inferred from messages already captured | Tasks created by a command |
| Read-only — nobody can edit a span | Assignment, owners, due dates |
| A span ends because the talking stopped | A span ends because someone clicked "done" |
| Zero new LLM calls | A model that names, plans, or prioritises tasks |
| A derived view, never stored | A `tasks` table that can disagree with reality |

If `/task assign` ever gets proposed, that is the kanban §3 rules out. It needs
its own conversation, not this doc.

---

## 3. Why build it — in priority order

Only the first one justifies building this now. The rest are enabled, not
promised.

1. **Follow-up timing.** The product's largest risk is the bot getting muted
   (§7, §8). Today the ask/stay-silent decision knows the record, the design
   thread, and a channel-wide count budget — but it does not know whether the
   person is mid-flow. A span supplies a budget that means something: **at most
   one question per task**. An author-scoped open-question check complements it
   so overlapping component spans cannot give one person several questions at
   once.
2. **The notebook's timeline.** "Tuesday 7:12–9:03pm · intake · problem →
   build → test" is what a notebook page actually is. §1 claims the notebook
   falls out of good process capture; a task timeline is the missing half.
3. **A future "what's the team on?" read.** Enabled by this. Not built here.

---

## 4. The core idea

> **Segmentation is arithmetic. Naming is judgment. Only the arithmetic is
> needed.**

The tempting design asks the model "what task is this?" That costs a call per
message, produces unstable labels that drift across synonyms ("intake fix" /
"fixing intake" / "intake"), and violates §7's rule that facts the channel
knows never go near the model — a start time is a fact, exactly like a
timestamp.

Everything a span needs is already in the entry: `author`, `created_at`, and
`record.component`. Grouping them is pure arithmetic over data on hand. The
span therefore needs **no new model call, no new table, and no background
job.**

The label problem disappears too: a span is *named by its component*, which
the model already extracts and which `storage.py` already folds for grouping.

---

## 5. The shape

```python
@dataclass(frozen=True)
class Span:
    author: str
    component: Optional[str]      # None = unfiled; treat as weak evidence
    started_at: datetime          # first entry's created_at
    last_at: datetime             # last entry's created_at — never "now"
    ended_at: Optional[datetime]  # == last_at once closed; None while open
    entry_ids: tuple[str, ...]
    stages: tuple[Stage, ...]     # the arc, in the order it happened
```

A dataclass, not a `BaseModel`: a span is never persisted, never serialised to
the model, and never comes back from the DB. It matches the existing
`pipeline.Ingested` and `tests/samples.Sample` precedent.

**`ended_at` vs `last_at` is the subtle part.** A span ends at its *last
observed activity*, not at the moment somebody noticed it was over. Setting
`ended_at = now` would inflate every task by a full idle timeout. `last_at` is
always the honest last-seen; `ended_at is None` is the "still live" predicate.

---

## 6. Segmentation rules

Key: `(channel, author, component_key)` where

```python
component_key = (record.component or "").strip().lower()
```

— **the identical fold `core/storage.py` already applies** to its
`component_key` generated column, so a span and a design thread can never
disagree about whether "Intake" and "intake" are the same thing.

Given entries sorted by `created_at` ascending, an `idle` timedelta, and an
explicit `now`:

1. Drop `stage == unknown` entries first. Chitchat must not extend a task.
2. For each key, walk its entries in order.
3. **Open** a span at entry `E` when the key has no current span, or when
   `E.created_at - current.last_at > idle`.
4. **Extend** otherwise: `last_at = E.created_at`, append the id and stage.
5. After the walk, a span is **closed** when `now - last_at > idle`, giving
   `ended_at = last_at`. Otherwise `ended_at = None`.

`spans(entries, *, now, idle)` is a pure function of its three arguments — no
`datetime.now()` inside, no DB, no I/O. Deterministic and testable.

### Spans may overlap, and that is correct

A component switch does **not** close the previous span. Consider:

```
7:10  "intake is jamming again"
7:12  "also the slide is sticking"
7:30  "ok intake's fixed, the belt was slipping"
```

Closing the intake span at 7:12 would be wrong — it is plainly still running
at 7:30. People genuinely juggle two things. So overlapping spans are allowed,
and the read API is honest about it: `current(author)` returns the **most
recently active** open span, not "the" span.

### Closing is lazy, not scheduled

There is no sweeper, no timer, and no cron. Closure is computed at read time
from timestamps. The cost of that choice: the agent cannot act at the *instant*
a task ends — it acts on the next message instead.

That turns out to be a feature. The question arrives as:

> "before you move on — why the dual roller over the wider one?"

...at the moment the person starts talking about something else, which is a
natural place for it and reads like a teammate. A timer would be needed only if
that proves insufficient, and it is not needed to find out.

---

## 7. What a span does NOT measure

**A span is a talk window, not hours worked.** Someone can machine a part for
two hours in silence and send one message about it. That span will read as
zero minutes long.

Consequences that must be respected by every consumer:

- Render spans as **"active between 7:12pm and 9:03pm"**, never "spent 1h51m".
- **Never sum durations.** A "total hours on the intake" number built from
  spans would be confidently, unfixably wrong, and it is exactly the kind of
  number that ends up in front of a judge.
- A one-entry span is normal, not a bug.

This limitation is inherent to inferring from chat and is not worth engineering
around. It only needs to be stated so nobody builds a timesheet on top of it.

---

## 8. Where it lives

| File | Change | Size |
|---|---|---|
| `core/progress.py` | **Create.** `Span`, `spans()`, `current()` | ~60 lines |
| `core/storage.py` | Optional author scope on `count_open_followups` | ~5 lines |
| `core/pipeline.py` | Per-span and per-author follow-up gates (phase 2) | ~20 lines |
| `exporters/notebook.py` | A session timeline section (phase 3) | ~30 lines |
| `tests/test_core.py` | `test_spans()` — pure, no DB, no API | ~40 lines |

Layering holds: `core/progress.py` imports only `schema`, and takes
`list[LoggedEntry]` from its caller. It does **not** read storage — the same
rule `exporters/notebook.py` follows, and for the same reason (§11): "export
only this channel" then becomes a caller-side query instead of a change here.

No span-specific storage read exists. `pipeline._question_for` reuses its
channel- and component-scoped `list_thread` rows for the span gate. The
per-author gate adds an optional author condition to the existing
`count_open_followups` query instead of introducing a second read path.

---

## 9. Consumers

### Phase 2 — the follow-up gate (the reason to build this)

`pipeline._question_for` gains two breadth gates:

1. If any other entry in the asker's current span carries a follow-up, stay
   silent. The current entry is excluded so productive multi-round depth still
   works.
2. If the same author already has an unanswered question in the channel, stay
   silent even when the new entry belongs to another component span.

These are stricter and more meaningful than the channel-wide count alone:
"one question per task" limits repeated interruption, while "one unanswered
question per person" covers people working across overlapping components. They
do *not* replace the count budget yet; measure real channel history before
relaxing the older gate.

No scoring harness measures these deterministic vetoes: the existing
conversation fixtures contain one entry per span and would report a perfect
number by construction. Whether the gates are too quiet needs real channel
history, not more invented model calls.

### Phase 3 — the notebook timeline

A per-day section lists spans in time order, with the stage arc per span. A
cross-midnight window includes the ending date so `23:40–00:20` never looks
backwards. A span whose arc is `build, build, build` with no `problem` is a
visible gap at exactly the granularity a judge reads — narrower than
`thread_gaps`, which spans the whole season.

### Enabled, not built

- A `/status` command: "what's the team working on." Trivial once `current()`
  exists; deliberately not in this design, because §3's success metric is
  input friction, and a read command does nothing for it.

---

## 10. Configuration

| Knob | Default | Status |
|---|---|---|
| `TASK_IDLE_MINUTES` | 60 | **Unmeasured guess** |

One knob on purpose. A separate "session" concept was considered and dropped:
the notebook already groups by date in the team's own timezone (gotcha 8), so a
channel-wide session boundary would duplicate it.

**How to tune it, when there is real history:** run the segmenter over a month
of the team's actual channel and check whether the spans match what people
remember doing that month. Too small and one task fragments into five spans;
too large and a whole meeting collapses into one. Do not tune it against
invented data — that is the same trap `notes.md` already documents for
`samples.py`.

---

## 11. Tests

All pure. No API, no DB, no framework — same bar as the rest of
`tests/test_core.py`.

| Case | Expected |
|---|---|
| Two entries, same author + component, 10 min apart | One span |
| Same, 3 h apart | Two spans |
| Author switches component mid-thread | Two overlapping spans, first not truncated |
| Two authors interleaved | Two independent spans |
| Last entry recent relative to `now` | `ended_at is None` |
| Last entry long before `now` | `ended_at == last_at`, not `now` |
| A `stage=unknown` message between two entries | Does not extend the span |
| `component=None` entries | Own bucket; do not merge into a named component |

---

## 12. Open questions

Recorded rather than guessed at:

1. **The idle default is unmeasured.** 60 minutes is a starting point, not a
   finding. §10 above says how to settle it.
2. **Author identity is a display-name string.** That is what `LoggedEntry`
   carries and what keeps `core/` ignorant of Discord. Two people sharing a
   display name would merge into one person's spans. Acceptable for a
   15-person team; revisit only if it actually happens.
3. **Should a `reflection` entry close a span early?** "that fixed it" is a
   plausible end-of-task signal. Unproven — do not build it until real history
   shows spans visibly running past their real end.
4. **Do the per-span and per-author gates make the global count budget dead
   weight?** Probably. Measure before deleting.

---

## 13. Deliberately not doing

- No `spans` table. A derived view cannot drift out of sync with the entries it
  derives from; a stored one can. Same argument `core/storage.py` already makes
  for its generated columns.
- No model-written span summary. The component name plus the stage arc is
  enough, and a real summary already has a home: a `reflection` entry, which
  the product captures today.
- No background sweeper. See §6.
- No duration arithmetic. See §7.
