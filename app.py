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

          /* --- scale with the window instead of a hardcoded pixel height ---
             A fixed table height taller than the viewport gives you TWO scrollbars:
             the page's and the grid's. Sizing the grid off 100vh means the page
             itself never needs to scroll, so only the grid scrolls -- and the table
             grows on a large monitor instead of wasting it.
             --chrome is everything stacked above the grid (header, tabs, metrics,
             filter row); tune that one number, not each call site. */
          :root {--chrome: 310px;}
          .block-container {max-width: 100%;}
          [class*="st-key-maintable"] [data-testid="stDataFrame"],
          [class*="st-key-maintable"] [data-testid="stDataFrameResizable"] {
              height: calc(100vh - var(--chrome)) !important;
              min-height: 240px;
          }
          @media (max-height: 780px) {:root {--chrome: 280px;}}
          @media (max-width: 1200px)  {:root {--chrome: 340px;}}
          /* Wide content scrolls inside its own box; the page never scrolls sideways. */
          [data-testid="stHorizontalBlock"] {flex-wrap: wrap;}

          /* --- detail modal: built to be read, not audited --- */
          .dt-head {display:flex; align-items:baseline; gap:.6rem; flex-wrap:wrap;
                    margin:0 0 .1rem 0;}
          .dt-name {font-size:1.35rem; font-weight:650; letter-spacing:-.01em;}
          .dt-tkr  {font-size:.78rem; font-weight:700; padding:.1rem .42rem;
                    border-radius:4px; background:#1f6feb; color:#fff;}
          .dt-sub  {opacity:.62; font-size:.84rem; margin:0 0 .7rem 0;}
          .dt-amt  {font-size:2.0rem; font-weight:680; letter-spacing:-.02em;
                    line-height:1.15;}
          .dt-badge{display:inline-block; font-size:.7rem; font-weight:750;
                    letter-spacing:.06em; padding:.16rem .5rem; border-radius:999px;}
          .dt-alert  {background:#7f1d1d; color:#fecaca;}
          .dt-notable{background:#78350f; color:#fde68a;}
          .dt-routine{background:#334155; color:#cbd5e1;}
          .dt-chip {display:inline-block; font-size:.7rem; padding:.14rem .46rem;
                    margin:.12rem .22rem .12rem 0; border-radius:4px;
                    background:rgba(128,128,128,.16); opacity:.85;}
          .dt-facts{width:100%; border-collapse:collapse; font-size:.87rem;}
          .dt-facts td {padding:.3rem .1rem; border-bottom:1px solid rgba(128,128,128,.16);
                        vertical-align:top;}
          .dt-facts td:first-child {opacity:.6; width:34%; white-space:nowrap;}
          .dt-why  {font-size:.94rem; line-height:1.5; margin:.1rem 0 .5rem 0;}
          .dt-lbl  {font-size:.7rem; font-weight:700; letter-spacing:.08em;
                    opacity:.5; margin:.85rem 0 .25rem 0;}
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
             "review_queue", "changes", "contracts"]

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
def load_table(table: str, stamp: str):
    """Return (frame, path_used, status). Status is found / missing / error: ...

    `stamp` MUST NOT be renamed to `_stamp`: st.cache_data excludes leading-underscore
    parameters from the cache key, so the fingerprint would be accepted and ignored and
    every reread would serve the first load forever. That is exactly what it did until
    the live watcher made the staleness visible.
    """
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


#: How often the watcher checks the exports for a write. Short enough that a running
#: pipeline looks live, long enough that it costs nothing: one os.stat per candidate
#: path, no query and no model call.
WATCH_SECONDS = 3.0


@st.fragment(run_every=WATCH_SECONDS)
def data_watcher(stamp: str) -> None:
    """Rerun the whole app when the pipeline writes new exports.

    This is what connects the front end to the back end. It deliberately watches the
    exported FILES rather than opening the database: DuckDB is single-writer, so a UI
    that held a connection would block the very pipeline it is trying to follow. The
    manager already exports after every wave (and now after every batch), so the files
    are the live surface.

    `_fingerprint` is the same mtime+size key `load_table` is cached on, so a changed
    fingerprint both triggers the rerun and invalidates the cache -- there is no
    separate cache-clearing step to get out of sync.
    """
    if _fingerprint() != stamp:
        st.rerun(scope="app")


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

    # Every award row is an EVENT on a contract; carry the contract it belongs to so a
    # single event can be shown in the context of its whole timeline.
    con = frames.get("contracts")
    if con is not None and not con.empty and "contract_uid" in df.columns:
        keep = ["contract_uid", "contract_number", "n_events", "n_modifications",
                "initial_value_usd", "total_actioned_usd", "ceiling_usd",
                "history_complete", "first_event_date", "last_event_date"]
        c = con[[k for k in keep if k in con.columns]].drop_duplicates("contract_uid")
        c = c.rename(columns={"contract_number": "contract_number_agg"})
        df = df.merge(c, on="contract_uid", how="left")
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
        # The key carries a nonce that `selected_row` bumps after it reads a click.
        # A selectable st.dataframe keeps its selection in session state forever, so a
        # naive `if selection.rows:` re-fires the modal on EVERY later rerun -- click a
        # row, dismiss it, change a filter, and it springs back. Re-mounting the table
        # under a fresh key is what actually clears the selection, which also means the
        # same row can be clicked a second time and still open.
        kwargs.update(on_select="rerun", selection_mode="single-row",
                      key=f"{key}__{st.session_state.get(f'_sel_nonce_{key}', 0)}")
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


def selected_row(sel, key: str) -> int | None:
    """The row a user just clicked, returned EXACTLY ONCE.

    Pair with `show_table(..., select=True, key=key)`: reading the click bumps that
    table's nonce, so the next rerun mounts a fresh widget with no selection and the
    modal does not reopen on its own. See the note in `show_table`.
    """
    try:
        rows = list(sel.selection.rows)
    except Exception:
        return None
    if not rows:
        return None
    slot = f"_sel_nonce_{key}"
    st.session_state[slot] = st.session_state.get(slot, 0) + 1
    return int(rows[0])


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
# Modals -- a click on a ticker or an event opens the detail, instead of stacking
# another panel onto an already dense page.
# ---------------------------------------------------------------------------------

TICKER_COL = st.column_config.LinkColumn(
    "Ticker", help="Open this company", display_text=r"ticker=([A-Z.]+)")

CONTRACT_COL = st.column_config.LinkColumn(
    "Contract", help="Open this contract", display_text=r"contract=(.+)$")


def ticker_link(v) -> str:
    """A ticker cell rendered as a link. st.dataframe cannot attach a callback to a
    cell, and row-selection swallows the click, so the cell carries a query-param
    URL and the shell opens the modal on the resulting rerun."""
    t = txt(v, "")
    return f"?ticker={t}" if t and t != DASH else ""


def contract_link(v) -> str:
    """A contract cell rendered as a link, addressed by its printed number.

    Streamlit cannot open a dialog from inside another dialog, so every hop in
    Event -> Contract -> Company is a query param plus a rerun: the current modal
    closes and the next one opens. Using the contract NUMBER rather than its uid
    keeps the link legible and lets `display_text` show it without a second column.
    """
    t = txt(v, "")
    return f"?contract={t}" if t and t != DASH else ""


def pretty_date(v) -> str:
    """'Jul 17, 2026'. ISO is for machines and for tables that need to sort."""
    s = iso_date(v)
    if s == DASH:
        return ""
    try:
        return pd.Timestamp(s).strftime("%b %d, %Y").replace(" 0", " ")
    except (ValueError, TypeError):
        return s


def company_name(frames, r) -> str:
    """The name a reader knows the company by, not the string the DoD filed.

    `contractor_raw` is deliberately byte-for-byte as printed -- trailing asterisk,
    'Inc.', division suffix and all -- because it is the join key. That is right for
    the database and wrong for a human, so the resolved parent is preferred here.
    """
    raw = txt(r.get("contractor_raw"))
    ent = frames.get("entities")
    if ent is None or ent.empty or raw == DASH:
        return raw
    row = ent[ent["contractor_raw"] == r.get("contractor_raw")].head(1)
    if row.empty:
        return raw
    for col in ("parent_company", "normalized_name"):
        name = txt(row.iloc[0].get(col))
        if name != DASH:
            return name
    return raw


def display_company(frame) -> "pd.Series":
    """The resolved parent where we have one, the filed name where we do not.

    Vectorised twin of `company_name` for table columns. 'Rockwell Collins Inc.' is a
    join key; 'RTX Corporation' is what a reader is holding.
    """
    raw = frame["contractor_raw"].map(txt)
    if "parent_company" not in frame.columns:
        return raw
    parent = frame["parent_company"].map(lambda v: txt(v, ""))
    return parent.where(parent.astype(bool), raw)


def facts_table(pairs) -> None:
    """Label/value rows. Only rows that actually have a value are rendered -- a modal
    full of em-dashes reads as broken data rather than as absent data."""
    rows = "".join(f"<tr><td>{lbl}</td><td>{val}</td></tr>"
                   for lbl, val in pairs if val and val != DASH)
    if rows:
        st.markdown(f"<table class='dt-facts'>{rows}</table>", unsafe_allow_html=True)


def competition_text(r) -> str:
    """'One bid solicited, one received' beats 'bids_solicited: 1'."""
    sol, rec = r.get("bids_solicited"), r.get("bids_received")
    parts = []
    if not _isna(sol):
        parts.append(f"{int(sol):,} solicited")
    if not _isna(rec):
        parts.append(f"{int(rec):,} received")
    if not parts:
        return ""
    label = ", ".join(parts)
    if not _isna(rec) and int(rec) == 1:
        label += "  ·  sole source"
    return label


def place_text(r) -> str:
    where = txt(r.get("place_of_performance"), "")
    done = pretty_date(r.get("completion_date"))
    if where and done:
        return f"{where}  ·  through {done}"
    return where or (f"through {done}" if done else "")


def event_kind(r) -> str:
    """What this event DID to the contract, in the words a reader thinks in.

    Derived, never stored: `is_creating_event` plus `action_type` already say it, and
    a fourth column repeating them could only drift from them.
    """
    if bool(r.get("is_creating_event")):
        return "Contract created"
    a = txt(r.get("action_type"), "")
    return {"modification": "Modification",
            "option_exercise": "Option exercised",
            "multi_award_pool": "Pool award"}.get(a, "Order placed")


def event_kinds(frame) -> "pd.Series":
    return frame.apply(event_kind, axis=1) if len(frame) else pd.Series(dtype="object")


@st.dialog("Contracts", width="large")
def ticker_dialog(frames, df, ticker: str) -> None:
    """Every contract for one ticker. Opened by clicking the ticker in any table."""
    sub = df[df["ticker"] == ticker].sort_values("announced_date", ascending=False)

    ent = frames.get("entities")
    name, how = ticker, ""
    if ent is not None and not ent.empty:
        row = ent[ent["ticker"] == ticker].head(1)
        if not row.empty:
            r = row.iloc[0]
            name = txt(r["parent_company"], ticker)
            how = txt(r["reasoning"], "")
    st.markdown(
        f"<div class='dt-head'><span class='dt-name'>{name}</span>"
        f"<span class='dt-tkr'>{ticker}</span></div>", unsafe_allow_html=True)

    if sub.empty:
        st.info(f"No contracts for {ticker} in the current export.")
        return

    # How many DoD entities roll up to this ticker, and how many contracts they hold:
    # the resolver's whole job, and the thing a reader cannot see from the ticker
    # alone ("Sikorsky" is LMT).
    units = sorted({txt(n) for n in sub["contractor_raw"].dropna().unique()})
    n_con = sub["contract_uid"].nunique() if "contract_uid" in sub.columns else len(sub)
    st.markdown(
        f"<div class='dt-sub'>{n_con:,} contracts · {len(sub):,} events across "
        f"{len(units)} contracting "
        f"{'entity' if len(units) == 1 else 'entities'}</div>",
        unsafe_allow_html=True)

    metric_row([
        ("Actioned", usd(sub["amount_usd"].sum(min_count=1))),
        ("Largest", usd(sub["amount_usd"].max())),
        ("Alerts", f"{int((sub['tier'] == 'alert').sum()):,}"),
    ])

    # A company holds CONTRACTS, so that is what its view lists. Each contract number
    # links back down into the contract, closing the loop:
    # Event -> Contract -> Company -> Contract.
    con = frames.get("contracts")
    held = None
    if con is not None and not con.empty and "contract_uid" in sub.columns:
        held = con[con["contract_uid"].isin(set(sub["contract_uid"].dropna()))]
    if held is not None and not held.empty:
        held = held.sort_values(["total_actioned_usd", "n_events"],
                                ascending=[False, False], na_position="last")
        show_table(pd.DataFrame({
            "Contract": held["contract_number"].map(contract_link),
            "Events": held["n_events"],
            "Mods": held["n_modifications"],
            "Actioned": held["total_actioned_usd"].map(usd),
            "Ceiling": held["ceiling_usd"].map(usd),
            "Latest": held["last_event_date"].map(pretty_date),
            "History": held["history_complete"].map(
                lambda v: "complete" if v is True else "opens earlier"),
        }), height=300, key=f"tk_{ticker}",
            column_config={
                "Contract": CONTRACT_COL,
                "Events": st.column_config.NumberColumn("Events", format="%d"),
                "Mods": st.column_config.NumberColumn("Mods", format="%d"),
            })
    else:
        show_table(pd.DataFrame({
            "Date": sub["announced_date"].map(pretty_date),
            "Tier": sub["tier"].map(tier_text),
            "Event": event_kinds(sub),
            "Contract": sub["contract_number"].map(contract_link),
            "Amount": sub["amount_usd"].map(usd),
        }), tier_col="Tier", height=300, key=f"tk_{ticker}",
            column_config={"Contract": CONTRACT_COL})
    if how:
        st.caption(f"Why these roll up to {ticker}: {how}")


def contract_timeline(frames, r) -> None:
    """Every event on this event's contract, oldest first. The point of the aggregate.

    Silent when the contract has only this one event: a one-row "history" is noise.
    """
    uid = r.get("contract_uid")
    aw = frames.get("awards")
    if not uid or aw is None or aw.empty or "contract_uid" not in aw.columns:
        return
    sib = aw[aw["contract_uid"] == uid]
    if "duplicate_of" in sib.columns:
        sib = sib[sib["duplicate_of"].isna()]
    if len(sib) < 2:
        return
    sib = sib.sort_values(["announced_date", "award_uid"])

    con = frames.get("contracts")
    head = ""
    if con is not None and not con.empty:
        row = con[con["contract_uid"] == uid].head(1)
        if not row.empty:
            c = row.iloc[0]
            head = f"{int(c['n_events'])} events · {usd(c['total_actioned_usd'])} actioned"
            if not bool(c.get("history_complete", True)):
                head += " · opens before our window"

    st.markdown("<div class='dt-lbl'>CONTRACT HISTORY</div>", unsafe_allow_html=True)
    if head:
        st.caption(head)
    show_table(pd.DataFrame({
        "Date": sib["announced_date"].map(pretty_date),
        "Event": event_kinds(sib),
        "Mod": sib["modification_number"].map(lambda v: txt(v, "")),
        "Amount": sib["amount_usd"].map(usd),
        "Running total": sib["amount_usd"].fillna(0).cumsum().map(usd),
    }), height=min(320, 40 + 35 * len(sib)), key=f"tl_{uid}")


@st.dialog("Contract", width="large")
def contract_dialog(frames, df, number: str, event=None) -> None:
    """One contract: who holds it, what it is worth now, and everything done to it.

    This is where an event click lands, because an event is only meaningful against
    the thing it changed. The company name is a link out to the company view, so the
    domain reads the same way the user navigates it:
    Event -> Contract -> Company.
    """
    con = frames.get("contracts")
    row = None
    if con is not None and not con.empty:
        m = con[con["contract_number"] == number]
        if not m.empty:
            row = m.iloc[0]

    # Membership is the AGGREGATE key, never the printed number: a task order carries
    # its own contract_number and names the vehicle in base_contract_number, so
    # filtering on the number alone returns just the one event you clicked.
    uid = row["contract_uid"] if row is not None else schemas.contract_uid(number)
    if not df.empty and "contract_uid" in df.columns:
        events = df[df["contract_uid"] == uid]
    else:
        events = df[df["contract_number"] == number] if not df.empty else df
    if row is None and (events is None or events.empty):
        st.info(f"No contract `{number}` in the current export.")
        return
    if "duplicate_of" in events.columns:
        events = events[events["duplicate_of"].isna()]
    events = events.sort_values(["announced_date", "award_uid"])

    src = row if row is not None else events.iloc[0]
    ticker = txt(src.get("ticker"), "")
    name = company_name(frames, events.iloc[0] if not events.empty else src)
    badge = f"<span class='dt-tkr'>{ticker}</span>" if ticker and ticker != DASH else ""
    st.markdown(
        f"<div class='dt-head'><span class='dt-name'>{name}</span>{badge}</div>",
        unsafe_allow_html=True)
    st.markdown(
        f"<div class='dt-sub'>{number}  ·  "
        f"{txt(src.get('service_branch'), '').title()}</div>", unsafe_allow_html=True)

    total = src.get("total_actioned_usd") if row is not None else events["amount_usd"].sum(min_count=1)
    left, right = st.columns([1.15, 1])
    with left:
        st.markdown(f"<div class='dt-amt'>{usd(total)}</div>", unsafe_allow_html=True)
        st.caption("actioned to date" + (f" · ceiling {usd(src.get('ceiling_usd'))}"
                                         if not _isna(src.get("ceiling_usd")) else ""))
    with right:
        n = int(src.get("n_events") or len(events))
        st.markdown(
            f"<div style='font-size:1.6rem;font-weight:650'>{n}"
            f"<span style='font-size:.8rem;opacity:.5'> event"
            f"{'' if n == 1 else 's'}</span></div>", unsafe_allow_html=True)
        if row is not None and not bool(src.get("history_complete", True)):
            st.caption("opens before our window")

    work = txt(src.get("work_description"), "")
    if work:
        st.markdown("<div class='dt-lbl'>WHAT IT IS</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='dt-why'>{work}</div>", unsafe_allow_html=True)

    if row is not None and not bool(src.get("history_complete", True)):
        st.info("The earliest event we hold is a modification, so this contract was "
                "created before our 50-day window. Its opening value is unknown and "
                "the total below counts only what we have seen.")

    st.markdown("<div class='dt-lbl'>HISTORY</div>", unsafe_allow_html=True)
    show_table(pd.DataFrame({
        "Date": events["announced_date"].map(pretty_date),
        "Event": event_kinds(events),
        "Mod": events["modification_number"].map(lambda v: txt(v, "")),
        "Tier": events["tier"].map(tier_text),
        "Score": events["score"],
        "Amount": events["amount_usd"].map(usd),
        "Running total": events["amount_usd"].fillna(0).cumsum().map(usd),
    }), tier_col="Tier", height=min(340, 45 + 35 * max(len(events), 1)),
        key=f"cd_{number}",
        column_config={"Score": st.column_config.NumberColumn("Score", format="%d")})

    facts_table([
        ("Awarded by", txt(src.get("contracting_activity"), "")),
        ("Opening value", usd(src.get("initial_value_usd"))
         if row is not None and not _isna(src.get("initial_value_usd")) else ""),
        ("Modifications", str(int(src["n_modifications"]))
         if row is not None and not _isna(src.get("n_modifications")) else ""),
        ("First seen", pretty_date(src.get("first_event_date"))),
        ("Latest event", pretty_date(src.get("last_event_date"))),
    ])

    if ticker and ticker != DASH:
        st.markdown(f"[View {name} and its other contracts →](?ticker={ticker})")

    # The event that was clicked keeps its own reasoning and audit trail, one level
    # down: the contract answers "what is this", the event answers "why now".
    if event is not None:
        with st.expander(f"This event — {event_kind(event)} on "
                         f"{pretty_date(event.get('announced_date'))}"):
            why = txt(event.get("rationale"), "")
            if why:
                st.markdown(f"<div class='dt-why'>{why}</div>", unsafe_allow_html=True)
            drivers = as_list(event.get("drivers"))
            if drivers:
                st.markdown("".join(
                    f"<span class='dt-chip'>{str(d).replace('_', ' ')}</span>"
                    for d in drivers), unsafe_allow_html=True)
            provenance_panel(frames, event)


@st.dialog("Detail", width="large")
def award_dialog(frames, r) -> None:
    """One event, written for someone deciding whether it moves a position.

    Ordering is the whole design: what it is, what it is worth, why it matters, what
    the work is, the contract it belongs to and that contract's history, then the
    mechanics. Identifiers, hashes, cache keys and model names are real and stay
    available -- this product's claim is that every number is traceable -- but they
    live one click down, because a reader who wants the audit trail will look for it
    and a reader who does not should never see it.
    """
    tier = txt(r.get("tier"), "routine").lower()
    ticker = txt(r.get("ticker"), "")
    badge = f"<span class='dt-tkr'>{ticker}</span>" if ticker and ticker != DASH else ""
    st.markdown(
        f"<div class='dt-head'><span class='dt-name'>{company_name(frames, r)}</span>"
        f"{badge}</div>", unsafe_allow_html=True)

    sub = "  ·  ".join(p for p in (
        pretty_date(r.get("announced_date")),
        txt(r.get("service_branch"), "").title(),
        event_kind(r),
    ) if p)
    st.markdown(f"<div class='dt-sub'>{sub}</div>", unsafe_allow_html=True)

    left, right = st.columns([1.15, 1])
    with left:
        st.markdown(f"<div class='dt-amt'>{usd(r.get('amount_usd'))}</div>",
                    unsafe_allow_html=True)
        st.caption(usd_exact(r.get("amount_usd")))
    with right:
        score = r.get("score")
        st.markdown(
            f"<div class='dt-badge dt-{tier}'>{tier.upper()}</div>"
            f"<div style='font-size:1.6rem;font-weight:650;margin-top:.2rem'>"
            f"{'' if _isna(score) else int(score)}"
            f"<span style='font-size:.8rem;opacity:.5'> / 100</span></div>",
            unsafe_allow_html=True)

    why = txt(r.get("rationale"), "")
    if why:
        st.markdown("<div class='dt-lbl'>WHY IT MATTERS</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='dt-why'>{why}</div>", unsafe_allow_html=True)
    drivers = as_list(r.get("drivers"))
    if drivers:
        st.markdown("".join(
            f"<span class='dt-chip'>{str(d).replace('_', ' ')}</span>" for d in drivers),
            unsafe_allow_html=True)

    work = txt(r.get("work_description"), "")
    if work:
        st.markdown("<div class='dt-lbl'>THE WORK</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='dt-why'>{work}</div>", unsafe_allow_html=True)

    st.markdown("<div class='dt-lbl'>CONTRACT</div>", unsafe_allow_html=True)
    mod = txt(r.get("modification_number"), "")
    base = txt(r.get("base_contract_number"), "")
    num = txt(r.get("contract_number"), "")
    # Skills rule R-002: a modification with no new number of its own carries the base
    # contract's number, so printing both reads as two facts when there is only one.
    on_base = f" on {base}" if base and base != num else ""
    facts_table([
        ("Contract", num),
        ("Modification", f"{mod}{on_base}" if mod else ""),
        ("Total contract value", usd(r.get("cumulative_face_value_usd"))
         if not _isna(r.get("cumulative_face_value_usd")) else ""),
        ("Performance", place_text(r)),
        ("Awarded by", txt(r.get("contracting_activity"), "")),
        ("Competition", competition_text(r)),
        ("Pricing", txt(r.get("pricing_type"), "").replace("_", " ")),
        ("Set-aside", "Small business" if r.get("small_business") is True else ""),
        ("Vehicle", "  ·  ".join(p for p in (
            "IDIQ" if r.get("is_idiq") is True else "",
            "Multiple award — the amount is a shared ceiling"
            if r.get("is_multi_award") is True else "") if p)),
    ])

    contract_timeline(frames, r)

    ann = frames.get("announcements")
    if ann is not None and not ann.empty:
        src = ann[ann["announcement_id"] == r.get("announcement_id")]
        url = txt(src.iloc[0]["url"], "") if not src.empty else ""
        if url:
            st.markdown(f"[Read the original announcement ↗]({url})")

    xc = r.get("extraction_confidence")
    if not _isna(xc) and float(xc) < CONFIDENCE_FLOOR:
        st.warning(f"Read this one with care — the extractor was only "
                   f"{float(xc):.0%} confident, below the "
                   f"{CONFIDENCE_FLOOR:.0%} floor, so it is queued for review.")

    with st.expander("Audit trail — sources, models and cache keys"):
        provenance_panel(frames, r)


# ---------------------------------------------------------------------------------
# View 1 -- the contract event log
# ---------------------------------------------------------------------------------

def view_events(frames, df):
    """Tab 1 -- the event log. Every change to every contract, most material first.

    Reads the EVENT table (awards) rather than the detection log (changes): a reader
    wants what the government did to a contract, not when our pipeline happened to
    notice. `changes` remains the tick-diff and lives in the audit trail.
    """
    if df.empty:
        empty_state("awards", "Contract events appear here once the pipeline has run.")
        return

    ev = df[df["duplicate_of"].isna()] if "duplicate_of" in df.columns else df
    ev = ev.assign(_kind=event_kinds(ev))

    kinds = ["Contract created", "Modification", "Option exercised",
             "Order placed", "Pool award"]
    present = [k for k in kinds if (ev["_kind"] == k).any()]
    with st.popover("Filters", width="content"):
        picked = st.multiselect("Event", present, default=present, key="ev_kind")
        tickers_only = st.toggle("Listed companies only", value=False, key="ev_listed")
        tiers = st.multiselect("Tier", TIER_ORDER, default=TIER_ORDER, key="ev_tier")

    f = ev[ev["_kind"].isin(picked)] if picked else ev
    if tickers_only:
        f = f[f["ticker"].notna()]
    if len(tiers) < len(TIER_ORDER):
        f = f[f["tier"].isin(tiers)]
    f = f.sort_values(["score", "announced_date"], ascending=[False, False],
                      na_position="last")

    metric_row([
        ("Events", f"{len(f):,}"),
        ("Contracts touched", f"{f['contract_uid'].nunique():,}"),
        ("Changes to existing", f"{int((f['_kind'] != 'Contract created').sum()):,}"),
        ("Value", usd(f["amount_usd"].sum(min_count=1))),
    ])

    if f.empty:
        st.warning("No events match these filters.")
        return

    with st.container(key="maintable_events"):
        sel = show_table(pd.DataFrame({
            "Date": f["announced_date"].map(pretty_date),
            "Event": f["_kind"],
            "Tier": f["tier"].map(tier_text),
            "Score": f["score"],
            "Ticker": f["ticker"].map(ticker_link),
            "Company": display_company(f),
            "Contract": f["contract_number"].map(txt),
            "Amount": f["amount_usd"].map(usd),
            "Contract to date": f["total_actioned_usd"].map(usd)
            if "total_actioned_usd" in f.columns else DASH,
        }), tier_col="Tier", height=520, key="change_events", select=True,
            column_config={
                "Ticker": TICKER_COL,
                "Score": st.column_config.NumberColumn("Score", format="%d"),
            })

    picked_row = selected_row(sel, "change_events")
    if picked_row is not None:
        row = f.iloc[picked_row]
        number = txt(row.get("contract_number"), "")
        if number:
            contract_dialog(frames, df, number, event=row)
        else:
            # 6 events in the corpus state no contract number at all. They belong to
            # no contract, so there is nothing to navigate to -- show the event.
            award_dialog(frames, row)


def view_contracts(frames, df):
    """Tab 2 -- contracts, the aggregate a company actually holds."""
    con = frames.get("contracts")
    if con is None or con.empty:
        empty_state("contracts", "Contracts appear here once the pipeline has rolled "
                                 "award events up into the vehicles they act on.")
        return

    with st.popover("Filters", width="content"):
        only_multi = st.toggle("Only contracts with a history", value=False,
                               key="ct_multi",
                               help="More than one event recorded against the vehicle.")
        tickers_only = st.toggle("Listed companies only", value=False, key="ct_listed")
        incomplete = st.toggle("Only where history predates our window", value=False,
                               key="ct_incomplete",
                               help="The earliest event we hold is a modification, so "
                                    "the contract's opening value is unknown.")

    # Contracts store the holder as filed; the reader wants the resolved parent.
    f = con.copy()
    ent = frames.get("entities")
    if ent is not None and not ent.empty:
        f = f.merge(ent[["contractor_raw", "parent_company"]].drop_duplicates(
            "contractor_raw"), on="contractor_raw", how="left")
    if only_multi:
        f = f[pd.to_numeric(f["n_events"], errors="coerce") > 1]
    if tickers_only:
        f = f[f["ticker"].notna()]
    if incomplete:
        f = f[f["history_complete"] == False]        # noqa: E712 -- pandas mask
    f = f.sort_values(["total_actioned_usd", "n_events"], ascending=[False, False],
                      na_position="last")

    metric_row([
        ("Contracts", f"{len(f):,}"),
        ("With a history", f"{int((pd.to_numeric(f['n_events'], errors='coerce') > 1).sum()):,}"),
        ("Companies", f"{f['ticker'].nunique():,}"),
        ("Actioned", usd(f["total_actioned_usd"].sum(min_count=1))),
    ])

    if f.empty:
        st.warning("No contracts match these filters.")
        return

    with st.container(key="maintable_contracts"):
        sel = show_table(pd.DataFrame({
            "Contract": f["contract_number"].map(txt),
            "Ticker": f["ticker"].map(ticker_link),
            "Company": display_company(f) if "parent_company" in f.columns
                       else f["contractor_raw"].map(txt),
            "Events": f["n_events"],
            "Mods": f["n_modifications"],
            "Initial": f["initial_value_usd"].map(usd),
            "Actioned": f["total_actioned_usd"].map(usd),
            "Ceiling": f["ceiling_usd"].map(usd),
            "First": f["first_event_date"].map(pretty_date),
            "Last": f["last_event_date"].map(pretty_date),
            "History": f["history_complete"].map(
                lambda v: "complete" if v is True else "opens earlier"),
        }), height=520, key="contracts_table", select=True,
            column_config={
                "Ticker": TICKER_COL,
                "Events": st.column_config.NumberColumn("Events", format="%d"),
                "Mods": st.column_config.NumberColumn("Mods", format="%d"),
            })

    picked = selected_row(sel, "contracts_table")
    if picked is not None:
        contract_dialog(frames, df, txt(f.iloc[picked]["contract_number"], ""))


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

    stamp = _fingerprint()
    live = st.sidebar.toggle(
        "Live", value=True,
        help="Watch the exports and refresh as the pipeline writes. Turn off to hold "
             "the current view steady while you read.")
    if live:
        data_watcher(stamp)
        st.sidebar.caption(f"live · checked every {WATCH_SECONDS:.0f}s")
    else:
        st.sidebar.caption("paused · showing a fixed snapshot")
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
    # Navigation between modals goes through the URL, because Streamlit cannot open a
    # dialog from inside one. Each param is consumed immediately so a later rerun does
    # not reopen the same view forever. A contract link wins over a ticker link when
    # both somehow appear: it is the more specific destination.
    picked_ticker = st.query_params.get("ticker")
    picked_contract = st.query_params.get("contract")
    if picked_ticker:
        del st.query_params["ticker"]
    if picked_contract:
        del st.query_params["contract"]

    if demo:
        st.warning("SYNTHETIC DEMO DATA — not real awards", icon=":material/science:")

    df = enrich(frames)

    if picked_contract:
        contract_dialog(frames, df, str(picked_contract))
    elif picked_ticker:
        ticker_dialog(frames, df, str(picked_ticker))

    tabs = st.tabs(["Events", "Contracts", "Companies", "Review", "Agents"])
    with tabs[0]:
        view_events(frames, df)
    with tabs[1]:
        view_contracts(frames, df)
    with tabs[2]:
        view_company(frames, df)
    with tabs[3]:
        view_review(frames)
    with tabs[4]:
        view_agents(frames)




main()
