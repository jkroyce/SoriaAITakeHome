## R-NOOP-000 · a control rule that changes nothing

Not a real rule and never promoted. It exists so the gate can be shown to
DISCRIMINATE: a candidate that does not change the extraction must come back
UNPROVEN, not REJECT. Without this control, a gate that rejected everything would
look identical to a gate that works.

---

When an entry's `work_description` runs longer than one sentence, keep the first
sentence and drop the rest.

- learned_from: control case for tests/test_golden.py; no announcement motivated it
- added: 2026-09-01T00:00Z · by: golden-set · confidence: n/a

### Expected gate outcome

UNPROVEN. No golden check asserts `work_description`, so the golden set cannot show
this rule helps or harms. Per `.claude/skills/golden-verify/SKILL.md`, an unproven rule
is kept only if it addresses a case the golden set does not cover, and the person
promoting it has to say which one.
