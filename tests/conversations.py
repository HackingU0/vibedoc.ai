"""Multi-message, multi-round fixtures for scripts/try_conversation.py.

⚠️  THESE ARE INVENTED, exactly like tests/samples.py. Same warning applies:
replace `burst` and `replies` with real transcripts from the team channel
before believing any number here. §9's rule that real messages come first is
not softer just because the unit got bigger.

What this measures that samples.py cannot:
  - a burst that must collapse into ONE record, not four
  - a follow-up that legitimately deserves a second round
  - a deflection that must end the conversation immediately
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Conversation:
    burst: list[str]
    # Replies, in order, as answers to whatever the bot asks. The run stops
    # early if the bot goes quiet before these run out.
    replies: list[str] = field(default_factory=list)
    # How many questions the bot is allowed to ask across the whole exchange.
    max_questions: int = 2
    # Fields that must be filled by the end, given these replies.
    want_filled: frozenset[str] = frozenset()


CONVERSATIONS = [
    # A burst that is one thought. One record, at most one question.
    Conversation(
        burst=[
            "intake keeps jamming",
            "like when two blocks come in at the same time",
            "tried compliant wheels, didn't help much",
            "going dual roller, it fits the current mount",
        ],
        replies=["haven't tested it yet, next meeting"],
        max_questions=2,
        want_filled=frozenset(),
    ),
    # Two productive rounds: the classic "why?" -> "weight" -> "how much?".
    Conversation(
        burst=["swapped the slide to 2 stage"],
        replies=["it was too heavy at 3 stage", "about 400g lighter"],
        max_questions=3,
        want_filled=frozenset({"rationale"}),
    ),
    # A deflection must end it. One question in, zero after the shrug.
    Conversation(
        burst=["redid the odometry pod mount"],
        replies=["idk ask sam", "lol"],
        max_questions=1,
        want_filled=frozenset(),
    ),
    # Chitchat: never a question, and triage should not even reach the model.
    Conversation(
        burst=["who's driving tmrw", "i can bring the cart"],
        replies=[],
        max_questions=0,
        want_filled=frozenset(),
    ),
]
