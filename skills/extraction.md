# skills/extraction.md — learned extraction rules

Append-only. Every rule cites the announcement that motivated it. These rules are
injected verbatim into the extraction prompt, which means **editing this file changes
the cache key and forces re-extraction of every document.** Promotion is gated
(`manager promote-skills`) and a candidate rule is accepted only if it does not
regress `tests/golden/`.

Rule ids are stable. Never renumber, never delete — supersede.

---

## R-001 · multi-award IDIQ pools emit one row per company
When a single paragraph lists several companies, each followed by its own contract
number in parentheses, and then a **single shared dollar figure** ("will compete for
each order of the $160,000,000 ... contract", "were awarded a combined $145,000,000
... multiple award contract"), emit **one row per company**, not one row for the
paragraph:

- `action_type` = `multi_award_pool`, `is_multi_award` = true on every row
- `contract_number` = that company's own number, never a sibling's
- `amount_usd` = the **shared ceiling**, repeated on every row. It is not divided by
  the number of awardees and it is not a per-company amount. Downstream code knows to
  treat it as shared via `is_multi_award`.
- everything else (work description, completion date, contracting activity, bid
  counts) is shared and is copied onto every row
- the trailing `*` belongs to the company it is printed next to, so `small_business`
  can differ row to row within the same pool

- learned_from: announcement 4586879 (2026-08-31), the seven-company $160M USACE
  Louisville design-bid-build pool (W912QR-26-D-A044 .. A050), and the ten-company
  $145M NAVFAC petroleum/oil/lubricants pool (N39430-26-D-2011 .. 2020)
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.95

## R-002 · a modification is not a new award
"was awarded a $180,000,000 modification (P00002) to contract W9124C-25-D-A003"
is one action against an existing vehicle:

- `action_type` = `modification`
- `modification_number` = `P00002` — the token in parentheses right after the word
  *modification*. It is not always a P-number: `001 CB` is also a modification number.
- `base_contract_number` = `W9124C-25-D-A003`, the contract being modified
- `contract_number` = the same base contract number when no separate new number is
  printed (the action lives on that contract)
- `amount_usd` = **the amount of this action** ($180,000,000), never the cumulative
- `cumulative_face_value_usd` = the stated total after the action ($280,000,000),
  from "a total cumulative face value of", "brings the total cumulative face value of
  the contract to", or "The total cumulative face value is"

- learned_from: announcement 4586879 (2026-08-31), South Carolina Commission for the
  Blind, $180M P00002 on W9124C-25-D-A003, cumulative $280M
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.95

## R-003 · a modification that exercises an option is an option_exercise
When the prose says the modification **exercises an option** ("The modification
exercises an option to provide continued program management"), prefer
`action_type` = `option_exercise` over `modification`. Still fill
`modification_number` and `base_contract_number` — an option exercise is a
modification in form, and the distinction is the one an investor cares about
(planned continuation vs. new money).

- learned_from: announcement 4586879 (2026-08-31), KBR Wyle Services LLC,
  $25,162,568 modification (P00003) to N0042125C1001 exercising an option
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.85

## R-004 · the asterisk is the small-business marker
A trailing `*` on the contractor name — printed after the name or after the comma
that follows it, e.g. `Action Manufacturing Co.,*` or `AAECON General Contracting
LLC,*` — means `small_business` = true. Keep the asterisk in `contractor_raw`; it is
part of the name "exactly as printed". The `*Small business` legend on the last line
of the document is a footnote, **not an award** — never emit a row for it. No
asterisk means `small_business` = false, not null.

- learned_from: announcement 4586879 (2026-08-31), 8 of its entries asterisked
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.97

## R-005 · service branch is inherited from the ALL-CAPS header above
Awards appear under ALL-CAPS section headers on their own line. Every award inherits
the nearest header **above** it, until the next header. Map the header to the enum by
exact match; anything with no exact match becomes `OTHER`, and the literal header text
goes in `extraction_notes`.

Headers observed across the 50-document corpus, with their mapping:

| printed header | `service_branch` |
|---|---|
| `ARMY` | `ARMY` |
| `NAVY` | `NAVY` |
| `AIR FORCE` | `AIR FORCE` |
| `DEFENSE LOGISTICS AGENCY` | `DEFENSE LOGISTICS AGENCY` |
| `MISSILE DEFENSE AGENCY` | `MISSILE DEFENSE AGENCY` |
| `SPACE FORCE`, `MARINE CORPS` | same, when they appear |
| `U.S. SPECIAL OPERATIONS COMMAND` | `OTHER` |
| `U.S. TRANSPORTATION COMMAND` | `OTHER` |
| `DEFENSE ADVANCED RESEARCH PROJECTS AGENCY` | `OTHER` |
| `DEFENSE HEALTH AGENCY` | `OTHER` |
| `DEFENSE COUNTERINTELLIGENCE AND SECURITY AGENCY` | `OTHER` |
| `WASHINGTON HEADQUARTERS SERVICES` | `OTHER` |

`DEFENSE` is reserved for a header that is literally `DEFENSE` (department-wide /
Office of the Secretary of Defense). Do **not** route DARPA or the Defense Health
Agency to `DEFENSE` merely because the word appears in the header.

- learned_from: header census over all 50 cached announcements (`raw/articles/*.html`)
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.9

## R-006 · a task order against an existing IDIQ is a new_award with a parent
"is being awarded a $9,747,240 task order (H9240826FE010), under an indefinite
delivery-indefinite quantity contract ... The contract (H9240824D4343)" has no
dedicated `action_type`. Record it as `new_award`, with `contract_number` = the task
order number, `base_contract_number` = the parent IDIQ, and `extraction_notes` saying
it is a task order under that parent. Cap `extraction_confidence` around 0.8 — the
action type is a judgement call the schema cannot express exactly.

- learned_from: announcement 4586879 (2026-08-31), Raytheon Co. Silent Knight radar
  task order H9240826FE010 under H9240824D4343
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.75

## R-007 · bid counts: only record what is stated
"One bid was solicited with one received" -> `bids_solicited` = 1, `bids_received` = 1.
"Bids were solicited via the internet with 19 received" -> `bids_solicited` = null
(the count is not stated), `bids_received` = 19.
"competitively procured via the SAM.gov website, with 14 offers received" ->
`bids_received` = 14, `bids_solicited` = null. Never infer a solicited count from a
received count, and never write 0 for "not stated".

- learned_from: announcement 4586879 (2026-08-31), all three phrasings present
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.92

## R-008 · the amount is the amount of THIS action
When a paragraph carries more than one dollar figure, `amount_usd` is the value of the
action being announced, in this order of preference:
1. "The amount of this action is $X" -> X
2. the figure in the award verb phrase ("was awarded a $X ... contract") -> X
3. for a multi-award pool, the shared ceiling (R-001)

Obligation figures ("Fiscal 2026 funds in the amount of $X were obligated at the time
of award"), option/maximum values ("The maximum dollar value, including the base price
and 13 options, is $Y") and cumulative face values are **not** `amount_usd`. The
cumulative one goes to `cumulative_face_value_usd`; the others are not in the schema
and, if a reviewer needs them, belong in `extraction_notes`.

- learned_from: announcement 4586879 (2026-08-31), Environmental Chemical Corp.
  ($128,363,354 award vs. $174,443,268 maximum with 13 options); Rockwell Collins
  ($38,916,896 this action vs. $55,146,864 cumulative)
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.93

## R-009 · a trailing "(Awarded Aug. 28, 2026)" does not change announced_date
Some entries end with a parenthetical award date earlier than the announcement date.
`announced_date` is deterministic (the date of the document) and is filled by code,
not by you. Do not try to correct it; if the discrepancy matters, mention it in
`extraction_notes`.

- learned_from: announcement 4586879 (2026-08-31), several entries marked
  "(Awarded Aug. 28, 2026)"
- added: 2026-09-01 · by: extract@haiku-4-5 · confidence: 0.9
