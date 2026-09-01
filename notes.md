# Scoring log

One row per run. Change ONE thing per run (§9). If a score drops, roll back.

Gates: silence must be 3/3 (non-negotiable, it is the whole 15/15 chitchat gate
scaled to this set). stage >= 13/15.

| # | date | change | model | stage | silence | missing |
|---|---|---|---|---|---|---|
| 1 | 2026-08-31 | first baseline | deepseek-v4-flash-vision-exp | 14/15 | 3/3 | 12/15 |
| 2 | 2026-08-31 | fixed two wrong expectations in samples.py (no prompt change) | same | 14/15 | 3/3 | 13/15 |
| 3 | 2026-09-01 | model settled to deepseek-chat (no prompt change) | deepseek-chat | 14/15 | 3/3 | 13/15 |

## ⚠️ This baseline is still not fully trustworthy

One reason resolved, one still blocking:

1. **Resolved at run 3.** The model is now `deepseek-chat`, the one §5 settles
   on, not the vision/experimental model runs 1–2 were scored against. Scores
   held identical (14/15, 3/3, 13/15) — the swap changed nothing observable on
   this set.
2. **Still blocking.** The 15 messages are invented, not real team messages.
   §9 wants real ones because a score against made-up text measures how well
   the prompt fits one person's idea of how a team talks. Run 1 proved the
   point immediately: of its three `missing` failures, two were wrong answers
   in the key, not model errors — samples 11 and 12 state no problem at all,
   and the key claimed `problem_statement` was present. Writing both the
   question and the answer is how you fool yourself. Do not treat run 3 as a
   real baseline; treat it as "the model swap didn't regress anything."

## Standing failures at run 3

- `"we're doing 4 wheels on the intake now instead of 6"` — want `decision`, got
  `build`. Genuinely ambiguous: it announces a choice with no build action, but
  reads like a done deal. Either the Stage enum's decision/build boundary needs a
  sharper line, or the expectation is wrong. Decide with a real message.
- `"dropping road runner for pedro pathing..."` — model marks
  `alternatives_considered` missing; key says Road Runner (the thing being
  dropped) counts as an alternative considered. Defensible both ways.
- `"swapped the 435 rpm motors on the intake for 1150s"` — model returns
  `alternatives_considered` (the 435s as the discarded option); key says it's
  not present. Mirror image of the previous failure — same underlying
  ambiguity: does naming the *old* part count as an alternative considered, or
  only a competing option that was actively weighed?

None of these are worth a prompt change yet. All three are one-sample effects
on invented text; fixing them now would be fitting the prompt to my own
writing. The alternatives_considered pair above is the same open question
twice — decide it once real messages surface the pattern.
