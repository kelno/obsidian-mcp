"""The agent ledger's search words, stored: `ledger_entry_words`.

Until now `ledger_search` and `ledger_add`'s similar entries computed every
entry's words (`to_tsvector` over its ref, title, body and own fields, once
per `FTS_CONFIGS` entry) at query time: 5.9 s for 691 entries on the
deployment, growing with the ledger. This table keeps them, with a GIN
index, so a search costs what its matches cost. See
docs/architecture/agent-ledger.md, "Search".

## What changes

- `ledger_entry_words`: one row per entry, `entry_id` (the primary key, a
  foreign key onto `ledger_entries` with `ON DELETE CASCADE`), `revision`
  and `configs` (the entry's revision and the ordered `FTS_CONFIGS` the
  words were computed under) and `words` (`tsvector`), with the GIN index
  `ix_ledger_entry_words_words`.
- Derived: a row is used only while its `revision` and `configs` match the
  entry and the settings; every other entry's words are computed on the spot,
  as before. So the table starts empty and needs no backfill here: the
  ledger writers fill the rows they write, and the ledger sweep of each index
  pass fills the rest.
- Nothing on 1002's tables changes (the foreign key belongs to this table),
  so 1002's stamp-back comparison is unaffected.

## Stamp-back rerun

A `ledger_entry_words` carrying this revision's marker is dropped and
created again: its rows are derived, and the sweep refills them. One without
the marker is refused by name, with nothing changed. `downgrade()` drops the
marked table (derived rows only). `search_path` is pinned; `lock_timeout` /
`statement_timeout` are set and `RESET`, as in 1002.

Revision ID: 1005
Revises: 1004
Create Date: 2026-11-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1005"
down_revision: Union[str, None] = "1004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out here, not imported: a migration keeps describing the schema it
# created after the model moves on. `src/models/db.py` mirrors it byte for byte.
TABLE = "ledger_entry_words"
TABLE_MARKER = "ledger entry search words, derived (1005_ledger_entry_words)"

TABLE_DDL = [
    f"""
    CREATE TABLE {TABLE} (
        entry_id integer NOT NULL,
        revision integer NOT NULL,
        configs text NOT NULL,
        words tsvector NOT NULL,
        CONSTRAINT {TABLE}_pkey PRIMARY KEY (entry_id),
        CONSTRAINT fk_ledger_entry_words_entry FOREIGN KEY (entry_id)
            REFERENCES ledger_entries (id) ON DELETE CASCADE
    )""",
    f"CREATE INDEX ix_ledger_entry_words_words ON {TABLE} USING gin (words)",
]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _table_comment(bind) -> tuple[bool, str | None]:
    if not bind.execute(sa.text("SELECT to_regclass(:t)"), {"t": f"public.{TABLE}"}).scalar():
        return False, None
    return True, bind.execute(sa.text(
        "SELECT obj_description(CAST(:t AS regclass), 'pg_class')"), {"t": f"public.{TABLE}"}).scalar()


def _create() -> None:
    for statement in TABLE_DDL:
        op.execute(statement)
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(TABLE_MARKER)}")


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("SET LOCAL search_path TO public")

    present, comment = _table_comment(bind)
    if present and comment != TABLE_MARKER:
        raise RuntimeError(
            f"1005 found a {TABLE} table without its marker, so it did not create it and will not adopt or "
            "drop it. Inspect it, move it aside by hand, then re-run. Nothing has been changed."
        )
    if present:
        # Its own table on a stamp-back rerun: derived rows, rebuilt as created.
        op.execute(f"DROP TABLE {TABLE}")
    _create()

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL search_path TO public")
    present, comment = _table_comment(bind)
    if present and comment != TABLE_MARKER:
        raise RuntimeError(
            f"1005's downgrade found a {TABLE} table without its marker and will not drop it. "
            "Nothing has been changed."
        )
    if present:
        op.execute(f"DROP TABLE {TABLE}")
    op.execute("RESET search_path")
