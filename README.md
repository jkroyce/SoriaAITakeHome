# DoD Contract Terminal

This grabs Department of War contract announcements — which are published as prose —
and distills them into a database you can actually query, so an investor can see what
changed and whether it matters.

The application first fetches the announcements, then AI agents read them in parallel
and produce structured rows. Company names are checked against existing logic first,
and only the ones that logic can't resolve are sent to AI.

**The structure is: companies have contracts, and contracts have events that change
their value.** Creating a contract is itself an event, so the whole thing reads as one
log.

---

## Run it

```bash
python run.py setup      # install dependencies
python run.py demo       # the whole pipeline from committed data — no API key, $0
python run.py ui         # the terminal itself
```

`demo` rebuilds the database from scratch using the 50 committed source documents and
the committed model responses. It takes about 40 seconds, makes zero API calls, and
produces:

```
50 announcements · 1,182 events · 1,128 contracts · 941 companies
llm: 0 live calls, 56 cache hits, $0.0000
```

Those are the same numbers the real paid run produced. Nothing is stubbed — the agents
genuinely run, they just get their answers from cache instead of the API.

**Want to prove it's real on your own key?**

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env    # gitignored
python run.py trial                            # one fresh document, live, capped at $0.25
```

That fetches today's announcements and processes one document the cache has never
seen. It's the honest answer to "is the cache just a fixture?"

Full run over all 50 documents is `python run.py live` (~$2.35, capped at $5.00).

---

## What I decided, and why

**I wanted the AI confined to judgement.** The UI, the fetching, and the storage are
deterministic. So is working out *what changed* — that's a set difference on IDs, not
something worth paying a model for. The AI reads prose, resolves ambiguous company
names, and decides whether an award actually matters to an investor. Everything else
is code.

**Company names hit a dictionary before they hit the API.** Of 941 distinct
contractors, 56 resolved for free against a known-aliases list, and every name resolved
once is written to `data/entity_map.json` and never reasoned about again. Re-running
the whole corpus now costs $0.

**Every model response is cached by a hash of its exact input, and the cache is
committed.** That's why `demo` works with no key. It also means the refresh cost is
proportional to genuinely new information rather than to corpus size — tomorrow's
announcement costs a few cents, not another full run.

**I used a multi-agent flow to build it**, to see what a startup would do for speed.
Sections were built in parallel against a frozen data contract (`src/schemas.py`) so
they could integrate without merge conflicts in data form.

**I put a lot of weight on token cost and monitoring**, more than the problem strictly
needed, because I wanted to keep my own usage down. That added complexity — batching,
spend caps, a cost estimate before anything is called — but it's the constraint I
chose.

---

## Limits I set to avoid scope creep

- No second AI verification layer over the extraction.
- No real tooling for reporting or working through low-confidence entries — they're
  detected and queued (23 of them), but a human would have to go read the queue.
- 50 weekdays of announcements. Most modifications reference contracts awarded years
  earlier, so ~93% of them have no parent in the window. The system says so
  (`history_complete = false`) rather than pretending the contract starts at zero.

---

## Layout

| Path | What it is |
|---|---|
| `src/schemas.py` | The data contract. One field list generates both the JSON schema sent to the model and the DuckDB DDL. |
| `src/fetch.py` | Acquisition. war.gov fingerprints the TLS handshake, so this uses `curl_cffi`. |
| `src/agents/` | The three agents: extract, resolve entity, score materiality. |
| `src/manager.py` | Orchestration, spend caps, change detection, contract aggregation. |
| `app.py` | The Streamlit terminal. |
| `cache/llm/` | Every model response, keyed by input hash. Committed on purpose. |
| `skills/` | Rules the agents wrote for themselves after getting cases wrong. |
| `tests/golden/` | Hand-verified fixtures that gate whether a new rule is accepted. |

```bash
python run.py test       # 70 tests
python run.py golden     # extraction scored against hand-verified fixtures
python run.py cost       # what the cache holds and what it saved
```
