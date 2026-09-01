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

## Orchestrator context discipline

The runtime pipeline is designed so nothing re-reads what it already knows. The same
rule applies to whoever is *building* it, and is much easier to violate — because the
orchestrator's cost is invisible until you measure it.

**Measured on this project:** the build orchestrator's own session cost more than all
four section agents combined (~$20 vs ~$3 of subagent work). Not because any single
action was expensive, but because **every turn re-reads the entire conversation**.
Cost is context size x turn count. Inflate the context once, pay for it on every
subsequent turn.

The specific failure: loading the `update-config` skill to write three lines of JSON
pulled **63,000 tokens** of settings schema into context permanently — more than any
subagent used in total, for a change that was already known from a doc lookup. A
second skill load added 19,000 more. Together, 62% of the context.

Rules:

1. **Do not load a large skill for a small, already-known change.** If a doc fetch or
   a grep already answered the question, act on it. A skill is worth its context when
   you genuinely need the procedure, not as insurance.
2. **Keep large documents out of context.** Fetch to a file and `grep` it. A 75KB doc
   read this way costs a few hundred tokens instead of 19,000 — and this project did
   exactly that for the settings docs, then failed to apply it to the skill loads.
3. **Batch turns.** Independent verification commands belong in one call. Each extra
   turn re-reads everything before it.
4. **Delegate heavy reading to a subagent.** A subagent's context is discarded when it
   finishes; the orchestrator's persists for the whole session. Work that requires
   reading a lot to produce a little is exactly what a subagent is for.
5. **Watch prompt-cache reuse, not just totals.** `python run.py usage` reports it.
   100% reuse means the prefix is stable. Falling reuse means something volatile —
   a timestamp, a changing tool list — has entered the cached region, and cost will
   climb quietly.

Run `python run.py usage` before and after a heavy stretch of work. If orchestrator
tokens are growing faster than delivered work, the fix is structural, not frugality.
