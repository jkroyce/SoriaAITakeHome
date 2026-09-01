"""The golden set, as pytest -- including the test that proves the gate works.

Three groups:

* **fixture integrity** -- every expected value names a real field in the frozen
  contract, and every expectation cites a sentence that is actually in the announcement
  prose. This is what makes "hand-verified" checkable by a machine rather than a claim.
* **runner behaviour** -- the runner passes a known-good extraction and fails a
  known-bad one, with a message that names the field.
* **the bad-rule gate** -- a plausible-but-wrong candidate rule is REJECTED. If this
  test can be deleted and everything still passes, the self-improvement layer is
  unfalsifiable and the golden set is decoration.

Everything here runs offline. No API key, no live call, no spend: the runner is only
ever constructed with ``live=False`` or with a stub payload, and one of the tests below
asserts that.

    .venv/Scripts/python.exe -m pytest tests/test_golden.py -q
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
for p in (str(ROOT / "src"), str(GOLDEN)):
    if p not in sys.path:
        sys.path.insert(0, p)

import schemas                       # noqa: E402
import runner                        # noqa: E402  tests/golden/runner.py

CASES = runner.load_cases()
CASE_IDS = [c["case_id"] for c in CASES]

STUB_GOOD = GOLDEN / "stubs" / "good"
BAD_RULES = {
    # candidate rule file            -> the stub directory embodying its behaviour
    "R-BAD-001-divide-pool-ceiling.md": "bad_rule_divide_pool",
    "R-BAD-002-cumulative-is-the-amount.md": "bad_rule_cumulative_as_amount",
    "R-BAD-003-collapse-pool-to-one-row.md": "bad_rule_collapse_pool",
}

AWARD_FIELD_NAMES = {f.name for f in schemas.AWARD_FIELDS}
LLM_FIELD_NAMES = {f.name for f in schemas.llm_fields(schemas.AWARD_FIELDS)}


def _run(stub_dir: pathlib.Path, cases=None) -> runner.RunResult:
    llm, label = runner.make_llm("stub", stub_dir)
    return runner.run_cases(cases if cases is not None else CASES, llm,
                            source_label=label)


# --------------------------------------------------------------------------------
# 1. fixture integrity -- the fixtures are ground truth, and traceable
# --------------------------------------------------------------------------------

def test_the_two_required_cases_exist():
    """The plan names these two by hand. Losing either guts the set."""
    text = " ".join(json.dumps({k: v for k, v in c.items() if k != "_path"})
                    for c in CASES)
    assert any("army-idiq-pool-seven" in cid for cid in CASE_IDS)
    assert any("modification-p00002" in cid for cid in CASE_IDS)
    for cn in [f"W912QR-26-D-A0{n}" for n in range(44, 51)]:
        assert cn in text, f"{cn} missing from the fixtures"
    assert "W9124C-25-D-A003" in text and "P00002" in text


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_case_shape(case):
    for key in ("case_id", "announcement_id", "title", "why", "checks"):
        assert case.get(key), f"{case.get('case_id')} missing {key}"
    assert case["checks"], "a case with no checks asserts nothing"
    for chk in case["checks"]:
        assert chk.get("id"), f"unnamed check in {case['case_id']}"
        assert chk.get("kind") in ("row", "count", "absent")
        assert chk.get("select"), f"{chk['id']} has no selector"
        assert chk.get("quote"), f"{chk['id']} cites no sentence from the source"
        assert chk.get("reason"), f"{chk['id']} does not say why"
        if chk["kind"] == "row":
            assert chk.get("expect"), f"{chk['id']} is a row check with nothing expected"
        if chk["kind"] == "count":
            assert isinstance(chk.get("expect"), int)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixtures_only_name_fields_the_frozen_contract_has(case):
    """A fixture asserting a field schemas.py does not define would pass vacuously
    (None == None) and quietly measure nothing."""
    for chk in case["checks"]:
        for f in chk["select"]:
            assert f in AWARD_FIELD_NAMES, f"{chk['id']}: no such award field {f!r}"
        for f in chk.get("expect", {}) if chk["kind"] == "row" else []:
            assert f in AWARD_FIELD_NAMES, f"{chk['id']}: no such award field {f!r}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixtures_do_not_assert_deterministic_fields(case):
    """award_uid, announced_date and the provenance columns are filled by code, not by
    the model. Asserting them here would be testing our own dict writes, not the
    extraction we are trying to gate."""
    deterministic = AWARD_FIELD_NAMES - LLM_FIELD_NAMES
    for chk in case["checks"]:
        if chk["kind"] != "row":
            continue
        overlap = set(chk.get("expect", {})) & deterministic
        assert not overlap, f"{chk['id']} asserts deterministic field(s) {sorted(overlap)}"


def _quote_fragments(quote: str) -> list[str]:
    """Split a citation into the pieces that must literally appear in the prose.

    Fixture quotes elide with ' ... ' and mark a section header with a literal '\\n'.
    Whitespace is normalised because the prose wraps differently to JSON.
    """
    parts = re.split(r"\.\.\.|\\n", quote)
    return [re.sub(r"\s+", " ", p).strip() for p in parts]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_every_quote_is_really_in_the_announcement(case):
    """The point of 'hand-verified': every expectation traces to text a human can read
    in the source. A quote that is not in the prose is an invented expectation."""
    body, _ = runner.resolve_body(str(case["announcement_id"]))
    flat = re.sub(r"\s+", " ", body)
    for chk in case["checks"]:
        if chk.get("quote_kind") == "summary":
            continue
        for frag in _quote_fragments(chk["quote"]):
            if len(frag) < 15:
                continue
            assert frag in flat, (
                f"{case['case_id']}/{chk['id']}: quoted text is not in "
                f"raw/articles/{case['announcement_id']}.html:\n  {frag[:160]}")


def test_prose_snapshots_match_their_recorded_digests():
    sums = json.loads((GOLDEN / "prose" / "SHA256SUMS.json").read_text(encoding="utf-8"))
    for aid, meta in sums.items():
        text = (GOLDEN / "prose" / f"{aid}.txt").read_text(encoding="utf-8")
        assert len(text) == meta["chars"]
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == meta["sha256"]


def test_prose_snapshot_matches_the_raw_html_when_it_is_present():
    """raw/*.html is gitignored, so this is a no-op in a fresh clone and a real check
    on any machine that has fetched. If it ever fails, the source changed under the
    fixtures and every expectation needs re-verifying by hand."""
    for aid in {str(c["announcement_id"]) for c in CASES}:
        html = ROOT / "raw" / "articles" / f"{aid}.html"
        if not html.exists():
            pytest.skip(f"raw/articles/{aid}.html not fetched in this checkout")
        live = runner.extract_mod.body_text(html.read_text(encoding="utf-8", errors="replace"))
        assert live == (GOLDEN / "prose" / f"{aid}.txt").read_text(encoding="utf-8")


# --------------------------------------------------------------------------------
# 2. the runner scores correctly
# --------------------------------------------------------------------------------

def test_known_good_extraction_passes_every_check():
    res = _run(STUB_GOOD)
    assert not res.blocked, [c.blocked_reason for c in res.blocked]
    assert res.n_fail == 0, [
        f"{c.case_id}/{k.check_id}: {k.detail}"
        for c in res.cases for k in c.checks if k.status == runner.FAIL]
    assert res.n_checks >= 40
    assert res.exit_code == 0


def test_the_seven_company_pool_really_is_seven_rows():
    """Spelled out rather than left implicit in the aggregate: this is the case the
    architecture plan names first."""
    llm, _ = runner.make_llm("stub", STUB_GOOD)
    rows = runner.extract_rows("4586879", llm)
    pool = [r for r in rows if (r.get("contract_number") or "").startswith("W912QR-26-D-A0")]
    assert len(pool) == 7
    assert {r["amount_usd"] for r in pool} == {160_000_000}
    assert all(r["is_multi_award"] is True for r in pool)
    assert all(r["small_business"] is True for r in pool)
    assert len({r["award_uid"] for r in pool}) == 7


def test_the_modification_is_not_a_new_award():
    llm, _ = runner.make_llm("stub", STUB_GOOD)
    rows = runner.extract_rows("4586879", llm)
    mod = [r for r in rows if r["contractor_raw"] == "South Carolina Commission for the Blind"]
    assert len(mod) == 1
    assert mod[0]["action_type"] == "modification"
    assert mod[0]["amount_usd"] == 180_000_000
    assert mod[0]["cumulative_face_value_usd"] == 280_000_000


@pytest.mark.parametrize("stub_name", sorted(BAD_RULES.values()))
def test_bad_extraction_fails_and_says_which_field(stub_name):
    res = _run(GOLDEN / "stubs" / stub_name)
    assert res.exit_code == 1, f"{stub_name} was not rejected"
    assert res.n_fail > 0
    details = " ".join(k.detail for c in res.cases for k in c.checks
                       if k.status == runner.FAIL)
    lines = " ".join(l for c in res.cases for k in c.checks for l in k.lines)
    assert "amount_usd" in details or "row count" in details or "matched 0 rows" in details
    assert "expected" in lines and "actual" in lines, "failure output names no values"


def test_failure_output_prints_field_expected_and_actual(capsys):
    res = _run(GOLDEN / "stubs" / "bad_rule_divide_pool")
    runner.report(res)
    out = capsys.readouterr().out
    assert "[FAIL] a044-aaecon" in out
    assert "field  : amount_usd" in out
    assert "expected : 160000000" in out
    assert "actual   : 22857142" in out
    assert "quote    :" in out


def test_a_fixture_naming_a_nonexistent_field_is_a_failure_not_a_pass():
    """Guards the guard: a typo'd field name must not pass silently."""
    broken = {"id": "typo", "kind": "row", "select": {"contract_numbre": "x"},
              "quote": "", "reason": ""}
    assert runner.run_check(broken, []).status == runner.FAIL


def test_true_is_not_one_and_false_is_not_zero():
    rows = [{"small_business": True, "bids_solicited": 1}]
    assert runner.select_rows(rows, {"small_business": 1}) == []
    assert runner.select_rows(rows, {"small_business": True}) == rows
    assert runner.select_rows([{"bids_received": 0}], {"bids_received": False}) == []


# --------------------------------------------------------------------------------
# 3. the bad-rule gate -- the acceptance test for this whole section
# --------------------------------------------------------------------------------

@pytest.mark.parametrize("rule_file,stub_name", sorted(BAD_RULES.items()))
def test_a_deliberately_bad_rule_is_rejected(rule_file, stub_name, capsys):
    """A plausible, well-formed, case-citing candidate rule that is WRONG must be
    caught. Baseline is the good extraction; the candidate side is the extraction that
    this rule's own arithmetic produces (derived mechanically in build_stubs.py).

    Return code 1 is the gate saying REJECT. If this ever returns 0, a bad rule can be
    promoted and the self-improvement layer has no falsifier."""
    rule = GOLDEN / "candidate_rules" / rule_file
    assert rule.exists()

    code = runner.gate(
        rule,
        lambda: runner.make_llm("stub", STUB_GOOD),
        cases=CASES,
        candidate_factory=lambda: runner.make_llm("stub", GOLDEN / "stubs" / stub_name),
    )
    out = capsys.readouterr().out
    assert code == 1, f"{rule_file} was NOT rejected by the golden set"
    assert "REJECT" in out
    assert "regresses the golden set" in out


def test_a_harmless_rule_is_not_rejected(capsys):
    """The gate must discriminate. A rule that changes nothing about the extraction
    comes back UNPROVEN (exit 0), not REJECT -- otherwise 'REJECT' means nothing."""
    rule = GOLDEN / "candidate_rules" / "R-NOOP-000-harmless.md"
    code = runner.gate(rule, lambda: runner.make_llm("stub", STUB_GOOD), cases=CASES,
                       candidate_factory=lambda: runner.make_llm("stub", STUB_GOOD))
    out = capsys.readouterr().out
    assert code == 0
    assert "UNPROVEN" in out


def test_bad_stubs_are_derived_from_the_good_one_not_hand_written():
    """The bad-rule test is only honest if the bad extraction is what the rule actually
    prescribes, rather than something chosen to fail. Re-derive and compare."""
    sys.path.insert(0, str(GOLDEN / "stubs"))
    import build_stubs  # noqa: E402

    for rule_file, stub_name in BAD_RULES.items():
        fn = build_stubs.BAD_RULES[stub_name]
        for aid in build_stubs.GOOD:
            on_disk = json.loads((GOLDEN / "stubs" / stub_name / f"{aid}.json")
                                 .read_text(encoding="utf-8"))
            assert on_disk == fn(build_stubs.GOOD[aid]), (
                f"{stub_name}/{aid}.json is not what {fn.__name__}() produces")


# --------------------------------------------------------------------------------
# 4. the hard constraint: this costs nothing
# --------------------------------------------------------------------------------

def test_cache_mode_is_never_live():
    llm, label = runner.make_llm("cache", None)
    assert llm.live is False
    assert "live=False" in label


def test_no_code_in_the_golden_set_can_go_live():
    """Patterns are assembled from parts so that this file, which has to name them,
    is not itself a match."""
    forbidden = ["live" + "=True", "ANTHROPIC" + "_API_KEY", "--" + "live"]
    for p in sorted(GOLDEN.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        for pat in forbidden:
            assert pat not in src, f"{p.name} contains {pat!r}"


def test_a_cache_miss_blocks_rather_than_spending():
    """With cache/llm/ empty the set reports BLOCKED and exits 2. Blocked is not a pass
    and it is not a silent skip: the gate refuses promotion on it."""
    from llm import CACHE_DIR
    if CACHE_DIR.exists() and any(CACHE_DIR.glob("*.json")):
        pytest.skip("cache is warm; the miss path cannot be exercised here")
    llm, label = runner.make_llm("cache", None)
    res = runner.run_cases(CASES, llm, source_label=label)
    assert res.blocked
    assert res.exit_code == 2
