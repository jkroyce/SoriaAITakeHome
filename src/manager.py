"""Runtime manager -- the only orchestrator.

`manager tick` is the whole refresh cycle, so cron calls one command:

    survey  ->  queue  ->  project cost  ->  dispatch  ->  escalate  ->  record

Everything here that *can* be deterministic *is* deterministic, per CLAUDE.md. In
particular:

  * **Change detection is a set difference on ids.** Not an agent. "What is new" is a
    `set(...) - set(...)`; ranking is a `sort`. The agent judges whether a change
    *matters*, never whether one *occurred*. See ``new_ids`` and ``detect_changes``.
  * **Routing is a dictionary lookup.** An award with no ticker goes to the resolver
    because that is a lookup, not a judgment. There is no triage model call.
  * **Table selection is a set operation.** A result dict is written to the one
    contract table whose primary key it carries and whose columns it fits
    (``table_for``). No agent tells the manager where its rows live, and no table name
    is hardcoded against an agent name.

Agents are **discovered, never hardcoded**. Any module under ``src/agents/`` that
exposes an object with ``name`` / ``detects(conn)`` / ``run(items, llm)`` /
``skills_path()`` is registered. Dropping in ``score_materiality.py`` requires no edit
to this file, and its absence is not an error.

``src/agents/extract.py`` is a module of functions rather than a protocol object -- it
is the primary pipeline stage, not a cleaning agent -- so this file carries a thin
built-in adapter (``ExtractAgent``) that presents it through the same protocol. That
adapter is the *only* agent-specific code here.

Three run modes, and the distinction is the point:

  --dry-run   ZERO API calls. Prints the planned queue, call count and estimated cost.
              A tripwire stands in for the LLM for the whole run: a call would raise.
  --offline   Replays entirely from the committed cache. A cache miss is a clean,
              explained error, never a live call. Needs no ANTHROPIC_API_KEY.
              This is `make demo`.
  --live      May call, but only for genuinely new inputs. --max-spend aborts BEFORE
              anything is spent when the projected queue cost exceeds the cap.

Exit codes for `tick`:
  0  the tick did what it was asked
  1  a dispatch failed unexpectedly
  2  refused before spending (--max-spend, or a live run with no key)
  3  offline replay produced nothing because the cache is cold -- not a crash
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import math
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

_SRC = pathlib.Path(__file__).resolve().parent
if str(_SRC) not in sys.path:            # `python -m src.manager` and `python src/manager.py`
    sys.path.insert(0, str(_SRC))

import config
import schemas
import store
from llm import CACHE_DIR, CachedLLM, NotCachedError, PRICING, Usage, cache_stats

ROOT = config.ROOT
AGENTS_DIR = _SRC / "agents"
SKILLS_DIR = ROOT / "skills"
GOLDEN_DIR = ROOT / "tests" / "golden"
GOLDEN_TEST = ROOT / "tests" / "test_golden.py"

BULK_MODEL = config.BULK_MODEL
JUDGE_MODEL = config.JUDGE_MODEL

LOW_CONFIDENCE = 0.7            # CLAUDE.md: below this escalates, then flags
DEFAULT_MAX_SPEND = 5.00        # USD, per the plan's cost-control section

# Dependency order. Anything discovered that is not named here runs after these, in
# name order, so a new agent has a defined -- and stable -- position without an edit.
STAGE_ORDER = ("extract", "resolve_entity", "score_materiality", "validate_award")

# --- estimation constants, all heuristics, all labelled as such in the output -------
CHARS_PER_TOKEN = 3.7                    # the divisor extract.dry_run uses
EST_PROMPT_SCAFFOLD_CHARS = 4_400        # base instructions + skills, measured
EST_OUTPUT_TOKENS_PER_EXTRACT = 5_500    # ~15 awards x ~24 JSON fields
EST_INPUT_TOKENS_PER_GENERIC_CALL = 2_000
EST_OUTPUT_TOKENS_PER_GENERIC_CALL = 800


# ================================================================================
# small deterministic helpers
# ================================================================================

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _uid(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for a call. An unknown model prices at 0 rather than guessing."""
    p = PRICING.get(model)
    if not p:
        return 0.0
    return round(input_tokens / 1e6 * p["in"] + output_tokens / 1e6 * p["out"], 6)


def new_ids(prior: Iterable[str], current: Iterable[str]) -> list[str]:
    """What is new = a set difference. Deliberately NOT an agent (CLAUDE.md).

    Sorted, so the answer -- and every id derived from it -- is stable across runs.
    """
    return sorted(set(map(str, current)) - set(map(str, prior)))


def result_confidence(result: Mapping[str, Any]) -> float | None:
    """The confidence an agent reported, whatever it called it.

    `entities.confidence` and `awards.extraction_confidence` are one convention with
    two spellings, and a new agent may bring a third. Any `*_confidence` key counts.
    """
    for key in ("confidence", *(k for k in result if k.endswith("_confidence"))):
        v = result.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def result_model(result: Mapping[str, Any]) -> str | None:
    """`extractor_model` / `resolver_model` / `scorer_model` -- same convention."""
    for k in ("model", *(k for k in result if k.endswith("_model"))):
        v = result.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def table_for(result: Mapping[str, Any]) -> str | None:
    """Which contract table this result belongs in. A set operation, not a lookup
    keyed by agent name -- so a new agent's rows land correctly with no edit here.

    A table matches when the result carries its whole primary key and introduces no
    column the table does not have. Ties break on how many columns are covered.
    """
    keys = set(result)
    best: tuple[str, int] | None = None
    for table in schemas.TABLES:
        cols = set(store.columns(table))
        pk = set(store.primary_key(table))
        if not pk <= keys or not keys <= cols:
            continue
        score = len(keys & cols)
        if best is None or score > best[1]:
            best = (table, score)
    return best[0] if best else None


def item_key_of(result: Mapping[str, Any], table: str | None) -> str:
    if table:
        return "|".join(str(result.get(k)) for k in store.primary_key(table))
    return str(result.get("item_key") or result.get("id") or "?")


# ================================================================================
# cost projection -- computed without calling anything
# ================================================================================

@dataclass
class Estimate:
    """A projection, never a measurement. `basis` says how it was arrived at."""
    agent: str = ""
    items: int = 0
    calls: int = 0                 # calls that would actually be made
    cached_calls: int = 0          # responses already committed -> free
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = BULK_MODEL
    basis: str = "heuristic"

    def cost_usd(self) -> float:
        return price(self.model, self.input_tokens, self.output_tokens)

    def merge(self, other: "Estimate") -> "Estimate":
        return Estimate(
            agent=f"{self.agent}+{other.agent}" if self.agent else other.agent,
            items=self.items + other.items,
            calls=self.calls + other.calls,
            cached_calls=self.cached_calls + other.cached_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            model=self.model,
            basis="mixed",
        )


def generic_estimate(agent: Any, items: Sequence[Any]) -> Estimate:
    """Projection for an agent that does not supply its own.

    Two optional conventions an agent may declare, both read reflectively so that
    neither requires an edit here:

      * ``agent.estimate(items) -> Estimate``  -- exact, used when present
      * a module-level ``BATCH_SIZE``          -- how many items share one call

    Absent both, one call per item is assumed. That over-projects for a batching
    agent, which is the safe direction for a spend cap.
    """
    n = len(items)
    if n == 0:
        return Estimate(agent=getattr(agent, "name", "?"), basis="no work")

    own = getattr(agent, "estimate", None)
    if callable(own):
        try:
            est = own(items)
            if isinstance(est, Estimate):
                return est
        except Exception as exc:            # a bad estimator must not abort a plan
            sys.stderr.write(f"! {getattr(agent, 'name', '?')}.estimate failed: "
                             f"{type(exc).__name__}: {exc}\n")

    module = sys.modules.get(type(agent).__module__)
    batch = getattr(module, "BATCH_SIZE", None) if module else None
    if isinstance(batch, int) and batch > 0:
        calls = math.ceil(n / batch)
        basis = f"heuristic, {batch} items/call from module BATCH_SIZE"
    else:
        calls = n
        basis = "heuristic, 1 call/item (module declares no BATCH_SIZE)"

    return Estimate(
        agent=getattr(agent, "name", "?"), items=n, calls=calls,
        input_tokens=calls * EST_INPUT_TOKENS_PER_GENERIC_CALL,
        output_tokens=calls * EST_OUTPUT_TOKENS_PER_GENERIC_CALL,
        model=BULK_MODEL, basis=basis,
    )


# ================================================================================
# the extract adapter -- the one piece of agent-specific code in this file
# ================================================================================

class ExtractAgent:
    """`src/agents/extract.py` presented through the CleaningAgent protocol.

    extract.py is a module of functions, not a protocol object, because extraction is
    the primary pipeline stage rather than a cleaning pass. Rather than ask another
    section to change a file it owns, the manager adapts it here.
    """

    name = "extract"

    def __init__(self, raw_dir: pathlib.Path | None = None):
        from agents import extract as extract_mod
        self._m = extract_mod
        self.raw_dir = raw_dir
        self._manifest: dict[str, dict] | None = None
        self._results: dict[str, Any] = {}

    # -- protocol ---------------------------------------------------------------
    def skills_path(self) -> pathlib.Path:
        return self._m.SKILLS_PATH

    def detects(self, conn) -> list[str]:
        """Announcements whose prose has not been turned into rows yet. A predicate on
        a status column -- no model is asked what needs extracting."""
        try:
            return [r["announcement_id"] for r in store.pending_announcements(conn)]
        except Exception as exc:
            sys.stderr.write(f"! extract.detects: {type(exc).__name__}: {exc}\n")
            return []

    def run(self, items, llm, **kw) -> list[dict]:
        model = kw.get("model") or BULK_MODEL
        rows: list[dict] = []
        self._results = {}
        for aid in items:
            ann = self._announcement(str(aid))
            res = self._m.extract_document(ann, llm, model=model, raw_dir=self.raw_dir)
            self._results[str(aid)] = res
            rows.extend(res.awards)
        return rows

    def after_write(self, conn, items, results, failures) -> None:
        """extracted | failed on the announcements row. A targeted UPDATE -- the only
        stateful side effect this adapter has, and not a model call."""
        for aid in items:
            status = "failed" if str(aid) in failures else "extracted"
            try:
                store.mark_extraction_status(conn, str(aid), status)
            except Exception as exc:
                sys.stderr.write(f"! mark_extraction_status({aid}): {exc}\n")

    # -- projection -------------------------------------------------------------
    def estimate(self, items, *, count_cache: bool = True) -> Estimate:
        """Exact where it can be: ``extract.dry_run`` builds the real prompt and the
        real cache key without sending anything, so a call already in cache is counted
        as free rather than guessed at.

        When the article HTML is absent (``raw/*.html`` is gitignored and regenerable
        via ``run.py fetch``) the prompt cannot be built, so the projection falls back
        to the manifest's recorded ``body_chars`` and says so in ``basis``.

        ``count_cache=False`` prices the queue as if the cache were empty. That is the
        honest projection for a skills promotion, where the prompt itself changes and
        therefore every existing cache key is invalidated.
        """
        est = Estimate(agent=self.name, items=len(items), model=BULK_MODEL,
                       basis="exact prompt via extract.dry_run")
        degraded = 0
        for aid in items:
            ann = self._announcement(str(aid))
            try:
                d = self._m.dry_run(ann, raw_dir=self.raw_dir)
                tin = int(d["est_input_tokens"])
                if count_cache and pathlib.Path(d["cache_path"]).exists():
                    est.cached_calls += 1
                    continue
            except Exception:
                degraded += 1
                body_chars = int(ann.get("body_chars") or 0)
                tin = int((body_chars + EST_PROMPT_SCAFFOLD_CHARS) / CHARS_PER_TOKEN)
            est.calls += 1
            est.input_tokens += tin
            est.output_tokens += EST_OUTPUT_TOKENS_PER_EXTRACT
        if degraded:
            est.basis = (f"manifest body_chars for {degraded}/{len(items)} "
                         f"(article HTML absent; `python run.py fetch 5` restores it)")
        return est

    # -- internals --------------------------------------------------------------
    def _announcement(self, aid: str) -> dict:
        if self._manifest is None:
            self._manifest = {str(r.get("article_id")): r
                              for r in self._m.load_manifest(self.raw_dir)}
        return self._manifest.get(aid) or {"article_id": aid}


# ================================================================================
# agent discovery -- add a file, not a line of this one
# ================================================================================

PROTOCOL_METHODS = ("detects", "run", "skills_path")


def implements_protocol(obj: Any) -> bool:
    return (isinstance(getattr(obj, "name", None), str)
            and all(callable(getattr(obj, m, None)) for m in PROTOCOL_METHODS))


def _load_module(path: pathlib.Path):
    """Import one agent file. Inside src/agents it is imported as a package member so
    its own import expectations hold; anywhere else (a temp dir, a plugin dir) it is
    loaded by file location."""
    if path.parent == AGENTS_DIR:
        return importlib.import_module(f"agents.{path.stem}")
    name = f"_manager_dyn_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def discover_agents(agents_dir: pathlib.Path | None = None,
                    *, verbose: bool = False) -> list[Any]:
    """Every protocol-conforming object under `agents_dir`, in dependency order.

    A module that does not conform is simply not an agent -- not an error. A module
    that fails to import is reported and skipped, because one broken agent must not
    take the whole tick down with it.
    """
    d = pathlib.Path(agents_dir) if agents_dir else AGENTS_DIR
    found: dict[str, Any] = {}
    if not d.exists():
        return []
    for path in sorted(d.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            module = _load_module(path)
        except Exception as exc:
            sys.stderr.write(f"! agent module {path.name} did not import: "
                             f"{type(exc).__name__}: {exc}\n")
            continue
        for attr in vars(module).values():
            inst = None
            if isinstance(attr, type):
                if getattr(attr, "__module__", None) != module.__name__:
                    continue            # imported from elsewhere, not defined here
                if not isinstance(getattr(attr, "name", None), str):
                    continue
                if not all(callable(getattr(attr, m, None)) for m in PROTOCOL_METHODS):
                    continue
                try:
                    inst = attr()       # protocol agents take no constructor args
                except Exception as exc:
                    sys.stderr.write(f"! {path.name}:{attr.__name__} would not "
                                     f"instantiate: {type(exc).__name__}: {exc}\n")
                    continue
            elif implements_protocol(attr):
                inst = attr
            if inst is not None and implements_protocol(inst):
                if inst.name not in found:
                    found[inst.name] = inst
                    if verbose:
                        print(f"  registered {inst.name:20s} <- {path.name}")
    return sorted(found.values(), key=_stage_key)


def _stage_key(agent: Any) -> tuple[int, str]:
    name = getattr(agent, "name", "")
    return (STAGE_ORDER.index(name) if name in STAGE_ORDER else len(STAGE_ORDER), name)


def build_registry(agents_dir: pathlib.Path | None = None, *,
                   raw_dir: pathlib.Path | None = None,
                   verbose: bool = False) -> list[Any]:
    """Discovered agents, plus the built-in adapter for extract.py when nothing under
    src/agents/ already claims the name `extract`."""
    agents = discover_agents(agents_dir, verbose=verbose)
    if not any(getattr(a, "name", "") == "extract" for a in agents):
        try:
            agents.append(ExtractAgent(raw_dir=raw_dir))
            if verbose:
                print("  registered extract              "
                      "<- built-in adapter over agents/extract.py")
        except Exception as exc:
            sys.stderr.write(f"! extract adapter unavailable: {type(exc).__name__}: {exc}\n")
    return sorted(agents, key=_stage_key)


# ================================================================================
# the tripwire -- production code, not a test helper
# ================================================================================

class Tripwire:
    """An LLM stand-in that raises if anything tries to spend money.

    Hand it to `dispatch` in place of a CachedLLM to prove an agent path is free.
    """

    def __init__(self):
        self.usage = Usage()
        self.live = False
        self.fired = 0

    def json_call(self, **kw):
        self.fired += 1
        raise AssertionError(
            f"TRIPWIRE: a model call was attempted for {kw.get('label')!r}")


class DryRunGuard:
    """Blocks the ONE function that can spend money, for the length of a dry run.

    `CachedLLM.json_call` is the single choke point -- CLAUDE.md forbids an
    `import anthropic` anywhere else, so replacing it makes "this run cannot call the
    API" a property of the process rather than a claim in a docstring. This is
    production code, not a test helper: `tick --dry-run` installs it, and the count it
    prints is a real measurement of a real guard.
    """

    def __init__(self):
        self.fired = 0
        self._original = None

    def __enter__(self) -> "DryRunGuard":
        guard = self
        self._original = CachedLLM.json_call

        def blocked(inner_self, **kw):
            guard.fired += 1
            raise AssertionError(
                f"DRY-RUN VIOLATION: a model call was attempted for {kw.get('label')!r}")

        CachedLLM.json_call = blocked
        return self

    def __exit__(self, *exc) -> bool:
        if self._original is not None:
            CachedLLM.json_call = self._original
        return False


# ================================================================================
# survey -- what is new, computed as a set difference
# ================================================================================

@dataclass
class Survey:
    known_ids: list[str] = dc_field(default_factory=list)
    manifest_ids: list[str] = dc_field(default_factory=list)
    rss_ids: list[str] = dc_field(default_factory=list)
    new_announcements: list[str] = dc_field(default_factory=list)
    unfetched: list[str] = dc_field(default_factory=list)
    rss_status: str = "not polled"


def poll_rss(max_items: int = 30) -> tuple[list[str], str]:
    """Announcement ids from the change feed. Deterministic parsing, no model.

    Network access is optional by design: the tick's source of truth is the fetched
    manifest, and the feed only ever tells us about work not yet fetched. A feed
    failure degrades to a status string and never aborts a tick -- and `--offline`
    never reaches here at all, so `make demo` needs no network.
    """
    import re as _re
    try:
        from curl_cffi import requests as _requests
        sess = _requests.Session(impersonate=config.IMPERSONATE)
        sess.verify = config.ca_bundle()
        resp = sess.get(config.RSS_URL.format(max=max_items),
                        timeout=config.REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code}"
        ids = _re.findall(r"/Article/(\d+)/", resp.text)
        build = _re.search(r"<lastBuildDate>([^<]+)</lastBuildDate>", resp.text)
        stamp = build.group(1) if build else "no lastBuildDate"
        return sorted(set(ids)), f"ok, lastBuildDate {stamp}"
    except Exception as exc:
        return [], f"unavailable ({type(exc).__name__}: {exc})"


def survey(conn, *, use_rss: bool = False,
           raw_dir: pathlib.Path | None = None) -> Survey:
    """Step 1. What does the world hold that the database does not?

    Three id sets, two set differences, zero model calls.
    """
    known = [str(r["announcement_id"]) for r in
             store.query(conn, "SELECT announcement_id FROM announcements")]
    from agents import extract as extract_mod
    manifest = [str(r.get("article_id")) for r in extract_mod.load_manifest(raw_dir)]

    rss_ids, status = [], "not polled (offline/dry-run)"
    if use_rss:
        rss_ids, status = poll_rss()

    return Survey(
        known_ids=sorted(known),
        manifest_ids=sorted(manifest),
        rss_ids=rss_ids,
        new_announcements=new_ids(known, manifest),
        unfetched=new_ids(manifest, rss_ids),
        rss_status=status,
    )


def detect_changes(conn, before: Mapping[str, set], after: Mapping[str, set]) -> list[dict]:
    """`changes` rows for everything that appeared during this tick.

    A set difference per entity type, then a deterministic id per event so a re-run
    converges instead of appending duplicates. No model is consulted about whether a
    change occurred; whether it *matters* is score_materiality's job.
    """
    scores = {r["award_uid"]: r["score"] for r in
              store.query(conn, "SELECT award_uid, score FROM materiality")}
    award_meta = {r["award_uid"]: r for r in store.query(
        conn, "SELECT award_uid, announcement_id, contractor_raw FROM awards")}
    tickers = {r["contractor_raw"]: r["ticker"] for r in
               store.query(conn, "SELECT contractor_raw, ticker FROM entities")}

    detected = _now()
    blank = {c: None for c in store.columns("changes")}
    rows: list[dict] = []

    for aid in new_ids(before.get("announcements", set()),
                       after.get("announcements", set())):
        rows.append({**blank, "change_id": _uid("new_announcement", aid),
                     "detected_at": detected, "change_type": "new_announcement",
                     "announcement_id": aid})

    for uid in new_ids(before.get("awards", set()), after.get("awards", set())):
        meta = award_meta.get(uid, {})
        rows.append({**blank, "change_id": _uid("new_award", uid),
                     "detected_at": detected, "change_type": "new_award",
                     "announcement_id": meta.get("announcement_id"), "award_uid": uid,
                     "ticker": tickers.get(meta.get("contractor_raw")),
                     "materiality_score": scores.get(uid)})
    return rows


# ================================================================================
# dispatch
# ================================================================================

@dataclass
class Dispatch:
    agent: str
    items: list[Any]
    runs: list[dict] = dc_field(default_factory=list)      # agent_runs rows
    results: list[dict] = dc_field(default_factory=list)
    reviews: list[dict] = dc_field(default_factory=list)   # review_queue rows
    written: dict[str, int] = dc_field(default_factory=dict)
    failures: dict[str, str] = dc_field(default_factory=dict)
    note: str = ""


def _skills_version(agent: Any) -> str:
    """Content hash of the agent's rules file.

    The rules are part of the prompt and the cache keys on the prompt, so this is
    exactly the thing whose change forces a re-run -- which is why every agent_runs
    row carries it.
    """
    try:
        p = pathlib.Path(agent.skills_path())
    except Exception:
        return "none"
    if not p.exists():
        return "none"
    text = p.read_text(encoding="utf-8").strip()
    return "sk-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8] if text else "none"


def _snapshot(usage: Usage) -> Usage:
    snap = Usage(calls=usage.calls, cache_hits=usage.cache_hits,
                 input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    snap.by_model = {m: dict(v) for m, v in usage.by_model.items()}
    return snap


def _usage_delta(before: Usage, after: Usage) -> dict:
    return {
        "calls": after.calls - before.calls,
        "cache_hits": after.cache_hits - before.cache_hits,
        "input_tokens": after.input_tokens - before.input_tokens,
        "output_tokens": after.output_tokens - before.output_tokens,
        "cost": round(after.cost_usd() - before.cost_usd(), 6),
    }


def _run_row(**kw) -> dict:
    """An agent_runs row: exactly store.columns("agent_runs"), never a typed-out list.
    An unknown key is an error rather than a silently dropped field."""
    row = {c: None for c in store.columns("agent_runs")}
    unknown = set(kw) - set(row)
    if unknown:
        raise KeyError(f"agent_runs has no column(s) {sorted(unknown)}")
    row.update(kw)
    return row


def _review_row(**kw) -> dict:
    row = {c: None for c in store.columns("review_queue")}
    unknown = set(kw) - set(row)
    if unknown:
        raise KeyError(f"review_queue has no column(s) {sorted(unknown)}")
    row.update(kw)
    return row


def _as_rows(out: Any) -> list[dict]:
    """An agent may return a list of rows or a name->row mapping. Both are fine."""
    if isinstance(out, Mapping):
        return [v for v in out.values() if isinstance(v, Mapping)]
    return [r for r in (out or []) if isinstance(r, Mapping)]


def _escalator(agent: Any) -> Callable[[list, Any], list[dict]] | None:
    """How to retry this agent's work on the judge model, discovered reflectively.

    Preference order, all generic -- none of it names a particular agent:
      1. ``agent.escalate(items, llm)``
      2. a module-level ``escalate(items, llm)`` beside the agent
      3. ``agent.run(items, llm, model=JUDGE_MODEL)`` when run accepts a model kwarg
    """
    own = getattr(agent, "escalate", None)
    if callable(own):
        return lambda items, llm: _as_rows(own(list(items), llm))

    module = sys.modules.get(type(agent).__module__)
    fn = getattr(module, "escalate", None) if module else None
    if callable(fn):
        return lambda items, llm: _as_rows(fn(list(items), llm))

    try:
        sig = inspect.signature(agent.run)
    except (TypeError, ValueError):
        return None
    takes_model = "model" in sig.parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    if not takes_model:
        return None
    return lambda items, llm: _as_rows(agent.run(list(items), llm, model=JUDGE_MODEL))


def _try_run(agent: Any, items: Sequence[Any], llm: Any) -> tuple[list[dict], str | None]:
    """Call an agent, turning any failure into a message instead of a traceback."""
    try:
        return _as_rows(agent.run(list(items), llm)), None
    except NotCachedError as exc:
        return [], f"NotCachedError: {exc}"
    except Exception as exc:
        sys.stderr.write(f"! {getattr(agent, 'name', '?')}.run failed: "
                         f"{type(exc).__name__}: {exc}\n")
        return [], f"{type(exc).__name__}: {exc}"


def _natural_item(result: Mapping[str, Any]) -> Any:
    """The work item an agent would recognise, recovered from its own result row.

    resolve_entity is handed contractor names and returns rows keyed by
    `contractor_raw`: the primary key IS the work item. Where a table's key is
    synthetic (`awards.award_uid`) the row itself is handed back instead.
    """
    table = table_for(result)
    if table is None:
        return result
    pk = store.primary_key(table)
    if len(pk) == 1 and isinstance(result.get(pk[0]), str):
        col = pk[0]
        if not col.endswith("_uid") and not col.endswith("_id"):
            return result[col]
    return result


def dispatch(agent: Any, items: Sequence[Any], llm: Any, conn, *,
             tick_id: str, escalate: bool = True) -> Dispatch:
    """Steps 3-5: run the agent, escalate what it was unsure about, record everything.

    Token attribution, stated plainly rather than fudged: agents batch
    (resolve_entity puts 25 names in one call), so per-item token counts are not
    observable. The rule is (a) when a dispatch made **zero live calls**, every item
    is recorded with 0 tokens and $0 -- exactly true; (b) otherwise the batch's
    measured tokens are divided evenly across its items. The sum over a tick therefore
    reconciles with `llm.usage`, which is the number that matters for spend.
    """
    d = Dispatch(agent=getattr(agent, "name", "?"), items=list(items))
    if not d.items:
        d.note = "no work detected"
        return d

    skills = _skills_version(agent)
    usage = getattr(llm, "usage", None) or Usage()
    before = _snapshot(usage)
    started = _now()
    t0 = time.perf_counter()

    results, error = _try_run(agent, d.items, llm)

    # One bad document must not lose the other forty-nine. When the run is free by
    # construction -- replay or dry-run, where `llm.live` is False -- a batch failure
    # is retried item by item so partial progress survives. In --live it is NOT
    # retried: splitting a batch of 25 into 25 calls to salvage one item is exactly
    # the cost bug cost-guard warns about.
    if error is not None and len(d.items) > 1 and not getattr(llm, "live", False):
        results = []
        for it in d.items:
            got, item_error = _try_run(agent, [it], llm)
            if item_error is None:
                results.extend(got)
            else:
                d.failures[str(it)] = item_error
        error = None if results else error

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    delta = _usage_delta(before, usage)

    if error is not None:
        for it in d.items:
            d.failures[str(it)] = error
    if d.failures:
        for it, why in d.failures.items():
            d.runs.append(_run_row(
                run_id=_uid(tick_id, d.agent, it, "fail"), tick_id=tick_id,
                agent=d.agent, item_key=str(it), model=BULK_MODEL, escalated=False,
                cache_hit=False, input_tokens=0, output_tokens=0, cost_usd=0.0,
                confidence=None, outcome="failed", error=why,
                skills_version=skills, started_at=started, duration_ms=elapsed_ms))
        d.note = next(iter(d.failures.values()))
    if error is not None:
        return d

    # -- one agent_runs row per result ------------------------------------------
    n = max(len(results), 1)
    share_in = delta["input_tokens"] // n
    share_out = delta["output_tokens"] // n
    no_spend = delta["calls"] == 0

    escalated_keys: set[str] = set()
    for res in results:
        table = table_for(res)
        key = item_key_of(res, table)
        conf = result_confidence(res)
        model = result_model(res) or BULK_MODEL
        d.runs.append(_run_row(
            run_id=_uid(tick_id, d.agent, key), tick_id=tick_id, agent=d.agent,
            item_key=key, model=model, escalated=False, cache_hit=no_spend,
            input_tokens=0 if no_spend else share_in,
            output_tokens=0 if no_spend else share_out,
            cost_usd=0.0 if no_spend else price(model, share_in, share_out),
            confidence=conf, outcome="ok", error=None, skills_version=skills,
            started_at=started, duration_ms=elapsed_ms))
        d.results.append(dict(res))
        if conf is not None and conf < LOW_CONFIDENCE:
            escalated_keys.add(key)

    # -- step 4: escalate low confidence onto the judge model --------------------
    escalator = _escalator(agent) if escalate else None
    if escalated_keys and escalator is not None:
        low = [r for r in d.results if item_key_of(r, table_for(r)) in escalated_keys]
        e_before = _snapshot(usage)
        e_started, e_t0 = _now(), time.perf_counter()
        try:
            retried = escalator([_natural_item(r) for r in low], llm)
            e_error = None
        except Exception as exc:
            retried, e_error = [], f"{type(exc).__name__}: {exc}"
        e_ms = int((time.perf_counter() - e_t0) * 1000)
        e_delta = _usage_delta(e_before, usage)
        e_n = max(len(retried), 1)
        e_in, e_out = e_delta["input_tokens"] // e_n, e_delta["output_tokens"] // e_n
        e_no_spend = e_delta["calls"] == 0

        by_key = {item_key_of(r, table_for(r)): r for r in retried}
        for key in sorted(escalated_keys):
            better = by_key.get(key)
            conf = result_confidence(better) if better is not None else None
            model = (result_model(better) if better is not None else None) or JUDGE_MODEL
            recovered = better is not None and conf is not None and conf >= LOW_CONFIDENCE
            d.runs.append(_run_row(
                run_id=_uid(tick_id, d.agent, key, "escalated"), tick_id=tick_id,
                agent=d.agent, item_key=key, model=model, escalated=True,
                cache_hit=e_no_spend,
                input_tokens=0 if e_no_spend else e_in,
                output_tokens=0 if e_no_spend else e_out,
                cost_usd=0.0 if e_no_spend else price(model, e_in, e_out),
                confidence=conf, outcome="escalated" if recovered else "flagged",
                error=e_error, skills_version=skills, started_at=e_started,
                duration_ms=e_ms))
            if better is not None:
                d.results = [better if item_key_of(r, table_for(r)) == key else r
                             for r in d.results]
            if not recovered:
                payload = next((r for r in d.results
                                if item_key_of(r, table_for(r)) == key), {})
                d.reviews.append(_review_row(
                    review_id=_uid(d.agent, key, "review"), flagged_at=_now(),
                    agent=d.agent, item_key=key, confidence=conf, payload=payload,
                    reason=(e_error or
                            f"confidence {conf if conf is not None else 'unknown'} still "
                            f"below {LOW_CONFIDENCE} after escalation to {JUDGE_MODEL}"),
                    resolved=False))
    elif escalated_keys:
        for key in sorted(escalated_keys):
            payload = next((r for r in d.results
                            if item_key_of(r, table_for(r)) == key), {})
            d.reviews.append(_review_row(
                review_id=_uid(d.agent, key, "review"), flagged_at=_now(),
                agent=d.agent, item_key=key,
                reason=f"confidence below {LOW_CONFIDENCE}; "
                       f"agent exposes no escalation path",
                confidence=result_confidence(payload), payload=payload, resolved=False))

    # -- write ------------------------------------------------------------------
    by_table: dict[str, list[dict]] = {}
    for res in d.results:
        table = table_for(res)
        if table is None:
            d.note = "some results matched no contract table and were not written"
            continue
        by_table.setdefault(table, []).append(res)
    for table, rows in by_table.items():
        d.written[table] = store.upsert(conn, table, rows)
    if d.reviews:
        d.written["review_queue"] = store.upsert(conn, "review_queue", d.reviews)

    hook = getattr(agent, "after_write", None)
    if callable(hook):
        hook(conn, d.items, d.results, d.failures)
    return d


# ================================================================================
# tick
# ================================================================================

def _rule(title: str) -> None:
    print()
    print("-" * 78)
    print(title)
    print("-" * 78)


def _entity_ids(conn) -> dict[str, set]:
    return {
        "announcements": {str(r["announcement_id"]) for r in
                          store.query(conn, "SELECT announcement_id FROM announcements")},
        "awards": {str(r["award_uid"]) for r in
                   store.query(conn, "SELECT award_uid FROM awards")},
    }


def tick(mode: str = "dry-run", *, max_spend: float = DEFAULT_MAX_SPEND,
         db_path: str | pathlib.Path | None = None,
         agents_dir: pathlib.Path | None = None,
         raw_dir: pathlib.Path | None = None,
         limit: int | None = None, use_rss: bool | None = None,
         do_export: bool | None = None, registry: list | None = None) -> int:
    """One refresh cycle. `mode` is one of dry-run | offline | live."""
    if mode not in ("dry-run", "offline", "live"):
        raise ValueError(f"mode must be dry-run|offline|live, got {mode!r}")

    tick_id = _uid("tick", _now(), mode)[:12]
    started = time.perf_counter()
    if use_rss is None:
        use_rss = (mode == "live")
    if do_export is None:
        do_export = (mode != "dry-run")

    print("=" * 78)
    print(f"manager tick  ·  mode={mode}  ·  tick_id={tick_id}  ·  {_now()}")
    print("=" * 78)

    stats = cache_stats()
    print(f"cache/llm      : {stats['entries']} committed response(s), "
          f"{stats['input_tokens']:,} in / {stats['output_tokens']:,} out tokens "
          f"already paid for")
    print(f"models         : bulk={BULK_MODEL}  judge={JUDGE_MODEL} (escalation only)")
    print(f"api key        : {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT set'}"
          f"{'  (not needed in this mode)' if mode != 'live' else ''}")

    conn = store.init_db(db_path or config.DB_PATH)
    try:
        if mode == "dry-run":
            # The guard is installed around the WHOLE run, survey included, so the
            # "zero model calls" line at the end is a measurement, not an assertion.
            with DryRunGuard() as guard:
                return _tick_body(conn, mode, tick_id, max_spend, agents_dir, raw_dir,
                                  limit, use_rss, do_export, registry, started, guard)
        return _tick_body(conn, mode, tick_id, max_spend, agents_dir, raw_dir,
                          limit, use_rss, do_export, registry, started, None)
    finally:
        conn.close()


def _tick_body(conn, mode, tick_id, max_spend, agents_dir, raw_dir,
               limit, use_rss, do_export, registry, started, guard) -> int:
    # ---------------------------------------------------------------- 1. survey
    _rule("1. survey  ·  set difference on announcement ids, zero model calls")
    before_ids = _entity_ids(conn)
    manifest_path = (pathlib.Path(raw_dir) if raw_dir else config.RAW) / "manifest.json"
    if manifest_path.exists():
        loaded = store.load_manifest(conn, manifest_path)
        print(f"  manifest        : {loaded} announcement(s) ingested")
    else:
        print(f"  manifest        : MISSING at {manifest_path} -- run `python run.py fetch`")

    sv = survey(conn, use_rss=use_rss, raw_dir=raw_dir)
    print(f"  rss feed        : {sv.rss_status}")
    print(f"  in database     : {len(sv.known_ids)}")
    print(f"  in manifest     : {len(sv.manifest_ids)}")
    print(f"  new to database : {len(sv.new_announcements)}"
          + (f"   e.g. {', '.join(sv.new_announcements[:5])}"
             if sv.new_announcements else ""))
    if sv.unfetched:
        print(f"  in feed, unfetched: {len(sv.unfetched)} -- "
              f"`python run.py fetch` would acquire these")

    # ----------------------------------------------------------------- 2. queue
    _rule("2. queue  ·  agents discovered from src/agents/, ordered by dependency")
    agents = registry if registry is not None else build_registry(
        agents_dir, raw_dir=raw_dir, verbose=True)
    present = {getattr(a, "name", "") for a in agents}
    missing = [s for s in STAGE_ORDER if s not in present]
    if missing:
        print(f"  not present yet : {', '.join(missing)}  (absence is not an error; "
              f"drop the file in\n                    and it is registered with no edit here)")

    queue: list[tuple[Any, list]] = []
    for agent in agents:
        try:
            items = list(agent.detects(conn))
        except Exception as exc:
            print(f"  ! {getattr(agent, 'name', '?')}.detects failed: "
                  f"{type(exc).__name__}: {exc}")
            items = []
        if limit:
            items = items[:limit]
        queue.append((agent, items))
        print(f"  {agent.name:20s} {len(items):5d} item(s)   skills={_skills_version(agent)}")
    print("  note            : downstream stages are re-detected after upstream ones run,")
    print("                    so a stage showing 0 here may still get work this tick.")

    # ------------------------------------------------------ 3. costed projection
    _rule("3. projected cost  ·  computed, not measured; nothing has been called")
    total = Estimate()
    print(f"  {'agent':20s} {'items':>6s} {'calls':>6s} {'cached':>7s} "
          f"{'tok in':>10s} {'tok out':>9s} {'est $':>9s}   basis")
    for agent, items in queue:
        est = generic_estimate(agent, items)
        total = total.merge(est)
        print(f"  {est.agent:20s} {est.items:6d} {est.calls:6d} {est.cached_calls:7d} "
              f"{est.input_tokens:10,d} {est.output_tokens:9,d} "
              f"{est.cost_usd():9.4f}   {est.basis}")
    projected = price(BULK_MODEL, total.input_tokens, total.output_tokens)
    print(f"  {'TOTAL':20s} {total.items:6d} {total.calls:6d} {total.cached_calls:7d} "
          f"{total.input_tokens:10,d} {total.output_tokens:9,d} {projected:9.4f}")
    print(f"  spend cap       : ${max_spend:.2f}"
          + ("   (enforced in --live)" if mode != "live" else ""))

    # ------------------------------------------------------------ dry-run exits
    if mode == "dry-run":
        _rule("4. dry run  ·  tripwire armed, nothing dispatched")
        print("  CachedLLM.json_call -- the single choke point through which any spend")
        print("  must pass -- has been replaced by a raising guard for this whole run.")
        print(f"  guard triggered : {guard.fired if guard else 'n/a'} time(s)")
        print("  live calls made : 0")
        print("  spent           : $0.0000")
        print(f"  would spend     : ${projected:.4f} for {total.calls} call(s) "
              f"under --live")
        _footer(conn, started, None, mode)
        return 0

    # ----------------------------------------------- max-spend, BEFORE any spend
    if mode == "live" and projected > max_spend:
        _rule("REFUSED  ·  projected spend exceeds --max-spend")
        print(f"  projected ${projected:.4f} > cap ${max_spend:.2f}")
        print("  Nothing was called and nothing was spent. Raise --max-spend "
              "deliberately, or narrow")
        print("  the queue with --limit N.")
        return 2

    if mode == "live" and not (os.environ.get("ANTHROPIC_API_KEY")
                               or config.ANTHROPIC_API_KEY):
        _rule("REFUSED  ·  --live with no ANTHROPIC_API_KEY")
        print("  Set the key, or drop --live to replay from the committed cache.")
        return 2

    llm = CachedLLM(live=(mode == "live"))

    # -------------------------------------------------------------- 4. dispatch
    _rule("4. dispatch  ·  deterministic routing; low confidence escalates to the judge")
    all_runs: list[dict] = []
    dispatches: list[Dispatch] = []
    for agent, _stale in queue:
        try:
            items = list(agent.detects(conn))   # re-detect: upstream may have added work
        except Exception as exc:
            print(f"  ! {agent.name}.detects failed: {type(exc).__name__}: {exc}")
            items = []
        if limit:
            items = items[:limit]
        d = dispatch(agent, items, llm, conn, tick_id=tick_id)
        dispatches.append(d)
        all_runs.extend(d.runs)
        wrote = ", ".join(f"{v} -> {k}" for k, v in sorted(d.written.items())) or "-"
        flag = f", {len(d.reviews)} flagged for review" if d.reviews else ""
        print(f"  {agent.name:20s} {len(items):4d} item(s)   {wrote}{flag}"
              + (f"\n      {d.note}" if d.note else ""))

    if all_runs:
        store.upsert(conn, "agent_runs", all_runs)

    # --------------------------------------------------------------- 5. changes
    _rule("5. change detection  ·  a set difference on ids, deliberately not an agent")
    changes = detect_changes(conn, before_ids, _entity_ids(conn))
    if changes:
        store.upsert(conn, "changes", changes)
    kinds: dict[str, int] = {}
    for c in changes:
        kinds[c["change_type"]] = kinds.get(c["change_type"], 0) + 1
    print(f"  changes written : {len(changes)}"
          + (f"   ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})"
             if kinds else ""))

    # ---------------------------------------------------------------- 6. export
    if do_export:
        _rule("6. export")
        try:
            files = store.export(conn)
            print(f"  wrote {len(files)} file(s) to {config.DATA}")
        except Exception as exc:
            print(f"  ! export failed: {type(exc).__name__}: {exc}")

    return _footer(conn, started, llm, mode, dispatches)


def _footer(conn, started, llm, mode, dispatches: list[Dispatch] | None = None) -> int:
    _rule("summary")
    for table, n in store.counts(conn).items():
        print(f"  {table:14s} {n:6d} rows")
    if llm is not None:
        print(f"  {llm.usage.summary()}")
    print(f"  wall clock     : {time.perf_counter() - started:.2f}s")

    if dispatches is None:
        return 0

    produced = sum(sum(d.written.values()) for d in dispatches)
    failures = {k: v for d in dispatches for k, v in d.failures.items()}

    if produced == 0 and failures and mode == "offline":
        _rule("COLD CACHE  ·  nothing could be replayed (exit 3, not a crash)")
        cold = [v for v in failures.values() if "NotCachedError" in v]
        nohtml = [v for v in failures.values() if "no cached article" in v]
        print(f"  {len(failures)} item(s) could not be produced offline.")
        if nohtml:
            print(f"  {len(nohtml)}: the source article HTML is not on disk. "
                  f"raw/*.html is gitignored")
            print("     (regenerable, and free -- no model, no key):")
            print("       python run.py fetch 5")
        if cold:
            print(f"  {len(cold)}: the model response is not in cache/llm/ "
                  f"({cache_stats()['entries']} entries committed).")
            print("     Populate it once, deliberately:")
            print("       ANTHROPIC_API_KEY=... python run.py live")
            print("     After that `python run.py demo` replays it for $0.00 with no key.")
        first_key, first_val = next(iter(failures.items()))
        print(f"  first failure   : {first_key} -> {first_val[:180]}")
        return 3

    if failures and mode != "offline":
        print(f"  ! {len(failures)} item(s) failed; "
              f"first: {next(iter(failures.values()))[:180]}")
        return 1
    return 0


# ================================================================================
# promote-skills -- the gate
# ================================================================================

def _candidate_path(agent_name: str) -> pathlib.Path:
    return SKILLS_DIR / f"{agent_name}.candidates.md"


def golden_gate_available() -> tuple[bool, str]:
    """Is there a measuring stick? Depended on by interface, never stubbed.

    The interface is the one `.claude/skills/golden-verify` documents: hand-verified
    fixtures under `tests/golden/`, run by `pytest tests/test_golden.py`.
    """
    if not GOLDEN_DIR.exists():
        return False, f"{GOLDEN_DIR} does not exist"
    fixtures = [p for p in GOLDEN_DIR.rglob("*") if p.is_file()]
    if not fixtures:
        return False, f"{GOLDEN_DIR} is empty"
    if not GOLDEN_TEST.exists():
        return False, f"{GOLDEN_TEST} does not exist"
    return True, f"{len(fixtures)} fixture file(s), runner {GOLDEN_TEST.name}"


def run_golden() -> tuple[bool, str]:
    """Run the golden set. Must hit the committed cache and cost nothing."""
    proc = subprocess.run([sys.executable, "-m", "pytest", str(GOLDEN_TEST), "-q"],
                          cwd=ROOT, capture_output=True, text=True)
    tail = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode == 0, tail[-1] if tail else f"exit {proc.returncode}"


def reextraction_cost(agent_name: str, *,
                      raw_dir: pathlib.Path | None = None) -> Estimate:
    """What promoting a rule for `agent_name` would cost.

    The skills file is part of the prompt and CachedLLM keys on the prompt, so every
    cached response for this agent is invalidated by the edit. This prices that
    re-run BEFORE it happens -- the whole reason promotion is gated.
    """
    if agent_name == "extract":
        adapter = ExtractAgent(raw_dir=raw_dir)
        ids = [str(r.get("article_id")) for r in adapter._m.load_manifest(raw_dir)]
        # count_cache=False: a skills edit changes the prompt, so nothing is cached
        # any more and the whole corpus must be re-extracted.
        est = adapter.estimate(ids, count_cache=False)
        est.basis += " · every cached response invalidated by the prompt change"
        return est

    agent = next((a for a in build_registry(raw_dir=raw_dir)
                  if a.name == agent_name), None)
    if agent is None:
        return Estimate(agent=agent_name, basis="agent not registered")
    conn = store.init_db(config.DB_PATH)
    try:
        items = list(agent.detects(conn))
    finally:
        conn.close()
    est = generic_estimate(agent, items)
    est.cached_calls = 0
    est.basis += " · every cached response invalidated by the prompt change"
    return est


def promote_skills(agent_name: str | None = None, rule_path: str | None = None,
                   *, apply: bool = False,
                   raw_dir: pathlib.Path | None = None) -> int:
    """`manager promote-skills`. Explicitly gated, never automatic.

    Order matters: price the consequence, then check the gate, and only then touch a
    prompt. Promoting without a passing golden set is refused outright -- an ungated
    self-modifying prompt is the failure mode this whole design exists to avoid, and
    a fake gate would be worse than no gate.
    """
    print("=" * 78)
    print("manager promote-skills  ·  a gated step, never automatic")
    print("=" * 78)

    agents = build_registry(raw_dir=raw_dir)
    names = [a.name for a in agents]
    targets = [agent_name] if agent_name else names
    unknown = [t for t in targets if t not in names]
    if unknown:
        print(f"! unknown agent(s) {unknown}. Registered: {', '.join(names)}")
        return 1

    _rule("1. candidate rules")
    candidates: dict[str, pathlib.Path] = {}
    for t in targets:
        p = pathlib.Path(rule_path) if rule_path else _candidate_path(t)
        if p.exists() and p.read_text(encoding="utf-8").strip():
            candidates[t] = p
            print(f"  {t:20s} {p}")
        else:
            print(f"  {t:20s} no candidate rules at {p}")
    if not candidates:
        print("  Nothing to promote. Agents stage rules in skills/<agent>.candidates.md;")
        print("  every rule must cite the case that motivated it (see skills/*.md).")
        return 0

    _rule("2. cost of promotion, priced BEFORE anything is changed")
    grand = 0.0
    for t in candidates:
        est = reextraction_cost(t, raw_dir=raw_dir)
        grand += est.cost_usd()
        print(f"  {t:20s} {est.calls} call(s) re-run, "
              f"{est.input_tokens:,} in / {est.output_tokens:,} out, "
              f"${est.cost_usd():.4f}")
        print(f"  {'':20s} {est.basis}")
    print(f"  {'TOTAL':20s} ${grand:.4f}")
    print("  Why: the skills file is injected into the prompt and CachedLLM keys on the")
    print("  prompt, so editing it invalidates that agent's entire cache.")

    _rule("3. golden gate")
    ok, why = golden_gate_available()
    if not ok:
        rel_dir = GOLDEN_DIR.relative_to(ROOT).as_posix()
        rel_test = GOLDEN_TEST.relative_to(ROOT).as_posix()
        print(f"  REFUSED: {why}")
        print("  A candidate rule is accepted only if it does not regress the golden set.")
        print(f"  Without {rel_dir} there is no measuring stick, so 'the agent improved'")
        print("  is unfalsifiable. Nothing was promoted and no skills file was touched.")
        print(f"  To satisfy the gate: hand-verified fixtures in {rel_dir}, plus a runner")
        print(f"  at {rel_test}.")
        return 2
    print(f"  available: {why}")

    baseline_ok, baseline = run_golden()
    print(f"  baseline: {'PASS' if baseline_ok else 'FAIL'} -- {baseline}")
    if not baseline_ok:
        print("  REFUSED: the golden set does not pass before the change, so it cannot")
        print("  tell us anything about the change. Fix the baseline first.")
        return 2

    if not apply:
        print()
        print("  Gate is open. Re-run with --apply to append the candidate rules and")
        print("  re-verify. This run changed nothing.")
        return 0

    _rule("4. apply, re-verify, revert on regression")
    rc = 0
    for t, cand in candidates.items():
        agent = next(a for a in agents if a.name == t)
        skills = pathlib.Path(agent.skills_path())
        original = skills.read_text(encoding="utf-8") if skills.exists() else ""
        rule = cand.read_text(encoding="utf-8").strip()
        skills.parent.mkdir(parents=True, exist_ok=True)
        skills.write_text(original.rstrip() + "\n\n" + rule + "\n", encoding="utf-8")
        after_ok, after = run_golden()
        if after_ok:
            print(f"  {t}: PROMOTED -- {after}")
            cand.unlink()
        else:
            skills.write_text(original, encoding="utf-8")
            print(f"  {t}: REVERTED -- golden regressed: {after}")
            rc = 1
    return rc


# ================================================================================
# selftest -- everything below is free: no API key, no network, no spend
# ================================================================================

def _ok(cond: bool, label: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def _modules_importing(package: str, root: pathlib.Path | None = None) -> list[str]:
    """Files that really import `package`, parsed rather than grepped.

    A plain `grep -rn "import anthropic" src/` also matches src/store.py, whose
    docstring says "there is deliberately no `import anthropic`" -- prose about the
    rule, not a violation of it. The AST answers the question the rule actually asks.
    """
    import ast
    hits: set[str] = set()
    for p in sorted((root or _SRC).rglob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == package for a in node.names):
                    hits.add(p.relative_to(ROOT).as_posix())
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] == package:
                    hits.add(p.relative_to(ROOT).as_posix())
    return sorted(hits)


def _code_only(source: str) -> str:
    """Source with every string literal and comment removed.

    Lets a check ask "does the *logic* mention this?" without tripping over a
    docstring that explains why the logic deliberately does not.
    """
    import io
    import tokenize
    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            out.append(tok.string)
    except tokenize.TokenError:
        return source
    return " ".join(out)


def _head(n: str) -> None:
    print()
    print("=" * 78)
    print(n)
    print("=" * 78)


class _StubAgent:
    """A scripted agent, used to prove escalation and recording without spending."""

    name = "stub_resolver"

    def __init__(self, confidences: dict[str, list[float]]):
        self.confidences = {k: list(v) for k, v in confidences.items()}
        self.calls: list[tuple[str, list]] = []

    def skills_path(self) -> pathlib.Path:
        return SKILLS_DIR / "does_not_exist.md"

    def detects(self, conn) -> list[str]:
        return sorted(self.confidences)

    def _row(self, name: str, model: str, conf: float) -> dict:
        row = {c: None for c in store.columns("entities")}
        row.update({"contractor_raw": name, "normalized_name": name.lower(),
                    "relationship": "unknown", "is_public": False, "confidence": conf,
                    "reasoning": "stub", "resolved_at": _now(),
                    "resolver_model": model, "skills_version": "none"})
        return row

    def run(self, items, llm, **kw) -> list[dict]:
        model = kw.get("model") or BULK_MODEL
        names = [it if isinstance(it, str) else it.get("contractor_raw") for it in items]
        self.calls.append((model, names))
        out = []
        for name in names:
            seq = self.confidences.get(name) or [0.5]
            conf = seq.pop(0) if len(seq) > 1 else seq[0]
            out.append(self._row(name, model, conf))
        return out


class _PriceyAgent(_StubAgent):
    """Stubbed projection well over any sane cap, to exercise --max-spend."""

    name = "pricey"

    def estimate(self, items) -> Estimate:
        return Estimate(agent="pricey", items=len(items), calls=1000,
                        input_tokens=50_000_000, output_tokens=1_000_000,
                        model=BULK_MODEL, basis="stubbed projection")


def selftest() -> int:
    import tempfile
    fails = 0
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="manager_selftest_"))
    mine = (_SRC / "manager.py").read_text(encoding="utf-8")
    original_json_call = CachedLLM.json_call

    def _exploding(self, **kw):
        raise AssertionError("TRIPWIRE: CachedLLM.json_call was reached")

    # ---------------------------------------------------------------------- 1
    _head("1. the frozen contract is intact")
    proc = subprocess.run([sys.executable, str(_SRC / "schemas.py")],
                          cwd=ROOT, capture_output=True, text=True)
    fails += not _ok(proc.returncode == 0 and "schema version" in proc.stdout,
                     "src/schemas.py still prints the contract")
    print("      " + (proc.stdout.strip().splitlines() or [""])[0])
    diff = subprocess.run(["git", "diff", "main", "--", "src/schemas.py"],
                          cwd=ROOT, capture_output=True, text=True)
    fails += not _ok(diff.returncode == 0 and not diff.stdout.strip(),
                     "git diff main -- src/schemas.py is empty")

    # ---------------------------------------------------------------------- 2
    _head("2. nothing outside src/llm.py imports anthropic")
    grepped = sorted(p.relative_to(ROOT).as_posix() for p in _SRC.rglob("*.py")
                     if "import anthropic" in p.read_text(encoding="utf-8", errors="replace"))
    real = _modules_importing("anthropic")
    print(f"      grep -rn 'import anthropic' src/ : {grepped}")
    print(f"      files that actually import it (AST): {real}")
    print("      the difference is prose: store.py's and manager.py's docstrings "
          "state the rule.")
    fails += not _ok(real == ["src/llm.py"],
                     "src/llm.py is the only module that imports anthropic")
    fails += not _ok("anthropic" not in _code_only(mine),
                     "src/manager.py's code never even names anthropic")

    # ---------------------------------------------------------------------- 3
    _head("3. `run.py tick` (tick --dry-run) makes ZERO model calls under a tripwire")
    CachedLLM.json_call = _exploding
    try:
        before = cache_stats()["entries"]
        rc = tick("dry-run", db_path=tmp / "dry.duckdb")
        after = cache_stats()["entries"]
        fails += not _ok(rc == 0, "tick --dry-run ran to completion (rc=0)")
        fails += not _ok(before == after,
                         f"cache/llm entry count unchanged ({before} -> {after})")
    except AssertionError as exc:
        fails += not _ok(False, f"tripwire FIRED: {exc}")
    finally:
        CachedLLM.json_call = original_json_call

    # ---------------------------------------------------------------------- 4
    _head("4. `run.py demo` with ANTHROPIC_API_KEY unset")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    demo = subprocess.run([sys.executable, str(ROOT / "run.py"), "demo"],
                          cwd=ROOT, capture_output=True, text=True, env=env)
    out = demo.stdout + demo.stderr
    fails += not _ok("ANTHROPIC_API_KEY" not in env, "the key really was unset")
    fails += not _ok("Traceback (most recent call last)" not in out, "no traceback")
    fails += not _ok(demo.returncode in (0, 3),
                     f"exit {demo.returncode} (0 = replayed, 3 = cold cache, explained)")
    fails += not _ok("COLD CACHE" in out or "-> awards" in out,
                     "the run says what it produced, or exactly what is missing")
    print("      last lines:")
    for line in out.strip().splitlines()[-8:]:
        print("        " + line)

    # ---------------------------------------------------------------------- 5
    _head("5. agent discovery: dynamic, and tolerant of an absent agent")
    names = [a.name for a in build_registry(verbose=True)]
    fails += not _ok("resolve_entity" in names,
                     "discovery finds resolve_entity with nothing hardcoded")
    fails += not _ok("extract" in names, "extract.py is adapted onto the protocol")
    fails += not _ok("score_materiality" not in names,
                     "score_materiality is absent -- and that is not an error")
    ordered = sorted(names, key=lambda n: (STAGE_ORDER.index(n)
                                           if n in STAGE_ORDER else len(STAGE_ORDER), n))
    fails += not _ok(names == ordered, f"stages are in dependency order: {names}")

    synth = tmp / "synthetic_agents"
    synth.mkdir(parents=True, exist_ok=True)
    (synth / "score_materiality.py").write_text(
        "import pathlib\n"
        "class ScoreMaterialityAgent:\n"
        "    name = 'score_materiality'\n"
        "    def skills_path(self): return pathlib.Path('skills/materiality.md')\n"
        "    def detects(self, conn): return ['abc123']\n"
        "    def run(self, items, llm, **kw): return []\n",
        encoding="utf-8")
    dropped = [a.name for a in build_registry(synth)]
    fails += not _ok("score_materiality" in dropped,
                     f"a newly dropped agent file registers with no edit here: {dropped}")
    # STAGE_ORDER's members and every docstring are string literals, so stripping
    # literals leaves only logic. No branch, no lookup, no table name is keyed on it.
    fails += not _ok("score_materiality" not in _code_only(mine),
                     "no logic in manager.py names score_materiality -- only an "
                     "ordering hint and prose")

    # ---------------------------------------------------------------------- 6
    _head("6. --max-spend aborts BEFORE any call")
    pricey = _PriceyAgent({"Expensive Co": [0.9]})
    print(f"      stubbed projection: ${pricey.estimate(['x']).cost_usd():.2f} "
          f"against a $5.00 cap")
    CachedLLM.json_call = _exploding
    try:
        rc = tick("live", max_spend=5.00, db_path=tmp / "cap.duckdb",
                  registry=[pricey], use_rss=False, do_export=False)
        fails += not _ok(rc == 2, f"tick --live refused with rc=2 (got {rc})")
        fails += not _ok(pricey.calls == [],
                         "the agent was never run -- nothing was spent")
    except AssertionError as exc:
        fails += not _ok(False, f"a call was attempted before the cap check: {exc}")
    finally:
        CachedLLM.json_call = original_json_call

    # ---------------------------------------------------------------------- 7
    _head("7. escalation: 0.5 -> judge model -> review_queue, in a real DuckDB")
    conn = store.init_db(tmp / "esc.duckdb")
    stub = _StubAgent({"Persistently Unclear LLC": [0.5, 0.5],
                       "Fixed On Retry Inc": [0.5, 0.95]})
    wire = Tripwire()
    d = dispatch(stub, stub.detects(conn), wire, conn, tick_id="T-esc")
    print(f"      stub runs: {stub.calls}")
    fails += not _ok(JUDGE_MODEL in [m for m, _ in stub.calls],
                     f"low confidence retried on JUDGE_MODEL ({JUDGE_MODEL})")
    esc = [r for r in d.runs if r["escalated"]]
    fails += not _ok(len(esc) == 2, "both low-confidence items got an escalation run row")
    fails += not _ok(any(r["outcome"] == "escalated" for r in esc),
                     "the item that recovered is recorded outcome=escalated")
    fails += not _ok(any(r["outcome"] == "flagged" for r in esc),
                     "the item that did not recover is recorded outcome=flagged")
    rq = store.review_queue(conn)
    print(f"      review_queue: {[(r['item_key'], r['confidence']) for r in rq]}")
    fails += not _ok(len(rq) == 1 and rq[0]["item_key"] == "Persistently Unclear LLC",
                     "the still-uncertain item is really in review_queue in DuckDB")
    fails += not _ok(bool(rq) and str(LOW_CONFIDENCE) in (rq[0]["reason"] or ""),
                     "the review row says why a human is needed")
    fixed = [r for r in d.results if r["contractor_raw"] == "Fixed On Retry Inc"]
    fails += not _ok(bool(fixed) and fixed[0]["confidence"] == 0.95,
                     "the escalated answer replaced the low-confidence one")
    fails += not _ok(wire.fired == 0,
                     "no model call was attempted (a stub agent did the work)")

    # ---------------------------------------------------------------------- 8
    _head("8. every dispatch writes an agent_runs row with contract-exact keys")
    expected = store.columns("agent_runs")
    bad = [r for r in d.runs if list(r.keys()) != expected]
    fails += not _ok(bool(d.runs) and not bad,
                     f"{len(d.runs)} run row(s), keys == store.columns('agent_runs')")
    store.upsert(conn, "agent_runs", d.runs)
    persisted = store.query(conn, "SELECT * FROM agent_runs ORDER BY run_id")
    fails += not _ok(len(persisted) == len(d.runs),
                     f"they round-trip through DuckDB ({len(persisted)} rows)")
    fails += not _ok(all(r["cost_usd"] is not None for r in persisted),
                     "every agent_runs row carries a cost")
    fails += not _ok(all(r["skills_version"] for r in persisted),
                     "every agent_runs row carries a skills version")
    print("      sample: " + json.dumps({k: str(persisted[0][k]) for k in (
        "agent", "item_key", "model", "escalated", "cache_hit", "cost_usd",
        "confidence", "outcome")}))
    fails += not _ok(list(_review_row().keys()) == store.columns("review_queue"),
                     "review_queue rows are shaped from the contract too")

    # ---------------------------------------------------------------------- 9
    _head("9. change detection is a set difference, with zero model calls")
    CachedLLM.json_call = _exploding
    try:
        prior = {"announcements": {"4586879", "4585002"}, "awards": {"aaaa", "bbbb"}}
        now = {"announcements": {"4586879", "4585002", "4590000"},
               "awards": {"aaaa", "bbbb", "cccc"}}
        fails += not _ok(new_ids(prior["announcements"], now["announcements"]) == ["4590000"],
                         "new announcement ids found by set difference")
        rows = detect_changes(conn, prior, now)
        kinds = sorted(r["change_type"] for r in rows)
        fails += not _ok(kinds == ["new_announcement", "new_award"],
                         f"one change row per new id: {kinds}")
        fails += not _ok(list(rows[0].keys()) == store.columns("changes"),
                         "change rows are contract-shaped")
        again = detect_changes(conn, prior, now)
        fails += not _ok([r["change_id"] for r in rows] == [r["change_id"] for r in again],
                         "change ids are deterministic, so a re-run converges")
    except AssertionError as exc:
        fails += not _ok(False, f"a model call was attempted during change detection: {exc}")
    finally:
        CachedLLM.json_call = original_json_call
    conn.close()

    # --------------------------------------------------------------------- 10
    _head("10. promote-skills refuses to promote while tests/golden/ is absent")
    available, why = golden_gate_available()
    print(f"      gate: available={available} -- {why}")
    cand = _candidate_path("extract")
    created = False
    if not cand.exists():
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text(
            "## R-999 · selftest candidate\n"
            "A deliberately unproven rule, staged only to exercise the gate.\n\n"
            f"- learned_from: manager selftest\n"
            f"- added: {_now()} · by: manager · confidence: 0.10\n", encoding="utf-8")
        created = True
    skills_file = SKILLS_DIR / "extraction.md"
    skills_before = skills_file.read_text(encoding="utf-8")
    rc = promote_skills("extract", apply=True)
    skills_after = skills_file.read_text(encoding="utf-8")
    if created:
        cand.unlink()
    if available:
        fails += not _ok(rc in (0, 1, 2), "gate present: promote-skills ran the gate")
    else:
        fails += not _ok(rc == 2, f"refused with rc=2 (got {rc})")
    fails += not _ok(skills_before == skills_after,
                     "skills/extraction.md was NOT touched")

    # ------------------------------------------------------------------ verdict
    print()
    print("=" * 78)
    print(f"{'ALL CHECKS PASSED' if fails == 0 else f'{fails} CHECK(S) FAILED'}")
    print("=" * 78)
    print(f"cache/llm holds {cache_stats()['entries']} response(s). This selftest "
          f"spent $0.00 and made no network call.")
    return fails


# ================================================================================
# CLI -- run.py calls these exact commands
# ================================================================================

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(prog="manager", description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="offline validation, free")
    sub = ap.add_subparsers(dest="command")

    t = sub.add_parser("tick", help="one refresh cycle")
    mode = t.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="plan and price the queue; ZERO model calls")
    mode.add_argument("--offline", action="store_true",
                      help="replay from the committed cache; no API key needed")
    mode.add_argument("--live", action="store_true",
                      help="may call the API for genuinely new inputs")
    t.add_argument("--max-spend", type=float, default=DEFAULT_MAX_SPEND, metavar="USD",
                   help=f"abort before spending if the projection exceeds this "
                        f"(default {DEFAULT_MAX_SPEND})")
    t.add_argument("--db", default=None, help="DuckDB path (default data/contracts.duckdb)")
    t.add_argument("--limit", type=int, default=None, help="cap items per stage")
    t.add_argument("--poll-rss", dest="poll_rss", action="store_true", default=None,
                   help="poll the change feed (default: only in --live)")
    t.add_argument("--no-poll-rss", dest="poll_rss", action="store_false")
    t.add_argument("--no-export", dest="export", action="store_false", default=None)

    p = sub.add_parser("promote-skills", help="gated promotion of a learned rule")
    p.add_argument("--agent", default=None, help="which agent's rules (default: all)")
    p.add_argument("--rule", default=None, help="candidate rule file")
    p.add_argument("--apply", action="store_true",
                   help="actually promote, if the golden gate passes")

    sub.add_parser("selftest", help="offline validation, free")
    sub.add_parser("agents", help="list discovered agents and exit")

    args = ap.parse_args(argv)

    if args.selftest or args.command == "selftest":
        return selftest()
    if args.command == "agents":
        for a in build_registry(verbose=True):
            print(f"  {a.name:20s} skills={_skills_version(a)}  path={a.skills_path()}")
        return 0
    if args.command == "promote-skills":
        return promote_skills(args.agent, args.rule, apply=args.apply)
    if args.command == "tick":
        mode_name = "live" if args.live else "offline" if args.offline else "dry-run"
        return tick(mode_name, max_spend=args.max_spend, db_path=args.db,
                    limit=args.limit, use_rss=args.poll_rss, do_export=args.export)

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
