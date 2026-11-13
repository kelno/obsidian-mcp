-- One-time conversion of a database the fork migrated under its old revision
-- ids, before the first image whose fork migrations are numbered from 1001
-- starts against it. Old layout: 031 = the fork's task table `note_tasks`,
-- 032 = the agent ledger. New layout: 1001 = `fork_note_tasks`, 1002 = the
-- agent ledger, on a branch of their own from 030, so that upstream's own 031
-- onward (which include a different `note_tasks`) can run after it.
--
-- Run it as the database's owner (the app's role), for example:
--   psql --set ON_ERROR_STOP=1 --dbname <the app database's connection URI> --file docker/upgrade-fork-revisions.sql
-- It refuses, changing nothing, unless the database is exactly at the fork's
-- old 032. The task rows are derived data; the ledger is untouched.
-- See docs/architecture/schema-and-migrations.md, "Fork migrations".

BEGIN;

DO $$
DECLARE
    current_rev text;
BEGIN
    SELECT version_num INTO current_rev FROM alembic_version;
    IF current_rev IS DISTINCT FROM '032' THEN
        RAISE EXCEPTION 'expected alembic_version 032 (the fork''s agent ledger), found %; nothing changed', current_rev;
    END IF;
    IF (SELECT count(*) FROM alembic_version) <> 1 THEN
        RAISE EXCEPTION 'alembic_version holds more than one row; nothing changed';
    END IF;
    IF to_regclass('public.ledger_entries') IS NULL OR to_regclass('public.note_tasks') IS NULL THEN
        RAISE EXCEPTION 'ledger_entries or note_tasks is missing: this is not the fork''s 032; nothing changed';
    END IF;
    IF to_regclass('public.fork_note_tasks') IS NOT NULL THEN
        RAISE EXCEPTION 'fork_note_tasks already exists; nothing changed';
    END IF;
END $$;

ALTER TABLE public.note_tasks RENAME TO fork_note_tasks;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT note_tasks_pkey TO fork_note_tasks_pkey;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT ck_note_tasks_kind TO ck_fork_note_tasks_kind;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT ck_note_tasks_line_number TO ck_fork_note_tasks_line_number;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT uq_note_tasks_note_line TO uq_fork_note_tasks_note_line;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT fk_note_tasks_note_id TO fk_fork_note_tasks_note_id;
ALTER SEQUENCE public.note_tasks_id_seq RENAME TO fork_note_tasks_id_seq;
COMMENT ON TABLE public.fork_note_tasks IS 'task lines and near misses per note (1001_fork_note_tasks)';

UPDATE alembic_version SET version_num = '1002';

COMMIT;
