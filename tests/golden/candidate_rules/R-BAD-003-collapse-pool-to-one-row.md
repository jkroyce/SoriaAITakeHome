## R-BAD-003 · a shared-ceiling paragraph is one contract, so emit one row

A deliberately WRONG candidate rule, kept here and never in `skills/extraction.md`.

Paired stub: `tests/golden/stubs/bad_rule_collapse_pool/`, produced by applying exactly
this rule to the good stub (`build_stubs.collapse_pool_to_one_row`).

---

One paragraph describing one ceiling, one completion date, one contracting activity and
one bid count is one contract action. Emitting seven near-identical rows duplicates the
$160,000,000 seven times over. Emit a single row for the paragraph with
`is_multi_award` = true, put the lead company in `contractor_raw` and its number in
`contract_number`, and list the remaining awardees in `extraction_notes`.

- learned_from: announcement 4586879 (2026-08-31), the seven-company USACE Louisville
  pool and the ten-company NAVFAC pool
- added: 2026-09-01T00:00Z · by: extract@haiku-4-5 · confidence: 0.79

### Why it is wrong

The unit of the `awards` table is the contractor-award, not the paragraph:
`award_uid` = sha256(announcement_id | contract_number | contractor_raw). Seven distinct
contract numbers were issued to seven distinct companies, and six of them disappear from
the terminal entirely -- including their small-business status, which differs company by
company inside a pool. The duplication this rule worries about is not real: downstream
code knows the ceiling is shared because `is_multi_award` says so.
