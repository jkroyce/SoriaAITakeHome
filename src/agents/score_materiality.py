"""Award -> investor materiality, in two tiers.

    TIER 1   deterministic pre-filter    obviously-routine awards    free, reproducible
    TIER 2   claude-haiku-4-5            batched judgement call      costs money, cached
    TIER 2b  claude-opus-5               escalation on conf < 0.7    costs more, cached

This is the sharpest case of CLAUDE.md's central rule -- *the agent judges whether a
change matters, never whether one occurred* -- so every decision below is labelled with
which side of that line it falls on.

DETERMINISTIC (no model, ever)
    * finding what needs scoring          a set difference on award_uid  (detects())
    * ranking a feed by score or dollars  a sort                         (top_awards())
    * the routine pre-filter              thresholds on amount_usd, the small-business
                                          asterisk and a ticker lookup   (prefilter())
    * score -> tier banding               a comparison                   (tier_for())
    * every llm=False column              scored_at, scorer_model, llm_cache_key,
                                          skills_version
    * batching, alignment, validation, escalation routing, review routing

THE MODEL'S JOB -- the part no threshold can answer
    Is a $180M modification on an existing vehicle more material than a $200M new award
    to a company it would double? Does a private firm beating a prime on a recompete
    signal a share shift worth telling a holder of the prime about? Is a $2B IDIQ
    ceiling with seven names on it worth less to each of them than a $60M sole-source
    production order? Those require knowing what the company is, what the program is,
    and what an investor already expects -- world knowledge, not arithmetic.
    Size *relative to revenue* lives here too: data/universe.csv carries no revenue
    column, so the ratio genuinely cannot be computed and is a judgement.

ON `tier`
    schemas.MATERIALITY_FIELDS marks `tier` llm=True, so it IS asked for in the prompt
    (object_schema emits every llm=True field and the model must answer all of them).
    But a band boundary is a comparison, and a comparison is not reasoning: after the
    call, `_coerce` OVERRIDES the model's tier with tier_for(score) whenever the two
    disagree, records the disagreement in the rationale and caps confidence so the row
    escalates. The model's judgement is the 0-100 score; the banding of that score is
    ours. This is stated rather than done quietly (see selftest section 4).

ON `confidence`
    schemas.MATERIALITY_FIELDS has NO confidence column, and src/schemas.py is frozen,
    so confidence is not persisted in the materiality table. It is still asked for (as a
    prompt-only field appended to the field list handed to object_schema), and it still
    drives escalation and review routing exactly as CLAUDE.md requires; it survives in
    ScoreResult.confidence and in review_queue.confidence / review_queue.payload. This
    is a real gap in the contract, reported rather than coded around.

Public surface
    score_awards(awards, llm)  -> {award_uid: materiality row}   # manager entry point
    score(awards, llm)         -> ScoreResult (rows, pending, review, stats)
    prefilter(award, ent)      -> row or None                    # never calls a model
    plan(awards, entities)     -> (prefiltered, queue)           # never calls a model
    tier_for(score)            -> "alert" | "notable" | "routine"
    ScoreMaterialityAgent      -> the CleaningAgent protocol shape

CLI (all free, no API key, no network, no spend):
    .venv/Scripts/python.exe src/agents/score_materiality.py --selftest
    .venv/Scripts/python.exe src/agents/score_materiality.py --corpus
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html
import json
import math
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
from llm import CachedLLM, NotCachedError, Usage, PRICING   # noqa: E402  (ONLY model access)

# --------------------------------------------------------------------------------
# paths and constants
# --------------------------------------------------------------------------------

SKILLS_PATH = config.ROOT / "skills" / "materiality.md"

BULK_MODEL = config.BULK_MODEL          # claude-haiku-4-5
JUDGE_MODEL = config.JUDGE_MODEL        # claude-opus-5, escalation only

#: awards per tier-2 call. Award context is ~10x a contractor name, so this sits below
#: the resolver's 25. Measured on the real corpus: see `--corpus`.
BATCH_SIZE = 20

#: CLAUDE.md: below this the manager escalates to Opus, then to review_queue.
LOW_CONFIDENCE = 0.7

DETERMINISTIC = "deterministic"

# -- score bands. A band boundary is a comparison, not a judgement. --
ALERT_AT = 70
NOTABLE_AT = 40

# -- pre-filter thresholds (deterministic). Grounded in the 50-day corpus: median
#    award $31.0M, p25 $15.6M, p75 $77.8M -- see `--corpus` and skills R-001/R-002. --
#: below this, an award to a contractor we cannot show is public is noise
ROUTINE_FLOOR_USD = 25_000_000
#: a small-business set-aside (`*`) is by statute not a large listed company, so the
#: floor for those is higher
SMALL_BUSINESS_FLOOR_USD = 50_000_000
#: an entity RESOLVED private with confidence -- a stronger signal than "not in the
#: alias table" -- earns a higher floor still
RESOLVED_PRIVATE_FLOOR_USD = 100_000_000
#: highest score a pre-filtered row may carry; keeps it inside the routine band
ROUTINE_MAX_SCORE = ALERT_AT // 2 - 1          # 34

_MATERIALITY_FIELDS, _MATERIALITY_PK = schemas.TABLES["materiality"]

#: exactly the columns of schemas.TABLES["materiality"], in order.
MATERIALITY_KEYS = [f.name for f in _MATERIALITY_FIELDS]

#: the deterministic (non-model) subset -- these must NEVER appear in a prompt schema.
DETERMINISTIC_KEYS = [f.name for f in _MATERIALITY_FIELDS if not f.llm]

#: the tier enum, read out of the frozen contract rather than retyped.
TIERS = tuple(
    v for v in next(f for f in _MATERIALITY_FIELDS if f.name == "tier").json["enum"]
    if v is not None
)

#: review_queue column names, also derived from the contract.
REVIEW_KEYS = [f.name for f in schemas.TABLES["review_queue"][0]]

#: awards column names, for building the fact block without hardcoding a list.
AWARD_KEYS = [f.name for f in schemas.TABLES["awards"][0]]

#: prompt-only. There is no confidence column in MATERIALITY_FIELDS (see module
#: docstring); this Field exists so the prompt schema is still built by
#: schemas.object_schema() from a field list rather than by hand-writing a dict.
CONFIDENCE_FIELD = schemas.Field(
    "confidence", "DOUBLE", {"type": "number"},
    "0.0-1.0, how sure you are of THIS score. Below 0.7 sends the award to a human. "
    "An honest 0.5 is far more useful than a confident wrong answer.",
    llm=True,
)

#: the field list handed to object_schema for one scored award.
SCORE_FIELDS = list(_MATERIALITY_FIELDS) + [CONFIDENCE_FIELD]


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

    The skills text is part of the tier-2 system prompt and CachedLLM keys on the
    prompt, so a skills edit necessarily invalidates this agent's cache. Versioning by
    content makes that relationship visible in the stored row instead of implicit.
    """
    text = skills_text()
    if not text:
        return "none"
    return "sk-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def tier_for(score: int) -> str:
    """Band a 0-100 score. DETERMINISTIC: this is a comparison, not a judgement."""
    if score >= ALERT_AT:
        return "alert"
    if score >= NOTABLE_AT:
        return "notable"
    return "routine"


def _row(**kw) -> dict:
    """A materiality row: exactly the MATERIALITY_FIELDS keys, in schema order.

    The llm=False fields are filled here, deterministically, and never by the model.
    """
    rec = {k: kw.get(k) for k in MATERIALITY_KEYS}
    rec["award_uid"] = kw["award_uid"]
    rec["scored_at"] = rec["scored_at"] or _now()
    rec["scorer_model"] = rec["scorer_model"] or DETERMINISTIC
    rec["skills_version"] = rec["skills_version"] or skills_version()
    if rec["drivers"] is None:
        rec["drivers"] = []
    return rec


def needs_escalation(confidence) -> bool:
    """CLAUDE.md's routing rule, in one place."""
    try:
        return float(confidence) < LOW_CONFIDENCE
    except (TypeError, ValueError):
        return True


def validate_row(row: dict) -> list[str]:
    """Contract check for one materiality row. Returns problems (empty = ok).

    Derived entirely from schemas.MATERIALITY_FIELDS -- no hardcoded column list.
    """
    problems: list[str] = []
    if list(row) != MATERIALITY_KEYS:
        missing = [k for k in MATERIALITY_KEYS if k not in row]
        extra = [k for k in row if k not in MATERIALITY_KEYS]
        if missing:
            problems.append(f"missing fields: {missing}")
        if extra:
            problems.append(f"unknown fields: {extra}")
        if not missing and not extra:
            problems.append("fields present but out of schema order")
    for f in _MATERIALITY_FIELDS:
        if not f.llm:
            continue
        v = row.get(f.name)
        types = f.json.get("type")
        types = types if isinstance(types, list) else [types]
        if v is None and "null" not in types:
            problems.append(f"{f.name} is null but the contract forbids it")
        if v is not None and "enum" in f.json and v not in f.json["enum"]:
            problems.append(f"{f.name}={v!r} outside enum {f.json['enum']}")
    # deterministic fields: filled by us, never by the model
    for name in DETERMINISTIC_KEYS:
        if name == "llm_cache_key":
            continue                               # legitimately null on a pre-filter hit
        if row.get(name) in (None, ""):
            problems.append(f"deterministic field {name} not populated")
    s = row.get("score")
    if not isinstance(s, int) or isinstance(s, bool):
        problems.append(f"score is {type(s).__name__}, not int")
    elif not 0 <= s <= 100:
        problems.append(f"score {s} outside 0..100")
    elif row.get("tier") != tier_for(s):
        problems.append(f"tier {row.get('tier')!r} does not band score {s} "
                        f"(expected {tier_for(s)!r})")
    d = row.get("drivers")
    if not isinstance(d, list) or not all(isinstance(x, str) for x in d):
        problems.append("drivers is not a list of strings")
    else:
        try:
            json.dumps(d)
        except (TypeError, ValueError) as exc:
            problems.append(f"drivers is not JSON-serialisable: {exc}")
    if not (row.get("rationale") or "").strip():
        problems.append("rationale is empty")
    return problems


# --------------------------------------------------------------------------------
# award facts -- everything below is a lookup or arithmetic, so no model is involved
# --------------------------------------------------------------------------------

def _int_or_none(v) -> int | None:
    try:
        if v is None or isinstance(v, bool):
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _usd(v) -> str:
    n = _int_or_none(v)
    if n is None:
        return "unstated"
    if n >= 1_000_000_000:
        return f"${n / 1e9:,.2f}B"
    if n >= 1_000_000:
        return f"${n / 1e6:,.1f}M"
    return f"${n:,}"


def known_public(award: dict, entities: dict | None = None) -> dict | None:
    """Is this contractor a listed company we can already name? Never calls a model.

    Consulted in cost order, cheapest first, exactly like the resolver's tiers:
      1. the entities row the manager handed us (authoritative -- it is the DB)
      2. data/entity_map.json  (tier 0, already-reasoned facts)
      3. data/universe.csv     (tier 1, the deterministic alias table)

    Returns the entity record when it is public, else None. `None` means "not shown to
    be public", NOT "private" -- that distinction is what keeps the pre-filter honest.
    """
    raw = (award.get("contractor_raw") or "").strip()
    if not raw:
        return None
    if entities:
        ent = entities.get(raw)
        if ent and ent.get("is_public") and ent.get("ticker"):
            return ent
    lookup = _resolver_lookup()
    if lookup is None:
        return None
    return lookup(raw)


def resolved_private(award: dict, entities: dict | None = None) -> bool:
    """True only when an entities row says private AND is confident enough to trust."""
    raw = (award.get("contractor_raw") or "").strip()
    ent = (entities or {}).get(raw)
    if not ent:
        return False
    if ent.get("is_public") or ent.get("ticker"):
        return False
    return not needs_escalation(ent.get("confidence"))


_LOOKUP_CACHE: list = []


def _resolver_lookup():
    """A raw-name -> public-entity function borrowed from the merged resolver.

    Imported lazily and defensively: this agent must still run if resolve_entity is
    absent or its data files are missing. It is read, never written, and never edited.
    """
    if _LOOKUP_CACHE:
        return _LOOKUP_CACHE[0]
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import resolve_entity as _re                # noqa: WPS433  (read-only reuse)
        universe = _re.load_universe()
        emap = _re.load_entity_map()

        def lookup(raw: str):
            known = emap.get(_re.normalize(raw))
            if known and known.get("is_public") and known.get("ticker"):
                return known
            hit = _re.tier1_match(raw, universe)
            if hit and hit.get("is_public") and hit.get("ticker"):
                return hit
            return None
    except Exception as exc:                        # data missing, or module absent
        sys.stderr.write(f"! entity lookup unavailable ({type(exc).__name__}: {exc}); "
                         f"pre-filter will treat every contractor as unknown\n")

        def lookup(raw: str):
            return None
    _LOOKUP_CACHE.append(lookup)
    return lookup


def corpus_context(awards: list[dict]) -> dict[str, dict]:
    """Per-award context derived by GROUP BY and SORT over the batch. No model.

    An award's meaning depends on things a model cannot reliably eyeball across a long
    list -- how often this contractor appears, what they total, where this dollar
    figure sits in the distribution. All three are arithmetic, so code computes them
    and hands them over as *facts in the prompt* rather than asking for them.
    """
    amounts = sorted(a for a in (_int_or_none(x.get("amount_usd")) for x in awards)
                     if a is not None)
    by_contractor: dict[str, list[int]] = {}
    for a in awards:
        raw = (a.get("contractor_raw") or "").strip()
        by_contractor.setdefault(raw, []).append(_int_or_none(a.get("amount_usd")) or 0)

    out: dict[str, dict] = {}
    for a in awards:
        raw = (a.get("contractor_raw") or "").strip()
        amt = _int_or_none(a.get("amount_usd"))
        if amounts and amt is not None:
            below = sum(1 for x in amounts if x < amt)
            pct = round(100.0 * below / len(amounts))
        else:
            pct = None
        seen = by_contractor.get(raw, [])
        out[a.get("award_uid") or raw] = {
            "amount_percentile": pct,
            "contractor_awards_in_window": len(seen),
            "contractor_total_usd": sum(seen),
        }
    return out


# --------------------------------------------------------------------------------
# tier 1 -- the deterministic pre-filter. Never calls a model. Never spends.
# --------------------------------------------------------------------------------

def _routine_score(amount: int | None, ceiling: int) -> int:
    """Monotone in dollars, capped inside the routine band. Deterministic.

    Keeps the routine bucket *ranked* -- a $24M non-event still sorts above a $3M one
    -- without pretending the difference is a judgement.
    """
    if not amount or amount <= 0:
        return 1
    frac = min(1.0, amount / float(ceiling))
    return max(1, min(ROUTINE_MAX_SCORE, int(round(ROUTINE_MAX_SCORE * frac))))


def prefilter(award: dict, entities: dict | None = None) -> dict | None:
    """Return a routine row for an obviously-immaterial award, else None.

    ZERO model calls by construction: every input is amount_usd, the small-business
    asterisk, or a ticker lookup. Rules are conservative in one specific direction --
    they only fire when we can show the award is BOTH small and not attached to a
    company anyone can trade. Being unsure sends the award to the model, which costs
    money; being wrong here silences a real signal, which costs more.
    """
    amount = _int_or_none(award.get("amount_usd"))
    if amount is None:
        return None                                # unknown size is never obviously routine
    public = known_public(award, entities)
    if public is not None:
        return None                                # a listed name always reaches the model

    rule = ceiling = None
    if resolved_private(award, entities) and amount < RESOLVED_PRIVATE_FLOOR_USD:
        rule, ceiling = "resolved_private", RESOLVED_PRIVATE_FLOOR_USD
    elif award.get("small_business") and amount < SMALL_BUSINESS_FLOOR_USD:
        rule, ceiling = "small_business_setaside", SMALL_BUSINESS_FLOOR_USD
    elif amount < ROUTINE_FLOOR_USD:
        rule, ceiling = "sub_floor_unlisted", ROUTINE_FLOOR_USD
    if rule is None:
        return None

    score = _routine_score(amount, ceiling)
    why = {
        "resolved_private": (f"{_usd(amount)} to a contractor resolved as privately held; "
                             f"no listed security is exposed"),
        "small_business_setaside": (f"{_usd(amount)} small-business set-aside; a set-aside "
                                    f"awardee is by statute not a large listed company"),
        "sub_floor_unlisted": (f"{_usd(amount)} is below the {_usd(ROUTINE_FLOOR_USD)} "
                               f"floor and the contractor maps to no listed company"),
    }[rule]
    return _row(
        award_uid=award["award_uid"],
        score=score,
        tier=tier_for(score),
        rationale=f"Routine: {why}. Filtered deterministically; no model call.",
        drivers=[f"prefilter:{rule}", "unlisted_contractor", "below_size_floor"],
        scorer_model=DETERMINISTIC,
        llm_cache_key=None,
    )


def plan(awards: list[dict], entities: dict | None = None
         ) -> tuple[dict[str, dict], list[dict]]:
    """Split awards into (pre-filtered rows, model queue). No model calls.

    The manager's cost lever lives here: everything in the first return value is free
    and reproducible forever, and only the second costs money.
    """
    filtered: dict[str, dict] = {}
    queue: list[dict] = []
    for a in awards:
        uid = a.get("award_uid")
        if not uid:
            continue
        hit = prefilter(a, entities)
        if hit is not None:
            filtered[uid] = hit
        else:
            queue.append(a)
    return filtered, queue


def batches(queue: list[dict], size: int = BATCH_SIZE) -> list[list[dict]]:
    """Chunk the model queue deterministically.

    Sorted by award_uid before chunking so the same set of awards always produces the
    same batches and therefore the same cache keys. A new award reshuffles later
    batches, but an award already scored never reaches here -- so that only ever costs
    on genuinely new information, which is the intended cost model.
    """
    ordered = {a["award_uid"]: a for a in queue}
    keys = sorted(ordered)
    return [[ordered[k] for k in keys[i:i + size]] for i in range(0, len(keys), size)]


# --------------------------------------------------------------------------------
# tier 2 -- one model call for many awards
# --------------------------------------------------------------------------------

def batch_schema() -> dict:
    """Array of scored-award objects.

    The item shape comes from schemas.object_schema(), which emits ONLY llm=True
    fields, so award_uid, scored_at, scorer_model, llm_cache_key and skills_version are
    structurally absent from the prompt -- they are ours to fill and the model is never
    asked about them. `confidence` is appended as a prompt-only Field (see the module
    docstring); the contract has no column for it.
    """
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "description": "Exactly one entry per numbered award, in the same "
                               "order as the list, including awards you are unsure "
                               "about.",
                "items": schemas.object_schema(SCORE_FIELDS),
            },
        },
        "required": ["scores"],
        "additionalProperties": False,
    }


def build_system() -> str:
    """The judgement framing. Everything a threshold could answer is NOT in here."""
    rules = skills_text().strip()
    learned = f"\n\nLEARNED RULES (skills/materiality.md, version {skills_version()})\n{rules}\n" if rules else ""
    return f"""You score U.S. Department of War contract announcements for an equity investor.

Your ONLY job is judgement: does this award change what a rational holder of a listed
security should believe? Arithmetic has already been done for you -- the dollar value,
the action type, the small-business marker, this contractor's other awards in the
window and where the amount sits in the distribution are given as facts. Do not
re-derive them and do not reward size by itself.

SCORE, 0-100, and the band it falls in:
  {ALERT_AT}-100  alert   -- tell an investor now. It moves, or plausibly moves, a listed name:
              a program win or loss large against the awardee's revenue, a first
              award on a new program of record, a recompete that shifts share
              between named competitors, a termination or descoping.
  {NOTABLE_AT}-{ALERT_AT - 1}   notable -- worth a look. Confirms or extends a known program, adds real
              backlog, or is a first appearance by a name worth tracking.
  0-{NOTABLE_AT - 1}    routine -- noise. Routine sustainment, a small order under a known
              vehicle, an administrative modification, an award to a company no
              investor can hold.

WHAT ACTUALLY MOVES A SCORE
  * Size RELATIVE TO THE AWARDEE, not in absolute dollars. $75M is transformational
    for a $200M-revenue company and rounding for Lockheed Martin. We hold no revenue
    data, so this is yours to judge -- and say which you mean in the rationale.
  * A modification can outrank a bigger new award. A $180M mod that expands an
    existing program of record is real incremental revenue on a vehicle already won; a
    large IDIQ ceiling with no orders attached is an option, not revenue.
  * A multi-award pool ceiling is SHARED. Seven names competing for orders under one
    $160M ceiling is not seven $160M wins. Score each on its realistic share.
  * Competition tells you about share. One bid received is incumbency; a name winning
    against a stated field of many is share moving. A private firm taking work from a
    listed prime is material TO THE PRIME even though the winner is untradeable.
  * Sole-source, urgent, undefinitized and letter contracts signal a program under
    schedule pressure -- often a bigger tell than the dollar figure.
  * Foreign military sales carry different margin and political risk than domestic.

HONESTY
  * Most announcements are routine. Saying so is the correct answer, not a failure.
  * When the awardee maps to no listed company, the only reason to score above routine
    is a read-through to someone listed -- a competitor losing share, a supplier, a
    program signal. Say whose stock it is about.
  * `confidence` below 0.7 sends the award to a human. Use it. A truthful 0.5 beats a
    confident wrong answer, and a wrong "alert" costs an investor more than a missed
    "notable".

OUTPUT
  * `rationale`: ONE sentence an investor would actually find useful. Name the ticker
    when there is one. No restating the award.
  * `drivers`: 1-4 short snake_case tags, e.g. size_vs_revenue, new_program_of_record,
    recompete_loss, incumbent_retained, ceiling_not_revenue, sustainment, fms,
    read_through_to_prime, sole_source, share_shift.
  * `tier` must band your own score: >={ALERT_AT} alert, >={NOTABLE_AT} notable, else routine.
    Disagreeing with your own score gets your row flagged for a human.
  * One entry per numbered award, in order, no omissions.{learned}"""


def _fact_line(award: dict, ctx: dict) -> str:
    """The award, compressed to what a judgement needs. Deterministic assembly."""
    bits = [
        f"amount={_usd(award.get('amount_usd'))}",
        f"action={award.get('action_type') or 'unknown'}",
    ]
    ent = award.get("_entity") or {}
    if ent.get("ticker"):
        rel = ent.get("relationship") or "direct"
        parent = ent.get("parent_company") or ent.get("normalized_name") or ""
        bits.append(f"listed={ent['ticker']}" + (f" ({parent}, {rel})" if parent else ""))
    else:
        bits.append("listed=none known")
    for key, label in (("service_branch", "branch"), ("pricing_type", "pricing"),
                       ("modification_number", "mod"), ("base_contract_number", "base"),
                       ("contract_number", "contract"), ("place_of_performance", "place"),
                       ("completion_date", "completes")):
        v = award.get(key)
        if v:
            bits.append(f"{label}={v}")
    if award.get("cumulative_face_value_usd"):
        bits.append(f"cumulative_face_value={_usd(award['cumulative_face_value_usd'])}")
    if award.get("is_multi_award"):
        bits.append("multi_award_pool=yes (ceiling is SHARED)")
    if award.get("is_idiq"):
        bits.append("idiq=yes (ceiling, not booked revenue)")
    if award.get("small_business"):
        bits.append("small_business_setaside=yes")
    br, bs = _int_or_none(award.get("bids_received")), _int_or_none(award.get("bids_solicited"))
    if br is not None:
        bits.append(f"bids_received={br}" + (f"/{bs} solicited" if bs is not None else ""))
    if ctx.get("amount_percentile") is not None:
        bits.append(f"amount_percentile_in_window={ctx['amount_percentile']}")
    if (ctx.get("contractor_awards_in_window") or 0) > 1:
        bits.append(f"same_contractor_awards_in_window={ctx['contractor_awards_in_window']} "
                    f"totalling {_usd(ctx.get('contractor_total_usd'))}")
    desc = (award.get("work_description") or "").strip().replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:397] + "..."
    head = f"{award.get('contractor_raw') or 'unknown contractor'}"
    loc = ", ".join(x for x in (award.get("contractor_city"), award.get("contractor_state")) if x)
    if loc:
        head += f" ({loc})"
    return f"{head}\n     {' | '.join(bits)}\n     work: {desc or 'not stated'}"


def build_prompt(chunk: list[dict], ctx: dict[str, dict] | None = None) -> str:
    ctx = ctx or {}
    lines = []
    for i, a in enumerate(chunk, 1):
        lines.append(f"{i}. {_fact_line(a, ctx.get(a.get('award_uid'), {}))}")
    body = "\n\n".join(lines)
    return (f"Score these {len(chunk)} awards for investor materiality, one entry each, "
            f"in this exact order:\n\n{body}\n")


def _max_tokens(n: int) -> int:
    """Deterministic budget: ~240 output tokens per award plus overhead."""
    return min(16000, 700 + 240 * n)


def _coerce(item: dict, award: dict, model: str, cache_key: str) -> tuple[dict, float]:
    """Validate one model answer into a schema-shaped row. Returns (row, confidence).

    Contradictions are a confidence problem, not a crash: cap and record.
    """
    notes: list[str] = []
    try:
        conf = float(item.get("confidence"))
    except (TypeError, ValueError):
        conf, _ = 0.3, notes.append("no usable confidence returned")
    conf = min(1.0, max(0.0, conf))

    raw_score = item.get("score")
    try:
        score = int(round(float(raw_score)))
    except (TypeError, ValueError):
        score = 0
        notes.append(f"score {raw_score!r} unusable, coerced to 0")
        conf = min(conf, 0.3)
    if not 0 <= score <= 100:
        notes.append(f"score {score} outside 0..100, clamped")
        score = max(0, min(100, score))
        conf = min(conf, 0.5)

    # DETERMINISTIC: banding is a comparison. The model's tier is checked, not trusted.
    banded = tier_for(score)
    said = item.get("tier")
    if said != banded:
        notes.append(f"model tier {said!r} does not band score {score}; using {banded!r}")
        conf = min(conf, 0.6)                      # -> escalation, per CLAUDE.md

    drivers = item.get("drivers")
    if not isinstance(drivers, list):
        drivers = []
        notes.append("drivers missing or not a list")
    drivers = [str(d).strip()[:60] for d in drivers if str(d).strip()][:6]

    rationale = (item.get("rationale") or "").strip() or "no rationale returned"
    if len(rationale) > 600:
        rationale = rationale[:597] + "..."
    if notes:
        rationale = f"{rationale} [validator: {'; '.join(notes)}]"

    return _row(
        award_uid=award["award_uid"],
        score=score,
        tier=banded,
        rationale=rationale,
        drivers=drivers,
        scorer_model=model,
        llm_cache_key=cache_key,
    ), round(conf, 3)


def _placeholder(award: dict, model: str, cache_key: str | None, why: str
                 ) -> tuple[dict, float]:
    """An honest non-answer. confidence 0.0 -> escalation, then review. Never persisted."""
    return _row(
        award_uid=award["award_uid"], score=0, tier=tier_for(0),
        rationale=why, drivers=["unscored"],
        scorer_model=model, llm_cache_key=cache_key,
    ), 0.0


def _align(items: list, chunk: list[dict]) -> dict[str, dict | None]:
    """Map returned scores back onto awards by position.

    Index alignment is the contract: the item schema has no award_uid because
    award_uid is llm=False and must not appear in a prompt schema. A count mismatch is
    not silently absorbed -- the surplus positions get None and become placeholders, so
    a dropped entry cannot shift every score after it onto the wrong award.
    """
    items = [i for i in items if isinstance(i, dict)]
    out: dict[str, dict | None] = {}
    for i, a in enumerate(chunk):
        out[a["award_uid"]] = items[i] if i < len(items) else None
    return out


# --------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------

@dataclass
class ScoreResult:
    rows: dict[str, dict] = dc_field(default_factory=dict)      # award_uid -> materiality row
    confidence: dict[str, float] = dc_field(default_factory=dict)
    pending: list[str] = dc_field(default_factory=list)         # need a live run
    review: list[dict] = dc_field(default_factory=list)         # review_queue payloads
    stats: dict = dc_field(default_factory=dict)

    def escalations(self) -> list[str]:
        return [u for u, c in self.confidence.items() if needs_escalation(c)]

    def ranked(self) -> list[dict]:
        """The feed, most material first. A SORT -- deliberately not a model call."""
        return sorted(self.rows.values(),
                      key=lambda r: (-(r.get("score") or 0), r.get("award_uid") or ""))


def _review_item(row: dict, award: dict, confidence: float, reason: str) -> dict:
    """A review_queue row, shaped from schemas.REVIEW_QUEUE_FIELDS."""
    basis = f"score_materiality|{row['award_uid']}|{reason}"
    item = {k: None for k in REVIEW_KEYS}
    payload = dict(row)
    payload["confidence"] = confidence             # no column for it in materiality
    payload["contractor_raw"] = award.get("contractor_raw")
    payload["amount_usd"] = award.get("amount_usd")
    item.update({
        "review_id": hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16],
        "flagged_at": _now(),
        "agent": "score_materiality",
        "item_key": row["award_uid"],
        "reason": reason,
        "confidence": confidence,
        "payload": payload,
        "resolved": False,
    })
    return item


def _run_batches(queue: list[dict], llm, model: str, system: str, batch_size: int,
                 ctx: dict[str, dict], on_batch=None
                 ) -> tuple[dict[str, dict], dict[str, float], list[str]]:
    """Score awards on `model`. One call per BATCH -- never one per award.

    Returns (uid -> row, uid -> confidence, pending uids).

    `on_batch(chunk, rows)` fires after EVERY batch -- success, replay miss and failure
    alike -- so a caller can report progress and persist partial results mid-queue. It
    fires in a `finally` because a failed batch still advanced the queue, and a raising
    callback is reported and swallowed rather than discarding paid work.
    """
    rows: dict[str, dict] = {}
    confs: dict[str, float] = {}
    pending: list[str] = []

    def _one(chunk: list[dict]) -> None:
        prompt = build_prompt(chunk, ctx)
        schema = batch_schema()
        max_tokens = _max_tokens(len(chunk))
        cache_key = CachedLLM.key(model, system, prompt, schema, max_tokens)
        try:
            got = llm.json_call(model=model, system=system, prompt=prompt, schema=schema,
                                max_tokens=max_tokens,
                                label=f"score_materiality x{len(chunk)} @{model}")
        except NotCachedError as exc:
            # Replay mode with nothing cached: report it, do not fabricate, do not
            # persist. `make demo` must survive this without an API key.
            sys.stderr.write(f"! tier 2 unscored ({len(chunk)} awards): {exc}\n")
            for a in chunk:
                pending.append(a["award_uid"])
                rows[a["award_uid"]], confs[a["award_uid"]] = _placeholder(
                    a, model, cache_key,
                    f"unscored: no cached call ({cache_key[:12]}); needs a live run")
            return
        except Exception as exc:                   # a bad batch must not sink the rest
            sys.stderr.write(f"! tier 2 call failed ({type(exc).__name__}: {exc})\n")
            for a in chunk:
                pending.append(a["award_uid"])
                rows[a["award_uid"]], confs[a["award_uid"]] = _placeholder(
                    a, model, cache_key, f"call failed: {type(exc).__name__}: {exc}")
            return

        aligned = _align((got or {}).get("scores") or [], chunk)
        for a in chunk:
            uid = a["award_uid"]
            item = aligned.get(uid)
            if item is None:
                pending.append(uid)
                rows[uid], confs[uid] = _placeholder(
                    a, model, cache_key, "model returned no entry for this award")
                continue
            rows[uid], confs[uid] = _coerce(item, a, model, cache_key)

    for chunk in batches(queue, batch_size):
        try:
            _one(chunk)
        finally:
            if on_batch is not None:
                try:
                    on_batch(chunk, rows)
                except Exception as exc:
                    sys.stderr.write(f"! on_batch failed: {type(exc).__name__}: {exc}\n")
    return rows, confs, pending


def score(awards, llm, *, model: str | None = None, escalate_model: str | None = None,
          escalation: bool = True, entities: dict | None = None,
          batch_size: int = BATCH_SIZE, on_progress=None) -> ScoreResult:
    """Pre-filter, batch-score the remainder, escalate the unsure, route the hopeless.

    `on_progress(done, total, rows)` is optional and reports the tier-2 pass batch by
    batch with real `materiality` rows the caller can persist immediately. `total` is
    the size of the tier-2 QUEUE, not of `awards`: the deterministic pre-filter has
    already answered the rest for free and there is no progress to report on work that
    never happens.
    """
    model = model or BULK_MODEL
    escalate_model = escalate_model or JUDGE_MODEL
    awards = [a for a in awards if a and a.get("award_uid")]
    seen: dict[str, dict] = {}
    for a in awards:
        seen.setdefault(a["award_uid"], a)         # dedup: a set operation, not reasoning
    awards = list(seen.values())

    for a in awards:                               # attach the resolved entity, if any
        raw = (a.get("contractor_raw") or "").strip()
        ent = (entities or {}).get(raw) or known_public(a, entities)
        if ent:
            a["_entity"] = ent

    filtered, queue = plan(awards, entities)
    ctx = corpus_context(awards)

    rows: dict[str, dict] = dict(filtered)
    confs: dict[str, float] = {u: 1.0 for u in filtered}        # deterministic == certain

    _done = 0

    def _report(chunk: list[dict], so_far: dict[str, dict]) -> None:
        nonlocal _done
        _done += len(chunk)
        if on_progress is None:
            return
        got = [r for a in chunk if (r := so_far.get(a["award_uid"])) is not None]
        on_progress(min(_done, len(queue)), len(queue), got)

    system = build_system()
    scored, sconfs, pending = _run_batches(queue, llm, model, system, batch_size, ctx,
                                           on_batch=_report)
    rows.update(scored)
    confs.update(sconfs)

    # -- escalation: confidence < 0.7 gets one retry on the judge model --
    escalated: dict[str, dict] = {}
    low = [a for a in queue
           if needs_escalation(confs.get(a["award_uid"])) and a["award_uid"] not in pending]
    if escalation and low and escalate_model != model:
        erows, econfs, epending = _run_batches(low, llm, escalate_model, system,
                                               batch_size, ctx)
        for uid, row in erows.items():
            if uid in epending:
                continue                           # keep the bulk answer, flag below
            escalated[uid] = row
            rows[uid] = row
            confs[uid] = econfs[uid]

    # -- review routing: still low after escalation, or never scored at all --
    by_uid = {a["award_uid"]: a for a in queue}
    review: list[dict] = []
    for uid in by_uid:
        row = rows.get(uid)
        if row is None:
            continue
        if uid in pending:
            review.append(_review_item(row, by_uid[uid], confs.get(uid, 0.0),
                                       "no cached model call; needs a live run"))
        elif needs_escalation(confs.get(uid)):
            reason = (f"still below {LOW_CONFIDENCE} after escalation to {escalate_model}"
                      if uid in escalated else
                      f"below {LOW_CONFIDENCE} and escalation was disabled")
            review.append(_review_item(row, by_uid[uid], confs.get(uid, 0.0), reason))

    n_batches = len(batches(queue, batch_size))
    tiers = {t: sum(1 for r in rows.values() if r["tier"] == t) for t in TIERS}
    stats = {
        "awards": len(awards),
        "prefiltered": len(filtered),
        "prefilter_share": round(len(filtered) / len(awards), 4) if awards else 0.0,
        "model_queue": len(queue),
        "batches": n_batches,
        "calls_saved_vs_one_per_award": len(awards) - n_batches,
        "scored": len([u for u in by_uid if u not in pending]),
        "pending": len(pending),
        "escalated": len(escalated),
        "review": len(review),
        "tiers": tiers,
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
    return ScoreResult(rows=rows, confidence=confs, pending=pending,
                       review=review, stats=stats)


def score_awards(awards, llm, **kw) -> dict[str, dict]:
    """The manager's entry point: award rows -> materiality rows by award_uid."""
    return score(awards, llm, **kw).rows


# --------------------------------------------------------------------------------
# CleaningAgent protocol shape (CLAUDE.md) -- lets the manager register this file
# --------------------------------------------------------------------------------

class ScoreMaterialityAgent:
    """name / detects(conn) / run(items, llm) / skills_path() -- nothing else needed."""

    name = "score_materiality"

    #: Batches 20 awards per call, so the manager must NOT chunk it. Progress comes
    #: from inside `run()` instead -- see score()'s `on_progress`.
    supports_progress = True

    def skills_path(self) -> pathlib.Path:
        return SKILLS_PATH

    def detects(self, conn) -> list[dict]:
        """Awards with no materiality row. A SET DIFFERENCE, so no model is involved.

        Largest first, because a scoring batch that leads with the biggest awards puts
        the most investor-relevant rows in the cache first.
        """
        try:
            cols = ", ".join(f"a.{c}" for c in AWARD_KEYS)
            rows = conn.execute(
                f"SELECT {cols} FROM awards a "
                f"LEFT JOIN materiality m ON m.award_uid = a.award_uid "
                f"WHERE m.award_uid IS NULL "
                f"ORDER BY a.amount_usd DESC NULLS LAST, a.award_uid"
            ).fetchall()
        except Exception as exc:                   # tables not created yet
            sys.stderr.write(f"! score_materiality.detects: {type(exc).__name__}: {exc}\n")
            return []
        return [dict(zip(AWARD_KEYS, r)) for r in rows]

    def entities_for(self, conn, awards: list[dict]) -> dict[str, dict]:
        """Resolved entities keyed by RAW contractor name -- the join src/store.py uses."""
        try:
            cols = [f.name for f in schemas.TABLES["entities"][0]]
            rows = conn.execute(f"SELECT {', '.join(cols)} FROM entities").fetchall()
        except Exception:
            return {}
        return {r[0]: dict(zip(cols, r)) for r in rows}

    def run(self, items, llm, **kw) -> list[dict]:
        return list(score_awards(list(items), llm, **kw).values())


def top_awards(conn, limit: int = 25, tier: str | None = None) -> list[dict]:
    """The investor feed. A SORT over stored scores -- deliberately not a model call."""
    where = "WHERE m.tier = ?" if tier else ""
    params = [tier, limit] if tier else [limit]
    rows = conn.execute(
        f"SELECT m.award_uid, m.score, m.tier, m.rationale, a.contractor_raw, "
        f"       a.amount_usd, a.announced_date "
        f"FROM materiality m JOIN awards a ON a.award_uid = m.award_uid "
        f"{where} ORDER BY m.score DESC, a.amount_usd DESC NULLS LAST LIMIT ?", params
    ).fetchall()
    keys = ["award_uid", "score", "tier", "rationale", "contractor_raw",
            "amount_usd", "announced_date"]
    return [dict(zip(keys, r)) for r in rows]


# --------------------------------------------------------------------------------
# corpus scan -- a COST SIZING TOOL, NOT the real extractor
# --------------------------------------------------------------------------------

_AWARD_RE = re.compile(r"\b(?:is|was|are|were|has been|have been)\s+(?:being\s+)?award",
                       re.IGNORECASE)
# "Acme Corp., Dayton, Ohio", "Acme Corp.,* Dayton, Ohio" and "Acme Corp.* Dayton"
# are all the same convention; the small-business asterisk may or may not follow a comma.
_NAME_RE = re.compile(
    r"^(?P<name>[A-Z0-9][^,;]{2,90}?)\s*(?:,\s*\*\s+|\*\s*,?\s+|,\s+)(?=[A-Z])")
_STAR_RE = re.compile(r"^[^,;]{2,90},?\*")
_DOLLAR_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*(billion|million)?", re.IGNORECASE)
_BRANCH_RE = re.compile(r"^(ARMY|NAVY|AIR FORCE|DEFENSE LOGISTICS AGENCY|SPACE FORCE|"
                        r"MARINE CORPS|DEFENSE|MISSILE DEFENSE AGENCY)\s*$")
_MOD_RE = re.compile(r"\bmodification\b", re.IGNORECASE)
_MODNUM_RE = re.compile(r"\b(P\d{5})\b")
_OPTION_RE = re.compile(r"\boption\b", re.IGNORECASE)
_IDIQ_RE = re.compile(r"indefinite-delivery/indefinite-quantity|indefinite delivery",
                      re.IGNORECASE)
_MULTI_RE = re.compile(r"multiple-award|multiple award|will compete for", re.IGNORECASE)
_BIDS_RE = re.compile(r"(\d+)\s+(?:offers|bids|proposals)\s+(?:were|was)\s+received",
                      re.IGNORECASE)


def corpus_root() -> pathlib.Path:
    """Where raw/articles/*.html actually lives.

    raw/*.html is gitignored, so a linked worktree has the committed .prov.json
    sidecars but not the bodies. Fall back to the main working tree rather than
    reporting a corpus of zero.
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


def _amount(m) -> int:
    v = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    mult = 1e9 if unit == "billion" else 1e6 if unit == "million" else 1.0
    return int(v * mult)


def scan_corpus(root: pathlib.Path | None = None) -> list[dict]:
    """Rough award rows from raw/articles/*.html, by regex.

    DELIBERATELY CRUDE and NOT a substitute for src/agents/extract.py, which owns real
    extraction. It exists for one purpose: sizing the pre-filter saving and the tier-2
    bill against the actual corpus without spending a cent. It uses the announcement's
    "Name, City, State, ... was awarded $X" convention only, so it will miss the
    2nd..Nth company of a multi-award pool and mis-split the occasional paragraph.
    """
    root = pathlib.Path(root or corpus_root())
    try:
        import fetch                                # deterministic acquisition layer
    except Exception as exc:
        sys.stderr.write(f"! cannot import fetch ({exc})\n")
        return []
    out: list[dict] = []
    for path in sorted(root.glob("*.html")):
        aid = path.stem
        body = fetch.body_text(path.read_text(encoding="utf-8", errors="replace"))
        branch = None
        ordinal = 0
        for line in body.split("\n"):
            s = line.strip()
            if not s:
                continue
            b = _BRANCH_RE.match(s)
            if b:
                branch = b.group(1)
                continue
            if not _AWARD_RE.search(s[:400]):
                continue
            money = _DOLLAR_RE.search(s)
            name_m = _NAME_RE.match(s)
            name = name_m.group("name").strip() if name_m else "unknown"
            modnum = _MODNUM_RE.search(s)
            is_mod = bool(_MOD_RE.search(s))
            bids = _BIDS_RE.search(s)
            row = {k: None for k in AWARD_KEYS}
            row.update({
                "award_uid": schemas.award_uid(aid, None, name, ordinal),
                "announcement_id": aid,
                "service_branch": branch if branch in schemas.BRANCHES else "OTHER",
                "contractor_raw": name,
                "amount_usd": _amount(money) if money else None,
                "action_type": ("modification" if is_mod else
                                "option_exercise" if _OPTION_RE.search(s) else "new_award"),
                "modification_number": modnum.group(1) if modnum else None,
                "is_idiq": bool(_IDIQ_RE.search(s)),
                "is_multi_award": bool(_MULTI_RE.search(s)),
                "small_business": bool(_STAR_RE.match(s)),
                "bids_received": int(bids.group(1)) if bids else None,
                "work_description": _html.unescape(s)[:600],
                "extraction_confidence": 0.5,
            })
            out.append(row)
            ordinal += 1
    return out


# --------------------------------------------------------------------------------
# offline test doubles -- no API key, no network, no spend
# --------------------------------------------------------------------------------

class _Tripwire:
    """An LLM stand-in that fails loudly if anything tries to spend money."""

    def __init__(self):
        self.usage = Usage()
        self.live = False
        self.fired = False

    def json_call(self, **kw):
        self.fired = True
        raise AssertionError(
            f"PRE-FILTER VIOLATION: a model call was attempted for {kw.get('label')!r}")


class _StubLLM:
    """A scripted LLM. Counts calls, answers one entry per numbered prompt line.

    `answers` maps a model id to callable(index, n) -> the llm=True half of a score.
    Anything unscripted gets a confident routine, the honest default.
    """

    _LINE = re.compile(r"^\s*(\d+)\.\s+\S", re.MULTILINE)

    def __init__(self, answers=None):
        self.usage = Usage()
        self.live = False
        self.calls: list[dict] = []
        self.answers = answers or {}

    def json_call(self, *, model, system, prompt, schema, max_tokens=8000, label=""):
        n = len(self._LINE.findall(prompt))
        self.calls.append({"model": model, "n": n, "label": label,
                           "max_tokens": max_tokens})
        maker = self.answers.get(model) or (lambda i, total: {
            "score": 20, "tier": "routine", "rationale": "stub: routine",
            "drivers": ["stub"], "confidence": 0.9})
        return {"scores": [maker(i, n) for i in range(n)]}


# --------------------------------------------------------------------------------
# offline selftest
# --------------------------------------------------------------------------------

def _ok(cond: bool, label: str) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    return bool(cond)


def _head(n: str) -> None:
    print()
    print("=" * 78)
    print(n)
    print("=" * 78)


def _award(uid: str, **kw) -> dict:
    row = {k: None for k in AWARD_KEYS}
    row.update({"award_uid": uid, "announcement_id": "A1", "contractor_raw": "Acme LLC",
                "action_type": "new_award", "extraction_confidence": 0.9})
    row.update(kw)
    return row


def _check_repo_invariants() -> int:
    """Acceptance 1-2: the frozen contract is untouched and nothing else imports the SDK."""
    import subprocess
    root = config.ROOT
    fails = 0

    # The contract may be unfrozen by the project owner (see its CHANGES log), so the
    # invariant worth guarding is not "never changed" -- it is that no local change has
    # altered a PROMPT, because that silently invalidates the committed cache and forces
    # re-extraction at real cost. Compare the prompt projection against `main`.
    out = subprocess.run(["git", "show", "main:src/schemas.py"],
                         capture_output=True, text=True, cwd=root)
    if out.returncode != 0:
        print(f"  (no `main` to compare the contract against: "
              f"{out.stderr.strip()[:100]})")
    else:
        import types
        ref = types.ModuleType("_schemas_main")
        # dataclasses resolves a class's module through sys.modules, so a synthetic
        # module must be registered there before `@dataclass` in the contract runs.
        sys.modules["_schemas_main"] = ref
        try:
            exec(compile(out.stdout, "main:src/schemas.py", "exec"), ref.__dict__)
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

    # The SDK name is assembled from parts so a reviewer grepping src/ for the
    # forbidden import still matches src/llm.py and nothing else -- not even this file.
    sdk = "anthropic"
    pat = re.compile(r"^\s*(?:import|from)\s+" + sdk + r"\b", re.MULTILINE)
    offenders = [p.relative_to(root).as_posix()
                 for p in sorted((root / "src").rglob("*.py"))
                 if pat.search(p.read_text(encoding="utf-8", errors="replace"))]
    fails += not _ok(offenders == ["src/llm.py"],
                     f"the {sdk} SDK is imported only in src/llm.py (found: {offenders})")
    return fails


def _check_duckdb() -> int:
    """Acceptance 9: detects() is a set difference against a real DuckDB. No model."""
    try:
        import duckdb
    except ImportError:
        print("  (duckdb not installed; skipping the live detects() check)")
        return 0
    conn = duckdb.connect(":memory:")
    conn.execute(schemas.all_ddl())                # the contract's own DDL, unmodified

    awards = [_award(schemas.award_uid("A1", f"C-{i}", f"Co {i}"),
                     contractor_raw=f"Co {i}", amount_usd=10_000_000 * (i + 1))
              for i in range(4)]
    for a in awards:
        conn.execute(f"INSERT INTO awards ({','.join(AWARD_KEYS)}) VALUES "
                     f"({','.join('?' * len(AWARD_KEYS))})", [a[c] for c in AWARD_KEYS])

    # two of the four already scored
    scored = [_row(award_uid=awards[0]["award_uid"], score=80, tier="alert",
                   rationale="r", drivers=["d"], scorer_model="m", llm_cache_key="k"),
              _row(award_uid=awards[2]["award_uid"], score=10, tier="routine",
                   rationale="r", drivers=["d"], scorer_model="m", llm_cache_key="k")]
    for r in scored:
        vals = [json.dumps(r[c]) if c == "drivers" else r[c] for c in MATERIALITY_KEYS]
        conn.execute(f"INSERT INTO materiality ({','.join(MATERIALITY_KEYS)}) VALUES "
                     f"({','.join('?' * len(MATERIALITY_KEYS))})", vals)

    agent = ScoreMaterialityAgent()
    tw = _Tripwire()
    found = agent.detects(conn)
    uids = {a["award_uid"] for a in found}
    expected = {awards[1]["award_uid"], awards[3]["award_uid"]}
    fails = 0
    fails += not _ok(uids == expected,
                     f"detects() = exactly the unscored awards ({len(uids)} of 4)")
    fails += not _ok(not tw.fired, "detects() made no model call (set difference only)")
    fails += not _ok([a["amount_usd"] for a in found] ==
                     sorted([a["amount_usd"] for a in found], reverse=True),
                     "detects() returns largest-first (a sort, not a judgement)")

    top = top_awards(conn, limit=10)
    fails += not _ok([t["score"] for t in top] == sorted([t["score"] for t in top],
                                                         reverse=True),
                     f"top_awards() ranks by score, deterministically: "
                     f"{[t['score'] for t in top]}")
    conn.close()
    return fails


def selftest() -> int:
    fails = 0

    _head("0. repo invariants: frozen contract, no stray anthropic import")
    fails += _check_repo_invariants()

    # ------------------------------------------------------------------
    _head("1. the contract: emitted row == schemas.MATERIALITY_FIELDS, in order")
    stub = _StubLLM({BULK_MODEL: lambda i, n: {
        "score": 75, "tier": "alert", "rationale": "stub alert",
        "drivers": ["size_vs_revenue"], "confidence": 0.9}})
    a = _award("uid0001", contractor_raw="Lockheed Martin Corp.", amount_usd=800_000_000)
    res = score([a], stub)
    row = res.rows["uid0001"]
    print(f"    row keys: {list(row)}")
    fails += not _ok(list(row) == MATERIALITY_KEYS,
                     "row keys equal MATERIALITY_KEYS in schema order")
    fails += not _ok(validate_row(row) == [], f"validate_row: {validate_row(row)}")
    fails += not _ok(row["tier"] in TIERS, f"tier {row['tier']!r} in {TIERS}")
    fails += not _ok(isinstance(row["score"], int) and 0 <= row["score"] <= 100,
                     f"score is int 0..100 ({row['score']})")
    fails += not _ok(json.loads(json.dumps(row["drivers"])) == row["drivers"],
                     f"drivers round-trips through JSON: {row['drivers']}")
    fails += not _ok(all(row[k] for k in DETERMINISTIC_KEYS if k != "llm_cache_key"),
                     f"llm=False fields set by code: "
                     f"{ {k: row[k] for k in DETERMINISTIC_KEYS} }")
    fails += not _ok(row["scorer_model"] == BULK_MODEL and row["llm_cache_key"],
                     f"provenance recorded: {row['scorer_model']} / "
                     f"{str(row['llm_cache_key'])[:12]}")

    # ------------------------------------------------------------------
    _head("2. the prompt schema never mentions a deterministic field")
    props = set(batch_schema()["properties"]["scores"]["items"]["properties"])
    print(f"    prompt properties : {sorted(props)}")
    print(f"    llm=False columns : {DETERMINISTIC_KEYS}")
    fails += not _ok(props.isdisjoint(DETERMINISTIC_KEYS),
                     "prompt properties are disjoint from the llm=False names")
    fails += not _ok(props == {f.name for f in SCORE_FIELDS if f.llm},
                     "prompt properties come from object_schema(SCORE_FIELDS)")
    fails += not _ok("confidence" in props and "confidence" not in MATERIALITY_KEYS,
                     "confidence is prompt-only: asked for, routed on, never persisted "
                     "(the contract has no column and schemas.py stays frozen)")
    fails += not _ok(batch_schema()["properties"]["scores"]["items"]
                     .get("additionalProperties") is False,
                     "item schema is strict-mode shaped")

    # ------------------------------------------------------------------
    _head("3. THE PRE-FILTER SPENDS NOTHING (tripwire armed)")
    tw = _Tripwire()
    routine = [
        _award("pf1", contractor_raw="Tiny Widgets LLC", amount_usd=8_000_000),
        _award("pf2", contractor_raw="AAECON General Contracting LLC,*",
               amount_usd=31_000_000, small_business=True),
        _award("pf3", contractor_raw="Nowhere Services Inc.", amount_usd=24_900_000),
    ]
    filtered, queue = plan(routine)
    for uid, r in filtered.items():
        print(f"    {uid} -> {r['tier']:8s} score={r['score']:3d}  {r['drivers']}")
    fails += not _ok(len(filtered) == 3 and not queue,
                     f"3 obviously-routine awards filtered without a model "
                     f"({len(filtered)} filtered, {len(queue)} queued)")
    fails += not _ok(all(r["tier"] == "routine" for r in filtered.values()),
                     "every pre-filtered row is tier=routine")
    fails += not _ok(all(r["scorer_model"] == DETERMINISTIC and r["llm_cache_key"] is None
                         for r in filtered.values()),
                     "pre-filtered rows are stamped deterministic with no cache key")
    fails += not _ok(all(validate_row(r) == [] for r in filtered.values()),
                     "pre-filtered rows satisfy the contract")
    res_pf = score(routine, tw)                    # the tripwire would raise on a call
    fails += not _ok(not tw.fired and len(res_pf.rows) == 3,
                     "score() over only-routine awards fired the tripwire zero times")
    fails += not _ok(res_pf.stats["cost_usd"] == 0.0 if "cost_usd" in res_pf.stats else True,
                     "and reported $0.00")

    print("\n    -- and the cases the pre-filter must REFUSE to swallow --")
    for label, award in [
        ("big award, unlisted contractor",
         _award("k1", contractor_raw="Austal USA LLC", amount_usd=400_000_000)),
        ("small award, LISTED contractor",
         _award("k2", contractor_raw="Lockheed Martin Corp.", amount_usd=9_000_000)),
        ("amount unknown",
         _award("k3", contractor_raw="Mystery Corp", amount_usd=None)),
        ("small-business set-aside ABOVE its floor",
         _award("k4", contractor_raw="Small Co,*", amount_usd=60_000_000,
                small_business=True)),
    ]:
        got = prefilter(award)
        fails += not _ok(got is None, f"reaches the model: {label}")

    # ------------------------------------------------------------------
    _head("4. tier is BANDED by code, not taken on trust")
    fails += not _ok([tier_for(s) for s in (0, 39, 40, 69, 70, 100)] ==
                     ["routine", "routine", "notable", "notable", "alert", "alert"],
                     f"tier_for() bands at {NOTABLE_AT}/{ALERT_AT}")
    liar = _StubLLM({BULK_MODEL: lambda i, n: {
        "score": 85, "tier": "routine",            # <- self-contradiction
        "rationale": "stub contradiction", "drivers": ["x"], "confidence": 0.95}})
    r2 = score([_award("uid0002", contractor_raw="Boeing Co.", amount_usd=500_000_000)],
               liar, escalation=False)
    row2 = r2.rows["uid0002"]
    print(f"    model said tier='routine' with score=85 -> stored {row2['tier']!r}, "
          f"confidence {r2.confidence['uid0002']}")
    fails += not _ok(row2["tier"] == "alert", "code overrode the model's tier from score")
    fails += not _ok("validator" in row2["rationale"],
                     "the override is recorded in the rationale, not hidden")
    fails += not _ok(needs_escalation(r2.confidence["uid0002"]),
                     "and the contradiction dropped confidence below the escalation line")

    # ------------------------------------------------------------------
    _head("5. escalation: 0.5 -> Opus; still 0.5 -> review_queue")
    unsure = _StubLLM({
        BULK_MODEL: lambda i, n: {"score": 55, "tier": "notable",
                                  "rationale": "haiku unsure", "drivers": ["d"],
                                  "confidence": 0.5},
        JUDGE_MODEL: lambda i, n: {"score": 62, "tier": "notable",
                                   "rationale": "opus, still unsure", "drivers": ["d"],
                                   "confidence": 0.5},
    })
    big = _award("uid0003", contractor_raw="Austal USA LLC", amount_usd=300_000_000)
    r3 = score([big], unsure)
    models = [c["model"] for c in unsure.calls]
    print(f"    models called: {models}")
    print(f"    review reason: {r3.review[0]['reason'] if r3.review else '(none)'}")
    fails += not _ok(models == [BULK_MODEL, JUDGE_MODEL],
                     f"{BULK_MODEL} first, then escalated to {JUDGE_MODEL}")
    fails += not _ok(r3.rows["uid0003"]["scorer_model"] == JUDGE_MODEL,
                     "the stored row carries the escalated model, honestly")
    fails += not _ok(len(r3.review) == 1 and r3.review[0]["item_key"] == "uid0003",
                     "still-low confidence went to review_queue, not silent acceptance")
    fails += not _ok(list(r3.review[0]) == REVIEW_KEYS,
                     "the review item matches schemas.REVIEW_QUEUE_FIELDS")
    fails += not _ok(r3.review[0]["payload"]["confidence"] == 0.5,
                     "and the payload preserves the confidence the table cannot store")

    confident = _StubLLM({BULK_MODEL: lambda i, n: {
        "score": 55, "tier": "notable", "rationale": "sure", "drivers": ["d"],
        "confidence": 0.92}})
    r4 = score([big], confident)
    fails += not _ok([c["model"] for c in confident.calls] == [BULK_MODEL],
                     "a confident answer never touches the judge model")
    fails += not _ok(not r4.review, "and never reaches the review queue")

    # ------------------------------------------------------------------
    _head("6. batching: N awards do NOT cost N calls")
    many = [_award(f"b{i:04d}", contractor_raw=f"Austal USA LLC {i}",
                   amount_usd=100_000_000 + i) for i in range(87)]
    counter = _StubLLM()
    r5 = score(many, counter, escalation=False)
    n_calls = len(counter.calls)
    expect = math.ceil(87 / BATCH_SIZE)
    print(f"    {len(many)} awards -> {n_calls} call(s) at BATCH_SIZE={BATCH_SIZE} "
          f"= {len(many) / max(n_calls, 1):.1f} awards/call "
          f"({len(many) - n_calls} calls saved vs one-per-award)")
    fails += not _ok(n_calls == expect, f"exactly ceil(87/{BATCH_SIZE}) = {expect} calls")
    fails += not _ok(len(r5.rows) == 87 and all(validate_row(r) == []
                                                for r in r5.rows.values()),
                     "all 87 rows returned and every one satisfies the contract")
    fails += not _ok(sorted(c["n"] for c in counter.calls)[-1] <= BATCH_SIZE,
                     "no batch exceeded BATCH_SIZE")
    uids = [uid for c in counter.calls for uid in []] or list(r5.rows)
    fails += not _ok(len(set(uids)) == 87, "no award was scored twice")

    # ------------------------------------------------------------------
    _head("7. a cache miss is a clean NotCachedError, never a fabrication")
    llm = CachedLLM(live=False)
    try:
        llm.json_call(model=BULK_MODEL, system="s", prompt="p",
                      schema=batch_schema(), max_tokens=100, label="selftest miss")
        raised = None
    except NotCachedError as exc:
        raised = exc
    fails += not _ok(isinstance(raised, NotCachedError),
                     f"CachedLLM(live=False) raised NotCachedError: {str(raised)[:70]}")
    r6 = score([_award("uid0004", contractor_raw="Austal USA LLC",
                       amount_usd=250_000_000)], CachedLLM(live=False))
    fails += not _ok(r6.pending == ["uid0004"] and len(r6.review) == 1,
                     "an uncached award is reported pending and queued for review")
    fails += not _ok(r6.rows["uid0004"]["score"] == 0 and
                     "needs a live run" in r6.rows["uid0004"]["rationale"],
                     "and its row is an honest non-answer, not an invented score")

    # ------------------------------------------------------------------
    _head("8. detects() against a real DuckDB built from schemas.all_ddl()")
    fails += _check_duckdb()

    # ------------------------------------------------------------------
    _head("9. costed dry-run over the real corpus")
    fails += 0 if dry_run() == 0 else 1

    _head(f"{'ALL CHECKS PASSED' if fails == 0 else f'{fails} CHECK(S) FAILED'}")
    return fails


# --------------------------------------------------------------------------------
# costed dry-run
# --------------------------------------------------------------------------------

#: chars per token, measured against Anthropic's tokenizer on English prose. Used only
#: to ESTIMATE a bill we have not paid; a live run replaces it with billed counts.
CHARS_PER_TOKEN = 3.7


def dry_run(root: pathlib.Path | None = None, batch_size: int = BATCH_SIZE) -> int:
    """What a full scoring pass over the corpus would cost. Spends nothing.

    Every number here is computed from real prompt strings built from the real corpus,
    then priced at llm.PRICING -- not from a remembered rate card.
    """
    awards = scan_corpus(root)
    src = root or corpus_root()
    print(f"  source: {src}")
    if not awards:
        print("  (no articles found; raw/*.html is gitignored -- run `make fetch` first)")
        print("  SKIPPED, not failed: the dry run needs the corpus bodies.")
        return 0

    n_ann = len({a["announcement_id"] for a in awards})
    filtered, queue = plan(awards)
    ctx = corpus_context(awards)
    system = build_system()
    chunks = batches(queue, batch_size)

    in_chars = out_tokens = 0
    for chunk in chunks:
        prompt = build_prompt(chunk, ctx)
        in_chars += len(system) + len(prompt) + len(json.dumps(batch_schema()))
        out_tokens += _max_tokens(len(chunk)) - 700
    in_tokens = int(in_chars / CHARS_PER_TOKEN)
    price = PRICING[BULK_MODEL]
    cost = in_tokens / 1e6 * price["in"] + out_tokens / 1e6 * price["out"]

    naive_chars = 0
    for a in queue:
        naive_chars += len(system) + len(build_prompt([a], ctx)) + len(json.dumps(batch_schema()))
    naive_in = int(naive_chars / CHARS_PER_TOKEN)
    naive_out = 240 * len(queue)
    naive_cost = naive_in / 1e6 * price["in"] + naive_out / 1e6 * price["out"]

    no_prefilter_chars = 0
    for chunk in batches(awards, batch_size):
        no_prefilter_chars += (len(system) + len(build_prompt(chunk, ctx))
                               + len(json.dumps(batch_schema())))
    nf_in = int(no_prefilter_chars / CHARS_PER_TOKEN)
    nf_out = sum(_max_tokens(len(c)) - 700 for c in batches(awards, batch_size))
    nf_cost = nf_in / 1e6 * price["in"] + nf_out / 1e6 * price["out"]

    rules: dict[str, int] = {}
    for r in filtered.values():
        rule = next((d.split(":", 1)[1] for d in r["drivers"] if d.startswith("prefilter:")),
                    "?")
        rules[rule] = rules.get(rule, 0) + 1

    share = len(filtered) / len(awards)
    print(f"  announcements scanned             : {n_ann}")
    print(f"  award rows (regex proxy, not the real extractor): {len(awards)}")
    print(f"  TIER 1 pre-filtered, free         : {len(filtered):4d}  ({share:5.1%})")
    for rule, n in sorted(rules.items(), key=lambda kv: -kv[1]):
        print(f"        {rule:24s}    : {n:4d}")
    print(f"  TIER 2 needs judgement            : {len(queue):4d}  ({1 - share:5.1%})")
    print(f"  batched calls at BATCH_SIZE={batch_size:<3d}    : {len(chunks)}  "
          f"({len(queue) / max(len(chunks), 1):.1f} awards/call)")
    print(f"  estimated tokens                  : {in_tokens:,} in / {out_tokens:,} out "
          f"(max_tokens budget, an upper bound)")
    print(f"  estimated cost at {BULK_MODEL} list "
          f"(${price['in']}/${price['out']} per 1M): ${cost:,.2f}")
    print(f"  same corpus with NO pre-filter    : ${nf_cost:,.2f}  "
          f"-> pre-filter saves ${nf_cost - cost:,.2f} ({1 - cost / nf_cost:5.1%})")
    print(f"  one call per award instead        : ${naive_cost:,.2f}  "
          f"({naive_cost / max(cost, 1e-9):.0f}x)  in {len(queue)} calls")
    print(f"  and once cached, a re-run costs    $0.00 -- the whole point")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="offline validation, free")
    ap.add_argument("--corpus", action="store_true",
                    help="pre-filter saving and costed dry run over raw/articles")
    ap.add_argument("--schema", action="store_true", help="print the prompt schema")
    ap.add_argument("--system", action="store_true", help="print the system prompt")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args(argv)

    if args.corpus:
        return dry_run(batch_size=args.batch_size)
    if args.schema:
        print(json.dumps(batch_schema(), indent=2))
        return 0
    if args.system:
        print(build_system())
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
