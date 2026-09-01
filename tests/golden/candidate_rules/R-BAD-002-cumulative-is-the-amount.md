## R-BAD-002 · record the cumulative face value as the amount of the action

A deliberately WRONG candidate rule, kept here and never in `skills/extraction.md`.

Paired stub: `tests/golden/stubs/bad_rule_cumulative_as_amount/`, produced by applying
exactly this rule to the good stub (`build_stubs.cumulative_as_amount`).

---

When a modification states a total cumulative face value ("The amount of this action is
$180,000,000 with a total cumulative face value of $280,000,000"), the cumulative figure
is the contract's real size and is what an investor is exposed to. Record it as
`amount_usd` so the awards table reflects the full obligation, and leave
`cumulative_face_value_usd` set to the same value for continuity.

- learned_from: announcement 4586879 (2026-08-31), South Carolina Commission for the
  Blind, $180M P00002 on W9124C-25-D-A003 with a $280M cumulative
- added: 2026-09-01T00:00Z · by: extract@haiku-4-5 · confidence: 0.82

### Why it is wrong

The cumulative face value includes money already announced and already counted on the
base contract. Writing it into `amount_usd` double-counts $100,000,000 for this single
row, and across the corpus it silently inflates every "new business this month" figure
the terminal reports. The schema already has a field for the cumulative; this rule
throws away the one number the sentence states explicitly ("The amount of this action
is $180,000,000") in favour of one that means something else.
