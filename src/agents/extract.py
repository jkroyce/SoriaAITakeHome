"""Agent A -- prose to award rows.

One model call per DAY-DOCUMENT, not per award. A single war.gov contract
announcement is 5k-26k characters carrying 10-40 awards under ALL-CAPS service
headers; asking once for the whole document is both cheaper and *more accurate*,
because the header a paragraph inherits, the shared ceiling of a multi-award pool
and the "*Small business" footnote are all document-level context that a
paragraph-at-a-time prompt would throw away.

Division of labour, per CLAUDE.md:

  * the MODEL reads prose and decides things that need judgement -- which action type
    this paragraph is, which of three dollar figures is the amount of *this* action,
    how confident it honestly is
  * DETERMINISTIC CODE fills everything exact and repeatable -- ``award_uid``,
    ``announcement_id``, ``announced_date``, ``extracted_at``, ``extractor_model``,
    ``llm_cache_key``, ``skills_version`` -- and repairs the handful of facts that are
    mechanically checkable (an asterisk in the name IS a small business; a
    ``multi_award_pool`` IS multi-award). Those five provenance fields are ``llm=False``
    in ``schemas.py``, so they are not in the prompt schema and the model cannot
    populate them even if it tried.

The prompt is ``BASE_INSTRUCTIONS + skills/extraction.md + the document``. Because
``CachedLLM`` keys its cache on the full prompt, editing the skills file invalidates
every extraction -- which is exactly why skill promotion is a gated step.

Offline behaviour is the normal case: ``CachedLLM(live=False)`` raises
``NotCachedError`` on a miss, and ``make demo`` replays entirely from the committed
cache with no API key.

CLI::

    python src/agents/extract.py 4586879 --dry-run   # print prompt + schema, no call
    python src/agents/extract.py 4586879             # replay from cache (or NotCached)
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any

# Allow both `import agents.extract` (with src/ on the path, how the manager runs)
# and `python src/agents/extract.py` (how a human pokes at it).
_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import schemas                                   # noqa: E402  the frozen contract
from config import BULK_MODEL, RAW, ROOT         # noqa: E402
from fetch import body_text                      # noqa: E402
import llm as llm_mod                            # noqa: E402  CACHE_DIR, for reporting
from llm import CachedLLM                        # noqa: E402

SKILLS_PATH = ROOT / "skills" / "extraction.md"

# Fixed, not derived from document length: max_tokens is part of the cache key, so a
# value that drifts with the input would fragment the cache. The largest document in
# the corpus is ~26k chars / ~35 awards; 32k output tokens clears that with room, and
# an unused ceiling costs nothing (billing is on tokens produced, not on the cap).
MAX_TOKENS = 32_000

SYSTEM = (
    "You are a contract-analysis extractor for a defense-investment research terminal. "
    "You read one day's U.S. Department of War contract announcement and return every "
    "award in it as structured JSON matching the supplied schema exactly. You are "
    "precise, you never invent a value that is not in the text, and you report honest "
    "confidence -- a truthful 0.5 is far more useful to this system than a confident "
    "wrong answer, because anything below 0.7 is routed to a human reviewer."
)

BASE_INSTRUCTIONS = """\
# Task

Extract EVERY award in the announcement below into the `awards` array, in the order
they appear, then write one `document_notes` string about the document as a whole
(or null if there is nothing a reviewer needs to know).

# What counts as one row

One row per CONTRACTOR-AWARD, not one row per paragraph.

* An ordinary paragraph naming one company and one dollar figure is ONE row.
* A paragraph that lists several companies -- each with its own contract number in
  parentheses -- competing for orders under ONE shared dollar ceiling is a
  MULTI-AWARD POOL: emit ONE ROW PER COMPANY. Seven companies means seven rows.
* Never merge two companies into one row. Never split one company into two rows.
* The `*Small business` legend at the foot of the document is a footnote, not an
  award. Section headers are not awards.

# Field rules

* `service_branch` -- the ALL-CAPS section header the award sits under. Headers are
  on their own line and every award inherits the nearest header ABOVE it until the
  next header appears. Use the enum value that matches the header exactly; if no enum
  value matches (for example `U.S. SPECIAL OPERATIONS COMMAND`), use `OTHER` and put
  the literal header text in `extraction_notes`.
* `contractor_raw` -- the name EXACTLY as printed, including `Inc.`, `LLC`, `Corp.`
  and any trailing asterisk. Do not clean it up, expand it, or resolve it to a parent
  company: entity resolution is a different agent's job and it needs the raw string.
* `contractor_city` / `contractor_state` -- as printed, e.g. `Bristol` /
  `Pennsylvania`. Null when the entry gives none.
* `amount_usd` -- the value of THIS action, in whole dollars, as an integer with no
  `$`, commas or decimals ($180,000,000 -> 180000000). In order of preference:
  "The amount of this action is $X" -> X; otherwise the figure in the award phrase
  ("was awarded a $X ... contract") -> X; for a multi-award pool, the shared ceiling.
  Money that is NOT `amount_usd`: obligation amounts ("funds in the amount of $X were
  obligated at the time of award"), maximum/option values ("The maximum dollar value,
  including the base price and 13 options, is $Y"), and cumulative face values.
* `action_type` -- exactly one of:
  - `multi_award_pool`: several companies compete for orders under one shared vehicle
    (takes precedence over `new_award` for every company in the pool)
  - `option_exercise`: the text says the action exercises an option
  - `modification`: a modification, amendment or change to an existing contract that
    is not an option exercise
  - `new_award`: everything else, including a task order placed under an existing IDIQ
* `contract_number` -- this contractor's own number, usually in parentheses at the end
  of the entry, e.g. `W15QKN-26-D-A084`. In a multi-award pool it is the number
  printed next to THAT company, never a sibling's. For a modification with no new
  number of its own, repeat the base contract number. Null if genuinely absent.
* `base_contract_number` -- for a modification, option exercise, or task order: the
  underlying contract being modified or ordered against. Null for a plain new award.
* `modification_number` -- the token in parentheses right after the word
  "modification", e.g. `P00002`, `P00005`, `001 CB`. Null when there is none.
* `cumulative_face_value_usd` -- the total AFTER this action, when stated ("with a
  total cumulative face value of $280,000,000", "brings the total cumulative face
  value of the contract to $165,655,369"). Integer, whole dollars. Null otherwise.
* `pricing_type` -- the pricing arrangement as printed, lowercased, e.g.
  `firm-fixed-price`, `cost-plus-fixed-fee`, `firm-fixed-price, indefinite-delivery/
  indefinite-quantity`. Null when not stated.
* `is_idiq` -- true when the entry says indefinite-delivery/indefinite-quantity,
  indefinite delivery-indefinite quantity, IDIQ, or describes orders placed under the
  contract. False when it is plainly a single definite contract.
* `is_multi_award` -- true when several companies will compete for orders under one
  shared vehicle. True on EVERY row of a pool, false on an ordinary single award.
* `work_description` -- what the contract is for, condensed to ONE sentence in your
  own words. No boilerplate about funding or bids.
* `place_of_performance` -- where work is performed, as printed. When the entry says
  locations are set per order, record that (e.g. "Work locations and funding will be
  determined with each order").
* `completion_date` -- the estimated completion date as printed, free text, e.g.
  `Aug. 31, 2031` or `July 2028`. Do not reformat it.
* `contracting_activity` -- the contracting activity and its location, e.g.
  `Army Contracting Command, Newark, New Jersey`.
* `bids_solicited` / `bids_received` -- only what is actually stated. "One bid was
  solicited with one received" -> 1 / 1. "Bids were solicited via the internet with 19
  received" -> null / 19 (the solicited count is NOT stated). "competitively procured
  via the SAM.gov website, with 14 offers received" -> null / 14. Never infer a
  solicited count from a received count and never write 0 for "not stated".
* `small_business` -- true when the entry carries the asterisk marker. Within a pool
  this differs company by company: only the asterisked companies are small businesses.
* `extraction_confidence` -- 0.0-1.0, honest. Guidance:
  - 0.95+ a plain, complete entry: one company, one amount, one contract number
  - 0.80-0.94 you had to make one judgement call (which action type, which of several
    dollar figures, a pool split)
  - 0.60-0.79 something is genuinely ambiguous or a key field is missing
  - below 0.6 you are guessing
  Below 0.7 sends the row to a human, so do not inflate it -- but do not deflate a
  clean entry either, because that wastes a reviewer's attention.
* `extraction_notes` -- what an ambiguous entry needs a reviewer to know. Empty string
  or null when the entry was unambiguous. Do not restate the work description here.

# Fields you must NOT produce

Ids, dates and provenance (`award_uid`, `announcement_id`, `announced_date`,
`extracted_at`, `extractor_model`, `llm_cache_key`, `skills_version`) are computed by
deterministic code after you answer. They are not in your schema. Do not invent them.

# Output

Return JSON matching the schema exactly: every property present on every award, using
`null` for a value the text does not give. Never drop a key, never add one.
"""

# Fields the model is allowed to fill, and the ones we fill ourselves.
_LLM_FIELD_NAMES: list[str] = [f.name for f in schemas.llm_fields(schemas.AWARD_FIELDS)]
_ALL_FIELD_NAMES: list[str] = [f.name for f in schemas.AWARD_FIELDS]
_DETERMINISTIC_FIELD_NAMES: list[str] = [n for n in _ALL_FIELD_NAMES if n not in _LLM_FIELD_NAMES]
_FIELD_BY_NAME = {f.name: f for f in schemas.AWARD_FIELDS}


class ExtractionError(RuntimeError):
    """The model answered, but the answer was not usable."""


@dataclass
class ExtractionResult:
    """Everything one day-document produced, rows plus the provenance around them."""
    announcement_id: str
    announced_date: str | None
    awards: list[dict] = dc_field(default_factory=list)
    document_notes: str | None = None
    cache_key: str = ""
    model: str = ""
    skills_version: str = ""
    body_chars: int = 0
    repairs: list[str] = dc_field(default_factory=list)

    @property
    def min_confidence(self) -> float:
        vals = [a.get("extraction_confidence") for a in self.awards]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return min(vals) if vals else 0.0

    @property
    def low_confidence(self) -> list[dict]:
        return [a for a in self.awards
                if not isinstance(a.get("extraction_confidence"), (int, float))
                or a["extraction_confidence"] < 0.7]


# --------------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------------

def load_skills(path: pathlib.Path | None = None) -> tuple[str, str]:
    """Return (skills text, skills_version).

    The version is a digest of the file's bytes, so it changes exactly when the rules
    change -- which is also exactly when the prompt, and therefore the cache key,
    changes. A missing file is legitimate on a cold start: the agent runs on base
    instructions alone and records ``skills_version='none'``.
    """
    p = pathlib.Path(path) if path else SKILLS_PATH
    if not p.exists():
        return "", "none"
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return "", "none"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return text, digest


# --------------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------------

def build_prompt(body: str, *, announced_date: str | None = None,
                 title: str | None = None, skills_text: str = "") -> str:
    """base instructions + learned skills + the document. Deterministic by design.

    Nothing time-varying goes in here -- no timestamps, no run ids -- or the cache key
    would change on every run and the whole architecture would be pointless.
    """
    parts = [BASE_INSTRUCTIONS]
    if skills_text:
        parts.append(
            "# Learned rules (skills/extraction.md)\n\n"
            "These were distilled from earlier documents by this same agent and "
            "reviewed before promotion. They override the general guidance above "
            "where they conflict.\n\n" + skills_text
        )
    header = "# Announcement"
    if title:
        header += f"\n\nTitle: {title}"
    if announced_date:
        header += f"\nAnnouncement date: {announced_date}"
    parts.append(f"{header}\n\n----- BEGIN ANNOUNCEMENT -----\n{body}\n----- END ANNOUNCEMENT -----")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------------
# Input resolution -- accept the several shapes callers actually have
# --------------------------------------------------------------------------------

def _announcement_id(announcement: dict) -> str:
    for k in ("announcement_id", "article_id", "id"):
        v = announcement.get(k)
        if v:
            return str(v)
    raise ExtractionError("announcement has no announcement_id/article_id")


def _resolve_html_path(announcement: dict, raw_dir: pathlib.Path | None) -> pathlib.Path:
    """Where the cached HTML lives, honouring the manifest's `cache_path` when given."""
    raw = pathlib.Path(raw_dir) if raw_dir else RAW
    cp = announcement.get("cache_path")
    if cp:
        # manifest stores it relative to the repo root, Windows-separated
        p = pathlib.Path(str(cp).replace("\\", "/"))
        if p.is_absolute() and p.exists():
            return p
        for base in (raw.parent, ROOT, raw):
            cand = base / p
            if cand.exists():
                return cand
    return raw / "articles" / f"{_announcement_id(announcement)}.html"


def announcement_body(announcement: dict, *, raw_dir: pathlib.Path | None = None) -> str:
    """Announcement prose, from whichever form the caller has.

    Accepts a pre-extracted ``body``/``body_text``, raw ``html``, or falls back to
    reading the cached article off disk and running ``fetch.body_text`` on it.
    """
    for k in ("body_text", "body", "prose"):
        v = announcement.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    html = announcement.get("html")
    if isinstance(html, str) and html.strip():
        return body_text(html)
    path = _resolve_html_path(announcement, raw_dir)
    if not path.exists():
        raise ExtractionError(f"no cached article for {_announcement_id(announcement)}: {path}")
    return body_text(path.read_text(encoding="utf-8", errors="replace"))


# --------------------------------------------------------------------------------
# Post-processing -- deterministic, and the only place the id/provenance fields are set
# --------------------------------------------------------------------------------

_MONEY_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _to_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v))
    if isinstance(v, str):
        m = _MONEY_RE.search(v.replace("$", ""))
        if not m:
            return None
        try:
            return int(round(float(m.group(0).replace(",", ""))))
        except ValueError:
            return None
    return None


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"true", "yes", "y", "1"}:
            return True
        if s in {"false", "no", "n", "0"}:
            return False
    if isinstance(v, (int, float)):
        return bool(v)
    return None


def _to_float(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _clean_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _coerce(row: dict) -> dict:
    """Type-normalize one model row and drop anything not in the contract."""
    out: dict[str, Any] = {}
    for name in _LLM_FIELD_NAMES:
        v = row.get(name)
        f = _FIELD_BY_NAME[name]
        sql = f.sql
        if sql in ("BIGINT", "INTEGER"):
            out[name] = _to_int(v)
        elif sql == "BOOLEAN":
            out[name] = _to_bool(v)
        elif sql == "DOUBLE":
            out[name] = _to_float(v)
        else:
            out[name] = _clean_str(v)
    return out


def _repair(row: dict, repairs: list[str]) -> dict:
    """Apply the rules that are exact enough that a model call would be a bug.

    Each repair is recorded so a reviewer can see where the model needed correcting --
    that record is the raw material for the next skill rule.
    """
    who = row.get("contractor_raw") or "?"

    # An asterisk in the printed name IS the small-business marker. This is a string
    # test, not a judgement, so code owns it.
    if row.get("contractor_raw") and "*" in row["contractor_raw"]:
        if row.get("small_business") is not True:
            repairs.append(f"small_business=True from asterisk in {who!r}")
        row["small_business"] = True

    # A pool is multi-award by definition, and vice versa.
    if row.get("action_type") == "multi_award_pool" and row.get("is_multi_award") is not True:
        repairs.append(f"is_multi_award=True implied by action_type for {who!r}")
        row["is_multi_award"] = True

    # A modification that names no base contract is exactly the sort of thing a human
    # should look at; flag it rather than silently guessing.
    if row.get("action_type") in ("modification", "option_exercise") and not row.get("base_contract_number"):
        if row.get("contract_number"):
            row["base_contract_number"] = row["contract_number"]
            repairs.append(f"base_contract_number defaulted to contract_number for {who!r}")
        else:
            repairs.append(f"{row['action_type']} with no contract number for {who!r}")

    # Confidence must exist and must be in range; a missing one is not an implicit 1.0.
    c = row.get("extraction_confidence")
    if c is None:
        row["extraction_confidence"] = 0.5
        repairs.append(f"missing extraction_confidence -> 0.5 for {who!r}")
    else:
        row["extraction_confidence"] = max(0.0, min(1.0, float(c)))

    # An action type outside the enum cannot be stored; keep the row but make it
    # obvious to the reviewer instead of dropping data on the floor.
    if row.get("action_type") not in schemas.ACTION_TYPES:
        bad = row.get("action_type")
        repairs.append(f"action_type {bad!r} not in enum -> new_award for {who!r}")
        row["action_type"] = "new_award"
        row["extraction_notes"] = _join_notes(row.get("extraction_notes"),
                                              f"model returned action_type={bad!r}")
        row["extraction_confidence"] = min(row["extraction_confidence"], 0.5)

    if row.get("service_branch") is not None and row["service_branch"] not in schemas.BRANCHES:
        bad = row["service_branch"]
        repairs.append(f"service_branch {bad!r} not in enum -> OTHER for {who!r}")
        row["extraction_notes"] = _join_notes(row.get("extraction_notes"), f"header {bad!r}")
        row["service_branch"] = "OTHER"

    return row


def _join_notes(existing: str | None, addition: str) -> str:
    parts = [p for p in [(existing or "").strip(), addition.strip()] if p]
    return "; ".join(parts)


def postprocess(payload: dict, *, announcement_id: str, announced_date: str | None,
                model: str, cache_key: str, skills_version: str,
                extracted_at: str | None = None) -> tuple[list[dict], list[str]]:
    """Turn the model's `{"awards": [...]}` into storable rows.

    This is where the five ``llm=False`` fields are filled -- and the ONLY place. The
    model never sees them.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("awards"), list):
        raise ExtractionError(f"model returned no awards array (got {type(payload).__name__})")

    ts = extracted_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    repairs: list[str] = []

    for ordinal, raw_row in enumerate(payload["awards"]):
        if not isinstance(raw_row, dict):
            repairs.append(f"skipped non-object award at index {ordinal}")
            continue
        row = _repair(_coerce(raw_row), repairs)
        if not row.get("contractor_raw"):
            repairs.append(f"skipped award at index {ordinal}: no contractor_raw")
            continue

        # -- deterministic identity, lineage and provenance --
        row["announcement_id"] = announcement_id
        row["announced_date"] = announced_date
        row["award_uid"] = schemas.award_uid(
            announcement_id, row.get("contract_number"), row["contractor_raw"], ordinal
        )
        row["extracted_at"] = ts
        row["extractor_model"] = model
        row["llm_cache_key"] = cache_key
        row["skills_version"] = skills_version

        rows.append({k: row.get(k) for k in _ALL_FIELD_NAMES})

    # award_uid is the primary key; a collision means two identical (contract number,
    # contractor) pairs in one document. Fall back to the ordinal form so nothing is
    # silently overwritten on upsert.
    seen: dict[str, int] = {}
    for ordinal, row in enumerate(rows):
        uid = row["award_uid"]
        if uid in seen:
            row["award_uid"] = schemas.award_uid(announcement_id, None, row["contractor_raw"], ordinal)
            repairs.append(f"duplicate award_uid for {row['contractor_raw']!r}, re-keyed by position")
        seen[row["award_uid"]] = ordinal

    return rows, repairs


# --------------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------------

def extract_document(announcement: dict, llm: CachedLLM, *,
                     model: str = BULK_MODEL,
                     skills_path: pathlib.Path | None = None,
                     raw_dir: pathlib.Path | None = None,
                     max_tokens: int = MAX_TOKENS,
                     extracted_at: str | None = None) -> ExtractionResult:
    """One model call for one day-document. Returns rows plus document-level context."""
    aid = _announcement_id(announcement)
    announced_date = announcement.get("announced_date")
    body = announcement_body(announcement, raw_dir=raw_dir)

    skills_text, skills_version = load_skills(skills_path)
    prompt = build_prompt(body, announced_date=announced_date,
                          title=announcement.get("title"), skills_text=skills_text)
    schema = schemas.extraction_schema()

    # Recomputed with exactly the arguments passed to json_call, so the row's
    # llm_cache_key really does resolve to cache/llm/<key>.json.
    cache_key = CachedLLM.key(model, SYSTEM, prompt, schema, max_tokens)

    payload = llm.json_call(model=model, system=SYSTEM, prompt=prompt, schema=schema,
                            max_tokens=max_tokens, label=f"extract:{aid}")

    rows, repairs = postprocess(
        payload, announcement_id=aid, announced_date=announced_date, model=model,
        cache_key=cache_key, skills_version=skills_version, extracted_at=extracted_at,
    )
    return ExtractionResult(
        announcement_id=aid,
        announced_date=announced_date,
        awards=rows,
        document_notes=_clean_str(payload.get("document_notes")),
        cache_key=cache_key,
        model=model,
        skills_version=skills_version,
        body_chars=len(body),
        repairs=repairs,
    )


def extract_announcement(announcement: dict, llm: CachedLLM, **kwargs) -> list[dict]:
    """The manager's entry point: announcement in, storable award rows out.

    Every returned dict has exactly the columns of the ``awards`` table, in order.
    """
    return extract_document(announcement, llm, **kwargs).awards


# --------------------------------------------------------------------------------
# Dry run -- everything except the API call, so it is free
# --------------------------------------------------------------------------------

def dry_run(announcement: dict, *, model: str = BULK_MODEL,
            skills_path: pathlib.Path | None = None,
            raw_dir: pathlib.Path | None = None,
            max_tokens: int = MAX_TOKENS) -> dict:
    """Build everything the live call would send, and send nothing.

    Returns the exact system/prompt/schema/max_tokens tuple plus the cache key it
    would hit, which is enough to tell whether a paid call is even needed.
    """
    aid = _announcement_id(announcement)
    body = announcement_body(announcement, raw_dir=raw_dir)
    skills_text, skills_version = load_skills(skills_path)
    prompt = build_prompt(body, announced_date=announcement.get("announced_date"),
                          title=announcement.get("title"), skills_text=skills_text)
    schema = schemas.extraction_schema()
    key = CachedLLM.key(model, SYSTEM, prompt, schema, max_tokens)
    return {
        "announcement_id": aid,
        "model": model,
        "system": SYSTEM,
        "prompt": prompt,
        "schema": schema,
        "max_tokens": max_tokens,
        "cache_key": key,
        "cache_path": str(llm_mod.CACHE_DIR / f"{key}.json"),
        "skills_version": skills_version,
        "body_chars": len(body),
        "prompt_chars": len(prompt),
        "est_input_tokens": round(len(prompt) / 3.7),
    }


def load_manifest(raw_dir: pathlib.Path | None = None) -> list[dict]:
    raw = pathlib.Path(raw_dir) if raw_dir else RAW
    p = raw / "manifest.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def announcement_from_manifest(article_id: str, raw_dir: pathlib.Path | None = None) -> dict:
    for row in load_manifest(raw_dir):
        if str(row.get("article_id")) == str(article_id):
            return row
    return {"article_id": str(article_id)}


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    aid = args[0] if args else "4586879"
    raw_dir = pathlib.Path(args[1]) if len(args) > 1 else None

    ann = announcement_from_manifest(aid, raw_dir)

    if "--dry-run" in flags:
        d = dry_run(ann, raw_dir=raw_dir)
        print("=" * 78)
        print(f"DRY RUN  announcement {d['announcement_id']}  model {d['model']}")
        print(f"body {d['body_chars']:,} chars -> prompt {d['prompt_chars']:,} chars "
              f"(~{d['est_input_tokens']:,} input tokens)")
        print(f"skills_version {d['skills_version']}   max_tokens {d['max_tokens']:,}")
        print(f"cache key {d['cache_key']}")
        print("=" * 78)
        print("--- SYSTEM ---")
        print(d["system"])
        print("--- PROMPT ---")
        print(d["prompt"])
        print("--- SCHEMA ---")
        print(json.dumps(d["schema"], indent=2))
        raise SystemExit(0)

    res = extract_document(ann, CachedLLM(live="--live" in flags), raw_dir=raw_dir)
    print(f"{res.announcement_id}: {len(res.awards)} award(s), "
          f"min confidence {res.min_confidence:.2f}, cache key {res.cache_key[:12]}")
    if res.repairs:
        print("repairs:")
        for r in res.repairs:
            print(f"  - {r}")
    print(json.dumps(res.awards, indent=2, default=str))
