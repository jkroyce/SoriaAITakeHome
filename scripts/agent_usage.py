"""Token and spend accounting for every agent that worked on this project.

Answers two questions: which agent is the most expensive, and is any of them
spiking? Both matter because this project's whole thesis is that agent reasoning
should be a one-time cost, and the first sign that something is wrong -- a model
being asked to do what a sort or a dict lookup should do -- is usage climbing when
it ought to be flat.

TWO SEPARATE BUDGETS, deliberately reported apart:

  * BUILD-TIME  -- Claude Code sessions and subagents, billed to the subscription.
  * RUNTIME     -- the pipeline's own calls through CachedLLM, billed to the API key.
                   Tracked per dispatch in the `agent_runs` table, and reported here
                   from cache/llm/ so first-run cost and replay cost are both visible.

Note on retrospective data: per-turn subagent transcripts are streamed and not kept
on disk, so a finished subagent's detail is gone. What survives is its completion
record in the parent session transcript, which is what this reads.

    python run.py usage           # or: python scripts/agent_usage.py
"""
from __future__ import annotations

import json
import pathlib
import re
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_DIRS = ["c--Workspace-SoriaProblem", "C--Users-jroyce"]

# USD per 1M tokens. Cache multipliers are the standard published ratios
# (read ~0.1x input, write ~1.25x input); they are applied as an ESTIMATE and the
# report says so rather than presenting a derived number as an invoice.
PRICING = {
    "claude-opus-5":    {"in": 5.00, "out": 25.00},
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
}
CACHE_READ_MULT = 0.10
CACHE_WRITE_MULT = 1.25

# The completion record puts <summary> and <usage> far apart, with the agent's whole
# result between them, so we anchor on the usage block and search BACKWARDS for the
# nearest preceding agent name. One big regex across that gap is brittle.
USAGE_RE = re.compile(
    r"<subagent_tokens>(\d+)</subagent_tokens>\s*"
    r"<tool_uses>(\d+)</tool_uses>\s*"
    r"<duration_ms>(\d+)</duration_ms>"
)
# The name is JSON-escaped inside the transcript, so the quote may arrive as \" .
NAME_RE = re.compile(r'<summary>Agent [\\]?"([^"\\]+)')


def _sessions() -> list[pathlib.Path]:
    base = pathlib.Path.home() / ".claude" / "projects"
    out: list[pathlib.Path] = []
    for d in PROJECT_DIRS:
        out.extend(sorted((base / d).glob("*.jsonl"))) if (base / d).exists() else None
    return out


def _cost(model: str, tin: int, tout: int, cread: int, cwrite: int) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    return (tin / 1e6 * p["in"] + tout / 1e6 * p["out"]
            + cread / 1e6 * p["in"] * CACHE_READ_MULT
            + cwrite / 1e6 * p["in"] * CACHE_WRITE_MULT)


def scan_sessions() -> tuple[list[dict], list[dict]]:
    """Return (per-turn rows, subagent completion rows)."""
    turns: list[dict] = []
    subagents: list[dict] = []
    for f in _sessions():
        text = f.read_text(encoding="utf-8", errors="replace")
        # A completion notification is repeated across several JSONL lines, so the same
        # usage block is found many times. Key on the exact usage triple: two agents
        # matching to the token, the tool call and the millisecond are the same record.
        # Where several copies disagree on the name (the backward search can miss),
        # prefer the one that actually found a name.
        by_usage: dict[tuple[int, int, int], str] = {}
        for m in USAGE_RE.finditer(text):
            names = NAME_RE.findall(text[max(0, m.start() - 60000):m.start()])
            key = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            found = names[-1] if names else ""
            if key not in by_usage or (found and not by_usage[key]):
                by_usage[key] = found
        for (tok, tools, ms), name in by_usage.items():
            subagents.append({"session": f.stem[:8], "agent": name or "(unnamed)",
                              "tokens": tok, "tool_uses": tools,
                              "duration_s": ms / 1000})
        # One assistant message spans several JSONL lines (one per content block) and
        # repeats its usage on each. Dedupe on message id or the same turn is counted
        # three or four times over.
        seen_ids: set[str] = set()
        for i, ln in enumerate(text.splitlines()):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            m = o.get("message") or {}
            u = m.get("usage") if isinstance(m, dict) else None
            if not isinstance(u, dict):
                continue
            model = m.get("model") or "?"
            if model == "<synthetic>":
                continue
            mid = m.get("id")
            if mid:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            turns.append({
                "session": f.stem[:8], "n": i, "model": model,
                "in": u.get("input_tokens", 0) or 0,
                "out": u.get("output_tokens", 0) or 0,
                "cread": u.get("cache_read_input_tokens", 0) or 0,
                "cwrite": u.get("cache_creation_input_tokens", 0) or 0,
            })
    return turns, subagents


def runtime_usage() -> dict:
    """What the pipeline itself has spent through CachedLLM."""
    cache = ROOT / "cache" / "llm"
    entries = list(cache.glob("*.json")) if cache.exists() else []
    tin = tout = 0
    by_model: dict[str, int] = {}
    for e in entries:
        try:
            rec = json.loads(e.read_text(encoding="utf-8"))
        except Exception:
            continue
        u = rec.get("usage", {})
        tin += u.get("input_tokens", 0)
        tout += u.get("output_tokens", 0)
        by_model[rec.get("model", "?")] = by_model.get(rec.get("model", "?"), 0) + 1
    return {"entries": len(entries), "in": tin, "out": tout, "by_model": by_model}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    turns, subagents = scan_sessions()

    print("=" * 74)
    print("BUILD-TIME  (Claude Code -- billed to the subscription, NOT the API key)")
    print("=" * 74)

    if subagents:
        print("\nSubagents, most expensive first:")
        print(f"  {'agent':<28} {'tokens':>10} {'tools':>6} {'secs':>7} {'tok/tool':>9}")
        for s in sorted(subagents, key=lambda x: -x["tokens"]):
            per = s["tokens"] / s["tool_uses"] if s["tool_uses"] else 0
            print(f"  {s['agent'][:28]:<28} {s['tokens']:>10,} {s['tool_uses']:>6} "
                  f"{s['duration_s']:>7.0f} {per:>9,.0f}")
        tot = sum(s["tokens"] for s in subagents)
        print(f"  {'TOTAL':<28} {tot:>10,}")
        print("\n  (a finished subagent's per-turn detail is not retained on disk;")
        print("   these totals come from its completion record in the parent session)")
    else:
        print("\n  no subagent completions recorded yet")

    if turns:
        by_sess: dict[str, list[dict]] = {}
        for t in turns:
            by_sess.setdefault(t["session"], []).append(t)
        print("\nMain sessions:")
        print(f"  {'session':<10} {'turns':>6} {'input':>11} {'output':>10} "
              f"{'cache rd':>11} {'cache wr':>10} {'est $':>9}")
        grand = 0.0
        for sess, rows in by_sess.items():
            ti = sum(r["in"] for r in rows); to = sum(r["out"] for r in rows)
            cr = sum(r["cread"] for r in rows); cw = sum(r["cwrite"] for r in rows)
            c = _cost(rows[0]["model"], ti, to, cr, cw); grand += c
            print(f"  {sess:<10} {len(rows):>6} {ti:>11,} {to:>10,} {cr:>11,} {cw:>10,} {c:>9.2f}")
        print(f"  {'TOTAL':<10} {len(turns):>6} {'':>11} {'':>10} {'':>11} {'':>10} {grand:>9.2f}")
        print("  est $ uses list price with standard cache multipliers "
              f"(read {CACHE_READ_MULT}x, write {CACHE_WRITE_MULT}x) -- an estimate, not an invoice")

        # --- spike detection ---------------------------------------------------
        outs = [t["out"] for t in turns if t["out"] > 0]
        if len(outs) >= 5:
            med = statistics.median(outs)
            spikes = sorted((t for t in turns if t["out"] > max(med * 3, med + 2000)),
                            key=lambda t: -t["out"])[:8]
            print(f"\nSpike check  (median output/turn = {med:,.0f} tokens)")
            if spikes:
                print("  turns above 3x median output:")
                for t in spikes:
                    print(f"    session {t['session']} line {t['n']:>5}: "
                          f"{t['out']:>7,} out, {t['in']:>8,} in, {t['cread']:>9,} cache-read")
            else:
                print("  no turn exceeded 3x the median. Usage is flat.")

            reads = [t["cread"] for t in turns]
            if reads and sum(reads):
                hit = sum(reads) / max(sum(r["in"] + r["cread"] for r in turns), 1)
                print(f"  prompt-cache reuse: {hit:.0%} of input tokens served from cache")
                if hit < 0.5:
                    print("  ! low reuse -- something volatile may be invalidating the prefix")

    rt = runtime_usage()
    print("\n" + "=" * 74)
    print("RUNTIME  (pipeline via CachedLLM -- billed to YOUR API key)")
    print("=" * 74)
    print(f"  cached responses : {rt['entries']}")
    print(f"  tokens paid for  : {rt['in']:,} in / {rt['out']:,} out")
    if rt["by_model"]:
        for m, n in sorted(rt["by_model"].items()):
            print(f"    {m}: {n} call(s)")
    spent = sum(_cost(m, 0, 0, 0, 0) for m in rt["by_model"]) or 0.0
    hay = PRICING["claude-haiku-4-5"]
    spent = rt["in"] / 1e6 * hay["in"] + rt["out"] / 1e6 * hay["out"]
    print(f"  estimated spend  : ${spent:.4f}")
    print(f"  replay cost      : $0.0000  (every response is cached and committed)")
    if rt["entries"] == 0:
        print("\n  Nothing has been paid for yet. The paid pass has not been run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
