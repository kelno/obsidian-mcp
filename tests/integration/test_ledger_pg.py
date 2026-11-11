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
from datetime import date, datetime, timezone

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
        FROM pg_constraint c WHERE c.conrelid::regclass::text LIKE 'ledger%' AND c.contype <> 't'""")
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
    for table in ("ledger_entries", "ledger_topics", "ledger_digest_lines"):
        assert f"trg_{table}_keeps_history" in names
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


async def test_an_entry_embedded_during_the_query_call_is_not_also_called_unembedded(sessionmaker, ctx, monkeypatch):
    # Found live: the background queue committed the new entry's vector while
    # the search waited on the provider for the query's, so the entry was in
    # by_meaning and counted in not_embedded at once.
    added = await add_quirk("Smoke test of the ledger tools")
    real = ledger_embed.get_embedding

    async def slow_query_embedding(text_):
        await embed_all(sessionmaker)  # the queue's worker, finishing meanwhile
        return await real(text_)

    monkeypatch.setattr(ledger_embed, "get_embedding", slow_query_embedding)
    result = await tools.ledger_search_impl(query="Smoke test of the ledger tools", mode="meaning")
    assert [e.ref for e in result.by_meaning] == [added.ref]
    assert not result.not_embedded and not result.notes


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

    # A source's date is the later of its observed_on (TODAY, frozen above) and the day the database last changed it, which is the day of the run.
    newest = max(TODAY, datetime.now(timezone.utc).date()).isoformat()
    plain = await tools.ledger_digest_impl("claude-surfaces")
    assert f"| Images | no (Q1; newest {newest}) |  |" in plain.markdown and plain.lines is None
    view = await tools.ledger_digest_impl("claude-surfaces", with_ids=True)
    assert f"no (Q1; newest {newest}) [line" in view.markdown
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
    assert f"| Images | yes (Q1 stale; newest {newest}) |" in view.markdown and view.lines_under_review == 0

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


# ── Import and export ───────────────────────────────────────────────────────

IMPORT_FILE = {
    "topics": [{"slug": "voice", "kind": "surface", "description": "The voice app"}],
    "entries": [
        {"ref": "Q01", "status": "current", "title": "Voice cannot read images", "body": "Seen twice.",
         "area": "area/agent-personnel", "topics": ["voice"], "details": {"certainty": "tested"},
         "observed_on": "2026-10-02", "created_at": "2026-10-02T09:00:00+00:00", "related": ["Q03"]},
        {"ref": "Q02", "status": "superseded", "superseded_by": "Q03", "title": "Old wording",
         "details": {"certainty": "inferred"}, "observed_on": "2026-10-03"},
        {"ref": "Q03", "status": "current", "title": "Newer wording", "details": {"certainty": "docs"},
         "observed_on": "2026-10-04"},
        {"ref": "F1", "status": "handled", "handled_account": "Done by hand.", "title": "Owner asked",
         "origin": "owner", "area": "area/home", "details": {"subtype": "suggestion"},
         "observed_on": "2026-10-05"},
    ],
    "digests": [{"slug": "claude-surfaces", "title": "Surfaces", "description": "Read first.", "layout": "matrix",
                 "columns": ["voice"], "lines": [{"row": "Images", "column": "voice", "text": "no",
                                                  "sources": ["Q01"]}]}],
}


class _NoEngine:
    async def dispose(self):
        pass


async def test_import_keeps_refs_and_dates_and_exports_back(sessionmaker, ctx, monkeypatch, tmp_path):
    import argparse

    from scripts import ledger_export, ledger_import

    for module in (ledger_import, ledger_export):
        monkeypatch.setattr(module, "async_session", sessionmaker)
        monkeypatch.setattr(module, "engine", _NoEngine())
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(IMPORT_FILE), encoding="utf-8")
    args = argparse.Namespace(file=str(path), user=None, single_user=True, dry_run=True, skip_area_check=False)
    dry = await ledger_import.run(args)
    assert dry["quirk"] == 3 and dry["feedback"] == 1
    assert (await tools.ledger_search_impl(status="any")).entries == []

    args.dry_run = False
    await ledger_import.run(args)
    got = (await tools.ledger_get_impl(["Q1", "Q02", "F1"])).entries
    assert [g.ref for g in got] == ["Q01", "Q02", "F1"]
    assert got[0].observed_on == "2026-10-02" and got[0].created_at.startswith("2026-10-02T09:00")
    assert got[0].related == ["Q03"] and got[1].superseded_by == "Q03"
    assert got[2].seen is True and got[2].handled_account == "Done by hand."
    new = await add_quirk("After the import")
    assert new.ref == "Q4"
    view = await tools.ledger_digest_impl("claude-surfaces")
    assert "| Images | no (Q01; newest" in view.markdown

    with pytest.raises(Exception, match="uq_ledger"):
        await ledger_import.run(args)

    exported = json.loads(ledger_export.as_json(await ledger_export.collect(
        argparse.Namespace(user=None, single_user=True, history=False))))
    assert [e["ref"] for e in exported["entries"]] == ["F1", "Q01", "Q02", "Q03", "Q4"]
    q1 = next(e for e in exported["entries"] if e["ref"] == "Q01")
    assert q1["topics"] == ["voice"] and q1["related"] == ["Q03"] and q1["details"] == {"certainty": "tested"}
    assert exported["digests"][0]["lines"][0]["sources"] == ["Q01"]
    markdown = ledger_export.as_markdown(await ledger_export.collect(
        argparse.Namespace(user=None, single_user=True, history=True)))
    assert "### Q01 · Voice cannot read images" in markdown


async def test_import_refuses_an_unknown_area_and_writes_nothing(sessionmaker, ctx, monkeypatch, tmp_path):
    import argparse

    from scripts import ledger_import

    monkeypatch.setattr(ledger_import, "async_session", sessionmaker)
    monkeypatch.setattr(ledger_import, "engine", _NoEngine())
    bad = {"topics": [{"slug": "voice", "kind": "surface", "description": "d"}],
           "entries": [{"ref": "Q1", "status": "current", "title": "t", "area": "area/nowhere",
                        "details": {"certainty": "tested"}}]}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ledger_import.ImportError_, match="area/nowhere is declared by no indexed main note"):
        await ledger_import.run(argparse.Namespace(file=str(path), user=None, single_user=True, dry_run=False,
                                                   skip_area_check=False))
    assert (await tools.ledger_search_impl(status="any")).entries == []
    assert (await tools.ledger_topics_impl()).topics == []


# ── Regressions from the adversarial review ─────────────────────────────────

async def test_a_no_op_update_is_refused_not_crashed_and_pointers_count_as_changes(ctx):
    q = await add_quirk("Same title")
    same = await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="r", title="Same title")
    assert code(same) == "invalid_argument" and "nothing to change" in same.error
    a, b = await add_quirk("Target A"), await add_quirk("Target B")
    first = await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="dup", status="superseded",
                                           superseded_by=a.ref)
    moved = await tools.ledger_update_impl(ref=q.ref, expected_revision=first.revision, reason="wrong target",
                                           superseded_by=b.ref)
    assert moved.error is None and moved.changed == ["superseded_by"]
    f = await tools.ledger_add_impl("feedback", "F", area="home", details={"subtype": "friction"})
    done = await tools.ledger_update_impl(ref=f.ref, expected_revision=1, reason="r", status="handled",
                                          handled_account="first")
    again = await tools.ledger_update_impl(ref=f.ref, expected_revision=done.revision, reason="clearer",
                                           status="handled", handled_account="second")
    assert again.changed == ["handled_account"]


async def test_superseding_refuses_a_closed_target_and_closing_a_target(ctx):
    a, b = await add_quirk("A"), await add_quirk("B")
    await tools.ledger_update_impl(ref=b.ref, expected_revision=1, reason="dup", status="superseded",
                                   superseded_by=a.ref)
    loop = await tools.ledger_update_impl(ref=a.ref, expected_revision=1, reason="dup", status="superseded",
                                          superseded_by=b.ref)
    assert code(loop) == "invalid_argument" and f"{b.ref} is superseded by {a.ref}" in loop.error
    close = await tools.ledger_update_impl(ref=a.ref, expected_revision=1, reason="wrong", status="retracted")
    assert code(close) == "invalid_argument" and f"{b.ref} is superseded by {a.ref}" in close.error
    c, d = await add_quirk("C"), await add_quirk("D")
    await tools.ledger_update_impl(ref=c.ref, expected_revision=1, reason="mistake", status="retracted")
    onto_closed = await tools.ledger_update_impl(ref=d.ref, expected_revision=1, reason="dup",
                                                 status="superseded", superseded_by=c.ref)
    assert code(onto_closed) == "invalid_argument" and "is itself retracted" in onto_closed.error


async def test_the_database_refuses_a_change_without_history(sessionmaker, ctx):
    q = await add_quirk("Kept")
    for statement in ("UPDATE ledger_entries SET title = 'rewritten'",
                      "UPDATE ledger_entries SET title = 'rewritten', revision = revision + 1"):
        async with sessionmaker() as session:
            await session.execute(text(statement))
            with pytest.raises(DBAPIError, match="keeps no history"):
                await session.commit()
    async with sessionmaker() as session:
        await session.execute(text("UPDATE ledger_entries SET embedded_hash = 'x'"))
        await session.commit()
        await session.execute(text("SET LOCAL ledger.allow_delete = 'on'"))
        await session.execute(text("UPDATE ledger_entries SET title = 'by the owner'"))
        await session.commit()
    up = await tools.ledger_update_impl(ref=q.ref, expected_revision=1, reason="r", title="through the tool")
    assert up.error is None and up.revision == 2


async def test_a_matrix_cell_holds_one_live_line(sessionmaker, ctx):
    q = await add_quirk("Q")
    await tools.ledger_digest_edit_impl("m", "start", [
        {"op": "create", "layout": "matrix", "title": "M", "description": "d", "columns": ["voice"]},
        {"op": "add", "row": "Images", "column": "voice", "text": "no", "sources": [q.ref]},
        {"op": "add", "row": "Browser", "column": "voice", "text": "yes"},
    ])
    lines = (await tools.ledger_digest_impl("m", with_ids=True)).lines
    browser = next(l for l in lines if l["row"] == "Browser")
    moved = await tools.ledger_digest_edit_impl("m", "r", [
        {"op": "replace", "line": browser["line"], "expected_revision": browser["revision"], "row": "Images"}])
    assert code(moved) == "already_exists"
    typo = await tools.ledger_digest_edit_impl("m", "r", [
        {"op": "replace", "line": browser["line"], "expected_revision": browser["revision"], "txt": "x"}])
    assert code(typo) == "invalid_argument" and "txt is not a key of replace" in typo.error
    empty = await tools.ledger_digest_edit_impl("m", "r", [
        {"op": "replace", "line": browser["line"], "expected_revision": browser["revision"]}])
    assert code(empty) == "invalid_argument"
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError, match="uq_ledger_digest_lines_live_cell"):
            await session.execute(text(
                "UPDATE ledger_digest_lines SET row_label = 'Images', revision = revision + 1 WHERE id = :i"),
                {"i": browser["line"]})


async def test_a_long_view_is_cut_at_the_response_cap(ctx, monkeypatch):
    monkeypatch.setattr(settings, "max_read_response_chars", 1000)
    ops = [{"op": "create", "layout": "list", "title": "L", "description": "d"}]
    ops += [{"op": "add", "text": f"line {i} " + "x" * 150} for i in range(20)]
    assert (await tools.ledger_digest_edit_impl("long", "r", ops)).error is None
    view = await tools.ledger_digest_impl("long", with_ids=True)
    assert view.lines_not_shown and len(view.lines) == 20 - view.lines_not_shown
    assert len(view.markdown) < 1000


async def test_meaning_search_ranks_entries_not_chunks(sessionmaker, ctx):
    long_body = "\n\n".join(f"paragraph {i} " + "word " * 450 for i in range(6))
    assert len(long_body) < ledger.BODY_MAX
    await tools.ledger_add_impl("proposal", "Long", body=long_body, status="rejected",
                                details={"target": "t", "proposed_text": "p"})
    for i in range(3):
        await add_quirk(f"Short {i}")
    await embed_all(sessionmaker)
    result = await tools.ledger_search_impl(query="paragraph 3", mode="meaning", limit=4, kinds=None)
    assert len(result.by_meaning) == 4
    assert len({e.ref for e in result.by_meaning}) == 4


async def test_entries_failing_at_the_provider_are_tried_last(sessionmaker, ctx, monkeypatch):
    bad = [await add_quirk(f"Rejected {i}") for i in range(3)]
    good = [await add_quirk(f"Fine {i}") for i in range(2)]
    real = ledger_embed.get_embeddings_batch

    async def picky(chunks):
        if any(c.startswith("Rejected") for c in chunks):
            raise ConnectionError("the provider rejects this one")
        return await real(chunks)

    monkeypatch.setattr(ledger_embed, "get_embeddings_batch", picky)
    ledger_embed._failed_entries.clear()
    for _ in range(2):
        budget = indexer.EmbedBudget(provider_failure_limit=3)
        await ledger_embed.sweep(None, budget, indexer.EmbedPassResult(), "", lambda: False)
    async with sessionmaker() as session:
        embedded = set((await session.execute(select(LedgerEntry.ref).where(
            LedgerEntry.embedded_hash == LedgerEntry.content_hash))).scalars().all())
    assert embedded == {g.ref for g in good}
    assert not embedded & {b.ref for b in bad}


# ── Task links (ledger_tasks) ───────────────────────────────────────────────

async def _task_note(sessionmaker, root, rel="t.md", body="# T\n"):
    """A note on disk and in the index, for the task tools to write into."""
    (root / rel).write_text(body, encoding="utf-8")
    async with sessionmaker() as session:
        await session.execute(text(
            "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash) "
            "VALUES (NULL, :p, 't', '{}', '{}', 'h')"), {"p": rel})
        await session.commit()


def without_day(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "updated"}


async def test_feedback_leads_replace_fix(ctx):
    added = await tools.ledger_add_impl("feedback", "Leads", area="home",
                                        details={"subtype": "friction", "leads": "maybe X"})
    assert added.error is None, added.error
    got = (await tools.ledger_get_impl([added.ref])).entries[0]
    assert got.details["leads"] == "maybe X"
    old_name = await tools.ledger_add_impl("feedback", "Old", area="home",
                                           details={"subtype": "friction", "fix": "x"})
    assert code(old_name) == "invalid_argument" and "leads" in str(old_name.allowed)


async def test_a_task_citing_feedback_is_linked_both_ways(sessionmaker, ctx):
    await _task_note(sessionmaker, ctx)
    entry = await tools.ledger_add_impl("feedback", "Bridge loops", area="home", details={"subtype": "friction"})
    other = await tools.ledger_add_impl("feedback", "Other thing", area="home", details={"subtype": "friction"})

    created = await tools.create_task_impl(f"Fix the loop [{entry.ref.replace('F', 'F0')}]", path="t.md")
    assert created.error is None, created.error
    assert [without_day(r) for r in created.ledger_refs] == [
        {"ref": entry.ref, "kind": "feedback", "title": "Bridge loops", "status": "open", "tasks": "open"}]
    assert created.ledger_refs[0]["updated"]
    got = (await tools.ledger_get_impl([entry.ref])).entries[0]
    assert got.tasks_state == "open" and got.tasks == [
        {"ref": f"t.md:{created.line}", "status": "TODO", "text": created.text}]
    listed = await tools.ledger_search_impl(kinds=["feedback"])
    assert {e.ref: e.tasks for e in listed.entries} == {entry.ref: "open", other.ref: None}
    assert (await tools.ledger_search_impl(tasks="done")).entries == []

    done = await tools.set_task_status_impl("t.md", created.line, created.text, created.content_hash, status="done")
    assert done.error is None, done.error
    assert [without_day(r) for r in done.ledger_refs] == [
        {"ref": entry.ref, "kind": "feedback", "title": "Bridge loops", "status": "open", "tasks": "done"}]
    assert [e.ref for e in (await tools.ledger_search_impl(tasks="done")).entries] == [entry.ref]
    by_query = await tools.ledger_search_impl(query="loops", mode="words", tasks="done")
    assert [e.tasks for e in by_query.by_words] == ["done"]

    # A second task citing it, still open: the entry is open again.
    second = await tools.create_task_impl(f"Check it holds [{entry.ref}]", path="t.md")
    assert second.ledger_refs[0]["tasks"] == "open"
    assert (await tools.ledger_search_impl(tasks="done")).entries == []

    # Only cancelled tasks: the decision was dropped.
    dropped = await tools.create_task_impl(f"Do it [{other.ref}]", path="t.md")
    cancelled = await tools.set_task_status_impl("t.md", dropped.line, dropped.text, dropped.content_hash,
                                                 status="cancelled")
    assert cancelled.ledger_refs[0]["tasks"] == "cancelled"
    assert [e.ref for e in (await tools.ledger_search_impl(tasks="cancelled")).entries] == [other.ref]

    bad = await tools.ledger_search_impl(tasks="closed")
    assert code(bad) == "invalid_argument"


async def test_a_task_in_a_note_the_index_lacks_still_counts(sessionmaker, ctx):
    """The adversarial review's case: an open task added where the immediate
    re-index cannot run (a new inbox, not yet indexed) beside a done one must
    not report the entry as done."""
    await _task_note(sessionmaker, ctx)
    entry = await tools.ledger_add_impl("feedback", "Half done", area="home", details={"subtype": "friction"})
    first = await tools.create_task_impl(f"Part one [{entry.ref}]", path="t.md")
    await tools.set_task_status_impl("t.md", first.line, first.text, first.content_hash, status="done")
    second = await tools.create_task_impl(f"Part two [{entry.ref}]")  # the inbox, created now
    assert second.error is None and second.created_note is True and second.indexed is False, second
    assert second.ledger_refs[0]["tasks"] == "open"


async def test_task_tools_point_out_a_bare_ref_and_an_unknown_one(sessionmaker, ctx):
    await _task_note(sessionmaker, ctx, "u.md")
    entry = await tools.ledger_add_impl("feedback", "Named", area="home", details={"subtype": "friction"})
    bare = await tools.create_task_impl(f"See {entry.ref} later, F5 key, `[{entry.ref}]` in code", path="u.md")
    assert bare.ledger_refs is None
    assert bare.notes and f"mentions {entry.ref} without brackets" in bare.notes[0] and len(bare.notes) == 1
    assert f"write [{entry.ref}]" in bare.notes[0]
    unknown = await tools.create_task_impl("Do [F999]", path="u.md")
    assert unknown.ledger_refs == [{"ref": "F999", "status": "unknown"}]
    edited = await tools.edit_task_impl("u.md", bare.line, bare.text, unknown.content_hash,
                                        description=f"See it later [{entry.ref}]")
    assert edited.error is None, edited.error
    assert edited.ledger_refs[0]["ref"] == entry.ref and edited.notes is None


# ── Everything an agent needs, up front (ledger F34) ─────────────────────────

async def test_one_add_refusal_names_every_problem_and_writes_nothing(ctx):
    """Tonight's case: the title's limit, the missing subtype and an unknown
    topic were three refusals in a row; now they are one."""
    result = await tools.ledger_add_impl("feedback", "x" * 151, area="nowhere", topics=["no-such-topic"],
                                         details={"component": "c"}, related=["F404"])
    assert code(result) == "invalid_argument"
    fields = [p["field"] for p in result.problems]
    assert fields == ["title", "details.subtype", "area", "topics", "related"], fields
    by_field = {p["field"]: p for p in result.problems}
    assert {a["name"] for a in by_field["details.subtype"]["allowed"]} == {
        "unclear_instruction", "friction", "suggestion"}
    assert {a["name"] for a in by_field["area"]["allowed"]} == {"area/agent-personnel", "area/home"}
    assert by_field["related"]["code"] == "not_found"
    assert "5 problems" in result.error
    assert (await tools.ledger_search_impl()).entries == []


async def test_a_wrong_kind_skips_the_checks_that_depend_on_it(ctx):
    result = await tools.ledger_add_impl("bug", "fine", details={"certainty": "tested"})
    assert code(result) == "invalid_argument" and result.problems is None
    assert "kind must be one of" in result.error


async def test_a_value_outside_the_published_enum_still_reaches_the_ledgers_refusal(ctx):
    """The enums describe; the server decides: through the registered tool a
    wrong value gets the ledger's own refusal, not a generic validation error."""
    from src.mcp_server.server import mcp

    _, structured = await mcp.call_tool("ledger_add", {
        "kind": "quirk", "title": "x", "details": {"certainty": "sure"}})
    assert "details.certainty must be one of" in structured["error"]


async def test_an_entry_without_topics_is_nudged_with_names_and_suggestions(ctx):
    await tools.ledger_topic_edit_impl("create", "voice-bridge", "needed", kind="system", description="Bridge")
    first = await add_quirk("Relay loses the thread on long answers", area="agent-personnel",
                            topics=["voice-bridge"])
    assert first.areas_and_topics is None and not any("no topics" in n for n in first.notes or [])
    second = await add_quirk("Relay loses the thread again")
    assert second.areas_and_topics == {"areas": "agent-personnel, home", "topics": "voice-bridge"}
    assert second.similar[0]["topics"] == ["voice-bridge"] and second.similar[0]["area"] == "area/agent-personnel"
    nudge = next(n for n in second.notes if "has no topics" in n)
    assert "if none fits, nothing to do" in nudge
    assert "Similar entries carry: voice-bridge." in nudge
    assert f'ref="{second.ref}", expected_revision=1' in nudge
    assert any("has no area" in n for n in second.notes)


async def test_search_results_carry_the_area_and_topic_names(ctx):
    await tools.ledger_topic_edit_impl("create", "voice", "needed", kind="surface", description="Voice")
    for result in (await tools.ledger_search_impl(), await tools.ledger_search_impl(query="anything")):
        assert result.areas_and_topics == {"areas": "agent-personnel, home", "topics": "voice"}


async def test_words_fall_back_to_any_word_when_no_entry_holds_them_all(ctx):
    """The agent test's case: "Homepie brightness preset" found nothing,
    though an entry about brightness existed."""
    await add_quirk("Node-RED light flow ignores brightness below 5%", area="home")
    result = await tools.ledger_search_impl(query="Homepie brightness preset", mode="words")
    assert [e.ref for e in result.by_words] == ["Q1"]
    assert any("No entry holds every word" in n for n in result.notes)
    exact = await tools.ledger_search_impl(query="light brightness", mode="words")
    assert [e.ref for e in exact.by_words] == ["Q1"] and not any("every word" in n for n in exact.notes or [])


async def test_an_add_with_nothing_similar_says_so(ctx):
    result = await add_quirk("Zebra unicorn telescope", area="home")
    assert result.similar is None
    assert any("No active entry shares words" in n for n in result.notes)


async def test_one_update_refusal_names_every_problem_and_changes_nothing(ctx):
    q = await add_quirk("Original title")
    result = await tools.ledger_update_impl(
        ref=q.ref, expected_revision=1, reason="r", title="y" * 151, topics_add=["nope"],
        details={"certainty": "sure"}, append="kept?")
    assert code(result) == "invalid_argument"
    assert [p["field"] for p in result.problems] == ["title", "details.certainty", "topics_add"]
    got = (await tools.ledger_get_impl([q.ref])).entries[0]
    assert (got.revision, got.title, got.body) == (1, "Original title", "")


async def test_update_still_refuses_a_stale_revision_alone(ctx):
    q = await add_quirk("Stale one")
    result = await tools.ledger_update_impl(ref=q.ref, expected_revision=3, title="z" * 151)
    assert code(result) == "stale_revision" and result.current_revision == 1 and result.problems is None


async def test_the_guide_lists_the_ledgers_rules_topics_and_areas(ctx):
    await tools.ledger_topic_edit_impl("create", "voice", "needed", kind="surface", description="Voice mode")
    guide = await tools.get_vault_guide_impl()
    section = guide.split("## Logging to the agent ledger (this server)", 1)[1].split("\n---\n", 1)[0]
    assert "- `voice`: Voice mode" in section
    assert "Areas: agent-personnel, home." in section
    assert "seen_once" in section and "unclear_instruction" in section and "150 characters" in section



# ── Citations: any kind, current state at write time, dependants on change ───

async def test_a_task_may_cite_a_quirk_and_the_quirk_shows_it_is_pending(sessionmaker, ctx):
    """F60's case: a decision postponed until some event, as a task citing the quirk."""
    await _task_note(sessionmaker, ctx, "q.md")
    quirk = await add_quirk("The app dropped a call at about 30 s", area="home")
    fb = await tools.ledger_add_impl("feedback", "Pending", area="home", details={"subtype": "friction"})
    created = await tools.create_task_impl(
        f"When the waits are reworked: revisit the cap [{quirk.ref}, {fb.ref}] #when", path="q.md")
    assert created.error is None, created.error
    assert [without_day(r) for r in created.ledger_refs] == [
        {"ref": quirk.ref, "kind": "quirk", "title": "The app dropped a call at about 30 s", "status": "current",
         "certainty": "tested", "tasks": "open"},
        {"ref": fb.ref, "kind": "feedback", "title": "Pending", "status": "open", "tasks": "open"}]

    got = await tools.ledger_get_impl([quirk.ref])
    assert got.entries[0].tasks_state == "open" and got.entries[0].tasks[0]["ref"] == f"q.md:{created.line}"
    assert any(n.startswith(f"{quirk.ref}: 1 open vault task cites it") and "pending" in n for n in got.notes)
    listed = await tools.ledger_search_impl(kinds=["quirk"])
    assert {e.ref: e.tasks for e in listed.entries}[quirk.ref] == "open"
    # The tasks filter stays about feedback: a quirk with open tasks is not listed.
    assert [e.ref for e in (await tools.ledger_search_impl(tasks="open")).entries] == [fb.ref]

    done = await tools.set_task_status_impl("q.md", created.line, created.text, created.content_hash, status="done")
    assert done.error is None, done.error
    got = await tools.ledger_get_impl([quirk.ref])
    assert got.entries[0].tasks_state == "done" and got.notes is None


async def test_a_write_shows_the_current_state_of_what_its_text_cites(ctx):
    """F58's case: "untested (Q14)" written while Q14 said tested."""
    quirk = await add_quirk("Long calls work in the background", area="home",
                            details={"certainty": "tested"})
    added = await tools.ledger_add_impl(
        "feedback", f"Wait loop; a longer cap untested ({quirk.ref})", area="home",
        body="See [P404] and F1 itself.", details={"subtype": "friction", "leads": f"read {quirk.ref} first"})
    assert added.error is None, added.error
    assert [without_day(c) for c in added.cites] == [
        {"ref": quirk.ref, "kind": "quirk", "title": "Long calls work in the background", "status": "current",
         "certainty": "tested"},
        {"ref": "P404", "status": "unknown"}]
    assert added.notes[0].startswith("Entries your text cites, as they stand now")
    assert "unknown ref" in added.notes[0]

    updated = await tools.ledger_update_impl(ref=added.ref, expected_revision=1, reason="more", append="Nothing new.")
    assert updated.error is None, updated.error
    assert updated.cites is None
    assert any(n.startswith(f'Does the title still hold? It reads: "Wait loop; a longer cap untested '
                            f'({quirk.ref})". If not') for n in updated.notes)
    retitled = await tools.ledger_update_impl(ref=added.ref, expected_revision=2, reason="fix",
                                              title=f"Wait loop; longer cap tested [{quirk.ref}]",
                                              append="Fixed the title.")
    assert [c["ref"] for c in retitled.cites] == [quirk.ref]
    assert not any("Does the title still hold" in n for n in retitled.notes)


async def test_an_update_lists_what_cites_the_entry(sessionmaker, ctx):
    quirk = await add_quirk("The cap is 20 s", area="home")
    citing = await add_quirk("A summary of the cap", area="home", body=f"Per {quirk.ref}, untested.")
    await add_quirk("Unrelated", area="home", body=f"{quirk.ref}0 is another entry")
    await _task_note(sessionmaker, ctx, "plans.md")
    task = await tools.create_task_impl(f"Revisit the cap [{quirk.ref}]", path="plans.md")
    (ctx / "_").mkdir()
    (ctx / "_" / "rules.md").write_text(f"- A rule. [P1, {quirk.ref}]\n- An example: `[{quirk.ref}]`\n",
                                        encoding="utf-8")
    (ctx / "Diary (private)").mkdir()
    (ctx / "Diary (private)" / "d.md").write_text(f"secret [{quirk.ref}]\n", encoding="utf-8")
    async with sessionmaker() as session:
        # The keyword index the note search starts from (the index pass fills it in production).
        for rel, content in (("_/rules.md", f"A rule. [P1, {quirk.ref}]"),
                             ("Diary (private)/d.md", f"secret [{quirk.ref}]")):
            await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, tags, frontmatter, content_hash) "
                "VALUES (NULL, :p, 'n', '{}', '{}', 'h')"), {"p": rel})
        for rel, content in (("_/rules.md", f"A rule. [P1, {quirk.ref}]"),
                             ("Diary (private)/d.md", f"secret [{quirk.ref}]"),
                             ("plans.md", f"Revisit the cap [{quirk.ref}]")):
            await session.execute(text(
                "UPDATE notes_metadata SET content_tsvector = to_tsvector('simple', :c) WHERE file_path = :p"),
                {"p": rel, "c": content})
        await session.commit()

    seen_only = await tools.ledger_update_impl(ref=quirk.ref, expected_revision=1, reason="topic",
                                               area="agent-personnel")
    assert seen_only.error is None and seen_only.cited_by is None, seen_only

    changed = await tools.ledger_update_impl(ref=quirk.ref, expected_revision=2, reason="tested",
                                             append="Tested: longer calls work.")
    assert changed.error is None, changed.error
    by = changed.cited_by
    assert [e["ref"] for e in by["entries"]] == [citing.ref]
    assert [t["ref"] for t in by["tasks"]] == [f"plans.md:{task.line}"]
    assert by["notes"] == [{"ref": "Diary (private)/d.md:1"},
                           {"ref": "_/rules.md:1", "text": f"- A rule. [P1, {quirk.ref}]"}]
    assert changed.notes[0].startswith(
        f"These cite {quirk.ref} (cited_by): check each still holds after your change. A note listed without "
        "its text is in a private folder: don't open it")


async def test_an_area_search_finds_entries_through_the_areas_topics(ctx):
    await tools.ledger_topic_edit_impl("create", "voice-bridge", "needed", kind="system", description="Bridge",
                                       area="home")
    own = await add_quirk("Filed under home", area="home")
    other = await add_quirk("Filed elsewhere", area="agent-personnel", topics=["voice-bridge"])
    await add_quirk("Not related", area="agent-personnel")
    listed = await tools.ledger_search_impl(area="home")
    assert {e.ref: e.via_topic for e in listed.entries} == {own.ref: None, other.ref: "voice-bridge"}
    by_words = await tools.ledger_search_impl(query="filed", area="home", mode="words")
    assert {e.ref: e.via_topic for e in by_words.by_words} == {own.ref: None, other.ref: "voice-bridge"}
