"""The task tools against a real PostgreSQL: the index rows and `list_tasks`.

What needs a database:

* the index pass writes a note's `fork_note_tasks` rows in its own transaction,
  replaces them when the note changes, and loses them with the note (cascade);
* a moved note keeps its rows (same id) and they answer under the new path;
* the extraction-version bump backfills rows for an unchanged note and
  re-embeds nothing;
* a task write re-indexes that note's rows at once, so the next `list_tasks`
  sees it with no pass in between;
* `list_tasks` answers from the caller's own rows only.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib
import json
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.auth.session import current_user_id
from src.config import settings
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.models.db import NoteMetadata, ForkNoteTask
from src.services import embeddings as embeddings_service
from src.services import indexer
from src.services.fork_tasks import ops as task_ops
from src.services import vault as vault_service
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
TODAY = date(2026, 10, 31)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("task_tools", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    for target in (settings, indexer.settings, tools.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(task_ops, "owner_today", lambda tz, now=None: TODAY)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    async with sessionmaker() as session:
        for table in ("fork_note_tasks", "note_links", "note_embeddings", "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    yield root
    current_user_id.reset(user)
    current_permission.reset(permission)


def write(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


async def task_rows(sessionmaker):
    async with sessionmaker() as session:
        result = await session.execute(
            select(NoteMetadata.file_path, ForkNoteTask.line_number, ForkNoteTask.kind, ForkNoteTask.line, ForkNoteTask.reason)
            .join(NoteMetadata, NoteMetadata.id == ForkNoteTask.note_id)
            .order_by(NoteMetadata.file_path, ForkNoteTask.line_number)
        )
        return [tuple(r) for r in result.all()]


async def test_the_pass_writes_replaces_and_prunes_task_rows(sessionmaker, vault):
    # A second note, so deleting `a.md` is a prune rather than an emptied root
    # (which the pass refuses to prune, #309).
    write(vault, "keep.md", "no tasks here\n")
    write(vault, "a.md", "---\nx: 1\n---\n- [ ] one\n```\n- [ ] code\n```\n-\t[ ] near\n")
    await indexer.index_vault(user_id=None)
    assert await task_rows(sessionmaker) == [
        ("a.md", 4, "task", "- [ ] one", None),
        ("a.md", 8, "problem", "-\t[ ] near", "whitespace_after_marker"),
    ]

    write(vault, "a.md", "- [x] one ✅ 2026-10-01\n")
    await indexer.index_vault(user_id=None)
    assert await task_rows(sessionmaker) == [("a.md", 1, "task", "- [x] one ✅ 2026-10-01", None)]

    (vault / "a.md").unlink()
    await indexer.index_vault(user_id=None)
    assert await task_rows(sessionmaker) == []


async def test_a_moved_note_keeps_its_rows_under_the_new_path(sessionmaker, vault):
    write(vault, "old.md", "- [ ] travel\n")
    await indexer.index_vault(user_id=None)
    (vault / "old.md").rename(vault / "new.md")
    await indexer.index_vault(user_id=None)
    assert await task_rows(sessionmaker) == [("new.md", 1, "task", "- [ ] travel", None)]


async def test_the_version_bump_backfills_an_unchanged_note_without_reembedding(sessionmaker, vault, monkeypatch):
    body = "# N\n- [ ] backfilled\n"
    write(vault, "n.md", body)
    h = hashlib.sha256(body.encode()).hexdigest()
    async with sessionmaker() as session:
        session.add(NoteMetadata(user_id=None, file_path="n.md", title="n", tags=[], frontmatter={},
                                 content_hash=h, embedded_content_hash=h, extraction_version=3))
        await session.commit()

    async def unexpected_provider_call(chunks):
        pytest.fail("the task backfill must not call the embedding provider")

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", unexpected_provider_call)
    await indexer.index_vault(user_id=None)
    assert await task_rows(sessionmaker) == [("n.md", 2, "task", "- [ ] backfilled", None)]
    async with sessionmaker() as session:
        row = (await session.execute(select(NoteMetadata).where(NoteMetadata.file_path == "n.md"))).scalar_one()
    assert row.extraction_version == indexer.CURRENT_EXTRACTION_VERSION == 4
    assert row.embedded_content_hash == row.content_hash == h


async def test_list_tasks_answers_from_the_index_and_writes_reindex_at_once(sessionmaker, vault):
    write(vault, "Tasks inbox.md", "- [ ] buy milk #next 📅 2026-11-02\n- [x] done thing ✅ 2026-10-01\n")
    write(vault, "projects/Area X/Area X.md", "---\narea_tag: area/area-x\n---\n- [ ] area task #area/area-x\n")
    await indexer.index_vault(user_id=None)

    listed = await tools.list_tasks_impl()
    assert [t.ref for t in listed.tasks] == ["Tasks inbox.md:1", "projects/Area X/Area X.md:4"]
    assert (await tools.list_tasks_impl(area="area-x")).tasks[0].description == "area task #area/area-x"
    assert [t.ref for t in (await tools.list_tasks_impl(status="done")).tasks] == ["Tasks inbox.md:2"]

    # A write is visible to the very next list_tasks, with no pass in between.
    created = await tools.create_task_impl("call the bank", path="projects/Area X/Area X.md")
    assert created.indexed is True, created
    by_area = await tools.list_tasks_impl(area="area-x")
    assert [t.description for t in by_area.tasks] == ["area task #area/area-x", "call the bank #area/area-x"]

    milk = listed.tasks[0]
    done = await tools.set_task_status_impl(milk.path, milk.line, milk.text, milk.content_hash, status="done")
    assert done.indexed is True and done.text == "- [x] buy milk #next 📅 2026-11-02 ✅ 2026-10-31"
    assert [t.ref for t in (await tools.list_tasks_impl(tags=["next"])).tasks] == []

    # A stale write is refused: the hash from before the status change.
    again = await tools.edit_task_impl(milk.path, milk.line, milk.text, milk.content_hash, due="today")
    assert json.loads(again.error.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))["code"] == "stale_precondition"


async def test_list_tasks_reports_a_line_changed_on_disk_as_stale(sessionmaker, vault):
    write(vault, "s.md", "- [ ] before\n")
    await indexer.index_vault(user_id=None)
    write(vault, "s.md", "- [ ] after\n")
    result = await tools.list_tasks_impl()
    assert result.tasks == [] and [s.ref for s in result.stale] == ["s.md:1"]


async def test_list_tasks_only_reads_the_callers_rows(sessionmaker, vault, monkeypatch):
    write(vault, "mine.md", "- [ ] mine\n")
    await indexer.index_vault(user_id=None)
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO users (id, username, password_hash, is_admin, is_active, session_version) "
            "VALUES (77, 'other', 'x', false, true, 1)"
        ))
        note_id = (await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash) "
            "VALUES (77, 'mine.md', 't', '{}', '{}', 'h') RETURNING id"
        ))).scalar()
        await session.execute(text(
            "INSERT INTO fork_note_tasks (note_id, line_number, kind, line) VALUES (:id, 1, 'task', '- [ ] theirs')"
        ), {"id": note_id})
        await session.commit()
    result = await tools.list_tasks_impl()
    assert [t.text for t in result.tasks] == ["- [ ] mine"]
    assert result.stale is None


async def test_with_the_agent_edit_stamp_the_reported_line_is_where_the_task_now_is(sessionmaker, vault, monkeypatch):
    """The stamp adds a three-line block above a note without properties; the
    written task's `ref` and `line` follow it, and the very next status change
    through that line succeeds (fork, 2026-11-10)."""
    monkeypatch.setattr(tools.settings, "last_ai_edit_field", "last_ai_edit")
    write(vault, "plain.md", "- [ ] first\n")
    await indexer.index_vault(user_id=None)
    listed = (await tools.list_tasks_impl(path="plain.md")).tasks[0]
    done = await tools.set_task_status_impl(listed.path, listed.line, listed.text, listed.content_hash, status="done")
    assert done.ref == "plain.md:4" and done.line == 4, done
    on_disk = (vault / "plain.md").read_text(encoding="utf-8").splitlines()
    assert on_disk[0] == "---" and on_disk[1].startswith("last_ai_edit: ") and on_disk[3] == done.text
    again = await tools.set_task_status_impl(done.path, done.line, done.text, done.content_hash, status="todo")
    assert again.line == 4 and again.error is None, again
