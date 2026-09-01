# skills/materiality.md

Learned procedural rules for scoring a DoD contract award's investor materiality.
Append-only. Every rule cites the case that motivated it, by war.gov announcement id.
This file is injected into the tier-2 system prompt, so editing it invalidates the
materiality LLM cache — that is deliberate (CLAUDE.md, "Writing skills for yourself").

The deterministic pre-filter never reads this file: a threshold on `amount_usd` cannot
be improved by a prose rule. These rules exist for the judgement calls only — except
R-009, which is a rule about the code.

Cases below were found by scanning the committed 50-announcement backfill (683 award
paragraphs, contracts for 2026-06-22 through 2026-08-31; median award $31.0M, p25 $15.6M, p75 $77.8M).

---

## R-001 · the routine case is the common case, and saying so is the answer
Roughly two in five announced awards are sustainment, spares, or a small order under a
vehicle the market already knows about. When an award neither changes a program's
direction nor is large against the awardee, score it in the routine band and say why
in one clause. Do not inflate a score because the contractor is famous.

- learned_from: announcement 4530662 — "Northrop Grumman Systems Corp., El Segundo,
  California, has been awarded a maximum $9,380,995 modification (P00001) to a delivery
  order (SPE4A5-25-F-7812)". A listed name (NOC), so the pre-filter correctly refuses
  it, but $9.4M of DLA spares is not news to a $40B-revenue company.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.93

## R-002 · the small-business asterisk bounds the COMPANY, not the AWARD
A trailing `*` means the awardee qualified as a small business under the set-aside, not
that the dollar figure is small. Never treat the asterisk as evidence of immateriality
on its own; the code only pre-filters an asterisked award when it is ALSO below the
$50M floor. Above that floor, judge it on its merits — including the read-through to
whichever listed prime did not win it.

- learned_from: announcement 4523515 — "Integrated Procurement Technologies,* Vandalia,
  Ohio, has been awarded a maximum $860,700,000 firm-fixed-price, price-redetermination,
  indefinite-delivery/indefinite-quantity contract for engine fuel tanks", sole-source;
  and announcement 4573283, a $55,000,000,000 multiple-award IDIQ whose first named
  awardee is also asterisked.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.95

## R-003 · a ceiling is an option, not revenue
An IDIQ "ceiling" or "maximum" is the most that MAY be ordered over the life of the
vehicle, often five to ten years, frequently across many holders, and routinely never
fully ordered. Score the position it creates, not the headline number, and put
`ceiling_not_revenue` in `drivers` so the rationale cannot be misread. A nine-figure
definitized production order is usually worth more to an investor than a ten-figure
ceiling.

- learned_from: announcement 4581800 — "The Boeing Co., St. Louis, Missouri, has been
  awarded a $131,230,000,000 ceiling, indefinite-delivery/indefinite-quantity contract
  for the F-15 Eagle Crest". Taken at face value that single line is larger than
  Boeing's annual revenue; it is a ceiling.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.94

## R-004 · a multi-award pool ceiling is SHARED, so divide before you judge
When one paragraph names several companies before a single dollar figure, or says "as
one of N multiple award awardees", every awardee competes for orders under one ceiling.
Extraction emits one row per company with the shared ceiling in `amount_usd`
(`is_multi_award=true`), so scoring each row at the full ceiling multiplies the same
money N times. Score the realistic share, and say in the rationale that the figure is
shared.

- learned_from: announcement 4527010 — an $8,000,000,000 construction pool naming
  Balfour Beatty, Clark Construction, EVCON-CWC JV,* Grunley and Whiting-Turner in one
  paragraph; and, in the same announcement, "True Zero Technologies LLC,* Fairfax,
  Virginia, was awarded a $350,000,000 ceiling ... as one of nine multiple award
  awardees".
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.92

## R-005 · on a modification, the action value is the news; cumulative face value is context
`amount_usd` on a modification is the incremental obligation — that is the new revenue.
A stated "total cumulative face value" tells you the size of the program the company
already holds, which is context for whether the increment matters, not a second win.
Never score the cumulative figure as if it were announced today. Modifications are
about 28% of announced awards, with a median of $25.1M, so a large one is a genuine
signal that a program of record is expanding.

- learned_from: announcement 4546541 — "Lockheed Martin Corp., Grand Prairie, Texas, was
  awarded a $439,387,261 undefinitized contract action for procurement of the ATACMS
  guided missile and launching assembly. The total cumulative face value of the contract
  is $896,708,…"; and announcement 4560267, a $53,859,843,289 P00002 on the PAC-3
  Production program, which is a program conversion and not $53B of same-day new work.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.90

## R-006 · an unlisted winner can still be material — name whose stock it is about
About 78% of award paragraphs name a contractor that maps to no listed company. Most
are routine, but a competitive award to a private firm is share moving away from
someone, and the loser is often listed. When you score an unlisted awardee above
routine, the rationale MUST name the listed company the read-through is about, and
`drivers` should carry `read_through_to_prime` or `share_shift`. If you cannot name
one, the correct answer is routine.

- learned_from: announcement 4527010 — "True Zero Technologies LLC,* Fairfax, Virginia,
  was awarded a $350,000,000 ceiling … as one of nine multiple award awardees", with 26
  offers received: a competitive federal-IT services pool whose other seats are held by
  listed integrators.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.78

## R-007 · undefinitized, urgent and letter contracts are a schedule signal
An undefinitized contract action (UCA) means work started before price was agreed —
the government could not wait. That is a statement about program urgency and about
negotiating leverage, and it often precedes a much larger definitized award. Treat it
as a forward signal even when the dollar value is unremarkable; 29 of 683 paragraphs in
the backfill carry this language, so it is distinctive rather than boilerplate.

- learned_from: announcement 4560267 — "RTX Corp., Pratt and Whitney Military Engines,
  East Hartford, Connecticut, is awarded a $1,295,745,898 undefinitized contract action
  modification (P00024)".
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.82

## R-008 · foreign military sales are real revenue with a different risk profile
FMS content (49 of 683 paragraphs) is booked revenue like any other, but it carries a
different customer, different margin, and political/approval risk domestic work does
not. Do not discount an award for being FMS; do say which country or program it is for
when the text names one, because that is the part an investor cannot get from the
dollar figure.

- learned_from: announcement 4546541 — "Lockheed Martin Corp., Lockheed Martin
  Aeronautics Co., Fort Worth, Texas, is awarded a $1,603,067,858 firm-fixed-price order
  (N0001926F2171) against a previously issued basic ordering agreement (N0001924G0010)"
  carrying FMS content; and announcement 4572212, a $211,435,067 P00082 under an FMS
  case to the United Arab Emirates.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.85

## R-009 · a rule about the code: never ask the model what a threshold already answers
The pre-filter (`prefilter()`), the score→tier banding (`tier_for()`), the work queue
(`detects()`, a LEFT JOIN set difference) and the feed ordering (`top_awards()`, a
sort) are deterministic and must stay that way. The prompt asks for `tier` only because
`schemas.MATERIALITY_FIELDS` marks it `llm=True`; `_coerce()` then overrides it from the
score and caps confidence when the model contradicts itself, so a disagreement becomes
a review item instead of a silently wrong band. If a future rule here would be better
expressed as a number, put the number in the constants block, not in this file.

- learned_from: announcement 4523515 — the $860.7M asterisked award that a naive
  "small business ⇒ routine" shortcut would have silenced, and announcement 4530662 —
  the $9.4M NOC modification that a naive "listed ⇒ notable" shortcut would have
  promoted. Both are decided by explicit thresholds, not by prose.
- added: 2026-09-01T23:55Z · by: score_materiality@design · confidence: 0.90
