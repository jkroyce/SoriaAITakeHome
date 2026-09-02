# Handoff — current state

Read this, then `CLAUDE.md`, then `docs/plans/03-end-to-end-architecture.md`. That is
enough to continue. Do not re-derive anything below; it is all verified.

## Where things stand

**Wave 0 complete** (`main`, 6 commits through `ca2ee6d`):

- `src/schemas.py` — the frozen contract. One field list generates both the JSON
  Schema sent to the model and the DuckDB DDL. **Never edit it.** 8 contract tests pass.
- `src/fetch.py` — acquisition. Uses `curl_cffi` with `impersonate="chrome"`; Akamai
  fingerprints the TLS handshake, so plain `requests` gets 403 even with perfect headers.
- `src/llm.py` — `CachedLLM`. The only thing allowed to call the API.
- `src/chatroom.py` + `ai-sessions/chatroom.md` — shared agent activity log, resolves to the main
  repo root from inside any worktree.
- `scripts/agent_usage.py` — token/spend tracking, build-time vs runtime, spike detection.
- `scripts/export_sessions.py` — exports transcripts from BOTH project directories.
  Re-run before submitting.
- `run.py` — task runner (`make` is not installed here; the Makefile delegates to it).
- `.claude/skills/` — the build manager's four skills.
- 50 announcements cached, 2026-06-22..2026-08-31, with provenance sidecars committed.

**Wave 1 — three of four done, on branches, NOT merged:**

| Branch | Contents | Verified |
|---|---|---|
| `agent/store` | `src/store.py` | contract untouched, no model calls, 50-row idempotent load |
| `agent/extract` | `src/agents/extract.py`, `skills/extraction.md` (R-001..R-009) | contract untouched, 55 offline checks |
| `agent/ui` | `app.py` | contract untouched, no forbidden imports, AppTest 10/10 |

`agent-resolve` was **stopped mid-work**; uncommitted changes sit in
`.claude/worktrees/agent-ab4f173510f1b3303`. It still needs `src/agents/resolve_entity.py`.

## Live constraint for the resolver

`extract` rule R-004 keeps the small-business `*` in `contractor_raw` ("exactly as
printed"), and `store` keys `entities` on that same string. So the resolver must keep
`entities.contractor_raw` RAW (so the join to `awards` is exact) while keying its own
cache and its model batching on the NORMALIZED name — otherwise one company splits into
two entity keys and is paid for twice.

## Next

1. Restart the resolver with the constraint above.
2. Merge the four branches (files are disjoint; expect clean merges).
3. Wave 2: `src/manager.py`, `src/agents/score_materiality.py`, `tests/golden/`, README.
4. Then the paid pass: smoke-test announcement `4586879` alone first (~$0.02), then
   all 50 (~$2). `python run.py usage` before and after.

## Money

**API spend so far: $0.00.** The cache is empty; nothing has been paid for. Every agent
ran under a hard no-live-API-calls constraint and all of them respected it.

Build-time (subscription) is ~$24, of which the orchestrator's own session is ~$21 —
more than all four section agents combined. Cause and fix are documented in
`.claude/skills/cost-guard/SKILL.md` under "Orchestrator context discipline". Apply it:
delegate heavy reading to subagents, batch turns, do not load large skills for small
known changes.
