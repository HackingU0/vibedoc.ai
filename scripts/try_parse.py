"""The scoring loop (§9). `python -m scripts.try_parse`

Three independent numbers, because "70% correct" gives no direction:

    stage    -> classification criteria unclear -> fix the Stage enum description
    silence  -> model is over-eager             -> fix followup_question + global rule
    missing  -> "missing" is ill-defined        -> fix missing_fields description

Change ONE thing per run. Log the score in notes.md. If it drops, roll back.
"""

import asyncio

from core.agent import parse_design_record
from tests.samples import SAMPLES

CONCURRENCY = 5


async def run_one(sample, sem):
    async with sem:
        try:
            return sample, await parse_design_record(sample.text), None
        except Exception as exc:  # a crash is a failure, not a stopped run
            return sample, None, exc


async def main():
    from core import triage
    from core.schema import Stage

    sem = asyncio.Semaphore(CONCURRENCY)
    skipped = [s for s in SAMPLES if not triage.worth_parsing(s.text)]
    wrongly = [s for s in skipped if s.stage is not Stage.UNKNOWN]
    results = await asyncio.gather(*(run_one(s, sem) for s in SAMPLES if s not in skipped))

    stage_ok = missing_ok = 0
    silent_ok = sum(1 for s in skipped if s.silent)
    n_silent = sum(s.silent for s in SAMPLES)
    fails = []

    for sample, got, exc in results:
        head = sample.text[:52]
        if exc:
            fails.append(f"  CRASH  {head} -> {type(exc).__name__}: {exc}")
            continue

        if got.stage is sample.stage:
            stage_ok += 1
        else:
            fails.append(f"  stage  {head}\n         want {sample.stage.value}, got {got.stage.value}")

        if sample.silent and sample in skipped:
            continue
        if sample.silent:
            if got.followup_question is None:
                silent_ok += 1
            else:
                fails.append(f"  SPOKE  {head}\n         asked: {got.followup_question}")

        if set(got.missing_fields) == set(sample.missing):
            missing_ok += 1
        else:
            extra = set(got.missing_fields) - sample.missing
            lack = sample.missing - set(got.missing_fields)
            fails.append(f"  missing {head}\n         +{sorted(extra)} -{sorted(lack)}")

    n = len(SAMPLES)
    print(f"triage   skipped {len(skipped)}/{n} before any call "
          f"({len(wrongly)} of them real — must be 0)")
    print(f"stage    {stage_ok:>2}/{n}   (gate >= 13)")
    print(f"silence  {silent_ok:>2}/{n_silent}   (gate {n_silent}/{n_silent}, non-negotiable)")
    print(f"missing  {missing_ok:>2}/{n}")
    if fails:
        print("\n" + "\n".join(fails))

    print("\n--- follow-ups, read these aloud ---")
    for sample, got, exc in results:
        if got and got.followup_question:
            print(f"  {got.followup_question}")


asyncio.run(main())
