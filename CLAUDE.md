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
2. **Never edit `src/schemas.py` on your own initiative.** It is the frozen contract
   that lets parallel work integrate. If a field is genuinely missing, stop and say so.
   Only the project owner unfreezes it, and every change is recorded in the CHANGES log
   at the bottom of that module. Before proposing one, know which kind it is:
   adding an `llm=False` field leaves every prompt schema byte-identical, so the
   committed cache still replays and the change is free; adding or altering an
   `llm=True` field invalidates that agent's cache and forces re-extraction at real
   cost. Assert the former with a test rather than assuming it.
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
| `src/store.py` (migration) | Agent C | `_add_missing_columns` reconciles an existing DB with the contract. Additive only — never drops or retypes. |
| `src/config.py` | shared, rarely | Hosts, TLS/CA, impersonation, model ids |
| `src/llm.py` | shared, rarely | `CachedLLM`, cost accounting. The only API caller. |
| `src/fetch.py` | Section 1 | Deterministic acquisition + provenance sidecars |
| `src/agents/extract.py` | Agent A | Prose → award rows |
| `src/agents/resolve_entity.py` | Agent B | Contractor → ticker |
| `src/agents/score_materiality.py` | Agent E | Investor relevance |
| `src/agents/diagnose.py` | Agent F | Reads what acquisition and extraction produced, diagnoses the root cause when something is wrong, and **proposes** the fix. Never applies one. |
| `src/store.py` | Agent C | DuckDB init, upserts, exports. **No model calls.** |
| `app.py` | Agent D | Streamlit UI |
| `src/manager.py` | Wave 2 | Runtime orchestrator |
| `skills/*.md` | agents write these | Learned procedural rules |
| `tests/golden/` | Wave 2 | Hand-verified fixtures that gate skill promotion |

## The domain: Company → Contract → Event

```
Company  1 ──*  Contract  1 ──*  Event
entities        contracts        awards
```

A company holds contracts; **events change a contract's value**, and the earliest event
we hold reads as the contract being created. This is what the UI presents and what the
tables encode.

`awards` was always the *event* table — its rows are actions (award, modification,
option exercise, order), never durable objects. `award_uid` identifies a line in a press
release, so one contract with six actions was six unrelated rows. `contracts` (schema
1.1.0) is the aggregate root those events belong to.

Every contract column is derived by `GROUP BY`/`MIN`/`MAX`/`SUM` in `manager.build_contracts`.
**There is no `llm=True` field on `contracts` and there must never be one** — a contract
is a fact about rows we already hold, not a judgement. The test asserts it.

Three derived columns carry the membership, all `llm=False`:

| Column | Meaning |
|---|---|
| `awards.contract_uid` | which contract this event acts on — prefers `base_contract_number`, so a modification lands on the vehicle it amends |
| `awards.is_creating_event` | earliest surviving event on the contract; what "contract created" means when the source never printed one |
| `awards.duplicate_of` | a re-announcement of an identical earlier event, excluded from contract totals |

Measured on the 50-document corpus: **1,128 contracts from 1,166 events**, 16 duplicates
excluded, 24 with multi-event timelines, and **zero contracts whose events resolve to
more than one company** — so single ownership is a property of the data, not an
assumption. 197 contracts open before our window; `history_complete=false` says so
rather than implying a zero opening value.

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

## The pipeline watching itself

`src/agents/diagnose.py` is the only agent whose subject is the system rather than the
data. The others ask "what does this document say?"; this one asks "is the thing that
read it still correct?" Source drift is silent — war.gov rewrites a header and the
extractor keeps returning rows that are quietly wrong — and nothing else looks for it.

Same three tiers as everywhere else: deterministic checks find the symptoms (11 SQL
checks over what is already stored, so a healthy pipeline costs **$0 and makes no
call**), and a model is asked only the question a threshold cannot answer — *why*.
`source_changed`, `extractor_gap`, `schema_gap`, `data_quality`, or `transient`.

**What it may not do, and why that is the design.** It may propose a rule for
`skills/extraction.md`, which then faces the same golden gate as any other rule. It may
**not** edit `src/schemas.py` — frozen, owner-only — and it may **not** edit any other
source file. It writes a proposal to `data/diagnosis/` and a `review_queue` row.

This agent judges the output of the very code it would be editing. A wrong diagnosis
that auto-applied would erase its own evidence, and nobody would have seen the change.
The value here is a correct diagnosis in front of a human quickly, not an unattended
commit. Its selftest asserts the restraint rather than trusting it: exactly one
`write_text` in the runtime code, no `unlink`, no `rmtree`, no write path to the
contract.

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
