"""Rule cases: a fourth agent-ledger kind, `case` (C1, C2, …), and its links to rules.

A case is a real situation where an agent's behaviour was right or wrong
against a rule: the test battery used when a rule is created, reworded, cut
or cold-read. Rules carry the refs of the proposals that shaped them
(`[P41]`), so a case is tied to its rules through those proposals. See
docs/architecture/agent-ledger.md, "Rule cases".

## What changes

- `ledger_entries` gains the case's own fields as columns (`outcome`,
  `expected`, `expected_by`, `owner_words`, `agent_did`), each with a column
  comment naming this revision. `sources`, a quirk's field until now, is
  shared with cases.
- Five CHECKs of 1002 are replaced under the same names (the kind list, the
  ref's letter, the statuses, the quirk fields, the counters' kinds), and
  `ck_ledger_entries_case_fields` is added. `UNIQUE (id, owner_key, kind)`
  lets the link table check both ends' kinds by foreign key.
- `ledger_case_rules`: one row per case–proposal link. Two stored generated
  columns, `case_kind` = 'case' and `rule_kind` = 'proposal', ride in the
  composite foreign keys, so the database itself refuses linking a quirk or
  another owner's entry. Rows are removed by the tools (with a history row),
  never truncated.

Every unit carries a marker comment. 1002's stamp-back rerun leaves exactly
these names with exactly these markers out of its comparison (its
`LATER_COLUMNS` / `LATER_CONSTRAINTS`, byte identical to the literals here).

## Stamp-back rerun

All units present and marked: the columns compared with their declared
shape, the constraints and the table rebuilt in place inside a savepoint
over the rows present and the catalog compared before and after (rolled
back), then accepted with nothing changed. None present: created. Anything in between,
or a unit without its marker, is refused by name with nothing changed.

`downgrade()` refuses while any case entry or case ref counter exists (the
ledger is primary data), else restores 1002's exact constraints (without
comments) and drops what this revision added. `search_path` is pinned;
`lock_timeout` / `statement_timeout` are set and `RESET`, as in 1002.

Revision ID: 1004
Revises: 1003
Create Date: 2026-11-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1004"
down_revision: Union[str, None] = "1003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out here, not imported: a migration keeps describing the schema it
# created after the model moves on. `src/models/db.py` mirrors the column and
# table markers byte for byte; 1002 lists the column and constraint markers.
COLUMN_MARKER = "rule case field (1004_ledger_cases)"
CONSTRAINT_MARKER = "rule case constraint (1004_ledger_cases)"
TABLE_MARKER = "rule case to proposal links (1004_ledger_cases)"

TABLE = "ledger_case_rules"

#: `{column: type}` added to `ledger_entries`, in creation order.
COLUMNS = {
    "outcome": "varchar(16)",
    "expected": "text",
    "expected_by": "varchar(8)",
    "owner_words": "text",
    "agent_did": "text",
}

#: 1002's CHECKs this revision replaces: `{name: (table, 1004's predicate, 1002's predicate)}`.
#: 1002's predicates are its own text, restored by the downgrade.
REPLACED = {
    "ck_ledger_entries_kind": (
        "ledger_entries",
        "kind IN ('quirk', 'feedback', 'proposal', 'case')",
        "kind IN ('quirk', 'feedback', 'proposal')",
    ),
    "ck_ledger_entries_ref": (
        "ledger_entries",
        "ref ~ '^[QFPC][0-9]{1,6}$' "
        "AND substr(ref, 1, 1) = CASE kind WHEN 'quirk' THEN 'Q' WHEN 'feedback' THEN 'F' "
        "WHEN 'proposal' THEN 'P' ELSE 'C' END "
        "AND CAST(substr(ref, 2) AS integer) = number",
        "ref ~ '^[QFP][0-9]{1,6}$' "
        "AND substr(ref, 1, 1) = CASE kind WHEN 'quirk' THEN 'Q' WHEN 'feedback' THEN 'F' ELSE 'P' END "
        "AND CAST(substr(ref, 2) AS integer) = number",
    ),
    "ck_ledger_entries_status": (
        "ledger_entries",
        "status IN ('superseded', 'retracted') "
        "OR (kind = 'quirk' AND status IN ('current', 'stale')) "
        "OR (kind = 'feedback' AND status IN ('to_triage', 'open', 'handled', 'dismissed')) "
        "OR (kind = 'proposal' AND status IN ('accepted', 'adjusted', 'rejected', 'withdrawn')) "
        "OR (kind = 'case' AND status IN ('current', 'retired'))",
        "status IN ('superseded', 'retracted') "
        "OR (kind = 'quirk' AND status IN ('current', 'stale')) "
        "OR (kind = 'feedback' AND status IN ('to_triage', 'open', 'handled', 'dismissed')) "
        "OR (kind = 'proposal' AND status IN ('accepted', 'adjusted', 'rejected', 'withdrawn'))",
    ),
    "ck_ledger_entries_quirk_fields": (
        "ledger_entries",
        "(kind = 'quirk') = (certainty IS NOT NULL) "
        "AND (kind = 'quirk' OR certainty_note IS NULL) "
        "AND (kind IN ('quirk', 'case') OR sources IS NULL)",
        "(kind = 'quirk') = (certainty IS NOT NULL) "
        "AND (kind = 'quirk' OR (certainty_note IS NULL AND sources IS NULL))",
    ),
    "ck_ledger_ref_counters_kind": (
        "ledger_ref_counters",
        "kind IN ('quirk', 'feedback', 'proposal', 'case')",
        "kind IN ('quirk', 'feedback', 'proposal')",
    ),
}

CASE_FIELDS_CHECK = "ck_ledger_entries_case_fields"
UNIQUE_KIND = "uq_ledger_entries_id_owner_kind"

#: Constraints this revision adds: `{name: (table, definition)}`.
ADDED = {
    CASE_FIELDS_CHECK: (
        "ledger_entries",
        "CHECK ("
        "(kind = 'case') = (outcome IS NOT NULL AND expected IS NOT NULL AND expected_by IS NOT NULL) "
        "AND (kind = 'case' OR (outcome IS NULL AND expected IS NULL AND expected_by IS NULL "
        "AND owner_words IS NULL AND agent_did IS NULL)) "
        "AND outcome IN ('slip', 'good', 'overdone') "
        "AND expected_by IN ('owner', 'agent') "
        "AND char_length(btrim(expected)) >= 1 AND char_length(expected) <= 4000 "
        "AND char_length(owner_words) <= 4000 AND char_length(agent_did) <= 4000)",
    ),
    UNIQUE_KIND: ("ledger_entries", "UNIQUE (id, owner_key, kind)"),
}

CONSTRAINTS = {**{name: table for name, (table, _new, _old) in REPLACED.items()},
               **{name: table for name, (table, _def) in ADDED.items()}}

TABLE_DDL = [
    f"""
    CREATE TABLE {TABLE} (
        user_id integer NULL,
        owner_key integer GENERATED ALWAYS AS (COALESCE(user_id, 0)) STORED NOT NULL,
        case_id integer NOT NULL,
        rule_id integer NOT NULL,
        case_kind varchar(16) GENERATED ALWAYS AS ('case') STORED NOT NULL,
        rule_kind varchar(16) GENERATED ALWAYS AS ('proposal') STORED NOT NULL,
        created_at timestamp with time zone NOT NULL DEFAULT now(),
        PRIMARY KEY (case_id, rule_id),
        CONSTRAINT fk_ledger_case_rules_user FOREIGN KEY (user_id) REFERENCES users (id),
        CONSTRAINT fk_ledger_case_rules_case FOREIGN KEY (case_id, owner_key, case_kind)
            REFERENCES ledger_entries (id, owner_key, kind) ON DELETE CASCADE,
        CONSTRAINT fk_ledger_case_rules_rule FOREIGN KEY (rule_id, owner_key, rule_kind)
            REFERENCES ledger_entries (id, owner_key, kind) ON DELETE CASCADE
    )""",
    f"CREATE INDEX ix_ledger_case_rules_rule ON {TABLE} (rule_id)",
    # A link table: the tools remove rows (with a history row), nothing truncates it.
    f"CREATE TRIGGER trg_{TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
    "FOR EACH STATEMENT EXECUTE FUNCTION ledger_refuse_change()",
]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


# ── catalog reads ─────────────────────────────────────────────────────────────

def _column_comments(bind) -> dict[str, str | None]:
    """`{column: comment}` for this revision's columns present on `ledger_entries`."""
    rows = bind.execute(sa.text(
        "SELECT a.attname, col_description(a.attrelid, a.attnum) FROM pg_attribute a "
        "WHERE a.attrelid = CAST('public.ledger_entries' AS regclass) AND a.attnum > 0 "
        "AND NOT a.attisdropped")).all()
    return {name: comment for name, comment in rows if name in COLUMNS}


def _constraint_comments(bind) -> dict[str, str | None]:
    """`{name: comment}` for this revision's constraint names present on their tables."""
    out = {}
    for name, table in CONSTRAINTS.items():
        row = bind.execute(sa.text(
            "SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND conname = :n"), {"t": f"public.{table}", "n": name}).first()
        if row is not None:
            out[name] = row[0]
    return out


def _table_comment(bind) -> tuple[bool, str | None]:
    if not bind.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}).scalar():
        return False, None
    return True, bind.execute(sa.text(
        "SELECT obj_description(CAST(:t AS regclass), 'pg_class')"), {"t": f"public.{TABLE}"}).scalar()


def _shape(bind) -> dict:
    """Everything this revision decides, as the catalog renders it, comparable by name."""
    def rows(query: str, **params) -> list:
        return sorted(tuple(r) for r in bind.execute(sa.text(query), params).all())

    shape = {
        "columns": rows(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "col_description(a.attrelid, a.attnum) FROM pg_attribute a "
            "WHERE a.attrelid = CAST('public.ledger_entries' AS regclass) AND a.attnum > 0 "
            "AND NOT a.attisdropped AND a.attname = ANY(:names)", names=list(COLUMNS)),
        "constraints": [],
    }
    for name, table in CONSTRAINTS.items():
        shape["constraints"] += rows(
            "SELECT conname, contype, pg_get_constraintdef(oid), convalidated, "
            "obj_description(oid, 'pg_constraint') FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND conname = :n", t=f"public.{table}", n=name)
    rel = f"public.{TABLE}"
    shape["table"] = {
        "comment": bind.execute(sa.text("SELECT obj_description(CAST(:r AS regclass), 'pg_class')"),
                                {"r": rel}).scalar(),
        # By name, not position: a rebuild puts nothing in a different order
        # here, but the comparison must not depend on it.
        "columns": rows(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attgenerated, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:r AS regclass) AND a.attnum > 0 AND NOT a.attisdropped", r=rel),
        "constraints": rows(
            "SELECT conname, contype, pg_get_constraintdef(oid), convalidated, condeferrable, condeferred "
            "FROM pg_constraint WHERE conrelid = CAST(:r AS regclass) AND contype <> 't'", r=rel),
        "indexes": rows(
            "SELECT c.relname, pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = CAST(:r AS regclass)", r=rel),
        "triggers": rows(
            "SELECT tgname, pg_get_triggerdef(oid), tgenabled FROM pg_trigger "
            "WHERE tgrelid = CAST(:r AS regclass) AND NOT tgisinternal", r=rel),
    }
    return shape


# ── building ──────────────────────────────────────────────────────────────────

def _create() -> None:
    for column, type_ in COLUMNS.items():
        op.execute(f"ALTER TABLE ledger_entries ADD COLUMN {column} {type_} NULL")
        op.execute(f"COMMENT ON COLUMN ledger_entries.{column} IS {_quote(COLUMN_MARKER)}")
    _create_constraints_and_table()


def _create_constraints_and_table() -> None:
    for name, (table, predicate, _old) in REPLACED.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({predicate})")
        op.execute(f"COMMENT ON CONSTRAINT {name} ON {table} IS {_quote(CONSTRAINT_MARKER)}")
    for name, (table, definition) in ADDED.items():
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} {definition}")
        op.execute(f"COMMENT ON CONSTRAINT {name} ON {table} IS {_quote(CONSTRAINT_MARKER)}")
    for statement in TABLE_DDL:
        op.execute(statement)
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(TABLE_MARKER)}")


#: How the catalog renders each column's type (`format_type`), for the rerun's check.
COLUMN_TYPES = {"varchar(16)": "character varying(16)", "varchar(8)": "character varying(8)", "text": "text"}


def _drop_constraints_and_table() -> None:
    """Drop this revision's constraints (the replaced CHECKs included, not restored) and its table.

    Not its columns: dropping them would drop the cases' content, and the
    CHECKs could not be added back over the rows.
    """
    op.execute(f"DROP TABLE {TABLE}")
    for name, table in CONSTRAINTS.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")


#: The savepoint a stamp-back rerun rebuilds this revision's units in, to compare.
SCRATCH = "ledger_1004_shape"


def _verify_own(bind) -> None:
    """Accept this revision's units only if they are exactly what it creates.

    The columns are compared with their declared shape; the constraints and
    the table are rebuilt in place inside a savepoint over the rows present,
    and the catalog compared before and after.
    """
    live = _shape(bind)
    expected_columns = sorted(
        (column, COLUMN_TYPES[type_], False, COLUMN_MARKER) for column, type_ in COLUMNS.items())
    if live["columns"] != expected_columns:
        raise RuntimeError(
            "1004 will not adopt rule-case units that differ from what it creates (columns): the ledger is "
            "primary data. Inspect them, fix or move them aside by hand, then re-run. Nothing has been changed."
        )
    op.execute(f"SAVEPOINT {SCRATCH}")
    try:
        _drop_constraints_and_table()
        _create_constraints_and_table()
        rebuilt = _shape(bind)
    except sa.exc.DBAPIError as exc:
        op.execute(f"ROLLBACK TO SAVEPOINT {SCRATCH}")
        op.execute(f"RELEASE SAVEPOINT {SCRATCH}")
        raise RuntimeError(
            "1004 found its units but could not rebuild them over the rows present "
            f"({type(exc.orig).__name__ if exc.orig is not None else type(exc).__name__}): something "
            "in the ledger is not what 1004 allows. Inspect it by hand. Nothing has been changed."
        ) from None
    op.execute(f"ROLLBACK TO SAVEPOINT {SCRATCH}")
    op.execute(f"RELEASE SAVEPOINT {SCRATCH}")
    differing = [key for key in ("constraints", "table") if live[key] != rebuilt[key]]
    if differing:
        raise RuntimeError(
            f"1004 will not adopt rule-case units that differ from what it creates ({', '.join(differing)}): "
            "the ledger is primary data. Inspect them, fix or move them aside by hand, then re-run. "
            "Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    columns = _column_comments(bind)
    constraints = _constraint_comments(bind)
    table_present, table_comment = _table_comment(bind)

    # 1002's own CHECKs carry no comment: those names are "not yet replaced".
    replaced_mine = {n for n in REPLACED if constraints.get(n) == CONSTRAINT_MARKER}
    replaced_foreign = sorted(n for n in REPLACED if n in constraints and constraints[n] not in
                              (None, CONSTRAINT_MARKER))
    present = sorted(columns) + sorted(replaced_mine) + sorted(n for n in ADDED if n in constraints) + (
        [TABLE] if table_present else [])
    everything = len(COLUMNS) + len(REPLACED) + len(ADDED) + 1
    unmarked = (
        sorted(c for c, comment in columns.items() if comment != COLUMN_MARKER)
        + sorted(n for n in ADDED if n in constraints and constraints[n] != CONSTRAINT_MARKER)
        + replaced_foreign
        + ([TABLE] if table_present and table_comment != TABLE_MARKER else [])
    )
    if unmarked:
        raise RuntimeError(
            f"1004 found units of its names that do not carry its marker ({', '.join(unmarked)}), so it did "
            "not create them and will not adopt them: the ledger is primary data. Inspect them, move them "
            "aside by hand, then re-run. Nothing has been changed."
        )
    if len(present) == everything:
        _verify_own(bind)
    elif present:
        raise RuntimeError(
            f"1004 found only part of its units ({', '.join(present)}): a half-applied rule-case schema is "
            "not adopted. Inspect it, finish or undo it by hand, then re-run. Nothing has been changed."
        )
    else:
        _create()

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Back to 1002's ledger, only while no case exists.

    Cases are primary data that nothing can rebuild, and a case ref counter
    would break 1002's counter CHECK; an operator who really means it removes
    them first (with the delete switch on), by hand.
    """
    bind = op.get_bind()
    _pin_search_path()
    cases = bind.execute(sa.text("SELECT count(*) FROM ledger_entries WHERE kind = 'case'")).scalar()
    if cases:
        raise RuntimeError(
            f"ledger_entries holds {cases} case entries, which nothing can rebuild. 1004's downgrade refuses "
            "to drop them; export them (scripts/ledger_export.py) and remove them by hand if you mean to. "
            "Nothing has been changed."
        )
    counters = bind.execute(sa.text("SELECT count(*) FROM ledger_ref_counters WHERE kind = 'case'")).scalar()
    if counters:
        raise RuntimeError(
            "ledger_ref_counters holds a counter for cases, which 1002's CHECK does not allow. Remove it by "
            "hand (SET LOCAL ledger.allow_delete = 'on' first) if you mean to. Nothing has been changed."
        )
    columns = _column_comments(bind)
    constraints = _constraint_comments(bind)
    table_present, table_comment = _table_comment(bind)
    foreign = (
        sorted(c for c, comment in columns.items() if comment != COLUMN_MARKER)
        + sorted(n for n, comment in constraints.items() if comment != CONSTRAINT_MARKER)
        + ([TABLE] if table_present and table_comment != TABLE_MARKER else [])
    )
    if foreign:
        raise RuntimeError(
            f"{', '.join(foreign)} {'does' if len(foreign) == 1 else 'do'} not carry 1004's marker, so 1004 did "
            "not create it and will not drop it. Nothing has been changed."
        )
    if table_present:
        op.execute(f"DROP TABLE {TABLE}")
    for name, (table, _def) in ADDED.items():
        if name in constraints:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    for name, (table, _new, old) in REPLACED.items():
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({old})")
    for column in COLUMNS:
        if column in columns:
            op.execute(f"ALTER TABLE ledger_entries DROP COLUMN {column}")
    op.execute("RESET search_path")
