# DoD Contract Terminal

Turns the Department of War's daily contract announcements — prose — into queryable
investment data, and tells an investor when something material changed.

The announcements are published as paragraphs of text. A $160,000,000 award split
across seven companies, a $180,000,000 modification against an existing contract, and
a footnote defining an asterisk all sit in the same page, with no API and no schema.

```
"AAECON General Contracting LLC,* Louisville, Kentucky (W912QR-26-D-A044); ...
 will compete for each order of the $160,000,000 firm-fixed-price contract"
```

becomes seven rows — one per company, each with its own contract number, each carrying
the shared ceiling rather than a divided seventh, each keyed to a ticker.

## The thesis

> **An agent reasons once; deterministic code applies that reasoning forever.**

Every model call goes through `CachedLLM` and is keyed by a SHA-256 of its full input.
The cache is **committed to the repository**. That is not a test fixture — it is the
architecture. An entity resolved once is never re-reasoned, and refresh cost is
proportional to *new information*, not corpus size.

Learning happens at three levels, each a reviewable artifact in git:

| Level | Artifact | Learns |
|---|---|---|
| Response | `cache/llm/<sha>.json` | this exact call's answer |
| Facts | `data/entity_map.json` | "Rockwell Collins Inc." → RTX |
| Procedures | `skills/*.md` | "a multi-award pool emits one row per company" |

### Where reasoning goes, and where it must not

**An agent** decides what needs context and judgement: extracting rows from prose,
resolving a contractor to a ticker, judging whether a change is material.

**Deterministic code** does everything exact and repeatable: fetching, caching,
provenance, parsing, normalizing, dedup, diffing, DuckDB, thresholds, sorting, exports,
UI.

The test: *if a sort, a set difference, or a dictionary lookup would answer it, a model
call is a bug.* So "what is new" is a set difference on award UIDs and is deliberately
**not** an agent. Ranking by dollar value is a sort. The agent judges whether a change
*matters*, never whether one *occurred*.

## Quick start

`make demo` replays the entire pipeline from the committed cache with **no API key and
no spend**. That is the two-minute path.

```bash
python run.py demo       # replay everything from cache, $0.00, no key
python run.py ui         # Streamlit terminal
python run.py golden     # score extraction against hand-verified fixtures
python run.py test       # 66 tests
python run.py contract   # print the frozen schema
python run.py cost       # what the cache holds, and what replaying it saves
```

Use `.venv/Scripts/python.exe`. The `python` on PATH may be another interpreter
entirely; on the machine this was built on it is LibreOffice's, which has no pip.

To refresh against live data you need a key in `.env` (copy `.env.example`):

```bash
python run.py tick       # plan and price the queue — ZERO model calls
python run.py live       # the paid pass, hard-capped by --max-spend (default $5)
```

`tick --dry-run` replaces `CachedLLM.json_call` with a raising guard for the whole run,
so "zero calls" is enforced rather than asserted.

## Architecture

```
war.gov  ──►  fetch.py  ──►  raw/*.html + *.prov.json     deterministic, cached
                                   │
                                   ▼
                              extract.py            prose ──► award rows      (agent)
                                   │
                                   ▼
                          resolve_entity.py         name  ──► ticker          (agent)
                                   │
                                   ▼
                        score_materiality.py        award ──► 0-100           (agent)
                                   │
                                   ▼
                              store.py  ──►  DuckDB  ──►  Parquet/CSV
                                   │
                                   ▼
                                app.py                Streamlit
```

`manager.py` is the only orchestrator: survey → queue → dispatch → escalate → record.
Routing is deterministic; an award with no ticker goes to the resolver because that is
a lookup, not a judgement. Confidence below 0.7 retries on Opus, and still-uncertain
rows land in `review_queue` for a human. Every dispatch is recorded in `agent_runs`
with tokens, cost, cache hit and skills version.

Agents are **discovered**, not registered: any module in `src/agents/` exposing
`name` / `detects` / `run` / `skills_path` is picked up. Adding a cleaning agent is
adding one file.

### The contract

`src/schemas.py` is frozen. One field list generates **both** the JSON Schema sent to
the model and the DuckDB DDL, so the two projections cannot drift — and a test proves
it. Fields marked `llm=False` (ids, timestamps, `llm_cache_key`, `skills_version`)
never appear in a prompt and are always filled by code.

### Self-improvement, and why it is falsifiable

When an agent handles a case badly it distils an append-only rule into `skills/*.md`,
citing the announcement that motivated it. Those rules are injected into later prompts.

That is also the most dangerous part of the design, because a self-modifying prompt can
silently degrade. So promotion is gated:

- `manager promote-skills` is explicit, never automatic.
- A candidate is verified against a **copy**; the live file is written only once the
  gate is green. The skills file is part of the prompt and the cache is keyed on the
  prompt, so a crash between write and revert would silently invalidate every cached
  response.
- A rule is accepted only if it does not regress `tests/golden/` — hand-verified
  fixtures whose expected values are quoted from the source prose. A deliberately bad
  rule is **rejected**, and that rejection is itself a test.
- Promotion reports the re-extraction cost *before* doing it.

## Verified source facts

| Fact | Detail |
|---|---|
| Host | `www.war.gov`; `defense.gov` redirects here after the rebrand |
| Access | Akamai fingerprints the **TLS handshake**. `requests` with a perfect Chrome header set still gets 403; `curl_cffi` with `impersonate="chrome"` gets 200 |
| Listing | Server-rendered into Vue attributes — no JS execution, no API reversing |
| Threshold | Only awards above $7.5M are published |
| Prior art | None. The one comparable project targets a URL scheme dead since ~2015 |

## Cost

Haiku for bulk work, Opus only on low-confidence escalation. The cache is committed, so
replay is free and reproducible for anyone who clones the repo.

