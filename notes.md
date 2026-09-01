# Scoring log

One row per run. Change ONE thing per run (§9). If a score drops, roll back.

Gates: silence must be 3/3 (non-negotiable, it is the whole 15/15 chitchat gate
scaled to this set). stage >= 13/15.

| # | date | change | model | stage | silence | missing |
|---|---|---|---|---|---|---|
| 1 | 2026-08-31 | first baseline | deepseek-v4-flash-vision-exp | 14/15 | 3/3 | 12/15 |
| 2 | 2026-08-31 | fixed two wrong expectations in samples.py (no prompt change) | same | 14/15 | 3/3 | 13/15 |

## ⚠️ This baseline is not trustworthy yet

Two reasons, both blocking before any of these numbers mean anything:

1. **The 15 messages are invented**, not real team messages. §9 wants real ones
   because a score against made-up text measures how well the prompt fits one
   person's idea of how a team talks. Run 1 proved the point immediately: of its
   three `missing` failures, **two were wrong answers in the key, not model
   errors** — samples 11 and 12 state no problem at all, and the key claimed
   `problem_statement` was present. Writing both the question and the answer is
   how you fool yourself.
2. **The model is `deepseek-v4-flash-vision-exp`, not the `deepseek-chat` §5
   settles on.** A score is bound to a model. Settle that before treating any
   row here as a baseline to compare against.

## Standing failures at run 2

- `"we're doing 4 wheels on the intake now instead of 6"` — want `decision`, got
  `build`. Genuinely ambiguous: it announces a choice with no build action, but
  reads like a done deal. Either the Stage enum's decision/build boundary needs a
  sharper line, or the expectation is wrong. Decide with a real message.
- `"after the slide flex thing we compared 2 stage vs 3 stage"` — model marks
  `problem_statement` missing; the key says the flex issue counts as stated.
  Defensible both ways. A passing reference to a prior problem probably is not a
  problem statement — leaning toward the model being right.

Neither is worth a prompt change yet. Both are one-sample effects on invented
text; fixing them now would be fitting the prompt to my own writing.
