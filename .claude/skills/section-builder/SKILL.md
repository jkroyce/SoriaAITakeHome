---
name: section-builder
description: Write the brief for a section agent and spawn it on its own branch in an isolated worktree. Use when fanning out parallel build work across sections of this project.
---

# Spawning a section agent

Parallel agents fail for one reason: shared mutable state. Everything here exists to
remove it.

## Preconditions — verify all four before spawning anything

1. **The contract is frozen.** `src/schemas.py` exists and no agent will edit it. If it
   is still in flux, fanning out means agents code against a moving target.
2. **A base commit exists.** Worktrees branch from a commit; there must be one.
3. **File ownership is disjoint.** Two agents must never be able to touch the same
   file. If two sections need the same file, they are one section.
4. **Fixtures exist.** Agents need real data, not imagined data. Gitignored files do
   NOT appear in a worktree checkout — pass absolute paths into the main checkout.

## The brief

Every section brief carries, in this order:

- **What to read first** — `CLAUDE.md`, then `src/schemas.py`, then the specific
  modules it will call. Named explicitly, by path.
- **Exactly one owned file**, stated as such, plus "do not modify any other file".
- **The cost constraint** — `NO LIVE API CALLS`. Agents build and validate offline;
  the builder runs the paid pass afterwards under a budget cap. Without this line,
  four agents will each independently spend the user's money.
- **Real cases from the actual data**, quoted concretely. "Handle edge cases" produces
  nothing; "a $160M IDIQ awarded to seven companies, each with its own contract
  number" produces correct code.
- **Offline acceptance tests** — what it can prove for free, listed as commands.
- **The interpreter path.** `.venv/Scripts/python.exe`; the `python` on PATH is
  LibreOffice's and will fail confusingly.
- **Chatroom instructions** — post at start, at blockers, and when done. This is how
  the user sees the work happening.
- **Honesty clause** — report what is untested rather than claiming it works.

## Spawning

Use the Agent tool with `isolation: "worktree"` so each agent gets its own checkout and
branch. Post a `SPAWN` line to the chatroom for each, so creation is visible:

```python
import chatroom
chatroom.spawn("agent-extract", "agent/extract", "src/agents/extract.py", "prose -> award rows")
```

## After they return

Do not merge on the agent's say-so. Run `contract-check`, then `golden-verify`, then
read the diff yourself. An agent reporting success is a claim, not evidence.
