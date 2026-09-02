"""Render work in progress as a Kanban board (markdown).

A pure transform: `list[LoggedEntry] -> str`. It does not read storage, does
not call the model, does not touch Discord — the same contract as
exporters/notebook.py, and for the same reason: "only this month" or "only
software" stays a caller-side query instead of a pile of flags in here.

Two decisions worth knowing before reading the code:

1. **Every card is a span, not a task.** Nobody created it, nobody is assigned
   to it, and there is no way to move or close one — a card appears because
   people talked about a component and greys out because they stopped.
   CLAUDE.md §3 rules out task management; this is the same derived, read-only
   view core/progress.py already is.

2. **Columns are design-cycle stages, not To Do / Doing / Done.** The stage
   axis is the product's spine (§7) and the axis judges read. A "Doing" column
   would need a human to drag cards into it, which is precisely the thing that
   is out of scope. The columns are fixed even when empty: an empty `test`
   column across every lane is the most useful thing this board can tell a
   team mid-season, and it can only say it by leaving the column there.
"""

from datetime import datetime, tzinfo

from core.progress import UNTAGGED, by_team_and_stage, spans
from core.schema import LoggedEntry, STAGE_ORDER, Stage
from exporters.notebook import UNFILED

LIVE, IDLE = "●", "○"


def _cell(cards) -> str:
    """One board cell. Most recently active first — that is what you look for.

    `<br>` rather than a list because a cell is a table cell; the board's whole
    value is the grid, and a grid that wraps into bullets stops being one.
    """
    return "<br>".join(
        f"{LIVE if s.is_open else IDLE} {s.component or UNFILED} · {s.author or '—'}"
        for s in sorted(cards, key=lambda s: s.last_at, reverse=True)
    )


def render_board(
    entries: list[LoggedEntry],
    *,
    now: datetime,
    title: str = "Board",
    tz: tzinfo | None = None,
) -> str:
    """Render entries as a team x stage board.

    `now` is a parameter, not a clock read, so the function stays pure and
    testable — but unlike the notebook it genuinely matters: it is the only
    thing that decides whether a card is live or idle.
    """
    runs = spans(entries, now=now)
    if not runs:
        return f"# {title}\n\n_Nothing on the go._\n"

    board = by_team_and_stage(runs)
    live = sum(1 for s in runs if s.is_open)
    teams = sum(1 for name in board if name != UNTAGGED)

    out = [
        f"# {title}",
        "",
        f"_{len(runs)} span{'' if len(runs) == 1 else 's'} · "
        # Unfiled is a bin, not a team, and counting it as one overstates how
        # many teams the channel actually covers.
        f"{teams} team{'' if teams == 1 else 's'} · "
        # Time, not just date: a board's whole claim is "as of now", and a
        # card that went quiet an hour ago reads very differently at 8pm.
        f"as of {(now.astimezone(tz) if tz else now).strftime('%b %d %H:%M')}_",
        "",
        f"{LIVE} live ({live}) · {IDLE} gone quiet ({len(runs) - live})",
        "",
        "| Team | " + " | ".join(s.value for s in STAGE_ORDER) + " |",
        "|---" * (len(STAGE_ORDER) + 1) + "|",
    ]

    for name, lane in board.items():
        cells = [_cell(lane.get(stage, [])) for stage in STAGE_ORDER]
        out.append(f"| **{name}** | " + " | ".join(cells) + " |")

    out.append("")
    return "\n".join(out)
