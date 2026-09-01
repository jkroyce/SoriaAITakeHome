# skills/entity_resolution.md

Learned procedural rules for mapping a raw contractor name to a public company.
Append-only. Every rule cites the case that motivated it. This file is injected into
the tier-2 prompt, so editing it invalidates the entity-resolution LLM cache — that is
deliberate (see CLAUDE.md, "Writing skills for yourself").

Tier 1 (deterministic alias match against `data/universe.csv`) never reads this file;
a dictionary lookup cannot be wrong in a way a prose rule would fix. These rules exist
for tier 2 only — except R-007, which is a rule about the code itself.

---

## R-001 · most DoD contractors are private, and that is the right answer
The publication threshold is $7.5M, which is well inside the range of a small
engineering, construction or logistics firm. When a name is not a recognisable listed
company or a division of one, answer `relationship="private"`, `ticker=null`,
`is_public=false` with high confidence. Do not reach for a same-sounding public
company. A confident "private" is a correct resolution, not a failure.

- learned_from: "AAECON General Contracting LLC" and similar small-business (`*`)
  entries in the 50-day backfill
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.95

## R-002 · resolve to the ULTIMATE public parent, not the operating unit
Announcements print the operating company as contracted. Map through to whatever is
actually listed and say so in `reasoning`: put the operating unit in
`normalized_name`, the listed entity in `parent_company`, and set
`relationship="subsidiary"`.

- learned_from: "Rockwell Collins Inc." → RTX Corporation (RTX); acquired by Raytheon
  in 2018, Raytheon Technologies renamed RTX in 2023
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.97

## R-003 · legal-suffix noise carries no information
`Inc`, `Inc.`, `Incorporated`, `LLC`, `L.L.C.`, `Corp`, `Corp.`, `Corporation`,
`Co.`, `Company`, `Ltd`, `LP`, `LLP`, `PLC`, a leading `The`, and a trailing `*`
(the small-business marker) are stripped before matching and must not appear in
`normalized_name`. "The Boeing Co." and "Boeing Company" are the same entity.

- learned_from: "The Boeing Co., Seattle, Washington" (announcement 4523515)
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.99

## R-004 · joint ventures get `joint_venture`, not a coin flip
A name containing "JV", "Joint Venture", or two recognisable primes joined by `/`,
`-` or `and` is a joint venture. If exactly one parent is public, give that ticker
with `relationship="joint_venture"` and confidence no higher than 0.75, because the
revenue split is not disclosed. If neither or both are public, prefer `ticker=null`
and explain in `reasoning`. Tier 1 refuses these deterministically (`_JV_RE`) rather
than letting an alias prefix claim them.

- learned_from: recurring pattern in Army construction MATOC pools across the 50-day
  backfill; no single announcement id
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.70

## R-005 · post-cutoff ownership changes are an honesty problem, not a guess
Defence ownership changes often (L3 + Harris 2019, Raytheon + United Technologies
2020, Aerojet Rocketdyne → L3Harris 2023). If a name might have changed hands after
your knowledge cutoff, resolve to the last ownership you actually know, cap
`confidence` at 0.65 so the manager escalates, and name the uncertainty in
`reasoning`.

- learned_from: design review of the escalation path (confidence < 0.7 → Opus → human)
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.80

## R-006 · a division name is not a new company
"Lockheed Martin Rotary and Mission Systems", "General Dynamics Land Systems" and
"Northrop Grumman Systems Corp." are divisions, and tier 1 already catches them by
alias prefix. If one reaches tier 2 (a division tier 1 has never seen), resolve it to
the prime's ticker with `relationship="subsidiary"` and confidence ≥ 0.9 — the prime's
name being a prefix of the raw name is strong evidence, not a coincidence.

- learned_from: universe.csv alias lists; the tier-1 prefix rule is the deterministic
  half of this same idea
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.90

## R-007 · two keys: RAW joins, NORMALIZED is reasoned about
`entities.contractor_raw` is the join key. `src/store.py` keys the `entities` table on
it and joins `entities.contractor_raw = awards.contractor_raw`, and agent-extract's
rule R-004 keeps the small-business trailing `*` in `awards.contractor_raw` "exactly
as printed". So a row's `contractor_raw` must be the printed string byte-for-byte —
asterisk, `, Inc.`, doubled whitespace and all. Normalizing it makes the join silently
return zero rows.

The *cache* key is the opposite. `data/entity_map.json`, the tier-2 batch slots and
the LLM cache key are all keyed on `normalize(raw)`, so every printed spelling of one
company is reasoned about **once**. The code path is: group raw variants by normalized
name → pick one `representative()` → resolve that representative once → `_for_raw()`
fans the single result out to every raw variant, each keeping its own
`contractor_raw`.

Get this backwards and one company either splits into two entity keys and is paid for
twice, or collapses into one row that no award can join to.

- learned_from: this section being restarted after the extract agent's R-004 fixed the
  asterisk into `contractor_raw`; asserted by selftest check 1, which requires exactly
  one model call across `"…Inc."` / `"…Inc.*"` pairs and two distinct rows out
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.99

## R-008 · a low-confidence answer is escalated once, then handed to a human
Below 0.7 the batch is re-run on `claude-opus-5` exactly once. If Opus clears the bar
its answer replaces the Haiku one and is persisted. If it does not, the uncertain
record is still emitted (so the UI shows something honest) but is **not** written to
`data/entity_map.json` — persisting a guess would freeze it forever and suppress every
future attempt — and a `review_queue` row is produced instead. The same applies to a
cache miss under `live=False`: pending is a review case, never a persisted fact.

- learned_from: designing the offline escalation test (stubbed 0.5 → Opus → 0.5)
  during this restart; CLAUDE.md "Cleaning-agent protocol"
- added: 2026-09-01T00:00Z · by: resolve@design · confidence: 0.90
