# DoD Contract Terminal — schema contract, CLAUDE.md, and parallel build

## Context

Take-home for Soria: an AI-native system turning public information into structured
data, analysis, and timely investor updates. Hard ceiling **3–5 hours**, tonight.
Graded on taste, systems thinking, **simple but powerful abstractions**, and
**coordinating agents efficiently**.

Topic (chosen and source-verified last session): **daily Department of War contract
announcements** — every federal award above $7.5M, published as prose each weekday.

This plan covers the remaining scope. Its organizing idea: **the schema is the
coordination contract.** Parallel agents collide when they share mutable state, so the
schema and the file-ownership map get frozen *before* any agent starts, and no two
agents ever touch the same file.

### Verified source facts (established live last session — agents must not re-spike)

| Fact | Detail |
|---|---|
| Host | `www.war.gov` — `defense.gov` redirects here after the department rebrand |
| Index | `/News/Contracts/`, paginated `?Page=N`; 5 pages ≈ 50 weekdays |
| Listing data | Server-rendered into Vue attrs (`article-id`, `article-title`, `article-url`) — no JS execution, no API reversing |
| Change feed | `RSS.ashx?ContentType=400&Site=945` → "Contracts - U.S. Dept. of War", carries `lastBuildDate` |
| Access | Akamai 403s sparse headers; **a complete browser header set returns 200** |
| Prior art | None. `mt-digital/contracts` targets a URL scheme dead since ~2015 |
| Local env | Bitdefender MITMs TLS on some hosts → merged CA bundle; real Python is 3.11 (PATH `python` is LibreOffice's) |

### Two defects found while verifying state

1. **The transcript deliverable is broken.** The session is split across two directories
   — `C--Users-jroyce/0edc65cf…` (topic selection + source spike) and
   `c--Workspace-SoriaProblem/6a44a53b…` (current) — because the working directory
   changed. `scripts/export_sessions.py:27` hardcodes only the first, silently dropping
   the **design** half that the brief explicitly asks for. Must enumerate both.
2. **`fetch.py` has never run.** 0 articles, no manifest, empty cache, no commits.

## Architecture thesis

> **The agent reasons once; deterministic code applies that reasoning forever.**

Every model call is keyed by SHA-256 of its full input and written to `cache/llm/`,
and **that cache is committed**. So `make demo` replays the whole pipeline with no API
key and zero spend; an entity resolved once is never re-reasoned; refresh cost is
proportional to *new information*, not corpus size. The cache is the thesis, not a
fixture.

**Agent reasoning:** prose→rows extraction, contractor→ticker resolution, materiality.
**Deterministic:** fetching, provenance, index parsing, normalization, dedup, diffing,
DuckDB, thresholds, exports, UI.

**Deliberately NOT an agent (README calls this out):** change detection. "What is new"
is a set difference on award UIDs plus field comparison on known keys — exact, cheap,
auditable. Ranking by dollar value is a sort. The agent judges whether a detected
change *matters*, never whether one *occurred*.

## The contract: `src/schemas.py`

Single source of truth. One field definition list generates **both** the JSON Schema
sent to the model **and** the DuckDB DDL, so the two cannot drift.

```python
@dataclass(frozen=True)
class Field:
    name: str; sql: str; json: dict; doc: str
    llm: bool = True        # does the model populate this?

AWARD_FIELDS: list[Field] = [...]
def json_schema(fields) -> dict   # -> output_config.format
def ddl(table, fields) -> str     # -> CREATE TABLE
```

A contract test asserts every `llm=True` field appears in the generated JSON Schema and
every field appears in the DDL. **This file is frozen once written — no agent edits it.**

### Tables

**`announcements`** — one row per daily release. `announcement_id` PK, `announced_date`,
`title`, `url`, `fetched_at`, `sha256`, `http_status`, `n_bytes`, `body_chars`.

**`awards`** — one row per contractor-award (a multi-award pool emits N rows).
`award_uid` PK = `sha256(announcement_id|contract_number|contractor_raw)[:16]`,
`announcement_id` FK, `announced_date`, `service_branch`, `contractor_raw`,
`contractor_city`, `contractor_state`, `amount_usd`, `action_type`
(`new_award|modification|option_exercise|multi_award_pool`), `contract_number`,
`base_contract_number`, `modification_number`, `cumulative_face_value_usd`,
`pricing_type`, `is_idiq`, `is_multi_award`, `work_description`,
`place_of_performance`, `completion_date`, `contracting_activity`, `bids_solicited`,
`bids_received`, `small_business`, `extraction_confidence`, `extracted_at`,
`extractor_model`, `llm_cache_key`.

**`entities`** — resolution cache, one row per distinct raw name. `contractor_raw` PK,
`normalized_name`, `ticker`, `parent_company`, `relationship`
(`direct|subsidiary|joint_venture|private|unknown`), `is_public`, `confidence`,
`reasoning`, `resolved_at`, `resolver_model`, `llm_cache_key`.

**`materiality`** — `award_uid` PK/FK, `score` 0–100, `tier`
(`alert|notable|routine`), `rationale`, `drivers` JSON, `scored_at`, `scorer_model`,
`llm_cache_key`.

**`changes`** — `change_id` PK, `detected_at`, `change_type`, `award_uid`,
`announcement_id`, `prev_value`, `new_value`, `materiality_score`.

The `llm_cache_key` columns mean **every AI-derived value traces to the exact cached
model call that produced it** — provenance extended through the reasoning layer, not
just the fetch layer.

### Coverage & universe (decided)

Extract **every** award; entity resolution tags which map to a public ticker. A private
firm beating a prime on a recompete is signal, and it keeps provenance complete. Seed
`data/universe.csv` with 12 primes (LMT, RTX, GD, NOC, LHX, BA, HII, LDOS, CACI, SAIC,
BAH, PLTR); the resolver may add any public ticker it encounters, including
subsidiaries rolling up to diversified or foreign parents.

## Wave 0 — serial, I do this (no agents)

Everything downstream depends on the contract; parallelizing before it exists is how
this fails.

1. `src/schemas.py` — the contract above, then frozen.
2. `CLAUDE.md` — see below.
3. `data/universe.csv` — 12 seed tickers with known aliases.
4. Fix `scripts/export_sessions.py` — enumerate **all** `~/.claude/projects/*/` dirs
   whose transcripts belong to this work, not one hardcoded path.
5. Run `python src/fetch.py 5` — produces real fixture data so agents code against
   actual prose, not imagined prose.
6. Commit. First commit must already have `.env` and `certs/` ignored (they are).

### `CLAUDE.md` contents

Purpose and thesis; the agent/deterministic boundary as a rule; the **verified source
facts table above** (so agents never re-spike or hallucinate `defense.gov`); hard rules
— *never call the Anthropic API outside `src/llm.py`; never edit `src/schemas.py`;
always route through `CachedLLM`; browser headers are mandatory for war.gov; use
`.venv`, not PATH python*; the file-ownership map; run commands; and the cost-discipline
rule (**if you're calling the model where a sort or a set difference would do, stop**).

## Wave 1 — 4 agents in parallel, one file each

Each brief carries: the frozen schema, the CLAUDE.md rules, real fixture paths, and a
"done" test. Disjoint files, so no collisions.

| Agent | Owns | Brief |
|---|---|---|
| **A** | `src/extract.py` | Agent layer. One `claude-haiku-4-5` call per day-document via `CachedLLM.json_call`, `output_config.format`. Must handle: the 7-company $160M IDIQ pool → 7 rows; the $180M `P00002` modification with cumulative face value → `action_type=modification`; the `*` small-business marker; service-branch attribution from section headers. |
| **B** | `src/resolve.py` | Contractor→ticker. Deterministic pass against `data/universe.csv` aliases first; only unmatched names reach the model, batched. Persists `data/entity_map.json`. Escalates to `claude-opus-5` only on low confidence — the minimal-spend lever. Must map "Rockwell Collins Inc." → RTX. |
| **C** | `src/store.py` | DuckDB. `init_db()` from `schemas.ddl()`, idempotent upserts keyed on the PKs, Parquet + CSV export to `data/`. Pure deterministic code — **no model calls**. |
| **D** | `app.py` | Streamlit. Company view (awards, totals, trend), change feed (materiality-ranked), provenance drill-down to source URL + fetch timestamp. Codes against the schema; runs on exported Parquet so it never blocks on the pipeline. |

## Wave 2 — serial, depends on Wave 1 output

7. `src/materiality.py` — scores each award 0–100 + one-line investor rationale;
   deterministic thresholds do the filtering.
8. `src/refresh.py` — poll RSS, diff by announcement/award UID, extract only new days,
   resolve only new names, write digest. The "keep current + alert" deliverable.
9. `Makefile` — `demo` (offline replay), `live`, `fetch`, `ui`, `test`.
10. **`README.md`** — a deliverable, not documentation. States the agent/deterministic
    boundary and why; the deliberate non-agent choice; and **unit economics**: cold
    backfill ≈ 50 Haiku calls ≈ **$1–2**; steady-state daily refresh ≈ 1–2 calls ≈
    **under $0.05/day**, because everything already seen is a cache lookup.
11. Re-run the fixed session export, commit.

## Verification

- **`make demo` from a clean clone with no `ANTHROPIC_API_KEY` completes end-to-end.**
  This is the reviewer's two-minute path and the single most important check.
- `make live` against a populated cache makes **zero** API calls — proves the cache-hit
  path and the incremental-cost claim.
- Contract test: generated JSON Schema and DDL stay in sync with `schemas.py`.
- Spot-check 3 extracted rows against source HTML — must include the 7-company IDIQ pool
  and the $180M modification.
- A sample `contract_number` resolves in USAspending
  (`POST /api/v2/search/spending_by_award/`, `award_type_codes` A/B/C/D).
- Every `awards` row joins to an `announcements` row with URL + `fetched_at`; every
  AI-derived row carries a resolvable `llm_cache_key`.
- Session export produces transcripts from **both** project directories.

## Out of scope

Other verticals, backtesting, email/Slack delivery (alerts = digest file + UI feed),
authentication, and any ticker universe expansion beyond what the resolver encounters
naturally. Scope discipline is itself graded.
