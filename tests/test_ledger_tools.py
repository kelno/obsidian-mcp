"""The agent ledger, offline: what is decided before any database statement.

Refs, the per-kind fields, the refusals a call gets before it opens a
session (permission, limits, malformed arguments), the background queue's
bound, and the tool registrations (first lines, read-only markers). Every
database behaviour — the rows, the history, the constraints, the triggers,
search — is in tests/integration/test_ledger_pg.py.
"""

import asyncio
import json

import pytest

from src.auth.session import current_user_id
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.models import db
from src.services import ledger, ledger_embed, refusals
from src.services import vault as vault_service


def code(result):
    assert result.error, result
    return json.loads(result.error.rsplit("\n", 1)[-1].removeprefix("MCP-REFUSAL "))["code"]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    def no_database():
        raise AssertionError("this refusal must come before any database session")

    monkeypatch.setattr(tools, "_log_usage", no_log)
    monkeypatch.setattr(tools, "async_session", no_database)
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


# ── Pure helpers ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, parsed", [
    ("Q14", ("quirk", 14)), ("q014", ("quirk", 14)), (" F3 ", ("feedback", 3)), ("P1", ("proposal", 1)),
    ("Q01", ("quirk", 1)),
])
def test_refs_parse_with_or_without_leading_zeros(raw, parsed):
    assert ledger.parse_ref(raw) == parsed


@pytest.mark.parametrize("raw", ["", "Q", "X1", "Q-1", "Q1a", "14", None, "Q1234567"])
def test_malformed_refs_are_refused(raw):
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.parse_ref(raw)
    assert exc.value.code == "invalid_argument"


def test_new_refs_have_no_padding():
    assert ledger.new_ref("quirk", 84) == "Q84" and ledger.new_ref("feedback", 1) == "F1"


def test_details_are_the_kinds_own_fields_only():
    assert ledger.validate_details("quirk", {"certainty": "tested", "sources": " a "}, for_add=True) == {
        "certainty": "tested", "sources": "a"}
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.validate_details("quirk", {"certainty": "tested", "subtype": "friction"}, for_add=True)
    assert {a["name"] for a in exc.value.allowed} == {"certainty", "certainty_note", "sources"}
    with pytest.raises(ledger.LedgerError, match="needs details.subtype"):
        ledger.validate_details("feedback", {}, for_add=True)
    with pytest.raises(ledger.LedgerError, match="must be one of"):
        ledger.validate_details("quirk", {"certainty": "sure"}, for_add=True)
    with pytest.raises(ledger.LedgerError, match="cannot be cleared"):
        ledger.validate_details("proposal", {"target": None}, for_add=False)
    assert ledger.validate_details("proposal", {"changes": None}, for_add=False) == {"changes": None}


def test_every_kind_field_is_a_model_column_and_the_statuses_agree():
    columns = set(db.LedgerEntry.__table__.columns.keys())
    for fields in ledger.KIND_FIELDS.values():
        assert set(fields) <= columns
    every = set(db.LEDGER_CLOSING_STATUSES).union(*db.LEDGER_KIND_STATUSES.values())
    assert set(ledger.ACTIVE_STATUSES) | set(ledger.CLOSED_STATUSES) == every
    assert not set(ledger.ACTIVE_STATUSES) & set(ledger.CLOSED_STATUSES)


def test_titles_and_reasons():
    assert ledger.clean_title("  a  ") == "a"
    for bad in ("", "a\nb", "x" * 151):
        with pytest.raises(ledger.LedgerError):
            ledger.clean_title(bad)
    with pytest.raises(ledger.LedgerError, match="reason is required"):
        ledger.clean_reason("  ")


def test_the_embedded_text_covers_the_kinds_fields():
    entry = db.LedgerEntry(title="T", body="B", kind="feedback", component="C", fix="F")
    assert ledger.embed_text(entry) == "T\n\nB\n\nC\n\nF"


def test_stale_revision_is_a_declared_body_code():
    assert "stale_revision" in refusals.BODY_CODES


def test_the_model_declares_the_constraints_the_migration_creates():
    names = {c.name for c in db.LedgerEntry.__table__.constraints}
    for name in ("ck_ledger_entries_seen", "ck_ledger_entries_quirk_fields", "ck_ledger_entries_proposal_fields",
                 "fk_ledger_entries_superseded_by", "uq_ledger_entries_owner_kind_number"):
        assert name in names


# ── Refusals before any database statement ───────────────────────────────────

async def test_search_refuses_a_bad_limit_or_mode(ctx):
    assert code(await tools.ledger_search_impl(limit=0)) == "invalid_argument"
    bad = await tools.ledger_search_impl(query="x", mode="merged")
    assert code(bad) == "invalid_argument" and {a["name"] for a in bad.allowed} == {"both", "words", "meaning"}


async def test_get_refuses_no_refs_or_a_malformed_one(ctx):
    assert code(await tools.ledger_get_impl([])) == "invalid_argument"
    assert code(await tools.ledger_get_impl(["Q1"] * 21)) == "invalid_argument"


async def test_writes_refuse_a_read_only_credential(ctx):
    token = current_permission.set("read")
    try:
        assert code(await tools.ledger_add_impl("quirk", "t", details={"certainty": "tested"})) == "permission_denied"
        assert code(await tools.ledger_update_impl(ref="Q1", expected_revision=1, reason="r",
                                                   title="t")) == "permission_denied"
        assert code(await tools.ledger_digest_edit_impl("v", "r", [{"op": "create"}])) == "permission_denied"
        assert code(await tools.ledger_topic_edit_impl("create", "t", "r")) == "permission_denied"
    finally:
        current_permission.reset(token)


# ── The background queue ─────────────────────────────────────────────────────

async def test_the_embed_queue_is_bounded_and_deduplicated(monkeypatch):
    monkeypatch.setattr(ledger_embed.settings, "ledger_embed_queue_max", 2)
    started = asyncio.Event()

    async def slow(entry_id):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(ledger_embed, "embed_entry", slow)
    monkeypatch.setattr(ledger_embed, "_paused", lambda: False)
    monkeypatch.setattr(ledger_embed, "_worker", None)
    ledger_embed._queue.clear()
    ledger_embed._queued.clear()
    try:
        assert ledger_embed.enqueue(1)
        await started.wait()
        assert ledger_embed.enqueue(2) and ledger_embed.enqueue(2) and ledger_embed.enqueue(3)
        assert not ledger_embed.enqueue(4)
        assert list(ledger_embed._queue) == [2, 3]
    finally:
        ledger_embed._worker.cancel()
        ledger_embed._queue.clear()
        ledger_embed._queued.clear()
        ledger_embed._in_flight.clear()


# ── Registration ─────────────────────────────────────────────────────────────

LEDGER_TOOLS = ("ledger_search", "ledger_get", "ledger_add", "ledger_update", "ledger_digest",
                "ledger_digest_edit", "ledger_topics", "ledger_topic_edit")
READ_ONLY = {"ledger_search", "ledger_get", "ledger_digest", "ledger_topics"}


def _registered():
    from src.mcp_server.server import mcp

    return {t.name: t for t in mcp._tool_manager.list_tools() if t.name.startswith("ledger_")}


def test_eight_tools_each_first_line_stands_alone_within_80_characters():
    registered = _registered()
    assert set(registered) == set(LEDGER_TOOLS)
    for name, tool in registered.items():
        first = tool.description.strip().splitlines()[0]
        assert len(first) <= 80 and first.endswith("."), (name, first)
        assert "agent-ledger" in first, name


def test_read_tools_are_marked_read_only_and_writes_not_destructive():
    for name, tool in _registered().items():
        if name in READ_ONLY:
            assert tool.annotations.readOnlyHint is True, name
        else:
            assert tool.annotations.readOnlyHint is False and tool.annotations.destructiveHint is False, name


def test_lock_conflicts_become_a_retryable_refusal_and_other_faults_are_raised():
    from sqlalchemy.exc import DBAPIError

    from src.mcp_server.ledger_result import LedgerWriteResult

    class Orig(Exception):
        def __init__(self, state):
            self.sqlstate = state

    deadlock = DBAPIError("UPDATE", {}, Orig("40P01"))
    assert code(tools._ledger_conflict(LedgerWriteResult, deadlock)) == "concurrent_write"
    other = DBAPIError("UPDATE", {}, Orig("XX000"))
    with pytest.raises(DBAPIError):
        tools._ledger_conflict(LedgerWriteResult, other)
