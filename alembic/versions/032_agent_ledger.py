"""The agent ledger: entries, topics, summary views, history, vectors.

The ledger holds what agents write for each other — quirks (Q), feedback (F)
and proposals (P) — reached only through the eight `ledger_*` tools. Unlike
every other table the index fills, **this is primary data**: nothing in the
vault can rebuild it, so it is backed up, never re-derived. See
docs/architecture/agent-ledger.md.

## What the database itself enforces

- **Every kind's own fields are real columns**, with a CHECK per field saying
  which kind may carry it and which kind must; the statuses allowed depend on
  the kind.
- **Every link between two ledger rows is a foreign key that also checks the
  owner.** Each table carries `owner_key`, a stored generated column
  `coalesce(user_id, 0)`, and every ledger-to-ledger foreign key is composite
  over `(…_id, owner_key)`. A composite key with a NULL column is not checked
  (MATCH SIMPLE), so a plain `user_id` would leave single-user mode — where
  `user_id` is NULL — unchecked; `owner_key` is never NULL, and user ids start
  at 1, so 0 is single-user mode's own owner. One user's entry can never cite,
  supersede or join another user's.
- **No row is deleted, and the history is append-only**, unless the session
  has switched the protection off: `SET LOCAL ledger.allow_delete = 'on'` (the
  owner, from `psql`, removing something for good). The trigger functions
  refuse `DELETE` on entries, topics, summary views and their lines and
  columns, the ref counters and the history, `UPDATE` on the history, and
  `TRUNCATE` on all of them. With the switch on, deleting an entry cascades to
  its history, links, topic links, line sources and vectors.
- **Links to the vault are not foreign keys**: an entry's or topic's area is a
  checked `area/<slug>` string, so a reindex or prune can never reach the
  ledger.
- **`users.id` is referenced with no action**, so the database refuses to
  delete a user who still has a ledger (the panel says so first).

## House shape

Every table carries a `COMMENT ON TABLE` marker, mirrored in
`src/models/db.py`. A pre-existing table of any of these names is refused,
never adopted: rows the server did not write would be served as the ledger.
`downgrade()` refuses while any entry exists, since the ledger cannot be
rebuilt. `search_path` is pinned; `lock_timeout` / `statement_timeout` are set
and `RESET`, for 013's to 031's reasons.

Revision ID: 032
Revises: 031
Create Date: 2026-11-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Written out here, not imported: a migration keeps describing the schema it
# created after the model moves on (019's rule). `src/models/db.py` mirrors
# each marker byte for byte.
MARKERS = {
    "ledger_ref_counters": "last ref number per owner and kind (032_agent_ledger)",
    "ledger_entries": "agent ledger entries, primary data (032_agent_ledger)",
    "ledger_entry_links": "see-also links between ledger entries (032_agent_ledger)",
    "ledger_topics": "agent ledger topics (032_agent_ledger)",
    "ledger_entry_topics": "ledger entry to topic links (032_agent_ledger)",
    "ledger_digests": "agent ledger summary views (032_agent_ledger)",
    "ledger_digest_columns": "matrix columns of a ledger summary view (032_agent_ledger)",
    "ledger_digest_lines": "lines of a ledger summary view (032_agent_ledger)",
    "ledger_digest_sources": "entries a summary-view line rests on (032_agent_ledger)",
    "ledger_revisions": "append-only history of ledger changes (032_agent_ledger)",
    "ledger_embeddings": "ledger entry vectors, derived (032_agent_ledger)",
}
TABLES = tuple(MARKERS)

#: Tables whose rows no tool deletes; `ledger_revisions` also refuses updates.
PROTECTED = (
    "ledger_ref_counters", "ledger_entries", "ledger_topics", "ledger_digests",
    "ledger_digest_columns", "ledger_digest_lines", "ledger_revisions",
)
#: Link tables: rows the tools remove (a topic taken off an entry, a source
#: taken off a line), always with a history row saying so. Still no TRUNCATE.
LINK_TABLES = ("ledger_entry_links", "ledger_entry_topics", "ledger_digest_sources")

SWITCH = "ledger.allow_delete"

OWNER = "owner_key integer GENERATED ALWAYS AS (COALESCE(user_id, 0)) STORED NOT NULL"
USER = "user_id integer NULL"
NOW = "timestamp with time zone NOT NULL DEFAULT now()"


def _ddl(dim: int) -> list[str]:
    return [
        f"""
        CREATE TABLE ledger_ref_counters (
            {USER},
            {OWNER},
            kind varchar(16) NOT NULL,
            last_number integer NOT NULL,
            PRIMARY KEY (owner_key, kind),
            CONSTRAINT fk_ledger_ref_counters_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT ck_ledger_ref_counters_kind CHECK (kind IN ('quirk', 'feedback', 'proposal')),
            CONSTRAINT ck_ledger_ref_counters_number CHECK (last_number >= 0)
        )""",
        f"""
        CREATE TABLE ledger_entries (
            id serial NOT NULL,
            {USER},
            {OWNER},
            kind varchar(16) NOT NULL,
            number integer NOT NULL,
            ref varchar(16) NOT NULL,
            status varchar(16) NOT NULL,
            origin varchar(8) NOT NULL DEFAULT 'agent',
            title varchar(150) NOT NULL,
            body text NOT NULL DEFAULT '',
            area_tag varchar(128) NULL,
            seen boolean NULL,
            certainty varchar(16) NULL,
            certainty_note text NULL,
            sources text NULL,
            subtype varchar(32) NULL,
            component varchar(300) NULL,
            fix text NULL,
            target varchar(500) NULL,
            proposed_text text NULL,
            final_text text NULL,
            changes text NULL,
            owner_reason text NULL,
            superseded_by integer NULL,
            handled_account text NULL,
            handled_at timestamp with time zone NULL,
            observed_on date NOT NULL,
            created_at {NOW},
            updated_at {NOW},
            revision integer NOT NULL DEFAULT 1,
            created_by varchar(255) NOT NULL,
            author varchar(100) NULL,
            content_hash varchar(64) NOT NULL,
            embedded_hash varchar(64) NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_entries_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT uq_ledger_entries_id_owner UNIQUE (id, owner_key),
            CONSTRAINT uq_ledger_entries_owner_kind_number UNIQUE (owner_key, kind, number),
            CONSTRAINT fk_ledger_entries_superseded_by FOREIGN KEY (superseded_by, owner_key)
                REFERENCES ledger_entries (id, owner_key),
            CONSTRAINT ck_ledger_entries_kind CHECK (kind IN ('quirk', 'feedback', 'proposal')),
            CONSTRAINT ck_ledger_entries_number CHECK (number >= 1),
            CONSTRAINT ck_ledger_entries_ref CHECK (
                ref ~ '^[QFP][0-9]{{1,6}}$'
                AND substr(ref, 1, 1) = CASE kind WHEN 'quirk' THEN 'Q' WHEN 'feedback' THEN 'F' ELSE 'P' END
                AND CAST(substr(ref, 2) AS integer) = number),
            CONSTRAINT ck_ledger_entries_status CHECK (
                status IN ('superseded', 'retracted')
                OR (kind = 'quirk' AND status IN ('current', 'stale'))
                OR (kind = 'feedback' AND status IN ('to_triage', 'open', 'handled', 'dismissed'))
                OR (kind = 'proposal' AND status IN ('accepted', 'adjusted', 'rejected', 'withdrawn'))),
            CONSTRAINT ck_ledger_entries_origin CHECK (origin IN ('agent', 'owner')),
            CONSTRAINT ck_ledger_entries_title CHECK (
                char_length(btrim(title)) >= 1 AND title !~ '[\\r\\n]'),
            CONSTRAINT ck_ledger_entries_body CHECK (char_length(body) <= 16000),
            CONSTRAINT ck_ledger_entries_area CHECK (area_tag ~ '^area/[a-z0-9]+(-[a-z0-9]+)*$'),
            CONSTRAINT ck_ledger_entries_seen CHECK (
                (kind = 'feedback') = (seen IS NOT NULL)
                AND NOT (kind = 'feedback' AND origin = 'owner' AND seen IS NOT TRUE)),
            CONSTRAINT ck_ledger_entries_triage CHECK (
                NOT (kind = 'feedback' AND status = 'to_triage' AND area_tag IS NOT NULL)),
            CONSTRAINT ck_ledger_entries_quirk_fields CHECK (
                (kind = 'quirk') = (certainty IS NOT NULL)
                AND (kind = 'quirk' OR (certainty_note IS NULL AND sources IS NULL))),
            CONSTRAINT ck_ledger_entries_certainty CHECK (
                certainty IN ('tested', 'seen_once', 'inferred', 'docs', 'untested')),
            CONSTRAINT ck_ledger_entries_feedback_fields CHECK (
                (kind = 'feedback') = (subtype IS NOT NULL)
                AND (kind = 'feedback' OR (component IS NULL AND fix IS NULL))),
            CONSTRAINT ck_ledger_entries_subtype CHECK (
                subtype IN ('unclear_instruction', 'friction', 'suggestion')),
            CONSTRAINT ck_ledger_entries_proposal_fields CHECK (
                (kind = 'proposal') = (target IS NOT NULL AND proposed_text IS NOT NULL)
                AND (kind = 'proposal' OR (final_text IS NULL AND changes IS NULL AND owner_reason IS NULL))
                AND (status NOT IN ('accepted', 'adjusted') OR final_text IS NOT NULL)),
            CONSTRAINT ck_ledger_entries_field_lengths CHECK (
                char_length(certainty_note) <= 2000 AND char_length(sources) <= 4000
                AND char_length(fix) <= 4000 AND char_length(proposed_text) <= 16000
                AND char_length(final_text) <= 16000 AND char_length(changes) <= 4000
                AND char_length(owner_reason) <= 4000 AND char_length(handled_account) <= 16000
                AND char_length(author) >= 1),
            CONSTRAINT ck_ledger_entries_superseded CHECK (
                (status = 'superseded') = (superseded_by IS NOT NULL) AND superseded_by <> id),
            CONSTRAINT ck_ledger_entries_handled CHECK (
                (status = 'handled') = (handled_account IS NOT NULL)
                AND (status = 'handled') = (handled_at IS NOT NULL)
                AND char_length(btrim(handled_account)) >= 1),
            CONSTRAINT ck_ledger_entries_revision CHECK (revision >= 1)
        )""",
        "CREATE INDEX ix_ledger_entries_owner_status ON ledger_entries (owner_key, status)",
        "CREATE INDEX ix_ledger_entries_owner_updated ON ledger_entries (owner_key, updated_at)",
        f"""
        CREATE TABLE ledger_entry_links (
            {USER},
            {OWNER},
            entry_id integer NOT NULL,
            related_id integer NOT NULL,
            created_at {NOW},
            PRIMARY KEY (entry_id, related_id),
            CONSTRAINT fk_ledger_entry_links_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_ledger_entry_links_entry FOREIGN KEY (entry_id, owner_key)
                REFERENCES ledger_entries (id, owner_key) ON DELETE CASCADE,
            CONSTRAINT fk_ledger_entry_links_related FOREIGN KEY (related_id, owner_key)
                REFERENCES ledger_entries (id, owner_key) ON DELETE CASCADE,
            CONSTRAINT ck_ledger_entry_links_order CHECK (entry_id < related_id)
        )""",
        "CREATE INDEX ix_ledger_entry_links_related ON ledger_entry_links (related_id)",
        f"""
        CREATE TABLE ledger_topics (
            id serial NOT NULL,
            {USER},
            {OWNER},
            slug varchar(64) NOT NULL,
            kind varchar(16) NOT NULL,
            description varchar(300) NOT NULL,
            area_tag varchar(128) NULL,
            status varchar(16) NOT NULL DEFAULT 'active',
            merged_into integer NULL,
            created_at {NOW},
            updated_at {NOW},
            revision integer NOT NULL DEFAULT 1,
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_topics_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT uq_ledger_topics_id_owner UNIQUE (id, owner_key),
            CONSTRAINT uq_ledger_topics_owner_slug UNIQUE (owner_key, slug),
            CONSTRAINT fk_ledger_topics_merged_into FOREIGN KEY (merged_into, owner_key)
                REFERENCES ledger_topics (id, owner_key),
            CONSTRAINT ck_ledger_topics_slug CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
            CONSTRAINT ck_ledger_topics_kind CHECK (kind IN ('system', 'surface')),
            CONSTRAINT ck_ledger_topics_description CHECK (
                char_length(btrim(description)) >= 1 AND description !~ '[\\r\\n]'),
            CONSTRAINT ck_ledger_topics_area CHECK (area_tag ~ '^area/[a-z0-9]+(-[a-z0-9]+)*$'),
            CONSTRAINT ck_ledger_topics_status CHECK (status IN ('active', 'merged')),
            CONSTRAINT ck_ledger_topics_merged CHECK (
                (status = 'merged') = (merged_into IS NOT NULL) AND merged_into <> id),
            CONSTRAINT ck_ledger_topics_revision CHECK (revision >= 1)
        )""",
        f"""
        CREATE TABLE ledger_entry_topics (
            {USER},
            {OWNER},
            entry_id integer NOT NULL,
            topic_id integer NOT NULL,
            PRIMARY KEY (entry_id, topic_id),
            CONSTRAINT fk_ledger_entry_topics_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_ledger_entry_topics_entry FOREIGN KEY (entry_id, owner_key)
                REFERENCES ledger_entries (id, owner_key) ON DELETE CASCADE,
            CONSTRAINT fk_ledger_entry_topics_topic FOREIGN KEY (topic_id, owner_key)
                REFERENCES ledger_topics (id, owner_key)
        )""",
        "CREATE INDEX ix_ledger_entry_topics_topic ON ledger_entry_topics (topic_id)",
        f"""
        CREATE TABLE ledger_digests (
            id serial NOT NULL,
            {USER},
            {OWNER},
            slug varchar(64) NOT NULL,
            title varchar(150) NOT NULL,
            description varchar(500) NOT NULL,
            layout varchar(8) NOT NULL,
            created_at {NOW},
            updated_at {NOW},
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_digests_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT uq_ledger_digests_id_owner UNIQUE (id, owner_key),
            CONSTRAINT uq_ledger_digests_owner_slug UNIQUE (owner_key, slug),
            CONSTRAINT ck_ledger_digests_slug CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
            CONSTRAINT ck_ledger_digests_layout CHECK (layout IN ('list', 'matrix')),
            CONSTRAINT ck_ledger_digests_text CHECK (
                char_length(btrim(title)) >= 1 AND char_length(btrim(description)) >= 1)
        )""",
        f"""
        CREATE TABLE ledger_digest_columns (
            id serial NOT NULL,
            {USER},
            {OWNER},
            digest_id integer NOT NULL,
            label varchar(100) NOT NULL,
            position integer NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_digest_columns_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_ledger_digest_columns_digest FOREIGN KEY (digest_id, owner_key)
                REFERENCES ledger_digests (id, owner_key),
            CONSTRAINT uq_ledger_digest_columns_digest_label UNIQUE (digest_id, label),
            CONSTRAINT ck_ledger_digest_columns_label CHECK (char_length(btrim(label)) >= 1)
        )""",
        f"""
        CREATE TABLE ledger_digest_lines (
            id serial NOT NULL,
            {USER},
            {OWNER},
            digest_id integer NOT NULL,
            row_label varchar(150) NULL,
            column_label varchar(100) NULL,
            position integer NOT NULL,
            text varchar(2000) NOT NULL,
            revision integer NOT NULL DEFAULT 1,
            needs_review boolean NOT NULL DEFAULT false,
            review_reason text NULL,
            removed_at timestamp with time zone NULL,
            created_at {NOW},
            updated_at {NOW},
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_digest_lines_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT uq_ledger_digest_lines_id_owner UNIQUE (id, owner_key),
            CONSTRAINT fk_ledger_digest_lines_digest FOREIGN KEY (digest_id, owner_key)
                REFERENCES ledger_digests (id, owner_key),
            CONSTRAINT fk_ledger_digest_lines_column FOREIGN KEY (digest_id, column_label)
                REFERENCES ledger_digest_columns (digest_id, label),
            CONSTRAINT ck_ledger_digest_lines_text CHECK (char_length(btrim(text)) >= 1),
            CONSTRAINT ck_ledger_digest_lines_review CHECK (
                needs_review = (review_reason IS NOT NULL) AND char_length(review_reason) <= 2000),
            CONSTRAINT ck_ledger_digest_lines_revision CHECK (revision >= 1)
        )""",
        "CREATE INDEX ix_ledger_digest_lines_digest ON ledger_digest_lines (digest_id)",
        f"""
        CREATE TABLE ledger_digest_sources (
            {USER},
            {OWNER},
            line_id integer NOT NULL,
            entry_id integer NOT NULL,
            PRIMARY KEY (line_id, entry_id),
            CONSTRAINT fk_ledger_digest_sources_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_ledger_digest_sources_line FOREIGN KEY (line_id, owner_key)
                REFERENCES ledger_digest_lines (id, owner_key),
            CONSTRAINT fk_ledger_digest_sources_entry FOREIGN KEY (entry_id, owner_key)
                REFERENCES ledger_entries (id, owner_key) ON DELETE CASCADE
        )""",
        "CREATE INDEX ix_ledger_digest_sources_entry ON ledger_digest_sources (entry_id)",
        f"""
        CREATE TABLE ledger_revisions (
            id bigserial NOT NULL,
            {USER},
            {OWNER},
            entry_id integer NULL,
            topic_id integer NULL,
            digest_id integer NULL,
            line_id integer NULL,
            revision integer NULL,
            action varchar(32) NOT NULL,
            reason text NOT NULL,
            before jsonb NULL,
            created_at {NOW},
            created_by varchar(255) NOT NULL,
            author varchar(100) NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_revisions_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_ledger_revisions_entry FOREIGN KEY (entry_id, owner_key)
                REFERENCES ledger_entries (id, owner_key) ON DELETE CASCADE,
            CONSTRAINT fk_ledger_revisions_topic FOREIGN KEY (topic_id, owner_key)
                REFERENCES ledger_topics (id, owner_key),
            CONSTRAINT fk_ledger_revisions_digest FOREIGN KEY (digest_id, owner_key)
                REFERENCES ledger_digests (id, owner_key),
            CONSTRAINT fk_ledger_revisions_line FOREIGN KEY (line_id, owner_key)
                REFERENCES ledger_digest_lines (id, owner_key),
            CONSTRAINT ck_ledger_revisions_one_subject CHECK (
                num_nonnulls(entry_id, topic_id, digest_id, line_id) = 1),
            CONSTRAINT ck_ledger_revisions_reason CHECK (
                char_length(btrim(reason)) >= 1 AND char_length(reason) <= 2000),
            CONSTRAINT ck_ledger_revisions_before CHECK ((action = 'create') = (before IS NULL))
        )""",
        "CREATE INDEX ix_ledger_revisions_entry ON ledger_revisions (entry_id)",
        "CREATE INDEX ix_ledger_revisions_topic ON ledger_revisions (topic_id)",
        "CREATE INDEX ix_ledger_revisions_digest ON ledger_revisions (digest_id)",
        "CREATE INDEX ix_ledger_revisions_line ON ledger_revisions (line_id)",
        f"""
        CREATE TABLE ledger_embeddings (
            id serial NOT NULL,
            entry_id integer NOT NULL,
            chunk_index integer NOT NULL,
            chunk_text text NOT NULL,
            embedding vector({dim}) NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT fk_ledger_embeddings_entry FOREIGN KEY (entry_id)
                REFERENCES ledger_entries (id) ON DELETE CASCADE
        )""",
        "CREATE INDEX ix_ledger_embeddings_entry ON ledger_embeddings (entry_id)",
    ]


def _trigger_ddl() -> list[str]:
    statements = [
        # One function per verb, so the refusal names what was attempted. The
        # switch is a custom setting read with `missing_ok`, so a session that
        # never set it reads NULL, not an error.
        f"""
        CREATE FUNCTION ledger_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF COALESCE(current_setting('{SWITCH}', true), '') = 'on' THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'agent ledger: % on % is refused; nothing in the ledger is deleted or rewritten',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation',
                      HINT = 'To remove something for good, run SET LOCAL {SWITCH} = ''on'' in the same transaction first.';
        END
        $$""",
    ]
    for table in PROTECTED:
        statements.append(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ledger_refuse_change()"
        )
    statements.append(
        "CREATE TRIGGER trg_ledger_revisions_no_update BEFORE UPDATE ON ledger_revisions "
        "FOR EACH ROW EXECUTE FUNCTION ledger_refuse_change()"
    )
    for table in PROTECTED + LINK_TABLES:
        statements.append(
            f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION ledger_refuse_change()"
        )
    return statements


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pin_search_path() -> None:
    op.execute("SET LOCAL search_path TO public")


def _existing(bind) -> list[str]:
    return [
        table for table in TABLES
        if bind.execute(sa.text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}).scalar()
    ]


def upgrade() -> None:
    from src.config import settings

    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '10s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    _pin_search_path()

    present = _existing(bind)
    if present:
        raise RuntimeError(
            f"032 will not adopt pre-existing tables ({', '.join(present)}): the ledger is "
            "primary data, and rows this server did not write would be served as agents' "
            "records. Inspect them, move them aside by hand, then re-run. Nothing has been changed."
        )
    function = bind.execute(
        sa.text("SELECT to_regprocedure('public.ledger_refuse_change()')")
    ).scalar()
    if function is not None:
        raise RuntimeError(
            "032 found a function public.ledger_refuse_change() it did not create. Drop it by "
            "hand, then re-run. Nothing has been changed."
        )

    for statement in _ddl(int(settings.embedding_dimensions)):
        op.execute(statement)
    for table, marker in MARKERS.items():
        op.execute(f"COMMENT ON TABLE {table} IS {_quote(marker)}")
    for statement in _trigger_ddl():
        op.execute(statement)

    op.execute("RESET lock_timeout")
    op.execute("RESET statement_timeout")
    op.execute("RESET search_path")


def downgrade() -> None:
    """Drop the ledger only while it holds no entry.

    The ledger is not derived from anything: dropping a table with entries in
    it destroys them. An operator who really means it empties the tables first
    (with the delete switch on), which is a decision made by hand, not by a
    downgrade.
    """
    bind = op.get_bind()
    _pin_search_path()
    present = _existing(bind)
    for table in present:
        comment = bind.execute(
            sa.text("SELECT obj_description(CAST(:t AS regclass), 'pg_class')"),
            {"t": f"public.{table}"},
        ).scalar()
        if comment != MARKERS[table]:
            raise RuntimeError(
                f"{table} does not carry 032's marker, so 032 did not create it and will not "
                "drop it. Nothing has been changed."
            )
    if "ledger_entries" in present:
        count = bind.execute(sa.text("SELECT count(*) FROM ledger_entries")).scalar()
        if count:
            raise RuntimeError(
                f"ledger_entries holds {count} entries, which nothing can rebuild. 032's "
                "downgrade refuses to drop them; export them (scripts/ledger_export.py) and "
                "remove them by hand if you mean to. Nothing has been changed."
            )
    for table in reversed(TABLES):
        if table in present:
            op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION IF EXISTS ledger_refuse_change()")
    op.execute("RESET search_path")
