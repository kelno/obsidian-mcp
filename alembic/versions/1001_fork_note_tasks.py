"""`fork_note_tasks`: the task lines of every indexed note (task tools).

`list_tasks` answers "which tasks match this Tasks query?" over the whole
vault within the time a connector call allows, so the task lines are derived
by the index pass — in the same transaction as a note's links and keyword
vector — rather than found by reading every note per call. See
docs/architecture/tasks.md.

## Shape

`(id serial PK, note_id integer NOT NULL → notes_metadata.id ON DELETE
CASCADE, line_number integer NOT NULL, kind varchar(16) NOT NULL, line text
NOT NULL, reason varchar(32) NULL)`, plus:

- `ck_fork_note_tasks_kind`: `kind IN ('task', 'problem')`. A kind the reader does
  not know is a row `list_tasks` would silently skip.
- `ck_fork_note_tasks_line_number`: `line_number >= 1`. The tools address a task as
  `path:line`, 1-based; a zero would name no line.
- `uq_fork_note_tasks_note_line` on `(note_id, line_number)`: one row per line, and
  the index every per-note read and delete uses.

The rows hold a line's **text**, never its parsed meaning (status type, global
filter and "today" come from the plugin settings and the clock at query time).

## No backfill here

The table starts empty. Every note is re-derived once by the next index pass,
because the same change raises `CURRENT_EXTRACTION_VERSION` from 3 to 4 —
the existing mechanism for adding derived data, which re-embeds nothing
(`embeddings._EXTRACTION_CLEANERS[4]` is the version-3 cleaner).

## House shape

021's: marker-owned (`COMMENT ON TABLE`, mirrored in `src/models/db.py`), and
a pre-existing table of this name is verified against the whole shape —
columns, primary key, the id default, the foreign key's definition, both CHECKs
by definition (measured off a scratch TEMP table), the unique constraint — and
refused, naming what disagreed, rather than adopted. `downgrade()` drops only
a marked table. `search_path` is pinned and asserted; `lock_timeout` /
`statement_timeout` are set and `RESET`, for 013's to 030's reasons.

Revision ID: 1001
Revises: 030
Create Date: 2026-10-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1001"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE = "fork_note_tasks"
QUALIFIED = "public.fork_note_tasks"

KIND_CHECK = "ck_fork_note_tasks_kind"
LINE_CHECK = "ck_fork_note_tasks_line_number"
UNIQUE = "uq_fork_note_tasks_note_line"
FOREIGN_KEY = "fk_fork_note_tasks_note_id"

# Written out as literals, not imported from the model: a migration keeps
# describing the schema it created after the model moves on (019's rule).
KIND_PREDICATE = "kind IN ('task', 'problem')"
LINE_PREDICATE = "line_number >= 1"

# Byte identical to `_NOTE_TASKS_TABLE_MARKER` in `src/models/db.py`.
MARKER = "task lines and near misses per note (1001_fork_note_tasks)"

EXPECTED_COLUMNS = (
    ("id", "integer", True),
    ("note_id", "integer", True),
    ("line_number", "integer", True),
    ("kind", "character varying(16)", True),
    ("line", "text", True),
    ("reason", "character varying(32)", False),
)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _oid(bind, name: str):
    return bind.execute(
        sa.text("SELECT CAST(to_regclass(:name) AS oid)"), {"name": name}
    ).scalar()


def _table_comment(bind):
    return bind.execute(
        sa.text("SELECT obj_description(CAST(:qualified AS regclass), 'pg_class')"),
        {"qualified": QUALIFIED},
    ).scalar()


def _columns(bind):
    return [
        (row[0], row[1], row[2])
        for row in bind.execute(
            sa.text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
                "FROM pg_attribute a "
                "WHERE a.attrelid = CAST(:table AS regclass) "
                "  AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"table": QUALIFIED},
        ).fetchall()
    ]


def _constraints(bind) -> dict:
    """`{name: (contype, definition, validated)}` for every table constraint."""
    rows = bind.execute(
        sa.text(
            # `contype` is PostgreSQL's one-byte `"char"`, which the driver
            # hands back as bytes; the cast makes it the text the checks name.
            "SELECT c.conname, CAST(c.contype AS text) AS contype, "
            "pg_get_constraintdef(c.oid) AS definition, c.convalidated "
            "FROM pg_constraint c WHERE c.conrelid = CAST(:table AS regclass)"
        ),
        {"table": QUALIFIED},
    ).fetchall()
    return {row.conname: (row.contype, row.definition, row.convalidated) for row in rows}


def _column_default(bind, column: str):
    return bind.execute(
        sa.text(
            "SELECT pg_get_expr(d.adbin, d.adrelid) "
            "FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column "
            "  AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": QUALIFIED, "column": column},
    ).scalar()


def _canonical_definitions(bind) -> dict:
    """How this server renders 1001's constraints, measured off a scratch table.

    013's device: a hand-written expected string pins the migration to one
    PostgreSQL major's normalisation; a TEMP table carrying the identical
    declarations cannot drift. The foreign key's target is not reproducible on
    a TEMP table (it may not reference a permanent one), so its definition is
    compared as the literal PostgreSQL renders for exactly this declaration.
    """
    scratch = "_omcp_031_probe"
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {scratch} ("
            "note_id integer NOT NULL, line_number integer NOT NULL, kind varchar(16) NOT NULL, "
            f"CONSTRAINT {KIND_CHECK} CHECK ({KIND_PREDICATE}), "
            f"CONSTRAINT {LINE_CHECK} CHECK ({LINE_PREDICATE}), "
            f"CONSTRAINT {UNIQUE} UNIQUE (note_id, line_number))"
        )
    )
    rows = bind.execute(
        sa.text(
            "SELECT c.conname, pg_get_constraintdef(c.oid) AS definition FROM pg_constraint c "
            "WHERE c.conrelid = CAST(:scratch AS regclass)"
        ),
        {"scratch": f"pg_temp.{scratch}"},
    ).fetchall()
    bind.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{scratch}"))
    out = {row.conname: row.definition for row in rows}
    out[FOREIGN_KEY] = "FOREIGN KEY (note_id) REFERENCES notes_metadata(id) ON DELETE CASCADE"
    return out


def _canonical_id_default(bind):
    sequence = bind.execute(
        sa.text("SELECT pg_get_serial_sequence(:qualified, 'id')"),
        {"qualified": QUALIFIED},
    ).scalar()
    if sequence is None:
        return None
    rendered = bind.execute(
        sa.text("SELECT CAST(CAST(:seq AS regclass) AS text)"), {"seq": sequence}
    ).scalar()
    return f"nextval('{rendered}'::regclass)"


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _assert_is_the_qualified_table(bind) -> None:
    qualified = _oid(bind, QUALIFIED)
    unqualified = _oid(bind, TABLE)
    if qualified is None or qualified != unqualified:
        raise RuntimeError(
            f"1001's {TABLE} is not the table {QUALIFIED} resolves to "
            f"(search_path-relative oid {unqualified!r}, qualified oid "
            f"{qualified!r}). Set the migration role's search_path so "
            "`public` comes first, then re-run."
        )


def _create() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("note_id", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes_metadata.id"], name=FOREIGN_KEY, ondelete="CASCADE"
        ),
        sa.CheckConstraint(KIND_PREDICATE, name=KIND_CHECK),
        sa.CheckConstraint(LINE_PREDICATE, name=LINE_CHECK),
        sa.UniqueConstraint("note_id", "line_number", name=UNIQUE),
    )
    op.execute(f"COMMENT ON TABLE {TABLE} IS {_quote(MARKER)}")


def _verify(bind) -> None:
    """Accept a pre-existing table only if it is exactly 1001's."""
    problems = []
    if _table_comment(bind) != MARKER:
        problems.append("it does not carry 1001's comment marker")
    columns = _columns(bind)
    if columns != list(EXPECTED_COLUMNS):
        problems.append(f"its columns are {columns}, not {list(EXPECTED_COLUMNS)}")

    live = _constraints(bind)
    canonical = _canonical_definitions(bind)
    primary = [name for name, (kind, _d, _v) in live.items() if kind == "p"]
    if len(primary) != 1 or live[primary[0]][1] != "PRIMARY KEY (id)":
        problems.append("its primary key is not (id)")
    for name, definition in canonical.items():
        actual = live.get(name)
        if actual is None:
            problems.append(f"it is missing constraint {name}")
        elif actual[1] != definition:
            problems.append(f"its constraint {name} is {actual[1]!r}, not {definition!r}")
        elif not actual[2]:
            problems.append(f"its constraint {name} is NOT VALID")
    extra = sorted(set(live) - set(canonical) - set(primary))
    if extra:
        problems.append(f"it carries constraints 1001 does not create: {extra}")

    live_id = _column_default(bind, "id")
    canonical_id = _canonical_id_default(bind)
    if canonical_id is None or live_id != canonical_id:
        problems.append(f"its id default is {live_id!r}, not the serial 1001 creates")

    if problems:
        raise RuntimeError(
            f"{TABLE} already exists but {'; '.join(problems)}. 1001 will not "
            "adopt a table of unknown provenance: list_tasks answers from it, "
            "and rows it did not derive would be returned as the vault's tasks. "
            "Resolve by hand — drop it and let 1001 create it — then re-run. "
            "Nothing has been changed."
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    if _oid(bind, QUALIFIED) is not None:
        _assert_is_the_qualified_table(bind)
        _verify(bind)
    else:
        _create()
        _assert_is_the_qualified_table(bind)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop the table only if it carries 1001's marker.

    Losing it loses derived data only: a re-upgrade starts empty and the next
    pass after `CURRENT_EXTRACTION_VERSION` moves re-derives it. The previous
    build neither reads nor writes it.
    """
    bind = op.get_bind()
    _pin_search_path()
    if _oid(bind, QUALIFIED) is None:
        op.execute("RESET search_path")
        return
    _assert_is_the_qualified_table(bind)
    if _table_comment(bind) != MARKER:
        raise RuntimeError(
            f"{TABLE} does not carry 1001's comment marker ({MARKER!r}), so 1001 "
            "did not create it and will not drop it. Nothing has been changed. "
            "Remove it by hand if you mean to."
        )
    op.drop_table(TABLE)
    op.execute("RESET search_path")
