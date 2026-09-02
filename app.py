"""DoD Contract Terminal -- investor-facing UI over Department of War contract awards.

WHAT THIS FILE IS
    The read side of the system, and nothing else. It opens exported Parquet (or CSV)
    from ``data/`` and renders five views:

        1. Change feed        -- what happened, ranked by materiality
        2. Company view       -- one ticker's awards, totals and trend
        3. Review queue       -- what the agents were not sure about
        4. Agent activity     -- what ran, on which model, cache hits, tokens, cost
        5. Provenance         -- source URL, sha256, and the exact model call behind a row

WHAT THIS FILE DELIBERATELY IS NOT
    * It makes **no model calls**. It does not import ``anthropic`` or ``src/llm.py``.
      Ranking is a sort, filtering is a predicate, totals are a groupby -- per CLAUDE.md,
      a model call here would be a bug.
    * It does not import ``src/store.py``. The UI reads exported files so it never
      blocks on the pipeline, and so it stays usable while the pipeline is mid-build.
    * It never writes to ``data/``.

COLUMN NAMES
    Every column rendered is taken from ``src/schemas.py`` -- the frozen contract. The
    demo frames are built by iterating the contract's field lists, and ``_need()``
    asserts at startup that every name this file references actually exists there. If
    the contract changes shape, this app fails loudly on launch rather than rendering a
    silently wrong column.

ZERO-DATA BEHAVIOUR
    With no export present the app still launches and every view is navigable: each one
    shows an empty state naming the exact paths that were searched. Synthetic demo data
    is available behind an explicit opt-in in the sidebar and is labelled as synthetic
    everywhere it is shown.
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    st.set_page_config(page_title="DoD Contract Terminal", layout="wide",
                       initial_sidebar_state="expanded")
except Exception:
    pass

# Streamlit reserves a lot of vertical space above the first element and between
# blocks. On a data-dense terminal that pushes the actual content below the fold,
# so trim it. Cosmetic only -- no behaviour depends on this.
try:
    st.markdown(
        """
        <style>
          .block-container {padding-top: 2.2rem; padding-bottom: 2rem;}
          h1 {font-size: 1.45rem; margin: 0 0 .15rem 0;}
          h2, h3 {margin-top: .4rem;}
          [data-testid="stMetricValue"] {font-size: 1.25rem;}
          [data-testid="stMetricLabel"] {font-size: .78rem; opacity: .75;}
          hr {margin: .7rem 0;}
        </style>
        """,
        unsafe_allow_html=True)
except Exception:
    pass

try:
    import schemas  # the frozen contract -- read only, never edited from here
except Exception as _exc:  # pragma: no cover -- a missing contract is a build error
    st.error(
        "Could not import the frozen data contract `src/schemas.py`.\n\n"
        f"Looked in `{SRC}`.\n\nError: `{_exc}`"
    )
    st.stop()


# ---------------------------------------------------------------------------------
# The contract, projected into the handful of things a UI needs from it
# ---------------------------------------------------------------------------------

TABLE_COLS: dict[str, list[str]] = {
    table: [f.name for f in fields] for table, (fields, _pk) in schemas.TABLES.items()
}


def _field(table: str, name: str):
    for f in schemas.TABLES[table][0]:
        if f.name == name:
            return f
    raise KeyError(f"{name!r} is not a column of {table!r}")


def _need(table: str, *names: str) -> tuple[str, ...]:
    """Assert these columns exist in the contract. Checked, not assumed."""
    known = set(TABLE_COLS.get(table, ()))
    missing = [n for n in names if n not in known]
    if missing:
        raise KeyError(
            f"app.py references columns that {table!r} does not have in "
            f"schemas.py v{schemas.SCHEMA_VERSION}: {missing}. "
            "The contract is frozen -- fix app.py, not schemas.py."
        )
    return names


# Every column this file touches, declared up front so contract drift fails on launch.
_need("awards", "award_uid", "announcement_id", "announced_date", "service_branch",
      "contractor_raw", "contractor_city", "contractor_state", "amount_usd",
      "action_type", "contract_number", "base_contract_number", "modification_number",
      "cumulative_face_value_usd", "pricing_type", "is_idiq", "is_multi_award",
      "work_description", "place_of_performance", "completion_date",
      "contracting_activity", "bids_solicited", "bids_received", "small_business",
      "extraction_confidence", "extraction_notes", "extracted_at", "extractor_model",
      "llm_cache_key", "skills_version")
_need("announcements", "announcement_id", "announced_date", "title", "url",
      "fetched_at", "http_status", "sha256", "n_bytes", "body_chars",
      "extraction_status")
_need("entities", "contractor_raw", "normalized_name", "ticker", "parent_company",
      "relationship", "is_public", "confidence", "reasoning", "resolved_at",
      "resolver_model", "llm_cache_key", "skills_version")
_need("materiality", "award_uid", "score", "tier", "rationale", "drivers",
      "scored_at", "scorer_model", "llm_cache_key", "skills_version")
_need("agent_runs", "run_id", "tick_id", "agent", "item_key", "model", "escalated",
      "cache_hit", "input_tokens", "output_tokens", "cost_usd", "confidence",
      "outcome", "error", "skills_version", "started_at", "duration_ms")
_need("review_queue", "review_id", "flagged_at", "agent", "item_key", "reason",
      "confidence", "payload", "resolved")

TIER_ORDER: list[str] = [
    v for v in _field("materiality", "tier").json.get("enum", []) if v
]
if not TIER_ORDER:  # the contract is the only source of tiers; never a second copy
    raise RuntimeError("materiality.tier carries no enum in src/schemas.py")
TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

DATE_COLS = {t: [f.name for f in fields if f.sql == "DATE"]
             for t, (fields, _pk) in schemas.TABLES.items()}
TS_COLS = {t: [f.name for f in fields if f.sql == "TIMESTAMP"]
           for t, (fields, _pk) in schemas.TABLES.items()}

UI_TABLES = ["awards", "announcements", "entities", "materiality", "agent_runs",
             "review_queue", "changes"]

# Confidence at or below which the manager escalates, then flags for review (CLAUDE.md).
CONFIDENCE_FLOOR = 0.7


# ---------------------------------------------------------------------------------
# Formatting -- a terminal, not a marketing page
# ---------------------------------------------------------------------------------

DASH = "—"


def _isna(v) -> bool:
    if v is None:
        return True
    if isinstance(v, (list, tuple, dict, set)):
        return False
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def usd(v) -> str:
    """712480000 -> '$712.5M'. Readable at a glance; exact on the drill-down."""
    if _isna(v):
        return DASH
    try:
        n = float(v)
    except (TypeError, ValueError):
        return DASH
    sign = "-" if n < 0 else ""
    n = abs(n)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{sign}${n / div:,.1f}{suf}"
    return f"{sign}${n:,.0f}"


def usd_exact(v) -> str:
    if _isna(v):
        return DASH
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return DASH


def iso_date(v) -> str:
    if _isna(v):
        return DASH
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except Exception:
        return str(v)


def iso_ts(v) -> str:
    if _isna(v):
        return DASH
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(v)


def txt(v, dash: str = DASH) -> str:
    if _isna(v):
        return dash
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or dash
    s = str(v).strip()
    return s or dash


def pct(v) -> str:
    if _isna(v):
        return DASH
    try:
        return f"{float(v):.0%}"
    except (TypeError, ValueError):
        return DASH


def conf(v) -> str:
    if _isna(v):
        return DASH
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return DASH


def as_list(v) -> list:
    """`drivers` arrives as a JSON string, a list, or already parsed."""
    if isinstance(v, (list, tuple)):
        return list(v)
    if _isna(v):
        return []
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            return [s]
        return parsed if isinstance(parsed, list) else [parsed]
    return [v]


def as_obj(v):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except Exception:
            return v
    return None


# ---------------------------------------------------------------------------------
# Loading -- exported files only. A missing file degrades to an empty, correct frame.
# ---------------------------------------------------------------------------------

def data_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("TERMINAL_DATA_DIR")
    if env:
        dirs.append(Path(env))
    dirs += [ROOT / "data", ROOT / "data" / "exports", ROOT / "data" / "parquet",
             ROOT / "exports"]
    seen, out = set(), []
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d)
    return out


def _candidates(table: str) -> list[Path]:
    return [d / f"{table}{ext}" for d in data_dirs() for ext in (".parquet", ".csv")]


def _empty(table: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TABLE_COLS[table]})


_NUMERIC = tuple(sorted(
    f.name for _t, (fs, _pk) in schemas.TABLES.items() for f in fs
    if f.sql in {"BIGINT", "INTEGER", "DOUBLE"}
))


def _conform(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Project onto the contract's columns, coercing types. Extra columns are dropped."""
    out = df.reindex(columns=TABLE_COLS[table])
    for c in DATE_COLS.get(table, []) + TS_COLS.get(table, []):
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce")
    for c in _NUMERIC:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


@st.cache_data(show_spinner=False)
def load_table(table: str, _stamp: str):
    """Return (frame, path_used, status). Status is found / missing / error: ..."""
    for p in _candidates(table):
        if not p.exists():
            continue
        try:
            raw = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
        except Exception as exc:
            return _empty(table), str(p), f"error: {exc}"
        return _conform(raw, table), str(p), "found"
    return _empty(table), None, "missing"


def _fingerprint() -> str:
    """Cheap cache key: every candidate path's mtime and size."""
    parts = []
    for t in UI_TABLES:
        for p in _candidates(t):
            try:
                s = p.stat()
                parts.append(f"{p}:{int(s.st_mtime)}:{s.st_size}")
            except OSError:
                continue
    return "|".join(parts) or "none"


# ---------------------------------------------------------------------------------
# DEMO ONLY -- synthetic data, used only when no export exists and the user opts in.
# Not derived from war.gov. Rows are projected through _conform(), so the columns are
# exactly the frozen contract's and match what src/store.py will export.
# ---------------------------------------------------------------------------------

_DEMO_FIRMS = [
    # (contractor_raw, ticker, parent_company, relationship)
    ("Lockheed Martin Corp.", "LMT", "Lockheed Martin Corporation", "direct"),
    ("Rockwell Collins Inc.", "RTX", "RTX Corporation", "subsidiary"),
    ("General Dynamics Ordnance and Tactical Systems Inc.", "GD",
     "General Dynamics Corporation", "subsidiary"),
    ("Northrop Grumman Systems Corp.", "NOC", "Northrop Grumman Corporation",
     "subsidiary"),
    ("L3Harris Technologies Inc.", "LHX", "L3Harris Technologies, Inc.", "direct"),
    ("The Boeing Co.", "BA", "The Boeing Company", "direct"),
    ("Huntington Ingalls Industries Inc.", "HII", "Huntington Ingalls Industries, Inc.",
     "direct"),
    ("Leidos Inc.", "LDOS", "Leidos Holdings, Inc.", "subsidiary"),
    ("CACI Inc.-Federal", "CACI", "CACI International Inc", "subsidiary"),
    ("Palantir USG Inc.", "PLTR", "Palantir Technologies Inc.", "subsidiary"),
    ("Sierra Nevada Corp.", None, None, "private"),
    ("Anduril Industries Inc.*", None, None, "private"),
]

_DEMO_WORK = [
    "Production and delivery of precision guided munitions kits",
    "Sustainment and depot-level repair of rotary-wing airframes",
    "Software engineering support for a battle management program",
    "Integrated air and missile defense radar production",
    "Shipboard electronics installation and modernization",
    "Satellite ground segment operations and maintenance",
    "Logistics support services for forward-deployed units",
    "Engineering, manufacturing and development of a sensor payload",
]

_DEMO_DRIVERS = [
    ["size_vs_revenue", "new_program"],
    ["recompete_win"],
    ["option_exercise", "routine_sustainment"],
    ["size_vs_revenue", "multi_year"],
    ["first_time_prime", "new_program"],
    ["routine_sustainment"],
]


@st.cache_data(show_spinner=False)
def demo_frames(seed: int = 7) -> dict:
    """Synthetic frames shaped to the contract. DEMO ONLY -- never real award data."""
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.date()
    branches = [b for b in schemas.BRANCHES if b != "OTHER"]
    actions = list(schemas.ACTION_TYPES)
    # Weights must not be coupled to the enum's length: a new action type
    # would otherwise make demo mode raise ValueError.
    _ACTION_WEIGHTS = ([6, 3, 2, 1] + [1] * len(actions))[:len(actions)]

    days = [today - timedelta(days=d) for d in range(0, 84, 3)]
    announcements, awards, materiality, entities, runs, reviews = [], [], [], [], [], []

    for i, day in enumerate(days):
        aid = f"45{86000 + i * 7}"
        announcements.append({
            "announcement_id": aid,
            "announced_date": day.isoformat(),
            "title": f"Contracts for {day.strftime('%b. %d, %Y')}",
            "url": f"https://www.war.gov/News/Contracts/Contract/Article/{aid}/",
            "fetched_at": datetime.combine(day, datetime.min.time()).replace(
                hour=22, minute=rng.randint(0, 59)),
            "http_status": 200,
            "sha256": f"{rng.getrandbits(256):064x}",
            "n_bytes": rng.randint(48_000, 210_000),
            "body_chars": rng.randint(6_000, 42_000),
            "extraction_status": "extracted",
        })
        for ordinal in range(rng.randint(1, 3)):
            firm, ticker, parent, _rel = rng.choice(_DEMO_FIRMS)
            action = rng.choices(actions, weights=_ACTION_WEIGHTS)[0]
            amount = int(rng.choice([1e7, 5e7, 1.5e8, 4e8, 9e8]) * rng.uniform(0.55, 1.9))
            cno = f"W{rng.randint(10, 99)}QKN-26-{rng.choice('DCF')}-{rng.randint(1000, 9999)}"
            is_mod = action == "modification"
            uid = schemas.award_uid(aid, cno, firm, ordinal)
            xconf = round(rng.choice([0.94, 0.91, 0.88, 0.83, 0.72, 0.61, 0.55]), 2)
            awards.append({
                "award_uid": uid,
                "announcement_id": aid,
                "announced_date": day.isoformat(),
                "service_branch": rng.choice(branches),
                "contractor_raw": firm,
                "contractor_city": rng.choice(["Arlington", "Huntsville", "Fort Worth",
                                               "St. Louis", "Melbourne", "Reston"]),
                "contractor_state": rng.choice(["Virginia", "Alabama", "Texas",
                                                "Missouri", "Florida", "California"]),
                "amount_usd": amount,
                "action_type": action,
                "contract_number": cno,
                "base_contract_number": cno if is_mod else None,
                "modification_number": f"P{rng.randint(1, 9):05d}" if is_mod else None,
                "cumulative_face_value_usd": (int(amount * rng.uniform(1.4, 3.1))
                                              if is_mod else None),
                "pricing_type": rng.choice(["firm-fixed-price", "cost-plus-fixed-fee",
                                            "cost-plus-incentive-fee",
                                            "firm-fixed-price, indefinite-delivery"]),
                "is_idiq": action == "multi_award_pool" or rng.random() < 0.3,
                "is_multi_award": action == "multi_award_pool",
                "work_description": rng.choice(_DEMO_WORK),
                "place_of_performance": rng.choice(
                    ["Locations determined with each order", "Fort Worth, Texas",
                     "Sunnyvale, California", "Pascagoula, Mississippi"]),
                "completion_date": (day + timedelta(days=rng.randint(300, 1800))
                                    ).strftime("%b. %d, %Y"),
                "contracting_activity": rng.choice(
                    ["Army Contracting Command, Redstone Arsenal, Alabama",
                     "Naval Air Systems Command, Patuxent River, Maryland",
                     "Air Force Life Cycle Management Center, Hanscom AFB, Massachusetts"]),
                "bids_solicited": rng.choice([None, 1, 3, 7]),
                "bids_received": rng.choice([None, 1, 2, 5]),
                "small_business": firm.endswith("*"),
                "extraction_confidence": xconf,
                "extraction_notes": ("Dollar figure stated as a ceiling shared across "
                                     "the pool" if action == "multi_award_pool" else None),
                "extracted_at": now - timedelta(hours=rng.randint(1, 200)),
                "extractor_model": "claude-haiku-4-5",
                "llm_cache_key": f"{rng.getrandbits(256):064x}",
                "skills_version": "extraction@3",
            })
            score = max(1, min(99, int(
                18 + 42 * (amount / 1.2e9) + rng.randint(-8, 30)
                + (14 if ticker else -6) + (10 if action == "new_award" else 0))))
            tier = "alert" if score >= 70 else ("notable" if score >= 40 else "routine")
            who = ticker or "a private prime"
            materiality.append({
                "award_uid": uid,
                "score": score,
                "tier": tier,
                "rationale": (
                    f"{usd(amount)} {action.replace('_', ' ')} to {who}; "
                    + ("material against annual revenue and tied to a growth program"
                       if tier == "alert" else
                       "worth a look but inside the normal run-rate" if tier == "notable"
                       else "routine sustainment work, no read-through")),
                "drivers": json.dumps(rng.choice(_DEMO_DRIVERS)),
                "scored_at": now - timedelta(hours=rng.randint(1, 200)),
                "scorer_model": "claude-haiku-4-5" if score < 70 else "claude-opus-5",
                "llm_cache_key": f"{rng.getrandbits(256):064x}",
                "skills_version": "materiality@2",
            })

    for firm, ticker, parent, rel in _DEMO_FIRMS:
        c = round(rng.choice([0.97, 0.95, 0.91, 0.86, 0.74, 0.62]), 2)
        alias_hit = ticker is not None and rng.random() < 0.4
        entities.append({
            "contractor_raw": firm,
            "normalized_name": firm.rstrip("*").strip(),
            "ticker": ticker,
            "parent_company": parent,
            "relationship": rel,
            "is_public": ticker is not None,
            "confidence": c,
            "reasoning": (f"Alias table hit on data/universe.csv -> {ticker}"
                          if alias_hit else
                          f"Resolved to {ticker} via ultimate-parent lookup" if ticker
                          else "No listed parent found; treated as private"),
            "resolved_at": now - timedelta(hours=rng.randint(1, 300)),
            "resolver_model": "deterministic" if alias_hit else "claude-haiku-4-5",
            "llm_cache_key": None if alias_hit else f"{rng.getrandbits(256):064x}",
            "skills_version": "entity_resolution@4",
        })

    tick_ids = [f"tick-{(today - timedelta(days=d)).isoformat()}" for d in range(0, 12)]
    award_keys = [a["award_uid"] for a in awards] or ["-"]
    for i in range(140):
        agent = rng.choice(["extract", "resolve_entity", "score_materiality"])
        hit = rng.random() < 0.62
        escalated = (not hit) and rng.random() < 0.16
        model = ("deterministic" if (agent == "resolve_entity" and rng.random() < 0.25)
                 else ("claude-opus-5" if escalated else "claude-haiku-4-5"))
        itok = 0 if hit or model == "deterministic" else rng.randint(1_200, 26_000)
        otok = 0 if hit or model == "deterministic" else rng.randint(200, 4_200)
        rate_in, rate_out = (5.0, 25.0) if model == "claude-opus-5" else (1.0, 5.0)
        cost = (0.0 if model == "deterministic"
                else round(itok / 1e6 * rate_in + otok / 1e6 * rate_out, 6))
        c = round(rng.choice([0.96, 0.93, 0.89, 0.82, 0.71, 0.64, 0.52]), 2)
        failed = rng.random() < 0.03
        outcome = ("failed" if failed else "escalated" if escalated
                   else "flagged" if c < CONFIDENCE_FLOOR else "ok")
        runs.append({
            "run_id": f"run-{i:05d}",
            "tick_id": rng.choice(tick_ids),
            "agent": agent,
            "item_key": (rng.choice(_DEMO_FIRMS)[0] if agent == "resolve_entity"
                         else rng.choice(award_keys)),
            "model": model,
            "escalated": escalated,
            "cache_hit": hit,
            "input_tokens": itok,
            "output_tokens": otok,
            "cost_usd": cost,
            "confidence": c,
            "outcome": outcome,
            "error": "HTTP 429 from the API; retried on the next tick" if failed else None,
            "skills_version": rng.choice(["extraction@3", "materiality@2",
                                          "entity_resolution@4"]),
            "started_at": now - timedelta(minutes=rng.randint(5, 20_000)),
            "duration_ms": 2 if hit else rng.randint(900, 14_000),
        })

    low = [a for a in awards if a["extraction_confidence"] < CONFIDENCE_FLOOR][:5]
    for i, a in enumerate(low):
        reviews.append({
            "review_id": f"rev-{i:04d}",
            "flagged_at": now - timedelta(hours=rng.randint(2, 90)),
            "agent": "extract",
            "item_key": a["award_uid"],
            "reason": "Two dollar figures in one paragraph; could not separate the value "
                      "of this action from the cumulative face value",
            "confidence": a["extraction_confidence"],
            "payload": json.dumps({"contractor_raw": a["contractor_raw"],
                                   "amount_usd": a["amount_usd"],
                                   "contract_number": a["contract_number"],
                                   "action_type": a["action_type"]}),
            "resolved": False,
        })
    for i, e in enumerate([e for e in entities if e["confidence"] < CONFIDENCE_FLOOR]):
        reviews.append({
            "review_id": f"rev-e{i:03d}",
            "flagged_at": now - timedelta(hours=rng.randint(2, 90)),
            "agent": "resolve_entity",
            "item_key": e["contractor_raw"],
            "reason": "Opus retry still below the 0.7 floor; several listed parents "
                      "remain plausible",
            "confidence": e["confidence"],
            "payload": json.dumps({"contractor_raw": e["contractor_raw"],
                                   "ticker": e["ticker"],
                                   "relationship": e["relationship"]}),
            "resolved": bool(i % 3 == 0),
        })

    # Change events. In the real pipeline these come from the manager's set
    # difference on award UIDs -- deterministic, never an agent. Demo mode fakes
    # the output of that diff, not the diff itself.
    score_by_uid = {m["award_uid"]: m["score"] for m in materiality}
    tick_by_raw = {e["contractor_raw"]: e["ticker"] for e in entities}
    changes = []
    for a in sorted(awards, key=lambda r: str(r["announced_date"]), reverse=True)[:24]:
        ctype = "new_award" if a["action_type"] == "new_award" else "amount_changed"
        prev_v = None if ctype == "new_award" else usd(a["amount_usd"] * 0.6)
        changes.append({
            "change_id": f"chg-{a['award_uid'][:16]}",
            "detected_at": now - timedelta(hours=rng.randint(1, 72)),
            "change_type": ctype,
            "announcement_id": a["announcement_id"],
            "award_uid": a["award_uid"],
            "ticker": tick_by_raw.get(a["contractor_raw"]),
            "prev_value": prev_v,
            "new_value": usd(a["amount_usd"]),
            "materiality_score": score_by_uid.get(a["award_uid"]),
        })

    raw = {"announcements": announcements, "awards": awards, "entities": entities,
           "materiality": materiality, "agent_runs": runs, "review_queue": reviews,
           "changes": changes}
    return {t: _conform(pd.DataFrame(rows, columns=TABLE_COLS[t]), t)
            for t, rows in raw.items()}


# ---------------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------------

def enrich(frames: dict) -> pd.DataFrame:
    """awards LEFT JOIN materiality LEFT JOIN entities.

    Award columns keep their contract names. Columns pulled from the other two tables
    are renamed with a table prefix so the three ``llm_cache_key`` and two
    ``confidence`` columns stay distinguishable on the provenance view.
    """
    aw = frames["awards"].copy()

    mat = frames["materiality"][TABLE_COLS["materiality"]].rename(columns={"llm_cache_key": "materiality_cache_key",
                      "skills_version": "materiality_skills_version"})
    ent = frames["entities"][TABLE_COLS["entities"]].rename(columns={"confidence": "entity_confidence",
                      "reasoning": "entity_reasoning",
                      "llm_cache_key": "entity_cache_key",
                      "skills_version": "entity_skills_version",
                      "resolved_at": "entity_resolved_at"})

    df = aw.merge(mat.drop_duplicates("award_uid"), on="award_uid", how="left")
    df = df.merge(ent.drop_duplicates("contractor_raw"), on="contractor_raw", how="left")
    df["tier_rank"] = df["tier"].map(TIER_RANK)
    df["tier_rank"] = pd.to_numeric(df["tier_rank"], errors="coerce").fillna(
        len(TIER_ORDER))
    if df.empty:
        df["label"] = pd.Series(dtype="object")
    else:
        df["label"] = [
            f"{iso_date(d)}  {txt(t, 'private'):<6}  {txt(c)[:44]}  {usd(a)}"
            for d, t, c, a in zip(df["announced_date"], df["ticker"],
                                  df["contractor_raw"], df["amount_usd"])
        ]
    return df


# ---------------------------------------------------------------------------------
# Shared presentation
# ---------------------------------------------------------------------------------

_TIER_CSS = {
    "alert": "background-color:#8c1d18;color:#ffffff;font-weight:600",
    "notable": "background-color:#7a4b00;color:#ffffff;font-weight:600",
    "routine": "color:#8a8a8a",
}


def show_table(df: pd.DataFrame, tier_col: str | None = None, height: int | None = None,
               key: str | None = None, select: bool = False, column_config=None):
    """One table renderer. Tints the tier column when there is one; never fails on it."""
    kwargs = {"hide_index": True, "width": "stretch"}
    if height:
        kwargs["height"] = height
    if column_config:
        kwargs["column_config"] = column_config
    if select:
        kwargs.update(on_select="rerun", selection_mode="single-row", key=key)
    elif key:
        kwargs["key"] = key
    data = df
    if tier_col and tier_col in df.columns:
        try:
            data = df.style.map(
                lambda v: _TIER_CSS.get(str(v).strip().lower(), ""), subset=[tier_col])
        except Exception:
            data = df
    try:
        return st.dataframe(data, **kwargs)
    except Exception:
        kwargs.pop("column_config", None)
        return st.dataframe(df, **kwargs)


def empty_state(table: str, what: str) -> None:
    st.info(f"**No `{table}` export yet.** {what}")
    with st.expander("Where the app looked, and how to fill it"):
        st.write("Searched, in order:")
        st.code("\n".join(str(p) for p in _candidates(table)), language="text")
        st.markdown(
            "The UI reads **exported files only**, so it never blocks on the pipeline. "
            "Populate them by running the pipeline and its export step: `make demo` "
            "replays everything from the committed cache with no API key, and "
            "`src/store.py` writes one Parquet per table into `data/`. Point "
            "`TERMINAL_DATA_DIR` at another directory to read an export from elsewhere."
            "\n\nTo look around the interface right now, turn on **synthetic demo data** "
            "in the sidebar."
        )


def metric_row(pairs) -> None:
    cols = st.columns(len(pairs))
    for col, (label, value) in zip(cols, pairs):
        col.metric(label, value)


def tier_text(v) -> str:
    s = txt(v)
    return s.upper() if s != DASH else DASH


# ---------------------------------------------------------------------------------
# View 1 -- change feed
# ---------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------
# Modals -- a click on a ticker or an event opens the detail, instead of stacking
# another panel onto an already dense page.
# ---------------------------------------------------------------------------------

TICKER_COL = st.column_config.LinkColumn(
    "Ticker", help="Open this ticker's awards", display_text=r"ticker=([A-Z.]+)")


def ticker_link(v) -> str:
    """A ticker cell rendered as a link. st.dataframe cannot attach a callback to a
    cell, and row-selection swallows the click, so the cell carries a query-param
    URL and the shell opens the modal on the resulting rerun."""
    t = txt(v, "")
    return f"?ticker={t}" if t and t != DASH else ""


@st.dialog("Awards", width="large")
def ticker_dialog(frames, df, ticker: str) -> None:
    """Every award for one ticker. Opened by clicking the ticker in any table."""
    sub = df[df["ticker"] == ticker].sort_values("announced_date", ascending=False)

    ent = frames.get("entities")
    if ent is not None and not ent.empty:
        row = ent[ent["ticker"] == ticker].head(1)
        if not row.empty:
            r = row.iloc[0]
            name = txt(r["parent_company"])
            st.markdown(f"**{name if name != DASH else ticker}** · `{ticker}` · "
                        f"{txt(r['relationship'])}")
            st.caption(txt(r["reasoning"]))

    if sub.empty:
        st.info(f"No awards for {ticker} in the current export.")
        return

    metric_row([
        ("Obligated", usd(sub["amount_usd"].sum(min_count=1))),
        ("Awards", f"{len(sub):,}"),
        ("Largest", usd(sub["amount_usd"].max())),
    ])
    show_table(pd.DataFrame({
        "Date": sub["announced_date"].map(iso_date),
        "Tier": sub["tier"].map(tier_text),
        "Contractor": sub["contractor_raw"].map(txt),
        "Amount": sub["amount_usd"].map(usd),
        "Action": sub["action_type"].map(lambda v: txt(v).replace("_", " ")),
        "Branch": sub["service_branch"].map(txt),
    }), tier_col="Tier", height=300, key=f"tk_{ticker}")


@st.dialog("Provenance", width="large")
def provenance_dialog(frames, r) -> None:
    """The audit trail for ONE row: source document, digest, and the model call
    behind every AI-derived field. Same panel as the Provenance view -- reached
    here by clicking the event or award it belongs to."""
    provenance_panel(frames, r)


def view_events(frames, df):
    """Tab 1 -- change events only. One table on the page."""
    ch = frames.get("changes")
    if ch is None or ch.empty:
        empty_state("changes", "Change events appear here once `manager tick` has "
                               "diffed award UIDs against the previous run.")
        return

    cols = ["award_uid", "contractor_raw", "amount_usd", "action_type", "service_branch"]
    ev = ch.merge(df[[c for c in cols if c in df.columns]].drop_duplicates("award_uid"),
                  on="award_uid", how="left")
    ev = ev.sort_values(["materiality_score", "detected_at"],
                        ascending=[False, False], na_position="last")

    metric_row([
        ("Events", f"{len(ev):,}"),
        ("Tickers", f"{ev['ticker'].nunique():,}"),
        ("Value", usd(ev["amount_usd"].sum(min_count=1))),
    ])

    sel = show_table(pd.DataFrame({
        "Detected": ev["detected_at"].map(iso_ts),
        "Change": ev["change_type"].map(lambda v: txt(v).replace("_", " ")),
        "Score": ev["materiality_score"],
        "Ticker": ev["ticker"].map(ticker_link),
        "Contractor": ev["contractor_raw"].map(txt),
        "Amount": ev["amount_usd"].map(usd),
        "Was": ev["prev_value"].map(txt),
        "Now": ev["new_value"].map(txt),
    }), height=520, key="change_events", select=True,
        column_config={"Ticker": TICKER_COL})

    try:
        picked = list(sel.selection.rows)
    except Exception:
        picked = []
    if picked:
        uid = ev.iloc[picked[0]]["award_uid"]
        match = df[df["award_uid"] == uid]
        if match.empty:
            st.warning(f"No award row for `{uid}` in this export.")
        else:
            provenance_dialog(frames, match.iloc[0])


def view_feed(frames, df):
    """Tab 2 -- awards, materiality-ranked. One table on the page."""
    if df.empty:
        empty_state("awards", "Awards ranked by investor materiality appear here.")
        return

    with st.popover("Filters", width="content"):
        tiers = st.multiselect("Tier", TIER_ORDER, default=TIER_ORDER)
        ticker = st.selectbox(
            "Ticker", ["all"] + sorted({str(t) for t in df["ticker"].dropna().unique()}))
        branches = st.multiselect(
            "Branch", sorted({str(b) for b in df["service_branch"].dropna().unique()}))
        days = st.number_input("Lookback (days)", min_value=1, max_value=3650,
                               value=90, step=7)

    f = df.copy()
    if len(tiers) < len(TIER_ORDER):
        f = f[f["tier"].isin(tiers)]
    if ticker != "all":
        f = f[f["ticker"] == ticker]
    if branches:
        f = f[f["service_branch"].isin(branches)]
    if f["announced_date"].notna().any():
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=int(days))
        f = f[f["announced_date"].isna() | (f["announced_date"] >= cutoff)]

    f = f.sort_values(["tier_rank", "score", "announced_date"],
                      ascending=[True, False, False], na_position="last")

    metric_row([
        ("Awards", f"{len(f):,}"),
        ("Alerts", f"{int((f['tier'] == 'alert').sum()):,}"),
        ("Obligated", usd(f["amount_usd"].sum(min_count=1))),
    ])

    if f.empty:
        st.warning("No awards match these filters.")
        return

    ev = show_table(pd.DataFrame({
        "Date": f["announced_date"].map(iso_date),
        "Tier": f["tier"].map(tier_text),
        "Score": f["score"],
        "Ticker": f["ticker"].map(ticker_link),
        "Contractor": f["contractor_raw"].map(txt),
        "Amount": f["amount_usd"].map(usd),
        "Action": f["action_type"].map(lambda v: txt(v).replace("_", " ")),
        "Branch": f["service_branch"].map(txt),
    }), tier_col="Tier", height=520, key="feed_table", select=True,
        column_config={"Ticker": TICKER_COL,
                       "Score": st.column_config.NumberColumn("Score", format="%d")})

    try:
        rows = list(ev.selection.rows)
    except Exception:
        rows = []
    if rows:
        provenance_dialog(frames, f.iloc[rows[0]])


# ---------------------------------------------------------------------------------
# View 2 -- company view
# ---------------------------------------------------------------------------------

def view_company(frames, df):
    if df.empty:
        empty_state("awards", "Pick a ticker to see its awards, totals and trend.")
        return

    tickers = sorted({str(t) for t in df["ticker"].dropna().unique()})
    if not tickers:
        st.warning("No award carries a resolved ticker yet. Entity resolution has not "
                   "run, or every contractor in this export is private.")
        counts = (df.assign(n=1).groupby(df["contractor_raw"].map(txt), as_index=False)
                    .agg(Awards=("n", "sum"), Obligated=("amount_usd", "sum")))
        counts.columns = ["Contractor", "Awards", "Obligated"]
        counts["Obligated"] = counts["Obligated"].map(usd)
        show_table(counts.sort_values("Awards", ascending=False), height=320)
        return

    left, right = st.columns([1, 3])
    t = left.selectbox("Ticker", tickers)
    sub = df[df["ticker"] == t].sort_values("announced_date", ascending=False)

    ent = frames["entities"]
    row = ent[ent["ticker"] == t].head(1)
    if not row.empty:
        r = row.iloc[0]
        name = txt(r["parent_company"])
        right.markdown(
            f"**{name if name != DASH else t}** &nbsp; · &nbsp; "
            f"{txt(r['relationship'])} &nbsp; · &nbsp; resolver confidence "
            f"{conf(r['confidence'])} ({txt(r['resolver_model'])})")
        right.caption(txt(r["reasoning"]))

    span = DASH
    if sub["announced_date"].notna().any():
        span = (f"{iso_date(sub['announced_date'].min())} to "
                f"{iso_date(sub['announced_date'].max())}")
    metric_row([
        ("Total obligated", usd(sub["amount_usd"].sum(min_count=1))),
        ("Awards", f"{len(sub):,}"),
        ("Largest", usd(sub["amount_usd"].max())),
        ("Alerts", f"{int((sub['tier'] == 'alert').sum()):,}"),
        ("Window", span),
    ])

    if sub["announced_date"].notna().any():
        m = sub.dropna(subset=["announced_date"]).copy()
        m["Month"] = m["announced_date"].dt.strftime("%Y-%m")
        agg = (m.groupby("Month", as_index=False)
                 .agg(Obligated=("amount_usd", "sum"), Awards=("award_uid", "count"))
                 .sort_values("Month"))
        st.markdown("**Obligated by month**")
        st.bar_chart(agg, x="Month", y="Obligated", x_label="Month",
                     y_label="Obligated (USD)", height=260)
        with st.expander("Monthly detail"):
            show_table(pd.DataFrame({"Month": agg["Month"], "Awards": agg["Awards"],
                                     "Obligated": agg["Obligated"].map(usd)}))
    else:
        st.caption("No announcement dates in this export, so no trend can be drawn.")

    st.markdown(f"**Awards — {t}**")
    show_table(pd.DataFrame({
        "Date": sub["announced_date"].map(iso_date),
        "Tier": sub["tier"].map(tier_text),
        "Score": sub["score"],
        "Amount": sub["amount_usd"].map(usd),
        "Action": sub["action_type"].map(lambda v: txt(v).replace("_", " ")),
        "Branch": sub["service_branch"].map(txt),
        "Contract number": sub["contract_number"].map(txt),
        "Work": sub["work_description"].map(txt),
    }), tier_col="Tier", height=420)


# ---------------------------------------------------------------------------------
# View 3 -- review queue
# ---------------------------------------------------------------------------------

def view_review(frames):
    rq = frames["review_queue"]
    if rq.empty:
        empty_state("review_queue", "Low-confidence rows the agents flagged for a human "
                                    "will appear here.")
        return

    resolved = rq["resolved"].fillna(False).astype(bool)
    metric_row([
        ("Flagged, all time", f"{len(rq):,}"),
        ("Open", f"{int((~resolved).sum()):,}"),
        ("Resolved", f"{int(resolved.sum()):,}"),
        ("Mean confidence", conf(rq["confidence"].mean())),
    ])

    open_only = st.toggle("Open items only", value=True)
    view = rq[~resolved] if open_only else rq
    if view.empty:
        st.success("Nothing open. Every flagged row has been dealt with.")
        return

    view = view.sort_values("confidence", ascending=True, na_position="first")
    show_table(pd.DataFrame({
        "Flagged": view["flagged_at"].map(iso_ts),
        "Agent": view["agent"].map(txt),
        "Confidence": view["confidence"].map(conf),
        "Item": view["item_key"].map(txt),
        "Reason": view["reason"].map(txt),
        "Resolved": view["resolved"].fillna(False).astype(bool),
    }), height=320)

    st.markdown("**The uncertain result, for a reviewer to accept or correct**")
    labels = [f"{txt(r['agent'])}  ·  conf {conf(r['confidence'])}  ·  "
              f"{txt(r['item_key'])[:44]}" for _, r in view.iterrows()]
    pick = st.selectbox("Item", range(len(labels)), format_func=lambda i: labels[i])
    r = view.iloc[pick]
    c1, c2 = st.columns([1, 2])
    c1.write(f"`review_id` {txt(r['review_id'])}")
    c1.write(f"`agent` {txt(r['agent'])}")
    c1.write(f"`item_key` {txt(r['item_key'])}")
    c1.write(f"`confidence` {conf(r['confidence'])}")
    c1.write(f"`flagged_at` {iso_ts(r['flagged_at'])}")
    c2.write(txt(r["reason"]))
    payload = as_obj(r["payload"])
    if payload is None:
        c2.caption("No payload recorded.")
    elif isinstance(payload, str):
        c2.code(payload, language="text")
    else:
        c2.json(payload)


# ---------------------------------------------------------------------------------
# View 4 -- agent activity
# ---------------------------------------------------------------------------------

def view_agents(frames):
    ar = frames["agent_runs"]
    if ar.empty:
        empty_state("agent_runs", "What ran, on which model, cache hit or not, tokens, "
                                  "cost and escalations will appear here.")
        return

    hits = ar["cache_hit"].fillna(False).astype(bool)
    cost = ar["cost_usd"].fillna(0.0)
    escalated = ar["escalated"].fillna(False).astype(bool)
    metric_row([
        ("Dispatches", f"{len(ar):,}"),
        ("Cache hit rate", pct(hits.mean()) if len(ar) else DASH),
        ("Total spend", f"${cost.sum():,.4f}"),
        ("Escalations", f"{int(escalated.sum()):,}"),
        ("Failures", f"{int((ar['outcome'] == 'failed').sum()):,}"),
    ])
    api_calls = int((~hits).sum())
    per_call = f"${cost.sum() / api_calls:.4f}" if api_calls else DASH
    work = ar.assign(_hit=hits, _cost=cost, _esc=escalated)
    breakdown = st.segmented_control(
        "Breakdown", ["Dispatches", "By agent", "By model", "By outcome"],
        default="Dispatches", label_visibility="collapsed")

    if breakdown == "By agent":
        by_agent = work.groupby("agent", as_index=False).agg(
            runs=("run_id", "count"), hits=("_hit", "sum"), escalations=("_esc", "sum"),
            input_tokens=("input_tokens", "sum"), output_tokens=("output_tokens", "sum"),
            spend=("_cost", "sum"), mean_conf=("confidence", "mean"))
        by_agent["hit_rate"] = by_agent["hits"] / by_agent["runs"]
        show_table(pd.DataFrame({
            "Agent": by_agent["agent"].map(txt),
            "Runs": by_agent["runs"],
            "Cache hits": by_agent["hits"].astype(int),
            "Hit rate": by_agent["hit_rate"].map(pct),
            "Escalations": by_agent["escalations"].astype(int),
            "In tokens": by_agent["input_tokens"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else DASH),
            "Out tokens": by_agent["output_tokens"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else DASH),
            "Spend": by_agent["spend"].map(lambda v: f"${v:,.4f}"),
            "Mean conf.": by_agent["mean_conf"].map(conf),
        }), height=480)

    elif breakdown == "By model":
        bm = work.groupby("model", as_index=False).agg(
            runs=("run_id", "count"), hits=("_hit", "sum"), spend=("_cost", "sum"))
        show_table(pd.DataFrame({"Model": bm["model"].map(txt), "Runs": bm["runs"],
                                 "Cache hits": bm["hits"].astype(int),
                                 "Spend": bm["spend"].map(lambda v: f"${v:,.4f}")}),
                   height=480)

    elif breakdown == "By outcome":
        bo = ar.groupby(ar["outcome"].map(txt), as_index=False).agg(
            runs=("run_id", "count"))
        bo.columns = ["Outcome", "Runs"]
        show_table(bo, height=480)

    else:
        recent = work[~work["_hit"]] if st.toggle("Hide cache hits", value=False) else work
        recent = recent.sort_values("started_at", ascending=False,
                                    na_position="last").head(300)
        show_table(pd.DataFrame({
            "Started": recent["started_at"].map(iso_ts),
            "Tick": recent["tick_id"].map(txt),
            "Agent": recent["agent"].map(txt),
            "Item": recent["item_key"].map(lambda v: txt(v)[:28]),
            "Model": recent["model"].map(txt),
            "Cache": recent["_hit"].map(lambda v: "hit" if v else "miss"),
            "Esc.": recent["_esc"].map(lambda v: "yes" if v else ""),
            "In": recent["input_tokens"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else DASH),
            "Out": recent["output_tokens"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else DASH),
            "Cost": recent["cost_usd"].map(
                lambda v: f"${float(v):.4f}" if pd.notna(v) else DASH),
            "Conf.": recent["confidence"].map(conf),
            "Outcome": recent["outcome"].map(txt),
            "ms": recent["duration_ms"].map(
                lambda v: f"{int(v):,}" if pd.notna(v) else DASH),
            "Skills": recent["skills_version"].map(txt),
        }), height=480)

    errs = ar[ar["outcome"] == "failed"]
    if not errs.empty:
        with st.expander(f"Failures ({len(errs)})"):
            show_table(pd.DataFrame({
                "Started": errs["started_at"].map(iso_ts),
                "Agent": errs["agent"].map(txt),
                "Item": errs["item_key"].map(txt),
                "Error": errs["error"].map(txt)}))


# ---------------------------------------------------------------------------------
# View 5 -- provenance
# ---------------------------------------------------------------------------------

def cache_note(key) -> str:
    k = txt(key)
    if k == DASH:
        return "no model call (deterministic path)"
    p = ROOT / "cache" / "llm" / f"{k}.json"
    return f"`cache/llm/{k}.json` {'(present)' if p.exists() else '(not in this checkout)'}"


def provenance_panel(frames, r) -> None:
    st.markdown("---")
    st.markdown(f"**Provenance — `{txt(r.get('award_uid'))}`**")

    ann = frames["announcements"]
    src = ann[ann["announcement_id"] == r.get("announcement_id")]
    src = src.iloc[0] if not src.empty else None

    c1, c2, c3 = st.columns(3)
    with c1:
        st.caption("AWARD")
        st.write(f"`award_uid` {txt(r.get('award_uid'))}")
        st.write(f"`announced_date` {iso_date(r.get('announced_date'))}")
        st.write(f"`contractor_raw` {txt(r.get('contractor_raw'))}")
        st.write(f"`ticker` {txt(r.get('ticker'), 'private / unresolved')}")
        st.write(f"`contract_number` {txt(r.get('contract_number'))}")
        st.write(f"`amount_usd` {usd_exact(r.get('amount_usd'))}")
        if txt(r.get("modification_number")) != DASH:
            st.write(f"`modification_number` {txt(r.get('modification_number'))} on "
                     f"`{txt(r.get('base_contract_number'))}`")
        if not _isna(r.get("cumulative_face_value_usd")):
            st.write("`cumulative_face_value_usd` "
                     f"{usd_exact(r.get('cumulative_face_value_usd'))}")
    with c2:
        st.caption("SOURCE DOCUMENT")
        if src is None:
            st.write("No `announcements` row for "
                     f"`{txt(r.get('announcement_id'))}` in this export.")
        else:
            url = txt(src["url"])
            st.write(f"`url` [{url}]({url})" if url != DASH else f"`url` {DASH}")
            st.write(f"`fetched_at` {iso_ts(src['fetched_at'])}")
            st.write(f"`http_status` {txt(src['http_status'])} · `n_bytes` "
                     f"{txt(src['n_bytes'])} · `body_chars` {txt(src['body_chars'])}")
            st.write("`sha256`")
            st.code(txt(src["sha256"]), language="text")
    with c3:
        st.caption("MATERIALITY")
        st.write(f"`tier` **{tier_text(r.get('tier'))}** · `score` {txt(r.get('score'))}")
        st.write(f"`rationale` {txt(r.get('rationale'))}")
        drivers = as_list(r.get("drivers"))
        st.write(f"`drivers` {', '.join(str(d) for d in drivers) if drivers else DASH}")
        st.write(f"`scorer_model` {txt(r.get('scorer_model'))}")

    st.caption("REASONING CHAIN")
    show_table(pd.DataFrame([
        {"Step": "extract", "Model": txt(r.get("extractor_model")),
         "Confidence": conf(r.get("extraction_confidence")),
         "Skills": txt(r.get("skills_version")),
         "Ran": iso_ts(r.get("extracted_at")),
         "llm_cache_key": txt(r.get("llm_cache_key"))},
        {"Step": "resolve_entity", "Model": txt(r.get("resolver_model")),
         "Confidence": conf(r.get("entity_confidence")),
         "Skills": txt(r.get("entity_skills_version")),
         "Ran": iso_ts(r.get("entity_resolved_at")),
         "llm_cache_key": txt(r.get("entity_cache_key"))},
        {"Step": "score_materiality", "Model": txt(r.get("scorer_model")),
         "Confidence": DASH,
         "Skills": txt(r.get("materiality_skills_version")),
         "Ran": iso_ts(r.get("scored_at")),
         "llm_cache_key": txt(r.get("materiality_cache_key"))},
    ]))
    for step, key in (("extract", r.get("llm_cache_key")),
                      ("resolve_entity", r.get("entity_cache_key")),
                      ("score_materiality", r.get("materiality_cache_key"))):
        st.caption(f"{step}: {cache_note(key)}")

    reasoning = txt(r.get("entity_reasoning"))
    if reasoning != DASH:
        st.caption(f"entity resolution: {reasoning}")
    notes = txt(r.get("extraction_notes"))
    if notes != DASH:
        st.warning(f"Extraction note: {notes}")
    xc = r.get("extraction_confidence")
    if not _isna(xc) and float(xc) < CONFIDENCE_FLOOR:
        st.warning(f"Extraction confidence {conf(xc)} is below the "
                   f"{CONFIDENCE_FLOOR:.1f} floor — treat this row as provisional.")

    with st.expander("Full award row, as stored"):
        st.json({c: (None if _isna(r.get(c)) else str(r.get(c)))
                 for c in TABLE_COLS["awards"] if c in r.index})


# ---------------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------------

def main() -> None:
    st.sidebar.title("DoD Contract Terminal")
    st.sidebar.caption(f"contract v{schemas.SCHEMA_VERSION} · read-only · no model calls")

    if st.sidebar.button("Reload data", width="stretch"):
        st.cache_data.clear()

    stamp = _fingerprint()
    loaded = {t: load_table(t, stamp) for t in UI_TABLES}
    frames = {t: v[0] for t, v in loaded.items()}
    found = [t for t, v in loaded.items() if v[2] == "found" and not v[0].empty]
    errors = {t: v[2] for t, v in loaded.items() if v[2].startswith("error")}

    demo = False
    if not found:
        st.sidebar.warning("No exported data found.")
        demo = st.sidebar.toggle(
            "Use synthetic demo data", value=False,
            help="Fabricated rows shaped to the frozen contract. Not from war.gov. "
                 "Offered only because no export is present.")
        if demo:
            frames = demo_frames()
    else:
        st.sidebar.success("Loaded: " + ", ".join(found))

    with st.sidebar.expander("Data source"):
        rows = []
        for t in UI_TABLES:
            _f, path, status = loaded[t]
            rows.append({"table": t, "rows": len(frames[t]),
                         "source": "synthetic" if demo else
                                   (Path(path).name if path else "—"),
                         "status": "demo" if demo else status})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(f"root: `{ROOT}`")

    for t, msg in errors.items():
        st.sidebar.error(f"{t}: {msg}")

    # A ticker cell is a link carrying ?ticker=XX. Consume it here, open the modal,
    # and clear it so a rerun does not reopen the same dialog forever.
    picked_ticker = st.query_params.get("ticker")
    if picked_ticker:
        del st.query_params["ticker"]

    if demo:
        st.warning("SYNTHETIC DEMO DATA — not real awards", icon=":material/science:")

    df = enrich(frames)

    if picked_ticker:
        ticker_dialog(frames, df, str(picked_ticker))

    tabs = st.tabs(["Events", "Awards", "Companies", "Review", "Agents"])
    with tabs[0]:
        view_events(frames, df)
    with tabs[1]:
        view_feed(frames, df)
    with tabs[2]:
        view_company(frames, df)
    with tabs[3]:
        view_review(frames)
    with tabs[4]:
        view_agents(frames)




main()
