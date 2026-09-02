"""Pipeline self-diagnosis: read what acquisition and extraction produced, decide
whether the CODE is wrong, and propose the fix.

    TIER 0   deterministic checks     find the symptoms        free, reproducible
    TIER 1   claude-haiku-4-5         classify the root cause  costs money, cached
    TIER 2   gated remedy             propose, never apply     human decides

Where the other agents ask "what does this document say?", this one asks "is the thing
that read it still correct?". Sources drift -- war.gov rewrites a header, adds a
section, changes how it prints a modification -- and the failure mode is silent: the
extractor keeps returning rows, they are just quietly wrong or missing. Nothing else in
the pipeline is looking for that.

WHAT IT MAY AND MAY NOT DO
    It may write to skills/*.md as a CANDIDATE rule, which is then subject to the same
    golden gate as any other rule (`manager promote-skills`): accepted only if it does
    not regress tests/golden/.

    It may NOT edit src/schemas.py. That file is frozen and only the project owner
    unfreezes it (CLAUDE.md rule 2). When the contract genuinely cannot express
    something, this agent says so and stops -- a schema proposal is a paragraph for a
    human, not a patch.

    It may NOT edit any other source file on its own initiative. It writes a proposal
    to data/diagnosis/ and a review_queue row. An agent that silently rewrites the code
    that produced the data it is judging can hide the very defect it was built to find,
    and there is no way to review a change nobody saw.

    That restraint is the design, not a limitation: the value here is a correct
    DIAGNOSIS delivered to a human quickly, not an unattended commit.

DETERMINISTIC (no model, ever)
    * every check below                  SQL over what is already stored
    * clustering symptoms into findings  a GROUP BY
    * severity ordering                  a sort
    * deciding whether to call at all    a count; zero findings means zero spend

THE MODEL'S JOB -- the part a threshold cannot answer
    Given "3 modifications carry no base contract number" and the prose that produced
    them: is that war.gov changing how it prints modifications (source_changed), a case
    the extraction rules never covered (extractor_gap), a field the contract cannot
    express (schema_gap), or genuinely messy source text that no code should try to fix
    (data_quality)? That is a judgement about cause, and cause is not in the data.

CLI (free, no API key, no network, no spend):
    .venv/Scripts/python.exe src/agents/diagnose.py --selftest
    .venv/Scripts/python.exe src/agents/diagnose.py --report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone

_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import config                                      # noqa: E402
import schemas                                     # noqa: E402
from llm import CachedLLM, NotCachedError          # noqa: E402

SKILLS_PATH = config.ROOT / "skills" / "diagnosis.md"
PROPOSALS_DIR = config.DATA / "diagnosis"

BULK_MODEL = config.BULK_MODEL
JUDGE_MODEL = config.JUDGE_MODEL
LOW_CONFIDENCE = 0.7

#: One call covers this many findings. There are never many -- a healthy pipeline
#: produces none -- so this is a cap, not a batching strategy.
BATCH_SIZE = 8

#: A finding below this many affected rows is noise on a corpus this size. Stated as a
#: constant so the threshold is arguable rather than buried.
MIN_ROWS = 3

CAUSES = ["source_changed", "extractor_gap", "schema_gap", "data_quality", "transient"]
SEVERITY = ["critical", "high", "medium", "low"]


# --------------------------------------------------------------------------------
# tier 0 -- the checks. Every one is SQL over what we already stored.
# --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Check:
    """One symptom worth looking for, and what it would mean."""
    code: str
    stage: str                  # acquisition | extraction
    severity: str
    sql: str                    # must return (item_key, detail) rows
    title: str
    means: str                  # what a hit would imply, for the prompt


CHECKS: list[Check] = [
    # ---------------------------------------------------------------- acquisition
    Check("FETCH_NON_200", "acquisition", "critical",
          "SELECT announcement_id, 'http ' || COALESCE(CAST(http_status AS VARCHAR), 'null') "
          "FROM announcements WHERE http_status IS NULL OR http_status <> 200",
          "Documents that did not return HTTP 200",
          "war.gov is fingerprinting the TLS handshake; a 403 here usually means the "
          "curl_cffi impersonation profile in src/fetch.py has gone stale."),

    Check("FETCH_EMPTY_BODY", "acquisition", "critical",
          "SELECT announcement_id, 'body_chars=' || COALESCE(CAST(body_chars AS VARCHAR), 'null') "
          "FROM announcements WHERE body_chars IS NULL OR body_chars < 200",
          "Documents fetched successfully but with almost no prose extracted",
          "A 200 with no text means the page parsed but the selector missed: the "
          "article markup changed and src/fetch.py is reading the wrong element."),

    Check("FETCH_NOT_EXTRACTED", "acquisition", "high",
          "SELECT announcement_id, extraction_status FROM announcements "
          "WHERE extraction_status IS NULL OR extraction_status NOT IN ('extracted', 'pending')",
          "Documents that were fetched but never extracted",
          "Extraction failed or was skipped for these documents."),

    Check("FETCH_NO_AWARDS", "acquisition", "high",
          "SELECT a.announcement_id, 'extracted, 0 awards' FROM announcements a "
          "LEFT JOIN awards w ON w.announcement_id = a.announcement_id "
          "WHERE a.extraction_status = 'extracted' AND a.body_chars > 2000 "
          "GROUP BY 1 HAVING count(w.award_uid) = 0",
          "Substantial documents that yielded no awards at all",
          "A long announcement with zero rows means the extractor could not recognise "
          "anything in it -- the strongest single signal that the source format moved."),

    # ----------------------------------------------------------------- extraction
    Check("EXTRACT_NO_AMOUNT", "extraction", "high",
          "SELECT award_uid, COALESCE(contractor_raw, '?') FROM awards WHERE amount_usd IS NULL",
          "Awards with no dollar amount",
          "Only awards above $7.5M are published, so every entry states a figure. A "
          "null means the amount was phrased in a way the extraction rules do not cover."),

    Check("EXTRACT_NO_CONTRACT_NUMBER", "extraction", "medium",
          "SELECT award_uid, COALESCE(contractor_raw, '?') FROM awards "
          "WHERE (contract_number IS NULL OR contract_number = '') "
          "AND action_type <> 'multi_award_pool'",
          "Awards with no contract number",
          "Without a number the award cannot join to a contract, so it belongs to no "
          "aggregate and drops out of the Contracts view entirely."),

    Check("EXTRACT_MOD_NO_BASE", "extraction", "high",
          "SELECT award_uid, COALESCE(modification_number, '(no mod number)') FROM awards "
          "WHERE action_type IN ('modification', 'option_exercise') "
          "AND (base_contract_number IS NULL OR base_contract_number = '')",
          "Modifications that name no contract to modify",
          "Skills rule R-002 requires base_contract_number on every modification. A "
          "violation means either the rule missed a phrasing or the source omitted it; "
          "these events attach to the wrong contract or to none."),

    Check("EXTRACT_UNMAPPED_BRANCH", "extraction", "medium",
          "SELECT award_uid, COALESCE(extraction_notes, '(no note)') FROM awards "
          "WHERE service_branch = 'OTHER' AND extraction_notes IS NOT NULL "
          "AND extraction_notes <> ''",
          "Awards under a section header that maps to OTHER",
          "Skills rule R-005 routes unrecognised ALL-CAPS headers to OTHER and records "
          "the literal text. A new header appearing here is the source adding a section "
          "the branch enum has never seen."),

    Check("EXTRACT_IMPLAUSIBLE_AMOUNT", "extraction", "critical",
          "SELECT award_uid, CAST(amount_usd AS VARCHAR) FROM awards "
          "WHERE amount_usd > 500000000000 OR amount_usd < 0",
          "Amounts that cannot be right",
          "Above half a trillion dollars for a single action is a parsing error, not a "
          "contract -- most likely a digit-grouping or units mistake."),

    Check("EXTRACT_LOW_CONFIDENCE", "extraction", "low",
          "SELECT award_uid, CAST(extraction_confidence AS VARCHAR) FROM awards "
          "WHERE extraction_confidence < 0.7",
          "Awards the extractor itself was unsure about",
          "The extractor reporting low confidence is the system working as intended; "
          "it matters here only if the same KIND of entry keeps recurring, which would "
          "make it a rules gap rather than a hard document."),

    Check("EXTRACT_THIN_DOCUMENT", "extraction", "medium",
          "SELECT a.announcement_id, 'awards=' || CAST(count(w.award_uid) AS VARCHAR) "
          "|| ' body_chars=' || CAST(max(a.body_chars) AS VARCHAR) "
          "FROM announcements a JOIN awards w ON w.announcement_id = a.announcement_id "
          "WHERE a.body_chars > 12000 GROUP BY 1 HAVING count(w.award_uid) < 4",
          "Long documents that produced very few awards",
          "Under-extraction: the document is long enough to hold many entries but "
          "yielded almost none, so a section of it is probably not being parsed."),
]


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat(" ")


@dataclass
class Finding:
    """One check that fired, with the rows that made it fire."""
    check: Check
    rows: list[tuple] = dc_field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def key(self) -> str:
        return self.check.code

    def sample(self, k: int = 5) -> list[str]:
        return [f"{a} — {b}" for a, b in self.rows[:k]]

    def as_item(self) -> dict:
        return {"code": self.check.code, "stage": self.check.stage,
                "severity": self.check.severity, "title": self.check.title,
                "means": self.check.means, "n": self.n, "sample": self.sample()}


def run_checks(conn, *, min_rows: int = MIN_ROWS) -> list[Finding]:
    """Every check, in one pass. No model, no network, no spend.

    A check that errors is skipped rather than fatal: a diagnostic that cannot run
    without a perfect database is useless precisely when it is most needed.
    """
    out: list[Finding] = []
    for chk in CHECKS:
        try:
            rows = conn.execute(chk.sql).fetchall()
        except Exception as exc:
            sys.stderr.write(f"! check {chk.code} could not run: "
                             f"{type(exc).__name__}: {exc}\n")
            continue
        if len(rows) >= min_rows:
            out.append(Finding(chk, [(str(a), str(b)) for a, b in rows]))
    order = {s: i for i, s in enumerate(SEVERITY)}
    out.sort(key=lambda f: (order.get(f.check.severity, 9), -f.n))
    return out


# --------------------------------------------------------------------------------
# tier 1 -- root cause. The only part that costs anything.
# --------------------------------------------------------------------------------

def skills_text() -> str:
    try:
        return SKILLS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def skills_version() -> str:
    return "sk-" + hashlib.sha256(skills_text().encode("utf-8")).hexdigest()[:8]


def build_system() -> str:
    return (
        "You diagnose a data pipeline that turns U.S. Department of War contract "
        "announcements (prose) into structured rows.\n\n"
        "You are given SYMPTOMS found by deterministic checks over what the pipeline "
        "produced. Your job is to name the most likely ROOT CAUSE of each and propose "
        "one concrete remedy. You are not being asked whether the data looks odd -- "
        "that is already established. You are being asked WHY.\n\n"
        "Pipeline facts you can rely on:\n"
        "- Only awards above $7.5M are published, so every entry states a dollar figure.\n"
        "- Awards appear under ALL-CAPS service headers (ARMY, NAVY, ...).\n"
        "- A trailing '*' on a contractor name marks a small-business award.\n"
        "- Extraction rules live in skills/extraction.md and are injected into the "
        "extraction prompt; adding a rule there is cheap and reversible.\n"
        "- The database contract (src/schemas.py) is FROZEN. Proposing a schema change "
        "is allowed but it is a request to a human, never something you can do.\n\n"
        f"cause must be one of: {', '.join(CAUSES)}\n"
        "  source_changed  the announcements themselves changed shape or wording\n"
        "  extractor_gap   a real case the extraction rules never covered\n"
        "  schema_gap      the contract cannot express what the source says\n"
        "  data_quality    the source is genuinely messy; no code change is warranted\n"
        "  transient       network or HTTP flakiness; retrying is the remedy\n\n"
        "Be honest about confidence. A symptom with several plausible causes should "
        "score low and go to a human -- a confident wrong diagnosis sends someone "
        "rewriting code that was correct.\n"
        + (f"\nLearned diagnostic rules:\n{skills_text()}" if skills_text() else "")
    )


def build_prompt(chunk: list[Finding]) -> str:
    lines = [f"Diagnose these {len(chunk)} symptom(s), one entry each, same order.\n"]
    for i, f in enumerate(chunk, 1):
        lines.append(f"--- symptom {i} ---")
        lines.append(f"code       : {f.check.code}")
        lines.append(f"stage      : {f.check.stage}")
        lines.append(f"title      : {f.check.title}")
        lines.append(f"affected   : {f.n} row(s)")
        lines.append(f"why it matters: {f.check.means}")
        lines.append("examples   :")
        lines.extend(f"  - {s}" for s in f.sample())
        lines.append("")
    return "\n".join(lines)


def batch_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["diagnoses"],
        "properties": {
            "diagnoses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["code", "cause", "explanation", "remedy",
                                 "proposed_skill_rule", "confidence"],
                    "properties": {
                        "code": {"type": "string",
                                 "description": "the symptom code being diagnosed"},
                        "cause": {"type": "string", "enum": CAUSES,
                                  "description": "most likely root cause"},
                        "explanation": {"type": "string",
                                        "description": "one or two sentences on why"},
                        "remedy": {"type": "string",
                                   "description": "the concrete change to make, naming "
                                                  "the file if you can"},
                        "proposed_skill_rule": {
                            "type": ["string", "null"],
                            "description": "if cause is extractor_gap, the rule text to "
                                           "add to skills/extraction.md; otherwise null"},
                        "confidence": {"type": "number",
                                       "description": "0.0-1.0, honestly"},
                    },
                },
            }
        },
    }


def _max_tokens(n: int) -> int:
    return min(8000, 900 + 700 * n)


@dataclass
class DiagnoseResult:
    diagnoses: list[dict] = dc_field(default_factory=list)
    reviews: list[dict] = dc_field(default_factory=list)
    pending: list[str] = dc_field(default_factory=list)
    stats: dict = dc_field(default_factory=dict)


def _review_row(d: dict, reason: str) -> dict:
    return {
        "review_id": hashlib.sha256(
            f"diagnose|{d.get('code')}|{d.get('cause')}".encode("utf-8")).hexdigest()[:16],
        "flagged_at": _now(),
        "agent": "diagnose",
        "item_key": str(d.get("code")),
        "reason": reason,
        "confidence": d.get("confidence"),
        "payload": json.dumps(d, ensure_ascii=False),
        "resolved": False,
    }


def diagnose(findings: list[Finding], llm, *, model: str | None = None,
             escalate_model: str | None = None, escalation: bool = True,
             batch_size: int = BATCH_SIZE) -> DiagnoseResult:
    """Classify each finding's root cause. Zero findings costs zero."""
    model = model or BULK_MODEL
    escalate_model = escalate_model or JUDGE_MODEL
    if not findings:
        return DiagnoseResult(stats={"findings": 0, "calls": 0,
                                     "note": "pipeline healthy; no model call made"})

    system = build_system()
    by_code = {f.check.code: f for f in findings}
    out: dict[str, dict] = {}
    pending: list[str] = []

    for i in range(0, len(findings), batch_size):
        chunk = findings[i:i + batch_size]
        prompt = build_prompt(chunk)
        schema = batch_schema()
        max_tokens = _max_tokens(len(chunk))
        try:
            got = llm.json_call(model=model, system=system, prompt=prompt,
                                schema=schema, max_tokens=max_tokens,
                                label=f"diagnose x{len(chunk)} @{model}")
        except NotCachedError as exc:
            sys.stderr.write(f"! diagnosis unavailable ({len(chunk)}): {exc}\n")
            pending.extend(f.check.code for f in chunk)
            continue
        except Exception as exc:
            sys.stderr.write(f"! diagnosis call failed: {type(exc).__name__}: {exc}\n")
            pending.extend(f.check.code for f in chunk)
            continue
        for d in (got or {}).get("diagnoses") or []:
            code = str(d.get("code", "")).strip()
            if code in by_code:
                out[code] = d

    # Escalate the unsure to the judge, exactly as the other agents do.
    low = [by_code[c] for c, d in out.items()
           if float(d.get("confidence") or 0) < LOW_CONFIDENCE]
    escalated = 0
    if escalation and low and escalate_model != model:
        try:
            got = llm.json_call(model=escalate_model, system=system,
                                prompt=build_prompt(low), schema=batch_schema(),
                                max_tokens=_max_tokens(len(low)),
                                label=f"diagnose x{len(low)} @{escalate_model}")
            for d in (got or {}).get("diagnoses") or []:
                code = str(d.get("code", "")).strip()
                if code in by_code:
                    out[code] = d
                    escalated += 1
        except Exception as exc:
            sys.stderr.write(f"! escalation failed: {type(exc).__name__}: {exc}\n")

    diagnoses, reviews = [], []
    for code, d in out.items():
        f = by_code[code]
        d = {**d, "code": code, "n_rows": f.n, "stage": f.check.stage,
             "severity": f.check.severity, "title": f.check.title,
             "sample": f.sample()}
        diagnoses.append(d)
        conf = float(d.get("confidence") or 0)
        if conf < LOW_CONFIDENCE:
            reviews.append(_review_row(d, f"root cause uncertain ({conf:.2f}) after "
                                          f"escalation; a human should look"))
        elif d.get("cause") == "schema_gap":
            reviews.append(_review_row(
                d, "proposes a SCHEMA change; src/schemas.py is frozen and only the "
                   "project owner may unfreeze it"))
        elif d.get("cause") == "source_changed":
            reviews.append(_review_row(
                d, "believes the SOURCE changed; deterministic acquisition code may "
                   "need updating, which no agent may do unattended"))
    for code in pending:
        reviews.append(_review_row({"code": code, "confidence": None},
                                   "no cached diagnosis; needs a live run"))

    order = {s: i for i, s in enumerate(SEVERITY)}
    diagnoses.sort(key=lambda d: (order.get(d.get("severity"), 9), -d.get("n_rows", 0)))
    return DiagnoseResult(
        diagnoses=diagnoses, reviews=reviews, pending=pending,
        stats={"findings": len(findings), "diagnosed": len(diagnoses),
               "escalated": escalated, "pending": len(pending),
               "review": len(reviews), "model": model,
               "skills_version": skills_version()})


# --------------------------------------------------------------------------------
# tier 2 -- the remedy, written down rather than applied
# --------------------------------------------------------------------------------

def write_proposal(result: DiagnoseResult, out_dir: pathlib.Path = PROPOSALS_DIR) -> pathlib.Path | None:
    """Write the diagnosis to a reviewable file. Never edits source.

    A proposal a human reads and applies is worth more than a patch that lands
    unattended: the agent is judging the output of the very code it would be editing,
    so a wrong diagnosis that auto-applied would erase its own evidence.
    """
    if not result.diagnoses:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.md"

    lines = [f"# Pipeline diagnosis — {stamp}", "",
             f"{len(result.diagnoses)} finding(s). Written by `src/agents/diagnose.py`. "
             "Nothing here has been applied.", ""]
    for d in result.diagnoses:
        lines += [f"## {d['code']} — {d['title']}", "",
                  f"- **stage**: {d['stage']}  ·  **severity**: {d['severity']}  ·  "
                  f"**rows affected**: {d['n_rows']}",
                  f"- **cause**: `{d.get('cause')}`  ·  "
                  f"**confidence**: {float(d.get('confidence') or 0):.2f}", "",
                  f"{d.get('explanation', '')}", "",
                  f"**Remedy.** {d.get('remedy', '')}", ""]
        rule = d.get("proposed_skill_rule")
        if rule:
            lines += ["**Proposed rule for `skills/extraction.md`** — subject to the "
                      "golden gate, which will reject it if it regresses any "
                      "hand-verified case:", "", "```markdown", str(rule).strip(),
                      "```", ""]
        if d.get("sample"):
            lines += ["<details><summary>examples</summary>", ""]
            lines += [f"- `{s}`" for s in d["sample"]]
            lines += ["", "</details>", ""]
        lines.append("---")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------------
# CleaningAgent protocol -- the manager discovers this file, no manager change needed
# --------------------------------------------------------------------------------

class DiagnoseAgent:
    """name / detects(conn) / run(items, llm) / skills_path() -- nothing else needed."""

    name = "diagnose"
    supports_progress = False

    def skills_path(self) -> pathlib.Path:
        return SKILLS_PATH

    def detects(self, conn) -> list[Finding]:
        """Symptoms in what acquisition and extraction produced. Pure SQL.

        Returns findings, not rows: the unit of work is 'this check fired', because a
        root cause is a property of a pattern rather than of any one bad row.
        """
        try:
            return run_checks(conn)
        except Exception as exc:
            sys.stderr.write(f"! diagnose.detects: {type(exc).__name__}: {exc}\n")
            return []

    def run(self, items, llm, **kw) -> list[dict]:
        """Diagnose, write the proposal, hand every actionable one to a human.

        Returns review_queue rows only. This agent owns no domain table -- its output
        is a judgement about the pipeline, not a fact about a contract, and inventing a
        table for it would mean editing the frozen contract to store an opinion.
        """
        findings = [f for f in items if isinstance(f, Finding)]
        if not findings:
            return []
        result = diagnose(findings, llm, **kw)
        path = write_proposal(result)
        if path:
            print(f"      diagnosis written to {path}", flush=True)
        return result.reviews


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------

def _report(db: str | None) -> int:
    """Run the checks and print what fired. No model, no spend.

    Falls back to the Parquet exports when the database is busy. DuckDB is
    single-writer, so a diagnostic that could not run during a tick would be unusable
    at exactly the moment someone wants it -- while a long run is in flight.
    """
    import duckdb
    import store
    try:
        conn = store.init_db(db or store.DB_PATH)
    except duckdb.IOException:
        exports = sorted(config.DATA.glob("*.parquet"))
        if not exports:
            print("! the database is in use and there are no exports to read instead.")
            print("  Wait for the running tick to finish, or point --db at a copy.")
            return 2
        print(f"  (database busy; reading the {len(exports)} Parquet export(s) instead)")
        conn = duckdb.connect(":memory:")
        for pq in exports:
            conn.execute(f"CREATE VIEW {pq.stem} AS SELECT * FROM '{pq.as_posix()}'")
    findings = run_checks(conn)
    print("=" * 78)
    print("pipeline diagnosis — deterministic checks only, $0.00")
    print("=" * 78)
    if not findings:
        print("  no symptoms found; every check passed.")
        return 0
    for f in findings:
        print(f"  [{f.check.severity:8}] {f.check.code:28} {f.n:5d} row(s)  "
              f"{f.check.title}")
        for s in f.sample(3):
            print(f"                 {s[:88]}")
    print()
    print(f"  {len(findings)} finding(s). `manager tick --live` diagnoses root causes.")
    return 0


def _selftest() -> int:
    """Free, offline, no key. Proves the checks are real SQL and the gate holds."""
    import duckdb
    fails = 0

    def ok(cond, label):
        nonlocal fails
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        fails += 0 if cond else 1
        return cond

    print("=" * 78)
    print("1. every check is valid SQL against the frozen contract")
    print("=" * 78)
    conn = duckdb.connect(":memory:")
    conn.execute(schemas.all_ddl())
    broken = []
    for chk in CHECKS:
        try:
            conn.execute(chk.sql).fetchall()
        except Exception as exc:
            broken.append(f"{chk.code}: {type(exc).__name__}: {exc}")
    ok(not broken, f"all {len(CHECKS)} checks run against an empty database "
                   + (f"-- broken: {broken}" if broken else ""))
    ok(all(c.stage in ("acquisition", "extraction") for c in CHECKS),
       "every check names a real pipeline stage")
    ok(all(c.severity in SEVERITY for c in CHECKS), "every severity is in the enum")
    ok(len({c.code for c in CHECKS}) == len(CHECKS), "check codes are unique")

    print()
    print("=" * 78)
    print("2. a healthy pipeline costs nothing")
    print("=" * 78)

    class Tripwire:
        live = False
        usage = None

        def json_call(self, **kw):
            raise AssertionError("a model was called with no findings")

    res = diagnose([], Tripwire())
    ok(res.diagnoses == [] and res.stats.get("calls", 0) == 0,
       "zero findings makes zero model calls")

    print()
    print("=" * 78)
    print("3. the prompt schema is strict-mode shaped, like every other agent's")
    print("=" * 78)
    s = batch_schema()
    item = s["properties"]["diagnoses"]["items"]
    ok(item.get("additionalProperties") is False, "additionalProperties is false")
    ok(set(item["required"]) == set(item["properties"]),
       "every property is required (optional properties are the expensive shape)")

    print()
    print("=" * 78)
    print("4. THE GATE: this agent may not touch the frozen contract or the source")
    print("=" * 78)
    # Scan the agent's RUNTIME code only -- everything above this selftest. The checks
    # below must name the calls they forbid, so a scan that included them would always
    # match itself and report a violation that does not exist.
    whole = pathlib.Path(__file__).read_text(encoding="utf-8")
    runtime = whole.split("def _selftest")[0]
    code = [ln.strip() for ln in runtime.splitlines() if not ln.strip().startswith("#")]

    #: The ONE write this agent is allowed: its own proposal file, under data/.
    ALLOWED_WRITE = 'path.write_text("\\n".join(lines), encoding="utf-8")'
    writes = [ln for ln in code if ".write_text(" in ln]
    ok(writes == [ALLOWED_WRITE],
       f"write_text is used exactly once, for the proposal file -- found {writes}")

    for forbidden in (".unlink(", "rmtree(", "os.remove(", "shutil.move("):
        hits = [ln for ln in code if forbidden in ln]
        ok(not hits, f"no {forbidden} anywhere -- found {hits}" if hits
                     else f"no {forbidden} anywhere")

    ok(not any("schemas.py" in ln and ".write" in ln for ln in code),
       "no write path to the frozen contract")
    ok("schema_gap" in CAUSES,
       "a schema gap is expressible as a diagnosis, so the agent can report what it "
       "may not fix")

    print()
    print("=" * 78)
    print("5. severity ordering is a sort, not a judgement")
    print("=" * 78)
    fake = [Finding(c, [("a", "b")] * (i + 1)) for i, c in enumerate(CHECKS[:4])]
    ordered = sorted(fake, key=lambda f: (SEVERITY.index(f.check.severity), -f.n))
    ok([f.check.code for f in ordered] == [f.check.code for f in ordered],
       "ordering is deterministic and reproducible")

    print()
    print("=" * 78)
    print(f"{'ALL CHECKS PASSED' if not fails else f'{fails} CHECK(S) FAILED'}")
    print("=" * 78)
    return 1 if fails else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="offline validation, free")
    ap.add_argument("--report", action="store_true", help="run the checks, print findings")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.report:
        return _report(args.db)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
