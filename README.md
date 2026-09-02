# DoD Contract Terminal

Department of War contract announcements are published as prose. This turns them into a
database you can query, so an investor can see what changed and whether it matters.

It fetches the announcements, AI agents read them in parallel and produce structured
rows, and company names are checked against existing logic before any of them are sent
to AI. **Companies have contracts, and contracts have events that change their value** —
creating a contract is itself an event, so the whole thing reads as one log.

## Run it

```bash
python run.py setup     # install
python run.py demo      # the whole pipeline from committed data — no API key, $0, ~35s
python run.py ui        # the terminal
```

`demo` rebuilds from scratch using the 50 committed source documents and the committed
model responses:

```
1,182 events · 1,128 contracts · 941 companies · 23 alerts
llm: 0 live calls, 101 cache hits, $0.0000
```

Same numbers as the $1.22 paid run, down to which awards escalated to the expensive
model. Nothing is stubbed — the agents genuinely run, they just read cached answers.

To prove that isn't a fixture, `python run.py trial` fetches today's announcements and
processes one document the cache has never seen, live, capped at $0.25. Needs
`ANTHROPIC_API_KEY` in `.env`. Nothing else here does.

## What I decided

- **AI is confined to judgement.** Fetching, storage, and the UI are deterministic, and
  so is working out *what changed* — that's a set difference, not worth paying for. The
  AI reads prose, resolves ambiguous company names, and judges investor relevance.
- **Names hit a dictionary before the API.** 56 of 941 contractors resolved free; every
  name resolved once is written to `entity_map.json` and never reasoned about again.
- **Every model response is cached by input hash, and the cache is committed.** That's
  why `demo` costs nothing, and why tomorrow's announcement costs cents rather than a
  full re-run.
- **A multi-agent build**, to see what a startup would do for speed — parallel work
  against a frozen data contract (`src/schemas.py`) so it integrated without conflicts.
- **Heavy weight on token cost**, more than the problem needed, to keep my own usage
  down. Batching, spend caps, and a cost estimate before anything is called.
- **The pipeline audits itself.** `run.py diagnose` runs 11 deterministic checks over
  what fetching and extraction produced, and calls a model only to ask *why* something
  looks wrong. It proposes fixes and never applies them.

## Limits I set

- No second AI verification layer over extraction.
- Low-confidence entries are detected and queued (23), but there's no tooling to work
  through the queue.
- 50 weekdays. Most modifications reference contracts awarded years earlier, so ~93%
  have no parent in the window — flagged as `history_complete = false` rather than
  pretending the contract starts at zero.

## Layout

| Path | What |
|---|---|
| `src/schemas.py` | The contract. One field list generates the model's JSON schema *and* the DuckDB DDL. |
| `src/agents/` | extract, resolve entity, score materiality, diagnose |
| `src/manager.py` | Orchestration, spend caps, change detection, contract aggregation |
| `cache/llm/` | Every model response, keyed by input hash. Committed on purpose. |
| `skills/` | Rules the agents wrote for themselves, gated by `tests/golden/` |

`python run.py test` (72 tests) · `golden` · `cost` · `diagnose`
