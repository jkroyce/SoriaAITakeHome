"""The contract test: proves schemas.py's two projections cannot silently drift.

src/schemas.py generates BOTH the JSON Schema sent to the model AND the DuckDB DDL
from one field list. That guarantee is what lets parallel agents integrate. This test
is what makes the guarantee real instead of aspirational.

Runs offline, costs nothing.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import schemas  # noqa: E402


def test_every_table_generates_ddl():
    for table in schemas.TABLES:
        sql = schemas.ddl(table)
        assert sql.startswith(f"CREATE TABLE IF NOT EXISTS {table}")
        assert "PRIMARY KEY" in sql


def test_ddl_contains_every_column():
    for table, (fields, _) in schemas.TABLES.items():
        sql = schemas.ddl(table)
        for f in fields:
            assert f" {f.name} " in sql, f"{table}.{f.name} missing from DDL"


def test_json_schema_contains_exactly_the_llm_fields():
    """A field the model fills must be in the prompt schema; one it must not fill
    must be absent. Getting this backwards means either the model inventing ids and
    timestamps, or silently dropped data."""
    for fields in (schemas.AWARD_FIELDS, schemas.ENTITY_FIELDS, schemas.MATERIALITY_FIELDS):
        obj = schemas.object_schema(fields)
        props = set(obj["properties"])
        assert props == {f.name for f in fields if f.llm}
        for f in fields:
            if not f.llm:
                assert f.name not in props, f"{f.name} is computed; it must not be in a prompt"


def _is_union(spec: dict) -> bool:
    return "anyOf" in spec or isinstance(spec.get("type"), list)


def test_json_schema_is_strict_mode_shaped():
    """Closed object, every property required.

    Not merely stylistic: the API's structured-output compiler treats optional
    properties as the expensive shape -- 19 of them returns "Schema is too
    complex", because the decoder must accept every subset. Nullability is
    carried by the type union instead, so the model must say "null" rather than
    silently dropping a key.
    """
    obj = schemas.object_schema(schemas.AWARD_FIELDS)
    assert obj["additionalProperties"] is False
    assert set(obj["required"]) == set(obj["properties"])


def test_prompt_schema_stays_under_the_union_cap():
    """The API rejects a structured-output schema with more than 16 union-typed
    parameters ("limit: 16 parameters with unions"). The awards schema sits just
    under it, so a newly-nullable field is exactly the change that would break
    extraction -- and it would break it at request time, not here, without this.
    """
    unions = []

    def walk(node, path="root"):
        if isinstance(node, dict):
            if _is_union(node):
                unions.append(path)
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schemas.extraction_schema())
    assert len(unions) <= 16, (
        f"{len(unions)} union-typed params exceeds the API cap of 16: {unions}")


def test_nullable_enums_avoid_the_union_type_form():
    """A union `type` paired with `enum` is rejected outright -- "Enum value
    'ARMY' does not match declared type '['string', 'null']'" -- so _enum emits
    anyOf(enum, null) instead."""
    for table, (fields, _pk) in schemas.TABLES.items():
        for f in fields:
            j = f.json
            if "enum" in j:
                assert not isinstance(j.get("type"), list), (
                    f"{table}.{f.name} uses the rejected union+enum form")


def test_every_field_is_documented():
    """Descriptions are how the model knows what a field means; a blank one is a bug."""
    for table, (fields, _) in schemas.TABLES.items():
        for f in fields:
            assert f.doc.strip(), f"{table}.{f.name} has no description"


def test_extraction_schema_shape():
    s = schemas.extraction_schema()
    assert s["properties"]["awards"]["type"] == "array"
    item = s["properties"]["awards"]["items"]
    assert item["properties"] == schemas.object_schema(schemas.AWARD_FIELDS)["properties"]


def test_award_uid_is_deterministic_and_distinguishing():
    a = schemas.award_uid("4586879", "W15QKN-26-D-A084", "Action Manufacturing Co.")
    b = schemas.award_uid("4586879", "W15QKN-26-D-A084", "Action Manufacturing Co.")
    assert a == b, "same inputs must give the same id, or re-runs duplicate rows"
    assert a != schemas.award_uid("4586879", "W912QR-26-D-A044", "Action Manufacturing Co.")
    # A multi-award pool: same announcement, same shared ceiling, different companies.
    c1 = schemas.award_uid("4586879", "W912QR-26-D-A044", "AAECON General Contracting LLC")
    c2 = schemas.award_uid("4586879", "W912QR-26-D-A045", "Amerifield LLC")
    assert c1 != c2, "pool members must not collide"


def test_uid_falls_back_when_no_contract_number():
    """Some entries carry no contract number; position keeps them distinct and stable."""
    x = schemas.award_uid("123", None, "Some Corp", ordinal=0)
    y = schemas.award_uid("123", None, "Some Corp", ordinal=1)
    assert x != y
    assert x == schemas.award_uid("123", None, "Some Corp", ordinal=0)


# --------------------------------------------------------------------------------
# 1.1.0 -- the Company -> Contract -> Event aggregate
# --------------------------------------------------------------------------------

def test_contract_uid_collapses_spellings_of_one_number():
    """A contract is identified by its number, however that number was printed."""
    a = schemas.contract_uid("W58RGZ-24-C-0028")
    assert a == schemas.contract_uid("w58rgz-24-c-0028")
    assert a == schemas.contract_uid("  W58RGZ-24-C-0028  ")
    assert a != schemas.contract_uid("W58RGZ-24-C-0029")


def test_a_modification_lands_on_the_contract_it_amends():
    """The whole aggregate rests on this: a mod joins its base, not its own number.

    Skills rule R-002 gives a modification the base contract's number when no new one
    is printed, so both spellings must resolve to the same contract.
    """
    base = schemas.contract_uid("N0001924G0010")
    assert schemas.contract_uid(None, "N0001924G0010") == base
    assert schemas.contract_uid("H9240826FE010", "N0001924G0010") == base, (
        "a task order must belong to its parent vehicle, not to itself")


def test_an_event_with_no_contract_number_belongs_to_no_contract():
    assert schemas.contract_uid(None, None) is None
    assert schemas.contract_uid("", "  ") is None


def test_contracts_are_wholly_derived_and_never_reasoned():
    """No field on `contracts` may be model-populated: it is a GROUP BY, not a judgement."""
    assert schemas.llm_fields(schemas.CONTRACT_FIELDS) == [], (
        "a contract is a fact about rows we already hold; if a field here needs a "
        "model, it belongs on awards or materiality instead")
    assert schemas.object_schema(schemas.CONTRACT_FIELDS)["properties"] == {}


def test_the_aggregate_columns_did_not_change_any_prompt():
    """1.1.0 must be free to adopt: adding llm=False fields cannot touch a prompt.

    If this fails, the committed cache is invalidated and every document must be
    re-extracted at real cost -- which is a decision, not an accident.
    """
    for name in ("contract_uid", "is_creating_event", "duplicate_of"):
        field = next(f for f in schemas.AWARD_FIELDS if f.name == name)
        assert field.llm is False
    prompt_cols = set(schemas.object_schema(schemas.AWARD_FIELDS)["properties"])
    assert prompt_cols.isdisjoint({"contract_uid", "is_creating_event", "duplicate_of"})
