-- Way back from docker/upgrade-fork-revisions.sql, only while nothing newer ran
-- (alembic_version still exactly 1002, as when the first new image failed
-- before its migrations committed): the image from before the fork's
-- migrations were renumbered can then start again. Refuses otherwise,
-- changing nothing. Run it like the upgrade script.
BEGIN;
DO $$
BEGIN
    IF (SELECT string_agg(version_num, ',') FROM alembic_version) IS DISTINCT FROM '1002' THEN
        RAISE EXCEPTION 'alembic_version is not exactly 1002; nothing changed';
    END IF;
    IF to_regclass('public.fork_note_tasks') IS NULL OR to_regclass('public.note_tasks') IS NOT NULL THEN
        RAISE EXCEPTION 'unexpected tables; nothing changed';
    END IF;
END $$;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT fork_note_tasks_pkey TO note_tasks_pkey;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT ck_fork_note_tasks_kind TO ck_note_tasks_kind;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT ck_fork_note_tasks_line_number TO ck_note_tasks_line_number;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT uq_fork_note_tasks_note_line TO uq_note_tasks_note_line;
ALTER TABLE public.fork_note_tasks RENAME CONSTRAINT fk_fork_note_tasks_note_id TO fk_note_tasks_note_id;
ALTER SEQUENCE public.fork_note_tasks_id_seq RENAME TO note_tasks_id_seq;
ALTER TABLE public.fork_note_tasks RENAME TO note_tasks;
COMMENT ON TABLE public.note_tasks IS 'task lines and near misses per note (031_note_tasks)';
UPDATE alembic_version SET version_num = '032';
COMMIT;
