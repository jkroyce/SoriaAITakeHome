# skills/diagnosis.md — learned pipeline-diagnosis rules

Append-only. Every rule cites the case that motivated it. These rules are injected
verbatim into the diagnosis prompt, which means **editing this file changes the cache
key and forces re-diagnosis.** Promotion is gated (`manager promote-skills`) and a
candidate rule is accepted only if it does not regress `tests/golden/`.

Rule ids are stable. Never renumber, never delete — supersede.

---

## D-001 · a null field is not evidence of a broken extractor

Most nulls in `awards` are the source not saying something, not the extractor failing
to read it. `completion_date`, `pricing_type`, `bids_solicited` and
`cumulative_face_value_usd` are absent from the majority of entries because the
announcement simply does not state them, and skills rule R-007 explicitly forbids
inferring a bid count that was never printed.

Diagnose `data_quality` unless the field is one the source states for **every** entry.
The two that genuinely always appear are `amount_usd` (only awards above $7.5M are
published, and each states its figure) and the contractor name.

- learned_from: the 50-document corpus, where 15 of 1,182 awards carry no
  `amount_usd` while hundreds legitimately carry no `completion_date`
- added: 2026-09-02 · by: diagnose@haiku-4-5 · confidence: 0.9

## D-002 · a modification with no base contract is an extractor gap, not a source change

Skills rule R-002 requires `base_contract_number` on every `modification` and
`option_exercise`. The announcements have stated the modified contract consistently for
the whole corpus, so a violation is a **phrasing the rules do not cover** rather than
war.gov changing format — unless the count is large and sudden, which would be the
opposite signal.

Prefer `extractor_gap` and propose a concrete addition to `skills/extraction.md`.
Escalate to `source_changed` only when the affected rows are concentrated in the most
recent documents; a defect spread evenly across dates is a rules gap, because the
source did not change on every date at once.

- learned_from: 3 modifications with no base contract, spread across the corpus rather
  than clustered in recent documents
- added: 2026-09-02 · by: diagnose@haiku-4-5 · confidence: 0.85

## D-003 · `service_branch = OTHER` is usually correct behaviour, not a defect

Skills rule R-005 deliberately maps unrecognised ALL-CAPS headers to `OTHER` and keeps
the literal text in `extraction_notes`. U.S. SPECIAL OPERATIONS COMMAND, DARPA, DEFENSE
HEALTH AGENCY and WASHINGTON HEADQUARTERS SERVICES are all expected to land there.

Diagnose `schema_gap` **only** when a header appears often enough to deserve its own
enum value, and say plainly that adding one requires the project owner to unfreeze
`src/schemas.py`. Otherwise this is working as designed and needs no change at all —
"no change warranted" is a valid and useful diagnosis.

- learned_from: 57 awards under OTHER across the corpus, every one an agency the enum
  never claimed to cover
- added: 2026-09-02 · by: diagnose@haiku-4-5 · confidence: 0.92

## D-004 · distinguish acquisition failures by whether text arrived

A non-200 and an empty body look similar in a report and have opposite causes:

- **HTTP 403 with no body** — Akamai rejected the TLS handshake. The `curl_cffi`
  impersonation profile in `src/fetch.py` is stale. `source_changed`.
- **HTTP 200 with `body_chars` near zero** — the fetch succeeded and the *parser*
  missed. The article markup moved and the selector needs updating. Also
  `source_changed`, but a different file and a different fix.
- **A scattered single failure with neither pattern** — `transient`; retry before
  changing anything.

Never propose a code change for a single isolated failure.

- learned_from: the verified source facts in CLAUDE.md — `requests` with perfect
  headers still gets 403 because the handshake is fingerprinted, not the headers
- added: 2026-09-02 · by: diagnose@haiku-4-5 · confidence: 0.88
