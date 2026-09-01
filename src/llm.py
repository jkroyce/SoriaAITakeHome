"""Cached model access.

This module is the architectural centre of the project. Every model call is
keyed by a SHA-256 of its full input (model + system + prompt + schema) and
written to cache/llm/. That cache is committed to the repository, which means:

  * `make demo` replays the entire pipeline with no API key and no spend
  * an entity resolved once is never re-reasoned about again
  * the marginal cost of a refresh is proportional to genuinely NEW information,
    not to the size of the corpus

The cache is not a test fixture. It is the mechanism by which agent reasoning
becomes a deterministic asset -- reason once, look up forever.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import ANTHROPIC_API_KEY, ROOT

CACHE_DIR = ROOT / "cache" / "llm"

# USD per 1M tokens (Anthropic first-party API rates).
PRICING = {
    "claude-haiku-4-5": {"in": 1.00, "out": 5.00},
    "claude-opus-5":    {"in": 5.00, "out": 25.00},
}


class NotCachedError(RuntimeError):
    """Raised when a call is missing from cache and live calls are disabled."""


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, tin: int, tout: int) -> None:
        self.calls += 1
        self.input_tokens += tin
        self.output_tokens += tout
        m = self.by_model.setdefault(model, {"calls": 0, "in": 0, "out": 0})
        m["calls"] += 1
        m["in"] += tin
        m["out"] += tout

    def cost_usd(self) -> float:
        total = 0.0
        for model, m in self.by_model.items():
            p = PRICING.get(model)
            if not p:
                continue
            total += m["in"] / 1e6 * p["in"] + m["out"] / 1e6 * p["out"]
        return round(total, 4)

    def summary(self) -> str:
        return (f"llm: {self.calls} live call(s), {self.cache_hits} cache hit(s), "
                f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens, "
                f"est ${self.cost_usd():.4f}")


class CachedLLM:
    """A content-addressed, replayable wrapper over the Messages API."""

    def __init__(self, live: bool = False, cache_dir: pathlib.Path = CACHE_DIR):
        self.live = live
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.usage = Usage()
        self._client = None

    def _client_lazy(self):
        if self._client is None:
            import anthropic
            key = os.environ.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
            if not key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Either export it for a --live run, "
                    "or run without --live to replay from the committed cache."
                )
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    @staticmethod
    def key(model: str, system: str, prompt: str, schema: dict | None, max_tokens: int) -> str:
        payload = json.dumps(
            {"model": model, "system": system, "prompt": prompt,
             "schema": schema, "max_tokens": max_tokens},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def json_call(self, *, model: str, system: str, prompt: str,
                  schema: dict, max_tokens: int = 8000, label: str = "") -> dict:
        """Ask for JSON matching `schema`. Returns the parsed object."""
        k = self.key(model, system, prompt, schema, max_tokens)
        path = self.cache_dir / f"{k}.json"

        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            self.usage.cache_hits += 1
            return rec["output"]

        if not self.live:
            raise NotCachedError(
                f"cache miss for {label or 'call'} ({k[:12]}). "
                f"Re-run with --live and ANTHROPIC_API_KEY set to populate the cache."
            )

        resp = self._client_lazy().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        output = json.loads(text)

        tin = getattr(resp.usage, "input_tokens", 0) or 0
        tout = getattr(resp.usage, "output_tokens", 0) or 0
        self.usage.add(model, tin, tout)

        path.write_text(json.dumps({
            "label": label,
            "model": model,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "key_inputs": {"system": system, "prompt": prompt,
                           "schema": schema, "max_tokens": max_tokens},
            "usage": {"input_tokens": tin, "output_tokens": tout},
            "output": output,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        return output


def cache_stats() -> dict:
    files = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
    tin = tout = 0
    for f in files:
        try:
            u = json.loads(f.read_text(encoding="utf-8")).get("usage", {})
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0)
        except Exception:
            continue
    return {"entries": len(files), "input_tokens": tin, "output_tokens": tout}
