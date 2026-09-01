"""The golden set runner -- the falsifier for the self-improvement layer.

Agents on this project write rules into ``skills/*.md`` and those rules are injected
into later prompts. Nothing else in the system can tell "the agent learned something"
apart from "the agent's prompt drifted and nobody noticed". This runner is what makes
that distinguishable: a fixed, hand-verified statement of what correct extraction of
two real announcements looks like, scored against whatever the extractor currently
produces.

Design, in three points:

1. **The fixtures are ground truth, not recorded behaviour.** Every expected value in
   ``cases/*.json`` was read out of the announcement prose by a human and carries the
   sentence it came from in a ``quote`` field. None of them were produced by running
   the extractor and writing down its answer -- that would make the set a mirror of
   current behaviour and it would agree with any regression.

2. **One code path.** Rows always come from ``agents.extract.extract_document``. The
   only thing that varies is where the model's JSON came from:

     * ``--from cache``  (default) -- ``CachedLLM(live=False)``, replay of a committed
       response. This is the mode the promotion gate uses. It never calls the API: a
       miss raises ``NotCachedError`` and the case is reported BLOCKED, not silently
       skipped and not paid for.
     * ``--from stub DIR`` -- a hand-written payload standing in for the model, so the
       scoring, selection and post-processing logic can be exercised offline while
       ``cache/llm/`` is still empty. Same ``extract_document`` call, same repairs,
       same deterministic id/provenance fill; only the response bytes differ.

   There is deliberately no separate "real mode" to keep in sync.

3. **Failures name the field.** A gate that prints "3 failed" is not a gate anybody
   will act on. Every failure prints the check id, the selector, the field, the
   expected value and the actual value, plus the quoted sentence the expectation came
   from, so a human can adjudicate against the source in seconds.

Exit codes -- both non-zero, so a promotion step can just test the status:

    0   every check passed
    1   at least one check FAILED (a regression)
    2   no failures, but at least one case was BLOCKED (could not be scored)

Usage::

    # score the extractor against the golden set, replaying the committed cache
    .venv/Scripts/python.exe tests/golden/runner.py

    # exercise the runner offline against a hand-written stand-in for model output
    .venv/Scripts/python.exe tests/golden/runner.py --from stub tests/golden/stubs/good
    .venv/Scripts/python.exe tests/golden/runner.py --from stub tests/golden/stubs/bad_rule_divide_pool

    # the promotion gate: baseline vs. the same set with a candidate rule appended
    .venv/Scripts/python.exe tests/golden/runner.py gate tests/golden/candidate_rules/R-BAD-001-divide-pool-ceiling.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable

HERE = pathlib.Path(__file__).resolve().parent          # tests/golden
ROOT = HERE.parent.parent                               # repo root
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import schemas                                          # noqa: E402  the frozen contract
from agents import extract as extract_mod               # noqa: E402
from llm import CachedLLM, NotCachedError               # noqa: E402  never constructed live here

CASES_DIR = HERE / "cases"
PROSE_DIR = HERE / "prose"
STUBS_DIR = HERE / "stubs"
CANDIDATE_RULES_DIR = HERE / "candidate_rules"

WIDTH = 78

#: Only fields that exist in the frozen contract may be asserted on. A fixture naming
#: a field the schema does not have is a broken fixture and says so loudly rather than
#: silently passing because ``row.get(name)`` returned None on both sides.
AWARD_FIELD_NAMES = {f.name for f in schemas.AWARD_FIELDS}


# --------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------

PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"


@dataclass
class CheckResult:
    check_id: str
    status: str
    detail: str = ""
    lines: list[str] = dc_field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    announcement_id: str
    title: str
    status: str
    checks: list[CheckResult] = dc_field(default_factory=list)
    blocked_reason: str = ""
    n_rows: int = 0

    @property
    def n_pass(self) -> int:
        return sum(1 for c in self.checks if c.status == PASS)

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)


@dataclass
class RunResult:
    cases: list[CaseResult] = dc_field(default_factory=list)
    source: str = ""

    @property
    def n_fail(self) -> int:
        return sum(c.n_fail for c in self.cases)

    @property
    def n_pass(self) -> int:
        return sum(c.n_pass for c in self.cases)

    @property
    def n_checks(self) -> int:
        return sum(len(c.checks) for c in self.cases)

    @property
    def blocked(self) -> list[CaseResult]:
        return [c for c in self.cases if c.status == BLOCKED]

    @property
    def exit_code(self) -> int:
        if self.n_fail:
            return 1
        if self.blocked:
            return 2
        return 0


# --------------------------------------------------------------------------------
# loading fixtures and prose
# --------------------------------------------------------------------------------

def load_cases(case_dir: pathlib.Path = CASES_DIR,
               only: Iterable[str] | None = None) -> list[dict]:
    wanted = set(only) if only else None
    cases = []
    for p in sorted(case_dir.glob("*.json")):
        case = json.loads(p.read_text(encoding="utf-8"))
        case["_path"] = p
        if wanted and case["case_id"] not in wanted and p.stem not in wanted:
            continue
        cases.append(case)
    if wanted and not cases:
        raise SystemExit(f"no golden case matched {sorted(wanted)}")
    return cases


def resolve_body(announcement_id: str) -> tuple[str, str]:
    """Announcement prose, plus a one-word note on where it came from.

    Prefers the real cached HTML (``raw/articles/<id>.html``, gitignored and
    regenerable with ``make fetch``) so the prompt -- and therefore the cache key --
    is byte-identical to what the pipeline built. Falls back to the committed prose
    snapshot under ``tests/golden/prose/`` so the set still runs in a fresh clone that
    has never fetched, and warns if the two ever disagree.
    """
    snapshot = PROSE_DIR / f"{announcement_id}.txt"
    html = ROOT / "raw" / "articles" / f"{announcement_id}.html"
    snap_text = snapshot.read_text(encoding="utf-8") if snapshot.exists() else None

    if html.exists():
        live = extract_mod.body_text(html.read_text(encoding="utf-8", errors="replace"))
        if snap_text is not None and live != snap_text:
            print(f"  ! prose snapshot for {announcement_id} differs from raw HTML "
                  f"({len(snap_text)} vs {len(live)} chars) -- the source changed; "
                  f"re-verify the fixtures before trusting this run")
        return live, "raw HTML"
    if snap_text is not None:
        return snap_text, "committed snapshot"
    raise FileNotFoundError(
        f"no prose for announcement {announcement_id}: neither {html} nor {snapshot}")


def prose_digest(announcement_id: str) -> str:
    body, _ = resolve_body(announcement_id)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------
# where the model's JSON comes from
# --------------------------------------------------------------------------------

class StubLLM:
    """A stand-in for ``CachedLLM`` that returns a hand-written payload.

    It exists so the runner can be built and proven offline while ``cache/llm/`` is
    empty. It is NOT a second extraction path: ``extract_document`` still builds the
    prompt, still applies every deterministic repair, and still fills the five
    ``llm=False`` provenance fields. Only the response bytes are substituted.
    """

    def __init__(self, stub_dir: pathlib.Path):
        self.stub_dir = pathlib.Path(stub_dir)
        self.seen: list[str] = []

    def json_call(self, *, model: str, system: str, prompt: str, schema: dict,
                  max_tokens: int, label: str = "") -> dict:
        aid = label.split(":")[-1]
        self.seen.append(aid)
        p = self.stub_dir / f"{aid}.json"
        if not p.exists():
            raise NotCachedError(f"no stub payload {p}")
        return json.loads(p.read_text(encoding="utf-8"))


def _rel(p: pathlib.Path) -> str:
    """Repo-relative path for display, whatever the caller passed in."""
    try:
        return pathlib.Path(p).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return pathlib.Path(p).as_posix()


def make_llm(source: str, stub_dir: pathlib.Path | None):
    if source == "stub":
        if stub_dir is None:
            raise SystemExit("--from stub needs a directory")
        return StubLLM(stub_dir), f"stub payloads in {_rel(stub_dir)}"
    # live is never passed True from here. A cache miss must fail, not spend money.
    return CachedLLM(live=False), "committed cache (cache/llm/), live=False"


def extract_rows(announcement_id: str, llm, *,
                 skills_path: pathlib.Path | None = None) -> list[dict]:
    """The one extraction path. Same call in cache mode and in stub mode."""
    body, _origin = resolve_body(announcement_id)
    ann = extract_mod.announcement_from_manifest(announcement_id)
    ann = dict(ann)
    ann.setdefault("article_id", announcement_id)
    ann["body_text"] = body
    return extract_mod.extract_announcement(ann, llm, skills_path=skills_path)


# --------------------------------------------------------------------------------
# selectors and comparison
# --------------------------------------------------------------------------------

def _strict_eq(actual: Any, expected: Any) -> bool:
    """True/1 and False/0 are different answers; Python's == says they are not."""
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def _match_one(value: Any, spec: Any) -> bool:
    if isinstance(spec, dict):
        if "in" in spec and not any(_strict_eq(value, v) for v in spec["in"]):
            return False
        if "not" in spec and _strict_eq(value, spec["not"]):
            return False
        if "contains" in spec:
            if not isinstance(value, str) or spec["contains"] not in value:
                return False
        if "startswith" in spec:
            if not isinstance(value, str) or not value.startswith(spec["startswith"]):
                return False
        if "is_null" in spec and (value is None) != bool(spec["is_null"]):
            return False
        return True
    return _strict_eq(value, spec)


def select_rows(rows: list[dict], selector: dict) -> list[dict]:
    return [r for r in rows if all(_match_one(r.get(k), v) for k, v in selector.items())]


def _fmt(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return repr(v)
    return str(v)


def _fmt_selector(selector: dict) -> str:
    bits = []
    for k, v in selector.items():
        if isinstance(v, dict):
            op, val = next(iter(v.items()))
            if op == "in" and len(val) > 3:
                bits.append(f"{k} in [{val[0]!r} .. {val[-1]!r}] ({len(val)} values)")
            else:
                bits.append(f"{k} {op} {val!r}")
        else:
            bits.append(f"{k}={_fmt(v)}")
    return ", ".join(bits)


def _row_label(row: dict) -> str:
    return (f"{row.get('contractor_raw')!r} / {row.get('contract_number')!r} "
            f"[{row.get('action_type')}]")


def _unknown_fields(names: Iterable[str]) -> list[str]:
    return sorted(n for n in names if n not in AWARD_FIELD_NAMES)


# --------------------------------------------------------------------------------
# scoring one check
# --------------------------------------------------------------------------------

def run_check(check: dict, rows: list[dict]) -> CheckResult:
    cid = check.get("id", "<unnamed>")
    kind = check.get("kind")
    selector = check.get("select", {})
    quote = check.get("quote", "")
    reason = check.get("reason", "")

    bad = _unknown_fields(list(selector) + list(check.get("expect", {})
                                                if kind == "row" else []))
    if bad:
        return CheckResult(cid, FAIL,
                           f"fixture names field(s) not in schemas.AWARD_FIELDS: {bad}")

    matched = select_rows(rows, selector)
    lines: list[str] = []

    if kind == "count":
        want = check["expect"]
        if len(matched) == want:
            return CheckResult(cid, PASS, f"{want} row(s) match {_fmt_selector(selector)}")
        lines.append(f"selector : {_fmt_selector(selector)}")
        lines.append(f"expected : {want} matching row(s)")
        lines.append(f"actual   : {len(matched)}")
        for r in matched[:8]:
            lines.append(f"           - {_row_label(r)}")
        lines.append(f"quote    : {quote}")
        if reason:
            lines.append(f"because  : {reason}")
        return CheckResult(cid, FAIL, f"row count {len(matched)} != {want}", lines)

    if kind == "absent":
        if not matched:
            return CheckResult(cid, PASS, f"no row matches {_fmt_selector(selector)}")
        lines.append(f"selector : {_fmt_selector(selector)}")
        lines.append(f"expected : no matching row")
        lines.append(f"actual   : {len(matched)} row(s)")
        for r in matched[:8]:
            lines.append(f"           - {_row_label(r)}  amount_usd={_fmt(r.get('amount_usd'))}")
        lines.append(f"quote    : {quote}")
        if reason:
            lines.append(f"because  : {reason}")
        return CheckResult(cid, FAIL, f"{len(matched)} row(s) present that must not be", lines)

    if kind == "row":
        expect = check["expect"]
        if len(matched) != 1:
            lines.append(f"selector : {_fmt_selector(selector)}")
            lines.append(f"expected : exactly 1 matching row")
            lines.append(f"actual   : {len(matched)}")
            for r in matched[:8]:
                lines.append(f"           - {_row_label(r)}")
            lines.append(f"quote    : {quote}")
            if reason:
                lines.append(f"because  : {reason}")
            return CheckResult(cid, FAIL,
                               f"selector matched {len(matched)} rows, need exactly 1", lines)
        row = matched[0]
        wrong = [(f, expect[f], row.get(f)) for f in expect
                 if not _strict_eq(row.get(f), expect[f])]
        if not wrong:
            return CheckResult(cid, PASS,
                               f"{len(expect)} field(s) correct on {_row_label(row)}")
        lines.append(f"row      : {_row_label(row)}")
        for f, want, got in wrong:
            lines.append(f"  field  : {f}")
            lines.append(f"    expected : {_fmt(want)}")
            lines.append(f"    actual   : {_fmt(got)}")
        lines.append(f"quote    : {quote}")
        if reason:
            lines.append(f"because  : {reason}")
        return CheckResult(cid, FAIL,
                           f"{len(wrong)} field(s) wrong: {', '.join(f for f, _, _ in wrong)}",
                           lines)

    return CheckResult(cid, FAIL, f"unknown check kind {kind!r} in fixture")


# --------------------------------------------------------------------------------
# scoring the set
# --------------------------------------------------------------------------------

def run_cases(cases: list[dict], llm, *, skills_path: pathlib.Path | None = None,
              source_label: str = "") -> RunResult:
    result = RunResult(source=source_label)
    rows_by_ann: dict[str, list[dict] | Exception] = {}

    for case in cases:
        aid = str(case["announcement_id"])
        if aid not in rows_by_ann:
            try:
                rows_by_ann[aid] = extract_rows(aid, llm, skills_path=skills_path)
            except (NotCachedError, FileNotFoundError, extract_mod.ExtractionError) as e:
                rows_by_ann[aid] = e

        got = rows_by_ann[aid]
        cr = CaseResult(case["case_id"], aid, case.get("title", ""), PASS)
        if isinstance(got, Exception):
            cr.status = BLOCKED
            cr.blocked_reason = f"{type(got).__name__}: {got}"
            result.cases.append(cr)
            continue

        cr.n_rows = len(got)
        for check in case["checks"]:
            cr.checks.append(run_check(check, got))
        cr.status = FAIL if cr.n_fail else PASS
        result.cases.append(cr)

    return result


# --------------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------------

def _head(title: str) -> None:
    print()
    print("=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def report(result: RunResult, *, verbose: bool = False) -> None:
    _head("GOLDEN SET")
    print(f"source of extraction output : {result.source}")
    print(f"cases                       : {len(result.cases)}")
    print(f"checks                      : {result.n_checks}")

    for i, cr in enumerate(result.cases, 1):
        print()
        print(f"{i}. {cr.case_id}   [announcement {cr.announcement_id}]")
        print(f"   {cr.title}")
        if cr.status == BLOCKED:
            print(f"   [BLOCKED] {cr.blocked_reason}")
            print("             not scored -- a blocked case is not a pass. The gate "
                  "refuses promotion")
            print("             on any blocked case, because it cannot show the rule "
                  "did no harm.")
            continue
        print(f"   {cr.n_rows} award row(s) extracted from this announcement")
        for chk in cr.checks:
            if chk.status == PASS:
                if verbose:
                    print(f"   [PASS] {chk.check_id}: {chk.detail}")
                else:
                    print(f"   [PASS] {chk.check_id}")
            else:
                print(f"   [FAIL] {chk.check_id}: {chk.detail}")
                for line in chk.lines:
                    print(f"          {line}")

    _head("VERDICT")
    n_blocked = len(result.blocked)
    n_case_fail = sum(1 for c in result.cases if c.status == FAIL)
    print(f"checks  : {result.n_pass} passed, {result.n_fail} failed")
    print(f"cases   : {sum(1 for c in result.cases if c.status == PASS)} passed, "
          f"{n_case_fail} failed, {n_blocked} blocked")
    if result.exit_code == 0:
        print("verdict : PASS -- the golden set is green.")
    elif result.exit_code == 1:
        print("verdict : FAIL -- regression. Do not promote. Exit 1.")
        for c in result.cases:
            for chk in c.checks:
                if chk.status == FAIL:
                    print(f"          {c.case_id} / {chk.check_id}: {chk.detail}")
    else:
        print("verdict : BLOCKED -- nothing regressed, but the set could not be scored. "
              "Exit 2.")
        print("          cache/llm/ has no response for these announcements yet. Run the")
        print("          paid extraction pass to populate it, then re-run; or exercise the")
        print("          runner offline with --from stub.")
    print("=" * WIDTH)


# --------------------------------------------------------------------------------
# the promotion gate
# --------------------------------------------------------------------------------

def gate(candidate_rule: pathlib.Path, llm_factory, *,
         cases: list[dict] | None = None, candidate_factory=None) -> int:
    """`manager promote-skills` in miniature: baseline, then the candidate rule.

    The candidate rule is appended to a COPY of ``skills/extraction.md`` in a temp
    file and handed to ``extract_document(skills_path=...)``. The real
    ``skills/extraction.md`` is never written to -- this runner does not own it, and a
    gate that mutates the thing it is gating is not a gate.

    Accept only if the candidate does not reduce the number of passing checks.

    ``candidate_factory`` supplies the extraction output the candidate rule produces.
    In cache mode there is none: the appended rule changes the prompt, so the candidate
    side legitimately misses the cache and the gate reports BLOCKED -- promotion really
    does require paying for re-extraction first. Offline, ``--candidate-stub`` points at
    a payload directory mechanically derived from the rule, which is how the bad-rule
    rejection is demonstrated without spending anything.
    """
    import tempfile

    cases = cases if cases is not None else load_cases()
    rule_text = candidate_rule.read_text(encoding="utf-8")
    candidate_factory = candidate_factory or llm_factory

    _head(f"PROMOTION GATE: {candidate_rule.name}")
    print("candidate rule:")
    for line in rule_text.strip().splitlines():
        print(f"  | {line}")

    llm, label = llm_factory()
    base = run_cases(cases, llm, source_label=label)
    print()
    print(f"baseline (skills/extraction.md as committed): "
          f"{base.n_pass}/{base.n_checks} checks pass, {len(base.blocked)} blocked")

    current = extract_mod.SKILLS_PATH.read_text(encoding="utf-8") if extract_mod.SKILLS_PATH.exists() else ""
    with tempfile.TemporaryDirectory() as td:
        candidate_skills = pathlib.Path(td) / "extraction.md"
        candidate_skills.write_text(current.rstrip() + "\n\n" + rule_text.strip() + "\n",
                                    encoding="utf-8")
        llm2, label2 = candidate_factory()
        cand = run_cases(cases, llm2, skills_path=candidate_skills, source_label=label2)

    print(f"candidate (rule appended)                  : "
          f"{cand.n_pass}/{cand.n_checks} checks pass, {len(cand.blocked)} blocked")
    print(f"  candidate extraction output from: {label2}")

    _head("GATE VERDICT")
    if base.blocked or cand.blocked:
        print("REFUSE -- the golden set could not be scored on one or both sides, so the")
        print("          candidate cannot be shown to do no harm. A rule change alters the")
        print("          prompt, which changes the cache key, which is why the candidate")
        print("          side misses the cache: promotion genuinely requires paying for")
        print("          re-extraction first. That cost is the point of gating.")
        for c in (cand.blocked or base.blocked):
            print(f"          blocked: {c.case_id} -- {c.blocked_reason}")
        print("=" * WIDTH)
        return 2
    if cand.n_pass < base.n_pass:
        print(f"REJECT -- the candidate regresses the golden set "
              f"({base.n_pass} -> {cand.n_pass} passing checks).")
        for c in cand.cases:
            for chk in c.checks:
                if chk.status == FAIL:
                    print(f"          {c.case_id} / {chk.check_id}: {chk.detail}")
        print("=" * WIDTH)
        return 1
    if cand.n_pass == base.n_pass:
        print("UNPROVEN -- same pass rate. Keep the rule only if it addresses a case the")
        print("            golden set does not cover, and say which one.")
        print("=" * WIDTH)
        return 0
    print(f"PROMOTE -- the candidate improves the golden set "
          f"({base.n_pass} -> {cand.n_pass} passing checks).")
    print("=" * WIDTH)
    return 0


# --------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tests/golden/runner.py",
        description="Score extraction output against the hand-verified golden set.")
    p.add_argument("command", nargs="?", default="check", choices=["check", "gate", "list"])
    p.add_argument("rule", nargs="?", help="candidate rule file, for `gate`")
    p.add_argument("--from", dest="source", default="cache", choices=["cache", "stub"],
                   help="where the model's JSON comes from (default: cache)")
    p.add_argument("--stub-dir", dest="stub_dir_pos", default=None,
                   help="stub directory (may also be given positionally after --from stub)")
    p.add_argument("--candidate-stub", default=None,
                   help="for `gate`: the payload directory embodying the candidate rule's "
                        "behaviour, when the cache cannot be paid to produce it")
    p.add_argument("--case", action="append", default=None,
                   help="run only this case id (repeatable)")
    p.add_argument("-v", "--verbose", action="store_true", help="print detail on passes too")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    argv = list(sys.argv[1:] if argv is None else argv)
    # allow `--from stub tests/golden/stubs/good`
    stub_dir = None
    if "--from" in argv:
        i = argv.index("--from")
        if len(argv) > i + 2 and argv[i + 1] == "stub" and not argv[i + 2].startswith("-"):
            stub_dir = pathlib.Path(argv.pop(i + 2))

    args = _parser().parse_args(argv)
    if args.stub_dir_pos:
        stub_dir = pathlib.Path(args.stub_dir_pos)

    cases = load_cases(only=args.case)

    if args.command == "list":
        _head("GOLDEN CASES")
        for c in cases:
            print(f"{c['case_id']}   [announcement {c['announcement_id']}] "
                  f"{len(c['checks'])} check(s)")
            print(f"    {c.get('title','')}")
        print()
        for aid in sorted({str(c["announcement_id"]) for c in cases}):
            body, origin = resolve_body(aid)
            print(f"prose {aid}: {len(body):,} chars, sha256 {prose_digest(aid)[:16]} "
                  f"({origin})")
        return 0

    def factory():
        return make_llm(args.source, stub_dir)

    if args.command == "gate":
        if not args.rule:
            raise SystemExit("gate needs a candidate rule file")
        rule = pathlib.Path(args.rule)
        if not rule.exists():
            rule = CANDIDATE_RULES_DIR / args.rule
        cand_factory = None
        if args.candidate_stub:
            cand_dir = pathlib.Path(args.candidate_stub)
            cand_factory = lambda: make_llm("stub", cand_dir)  # noqa: E731
        return gate(rule, factory, cases=cases, candidate_factory=cand_factory)

    llm, label = factory()
    result = run_cases(cases, llm, source_label=label)
    report(result, verbose=args.verbose)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
