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


def test_field_value_stays_under_discords_cap():
    """1024 characters per field value. Over it, Discord answers HTTP 400 and
    the whole command dies — the card does not degrade, it disappears.

    BOARD_MAX_CARDS counts cards, and a component is whatever the model
    pulled out of a Discord message, so cards alone cannot bound the string.
    """
    cards = [S("swerve module bearing retainer " * 10) for _ in range(BOARD_MAX_CARDS)]
    embed = _board_card(Board({ANDROMEDA: {Stage.BUILD: cards}}, SINCE))
    assert len(embed.fields[0].value) <= 1024


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
