"""A fourth rule-case outcome: `friction` (P386).

A case's outcome was `slip`, `good` or `overdone` (1004). `friction` records a
rule that got in an agent's way, by the agent's own reading: not a judgment of
the agent or the rule. See docs/architecture/agent-ledger.md, "Rule cases".

## What changes

- `ck_ledger_entries_case_fields` (1004's) is replaced under the same name by
  the same predicate with `'friction'` added to the outcomes, commented with
  this revision's marker. Nothing else changes.
- 1002's and 1004's stamp-back reruns leave this name with this marker out of
  their comparisons (their allow-lists name it, byte identical to the literals
  here): this revision verifies it.

## Stamp-back rerun

The CHECK carrying this revision's marker: rebuilt in place inside a
savepoint over the rows present, the catalog compared before and after,
rolled back; accepted with nothing changed when identical, refused otherwise.
Carrying 1004's marker: replaced. Missing, or carrying another comment:
refused by name, nothing changed.

`downgrade()` refuses while any case has the outcome `friction` (the ledger is
primary data), else restores 1004's CHECK with 1004's text and marker.
`search_path` is pinned; `lock_timeout` / `statement_timeout` are set and
`RESET`, as in 1002.

Revision ID: 1006
Revises: 1005
Create Date: 2026-11-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1006"
down_revision: Union[str, None] = "1005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out here, not imported: a migration keeps describing the schema it
# created after the model moves on. `src/models/db.py` mirrors the CHECK text;
# 1002 and 1004 list the name with this marker.
CONSTRAINT_MARKER = "rule case constraint (1006_ledger_case_friction)"
NAME = "ck_ledger_entries_case_fields"
TABLE = "ledger_entries"

PREDICATE = (
    "(kind = 'case') = (outcome IS NOT NULL AND expected IS NOT NULL AND expected_by IS NOT NULL) "
    "AND (kind = 'case' OR (outcome IS NULL AND expected IS NULL AND expected_by IS NULL "
    "AND owner_words IS NULL AND agent_did IS NULL)) "
    "AND outcome IN ('slip', 'good', 'overdone', 'friction') "
    "AND expected_by IN ('owner', 'agent') "
    "AND char_length(btrim(expected)) >= 1 AND char_length(expected) <= 4000 "
    "AND char_length(owner_words) <= 4000 AND char_length(agent_did) <= 4000"
)

#: What this revision replaces, restored by the downgrade: 1004's text and marker.
PREDICATE_1004 = (
    "(kind = 'case') = (outcome IS NOT NULL AND expected IS NOT NULL AND expected_by IS NOT NULL) "
    "AND (kind = 'case' OR (outcome IS NULL AND expected IS NULL AND expected_by IS NULL "
    "AND owner_words IS NULL AND agent_did IS NULL)) "
    "AND outcome IN ('slip', 'good', 'overdone') "
    "AND expected_by IN ('owner', 'agent') "
    "AND char_length(btrim(expected)) >= 1 AND char_length(expected) <= 4000 "
    "AND char_length(owner_words) <= 4000 AND char_length(agent_did) <= 4000"
)
MARKER_1004 = "rule case constraint (1004_ledger_cases)"

#: The savepoint a stamp-back rerun rebuilds the CHECK in, to compare.
SCRATCH = "ledger_1006_shape"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _constraint(bind) -> tuple | None:
    """`(definition, validated, comment)` of the CHECK, or None when there is none of that name."""
    row = bind.execute(sa.text(
        "SELECT pg_get_constraintdef(oid), convalidated, obj_description(oid, 'pg_constraint') "
        "FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) AND conname = :n"),
        {"t": f"public.{TABLE}", "n": NAME}).first()
    return tuple(row) if row is not None else None


def _replace(predicate: str, marker: str) -> None:
    op.execute(f"ALTER TABLE {TABLE} DROP CONSTRAINT {NAME}")
    op.execute(f"ALTER TABLE {TABLE} ADD CONSTRAINT {NAME} CHECK ({predicate})")
    op.execute(f"COMMENT ON CONSTRAINT {NAME} ON {TABLE} IS {_quote(marker)}")


def _verify_own(bind, live: tuple) -> None:
    """Accept this revision's CHECK only if it is exactly what it creates, over the rows present."""
    op.execute(f"SAVEPOINT {SCRATCH}")
    try:
        _replace(PREDICATE, CONSTRAINT_MARKER)
        rebuilt = _constraint(bind)
    except sa.exc.DBAPIError as exc:
        op.execute(f"ROLLBACK TO SAVEPOINT {SCRATCH}")
        op.execute(f"RELEASE SAVEPOINT {SCRATCH}")
        raise RuntimeError(
            f"1006 found its {NAME} but could not rebuild it over the rows present "
            f"({type(exc.orig).__name__ if exc.orig is not None else type(exc).__name__}): something "
            "in the ledger is not what 1006 allows. Inspect it by hand. Nothing has been changed."
        ) from None
    op.execute(f"ROLLBACK TO SAVEPOINT {SCRATCH}")
    op.execute(f"RELEASE SAVEPOINT {SCRATCH}")
    if live != rebuilt:
        raise RuntimeError(
            f"1006 will not adopt a {NAME} that differs from what it creates: the ledger is primary data. "
            "Inspect it, fix it by hand, then re-run. Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("SET LOCAL search_path TO public")

    live = _constraint(bind)
    comment = live[2] if live is not None else None
    if comment == CONSTRAINT_MARKER:
        _verify_own(bind, live)
    elif comment == MARKER_1004:
        _replace(PREDICATE, CONSTRAINT_MARKER)
    else:
        found = f"no {NAME}" if live is None else f"{NAME} without 1004's or its own marker"
        raise RuntimeError(
            f"1006 found {found}, so it will not replace it: the ledger is primary data. Inspect it by hand, "
            "then re-run. Nothing has been changed."
        )

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Back to 1004's outcomes, only while no case has the outcome `friction`.

    Cases are primary data that nothing can rebuild; an operator who really
    means it changes or removes those cases first, by hand.
    """
    bind = op.get_bind()
    op.execute("SET LOCAL search_path TO public")
    friction = bind.execute(sa.text(
        "SELECT count(*) FROM ledger_entries WHERE kind = 'case' AND outcome = 'friction'")).scalar()
    if friction:
        raise RuntimeError(
            f"ledger_entries holds {friction} case entries with the outcome friction, which 1004's CHECK does "
            "not allow. 1006's downgrade refuses to change them; export them (scripts/ledger_export.py) and "
            "change or remove them by hand if you mean to. Nothing has been changed."
        )
    live = _constraint(bind)
    if live is None or live[2] != CONSTRAINT_MARKER:
        raise RuntimeError(
            f"{NAME} does not carry 1006's marker, so 1006 did not create it and will not replace it. "
            "Nothing has been changed."
        )
    _replace(PREDICATE_1004, MARKER_1004)
    op.execute("RESET search_path")
