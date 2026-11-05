"""The agent ledger against a real PostgreSQL.

What needs a database, and is checked here:

* the schema: the model's CHECKs, keys and foreign keys equal the migration's
  (a `create_all` database compared with the migrated one, since `alembic
  check` does not compare predicates), and the triggers exist;
* what the database itself refuses: deleting an entry, topic, view or line,
  rewriting the history, TRUNCATE, a link between two owners' rows (also in
  single-user mode), a field of the wrong kind, deleting a user who has a
  ledger — and the owner's per-session switch that allows a removal, which
  cascades to the entry's history;
* the eight tools end to end, in single-user mode: refs, refusals with the
  valid values, the expected revision, history, closing statuses, bulk seen,
  search by words and by meaning, summary views and their review flags,
  topics and merges;
* the vectors: written after the entry, never used once outdated, swept by
  the indexer's embed stage within its budget, reset with the notes'.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import json
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.auth.session import current_user_id
from src.config import settings
from src.control_panel import users as panel_users
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.models.db import Base, LedgerEmbedding, LedgerEntry
from src.services import indexer, ledger, ledger_embed, task_ops
from src.services import vault as vault_service
import _harness
from fakes import FakeProvider

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)
TODAY = date(2026, 11, 5)
LEDGER_TABLES = (
    "ledger_revisions", "ledger_digest_sources", "ledger_digest_lines", "ledger_digest_columns",
    "ledger_digests", "ledger_entry_topics", "ledger_entry_links", "ledger_embeddings", "ledger_entries",
    "ledger_topics", "ledger_ref_counters",
)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("ledger", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def wipe(sessionmaker):
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL ledger.allow_delete = 'on'"))
        for table in (*LEDGER_TABLES, "notes_metadata", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await session.commit()


@pytest_asyncio.fixture(loop_scope="module")
async def ctx(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)
    for target in (settings, indexer.settings, tools.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    for module in (indexer, src.database, tools, ledger_embed):
        monkeypatch.setattr(module, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)
    monkeypatch.setattr(task_ops, "owner_today", lambda tz, now=None: TODAY)
    provider = FakeProvider()
    monkeypatch.setattr(ledger_embed, "get_embeddings_batch", provider.embed_batch)
    monkeypatch.setattr(ledger_embed, "get_embedding", provider.embed_one)
    monkeypatch.setattr(ledger_embed, "enqueue", lambda entry_id: True)  # embedded explicitly below
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    await wipe(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash) VALUES "
            "(NULL, 'projects/Agent personnel/Agent personnel.md', 'Agent personnel', '{}', "
            "'{\"area_tag\": \"area/agent-personnel\", \"description\": \"How agents are set up.\"}', 'h1'), "
            "(NULL, 'projects/Home/Home.md', 'Home', '{}', '{\"area_tag\": \"area/home\"}', 'h2')"))
        await session.commit()
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    yield root
    current_user_id.reset(user)
    current_permission.reset(permission)


def code(result) -> str:
    assert result.error, result
    return json.loads(result.error.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))["code"]


async def embed_all(sessionmaker):
    async with sessionmaker() as session:
        ids = (await session.execute(select(LedgerEntry.id).where(
            LedgerEntry.embedded_hash.is_distinct_from(LedgerEntry.content_hash)))).scalars().all()
    for entry_id in ids:
        await ledger_embed.embed_entry(entry_id)


async def add_quirk(title, **kw):
    kw.setdefault("details", {"certainty": "tested"})
    result = await tools.ledger_add_impl("quirk", title, **kw)
    assert result.error is None, result.error
    return result


# ── The schema ──────────────────────────────────────────────────────────────

async def _fetch(url, sql):
    import asyncpg

    conn = await asyncpg.connect(_harness.asyncpg_dsn(url))
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


async def _constraints(url):
    rows = await _fetch(url, """
        SELECT c.conrelid::regclass::text AS tbl, c.conname, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c WHERE c.conrelid::regclass::text LIKE 'ledger%'""")
    return {(r["tbl"], r["conname"]): r["def"] for r in rows}


async def test_the_model_declares_exactly_the_migrations_constraints(migrated_url):
    import uuid

    migrated = await _constraints(migrated_url)
    name = f"test_ledger_model_{uuid.uuid4().hex}"
    admin = _harness.PGVECTOR_TEST_ADMIN_URL
    await _harness._run_maintenance(admin, f'CREATE DATABASE "{name}"')
    try:
        url = _harness._with_database(admin, name)
        engine = create_async_engine(url, poolclass=None)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
        modelled = await _constraints(url)
    finally:
        await _harness._run_maintenance(admin, f'DROP DATABASE IF EXISTS "{name}" (FORCE)')
    assert len(migrated) > 60
    # Primary keys are unnamed in both and render the same; every CHECK,
    # unique and foreign key must agree by name and definition.
    assert modelled == migrated


async def test_the_triggers_exist(migrated_url):
    rows = await _fetch(migrated_url, """
        SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE 'trg_ledger%'""")
    names = {r["tgname"] for r in rows}
    for table in ("ledger_entries", "ledger_topics", "ledger_digests", "ledger_digest_lines",
                  "ledger_digest_columns", "ledger_revisions", "ledger_ref_counters"):
        assert f"trg_{table}_no_delete" in names
        assert f"trg_{table}_no_truncate" in names
    assert "trg_ledger_revisions_no_update" in names
    for table in ("ledger_entry_links", "ledger_entry_topics", "ledger_digest_sources"):
        assert f"trg_{table}_no_truncate" in names


# ── What the database refuses on its own ─────────────────────────────────────

ENTRY_SQL = (
    "INSERT INTO ledger_entries (user_id, kind, number, ref, status, title, observed_on, created_by, "
    "content_hash, certainty) VALUES ({uid}, 'quirk', {n}, 'Q{n}', 'current', 't', '2026-11-05', 's', 'h', "
    "'tested') RETURNING id"
)


async def test_no_delete_no_rewrite_and_the_owners_switch(sessionmaker, ctx):
    q = await add_quirk("a quirk")
    await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="fix", title="a better quirk")
    async with sessionmaker() as session:
        with pytest.raises(DBAPIError, match="agent ledger: DELETE on ledger_entries is refused"):
            await session.execute(text("DELETE FROM ledger_entries"))
    for statement in ("UPDATE ledger_revisions SET reason = 'x'", "DELETE FROM ledger_revisions",
                      "TRUNCATE ledger_entries CASCADE", "TRUNCATE ledger_entry_topics"):
        async with sessionmaker() as session:
            with pytest.raises(DBAPIError, match="agent ledger"):
                await session.execute(text(statement))
    async with sessionmaker() as session:
        await session.execute(text("SET LOCAL ledger.allow_delete = 'on'"))
        await session.execute(text("DELETE FROM ledger_entries WHERE ref = :r"), {"r": q.ref})
        await session.commit()
    async with sessionmaker() as session:
        assert (await session.execute(text("SELECT count(*) FROM ledger_revisions"))).scalar() == 0
        # The switch was transaction-local: the next transaction is protected again.
        await session.execute(text(ENTRY_SQL.format(uid="NULL", n=900)))
        await session.commit()
        with pytest.raises(DBAPIError, match="refused"):
            await session.execute(text("DELETE FROM ledger_entries"))


async def test_links_between_two_owners_are_refused_also_in_single_user_mode(sessionmaker, ctx):
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO users (id, username, password_hash, is_admin, is_active, session_version) "
            "VALUES (5, 'u5', 'x', false, true, 1)"))
        mine = (await session.execute(text(ENTRY_SQL.format(uid="NULL", n=1)))).scalar()
        theirs = (await session.execute(text(ENTRY_SQL.format(uid=5, n=1)))).scalar()
        await session.commit()
    for statement in (
        f"INSERT INTO ledger_entry_links (user_id, entry_id, related_id) VALUES (NULL, {min(mine, theirs)}, "
        f"{max(mine, theirs)})",
        f"UPDATE ledger_entries SET status = 'superseded', superseded_by = {theirs} WHERE id = {mine}",
        f"UPDATE ledger_entries SET status = 'superseded', superseded_by = {mine} WHERE id = {theirs}",
    ):
        async with sessionmaker() as session:
            with pytest.raises(IntegrityError, match="foreign key"):
                await session.execute(text(statement))
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="foreign key"):
            await session.execute(text("DELETE FROM users WHERE id = 5"))
        await session.rollback()
        assert await panel_users._has_ledger(session, 5)


@pytest.mark.parametrize("statement, constraint", [
    ("INSERT INTO ledger_entries (kind, number, ref, status, title, observed_on, created_by, content_hash, "
     "certainty, subtype) VALUES ('quirk', 7, 'Q7', 'current', 't', now(), 's', 'h', 'tested', 'friction')",
     "ck_ledger_entries_feedback_fields"),
    ("INSERT INTO ledger_entries (kind, number, ref, status, title, observed_on, created_by, content_hash, "
     "subtype, seen, origin) VALUES ('feedback', 7, 'F7', 'to_triage', 't', now(), 's', 'h', 'friction', false, "
     "'owner')", "ck_ledger_entries_seen"),
    ("INSERT INTO ledger_entries (kind, number, ref, status, title, observed_on, created_by, content_hash, "
     "certainty) VALUES ('quirk', 7, 'Q8', 'current', 't', now(), 's', 'h', 'tested')", "ck_ledger_entries_ref"),
    ("INSERT INTO ledger_entries (kind, number, ref, status, title, observed_on, created_by, content_hash, "
     "certainty) VALUES ('quirk', 7, 'Q7', 'open', 't', now(), 's', 'h', 'tested')", "ck_ledger_entries_status"),
    ("INSERT INTO ledger_entries (kind, number, ref, status, title, observed_on, created_by, content_hash, "
     "target, proposed_text) VALUES ('proposal', 7, 'P7', 'accepted', 't', now(), 's', 'h', 'x', 'y')",
     "ck_ledger_entries_proposal_fields"),
    ("INSERT INTO ledger_revisions (entry_id, topic_id, action, reason, before, created_by) VALUES (NULL, NULL, "
     "'update', 'r', '{}', 's')", "ck_ledger_revisions_one_subject"),
])
async def test_the_checks_refuse_fields_of_the_wrong_kind(sessionmaker, ctx, statement, constraint):
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match=constraint):
            await session.execute(text(statement))


# ── The tools ───────────────────────────────────────────────────────────────

async def test_add_numbers_refs_refuses_unknowns_and_returns_similar(ctx):
    q1 = await add_quirk("Voice relay cannot read images")
    q2 = await add_quirk("Voice relay cannot read PDF files", area="agent-personnel")
    assert (q1.ref, q2.ref) == ("Q1", "Q2") and q2.revision == 1
    assert [s["ref"] for s in q2.similar] == ["Q1"]

    unknown = await tools.ledger_add_impl("quirk", "x", area="nowhere", details={"certainty": "tested"})
    assert code(unknown) == "invalid_argument"
    assert {a["name"] for a in unknown.allowed} == {"area/agent-personnel", "area/home"}
    assert next(a for a in unknown.allowed if a["name"] == "area/agent-personnel")["description"] == \
        "How agents are set up."
    wrong_field = await tools.ledger_add_impl("quirk", "x", details={"certainty": "tested", "subtype": "friction"})
    assert code(wrong_field) == "invalid_argument" and "certainty_note" in str(wrong_field.allowed)
    no_topic = await tools.ledger_add_impl("quirk", "x", topics=["voice"], details={"certainty": "tested"})
    assert code(no_topic) == "invalid_argument"

    f1 = await tools.ledger_add_impl("feedback", "Unclear rule", details={"subtype": "unclear_instruction"})
    assert (f1.ref, f1.status) == ("F1", "to_triage")
    f2 = await tools.ledger_add_impl("feedback", "Owner wish", area="home", origin="owner",
                                     details={"subtype": "suggestion"})
    assert f2.status == "open"
    p = await tools.ledger_add_impl("proposal", "New line", status="rejected",
                                    details={"target": "Common-rules", "proposed_text": "Do X."})
    assert p.ref == "P1"
    missing_final = await tools.ledger_add_impl("proposal", "New", status="accepted",
                                                details={"target": "t", "proposed_text": "x"})
    assert code(missing_final) == "invalid_argument"


async def test_update_needs_the_revision_keeps_history_and_closes(ctx):
    q = await add_quirk("Something about voice")
    stale = await tools.ledger_update_impl(ref=q.ref, expected_revision=7, reason="r", title="t")
    assert code(stale) == "stale_revision" and stale.current_revision == 1
    no_reason = await tools.ledger_update_impl(ref=q.ref, expected_revision=1, title="t")
    assert code(no_reason) == "invalid_argument"

    up = await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="seen again", append="Still true.")
    assert up.revision == 2 and up.changed == ["body (appended)"]
    got = (await tools.ledger_get_impl([q.ref], history=True)).entries[0]
    assert got.body.endswith("2026-11-05: Still true.")
    assert [(h["revision"], h["reason"]) for h in got.history] == [(1, "seen again")]
    assert got.history[0]["before"]["body"] == ""

    dup = await add_quirk("Something about voice again")
    sup = await tools.ledger_update_impl(ref=dup.ref, expected_revision=1, reason="duplicate", status="superseded",
                                         superseded_by=q.ref)
    assert sup.status == "superseded"
    assert (await tools.ledger_get_impl([dup.ref])).entries[0].superseded_by == q.ref
    self_sup = await tools.ledger_update_impl(ref=q.ref, expected_revision=2, reason="r", status="superseded",
                                              superseded_by=q.ref)
    assert code(self_sup) == "invalid_argument"

    f = await tools.ledger_add_impl("feedback", "Friction", area="home", details={"subtype": "friction"})
    no_account = await tools.ledger_update_impl(ref=f.ref, expected_revision=1, reason="done", status="handled")
    assert code(no_account) == "invalid_argument"
    done = await tools.ledger_update_impl(ref=f.ref, expected_revision=1, reason="done", status="handled",
                                          handled_account="Fixed the note.")
    assert done.status == "handled"
    reopened = await tools.ledger_update_impl(ref=f.ref, expected_revision=2, reason="came back", status="open")
    assert reopened.status == "open"
    assert (await tools.ledger_get_impl([f.ref])).entries[0].handled_account is None


async def test_seen_is_bulk_without_reason_and_feedback_only(ctx):
    f1 = await tools.ledger_add_impl("feedback", "One", details={"subtype": "friction"})
    f2 = await tools.ledger_add_impl("feedback", "Two", details={"subtype": "friction"})
    q = await add_quirk("A quirk")
    marked = await tools.ledger_update_impl(refs=[f1.ref, f2.ref, q.ref, "F99"], seen=True)
    assert marked.seen == {"marked": ["F1", "F2"], "already_seen": [], "not_feedback": ["Q1"], "not_found": ["F99"]}
    unseen = await tools.ledger_search_impl(unseen=True)
    assert unseen.entries == []
    got = (await tools.ledger_get_impl([f1.ref])).entries[0]
    assert got.seen is True and got.revision == 1


async def test_search_words_and_meaning_are_two_lists(sessionmaker, ctx):
    a = await add_quirk("Claude Code cannot open a browser", body="It needs an extension.")
    b = await add_quirk("Voice cannot show images")
    await tools.ledger_add_impl("feedback", "Browser friction", area="home", details={"subtype": "friction"})
    listed = await tools.ledger_search_impl(kinds=["quirk"])
    assert [e.ref for e in listed.entries] == [b.ref, a.ref]

    before = await tools.ledger_search_impl(query="browser", kinds=["quirk"])
    assert [e.ref for e in before.by_words] == [a.ref]
    assert before.by_meaning == [] and before.not_embedded == 2
    await embed_all(sessionmaker)
    after = await tools.ledger_search_impl(query=ledger.embed_text(
        (await _entry(sessionmaker, a.ref))), kinds=["quirk"])
    assert after.by_meaning[0].ref == a.ref and after.by_meaning[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert after.not_embedded == 0
    by_ref = await tools.ledger_search_impl(query=a.ref, mode="words")
    assert by_ref.by_words[0].ref == a.ref and by_ref.by_meaning is None

    # An outdated vector is never used: changed text, no meaning hit until re-embedded.
    await tools.ledger_update_impl(ref=a.ref, expected_revision=1, reason="r", body="Different now.")
    stale = await tools.ledger_search_impl(query="anything", mode="meaning", kinds=["quirk"])
    assert a.ref not in [e.ref for e in stale.by_meaning] and stale.not_embedded == 1


async def test_meaning_list_says_when_the_provider_is_down(ctx, monkeypatch):
    await add_quirk("Something")

    async def down(text_):
        raise ConnectionError("refused")

    monkeypatch.setattr(ledger_embed, "get_embedding", down)
    result = await tools.ledger_search_impl(query="something")
    assert [e.ref for e in result.by_words] == ["Q1"]
    assert result.by_meaning == [] and "unavailable" in result.meaning_unavailable


async def _entry(sessionmaker, ref):
    async with sessionmaker() as session:
        kind, number = ledger.parse_ref(ref)
        return (await session.execute(select(LedgerEntry).where(
            LedgerEntry.kind == kind, LedgerEntry.number == number))).scalar_one()


async def test_views_cite_entries_and_are_flagged_when_one_changes(ctx):
    q1 = await add_quirk("Voice: no images")
    q2 = await add_quirk("Code: has a browser")
    created = await tools.ledger_digest_edit_impl("claude-surfaces", "start the matrix", [
        {"op": "create", "layout": "matrix", "title": "What Claude can do", "description": "Read before saying "
         "something must be done by hand.", "columns": ["voice", "claude-code"]},
        {"op": "add", "row": "Images", "column": "voice", "text": "no", "sources": [q1.ref]},
        {"op": "add", "row": "Browser", "column": "claude-code", "text": "yes", "sources": [q2.ref]},
    ])
    assert created.error is None and created.created is True and len(created.applied) == 3
    bad_column = await tools.ledger_digest_edit_impl("claude-surfaces", "r", [
        {"op": "add", "row": "X", "column": "nope", "text": "y"}])
    assert code(bad_column) == "invalid_argument"

    plain = await tools.ledger_digest_impl("claude-surfaces")
    assert "| Images | no (Q1; newest 2026-11-05) |  |" in plain.markdown and plain.lines is None
    view = await tools.ledger_digest_impl("claude-surfaces", with_ids=True)
    assert "no (Q1; newest 2026-11-05) [line" in view.markdown
    line = next(l for l in view.lines if l["row"] == "Images")

    up = await tools.ledger_update_impl(ref=q1.ref, expected_revision=1, reason="images work now", status="stale")
    assert [f.line for f in up.flagged_lines] == [line["line"]]
    flagged = await tools.ledger_digest_impl("claude-surfaces", with_ids=True)
    assert flagged.lines_under_review == 1 and "under review: Q1 changed: images work now" in flagged.markdown
    old = await tools.ledger_digest_edit_impl("claude-surfaces", "r", [
        {"op": "resolve", "line": line["line"], "expected_revision": line["revision"]}])
    assert code(old) == "stale_revision"
    fixed = await tools.ledger_digest_edit_impl("claude-surfaces", "images work now", [
        {"op": "replace", "line": line["line"], "expected_revision": line["revision"] + 1, "text": "yes"}])
    assert fixed.error is None
    view = await tools.ledger_digest_impl("claude-surfaces")
    assert "| Images | yes (Q1 stale; newest 2026-11-05) |" in view.markdown and view.lines_under_review == 0

    listed = await tools.ledger_digest_impl()
    assert [v["slug"] for v in listed.views] == ["claude-surfaces"]


async def test_topics_create_merge_and_count(ctx):
    for slug, kind in (("voice", "surface"), ("voice-bridge", "system"), ("relay", "system")):
        made = await tools.ledger_topic_edit_impl("create", slug, "needed", kind=kind, description=f"{slug} things")
        assert made.error is None and "Tell the owner" in made.notes[0]
    q = await add_quirk("Relay drops", topics=["voice", "relay"])
    merged = await tools.ledger_topic_edit_impl("merge", "relay", "same thing", into="voice-bridge")
    assert merged.moved == [q.ref] and merged.topic["status"] == "merged"
    got = (await tools.ledger_get_impl([q.ref])).entries[0]
    assert got.topics == ["voice", "voice-bridge"]
    use_merged = await tools.ledger_add_impl("quirk", "x", topics=["relay"], details={"certainty": "tested"})
    assert code(use_merged) == "invalid_argument" and "voice-bridge" in use_merged.error
    by_old_name = await tools.ledger_search_impl(topics=["relay"])
    assert [e.ref for e in by_old_name.entries] == [q.ref]
    listed = await tools.ledger_topics_impl()
    counts = {t["name"]: t["entries_active"] for t in listed.topics}
    assert counts == {"voice": 1, "voice-bridge": 1, "relay": 0}
    assert {a["name"] for a in listed.areas} == {"area/agent-personnel", "area/home"}


async def test_the_view_hint_appears_past_the_threshold(ctx, monkeypatch):
    monkeypatch.setattr(settings, "ledger_view_hint_min_quirks", 3)
    await tools.ledger_topic_edit_impl("create", "voice", "needed", kind="surface", description="Voice")
    results = [await add_quirk(f"Voice quirk {i}", topics=["voice"]) for i in range(3)]
    assert results[1].view_hints is None
    assert results[2].view_hints[0]["topic"] == "voice"


async def test_writes_need_write_permission(ctx):
    token = current_permission.set("read")
    try:
        assert code(await tools.ledger_add_impl("quirk", "x", details={"certainty": "tested"})) == "permission_denied"
        assert code(await tools.ledger_update_impl(refs=["F1"], seen=True)) == "permission_denied"
    finally:
        current_permission.reset(token)


# ── Vectors: the sweep, the reset ───────────────────────────────────────────

async def test_the_embed_stage_sweeps_ledger_entries_within_its_budget(sessionmaker, ctx, monkeypatch):
    for i in range(3):
        await add_quirk(f"Entry {i}")
    budget = indexer.EmbedBudget(chunk_budget=1, enforced=True)
    outcome = indexer.EmbedPassResult()
    budget.notes_completed = 1  # a note of the stage already ran
    done = await ledger_embed.sweep(None, budget, outcome, "", lambda: False)
    assert done == 1 and budget.stopped is indexer.EmbedStop.BUDGET
    unlimited = indexer.EmbedBudget()
    assert await ledger_embed.sweep(None, unlimited, indexer.EmbedPassResult(), "", lambda: False) == 2
    async with sessionmaker() as session:
        assert (await session.execute(select(LedgerEmbedding.entry_id))).scalars().all()

    async with sessionmaker() as session:
        await ledger_embed.reset_vectors(session, DIM)
        await session.commit()
        assert (await session.execute(text("SELECT count(*) FROM ledger_embeddings"))).scalar() == 0
        assert (await session.execute(text(
            "SELECT count(*) FROM ledger_entries WHERE embedded_hash IS NULL"))).scalar() == 3


async def test_a_vector_for_text_that_changed_meanwhile_is_discarded(sessionmaker, ctx, monkeypatch):
    q = await add_quirk("Before")
    entry_id = (await _entry(sessionmaker, q.ref)).id
    real = ledger_embed.get_embeddings_batch

    async def edit_during_the_call(chunks):
        await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="r", title="After")
        return await real(chunks)

    monkeypatch.setattr(ledger_embed, "get_embeddings_batch", edit_during_the_call)
    result = await ledger_embed.embed_entry(entry_id)
    assert result.embedded is False and result.skipped == "changed meanwhile"
    async with sessionmaker() as session:
        assert (await session.execute(text("SELECT count(*) FROM ledger_embeddings"))).scalar() == 0
