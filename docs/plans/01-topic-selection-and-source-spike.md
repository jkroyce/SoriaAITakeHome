# AI-Native Financial Terminal — DoD Contract Announcements

## Context

Take-home for Soria: build an AI-native system that turns public information into
structured data, analysis, and timely investor updates. Hard ceiling of **3–5 hours**,
delivered tonight. Graded on taste, systems thinking, agent coordination, and the
judgment of *where* to put agent reasoning versus deterministic code.

**Topic chosen:** daily Department of War (formerly DoD) contract announcements —
every federal contract award above $7.5M, published as prose each weekday.

**Why this source, verified by spike (not assumed):**

- The announcements are **prose**, so turning them into rows is genuine
  unstructured→structured work — the strongest possible answer to "agents should do
  meaningful work." Contrast with USAspending, which is already tabular.
- Every entry carries a **contract number** (e.g. `W15QKN-26-D-A084`), which is a join
  key into USAspending/FPDS. Every extracted row is independently verifiable against
  the system of record.
- It contains real ambiguity that defeats regex: one $160M IDIQ awarded to **seven**
  companies at once, each with its own contract number; modifications (`P00002`) with
  cumulative face values that mean something different from new awards.
- It contains the entity-resolution problem unprompted: "Rockwell Collins Inc." is RTX.
- Daily cadence means the change-detection requirement is native, not bolted on.

**Facts established during the spike (all verified live, not from memory):**

| Finding | Detail |
|---|---|
| Canonical host | `www.war.gov` — defense.gov redirects here after the department rebrand |
| Index | `/News/Contracts/`, paginated `?Page=N`, 5 pages ≈ 50 weekdays |
| Listing data | Server-rendered into Vue attrs (`article-id`, `article-title`, `article-url`) — no JS execution or API reversing needed |
| Change feed | `RSS.ashx?ContentType=400&Site=945` → "Contracts - U.S. Dept. of War", has `lastBuildDate` |
| Access | Akamai 403s sparse headers; a **complete browser header set returns 200** |
| Existing tooling | None. `mt-digital/contracts` targets a URL scheme dead since ~2015; `awesome-procurement-data` has no entry for this source |
| Local env | Bitdefender MITMs TLS on some hosts → merged CA bundle required; real Python is 3.11 (PATH `python` is LibreOffice's) |

## Architecture thesis

> **The agent reasons once; deterministic code applies that reasoning forever.**

Every model call is keyed by SHA-256 of its full input and written to `cache/llm/`.
**That cache is committed.** Consequences:

- `make demo` replays the entire pipeline with **no API key and zero spend**
- an entity resolved once is never re-reasoned
- refresh cost is proportional to *new information*, not corpus size

The cache is not a test fixture — it is the thesis made executable.

### Where the boundary falls

**Agent reasoning** (context and edge cases matter):
1. **Extraction** — prose → award rows. Handles multi-award pools, modifications vs.
   new awards, service attribution, options exercised.
2. **Entity resolution** — contractor legal name → ticker/parent. Cached per name.
3. **Materiality** — is this investor-relevant? Needs award size relative to the
   company, recompete vs. new work, ceiling vs. obligation.

**Deterministic code** (simple and repeatable): fetching, caching, provenance,
index parsing, dollar/date normalization, dedup, diffing, DuckDB, thresholds, exports, UI.

### Deliberately NOT an agent (the README calls this out explicitly)

**Change detection.** "What's new since yesterday" is a set difference on article IDs
and a field-level comparison on known keys. It is exact, cheap, and auditable. Sending
two snapshots to a model and asking what changed would be slower, non-reproducible, and
strictly worse. **Ranking by dollar value** is likewise a sort, not a judgment.
The agent is invoked only for whether a detected change *matters*, never for
whether one *occurred*.

## Current state (already built this session)

| Path | Status |
|---|---|
| `.gitignore`, `.env.example` | Done — `.env` and `certs/` ignored *before* first commit |
| `scripts/export_ca.ps1` | Done — exports Bitdefender root for the merged CA bundle |
| `scripts/export_sessions.py` | Done, verified — exports transcript as raw `.jsonl` + readable `.md`, scrubs credentials only, leaves wrong turns in. Re-run before submitting. |
| `src/config.py` | Done — hosts, browser headers, `ca_bundle()`, model IDs |
| `src/fetch.py` | Written, **not yet run** — acquisition + per-fetch provenance sidecars |
| `src/llm.py` | Done — content-addressed cache, `NotCachedError` when not `--live`, token/cost accounting |
| venv | Done — `anthropic`, `duckdb`, `requests`, `streamlit`, `pandas`, `pyarrow` |

## Remaining work

1. **Run the backfill** — `python src/fetch.py 5`. Deterministic and cheap, so pull all
   5 pages up front; extraction then runs incrementally against the cache. This makes
   depth a *runtime parameter*, not a scope commitment — if time runs short I stop
   early with zero rework.

2. **`src/extract.py`** — agent. One call per day-document (not per award) on
   `claude-haiku-4-5`, `output_config.format` with a raw JSON schema. Schema fields:
   contractor, city/state, amount, pricing type, IDIQ flag, award vs. modification,
   base contract + mod number, contract number, work description, completion date,
   contracting activity, service branch, small-business flag, competition info.
   Multi-award pools emit one row per company.

3. **`src/resolve.py`** — agent + persistent cache. Contractor name → `{ticker, parent,
   confidence, reasoning}`. Seed ~12 federal IT/defense primes (LDOS, CACI, SAIC, BAH,
   RTX, LMT, NOC, GD, LHX, HII, PLTR, ACN). Unmapped names batch to the model; results
   persist to `data/entity_map.json`. Escalates to `claude-opus-5` only on low
   confidence — this is the "minimal spend" lever.

4. **`src/materiality.py`** — agent. Scores each award 0–100 with a one-line investor
   rationale. Deterministic code applies thresholds and does the filtering.

5. **`src/store.py`** — DuckDB schema (`announcements`, `awards`, `entities`,
   `materiality`, `provenance`) + Parquet/CSV exports committed to `data/`.

6. **`src/refresh.py`** — poll RSS, diff by article ID, extract only new days,
   resolve only new names, write a digest. This is the "keeping it current" deliverable.

7. **`app.py`** — Streamlit. Company view (awards, totals, trend) + change feed
   (recent, materiality-ranked) + provenance drill-down to the source URL and fetch time.

8. **`Makefile`** — `make demo` (offline replay), `make live`, `make fetch`, `make ui`.

9. **`README.md`** — treated as a deliverable, not documentation. Must state the
   agent/deterministic boundary and why, the deliberate non-agent choice above, and a
   **unit economics** section:
   - cold backfill ≈ 50 Haiku calls ≈ **$1–2**
   - steady-state daily refresh ≈ 1–2 calls ≈ **under $0.05/day**
   - because everything already seen is a cache lookup

10. **Re-run `scripts/export_sessions.py`**, then commit.

## Verification

- `make demo` from a clean clone with **no `ANTHROPIC_API_KEY`** completes end-to-end —
  this is the reviewer's two-minute path and the single most important check.
- Spot-check 3 extracted rows against their source HTML, including the 7-company IDIQ
  pool and the $180M modification, confirming multi-award and mod handling.
- Confirm a sample contract number resolves in USAspending `spending_by_award`
  (`POST /api/v2/search/spending_by_award/`, filter `award_type_codes` A/B/C/D).
- Assert every `awards` row joins to a `provenance` row with a URL and fetch timestamp.
- `make live` on an already-populated cache should make **zero** API calls — proves the
  cache-hit path and the incremental-cost claim.

## Explicitly out of scope

Broader verticals, more than ~12 tickers, backtesting, email/Slack alert delivery
(alerts are a digest file plus the UI feed), and authentication. Scope discipline is
itself part of what is being graded.
