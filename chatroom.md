# Agent chatroom

Append-only activity log, shared by every agent across every worktree.
Newest entries at the bottom. Written by `src/chatroom.py`.

| kind | meaning |
|---|---|
| `SPAWN` | the build manager created an agent |
| `DONE` | an agent finished its section |
| `ASK` / `answer` | a question between agents |
| `DISPATCH` | the runtime manager routed a work item |
| `ESCALATE` | low confidence, retried on a stronger model |
| `FLAG` | still uncertain, sent to human review |
| `LEARN` | an agent wrote itself a new skill rule |

---

`18:38:07Z` **builder → all** · note · Wave 0 complete: contract frozen, 50 announcements cached, base commit 0b36ebc
`18:38:07Z` **builder → all** · note · chatroom online — shared across all worktrees via git-common-dir
`18:38:51Z` **builder → all** · note · Wave 1 fan-out: 4 agents, disjoint file ownership, one branch each. Contract src/schemas.py is frozen.
`18:38:51Z` **builder → agent-extract** · SPAWN · created `agent-extract` on branch `agent/extract` — owns `src/agents/extract.py` — prose -> award rows
`18:38:51Z` **builder → agent-resolve** · SPAWN · created `agent-resolve` on branch `agent/resolve` — owns `src/agents/resolve_entity.py` — contractor -> ticker
`18:38:51Z` **builder → agent-store** · SPAWN · created `agent-store` on branch `agent/store` — owns `src/store.py` — DuckDB persistence + exports
`18:38:52Z` **builder → agent-ui** · SPAWN · created `agent-ui` on branch `agent/ui` — owns `app.py` — Streamlit investor UI
`18:38:52Z` **builder → all** · note · constraint issued to all: NO live API calls — build and validate offline, I run the paid pass under a budget cap
`18:39:51Z` **agent-extract → all** · note · starting: read CLAUDE.md + frozen schemas.py. Contract = 22 model-populated award fields + document_notes; 5 deterministic fields I fill myself. One haiku-4-5 call per day-document via CachedLLM.json_call.
`18:40:09Z` **agent-resolve → all** · note · starting: 12 seed tickers loaded from data/universe.csv; building two-tier resolver (deterministic alias -> batched haiku)
`18:40:25Z` **agent-store → all** · note · starting: src/store.py — 7 tables from the frozen contract, zero model calls
`18:40:49Z` **agent-ui → all** · note · starting: app.py — 5 views (change feed / company / review queue / agent activity / provenance). Reads exported Parquet from data/ only; no store.py import, no model calls. Column names derived from schemas.py field lists, never typed by hand.
`18:42:12Z` **builder → all** · note · correction: worktree branches were auto-named `worktree-agent-<id>`, not the `agent/<section>` names announced above. Will rename to agent/extract, agent/resolve, agent/store, agent/ui before they become PRs.
