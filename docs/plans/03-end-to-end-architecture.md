# DoD Contract Terminal — end-to-end architecture

## Context

Take-home for Soria: an AI-native system turning public information into structured
data, analysis, and timely investor updates. Ceiling **3–5 hours**, tonight. Graded on
taste, systems thinking, **simple but powerful abstractions**, **coordinating agents
efficiently**, and quality control.

Source (verified live last session): **daily Department of War contract announcements**
— every federal award above $7.5M, published as prose each weekday.

Five sections, per your decomposition:

1. Extraction of raw data
2. DuckDB persistence
3. Cleaning via agents
4. UI for viewing data and receiving updates
5. Agent management

### Verified source facts (agents must not re-spike these)

| Fact | Detail |
|---|---|
| Host | `www.war.gov` — `defense.gov` redirects here after the rebrand |
| Index | `/News/Contracts/`, paginated `?Page=N`; 5 pages ≈ 50 weekdays |
| Listing data | Server-rendered into Vue attrs (`article-id`, `article-title`, `article-url`) — no JS, no API reversing |
| Change feed | `RSS.ashx?ContentType=400&Site=945`, carries `lastBuildDate` |
| Access | Akamai 403s sparse headers; **complete browser header set returns 200** |
| Prior art | None. `mt-digital/contracts` targets a URL scheme dead since ~2015 |
| Local env | Bitdefender MITMs TLS on some hosts → merged CA bundle; real Python is `.venv/Scripts/python.exe` (3.11) |

### Known defects to fix

1. `scripts/export_sessions.py:27` hardcodes one transcript dir. The session is split
   across `C--Users-jroyce/` (design) and `c--Workspace-SoriaProblem/` (build) — it
   currently drops half the deliverable. Must enumerate both.
2. `fetch.py` has never run. No manifest, empty cache, no commits.

## Core thesis

> **An agent reasons once; deterministic code applies that reasoning forever.**

Applied at three levels, each producing a committed, reviewable artifact:

| Level | Artifact | What is learned |
|---|---|---|
| Response | `cache/llm/<sha>.json` | this exact call's answer |
| Facts | `data/entity_map.json` | "Rockwell Collins Inc." → RTX |
| **Procedures** | `skills/*.md` | "a multi-award pool emits one row per company" |

The third level is the self-improvement layer: agents write rules for themselves, and
those rules are markdown in git — not hidden state.

## Section 1 — Extraction

`src/fetch.py` (written, unrun): deterministic acquisition, browser headers, per-fetch
provenance sidecars (URL, `fetched_at`, sha256, status, bytes).

`src/agents/extract.py`: one `claude-haiku-4-5` call per **day-document** (not per
award) through `CachedLLM`, `output_config.format` with the generated JSON Schema.
Must handle the cases verified in the real prose: a $160M IDIQ split across **seven**
companies → seven rows; a $180M `P00002` modification with cumulative face value →
`action_type=modification`; the `*` small-business marker; branch attribution from
section headers.

## Section 2 — DuckDB persistence

`src/schemas.py` is **the contract** and the centrepiece abstraction. One field list
generates *both* the JSON Schema sent to the model *and* the DuckDB DDL, so they cannot
drift:

```python
@dataclass(frozen=True)
class Field:
    name: str; sql: str; json: dict; doc: str
    llm: bool = True          # does the model populate this?

AWARD_FIELDS: list[Field] = [...]
def json_schema(fields) -> dict   # -> output_config.format
def ddl(table, fields) -> str     # -> CREATE TABLE
```

Frozen once written. No agent edits it. A contract test asserts both generators stay
in sync.

**Tables.** `announcements` (per daily release, with fetch provenance) · `awards`
(one row per contractor-award; `award_uid` = `sha256(announcement_id|contract_number|contractor_raw)[:16]`)
· `entities` (resolution cache) · `materiality` · `changes` (the feed) ·
**`agent_runs`** (every dispatch the manager made: agent, model, item, confidence,
tokens, cost, cache hit, skills version) · **`review_queue`** (flagged low-confidence
rows).

Every AI-derived row carries `llm_cache_key`, so **any value traces back to the exact
cached model call that produced it** — provenance extended through the reasoning layer.

`src/store.py`: `init_db()` from `schemas.ddl()`, idempotent upserts, Parquet + CSV
export. Pure deterministic code, no model calls.

## Section 3 — Cleaning agents

Each cleaning agent is one file implementing a three-method protocol:

```python
class CleaningAgent(Protocol):
    name: str
    def detects(self, conn) -> list[WorkItem]:  ...   # what work do I see?
    def run(self, items, llm) -> list[Result]:  ...   # do it
    def skills_path(self) -> Path:              ...   # my learned rules
```

Registry ships with: `resolve_entity` (contractor → ticker; deterministic alias pass
against `data/universe.csv` first, model only for unmatched), `score_materiality`
(0–100 + investor rationale), `validate_award` (cross-check `contract_number` against
USAspending `spending_by_award`). Adding a cleaning agent = adding one file. That is
the abstraction the manager is built on.

**Coverage:** extract *every* award; resolution tags which map to a public ticker.
A private firm beating a prime on a recompete is signal. Seed `data/universe.csv` with
12 primes (LMT, RTX, GD, NOC, LHX, BA, HII, LDOS, CACI, SAIC, BAH, PLTR); the resolver
may add any public ticker it encounters.

## Section 4 — UI and updates

`app.py` (Streamlit): company view (awards, totals, trend) · change feed
(materiality-ranked) · **review queue** (low-confidence rows needing a human) ·
**agent activity** (from `agent_runs` — what ran, what it cost, what it learned) ·
provenance drill-down to source URL and fetch timestamp. Reads exported Parquet so it
never blocks on the pipeline.

## Section 5 — Agent management

Two managers, both deliverables.

### 5a. Runtime manager — `src/manager.py`

The only orchestrator. `manager tick` is the whole refresh cycle, so cron calls one
command:

1. **Survey** — poll RSS, diff by announcement id, ask every registered agent
   `detects(conn)`.
2. **Queue** — build a work queue in dependency order (extract → resolve → score → validate).
3. **Dispatch** — route each item. **Routing is deterministic**: an award with no
   ticker goes to the resolver because that is a lookup, not a judgment. Only genuinely
   ambiguous items reach a triage agent.
4. **Escalate** — low confidence from Haiku retries on `claude-opus-5`; still
   uncertain → `review_queue`, surfaced in the UI.
5. **Record** — every dispatch into `agent_runs` with tokens, cost, and skills version.

### 5b. Build-time manager — `.claude/` skills + section briefs

Spawns Claude Code subagents to write and test the five sections, with **its own skill
set** in `.claude/skills/`:

| Skill | Does |
|---|---|
| `section-builder` | Brief format for a section agent: frozen contract, fixture paths, file ownership, done-test |
| `contract-check` | Verify a section's output validates against `schemas.py` before accepting it |
| `golden-verify` | Run the golden set and report regressions |
| `cost-guard` | Refuse to run anything that would exceed the configured spend cap |

Fan-out is safe because **file ownership is disjoint** — no two agents ever touch the
same file, and `schemas.py` is frozen before any of them start.

## The self-improvement layer

When an agent handles a case badly — low confidence, a validation failure, or a
correction — it distills what it learned into an **append-only, versioned rule**:

```markdown
## R-007 · multi-award IDIQ pools
When one paragraph lists several companies each with their own contract number
before a single shared dollar figure, emit one row per company with
`is_multi_award=true`; the amount is the shared ceiling, not per-company.

- learned_from: announcement 4586879 (2026-08-31)
- added: 2026-09-01T21:14Z · by: extract@haiku-4-5 · confidence: 0.91
```

`skills/extraction.md` and friends are injected into the agent's prompt. Rules are
human-readable, git-diffable, and individually deletable.

**Guardrails, because a self-modifying prompt can silently degrade:**

- Every rule cites the specific case that motivated it.
- **Promotion is an explicit gated step** (`manager promote-skills`), never automatic
  on a normal run — see the cost consequence below.
- A candidate rule is accepted only if it does not regress the **golden set**
  (`tests/golden/` — a handful of hand-verified extractions including the seven-company
  pool and the $180M modification). Without this, "self-improvement" is unfalsifiable.
- Rules files are size-capped so prompts, and cost, cannot grow without bound.

**Cost consequence, stated plainly:** the skills file is part of the prompt, and the
cache is keyed on the prompt. **Updating a skill invalidates that agent's cache and
forces re-extraction.** That is why promotion is gated and batched, and why
`manager promote-skills` reports the re-extraction cost *before* doing it.

## Cost control (your API key, not the reviewer's)

- Haiku for all bulk work; Opus only on low-confidence escalation.
- `CachedLLM` never re-calls on a cache hit; the cache is committed.
- **Hard spend cap.** `--max-spend` (default $5) aborts the run when the projected
  cost of the queue exceeds it. `manager tick --dry-run` prints the planned queue,
  call count, and estimated cost **without calling anything**.
- Every run prints a cost summary; `agent_runs` makes spend queryable per agent.
- Expensive exploratory work goes to **build-time Claude Code subagents (subscription)**
  rather than **API calls (your key)**.

Expected: cold backfill ≈ 50 Haiku calls ≈ **$1–2**. Steady-state daily tick ≈ 1–2
calls ≈ **under $0.05/day**. A skills promotion is the expensive event — re-extraction
of affected announcements, priced and confirmed before it runs.

## Build sequence

**Wave 0 — serial, me, no agents.** The contract must exist before anything forks.
`src/schemas.py` → freeze · `CLAUDE.md` (thesis, boundary rule, verified source facts,
hard rules: *never call the API outside `llm.py`; never edit `schemas.py`; always route
through `CachedLLM`; browser headers mandatory; use `.venv`*) · `.claude/skills/` (the
four build-time skills) · `data/universe.csv` · fix `export_sessions.py` · run
`fetch.py 5` for real fixtures · first commit.

**Wave 1 — 4 subagents, disjoint files.** A: `src/agents/extract.py` · B:
`src/agents/resolve_entity.py` · C: `src/store.py` · D: `app.py`.

**Wave 2 — serial, depends on Wave 1.** `src/manager.py` · `src/agents/score_materiality.py`
· `src/agents/validate_award.py` · `skills/*.md` seeds · `tests/golden/` · `Makefile`
(`demo`, `live`, `tick`, `ui`, `test`) · **`README.md`** — a deliverable, not docs:
states the agent/deterministic boundary, the deliberate non-agent choice below, the
three-level learning model, and the unit economics.

**Deliberately NOT an agent (README calls this out):** change detection. "What is new"
is a set difference on award UIDs. Ranking by dollar value is a sort. The agent judges
whether a change *matters*, never whether one *occurred*. Routing in the manager is
deterministic for the same reason.

## Verification

- `make demo` from a clean clone with **no `ANTHROPIC_API_KEY`** completes end-to-end.
- `manager tick --dry-run` makes **zero** API calls and prints a costed plan.
- `make live` on a warm cache makes **zero** API calls.
- Contract test: JSON Schema and DDL stay in sync with `schemas.py`.
- Golden set passes; a deliberately bad rule is caught and rejected by `golden-verify`.
- Spot-check the seven-company IDIQ pool and the $180M modification against source HTML.
- A sample `contract_number` resolves in USAspending.
- Every `awards` row joins to provenance; every AI-derived row has a resolvable
  `llm_cache_key`; every `agent_runs` row has a cost.
- Session export produces transcripts from **both** project directories.

## Scope reality

This is more than 3–5 hours if everything is built to depth. Order of sacrifice, so the
core always ships: `validate_award` (USAspending cross-check) drops first, then the
triage agent (routing stays fully deterministic), then automated skill promotion (seed
rules by hand, keep the mechanism and the golden set). **Never cut:** the schema
contract, the committed cache, provenance, `--dry-run`, and the README.

## Out of scope

Other verticals, backtesting, email/Slack delivery (alerts = digest + UI feed),
authentication, and universe expansion beyond what the resolver meets naturally.
