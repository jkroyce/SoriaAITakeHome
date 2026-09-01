"""DuckDB persistence: init, idempotent upserts, exports, and a few thin queries.

THIS MODULE MAKES NO MODEL CALLS. Ever. Persistence is exact, repeatable work --
creating tables, writing rows, diffing keys, sorting, exporting -- and by the rule in
CLAUDE.md that means deterministic code. There is deliberately no `import anthropic`
and no `import llm` here.

Two invariants hold the design together:

1. **Every column name comes from `schemas.TABLES`.** Nothing here hardcodes a column
   list, and the DDL is `schemas.all_ddl()` verbatim. If the contract gains a field,
   this module picks it up with no edit. A hand-written column list is exactly how a
   storage layer drifts away from the contract it is supposed to serve.

2. **Writes are idempotent.** The pipeline re-runs constantly (a cached extraction is
   free, so re-running is the normal case). A re-run must converge, not accumulate.
   DuckDB has no general `ON CONFLICT ... DO UPDATE` covering every case here, so a
   write is `DELETE` the incoming primary keys then `INSERT`, both inside one
   transaction. Honest and total: the new row wins, wholesale.

Type handling worth knowing about (each verified against duckdb 1.5.5, not assumed):

  * An ISO date/timestamp *string* binds straight into `DATE`/`TIMESTAMP`.
  * A **timezone-aware** `datetime` is converted to the machine's *local* zone on the
    way in -- `2026-01-02T03:04:05+00:00` came back as `2026-01-01 22:04:05` on a
    US/Eastern box. So aware values are normalised to naive UTC in Python first.
    Same for ISO strings carrying an offset, whose offset DuckDB simply truncates.
  * `""` into a `DATE` is a conversion error, not a null. Empty strings become NULL
    for every non-VARCHAR column.
  * `JSON` columns take text, so `drivers` / `payload` are `json.dumps`-ed on the way
    in and come back as JSON text.
"""
from __future__ import annotations

import json
import pathlib
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import duckdb

if __package__ in (None, "") and str(pathlib.Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import schemas
from config import DATA, DB_PATH, RAW

# Tables an investor (or the UI) reads directly, so they get CSV as well as Parquet.
# agent_runs and review_queue are operational -- Parquet only.
CSV_TABLES: tuple[str, ...] = ("awards", "entities", "materiality", "changes", "announcements")


# --------------------------------------------------------------------------------
# contract projections -- every name below is derived, never typed out
# --------------------------------------------------------------------------------

def columns(table: str) -> list[str]:
    """Column names for `table`, in contract order."""
    fields, _ = schemas.TABLES[table]
    return [f.name for f in fields]


def primary_key(table: str) -> list[str]:
    return list(schemas.TABLES[table][1])


def sql_types(table: str) -> dict[str, str]:
    fields, _ = schemas.TABLES[table]
    return {f.name: f.sql for f in fields}


# --------------------------------------------------------------------------------
# value coercion
# --------------------------------------------------------------------------------

def _to_utc_naive(v: Any) -> Any:
    """Normalise a timestamp to a naive UTC datetime.

    DuckDB shifts an aware datetime into the local zone and silently truncates a
    string's UTC offset. Both are wrong for provenance, so the conversion happens
    here where it is explicit.
    """
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return s  # let DuckDB have a go at anything exotic
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    return v


def _to_date(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return v


def _to_int(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("$", "")
        if not s:
            return None
        return int(float(s))
    if isinstance(v, float):
        return int(v)
    return v


def _to_float(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        return float(s) if s else None
    return v


def _to_bool(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return None
        return s in ("true", "t", "1", "yes", "y")
    return v


def _to_json(v: Any) -> Any:
    """JSON columns are text. Anything structured is dumped; text already holding
    valid JSON is passed through untouched so a round-trip is a no-op."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            json.loads(v)
            return v
        except (ValueError, TypeError):
            return json.dumps(v)
    return json.dumps(v, default=str)


_COERCE = {
    "DATE": _to_date,
    "TIMESTAMP": _to_utc_naive,
    "JSON": _to_json,
    "BIGINT": _to_int,
    "INTEGER": _to_int,
    "DOUBLE": _to_float,
    "BOOLEAN": _to_bool,
}


def _coerce_row(table: str, row: Mapping[str, Any]) -> list[Any]:
    """One dict -> one positional tuple in contract column order.

    Missing keys become NULL rather than an error: a partially-populated row (an
    award before it has been scored, say) is a normal state, not a bug.
    """
    types = sql_types(table)
    out: list[Any] = []
    for col in columns(table):
        v = row.get(col)
        fn = _COERCE.get(types[col])
        if fn is not None:
            v = fn(v)
        elif isinstance(v, (dict, list)):       # VARCHAR handed structured data
            v = json.dumps(v, default=str)
        out.append(v)
    return out


# --------------------------------------------------------------------------------
# connection and DDL
# --------------------------------------------------------------------------------

def init_db(path: str | pathlib.Path = DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open `path` and ensure every contract table exists. Safe to call repeatedly.

    The DDL is `schemas.all_ddl()` unmodified -- there is no hand-written CREATE
    TABLE anywhere in this file, which is what keeps storage and contract identical.
    """
    if str(path) != ":memory:":
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        path = str(p)
    conn = duckdb.connect(str(path))
    conn.execute(schemas.all_ddl())          # every CREATE is IF NOT EXISTS
    return conn


def table_names() -> list[str]:
    return list(schemas.TABLES)


# --------------------------------------------------------------------------------
# idempotent writes
# --------------------------------------------------------------------------------

def upsert(conn: duckdb.DuckDBPyConnection, table: str,
           rows: Iterable[Mapping[str, Any]]) -> int:
    """Delete-then-insert on the declared primary key, in one transaction.

    Returns the number of rows written. Re-running with the same input is a no-op in
    effect: the row count does not move and the values are replaced with themselves.

    Within a single batch the last row for a given key wins -- otherwise the batch
    would trip the PK constraint, and silently dropping the later value would be the
    less predictable choice.
    """
    if table not in schemas.TABLES:
        raise KeyError(f"unknown table {table!r}; known: {sorted(schemas.TABLES)}")
    rows = list(rows)
    if not rows:
        return 0

    pk = primary_key(table)
    cols = columns(table)

    deduped: dict[tuple, list[Any]] = {}
    for row in rows:
        values = _coerce_row(table, row)
        key = tuple(values[cols.index(k)] for k in pk)
        if any(k is None for k in key):
            raise ValueError(f"{table}: row has NULL in primary key {pk}: {key}")
        deduped[key] = values

    keys = list(deduped)
    payload = list(deduped.values())
    where = " AND ".join(f"{k} = ?" for k in pk)
    placeholders = ", ".join("?" for _ in cols)

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.executemany(f"DELETE FROM {table} WHERE {where}", [list(k) for k in keys])
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", payload)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(payload)


def upsert_announcements(conn, rows) -> int:
    return upsert(conn, "announcements", rows)


def upsert_awards(conn, rows) -> int:
    return upsert(conn, "awards", rows)


def upsert_entities(conn, rows) -> int:
    return upsert(conn, "entities", rows)


def upsert_materiality(conn, rows) -> int:
    return upsert(conn, "materiality", rows)


def upsert_changes(conn, rows) -> int:
    return upsert(conn, "changes", rows)


def upsert_agent_runs(conn, rows) -> int:
    return upsert(conn, "agent_runs", rows)


def upsert_review_queue(conn, rows) -> int:
    return upsert(conn, "review_queue", rows)


def mark_extraction_status(conn: duckdb.DuckDBPyConnection,
                           announcement_id: str, status: str) -> None:
    """pending -> extracted | failed. A targeted UPDATE, not a rewrite of the row."""
    conn.execute("UPDATE announcements SET extraction_status = ? WHERE announcement_id = ?",
                 [status, announcement_id])


def count(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    if table not in schemas.TABLES:
        raise KeyError(f"unknown table {table!r}")
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def counts(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {t: count(conn, t) for t in schemas.TABLES}


# --------------------------------------------------------------------------------
# manifest ingest
# --------------------------------------------------------------------------------

def load_manifest(conn: duckdb.DuckDBPyConnection,
                  manifest_path: str | pathlib.Path = RAW / "manifest.json") -> int:
    """Populate `announcements` from the fetch layer's manifest.

    The manifest nests fetch provenance under `provenance`; the table stores it flat,
    so that object is unpacked into fetched_at / http_status / sha256 / n_bytes.
    `extraction_status` starts at 'pending' -- but only for announcements that are
    new. Re-loading the manifest must not reset a row that has already been
    extracted, so an existing status is carried over.
    """
    manifest_path = pathlib.Path(manifest_path)
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))

    known = {r[0]: r[1] for r in conn.execute(
        "SELECT announcement_id, extraction_status FROM announcements").fetchall()}

    rows = []
    for e in entries:
        prov = e.get("provenance") or {}
        aid = e.get("article_id") or e.get("announcement_id")
        if aid is None:
            raise ValueError(
                f"manifest entry has neither article_id nor announcement_id: {e!r}. "
                "Coercing would write the string 'None' as a primary key.")
        aid = str(aid)
        rows.append({
            "announcement_id": aid,
            "announced_date": e.get("announced_date"),
            "title": e.get("title"),
            "url": e.get("url") or prov.get("url"),
            "fetched_at": prov.get("fetched_at"),
            "http_status": prov.get("http_status"),
            "sha256": prov.get("sha256"),
            "n_bytes": prov.get("n_bytes"),
            "body_chars": e.get("body_chars"),
            "extraction_status": known.get(aid) or "pending",
        })
    return upsert(conn, "announcements", rows)


# --------------------------------------------------------------------------------
# exports -- what gets committed, and what the UI reads
# --------------------------------------------------------------------------------

def _lit(p: pathlib.Path) -> str:
    """A DuckDB string literal for a path. COPY ... TO takes no bind parameter for
    its destination, so the path is quoted by hand -- forward slashes keep Windows
    backslashes from being read as escapes."""
    return "'" + str(p).replace("\\", "/").replace("'", "''") + "'"


_EXPORT_RETRIES = 5


def _copy(conn: duckdb.DuckDBPyConnection, table: str,
          dest: pathlib.Path, options: str) -> None:
    """One COPY, retried briefly on a Windows sharing violation.

    Verified failure mode: DuckDB writes the export to a temp file and *moves* it
    into place. On Windows that move fails with "Could not move file: Access is
    denied" while any other DuckDB connection still holds the previous export open
    -- which is exactly what happens when the Streamlit UI is reading
    `data/awards.csv` as a tick exports over it. A short retry clears a reader that
    is merely mid-query; a reader that holds the file indefinitely cannot be cleared
    from here, so the error says what to do about it instead of surfacing a bare
    IOException from the middle of a pipeline run.
    """
    sql = f"COPY (SELECT * FROM {table}) TO {_lit(dest)} ({options})"
    for attempt in range(_EXPORT_RETRIES):
        try:
            conn.execute(sql)
            return
        except duckdb.IOException:
            if attempt == _EXPORT_RETRIES - 1:
                raise duckdb.IOException(
                    f"could not write {dest}: the file is locked by another process. "
                    "Close anything reading the exports (the Streamlit app, an open "
                    "DuckDB connection, Excel) and re-run the export."
                ) from None
            time.sleep(0.1 * (attempt + 1))


def export(conn: duckdb.DuckDBPyConnection,
           out_dir: str | pathlib.Path = DATA) -> dict[str, str]:
    """Write every table to Parquet, and the investor-facing ones to CSV as well.

    These files are the committed artifact of a run: the UI reads them, and a
    reviewer can diff them between runs to see exactly what changed. Returns
    {name: path} for everything written.
    """
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for table in schemas.TABLES:
        pq = out / f"{table}.parquet"
        _copy(conn, table, pq, "FORMAT PARQUET")
        written[f"{table}.parquet"] = str(pq)
        if table in CSV_TABLES:
            csv = out / f"{table}.csv"
            _copy(conn, table, csv, "FORMAT CSV, HEADER")
            written[f"{table}.csv"] = str(csv)
    return written


# --------------------------------------------------------------------------------
# query helpers -- thin on purpose
# --------------------------------------------------------------------------------

def query(conn: duckdb.DuckDBPyConnection, sql: str,
          params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    """Run SQL, return a list of dicts. The UI can hand these straight to Streamlit
    and pandas can wrap them; neither is a dependency of this module."""
    cur = conn.execute(sql, list(params) if params else [])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def pending_announcements(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Announcements whose prose has not been extracted yet -- the extractor's queue.
    Newest first: recent contracts are the ones an investor cares about."""
    return query(conn, """
        SELECT announcement_id, announced_date, title, url, body_chars
        FROM announcements
        WHERE extraction_status = 'pending'
        ORDER BY announced_date DESC, announcement_id DESC
    """)


def awards_for_ticker(conn: duckdb.DuckDBPyConnection, ticker: str) -> list[dict[str, Any]]:
    """Every award that resolves to one ticker, largest first, with its score."""
    return query(conn, """
        SELECT a.award_uid, a.announced_date, a.service_branch, a.contractor_raw,
               e.normalized_name, e.ticker, e.parent_company, e.relationship,
               a.amount_usd, a.action_type, a.contract_number, a.work_description,
               m.score AS materiality_score, m.tier
        FROM awards a
        JOIN entities e ON e.contractor_raw = a.contractor_raw
        LEFT JOIN materiality m ON m.award_uid = a.award_uid
        WHERE e.ticker = ?
        ORDER BY a.amount_usd DESC NULLS LAST, a.announced_date DESC
    """, [ticker])


def recent_changes(conn: duckdb.DuckDBPyConnection, limit: int = 50) -> list[dict[str, Any]]:
    """The alert feed: newest changes, most material first within a detection batch."""
    return query(conn, f"""
        SELECT {', '.join(columns("changes"))}
        FROM changes
        ORDER BY detected_at DESC, materiality_score DESC NULLS LAST
        LIMIT ?
    """, [int(limit)])


def unresolved_contractors(conn: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Contractor names in `awards` with no row in `entities` yet -- the entity
    resolver's queue. A set difference, which is why no model is involved in
    finding it. Busiest names first: resolving one clears the most rows."""
    return query(conn, """
        SELECT a.contractor_raw,
               count(*)          AS n_awards,
               sum(a.amount_usd) AS total_usd,
               max(a.announced_date) AS last_seen
        FROM awards a
        LEFT JOIN entities e ON e.contractor_raw = a.contractor_raw
        WHERE e.contractor_raw IS NULL
        GROUP BY a.contractor_raw
        ORDER BY n_awards DESC, total_usd DESC NULLS LAST
    """)


def review_queue(conn: duckdb.DuckDBPyConnection,
                 include_resolved: bool = False) -> list[dict[str, Any]]:
    """What a human still has to look at -- least confident first."""
    where = "" if include_resolved else "WHERE resolved IS NOT TRUE"
    return query(conn, f"""
        SELECT {', '.join(columns("review_queue"))}
        FROM review_queue
        {where}
        ORDER BY confidence ASC NULLS FIRST, flagged_at DESC
    """)


if __name__ == "__main__":
    conn = init_db()
    n = load_manifest(conn)
    print(f"announcements loaded: {n}")
    for table, c in counts(conn).items():
        print(f"  {table:14s} {c:5d} rows, {len(columns(table)):2d} cols")
    files = export(conn)
    print(f"exported {len(files)} files to {DATA}")
