"""Contractor name -> public company, in three tiers.

    TIER 0   data/entity_map.json   already resolved       free, instant, permanent
    TIER 1   data/universe.csv      deterministic alias     free, instant, reproducible
    TIER 2   claude-haiku-4-5       batched model call      costs money, cached forever
    TIER 2b  claude-opus-5          escalation on conf<0.7  costs more, cached forever

The architecture is the point. Resolution is overwhelmingly a *dictionary lookup*
problem wearing a reasoning problem's clothes: "Rockwell Collins Inc." is RTX because
the alias list says so, and CLAUDE.md is explicit that if a dictionary lookup would
answer it, a model call is a bug. So tier 1 answers every name it can, for nothing,
with a fully reproducible result. Only the residue -- names no alias covers -- reaches
a model, and it reaches it in *batches*, because the cost lever on tier 2 is names per
call, not tokens per name.

Tier 0 closes the loop: whatever tier 2 concludes is written to data/entity_map.json
keyed by the NORMALIZED name, so the same contractor is never reasoned about twice.

THE TWO KEYS -- the single most important property of this file
--------------------------------------------------------------
There are deliberately two different keys, and confusing them costs money twice:

  entities.contractor_raw   RAW, byte-for-byte as printed, trailing `*` and all.
                            src/store.py keys the entities table on this string and
                            joins `entities.contractor_raw = awards.contractor_raw`.
                            agent-extract's rule R-004 keeps the small-business
                            asterisk in awards.contractor_raw "exactly as printed".
                            Normalize it here and the join silently returns nothing.
                            => ONE entities ROW per distinct printed spelling.

  entity_map / batch key    NORMALIZED. "Action Manufacturing Co.,*" and "Action
                            Manufacturing Co." are one company and must be REASONED
                            about once, then fanned out to both raw variants.
                            => ONE MODEL CALL SLOT per distinct company.

Neither needs a schema change: src/schemas.py stays frozen.

Everything here is honest about uncertainty. Most DoD contractors are small private
firms; `relationship="private"`, `ticker=null` is the correct and common answer, not a
failure, and the prompt says so. `confidence` below 0.7 escalates to Opus and then to
the review queue -- a truthful 0.5 beats a confident wrong ticker.

Public surface
    resolve_names(names, llm)        -> {raw_name: entities row}     # manager entry point
    resolve(names, llm)              -> ResolveResult (records, pending, review, stats)
    normalize(raw)                   -> deterministic match key
    tier1_match(raw)                 -> record or None                # never calls a model
    plan(names)                      -> (cached, tier1, tier2_queue)  # never calls a model
    ResolveEntityAgent               -> the CleaningAgent protocol shape

CLI (all free, no API key, no network, no spend):
    .venv/Scripts/python.exe src/agents/resolve_entity.py --selftest
    .venv/Scripts/python.exe src/agents/resolve_entity.py --coverage
    .venv/Scripts/python.exe src/agents/resolve_entity.py --warm
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as _html
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

# src/ is the import root for this project (llm.py does `from config import ...`),
# and this module lives one level down in src/agents/.
_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import config                                      # noqa: E402
import schemas                                     # noqa: E402
from llm import CachedLLM, NotCachedError, Usage    # noqa: E402  (the ONLY model access)

# --------------------------------------------------------------------------------
# paths and constants
# --------------------------------------------------------------------------------

UNIVERSE_PATH = config.DATA / "universe.csv"
ENTITY_MAP_PATH = config.DATA / "entity_map.json"
SKILLS_PATH = config.ROOT / "skills" / "entity_resolution.md"

BULK_MODEL = config.BULK_MODEL          # claude-haiku-4-5
JUDGE_MODEL = config.JUDGE_MODEL        # claude-opus-5, escalation only

#: names per tier-2 call. The cost lever: many names in one call bills one system
#: prompt and one universe listing instead of one of each per name.
BATCH_SIZE = 25

#: CLAUDE.md: below this the manager escalates to Opus, then to review_queue.
LOW_CONFIDENCE = 0.7

DETERMINISTIC = "deterministic"

_ENTITY_FIELDS, _ENTITY_PK = schemas.TABLES["entities"]

#: the relationship enum, read out of the frozen contract rather than retyped.
RELATIONSHIPS = tuple(
    v for v in next(f for f in _ENTITY_FIELDS if f.name == "relationship").json["enum"]
    if v is not None
)

#: exactly the columns of schemas.TABLES["entities"], in order.
ENTITY_KEYS = [f.name for f in _ENTITY_FIELDS]

#: the deterministic (non-model) subset -- these must never appear in a prompt schema.
DETERMINISTIC_KEYS = [f.name for f in _ENTITY_FIELDS if not f.llm]

#: review_queue column names, also derived from the contract.
REVIEW_KEYS = [f.name for f in schemas.TABLES["review_queue"][0]]

#: legal-form noise. Carries no identity information, so it is removed on both sides
#: of every comparison (skills R-003).
_LEGAL_TOKENS = frozenset({
    "inc", "incorporated", "llc", "llp", "lp", "plc", "ltd", "limited",
    "corp", "corporation", "co", "company", "companies", "gmbh", "sarl", "srl",
    "spa", "nv", "bv", "pty", "pte",
})


# --------------------------------------------------------------------------------
# tier 1a -- normalization (deterministic, free, total)
# --------------------------------------------------------------------------------

def normalize(raw: str) -> str:
    """Collapse a printed contractor name to its match key.

    Strips the small-business asterisk, folds ``&`` to ``and``, deletes the periods
    that distinguish ``L.L.C.`` from ``LLC``, drops every legal-form token and a
    leading ``The``, collapses whitespace and casefolds. Applied identically to raw
    names and to universe aliases, so the two can be compared as plain strings.

    This is the KEY of data/entity_map.json and of tier-2 batching -- NEVER the key of
    the entities table. See the module docstring: entities.contractor_raw stays raw.

        >>> normalize("Rockwell Collins Inc.")
        'rockwell collins'
        >>> normalize("The Boeing Co.")
        'boeing'
        >>> normalize("AAECON General Contracting LLC*")
        'aaecon general contracting'
        >>> normalize("Action Manufacturing Co.,*") == normalize("Action Manufacturing Co.")
        True
    """
    if not raw:
        return ""
    s = raw.replace("*", " ").strip()             # the small-business marker, anywhere
    s = s.casefold()
    s = s.replace("&", " and ")
    s = s.replace(".", "")                        # L.L.C. -> LLC, Inc. -> Inc
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = [t for t in s.split() if t]
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    stripped = [t for t in tokens if t not in _LEGAL_TOKENS]
    # A name made ENTIRELY of legal-form tokens would normalize to the empty string
    # and silently merge with every other such name. Keep the unstripped tokens.
    return " ".join(stripped or tokens)


_SUFFIX_RE = re.compile(
    r"[\s,]+(?:inc|incorporated|llc|l\.l\.c|llp|lp|plc|ltd|limited|corp|corporation|"
    r"co|company|gmbh|pty|pte|nv|bv)\.?\s*$",
    re.IGNORECASE,
)


def display_name(raw: str) -> str:
    """A human-readable canonical name: original casing, legal suffixes removed."""
    s = (raw or "").replace("*", " ").strip().rstrip(",").strip()
    s = re.sub(r"^[Tt]he\s+", "", s)
    prev = None
    while prev != s:                              # "Foo Holdings Inc. Corp." -> "Foo Holdings"
        prev = s
        s = _SUFFIX_RE.sub("", s).strip().rstrip(",").strip()
    return s or (raw or "").strip()


# --------------------------------------------------------------------------------
# tier 1b -- the alias index
# --------------------------------------------------------------------------------

#: markers that make a name a joint venture rather than a wholly-owned unit
_JV_RE = re.compile(r"\b(?:jv|joint\s+ventures?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Alias:
    key: str            # normalized
    display: str        # as written in universe.csv
    ticker: str
    company: str
    company_key: str

    @property
    def is_company_level(self) -> bool:
        """True when this alias names the listed parent itself, not a division."""
        return self.key == self.company_key or self.company_key.startswith(self.key + " ")


@dataclass
class Universe:
    by_ticker: dict[str, dict] = dc_field(default_factory=dict)
    exact: dict[str, Alias] = dc_field(default_factory=dict)
    prefixes: list[Alias] = dc_field(default_factory=list)   # longest first

    def match(self, raw: str) -> tuple[Alias, str] | None:
        """(alias, method) for the best deterministic match, or None."""
        key = normalize(raw)
        if not key:
            return None
        # A joint venture is a judgment call about ownership, not a lookup:
        # "XYZ-Lockheed Martin JV" is not a Lockheed subsidiary. Refuse it
        # deterministically and let tier 2 apply skills R-004.
        if _JV_RE.search(raw):
            return None
        hit = self.exact.get(key)
        if hit is not None:
            return hit, "alias_exact"
        # Prefix matching: "General Dynamics Land Systems Inc." starts with the alias
        # "General Dynamics", so it is General Dynamics. Restricted to aliases of two
        # or more tokens -- a single-token alias like "Boeing" or "SAIC" is too short
        # to be safe as a prefix, and a wrong ticker is worse than a tier-2 call.
        for alias in self.prefixes:
            if key.startswith(alias.key + " "):
                return alias, "alias_prefix"
        return None


_UNIVERSE: Universe | None = None


def load_universe(path: pathlib.Path = UNIVERSE_PATH, *, reload: bool = False) -> Universe:
    """Read data/universe.csv into an alias index. Cached per process."""
    global _UNIVERSE
    if _UNIVERSE is not None and not reload and path == UNIVERSE_PATH:
        return _UNIVERSE

    u = Universe()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or "").strip().upper()
            company = (row.get("company") or "").strip()
            if not ticker or not company:
                continue
            aliases = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
            u.by_ticker[ticker] = {"ticker": ticker, "company": company, "aliases": aliases}
            company_key = normalize(company)
            # The company's own name is an alias too -- "L3Harris Technologies Inc"
            # must resolve even though the alias list only carries "L3Harris".
            for text in [company] + aliases:
                key = normalize(text)
                if not key:
                    continue
                alias = Alias(key=key, display=text, ticker=ticker,
                              company=company, company_key=company_key)
                # First writer wins: an earlier row owns an ambiguous alias, and the
                # collision is loud rather than silent.
                if key in u.exact and u.exact[key].ticker != ticker:
                    sys.stderr.write(
                        f"! universe alias collision: {text!r} claimed by "
                        f"{u.exact[key].ticker} and {ticker}; keeping {u.exact[key].ticker}\n")
                    continue
                u.exact.setdefault(key, alias)
    u.prefixes = sorted(
        {a.key: a for a in u.exact.values() if len(a.key.split()) >= 2}.values(),
        key=lambda a: (-len(a.key.split()), -len(a.key), a.key),
    )
    if path == UNIVERSE_PATH:
        _UNIVERSE = u
    return u


# --------------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def skills_text() -> str:
    if SKILLS_PATH.exists():
        return SKILLS_PATH.read_text(encoding="utf-8")
    return ""


def skills_version() -> str:
    """Content hash of the skills file.

    The skills text is part of the tier-2 system prompt, and CachedLLM keys on the
    prompt, so a skills edit necessarily invalidates this agent's cache. Versioning by
    content makes that relationship visible in the stored record instead of implicit.
    """
    text = skills_text()
    if not text:
        return "none"
    return "sk-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _record(**kw) -> dict:
    """An entities row: exactly the ENTITY_FIELDS keys, in schema order.

    The llm=False fields are filled here, deterministically, and never by the model.
    """
    rec = {k: kw.get(k) for k in ENTITY_KEYS}
    rec["contractor_raw"] = kw["contractor_raw"]            # RAW. never normalized.
    rec["resolved_at"] = rec["resolved_at"] or _now()
    rec["resolver_model"] = rec["resolver_model"] or DETERMINISTIC
    rec["skills_version"] = rec["skills_version"] or skills_version()
    return rec


def needs_escalation(rec: dict) -> bool:
    """CLAUDE.md's routing rule, in one place."""
    try:
        return float(rec.get("confidence") or 0.0) < LOW_CONFIDENCE
    except (TypeError, ValueError):
        return True


def validate_record(rec: dict) -> list[str]:
    """Contract check for one entities row. Returns a list of problems (empty = ok).

    Derived entirely from schemas.ENTITY_FIELDS -- no hardcoded column list.
    """
    problems: list[str] = []
    if list(rec) != ENTITY_KEYS:
        missing = [k for k in ENTITY_KEYS if k not in rec]
        extra = [k for k in rec if k not in ENTITY_KEYS]
        if missing:
            problems.append(f"missing fields: {missing}")
        if extra:
            problems.append(f"unknown fields: {extra}")
    for f in _ENTITY_FIELDS:
        v = rec.get(f.name)
        nullable = f.json.get("type") is None or "null" in (
            f.json.get("type") if isinstance(f.json.get("type"), list) else [f.json.get("type")]
        )
        if not f.llm:
            continue                                       # checked separately below
        if v is None and not nullable:
            problems.append(f"{f.name} is null but the contract forbids it")
        if v is not None and "enum" in f.json and v not in f.json["enum"]:
            problems.append(f"{f.name}={v!r} outside enum {f.json['enum']}")
    # deterministic fields: filled by us, never by the model
    for name in DETERMINISTIC_KEYS:
        if name == "llm_cache_key":
            continue                                       # legitimately null on alias hits
        if rec.get(name) in (None, ""):
            problems.append(f"deterministic field {name} not populated")
    if not isinstance(rec.get("confidence"), float):
        problems.append(f"confidence is {type(rec.get('confidence')).__name__}, not float")
    elif not 0.0 <= rec["confidence"] <= 1.0:
        problems.append(f"confidence {rec['confidence']} outside 0..1")
    if not isinstance(rec.get("is_public"), bool):
        problems.append("is_public is not a bool")
    if rec.get("relationship") not in RELATIONSHIPS:
        problems.append(f"relationship {rec.get('relationship')!r} not in {RELATIONSHIPS}")
    return problems


# --------------------------------------------------------------------------------
# tier 1c -- deterministic resolution
# --------------------------------------------------------------------------------

def tier1_match(raw: str, universe: Universe | None = None) -> dict | None:
    """Resolve `raw` from the alias table alone. Never calls a model. Never spends."""
    u = universe or load_universe()
    hit = u.match(raw)
    if hit is None:
        return None
    alias, method = hit
    key = normalize(raw)
    exact = method == "alias_exact"

    # The raw name IS the listed parent only when the matching alias names the parent
    # and the raw name adds nothing to it; anything longer is a division or unit.
    direct = alias.is_company_level and key == alias.key
    relationship = "direct" if direct else "subsidiary"

    if exact:
        confidence, how = 0.99, f"exact alias match on {alias.display!r}"
    else:
        confidence, how = 0.94, f"alias prefix match on {alias.display!r}"

    return _record(
        contractor_raw=raw,
        normalized_name=alias.display if exact else display_name(raw),
        ticker=alias.ticker,
        parent_company=alias.company,
        relationship=relationship,
        is_public=True,
        confidence=confidence,
        reasoning=(f"Deterministic {how} in data/universe.csv -> {alias.ticker} "
                   f"({alias.company}); no model call."),
        resolver_model=DETERMINISTIC,
        llm_cache_key=None,
    )


# --------------------------------------------------------------------------------
# tier 0 -- the persistent fact store, keyed by NORMALIZED name
# --------------------------------------------------------------------------------

#: extra bookkeeping stored in the map but never emitted in an entities row
_MAP_EXTRA = "raw_variants"


def load_entity_map(path: pathlib.Path = ENTITY_MAP_PATH) -> dict[str, dict]:
    """Load data/entity_map.json, keyed by NORMALIZED name.

    Missing or corrupt -> empty, never fatal. Keys are re-normalized on load, which
    makes the loader idempotent for the current format and self-migrating for any map
    that was written keyed by the raw name. On a collision the higher-confidence
    record wins.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"! entity_map unreadable ({exc}); starting empty\n")
        return {}
    if not isinstance(data, dict):
        return {}
    entries = data.get("entities", data)          # tolerate a wrapped or bare mapping
    if not isinstance(entries, dict):
        return {}
    out: dict[str, dict] = {}
    for key, rec in entries.items():
        if not isinstance(rec, dict):
            continue
        norm = normalize(rec.get("contractor_raw") or key) or normalize(key)
        if not norm:
            continue
        prev = out.get(norm)
        if prev is None or (rec.get("confidence") or 0) > (prev.get("confidence") or 0):
            merged = set(prev.get(_MAP_EXTRA, []) if prev else [])
            merged |= set(rec.get(_MAP_EXTRA, []) or [])
            if rec.get("contractor_raw"):
                merged.add(rec["contractor_raw"])
            rec = dict(rec)
            rec[_MAP_EXTRA] = sorted(merged)
            out[norm] = rec
        else:
            prev.setdefault(_MAP_EXTRA, [])
            extra = set(prev[_MAP_EXTRA]) | set(rec.get(_MAP_EXTRA, []) or [])
            if rec.get("contractor_raw"):
                extra.add(rec["contractor_raw"])
            prev[_MAP_EXTRA] = sorted(extra)
    return out


def save_entity_map(entries: dict[str, dict], path: pathlib.Path = ENTITY_MAP_PATH) -> pathlib.Path:
    """Write the map atomically, sorted, so git diffs stay reviewable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = ENTITY_KEYS + [_MAP_EXTRA]
    payload = {
        "schema_version": schemas.SCHEMA_VERSION,
        "key": "normalized contractor name (see resolve_entity.normalize); "
               "raw_variants lists every printed spelling that maps here. "
               "entities.contractor_raw stays RAW -- this key is not that key.",
        "updated_at": _now(),
        "n_entities": len(entries),
        "entities": {k: {f: entries[k].get(f) for f in keys}
                     for k in sorted(entries, key=str.casefold)},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _for_raw(rec: dict, raw: str) -> dict:
    """Project a resolved (per-company) record onto ONE raw printed variant.

    Exactly the ENTITY_FIELDS keys, with contractor_raw set to the byte-for-byte
    string src/store.py will join awards.contractor_raw against.
    """
    out = {k: rec.get(k) for k in ENTITY_KEYS}
    out["contractor_raw"] = raw
    return out


# --------------------------------------------------------------------------------
# planning -- who needs a model, decided before any model is touched
# --------------------------------------------------------------------------------

def group_variants(names) -> dict[str, list[str]]:
    """{normalized name: [raw variants, first-seen order]}.

    This is where duplicate spend dies: every printed spelling of one company lands in
    one group and is resolved once.
    """
    groups: dict[str, list[str]] = {}
    for raw in dict.fromkeys(n for n in names if n and n.strip()):
        norm = normalize(raw)
        if not norm:
            continue
        groups.setdefault(norm, []).append(raw)
    return groups


def representative(variants: list[str]) -> str:
    """The printed spelling shown to the model for a group of variants.

    The most informative one -- longest after suffix stripping, ties broken by the
    string itself -- so the model sees "Action Manufacturing Co." rather than a
    lowercased key. It is a function of the variant SET only, so the batch prompt (and
    therefore the cache key) is stable for as long as that set is.
    """
    return max(variants, key=lambda v: (len(display_name(v)), v))


def plan(names, entity_map: dict | None = None, universe: Universe | None = None,
         *, refresh: bool = False) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """Split names into (already-known, tier-1 hits, tier-2 queue). No model calls.

    Keys of the first two dicts are RAW names (one entry per printed variant, what the
    manager and src/store.py expect). The queue holds one REPRESENTATIVE raw name per
    normalized group -- N printed spellings of one company cost one slot in one batch,
    not N.

    Order of consultation is deliberate: the persisted map first (a fact we already
    own), then the alias table, and only then the queue that costs money.
    """
    emap = load_entity_map() if entity_map is None else entity_map
    u = universe or load_universe()
    cached: dict[str, dict] = {}
    tier1: dict[str, dict] = {}
    queue: list[str] = []
    for norm, variants in group_variants(names).items():
        known = None if refresh else emap.get(norm)
        if known is not None:
            for raw in variants:
                cached[raw] = _for_raw(known, raw)
            continue
        rep = representative(variants)
        hit = tier1_match(rep, u)
        if hit is not None:
            for raw in variants:
                tier1[raw] = _for_raw(hit, raw)
        else:
            queue.append(rep)
    return cached, tier1, queue


def batches(queue: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    """Chunk the tier-2 queue into deterministic batches.

    Sorted before chunking so the same set of unresolved names always produces the
    same batches and therefore the same cache keys. Adding one new name does reshuffle
    later batches -- but a name already in entity_map never reaches here, so that only
    ever costs on genuinely new information, which is the intended cost model.
    """
    ordered = sorted(dict.fromkeys(queue), key=str.casefold)
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


# --------------------------------------------------------------------------------
# tier 2 -- one model call for many names
# --------------------------------------------------------------------------------

def batch_schema() -> dict:
    """Array of ENTITY_FIELDS objects.

    The item shape comes from schemas.object_schema(), which emits only the llm=True
    fields. contractor_raw, resolved_at, resolver_model, llm_cache_key and
    skills_version are therefore structurally absent from the prompt -- they are ours
    to fill, and the model is never asked about them.
    """
    return {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "description": "Exactly one entry per input name, in the same order "
                               "as the numbered list, including names you are unsure "
                               "about.",
                "items": schemas.object_schema(_ENTITY_FIELDS),
            },
        },
        "required": ["entities"],
        "additionalProperties": False,
    }


def _universe_block(u: Universe) -> str:
    lines = []
    for t, row in u.by_ticker.items():
        lines.append(f"  {t:5s} {row['company']}  [aliases: {', '.join(row['aliases'])}]")
    return "\n".join(lines)


def build_system(universe: Universe | None = None) -> str:
    u = universe or load_universe()
    skills = skills_text().strip()
    return f"""You resolve U.S. Department of War contract-announcement contractor names to \
public companies, for an equity-research terminal.

Each name below was printed in a daily contract announcement. For each one, decide
whether it maps to a listed company and how.

WHAT MATTERS MOST
* Most DoD contractors are small PRIVATE firms: engineering shops, construction
  outfits, logistics providers, 8(a) and small-business awardees. Only awards above
  $7.5M are published, which is well within reach of a company nobody has heard of.
  `relationship="private"`, `ticker=null`, `is_public=false` is a CORRECT and COMMON
  answer. Give it confidently when it is right. Do NOT force a match onto a public
  company because the names rhyme.
* Resolve to the ULTIMATE public parent, not the operating unit. Rockwell Collins is
  RTX. Sikorsky is Lockheed Martin. Put the unit in `normalized_name`, the listed
  entity in `parent_company`, the exchange ticker in `ticker`, and set
  `relationship="subsidiary"`.
* `relationship` is one of: {", ".join(RELATIONSHIPS)}. direct = the raw name IS the
  listed company; subsidiary = a unit or acquired company of one.
* `normalized_name`: the company name with legal-form suffixes (Inc, LLC, Corp, Co.,
  Ltd, a leading "The") and the small-business asterisk removed. Never null.
* `reasoning`: ONE sentence saying how you know. Name the acquisition or the parent.
* `confidence` is used, not decorative. Below {LOW_CONFIDENCE} this result is re-run on a
  stronger model and then shown to a human, which is exactly what should happen when
  you are guessing. Calibrate: 0.95+ you are certain; 0.8 confident; 0.6 plausible;
  0.3 a guess. A truthful 0.5 is far more useful than a confident wrong ticker. Never
  report high confidence on a company you do not actually recognise -- an unfamiliar
  name is evidence of "private", and if you cannot tell, say `unknown` with low
  confidence.
* If a name may have changed ownership after your knowledge cutoff, resolve to the
  ownership you know, cap confidence at 0.65, and say so in `reasoning`.
* A foreign-listed parent is still public: give its primary-listing ticker and name
  the exchange in `reasoning`.

TICKERS ALREADY KNOWN TO THE SYSTEM (prefer these when they apply; you are not
limited to them -- any correctly listed company may be returned):
{_universe_block(u)}

Names already covered by that alias table never reach you: they are resolved
deterministically for free. Everything you are asked about failed that lookup, so
expect a high proportion of genuinely private companies.

LEARNED RULES ({skills_version()})
{skills or "(none yet)"}

Return one object per input name, in the input order, and nothing else."""


def build_prompt(names: list[str]) -> str:
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    return (f"Resolve these {len(names)} contractor names, one entry each, in this "
            f"exact order:\n\n{listing}\n")


def _max_tokens(n: int) -> int:
    """Deterministic budget: ~340 output tokens per entity plus overhead."""
    return min(16000, 900 + 340 * n)


def _coerce(item: dict, raw: str, model: str, cache_key: str) -> dict:
    """Validate one model-produced entity into a schema-shaped record.

    `raw` is the representative printed spelling; the caller fans the result out to
    every raw variant with _for_raw().
    """
    notes = []
    ticker = item.get("ticker")
    ticker = (ticker.strip().upper() or None) if isinstance(ticker, str) else None
    rel = item.get("relationship")
    if rel not in RELATIONSHIPS:
        notes.append(f"relationship {rel!r} not in enum, coerced to unknown")
        rel = "unknown"
    is_public = item.get("is_public")
    is_public = bool(is_public) if isinstance(is_public, bool) else bool(ticker)
    try:
        conf = float(item.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.3
        notes.append("no usable confidence returned")
    conf = min(1.0, max(0.0, conf))

    # Internal contradictions are a confidence problem, not a crash. Cap and record.
    if ticker and rel == "private":
        notes.append("ticker given with relationship=private")
        conf = min(conf, 0.5)
    if is_public and not ticker:
        notes.append("is_public with no ticker")
        conf = min(conf, 0.5)
    if ticker and not is_public:
        is_public = True

    reasoning = (item.get("reasoning") or "").strip() or "no reasoning returned"
    if notes:
        reasoning = f"{reasoning} [validator: {'; '.join(notes)}]"

    name = (item.get("normalized_name") or "").strip() or display_name(raw)
    return _record(
        contractor_raw=raw,
        normalized_name=name,
        ticker=ticker,
        parent_company=(item.get("parent_company") or None),
        relationship=rel,
        is_public=is_public,
        confidence=round(conf, 3),
        reasoning=reasoning,
        resolver_model=model,
        llm_cache_key=cache_key,
    )


def _placeholder(raw: str, model: str, cache_key: str | None, why: str) -> dict:
    """An honest non-answer. confidence 0.0 -> escalation, then review. Never persisted."""
    return _record(
        contractor_raw=raw, normalized_name=display_name(raw), ticker=None,
        parent_company=None, relationship="unknown", is_public=False,
        confidence=0.0, reasoning=why, resolver_model=model, llm_cache_key=cache_key)


def _align(items: list, names: list[str]) -> dict[str, dict]:
    """Map returned entities back onto input names.

    Index alignment is the contract (the schema has no contractor_raw -- it is a
    deterministic field and must not appear in a prompt schema). When the model
    returns the wrong count, fall back to matching on normalized_name so a single
    dropped entry does not corrupt every name after it.
    """
    items = [i for i in items if isinstance(i, dict)]
    if len(items) == len(names):
        return dict(zip(names, items))
    by_key = {normalize(n): n for n in names}
    out: dict[str, dict] = {}
    leftovers = []
    for item in items:
        key = normalize(item.get("normalized_name") or "")
        target = by_key.get(key)
        if target and target not in out:
            out[target] = item
        else:
            leftovers.append(item)
    for name in names:                             # positional fill for the remainder
        if name not in out and leftovers:
            out[name] = leftovers.pop(0)
    return out


# --------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------

@dataclass
class ResolveResult:
    records: dict[str, dict] = dc_field(default_factory=dict)   # RAW name -> entities row
    pending: list[str] = dc_field(default_factory=list)         # need a live run
    review: list[dict] = dc_field(default_factory=list)         # review_queue payloads
    stats: dict = dc_field(default_factory=dict)

    def escalations(self) -> list[str]:
        return [n for n, r in self.records.items() if needs_escalation(r)]


def _review_item(rec: dict, reason: str) -> dict:
    """A review_queue row, shaped from schemas.REVIEW_QUEUE_FIELDS."""
    basis = f"resolve_entity|{rec['contractor_raw']}|{reason}"
    item = {k: None for k in REVIEW_KEYS}
    item.update({
        "review_id": hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16],
        "flagged_at": _now(),
        "agent": "resolve_entity",
        "item_key": rec["contractor_raw"],
        "reason": reason,
        "confidence": rec.get("confidence"),
        "payload": rec,
        "resolved": False,
    })
    return item


def _run_batches(reps: list[str], llm, model: str, system: str, batch_size: int,
                 on_batch=None) -> tuple[dict[str, dict], list[str]]:
    """Resolve representative names on `model`. Returns (rep -> record, pending reps).

    One call per batch. Never one call per raw variant -- that is the caller's job to
    keep true by only ever passing representatives.

    `on_batch(chunk, out)` fires after EVERY batch -- success, replay miss and failure
    alike -- so a caller can report progress and persist partial results while a long
    queue is still running. It fires in a `finally`, because a batch that failed still
    advanced the queue and the caller still needs to know. A callback that raises is
    reported and swallowed: losing a batch the run already paid for because a progress
    printer threw would be a strictly worse bug than no progress at all.
    """
    out: dict[str, dict] = {}
    pending: list[str] = []

    def _one(chunk: list[str]) -> None:
        prompt = build_prompt(chunk)
        schema = batch_schema()
        max_tokens = _max_tokens(len(chunk))
        cache_key = CachedLLM.key(model, system, prompt, schema, max_tokens)
        try:
            got = llm.json_call(model=model, system=system, prompt=prompt, schema=schema,
                                max_tokens=max_tokens,
                                label=f"resolve_entity x{len(chunk)} @{model}")
        except NotCachedError as exc:
            # Replay mode with nothing cached: report it, do not fabricate, do not
            # persist. `make demo` must survive this without an API key.
            sys.stderr.write(f"! tier 2 unresolved ({len(chunk)} names): {exc}\n")
            pending.extend(chunk)
            for rep in chunk:
                out[rep] = _placeholder(
                    rep, model, cache_key,
                    f"unresolved: no cached call ({cache_key[:12]}); needs a live run")
            return
        except Exception as exc:                   # a bad batch must not sink the rest
            sys.stderr.write(f"! tier 2 call failed ({type(exc).__name__}: {exc})\n")
            pending.extend(chunk)
            for rep in chunk:
                out[rep] = _placeholder(rep, model, cache_key,
                                        f"call failed: {type(exc).__name__}: {exc}")
            return

        aligned = _align((got or {}).get("entities") or [], chunk)
        for rep in chunk:
            item = aligned.get(rep)
            if item is None:
                pending.append(rep)
                out[rep] = _placeholder(rep, model, cache_key,
                                        "model returned no entry for this name")
                continue
            out[rep] = _coerce(item, rep, model, cache_key)

    for chunk in batches(reps, batch_size):
        try:
            _one(chunk)
        finally:
            if on_batch is not None:
                try:
                    on_batch(chunk, out)
                except Exception as exc:
                    sys.stderr.write(f"! on_batch failed: {type(exc).__name__}: {exc}\n")
    return out, pending


def resolve(names, llm, *, model: str | None = None,
            escalate_model: str | None = None, escalation: bool = True,
            map_path: pathlib.Path = ENTITY_MAP_PATH,
            universe_path: pathlib.Path = UNIVERSE_PATH,
            batch_size: int = BATCH_SIZE, persist: bool = True,
            refresh: bool = False, on_progress=None) -> ResolveResult:
    """Full tiered resolution with escalation, review routing and persistence.

    `records` is keyed by the RAW printed name and every row's contractor_raw is that
    same raw string -- that is the join key src/store.py uses. Model work is done once
    per NORMALIZED company and fanned out.

    `on_progress(done, total, rows)` is optional and reports the tier-2 pass batch by
    batch, with `rows` ALREADY FANNED OUT to raw variants -- i.e. real `entities` rows
    the caller can persist immediately. Only the main pass reports; escalation is a
    short tail whose improved rows land in the final result anyway.
    """
    model = model or BULK_MODEL
    escalate_model = escalate_model or JUDGE_MODEL
    u = load_universe(universe_path)
    emap = load_entity_map(map_path)

    requested = list(dict.fromkeys(n for n in names if n and n.strip()))
    groups = group_variants(requested)
    variants_of = {representative(v): v for v in groups.values()}
    cached, tier1, queue = plan(requested, emap, u, refresh=refresh)

    records: dict[str, dict] = {}
    records.update(cached)
    records.update(tier1)

    def _fan_out(rep: str, rec: dict) -> None:
        """One resolution -> one row per printed variant of that company.

        Each row keeps its OWN raw contractor_raw. This is the whole point.
        """
        for raw in variants_of.get(rep, [rep]):
            records[raw] = _for_raw(rec, raw)

    # Progress is reported in the caller's currency -- entities rows keyed on the RAW
    # name -- not in the resolver's internal one (representatives). The fan-out has to
    # happen here because `variants_of` lives here.
    _done = 0

    def _report(chunk: list[str], so_far: dict[str, dict]) -> None:
        nonlocal _done
        _done += len(chunk)
        if on_progress is None:
            return
        rows = [_for_raw(rec, raw)
                for rep in chunk
                if (rec := so_far.get(rep)) is not None
                for raw in variants_of.get(rep, [rep])]
        on_progress(min(_done, len(queue)), len(queue), rows)

    system = build_system(u)
    resolved, pending = _run_batches(queue, llm, model, system, batch_size,
                                     on_batch=_report)

    # -- escalation: confidence < 0.7 gets one retry on the judge model --
    escalated: dict[str, dict] = {}
    low = [rep for rep, rec in resolved.items()
           if needs_escalation(rec) and rep not in pending]
    if escalation and low and escalate_model != model:
        escalated, esc_pending = _run_batches(low, llm, escalate_model, system, batch_size)
        for rep, rec in escalated.items():
            if rep in esc_pending:
                continue                           # keep the haiku answer, flag below
            resolved[rep] = rec

    for rep, rec in resolved.items():
        _fan_out(rep, rec)

    # -- review routing: still low after escalation, or never resolved at all --
    review: list[dict] = []
    for rep in queue:
        rec = resolved.get(rep)
        if rec is None:
            continue
        if rep in pending:
            review.append(_review_item(rec, "no cached model call; needs a live run"))
        elif needs_escalation(rec):
            reason = ("still below %.2f after escalation to %s" % (LOW_CONFIDENCE, escalate_model)
                      if rep in escalated else
                      "below %.2f and escalation was disabled" % LOW_CONFIDENCE)
            review.append(_review_item(rec, reason))

    n_exact = sum(1 for r in tier1.values() if "exact alias" in (r["reasoning"] or ""))
    stats = {
        "requested": len(requested),
        "distinct_raw": len(requested),
        "distinct_normalized": len(groups),
        "duplicate_variants": len(requested) - len(groups),
        "tier0_entity_map": len(cached),
        "tier1_alias": len(tier1),
        "tier1_exact": n_exact,
        "tier1_prefix": len(tier1) - n_exact,
        "tier2_queued": len(queue),
        "tier2_batches": len(batches(queue, batch_size)),
        "tier2_resolved": len([r for r in queue if r not in pending]),
        "tier2_pending": len(pending),
        "escalated": len(escalated),
        "review": len(review),
        "model": model,
        "escalate_model": escalate_model,
        "batch_size": batch_size,
        "skills_version": skills_version(),
    }
    usage = getattr(llm, "usage", None)
    if usage is not None:
        stats["llm_calls"] = usage.calls
        stats["llm_cache_hits"] = usage.cache_hits
        stats["cost_usd"] = usage.cost_usd()

    if persist:
        # Persist ONE entry per normalized company, not one per printed variant, and
        # never a placeholder or a review case -- either in the map would suppress the
        # real resolution forever.
        blocked = {normalize(p) for p in pending}
        blocked |= {normalize(i["item_key"]) for i in review}
        fresh: dict[str, dict] = {}
        for raw, rec in records.items():
            norm = normalize(raw)
            if not norm or norm in blocked:
                continue
            if norm in emap and not refresh:       # refresh=True is an escalation rerun
                continue
            entry = dict(rec)
            entry[_MAP_EXTRA] = sorted(groups.get(norm, [raw]))
            entry["contractor_raw"] = representative(groups.get(norm, [raw]))
            fresh[norm] = entry
        if fresh:
            emap.update(fresh)
            save_entity_map(emap, map_path)
        stats["persisted"] = len(fresh)

    return ResolveResult(records=records, pending=pending, review=review, stats=stats)


def resolve_names(names: list[str], llm, **kw) -> dict[str, dict]:
    """The manager's entry point: raw contractor name -> entities row."""
    return resolve(names, llm, **kw).records


def escalate(names: list[str], llm, **kw) -> dict[str, dict]:
    """Re-resolve names directly on the judge model, ignoring the stored map."""
    kw.setdefault("model", JUDGE_MODEL)
    kw.setdefault("escalation", False)
    kw.setdefault("refresh", True)
    return resolve(names, llm, **kw).records


# --------------------------------------------------------------------------------
# CleaningAgent protocol shape (CLAUDE.md) -- lets the manager register this file
# --------------------------------------------------------------------------------

class ResolveEntityAgent:
    """name / detects(conn) / run(items, llm) / skills_path() -- nothing else needed."""

    name = "resolve_entity"

    #: This agent batches 25 names per call, so the manager must NOT chunk it -- that
    #: turns one call into 25. It reports progress from inside `run()` instead, which
    #: is what `supports_progress` advertises. See resolve()'s `on_progress`.
    supports_progress = True

    def skills_path(self) -> pathlib.Path:
        return SKILLS_PATH

    def detects(self, conn) -> list[str]:
        """Contractor names in awards with no entities row, plus low-confidence ones.

        The join is on the RAW string, exactly as src/store.py stores it.
        """
        try:
            rows = conn.execute(
                "SELECT DISTINCT a.contractor_raw "
                "FROM awards a LEFT JOIN entities e "
                "  ON e.contractor_raw = a.contractor_raw "
                "WHERE a.contractor_raw IS NOT NULL "
                "  AND (e.contractor_raw IS NULL OR e.confidence < ?) "
                "ORDER BY 1", [LOW_CONFIDENCE]
            ).fetchall()
        except Exception as exc:                   # tables not created yet
            sys.stderr.write(f"! resolve_entity.detects: {type(exc).__name__}: {exc}\n")
            return []
        return [r[0] for r in rows]

    def run(self, items, llm, **kw) -> list[dict]:
        return list(resolve_names(list(items), llm, **kw).values())


# --------------------------------------------------------------------------------
# corpus scan -- a coverage statistic, NOT the real extractor
# --------------------------------------------------------------------------------

_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_AWARD_RE = re.compile(r"\b(?:is|was|are|were|has been|have been)\s+(?:being\s+)?award",
                       re.IGNORECASE)
_NAME_RE = re.compile(r"^(?P<name>[A-Z0-9][^,;]{2,90}?)\s*,\s+[A-Z]")


def corpus_root() -> pathlib.Path:
    """Where raw/articles/*.html actually lives.

    raw/*.html is gitignored, so a linked worktree has the committed .prov.json
    sidecars but not the bodies. Fall back to the main working tree (the same trick
    chatroom.py uses) rather than reporting a corpus of zero.
    """
    here = config.RAW / "articles"
    if any(here.glob("*.html")):
        return here
    import subprocess
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=10, cwd=config.ROOT)
        if out.returncode == 0:
            git_dir = pathlib.Path(out.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = (config.ROOT / git_dir).resolve()
            main = git_dir.parent / "raw" / "articles"
            if any(main.glob("*.html")):
                return main
    except Exception:
        pass
    return here


def scan_corpus_counts(root: pathlib.Path | None = None) -> dict[str, int]:
    """{contractor name: times it appears} from raw/articles/*.html by rough regex.

    Deliberately crude: the announcement's first-comma convention only. Agent A owns
    real extraction; this exists solely to size the tier-1 / tier-2 split. It will
    miss the 2nd..Nth company of a multi-award pool.
    """
    root = root or corpus_root()
    counts: dict[str, int] = {}
    canon: dict[str, str] = {}
    for path in sorted(pathlib.Path(root).glob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for block in _P_RE.findall(text):
            para = _html.unescape(_TAG_RE.sub("", block)).replace("\xa0", " ")
            para = re.sub(r"\s+", " ", para).strip()
            if not para or not _AWARD_RE.search(para[:400]):
                continue
            m = _NAME_RE.match(para)
            if not m:
                continue
            name = m.group("name").strip()
            if len(name.split()) > 12 or not re.search(r"[A-Za-z]{3}", name):
                continue
            key = name.casefold()
            first = canon.setdefault(key, name)
            counts[first] = counts.get(first, 0) + 1
    return counts


def scan_corpus(root: pathlib.Path | None = None) -> list[str]:
    """Distinct contractor names in the corpus, in first-seen order."""
    return list(scan_corpus_counts(root))


# --------------------------------------------------------------------------------
# offline test doubles -- no API key, no network, no spend
# --------------------------------------------------------------------------------

class _Tripwire:
    """An LLM stand-in that fails loudly if anything tries to spend money."""

    def __init__(self):
        self.usage = Usage()
        self.live = False

    def json_call(self, **kw):
        raise AssertionError(
            f"TIER-1 VIOLATION: a model call was attempted for {kw.get('label')!r}")


class _StubLLM:
    """A scripted LLM. Counts calls, echoes one entity per numbered prompt line.

    `answers` maps a model id to a callable(name) -> the llm=True half of an entity.
    Anything not scripted gets a confident "private", which is the honest default.
    """

    _LINE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$", re.MULTILINE)

    def __init__(self, answers=None):
        self.usage = Usage()
        self.live = False
        self.calls: list[dict] = []
        self.answers = answers or {}

    def _default(self, name: str) -> dict:
        return {"normalized_name": display_name(name), "ticker": None,
                "parent_company": None, "relationship": "private", "is_public": False,
                "confidence": 0.9, "reasoning": "stub: unrecognised, assumed private"}

    def json_call(self, *, model, system, prompt, schema, max_tokens=8000, label=""):
        names = self._LINE.findall(prompt)
        self.calls.append({"model": model, "names": names, "label": label,
                           "schema": schema, "max_tokens": max_tokens})
        fn = self.answers.get(model)
        return {"entities": [(fn(n) if fn else None) or self._default(n) for n in names]}


# --------------------------------------------------------------------------------
# selftest -- everything below is free: no API key, no network, no spend
# --------------------------------------------------------------------------------

def _ok(cond: bool, label: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def _head(n: str) -> None:
    print()
    print("=" * 78)
    print(n)
    print("=" * 78)


def _check_repo_invariants() -> int:
    """Acceptance items 1-3: the contract is intact and nothing imports anthropic."""
    import subprocess
    fails = 0
    root = config.ROOT

    r = subprocess.run([sys.executable, str(root / "src" / "schemas.py")],
                       capture_output=True, text=True, cwd=str(root))
    fails += not _ok(r.returncode == 0 and "schema version" in r.stdout,
                     f"src/schemas.py still prints the contract "
                     f"({r.stdout.splitlines()[0] if r.stdout else r.stderr[:60]!r})")

    # The contract may be unfrozen by the project owner (see its CHANGES log), so the
    # invariant worth guarding is not "never changed" -- it is that no local change to
    # the contract has altered a PROMPT, because that silently invalidates the
    # committed cache and forces re-extraction at real cost. Compare this agent's
    # prompt projection against the one on `main`.
    d = subprocess.run(["git", "show", "main:src/schemas.py"],
                       capture_output=True, text=True, cwd=str(root))
    if d.returncode != 0:
        fails += not _ok(True, "no `main` to compare the contract against (skipped)")
    else:
        import types
        ref = types.ModuleType("_schemas_main")
        # dataclasses resolves a class's module through sys.modules, so a synthetic
        # module must be registered there before `@dataclass` in the contract runs.
        sys.modules["_schemas_main"] = ref
        try:
            exec(compile(d.stdout, "main:src/schemas.py", "exec"), ref.__dict__)
            same = all(
                ref.object_schema(getattr(ref, n)) == schemas.object_schema(getattr(schemas, n))
                for n in ("AWARD_FIELDS", "ENTITY_FIELDS", "MATERIALITY_FIELDS"))
            fails += not _ok(same, "no contract edit changed a prompt schema "
                                   "(the committed cache still replays)")
        except Exception as exc:
            fails += not _ok(False, f"could not compare contracts: "
                                    f"{type(exc).__name__}: {exc}")
        finally:
            sys.modules.pop("_schemas_main", None)

    # The SDK name is assembled from parts so that a reviewer grepping src/ for the
    # forbidden import still matches src/llm.py and nothing else -- not even this file.
    sdk = "anthropic"
    pat = re.compile(r"^\s*(?:import|from)\s+" + sdk + r"\b", re.MULTILINE)
    offenders = [p.relative_to(root).as_posix()
                 for p in sorted((root / "src").rglob("*.py"))
                 if pat.search(p.read_text(encoding="utf-8"))]
    fails += not _ok(offenders == ["src/llm.py"],
                     f"the {sdk} SDK is imported only in src/llm.py (found: {offenders})")
    return fails


def _check_join() -> int:
    """The live constraint, end to end, against a real DuckDB.

    src/store.py is Agent C's file and does not exist here yet, so this builds the
    same two tables straight from the frozen contract and exercises the join it
    documents: entities.contractor_raw = awards.contractor_raw.
    """
    try:
        import duckdb
    except ImportError:
        print("  (duckdb not installed; skipping the live join check)")
        return 0

    ent_cols = [f.name for f in _ENTITY_FIELDS]
    awd_cols = [f.name for f in schemas.TABLES["awards"][0]]
    starred = "Action Manufacturing Co.,*"
    plain = "Action Manufacturing Co."
    unseen = "Zone 5 Technologies LLC"

    conn = duckdb.connect(":memory:")
    conn.execute(schemas.ddl("awards"))
    conn.execute(schemas.ddl("entities"))
    for i, raw in enumerate((starred, plain, unseen)):
        row = {c: None for c in awd_cols}
        row.update({"award_uid": schemas.award_uid("A1", f"C-{i}", raw),
                    "announcement_id": "A1", "contractor_raw": raw,
                    "action_type": "new_award", "extraction_confidence": 0.9})
        conn.execute(f"INSERT INTO awards ({','.join(awd_cols)}) VALUES "
                     f"({','.join('?' * len(awd_cols))})", [row[c] for c in awd_cols])

    # Resolve the two spellings the way the manager would, then store them raw.
    # The map path MUST be a scratch file: this section asserts that normalization
    # collapses the two spellings into ONE model call, and the real
    # data/entity_map.json now contains this very company, so reading it would make
    # the call count 0 and the check would pass or fail on ambient state instead of on
    # the invariant. Every other section already isolates this way.
    import tempfile
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="resolve_join_")) / "join.json"
    stub = _StubLLM()
    recs = resolve_names([starred, plain], stub, persist=False, map_path=scratch)
    for rec in recs.values():
        conn.execute(f"INSERT INTO entities ({','.join(ent_cols)}) VALUES "
                     f"({','.join('?' * len(ent_cols))})", [rec[c] for c in ent_cols])

    joined = conn.execute(
        "SELECT a.contractor_raw, e.ticker, e.normalized_name FROM awards a "
        "JOIN entities e ON e.contractor_raw = a.contractor_raw ORDER BY 1").fetchall()
    for r in joined:
        print(f"    joined  {r[0]!r}")

    fails = 0
    fails += not _ok(len(stub.calls) == 1,
                     f"one model call covered both spellings ({len(stub.calls)})")
    fails += not _ok(sorted(r[0] for r in joined) == sorted([plain, starred]),
                     "both raw spellings JOIN to their entities row, asterisk included")
    fails += not _ok(len({r[2] for r in joined}) == 1,
                     "and both resolve to the same normalized_name")

    # Negative control: had we keyed entities on the normalized name, the join dies.
    conn.execute("DELETE FROM entities")
    for rec in recs.values():
        bad = dict(rec)
        bad["contractor_raw"] = normalize(bad["contractor_raw"])   # the bug
        try:
            conn.execute(f"INSERT INTO entities ({','.join(ent_cols)}) VALUES "
                         f"({','.join('?' * len(ent_cols))})", [bad[c] for c in ent_cols])
        except Exception:
            pass                                   # the two variants collide on the PK
    n = conn.execute("SELECT count(*) FROM awards a JOIN entities e "
                     "ON e.contractor_raw = a.contractor_raw").fetchone()[0]
    fails += not _ok(n == 0,
                     f"negative control: normalizing contractor_raw yields {n} joined "
                     f"rows -- silent data loss, which is why it stays raw")

    agent = ResolveEntityAgent()
    detected = agent.detects(conn)
    fails += not _ok(unseen in detected,
                     f"detects() finds the award with no entities row: {detected}")
    conn.close()
    return fails


def selftest(tmp_root: pathlib.Path | None = None) -> int:
    global tier1_match                  # check 6 disables it on purpose; see below
    import tempfile
    tmp = pathlib.Path(tmp_root or tempfile.mkdtemp(prefix="resolve_selftest_"))
    u = load_universe()
    fails = 0

    _head("0. repo invariants: frozen contract, no stray anthropic import")
    fails += _check_repo_invariants()

    # ------------------------------------------------------------------
    _head("1. THE LIVE CONSTRAINT: raw join key, normalized cache key")
    # Two pairs that differ ONLY by the small-business asterisk and by Inc. vs Inc.
    # Both pairs are private (tier 1 must not match them), so both reach the model --
    # and must cost exactly ONE call between them.
    pair_star = ["Cobham Advanced Electronic Solutions Inc.",
                 "Cobham Advanced Electronic Solutions Inc.*"]
    pair_inc = ["Vertex Aerospace Services LLC", "Vertex Aerospace Services LLC*"]
    stub = _StubLLM()
    res = resolve(pair_star + pair_inc, stub, map_path=tmp / "constraint.json",
                  persist=False)

    print(f"    model calls made: {len(stub.calls)}")
    for c in stub.calls:
        print(f"      {c['model']}  names={c['names']}")
    for raw in pair_star + pair_inc:
        r = res.records[raw]
        print(f"    row  contractor_raw={r['contractor_raw']!r:52s} "
              f"norm={normalize(raw)!r}")

    fails += not _ok(len(stub.calls) == 1,
                     f"EXACTLY ONE model call for 4 raw spellings of 2 companies "
                     f"(made {len(stub.calls)})")
    fails += not _ok(sorted(stub.calls[0]["names"]) == sorted(
        [representative(pair_star), representative(pair_inc)]),
        f"the single call carried 2 representative names: {stub.calls[0]['names']}")
    fails += not _ok(len(res.records) == 4, f"4 rows emitted (got {len(res.records)})")
    fails += not _ok([res.records[r]["contractor_raw"] for r in pair_star] == pair_star,
                     "asterisk pair: each row keeps its OWN byte-for-byte raw string")
    fails += not _ok([res.records[r]["contractor_raw"] for r in pair_inc] == pair_inc,
                     "Inc./Inc pair: each row keeps its OWN byte-for-byte raw string")
    fails += not _ok(
        res.records[pair_star[0]]["contractor_raw"] != res.records[pair_star[1]]["contractor_raw"],
        "the two contractor_raw values are DISTINCT (two entities rows, one company)")
    for pair, tag in ((pair_star, "asterisk"), (pair_inc, "Inc.")):
        a, b = (res.records[p] for p in pair)
        same = all(a[k] == b[k] for k in
                   ("ticker", "parent_company", "normalized_name", "relationship",
                    "is_public", "confidence", "reasoning", "llm_cache_key"))
        fails += not _ok(same, f"{tag} pair carries the IDENTICAL resolution "
                               f"(ticker/parent/normalized_name/cache key)")
    fails += not _ok(all("*" not in normalize(r) for r in pair_star),
                     "normalize() strips the asterisk; contractor_raw does not")

    # ------------------------------------------------------------------
    _head("2. 'Rockwell Collins Inc.' -> RTX via tier 1, tripwire armed")
    trip = _Tripwire()
    recs = resolve_names(["Rockwell Collins Inc."], trip, map_path=tmp / "m1.json")
    rec = recs["Rockwell Collins Inc."]
    print(json.dumps(rec, indent=2))
    for cond, label in [
        (rec["ticker"] == "RTX", "ticker == RTX"),
        (rec["parent_company"] == "RTX Corporation", "parent_company == RTX Corporation"),
        (rec["relationship"] == "subsidiary", "relationship == subsidiary"),
        (rec["is_public"] is True, "is_public"),
        (rec["contractor_raw"] == "Rockwell Collins Inc.", "contractor_raw kept raw"),
        (rec["resolver_model"] == DETERMINISTIC, "resolver_model == 'deterministic'"),
        (rec["llm_cache_key"] is None, "llm_cache_key is null (no model call)"),
        (rec["confidence"] >= 0.99, "confidence >= 0.99"),
        (trip.usage.calls == 0, f"ZERO model calls (tripwire never fired)"),
        (normalize("Rockwell Collins Inc.") == "rockwell collins",
         "normalize() -> 'rockwell collins'"),
        (list(rec) == ENTITY_KEYS, "record keys == schemas.ENTITY_FIELDS, in order"),
    ]:
        fails += not _ok(cond, label)

    # ------------------------------------------------------------------
    _head("3. every universe alias resolves at tier 1, with ZERO model calls")
    variants = ["{}", "{} Inc.", "{} LLC", "{}, Inc.", "The {} Co.", "{} Corporation", "{}*"]
    trip2 = _Tripwire()
    all_alias_names: list[str] = []
    for ticker, row in u.by_ticker.items():
        hits = wrong = 0
        for text in [row["company"]] + row["aliases"]:
            for pattern in variants:
                name = pattern.format(text)
                all_alias_names.append(name)
                r = tier1_match(name, u)
                if r is None:
                    continue
                if r["ticker"] == ticker:
                    hits += 1
                else:
                    wrong += 1
                    print(f"    ! {name!r} -> {r['ticker']}, expected {ticker}")
        tried = (len(row["aliases"]) + 1) * len(variants)
        fails += not _ok(hits > 0 and wrong == 0,
                         f"{ticker:5s} {hits:3d}/{tried} alias variants hit tier 1, "
                         f"{wrong} mis-routed")
    # and the whole set through the real entry point, against a tripwire
    every = resolve(all_alias_names, trip2, map_path=tmp / "aliases.json", persist=False)
    fails += not _ok(trip2.usage.calls == 0 and every.stats["tier2_queued"] == 0,
                     f"all {len(all_alias_names)} alias spellings resolved with 0 model "
                     f"calls (tier2_queued={every.stats['tier2_queued']})")

    # ------------------------------------------------------------------
    _head("4. every emitted row validates against schemas.ENTITY_FIELDS")
    sample = dict(every.records)
    sample.update(res.records)
    bad = {k: validate_record(v) for k, v in sample.items()}
    bad = {k: v for k, v in bad.items() if v}
    for k, v in list(bad.items())[:5]:
        print(f"    ! {k!r}: {v}")
    fails += not _ok(not bad, f"{len(sample)} rows validate (deterministic fields "
                              f"populated, relationship in enum, confidence float 0..1)")
    fails += not _ok(all(isinstance(v["confidence"], float) and 0.0 <= v["confidence"] <= 1.0
                         for v in sample.values()), "confidence is a float in 0..1 everywhere")

    # ------------------------------------------------------------------
    _head("5. the prompt schema excludes every llm=False field")
    item_props = set(batch_schema()["properties"]["entities"]["items"]["properties"])
    leaked = sorted(item_props & set(DETERMINISTIC_KEYS))
    print(f"    prompt fields   : {sorted(item_props)}")
    print(f"    deterministic   : {DETERMINISTIC_KEYS}")
    fails += not _ok(not leaked, f"no llm=False field in the prompt schema (leaked: {leaked})")
    fails += not _ok(item_props == {f.name for f in _ENTITY_FIELDS if f.llm},
                     "prompt fields == schemas.llm_fields(ENTITY_FIELDS), built by "
                     "schemas.object_schema() and not by hand")
    fails += not _ok(BULK_MODEL == "claude-haiku-4-5" and JUDGE_MODEL == "claude-opus-5",
                     f"model ids from config, no date suffix: {BULK_MODEL} / {JUDGE_MODEL}")

    # ------------------------------------------------------------------
    _head("6. entity_map round-trips and short-circuits recomputation")
    mp = tmp / "roundtrip.json"
    names6 = ["Rockwell Collins Inc.", "General Dynamics Land Systems Inc.",
              "The Boeing Co.", "The Boeing Co.*"]
    first = resolve(names6, _Tripwire(), map_path=mp)
    reloaded = load_entity_map(mp)
    fails += not _ok(mp.exists(), f"written: {mp}")
    fails += not _ok(set(reloaded) == {normalize(n) for n in names6},
                     f"map keyed by NORMALIZED name: {sorted(reloaded)}")
    fails += not _ok(len(reloaded) == 3 and len(names6) == 4,
                     "4 raw names stored as 3 cache entries (the asterisk pair merged)")
    fails += not _ok(reloaded[normalize("The Boeing Co.")][_MAP_EXTRA] ==
                     ["The Boeing Co.", "The Boeing Co.*"],
                     "both printed spellings recorded under raw_variants")
    fails += not _ok(all(first.records[n]["contractor_raw"] == n for n in names6),
                     "every emitted row still carries its own raw contractor_raw")

    real_tier1 = tier1_match

    def _exploding(*a, **k):
        raise AssertionError("TIER-0 VIOLATION: tier 1 ran for an already-known name")

    tier1_match = _exploding
    try:
        again = resolve(names6, _Tripwire(), map_path=mp, persist=False)
        served = all(again.records[n] == _for_raw(reloaded[normalize(n)], n) for n in names6)
        fails += not _ok(served, "second run served from entity_map with tier 1 disabled")
        fails += not _ok(again.stats["tier0_entity_map"] == len(names6),
                         f"stats: tier0={again.stats['tier0_entity_map']}, "
                         f"tier1={again.stats['tier1_alias']}, "
                         f"tier2={again.stats['tier2_queued']}")
    except AssertionError as exc:
        fails += not _ok(False, f"cache short-circuit failed: {exc}")
    finally:
        tier1_match = real_tier1

    # ------------------------------------------------------------------
    _head("7. escalation routing: 0.5 -> Opus -> accepted, or -> review")
    unknown = ["Zeta Dynamics Holdings LLC"]

    def _low(name):
        return {"normalized_name": display_name(name), "ticker": None,
                "parent_company": None, "relationship": "unknown", "is_public": False,
                "confidence": 0.5, "reasoning": "stub: genuinely unsure"}

    def _high(name):
        return {"normalized_name": display_name(name), "ticker": "ZZZ",
                "parent_company": "Zeta Dynamics Corporation", "relationship": "subsidiary",
                "is_public": True, "confidence": 0.88, "reasoning": "stub: Opus knew it"}

    rescued = _StubLLM({BULK_MODEL: _low, JUDGE_MODEL: _high})
    r7a = resolve(unknown, rescued, map_path=tmp / "esc_a.json", persist=False)
    rec7a = r7a.records[unknown[0]]
    print(f"    calls: {[c['model'] for c in rescued.calls]}")
    print(f"    result: model={rec7a['resolver_model']} conf={rec7a['confidence']} "
          f"ticker={rec7a['ticker']}")
    fails += not _ok([c["model"] for c in rescued.calls] == [BULK_MODEL, JUDGE_MODEL],
                     f"0.5 on {BULK_MODEL} escalated to {JUDGE_MODEL}")
    fails += not _ok(rec7a["resolver_model"] == JUDGE_MODEL and rec7a["confidence"] == 0.88,
                     "the Opus answer replaced the low-confidence one")
    fails += not _ok(r7a.review == [], "nothing sent to review once confidence recovered")

    stuck = _StubLLM({BULK_MODEL: _low, JUDGE_MODEL: _low})
    r7b = resolve(unknown, stuck, map_path=tmp / "esc_b.json", persist=True)
    rec7b = r7b.records[unknown[0]]
    print(f"    calls: {[c['model'] for c in stuck.calls]}")
    print(f"    review: {[(i['item_key'], i['reason'], i['confidence']) for i in r7b.review]}")
    fails += not _ok([c["model"] for c in stuck.calls] == [BULK_MODEL, JUDGE_MODEL],
                     "still-low answer was escalated exactly once, not looped")
    fails += not _ok(len(r7b.review) == 1 and r7b.review[0]["item_key"] == unknown[0],
                     "still-low answer goes to the review queue")
    fails += not _ok(list(r7b.review[0]) == REVIEW_KEYS,
                     "review item has exactly schemas.REVIEW_QUEUE_FIELDS keys")
    fails += not _ok(rec7b["confidence"] == 0.5 and rec7b["ticker"] is None,
                     "the uncertain answer is surfaced as-is, not silently accepted")
    fails += not _ok(not (tmp / "esc_b.json").exists() or
                     normalize(unknown[0]) not in load_entity_map(tmp / "esc_b.json"),
                     "a review case is NOT persisted to entity_map (it would freeze the guess)")

    # ------------------------------------------------------------------
    _head("8. cache-miss path raises NotCachedError, cleanly, and spends nothing")
    private = ["AAECON General Contracting LLC", "Bering Straits Professional Services LLC"]
    offline = CachedLLM(live=False, cache_dir=tmp / "cache")
    raised = False
    try:
        offline.json_call(model=BULK_MODEL, system="s", prompt="p",
                          schema=batch_schema(), max_tokens=100, label="probe")
    except NotCachedError as exc:
        raised = True
        print(f"    NotCachedError: {exc}")
    fails += not _ok(raised, "CachedLLM(live=False) raises NotCachedError on a miss")
    fails += not _ok(offline.live is False, "live is False -- no API key was needed")

    res8 = resolve(private, offline, map_path=tmp / "m8.json", persist=True)
    fails += not _ok(sorted(res8.pending) == sorted(private),
                     "resolve() degrades to 'pending', it does not crash or fabricate")
    fails += not _ok(offline.usage.calls == 0, "offline replay spent nothing")
    fails += not _ok(len(res8.review) == 2, "both pending names routed to review")
    fails += not _ok(not (tmp / "m8.json").exists() or
                     all(normalize(p) not in load_entity_map(tmp / "m8.json") for p in private),
                     "pending names are NOT persisted (they would poison the map)")
    fails += not _ok(all(not validate_record(r) for r in res8.records.values()),
                     "even the placeholder rows satisfy the contract")

    # ------------------------------------------------------------------
    _head("9. tier-1 refuses what it should not answer")
    fails += not _ok(tier1_match("Alpha-Lockheed Martin JV", u) is None,
                     "a joint venture is not force-matched to a prime (skills R-004)")
    fails += not _ok(tier1_match("AAECON General Contracting LLC", u) is None,
                     "an unknown private firm falls through to tier 2")
    fails += not _ok("llm" not in plan.__code__.co_varnames,
                     "plan() has no llm parameter: queueing cannot spend by construction")
    cached9, tier1_9, queue9 = plan(private, entity_map={}, universe=u)
    fails += not _ok(sorted(queue9) == sorted(private) and not tier1_9,
                     "both private names queued for tier 2, neither force-matched")

    # ------------------------------------------------------------------
    _head("10. the join actually holds, in a real DuckDB built from schemas.ddl()")
    fails += _check_join()

    _head("11. corpus coverage: tier 1 vs tier 2 on real contractor names")
    fails += coverage(u)

    print()
    print("=" * 78)
    print(f"{'ALL CHECKS PASSED' if fails == 0 else f'{fails} CHECK(S) FAILED'}")
    print("=" * 78)
    return fails


def coverage(universe: Universe | None = None, root: pathlib.Path | None = None) -> int:
    u = universe or load_universe()
    counts = scan_corpus_counts(root)
    names = list(counts)
    if not names:
        print(f"  (no articles found under {root or corpus_root()}; "
              f"raw/*.html is gitignored, run `make fetch` first)")
        return 0
    print(f"  source: {root or corpus_root()}")
    groups = group_variants(names)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    exact = prefix = 0
    hit_mentions = 0
    misses: list[str] = []
    by_ticker: dict[str, int] = {}
    group_counts = {representative(v): sum(counts[x] for x in v) for v in groups.values()}
    for n, c in group_counts.items():
        m = u.match(n)
        if m is None:
            misses.append(n)
            continue
        alias, method = m
        by_ticker[alias.ticker] = by_ticker.get(alias.ticker, 0) + c
        hit_mentions += c
        if method == "alias_exact":
            exact += 1
        else:
            prefix += 1
    hit = exact + prefix
    total = len(groups)
    mentions = sum(counts.values())
    n_batches = (len(misses) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"  distinct RAW contractor names     : {len(names)}  ({mentions} award mentions)")
    print(f"  distinct NORMALIZED companies     : {total}")
    print(f"  duplicate raw spellings collapsed : {len(names) - total}  "
          f"({len(dupes)} companies printed more than one way) <- duplicate spend avoided")
    print(f"  TIER 1 deterministic (free)       : {hit:4d}  ({hit / total:5.1%} of companies)"
          f"   [{exact} exact, {prefix} prefix]")
    print(f"  TIER 2 model needed               : {len(misses):4d}  "
          f"({len(misses) / total:5.1%} of companies)")
    print(f"  TIER 1 by award mention (weighted): {hit_mentions:4d}/{mentions} "
          f"({hit_mentions / mentions:5.1%})  <- the primes repeat; the tail does not")
    print(f"  tier-2 cost                       : {n_batches} batched call(s) at "
          f"BATCH_SIZE={BATCH_SIZE}, once, ever (then entity_map)")
    print(f"  naive one-call-per-raw-name cost  : {len(names)} calls "
          f"({len(names) / max(n_batches, 1):.0f}x more)")
    if dupes:
        print("  examples of collapsed variants:")
        for k, v in list(dupes.items())[:5]:
            print(f"      {k!r} <- {v}")
    if by_ticker:
        print("  tier-1 hits by ticker             : " +
              ", ".join(f"{t}={c}" for t, c in sorted(by_ticker.items(),
                                                      key=lambda kv: -kv[1])))
    print("  sample of tier-2 names (these are what a model would see):")
    for n in misses[:12]:
        print(f"      {n}")
    if len(misses) > 12:
        print(f"      ... and {len(misses) - 12} more")
    return 0


def warm(map_path: pathlib.Path = ENTITY_MAP_PATH) -> int:
    """Persist every tier-1 hit in the corpus. Free, deterministic, no model calls."""
    names = scan_corpus()
    emap = load_entity_map(map_path)
    groups = group_variants(names)
    added = 0
    for norm, variants in groups.items():
        if norm in emap:
            continue
        rep = representative(variants)
        r = tier1_match(rep)
        if r is None:
            continue
        entry = dict(r)
        entry[_MAP_EXTRA] = sorted(variants)
        emap[norm] = entry
        added += 1
    save_entity_map(emap, map_path)
    print(f"warmed {map_path}: +{added} deterministic entries, {len(emap)} total, $0.00")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="offline validation, free")
    ap.add_argument("--coverage", action="store_true", help="tier-1 vs tier-2 corpus split")
    ap.add_argument("--warm", action="store_true", help="persist every tier-1 corpus hit")
    ap.add_argument("--resolve", nargs="+", metavar="NAME", help="resolve names (replay only)")
    ap.add_argument("--live", action="store_true", help="allow tier-2 API calls (costs money)")
    args = ap.parse_args(argv)

    if args.coverage:
        return coverage()
    if args.warm:
        return warm()
    if args.resolve:
        llm = CachedLLM(live=args.live)
        res = resolve(args.resolve, llm)
        print(json.dumps(res.records, indent=2))
        print(json.dumps(res.stats, indent=2))
        print(llm.usage.summary())
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
