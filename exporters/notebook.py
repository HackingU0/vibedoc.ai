"""Render logged design records as an FTC engineering notebook (markdown).

A pure transform: `list[LoggedEntry] -> str`. It does not read storage, does
not call the model, does not touch Discord. The caller decides which entries to
hand it — that is what makes "export only the intake thread" or "export only
what software did" a caller-side query rather than a change in here.

Two decisions worth knowing before reading the code:

1. **Grouping is by component, not by date.** A date-ordered dump is a chat log
   with headings. Grouping by component turns the same records into a design
   thread — problem, what else was considered, what was chosen, what the test
   showed — which is the shape judges are actually reading for, and the reason
   `stage` exists as an axis at all.

2. **Gaps are printed, not hidden.** A notebook generator would quietly render
   an empty section. This prints "not recorded: rationale" and rolls the holes
   up into a coverage table at the top. Mid-season that table is the whole
   product: it tells you which design thread is thin while there is still time
   to fix it.
"""

from datetime import datetime, tzinfo

from core.followup import thread_gaps
from core.progress import spans
from core.schema import LoggedEntry, Stage

# Reading order of the design cycle — used to show a thread's arc compactly.
STAGE_ORDER = [
    Stage.PROBLEM,
    Stage.IDEATION,
    Stage.DECISION,
    Stage.BUILD,
    Stage.TEST,
    Stage.REFLECTION,
]

# Content fields, in the order a notebook entry reads best.
SECTIONS = [
    ("problem_statement", "Problem"),
    ("alternatives_considered", "Alternatives considered"),
    ("rationale", "Why"),
    ("test_evidence", "Results"),
]

UNFILED = "Unfiled"


def _date(dt: datetime, tz: tzinfo | None = None) -> str:
    """Format an entry date in the team's own timezone.

    Timestamps are stored in UTC, but the notebook is read as a record of
    meetings. A team meeting at 8pm Pacific is stored as 03:00 the next UTC day,
    so rendering raw UTC would file half the season's evening work under the
    wrong date — and date is what orders the threads. Pass the team's zone
    (`ZoneInfo("America/Los_Angeles")`) to fix that.
    """
    return (dt.astimezone(tz) if tz else dt).strftime("%b %d")


def _thread_key(entry: LoggedEntry) -> str:
    component = (entry.record.component or "").strip()
    return component.lower() if component else UNFILED.lower()


def _group(entries: list[LoggedEntry]) -> dict[str, list[LoggedEntry]]:
    """Component -> entries, oldest first. Threads ordered by when they started.

    The display name is taken from the first entry in the thread, so "Intake"
    and "intake" collapse into one thread without normalising the team's own
    capitalisation away.
    """
    threads: dict[str, list[LoggedEntry]] = {}
    names: dict[str, str] = {}

    for entry in sorted(entries, key=lambda e: e.created_at):
        key = _thread_key(entry)
        threads.setdefault(key, []).append(entry)
        names.setdefault(
            key, (entry.record.component or UNFILED).strip() or UNFILED
        )

    # Unfiled last — it is a loose-ends bin, not a design thread.
    ordered = sorted(threads, key=lambda k: (k == UNFILED.lower(), threads[k][0].created_at))
    return {names[k]: threads[k] for k in ordered}


def _arc(entries: list[LoggedEntry]) -> str:
    """The stages this thread actually reached, in design-cycle order."""
    seen = {e.record.stage for e in entries}
    reached = [s.value for s in STAGE_ORDER if s in seen]
    return " → ".join(reached) if reached else "—"


def _gaps(entries: list[LoggedEntry]) -> str:
    """What this thread never supplies anywhere.

    The rule lives in core.followup.thread_gaps because the follow-up policy
    needs exactly the same answer — a question about a field the thread already
    fills is the most annoying thing the bot can ask.
    """
    absent = thread_gaps(entries)
    labels = [label for name, label in SECTIONS if name in absent]
    return ", ".join(label.lower() for label in labels) if labels else "—"


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

    out = ["## Sessions", ""]
    day = None
    for span in runs:
        start = span.started_at.astimezone(tz) if tz else span.started_at
        end = span.last_at.astimezone(tz) if tz else span.last_at
        if start.date() != day:
            if day is not None:
                out.append("")
            out += [
                f"### {start.strftime('%b %d')}",
                "",
                "| Who | Component | Active | Stages |",
                "|---|---|---|---|",
            ]
            day = start.date()
        window = start.strftime("%H:%M")
        if span.last_at != span.started_at:
            end_format = "%H:%M" if end.date() == start.date() else "%b %d %H:%M"
            window += f"–{end.strftime(end_format)}"
        # dict.fromkeys dedupes while keeping the order it happened in.
        arc = " → ".join(dict.fromkeys(s.value for s in span.stages))
        out.append(
            f"| {span.author or '—'} | {span.component or UNFILED} "
            f"| {window} | {arc} |"
        )
    out.append("")
    return out


def _render_entry(entry: LoggedEntry, tz: tzinfo | None = None) -> list[str]:
    record = entry.record
    head = f"### {_date(entry.created_at, tz)} · {record.stage.value} · {record.subteam.value}"
    if entry.author:
        head += f" · {entry.author}"

    out = [head, "", f"**{record.title}**", "", record.summary, ""]

    for name, label in SECTIONS:
        value = getattr(record, name)
        if not value:
            continue
        if isinstance(value, list):
            value = "; ".join(value)
        out.append(f"- **{label}** — {value}")

    if any(getattr(record, n) for n, _ in SECTIONS):
        out.append("")

    # No per-entry "not recorded" line on purpose: most single messages are
    # legitimately partial, and flagging each one would bury a complete thread
    # in warnings. Gaps are reported once per thread, in the coverage table.
    return out


def render_notebook(
    entries: list[LoggedEntry],
    *,
    title: str = "Engineering Notebook",
    tz: tzinfo | None = None,
) -> str:
    """Render entries as notebook markdown.

    Entries with `stage == unknown` are dropped: chitchat never enters the
    notebook. The count is reported in the footer rather than silently swallowed,
    so a suspiciously large number is visible as a classification problem.
    """
    logged = [e for e in entries if e.record.stage is not Stage.UNKNOWN]
    skipped = len(entries) - len(logged)

    if not logged:
        # Still report the skipped count. "50 messages, none of them design" is
        # not an empty notebook, it is a classification problem, and silently
        # printing "no records yet" would hide exactly that signal.
        note = (
            f" _{skipped} message{'s' if skipped > 1 else ''} classified as "
            f"unrelated to design._"
            if skipped
            else ""
        )
        return f"# {title}\n\n_No design records yet._{note}\n"

    threads = _group(logged)
    dates = sorted(e.created_at for e in logged)

    out = [
        f"# {title}",
        "",
        f"_{len(logged)} entr{'y' if len(logged) == 1 else 'ies'} · "
        f"{_date(dates[0], tz)} – "
        f"{_date(dates[-1], tz)}, {(dates[-1].astimezone(tz) if tz else dates[-1]).year} · "
        f"{len(threads)} component{'' if len(threads) == 1 else 's'}_",
        "",
        "## Coverage",
        "",
        "| Component | Entries | Stages reached | Not recorded |",
        "|---|---|---|---|",
    ]

    for name, group in threads.items():
        out.append(f"| {name} | {len(group)} | {_arc(group)} | {_gaps(group)} |")

    out.append("")
    out += _timeline(logged, tz)

    for name, group in threads.items():
        out += [f"## {name}", ""]
        for entry in group:
            out += _render_entry(entry, tz)

    if skipped:
        out.append(
            f"_{skipped} message{'s' if skipped > 1 else ''} classified as "
            f"unrelated to design and not logged._"
        )
        out.append("")

    return "\n".join(out)
