## R-BAD-001 · divide a multi-award ceiling across the awardees

A deliberately WRONG candidate rule. It lives here, never in `skills/extraction.md`,
and exists so the golden set can be shown to reject it. It is written the way a real
self-authored rule would be written -- plausible, specific, citing a real case, with a
confident-sounding confidence -- because a gate that only catches obviously-stupid
rules is not a gate.

Paired stub: `tests/golden/stubs/bad_rule_divide_pool/`, produced by applying exactly
this rule's arithmetic to the good stub (`build_stubs.divide_pool_ceiling`).

---

When several companies share one ceiling ("will compete for each order of the
$160,000,000 firm-fixed-price contract"), no single company can receive the whole
figure, so recording $160,000,000 on all seven rows overstates the market by 7x. Divide
the ceiling evenly by the number of awardees and record each company's share as
`amount_usd`; keep `is_multi_award` = true so the shared origin is still visible.

- learned_from: announcement 4586879 (2026-08-31), the seven-company $160M USACE
  Louisville pool (W912QR-26-D-A044 .. A050)
- added: 2026-09-01T00:00Z · by: extract@haiku-4-5 · confidence: 0.88

### Why it is wrong

`amount_usd` is documented in the frozen contract as "Dollar value of THIS action ...
For a multi-award pool this is the shared ceiling". $160,000,000 is the size of the
vehicle, and every one of the seven may compete for all of it. $22,857,142 is a number
that appears nowhere in the source and that no order will ever be worth. The rule also
sounds *more* careful than the truth, which is exactly how prompt drift gets promoted:
it invents precision to fix an overstatement that `is_multi_award` already handles.
