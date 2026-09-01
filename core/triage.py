"""Cheap pre-filter: is this text worth an LLM call at all?

Most of what lands in a team Discord is "lol", "omw", a link, or a question
about snacks. Sending each one to the model costs a call, writes a row, and
inflates the notebook's "classified as unrelated" footer with noise that was
never a judgement call in the first place.

Deliberately conservative. A false negative silently loses real content — the
one failure mode this project cannot tolerate — while a false positive costs
one call and lands as stage=unknown anyway. So: anything long, anything with a
number in it, and anything naming a robot part goes through.

This runs on a coalesced burst (see core/inbox.py), not on single messages.
"it broke again" on its own is skipped; "it broke again" followed by "the arm
mount snapped" is one unit and passes.
"""

import re

# ponytail: hand-kept keyword list, and a calibration knob rather than a
# finished artifact — it is game- and team-specific and will rot between
# seasons. Retune it against tests/samples.py (test_triage guards the floor).
# Upgrade path if it starts costing recall: drop the list and gate on length
# alone, which is strictly safer and only slightly more expensive.
KEYWORDS = frozenset("""
    intake slide slides arm claw grabber odometry odo auto autonomous teleop
    opmode pid pathing vision april apriltag limelight camera
    motor encoder servo gear belt chain sprocket spool bearing bushing
    wheel wheels drivetrain chassis frame mount bracket plate standoff
    hang climb spec specimen sample basket bucket hook linkage fourbar
    cad print printed tolerance flex jam jammed stall slip torque rpm
    battery wiring hub expansion sensor limit switch
""".split())

_WORD = re.compile(r"[a-z]+")

# A burst this long is somebody explaining something. Let the model read it.
_LONG_ENOUGH = 80


def worth_parsing(text: str) -> bool:
    """False only when the text cannot plausibly contain a design record."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith(("http://", "https://")) and not any(ch.isspace() for ch in stripped):
        return False
    if len(stripped) >= _LONG_ENOUGH:
        return True
    if any(ch.isdigit() for ch in stripped):
        return True

    words = set(_WORD.findall(stripped.lower()))
    if words & KEYWORDS:
        return True
    return len(words) > 12
