---
name: contract-check
description: Verify a section's code conforms to the frozen data contract in src/schemas.py before merging it. Use after any section agent reports done, and before merging any branch.
---

# Contract check

`src/schemas.py` is the single source of truth: one field list generates both the JSON
Schema sent to the model and the DuckDB DDL. Everything integrates only because every
section codes against it. This check is what makes that guarantee real rather than
aspirational.

## Run

```bash
.venv/Scripts/python.exe src/schemas.py          # contract still generates
.venv/Scripts/python.exe -m pytest tests/ -q     # contract test, once it exists
```

## Inspect the diff for these failures

1. **Contract edited.** `git diff main -- src/schemas.py` must be EMPTY on a section
   branch. A section that edited the contract has broken every other section; reject it.
2. **Hardcoded column lists.** Grep the diff for literal column-name lists. Columns must
   be derived from `schemas.TABLES` / `AWARD_FIELDS`. Hardcoding is exactly how the two
   projections drift apart, and it drifts silently.
3. **Hand-written DDL.** `CREATE TABLE` text anywhere but `schemas.ddl()` is a defect.
4. **Hand-written JSON Schema.** Model calls must pass `schemas.extraction_schema()` or
   `schemas.object_schema(...)`, never a literal dict.
5. **Model-populated fields confused with computed ones.** Fields with `llm=False`
   (ids, timestamps, `llm_cache_key`, `skills_version`) must NEVER appear in a prompt
   schema, and must always be filled by deterministic code.
6. **API calls outside `src/llm.py`.** `grep -rn "import anthropic" src/` should match
   `src/llm.py` and nothing else.

## Verdict

Reject and send back with specifics. A vague "please fix" wastes another round trip;
name the file, the line, and the rule.
