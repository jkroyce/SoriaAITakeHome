# CLAUDE.md — DoD Contract Terminal

An AI-native research system that turns the Department of War's daily contract
announcements (prose) into queryable investment data, and alerts investors when
something material changes.

## The thesis, and the rule that follows from it

> **An agent reasons once; deterministic code applies that reasoning forever.**

Every model call goes through `CachedLLM` and is keyed by a SHA-256 of its full input.
The cache is **committed to the repository**. This is not a test fixture — it is the
architecture. An entity resolved once is never re-reasoned. Refresh cost is
proportional to *new information*, not corpus size.

Learning happens at three levels, each a reviewable artifact in git:

| Level | Artifact | Learns |
|---|---|---|
| Response | `cache/llm/<sha>.json` | this exact call's answer |
| Facts | `data/entity_map.json` | "Rockwell Collins Inc." → RTX |
| Procedures | `skills/*.md` | "a multi-award pool emits one row per company" |

### Where reasoning goes, and where it must not

**Use an agent** when context and edge cases matter: extracting rows from prose,
resolving a contractor name to a ticker, judging whether a change is material.

**Use deterministic code** when the work is exact and repeatable: fetching, caching,
provenance, parsing index attributes, normalizing dates and dollars, dedup, diffing,
DuckDB operations, thresholds, sorting, exports, UI.

**The test:** if a sort, a set difference, or a dictionary lookup would answer it, a
model call is a bug — it costs money, adds latency, and is not reproducible.
Change detection ("what is new") is a set difference on award UIDs and is deliberately
**not** an agent. Ranking by dollar value is a sort. The agent judges whether a change
*matters*, never whether one *occurred*.

## Hard rules

1. **Never call the Anthropic API outside `src/llm.py`.** Always route through
   `CachedLLM`. No `import anthropic` anywhere else.
2. **Never edit `src/schemas.py`.** It is the frozen contract that lets parallel work
   integrate. If a field is genuinely missing, stop and say so rather than adding one.
3. **Never edit a file you do not own.** See the ownership map below.
4. **Use `.venv/Scripts/python.exe`.** The `python` on PATH is LibreOffice's bundled
   interpreter — no pip, no venv.
5. **Do not re-verify the source.** The facts below were established live. Re-spiking
   wastes turns and risks acting on a stale guess.
6. **Cost discipline.** Haiku (`claude-haiku-4-5`) for bulk work; Opus
   (`claude-opus-5`) only on low-confidence escalation. Model ids carry **no date
   suffix**.
7. **Report honestly.** If something fails, say so with the output. Do not describe
   unrun code as working.

## Verified source facts — do not re-derive

| Fact | Detail |
|---|---|
| Host | `www.war.gov` — `defense.gov` redirects here after the department rebrand. Never target defense.gov. |
| Index | `/News/Contracts/`, paginated `?Page=N`; 5 pages ≈ 50 weekdays |
| Listing data | Server-rendered into Vue attributes (`article-id`, `article-title`, `article-url`). No JS execution, no API reversing. |
| Change feed | `RSS.ashx?ContentType=400&Site=945` → "Contracts - U.S. Dept. of War", carries `lastBuildDate` |
| **Access** | Akamai fingerprints the **TLS handshake**, not just headers. `requests` with a perfect Chrome header set still gets 403; `curl_cffi` with `impersonate="chrome"` gets 200. Always fetch through `src/fetch.py`. |
| Threshold | Only awards above $7.5M are published |
| TLS | Bitdefender MITMs some hosts locally → always use `config.ca_bundle()`, never `verify=False` |
| Prior art | None. `mt-digital/contracts` targets a URL scheme dead since ~2015. |

### Real cases the extractor must handle

Taken from the live prose, not invented:

- A **$160M IDIQ awarded to seven companies at once**, each with its own contract
  number, competing for orders under one ceiling → seven rows, `is_multi_award=true`.
- A **$180M modification (`P00002`)** against an existing contract, stating a
  *cumulative face value* of $280M → `action_type=modification`, not a new award.
- A trailing **`*`** marks a small-business award.
- Service branch comes from an ALL-CAPS section header (`ARMY`, `NAVY`, …).
- "Rockwell Collins Inc." is **RTX** — the entity-resolution problem, live on day one.

## Layout and file ownership

Exactly one owner per file. Do not cross these lines.

| Path | Owner | Purpose |
|---|---|---|
| `src/schemas.py` | **frozen** | The contract. Generates JSON Schema *and* DuckDB DDL from one field list. |
| `src/config.py` | shared, rarely | Hosts, TLS/CA, impersonation, model ids |
| `src/llm.py` | shared, rarely | `CachedLLM`, cost accounting. The only API caller. |
| `src/fetch.py` | Section 1 | Deterministic acquisition + provenance sidecars |
| `src/agents/extract.py` | Agent A | Prose → award rows |
| `src/agents/resolve_entity.py` | Agent B | Contractor → ticker |
| `src/agents/score_materiality.py` | Agent E | Investor relevance |
| `src/store.py` | Agent C | DuckDB init, upserts, exports. **No model calls.** |
| `app.py` | Agent D | Streamlit UI |
| `src/manager.py` | Wave 2 | Runtime orchestrator |
| `skills/*.md` | agents write these | Learned procedural rules |
| `tests/golden/` | Wave 2 | Hand-verified fixtures that gate skill promotion |

## Cleaning-agent protocol

Every cleaning agent is one file implementing three methods. Adding an agent means
adding a file — the manager needs no changes.

```python
class CleaningAgent(Protocol):
    name: str
    def detects(self, conn) -> list[WorkItem]: ...   # what work do I see?
    def run(self, items, llm) -> list[Result]:  ...  # do it
    def skills_path(self) -> Path:              ...  # my learned rules
```

Return a `confidence` on every result. Below **0.7** the manager escalates to Opus;
still low, it goes to `review_queue` for a human. Be honest about confidence — a
truthful 0.5 is far more useful than a confident wrong answer.

## Writing skills for yourself

When you handle a case badly, distil what you learned into an append-only rule in your
`skills/*.md`. Rules are injected into later prompts.

```markdown
## R-007 · multi-award IDIQ pools
When one paragraph lists several companies each with their own contract number before
a single shared dollar figure, emit one row per company with `is_multi_award=true`;
the amount is the shared ceiling, not per-company.

- learned_from: announcement 4586879 (2026-08-31)
- added: 2026-09-01T21:14Z · by: extract@haiku-4-5 · confidence: 0.91
```

Every rule must cite the case that motivated it. **Promotion is a gated step**
(`manager promote-skills`), never automatic: the skills file is part of the prompt and
the cache is keyed on the prompt, so changing a skill invalidates that agent's cache
and forces re-extraction. A candidate rule is accepted only if it does not regress
`tests/golden/`.

## Commands

```bash
.venv/Scripts/python.exe src/fetch.py 5      # backfill 5 index pages
.venv/Scripts/python.exe src/schemas.py      # print the contract
make demo                                    # replay everything from cache, no API key
make tick                                    # one refresh cycle
make ui                                      # Streamlit
```

`make demo` **must** work with no `ANTHROPIC_API_KEY` set. That is the reviewer's
two-minute path, and it is also what keeps development from re-spending on every run.
