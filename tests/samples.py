"""Scoring set for scripts/try_parse.py.

⚠️  THESE MESSAGES ARE INVENTED. Nobody on a real team wrote them.

§9 puts real messages first for a reason: a score against made-up text measures
how well the prompt fits one person's idea of how a team talks. Treat this as a
smoke-level baseline only. Replace `text` with 15 real ones before believing any
number, and re-record the baseline when you do — the expectations below were
written by reading the text, so they will need rewriting too.

Composition per §9: 3 complete decisions, 4 result-without-reason,
3 complaint-without-solution, 2 with test data, 3 pure chitchat.
"""

from dataclasses import dataclass, field

from core.schema import Stage


@dataclass(frozen=True)
class Sample:
    text: str
    stage: Stage
    #当 followup_question 必须为 None 时为 True。闲聊必须静默。
    silent: bool = False
    # missing_fields 的期望值,只用这四个名字
    missing: frozenset[str] = field(default_factory=frozenset)


P, A, R, T = "problem_statement", "alternatives_considered", "rationale", "test_evidence"

SAMPLES = [
    # ── 3 complete decisions ────────────────────────────────────────────────
    Sample(
        "intake kept jamming when two samples came in at once. we looked at "
        "compliant wheels and a wider funnel plate, ended up going dual roller "
        "since it fits the current frame and we don't have to redo the mount",
        Stage.DECISION, missing=frozenset({T}),
    ),
    Sample(
        "after the slide flex thing we compared 2 stage vs 3 stage viper slides. "
        "going with 2 stage, the 3 stage was like 180g heavier and we're already "
        "way over on the arm side",
        Stage.DECISION, missing=frozenset({T}),
    ),
    Sample(
        "dropping road runner for pedro pathing. rr tuning was eating a whole "
        "meeting every week and pedro's follower handles our heading drift better",
        Stage.DECISION, missing=frozenset({T}),
    ),

    # ── 4 result without reason ─────────────────────────────────────────────
    Sample(
        "swapped the 435 rpm motors on the intake for 1150s",
        Stage.BUILD, missing=frozenset({P, A, R, T}),
    ),
    Sample(
        "moved the odometry pods to the front of the chassis today",
        Stage.BUILD, missing=frozenset({P, A, R, T}),
    ),
    Sample(
        "we're doing 4 wheels on the intake now instead of 6",
        Stage.DECISION, missing=frozenset({P, A, R, T}),
    ),
    Sample(
        "rewrote auto to score 3 specimens instead of 2",
        Stage.BUILD, missing=frozenset({P, A, R, T}),
    ),

    # ── 3 complaint without solution ────────────────────────────────────────
    Sample(
        "the arm keeps stalling when we hold a sample at full extension",
        Stage.PROBLEM, missing=frozenset({A, R, T}),
    ),
    Sample(
        "auto is super inconsistent, some runs it parks fine and some runs it's "
        "just nowhere near",
        Stage.PROBLEM, missing=frozenset({A, R, T}),
    ),
    Sample(
        "hang is way too slow, we're barely getting up before the buzzer",
        Stage.PROBLEM, missing=frozenset({A, R, T}),
    ),

    # ── 2 with test data ────────────────────────────────────────────────────
    Sample(
        "ran the new intake 20 times, 18 clean. both misses were the sample "
        "sitting against the wall",
        Stage.TEST, missing=frozenset({P, A, R}),
    ),
    Sample(
        "odometry is down to about 2cm error over 3m after retuning the lateral "
        "multiplier",
        Stage.TEST, missing=frozenset({P, A, R}),
    ),

    # ── 3 pure chitchat — the product's survival line ───────────────────────
    Sample("does anyone have a 3mm hex key, mine walked off",
           Stage.UNKNOWN, silent=True),
    Sample("meeting saturday is 10-4 right? my mom needs to know when to get me",
           Stage.UNKNOWN, silent=True),
    Sample("who's bringing food saturday, i vote not pizza again",
           Stage.UNKNOWN, silent=True),
]

assert len(SAMPLES) == 15
