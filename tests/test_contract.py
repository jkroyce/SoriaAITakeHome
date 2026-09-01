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


def test_json_schema_is_strict_mode_shaped():
    obj = schemas.object_schema(schemas.AWARD_FIELDS)
    assert obj["additionalProperties"] is False
    assert set(obj["required"]) == set(obj["properties"])


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
