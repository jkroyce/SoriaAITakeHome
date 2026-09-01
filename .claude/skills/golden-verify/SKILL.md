---
name: golden-verify
description: Run the golden fixture set to check extraction quality and gate skill promotion. Use before promoting any self-written agent rule, and before merging changes to extraction or resolution.
---

# Golden verification

Agents in this system write rules for themselves (`skills/*.md`) that get injected into
later prompts. Without a fixed measuring stick, "the agent improved" is an unfalsifiable
claim. The golden set is that measuring stick.

## What the golden set is

Hand-verified expected output for a handful of announcements in `tests/golden/`. Small
on purpose — every case is one a human actually checked against the source prose.

Must include the two cases that break naive extraction:

- **`4586879` (2026-08-31)** — a $160M IDIQ awarded to SEVEN companies, each with its
  own contract number, competing under one shared ceiling. Correct output is seven rows
  with `is_multi_award=true`, not one row and not seven separate ceilings.
- **`4586879` modification** — a $180M `P00002` against `W9124C-25-D-A003` with a stated
  cumulative face value of $280M. Correct output is `action_type=modification` with
  `cumulative_face_value_usd=280000000`, NOT a new $180M award. Getting this wrong
  inflates every downstream revenue read.

## Run

```bash
.venv/Scripts/python.exe -m pytest tests/test_golden.py -q
```

Golden runs must hit the committed cache and cost nothing. A golden run that tries to
call the API means the fixture drifted from the cache — investigate, do not just re-run
with `--live`.

## Gating skill promotion

A candidate rule is accepted only if the golden set does not regress. Procedure:

1. Record the current pass rate.
2. Append the candidate rule to the agent's `skills/*.md`.
3. Re-run the golden set.
4. Worse → revert the rule and say why. Same → the rule is unproven; keep it only if it
   addresses a case the golden set does not cover, and say so. Better → promote.

Remember the cost consequence: the skills file is part of the prompt and the cache is
keyed on the prompt, so promotion invalidates that agent's cache and forces
re-extraction. Batch promotions and price them before running.
