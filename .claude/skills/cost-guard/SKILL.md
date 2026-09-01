---
name: cost-guard
description: Estimate and cap spend before any paid model run. Use before running the pipeline live, before promoting a skill, and whenever an agent is about to call the API.
---

# Cost guard

Spend here is the user's own API key, not the reviewer's. The architecture already
minimises it — every response is cached by input hash and the cache is committed — but
caching only helps on the second run. This skill governs the first.

## Prices (USD per 1M tokens)

| Model | Input | Output |
|---|---|---|
| `claude-haiku-4-5` | $1.00 | $5.00 |
| `claude-opus-5` | $5.00 | $25.00 |

Haiku for all bulk work. Opus only on low-confidence escalation. Model ids carry **no
date suffix**.

## Before any paid run

1. **Dry run first, always.**
   ```bash
   .venv/Scripts/python.exe -m src.manager tick --dry-run
   ```
   It prints the planned queue, the call count, and an estimated cost, and makes zero
   API calls.
2. **Check the cache first.** Work already in `cache/llm/` is free. Only genuinely new
   inputs cost anything. If a "new" run projects full corpus cost, something is
   invalidating the cache — find it before paying for it.
3. **Cap it.** `--max-spend` (default $5) aborts when the projected cost exceeds it.
4. **Prefer the subscription.** Exploratory and build work belongs in Claude Code
   subagents, which run against the subscription. API calls hit the user's key. Move
   expensive open-ended work to the former.

## Expected magnitudes

Cold backfill of 50 announcements: ~50 Haiku calls, roughly $1–2. A steady-state daily
tick: 1–2 calls, under $0.05. **Skill promotion is the expensive event** — it
invalidates a cache and forces re-extraction, so price it and confirm before running.

## Red flags — stop and investigate

- One model call per award instead of per day-document (≈20x the calls).
- One resolution call per contractor name instead of a batch.
- A model call deciding something a sort, a set difference, or a dict lookup would
  answer. That is a design bug, not a cost problem, and it will recur every run.
- Repeated runs that never hit cache — the prompt is carrying something volatile like a
  timestamp.
