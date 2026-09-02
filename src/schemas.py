"""The data contract. FROZEN -- do not edit without re-freezing downstream consumers.

This module is the single source of truth for the shape of the system's data. One
field list generates BOTH:

  * the JSON Schema handed to the model via ``output_config.format``
  * the DuckDB DDL used to persist what comes back

so the two can never drift. A contract test (tests/test_contract.py) asserts it.

Why this matters: parallel agents building different sections all code against these
definitions. If extraction and storage each carried their own idea of what an "award"
is, integration would be a merge conflict in data form. Here there is one definition
and two projections of it.

THE DOMAIN, as the UI presents it and as the tables encode it:

    Company  1 --* Contract  1 --* Event
    entities       contracts       awards

A company holds contracts; events change a contract's value, and the first event we
hold reads as the contract being created. `awards` is the event table -- its rows were
always actions (award, modification, option exercise, order), never durable objects.
`contracts` is the aggregate root added in 1.1.0 to give those events something to
belong to. Verified on the 50-document corpus: 1,128 contracts, 40 with multi-event
timelines, and ZERO contracts whose events resolve to more than one company.

RULES FOR AGENTS
  * Never edit this file. If a field is genuinely missing, stop and say so.
    Changing it is a human decision, made once and recorded here -- see the CHANGES
    log at the bottom of this module.
  * ``llm=True`` fields are populated by a model. Everything else is computed by
    deterministic code (ids, timestamps, provenance) and must NOT appear in any
    prompt schema.
  * Adding an ``llm=False`` field leaves every prompt schema byte-identical, so the
    committed model cache stays valid and the change costs nothing to adopt. Adding
    or altering an ``llm=True`` field invalidates that agent's cache and forces
    re-extraction at real cost. Know which one you are doing.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field as dc_field
from typing import Any

SCHEMA_VERSION = "1.1.0"


@dataclass(frozen=True)
class Field:
    """One column, in both of its projections."""
    name: str
    sql: str                       # DuckDB type
    json: dict[str, Any]           # JSON Schema fragment (ignored when llm=False)
    doc: str                       # becomes the schema description AND the SQL comment
    llm: bool = True               # is this populated by the model?


def _s(nullable: bool = True) -> dict:
    return {"type": ["string", "null"]} if nullable else {"type": "string"}


def _int(nullable: bool = True) -> dict:
    return {"type": ["integer", "null"]} if nullable else {"type": "integer"}


def _num(nullable: bool = True) -> dict:
    return {"type": ["number", "null"]} if nullable else {"type": "number"}


def _bool(nullable: bool = True) -> dict:
    return {"type": ["boolean", "null"]} if nullable else {"type": "boolean"}


def _enum(values: list[str], nullable: bool = True) -> dict:
    """A closed set of strings, optionally nullable.

    A nullable enum is expressed as anyOf(enum, null) rather than the more obvious
    {"type": ["string", "null"], "enum": [...,  None]}. Both are valid JSON Schema,
    but the Messages API's structured-output validator rejects a union `type`
    paired with `enum`:

        Invalid schema: Enum value 'ARMY' does not match declared type
        '['string', 'null']'

    Verified against the live API on 2026-09-02: the union form 400s, anyOf is
    accepted, and plain nullable types without an enum are fine -- so this helper
    is the only one that needs the alternate shape. The permitted values and the
    nullability are unchanged, as is the DDL projection.
    """
    if not nullable:
        return {"type": "string", "enum": list(values)}
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}]}


# --------------------------------------------------------------------------------
# awards -- one row per contractor-award. A multi-award pool emits N rows.
# --------------------------------------------------------------------------------

ACTION_TYPES = ["new_award", "modification", "option_exercise", "multi_award_pool"]
BRANCHES = ["ARMY", "NAVY", "AIR FORCE", "DEFENSE LOGISTICS AGENCY", "SPACE FORCE",
            "MARINE CORPS", "DEFENSE", "MISSILE DEFENSE AGENCY", "OTHER"]

AWARD_FIELDS: list[Field] = [
    # -- identity and lineage (deterministic) --
    Field("award_uid", "VARCHAR", {}, "Deterministic id: sha256(announcement_id|contract_number|contractor_raw)[:16]", llm=False),
    Field("announcement_id", "VARCHAR", {}, "war.gov article id this award was extracted from", llm=False),
    Field("announced_date", "DATE", {}, "Date of the announcement that carried this award", llm=False),

    # -- extracted by the model --
    Field("service_branch", "VARCHAR", _enum(BRANCHES),
          "Service section this award appeared under, from the ALL-CAPS header"),
    Field("contractor_raw", "VARCHAR", _s(False),
          "Contractor name exactly as printed, including Inc./LLC/Corp and any trailing asterisk"),
    Field("contractor_city", "VARCHAR", _s(False), "City given for the contractor"),
    Field("contractor_state", "VARCHAR", _s(False), "State or territory given for the contractor"),
    Field("amount_usd", "BIGINT", _int(),
          "Dollar value of THIS action in whole dollars. For a multi-award pool this is the shared ceiling"),
    Field("action_type", "VARCHAR", _enum(ACTION_TYPES, nullable=False),
          "new_award | modification | option_exercise | multi_award_pool"),
    Field("contract_number", "VARCHAR", _s(),
          "Contract number for this contractor, e.g. W15QKN-26-D-A084. Join key into USAspending/FPDS"),
    Field("base_contract_number", "VARCHAR", _s(),
          "For a modification, the underlying contract being modified"),
    Field("modification_number", "VARCHAR", _s(), "Modification id such as P00002, when present"),
    Field("cumulative_face_value_usd", "BIGINT", _int(),
          "Total cumulative face value after this action, when the text states one"),
    Field("pricing_type", "VARCHAR", _s(),
          "Pricing arrangement, e.g. firm-fixed-price, cost-plus-fixed-fee"),
    Field("is_idiq", "BOOLEAN", _bool(),
          "True when described as indefinite-delivery/indefinite-quantity"),
    Field("is_multi_award", "BOOLEAN", _bool(),
          "True when several companies will compete for orders under one shared vehicle"),
    Field("work_description", "VARCHAR", _s(False), "What the contract is for, condensed to one sentence"),
    Field("place_of_performance", "VARCHAR", _s(False),
          "Where work is performed, or a note that locations are determined per order"),
    Field("completion_date", "VARCHAR", _s(), "Estimated completion date as printed (free text)"),
    Field("contracting_activity", "VARCHAR", _s(False), "The contracting activity and its location"),
    Field("bids_solicited", "INTEGER", _int(), "Number of bids solicited, when stated"),
    Field("bids_received", "INTEGER", _int(), "Number of bids received, when stated"),
    Field("small_business", "BOOLEAN", _bool(),
          "True when the entry carries the asterisk marking a small-business award"),
    Field("extraction_confidence", "DOUBLE", _num(False),
          "0.0-1.0. Be honest: below 0.7 routes this row to human review"),
    Field("extraction_notes", "VARCHAR", _s(),
          "Anything ambiguous a reviewer should know. Empty when the entry was unambiguous"),

    # -- provenance of the reasoning (deterministic) --
    Field("extracted_at", "TIMESTAMP", {}, "When extraction ran", llm=False),
    Field("extractor_model", "VARCHAR", {}, "Model id that produced this row", llm=False),
    Field("llm_cache_key", "VARCHAR", {}, "sha256 of the model call; resolves to cache/llm/<key>.json", llm=False),
    Field("skills_version", "VARCHAR", {}, "Version of skills/extraction.md in force for this call", llm=False),

    # -- aggregate membership (deterministic; added in schema 1.1.0) --
    # An award row is an EVENT on a contract. These two say which contract and where
    # in its life the event sits. Both are computed by a GROUP BY in the manager, never
    # by a model -- so they are llm=False and stay out of every prompt, which is what
    # keeps the committed extraction cache valid across this change.
    Field("contract_uid", "VARCHAR", {},
          "FK to contracts: sha256 of the contract number this event acts on", llm=False),
    Field("is_creating_event", "BOOLEAN", {},
          "True on the earliest event we hold for the contract -- the one that reads "
          "as 'contract created'. False on later modifications and orders", llm=False),
    Field("duplicate_of", "VARCHAR", {},
          "Set when this row re-announces an identical earlier event; the surviving "
          "award_uid. Null for the row that survives. Excluded from contract totals",
          llm=False),
]

#: A contract is the aggregate root: a company holds contracts, and events change
#: their value. Every field here is derived from the award rows by GROUP BY, MIN, MAX
#: or SUM -- there is no `llm=True` field in this list and there must never be one.
#: A contract is a fact about rows we already hold, not a judgement.
CONTRACT_FIELDS: list[Field] = [
    Field("contract_uid", "VARCHAR", {}, "sha256(contract_number)[:16]", llm=False),
    Field("contract_number", "VARCHAR", {}, "The PIIN, as printed", llm=False),
    Field("contractor_raw", "VARCHAR", {}, "Holder, as printed on the creating event", llm=False),
    Field("ticker", "VARCHAR", {}, "Resolved public parent, null when private", llm=False),
    Field("service_branch", "VARCHAR", {}, "Branch of the creating event", llm=False),
    Field("work_description", "VARCHAR", {}, "What the contract is for, from its creating event", llm=False),
    Field("contracting_activity", "VARCHAR", {}, "Awarding office", llm=False),
    Field("first_event_date", "DATE", {}, "Earliest event we hold", llm=False),
    Field("last_event_date", "DATE", {}, "Most recent event we hold", llm=False),
    Field("n_events", "INTEGER", {}, "Events on this contract, duplicates excluded", llm=False),
    Field("n_modifications", "INTEGER", {}, "Of those, modifications and option exercises", llm=False),
    Field("initial_value_usd", "BIGINT", {}, "Amount of the creating event", llm=False),
    Field("total_actioned_usd", "BIGINT", {}, "Sum of every event's amount, duplicates excluded", llm=False),
    Field("ceiling_usd", "BIGINT", {}, "Largest stated cumulative face value, null if never stated", llm=False),
    Field("history_complete", "BOOLEAN", {},
          "False when the earliest event we hold is a modification -- the contract was "
          "created before our window and its opening value is unknown", llm=False),
    Field("built_at", "TIMESTAMP", {}, "When this aggregate was last rebuilt", llm=False),
]

ANNOUNCEMENT_FIELDS: list[Field] = [
    Field("announcement_id", "VARCHAR", {}, "war.gov article id", llm=False),
    Field("announced_date", "DATE", {}, "Date parsed from the title", llm=False),
    Field("title", "VARCHAR", {}, "Announcement title", llm=False),
    Field("url", "VARCHAR", {}, "Canonical source URL", llm=False),
    Field("fetched_at", "TIMESTAMP", {}, "When we retrieved it (UTC)", llm=False),
    Field("http_status", "INTEGER", {}, "HTTP status of the fetch", llm=False),
    Field("sha256", "VARCHAR", {}, "Digest of the exact bytes retrieved", llm=False),
    Field("n_bytes", "BIGINT", {}, "Size of the retrieved body", llm=False),
    Field("body_chars", "BIGINT", {}, "Characters of extracted announcement prose", llm=False),
    Field("extraction_status", "VARCHAR", {}, "pending | extracted | failed", llm=False),
]

ENTITY_FIELDS: list[Field] = [
    Field("contractor_raw", "VARCHAR", {}, "Raw name as printed; the cache key", llm=False),
    Field("normalized_name", "VARCHAR", _s(False), "Canonical company name, suffixes normalized"),
    Field("ticker", "VARCHAR", _s(), "Exchange ticker of the ultimate public parent, null if private"),
    Field("parent_company", "VARCHAR", _s(), "Ultimate parent, e.g. Rockwell Collins -> RTX Corporation"),
    Field("relationship", "VARCHAR",
          _enum(["direct", "subsidiary", "joint_venture", "private", "unknown"], nullable=False),
          "How the raw name relates to the ticker"),
    Field("is_public", "BOOLEAN", _bool(False), "True when it maps to a listed company"),
    Field("confidence", "DOUBLE", _num(False), "0.0-1.0. Below 0.7 escalates, then flags for review"),
    Field("reasoning", "VARCHAR", _s(False), "One sentence on how the mapping was determined"),
    Field("resolved_at", "TIMESTAMP", {}, "When resolution ran", llm=False),
    Field("resolver_model", "VARCHAR", {}, "Model that resolved it, or 'deterministic' for an alias hit", llm=False),
    Field("llm_cache_key", "VARCHAR", {}, "Cache key of the resolving call, null for alias hits", llm=False),
    Field("skills_version", "VARCHAR", {}, "Version of skills/entity_resolution.md in force", llm=False),
]

MATERIALITY_FIELDS: list[Field] = [
    Field("award_uid", "VARCHAR", {}, "FK to awards", llm=False),
    Field("score", "INTEGER", _int(False), "0-100 investor relevance"),
    Field("tier", "VARCHAR", _enum(["alert", "notable", "routine"], nullable=False),
          "alert = tell an investor now; notable = worth a look; routine = noise"),
    Field("rationale", "VARCHAR", _s(False), "One sentence an investor would actually find useful"),
    Field("drivers", "JSON", {"type": "array", "items": {"type": "string"}},
          "Short tags for what drove the score, e.g. size_vs_revenue, new_program, recompete_loss"),
    Field("scored_at", "TIMESTAMP", {}, "When scoring ran", llm=False),
    Field("scorer_model", "VARCHAR", {}, "Model that scored it", llm=False),
    Field("llm_cache_key", "VARCHAR", {}, "Cache key of the scoring call", llm=False),
    Field("skills_version", "VARCHAR", {}, "Version of skills/materiality.md in force", llm=False),
]

CHANGE_FIELDS: list[Field] = [
    Field("change_id", "VARCHAR", {}, "Deterministic id of this change event", llm=False),
    Field("detected_at", "TIMESTAMP", {}, "When the diff ran", llm=False),
    Field("change_type", "VARCHAR", {}, "new_announcement | new_award | amount_changed | status_changed", llm=False),
    Field("announcement_id", "VARCHAR", {}, "Announcement involved", llm=False),
    Field("award_uid", "VARCHAR", {}, "Award involved, when applicable", llm=False),
    Field("ticker", "VARCHAR", {}, "Resolved ticker, when applicable", llm=False),
    Field("prev_value", "VARCHAR", {}, "Previous value for a field-level change", llm=False),
    Field("new_value", "VARCHAR", {}, "New value for a field-level change", llm=False),
    Field("materiality_score", "INTEGER", {}, "Score at detection time, for ranking the feed", llm=False),
]

# Every dispatch the manager makes. Makes agent behaviour and spend queryable.
AGENT_RUN_FIELDS: list[Field] = [
    Field("run_id", "VARCHAR", {}, "Unique id for this dispatch", llm=False),
    Field("tick_id", "VARCHAR", {}, "Groups every dispatch from one manager tick", llm=False),
    Field("agent", "VARCHAR", {}, "Registered agent name, e.g. resolve_entity", llm=False),
    Field("item_key", "VARCHAR", {}, "What it worked on (announcement id, award_uid, contractor name)", llm=False),
    Field("model", "VARCHAR", {}, "Model used, or 'deterministic'", llm=False),
    Field("escalated", "BOOLEAN", {}, "True when this was an Opus retry after low confidence", llm=False),
    Field("cache_hit", "BOOLEAN", {}, "True when served from cache with no API call", llm=False),
    Field("input_tokens", "BIGINT", {}, "Input tokens billed, 0 on a cache hit", llm=False),
    Field("output_tokens", "BIGINT", {}, "Output tokens billed, 0 on a cache hit", llm=False),
    Field("cost_usd", "DOUBLE", {}, "Estimated USD for this dispatch", llm=False),
    Field("confidence", "DOUBLE", {}, "Confidence the agent reported, when it reports one", llm=False),
    Field("outcome", "VARCHAR", {}, "ok | escalated | flagged | failed", llm=False),
    Field("error", "VARCHAR", {}, "Error text when outcome is failed", llm=False),
    Field("skills_version", "VARCHAR", {}, "Skills version in force for this dispatch", llm=False),
    Field("started_at", "TIMESTAMP", {}, "Dispatch start", llm=False),
    Field("duration_ms", "BIGINT", {}, "Wall-clock duration", llm=False),
]

REVIEW_QUEUE_FIELDS: list[Field] = [
    Field("review_id", "VARCHAR", {}, "Unique id", llm=False),
    Field("flagged_at", "TIMESTAMP", {}, "When it was flagged", llm=False),
    Field("agent", "VARCHAR", {}, "Agent that gave up on it", llm=False),
    Field("item_key", "VARCHAR", {}, "What needs review", llm=False),
    Field("reason", "VARCHAR", {}, "Why a human is needed", llm=False),
    Field("confidence", "DOUBLE", {}, "Best confidence achieved, after any escalation", llm=False),
    Field("payload", "JSON", {}, "The uncertain result, for a reviewer to accept or correct", llm=False),
    Field("resolved", "BOOLEAN", {}, "True once a human has dealt with it", llm=False),
]

TABLES: dict[str, tuple[list[Field], list[str]]] = {
    "announcements": (ANNOUNCEMENT_FIELDS, ["announcement_id"]),
    "awards": (AWARD_FIELDS, ["award_uid"]),
    "contracts": (CONTRACT_FIELDS, ["contract_uid"]),
    "entities": (ENTITY_FIELDS, ["contractor_raw"]),
    "materiality": (MATERIALITY_FIELDS, ["award_uid"]),
    "changes": (CHANGE_FIELDS, ["change_id"]),
    "agent_runs": (AGENT_RUN_FIELDS, ["run_id"]),
    "review_queue": (REVIEW_QUEUE_FIELDS, ["review_id"]),
}


# --------------------------------------------------------------------------------
# Projections
# --------------------------------------------------------------------------------

def llm_fields(fields: list[Field]) -> list[Field]:
    return [f for f in fields if f.llm]


def object_schema(fields: list[Field]) -> dict:
    """JSON Schema object for the model-populated subset of `fields`.

    Strict-mode shaped: every property required, additionalProperties false.
    Nullability is expressed in the type union, not by omission, so the model must
    say "null" rather than silently dropping a key.

    Both halves matter to the API's structured-output compiler, and each has a
    budget (verified live 2026-09-02):

      * optional properties are the expensive shape -- 19 of them returned
        "Schema is too complex", because the decoder must accept any subset. So
        every property stays required.
      * union types are capped at 16. 22 award fields with 19 nullable exceeded
        it, so the five fields a DoD announcement always states are declared
        non-nullable, leaving 14 unions here plus document_notes.
    """
    props, required = {}, []
    for f in llm_fields(fields):
        props[f.name] = {**f.json, "description": f.doc}
        required.append(f.name)
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}


def extraction_schema() -> dict:
    """Schema for one day-document: many awards plus a document-level note."""
    return {
        "type": "object",
        "properties": {
            "awards": {
                "type": "array",
                "description": "Every award in this announcement, in order. "
                               "A multi-award pool contributes one entry per company.",
                "items": object_schema(AWARD_FIELDS),
            },
            "document_notes": {
                "type": ["string", "null"],
                "description": "Anything about the document as a whole a reviewer should know.",
            },
        },
        "required": ["awards", "document_notes"],
        "additionalProperties": False,
    }


def ddl(table: str) -> str:
    """CREATE TABLE for a registered table, with PK and column comments."""
    if table not in TABLES:
        raise KeyError(f"unknown table {table!r}; known: {sorted(TABLES)}")
    fields, pk = TABLES[table]
    cols = [f"    {f.name} {f.sql}" for f in fields]
    cols.append(f"    PRIMARY KEY ({', '.join(pk)})")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(cols) + "\n);"


def all_ddl() -> str:
    return "\n\n".join(ddl(t) for t in TABLES)


def award_uid(announcement_id: str, contract_number: str | None,
              contractor_raw: str, ordinal: int = 0) -> str:
    """Stable id for an award.

    Contract number is the natural key, but some entries have none -- those fall back
    to their position within the announcement so re-running extraction is idempotent.
    """
    basis = f"{announcement_id}|{contract_number or f'#{ordinal}'}|{contractor_raw.strip().lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def contract_key(contract_number: str | None, base_contract_number: str | None) -> str | None:
    """The contract an event acts on, as a plain string. Deterministic, never a model.

    A modification names the vehicle it amends in `base_contract_number`; a new award
    names its own in `contract_number`. Preferring the base is what makes a
    modification land on the SAME contract as the award that created it -- which is
    the entire point of the aggregate. Returns None when neither is stated, and such
    an event belongs to no contract rather than to a fabricated one.
    """
    for v in (base_contract_number, contract_number):
        s = (v or "").strip()
        if s:
            return s
    return None


def contract_uid(contract_number: str | None, base_contract_number: str | None = None) -> str | None:
    """Stable id for a contract: sha256 of its number, case- and space-normalized.

    Normalizing here and not in `contract_key` keeps the printed number intact for
    display while still collapsing 'w58rgz-24-c-0028' and 'W58RGZ-24-C-0028 ' onto one
    aggregate.
    """
    key = contract_key(contract_number, base_contract_number)
    if not key:
        return None
    return hashlib.sha256(" ".join(key.split()).upper().encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------
# CHANGES -- every edit to this frozen contract, and why it was permitted
# --------------------------------------------------------------------------------
# 1.1.0  Added the `contracts` aggregate and three derived award columns
#        (contract_uid, is_creating_event, duplicate_of). Authorised by the project
#        owner. Every added field is llm=False, so all prompt schemas hash identically
#        to 1.0.0 and the committed cache replays unchanged -- verified, $0 re-spend.
#        Motivation: `award_uid` identifies a line in a press release, not a durable
#        thing, so one contract with six actions was six unrelated rows. See the
#        domain diagram at the top of this module.

if __name__ == "__main__":
    import json
    print(f"schema version {SCHEMA_VERSION}")
    for t, (fs, pk) in TABLES.items():
        print(f"  {t:14s} {len(fs):2d} cols, {len(llm_fields(fs)):2d} model-populated, pk={pk}")
    print(f"\nextraction schema: {len(extraction_schema()['properties']['awards']['items']['properties'])} "
          f"model-populated award fields")
    print(json.dumps(extraction_schema(), indent=2)[:400] + " ...")
