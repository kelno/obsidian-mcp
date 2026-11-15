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
    columns = set(db.LedgerEntry.__mapper__.column_attrs.keys())  # attribute names (`leads` maps to column `fix`)
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
    entry = db.LedgerEntry(title="T", body="B", kind="feedback", component="C", leads="F")
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


# ── Everything an agent needs, up front (ledger F34) ─────────────────────────

def test_details_name_every_problem_in_one_refusal():
    """An unknown field, a missing required one and a wrong value come back
    together, each with its field and, where there is a list, the values."""
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.validate_details("quirk", {"subtype": "friction", "sources": ""}, for_add=True)
    problems = {p["field"]: p for p in exc.value.problems}
    assert set(problems) == {"details", "details.sources", "details.certainty"}
    assert {a["name"] for a in problems["details.certainty"]["allowed"]} == set(db.LEDGER_CERTAINTIES)
    assert "3 problems" in str(exc.value) and "Nothing was written" not in str(exc.value).split(". ", 1)[1]


def test_a_missing_required_field_names_its_values():
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.validate_details("feedback", {}, for_add=True)
    assert exc.value.problems is None  # one problem: refused exactly as before
    assert "unclear_instruction, friction, suggestion" in str(exc.value)
    assert {a["name"] for a in exc.value.allowed} == set(db.LEDGER_FEEDBACK_SUBTYPES)


def test_problems_pass_one_through_and_combine_several():
    one = ledger.Problems()
    with one.check("title"):
        raise ledger.invalid("bad title.", allowed=[{"name": "x"}])
    with pytest.raises(ledger.LedgerError) as exc:
        one.raise_if_any()
    assert str(exc.value) == "bad title." and exc.value.problems is None and exc.value.allowed == [{"name": "x"}]

    many = ledger.Problems()
    with many.check("title"):
        raise ledger.invalid("bad title. Nothing was written.")
    with many.check("related"):
        raise ledger.LedgerError("not_found", "no F9.")
    with pytest.raises(ledger.LedgerError) as exc:
        many.raise_if_any()
    assert exc.value.code == "invalid_argument"
    assert [p["field"] for p in exc.value.problems] == ["title", "related"]
    assert exc.value.problems[0]["message"] == "bad title."
    assert str(exc.value).startswith("2 problems; nothing was written. (1) title: bad title. (2) related: no F9.")
    ledger.Problems().raise_if_any()  # nothing collected: no refusal


def test_a_refusal_carries_its_problems_into_the_result():
    exc = ledger.LedgerError("invalid_argument", "2 problems", problems=[{"field": "a", "code": "c", "message": "m"}])
    result = tools._ledger_refused(tools.LedgerWriteResult, exc)
    assert result.problems == [{"field": "a", "code": "c", "message": "m"}]


def test_add_and_update_publish_each_rule_in_the_parameter_list():
    """Clients show the parameter list more reliably than the docstring:
    every parameter is described, and the values are enums."""
    registered = _registered()
    for param, schema in registered["ledger_search"].parameters["properties"].items():
        assert schema.get("description"), ("ledger_search", param)
    for name in ("ledger_add", "ledger_update"):
        props = registered[name].parameters["properties"]
        for param, schema in props.items():
            assert schema.get("description"), (name, param)
        details = props["details"]["properties"]
        assert set(details) == {f for fields in ledger.KIND_FIELDS.values() for f in fields}
        assert [v for v in details["certainty"]["enum"] if v] == list(db.LEDGER_CERTAINTIES)
        assert [v for v in details["subtype"]["enum"] if v] == list(db.LEDGER_FEEDBACK_SUBTYPES)
        assert "required" in details["certainty"]["description"]
        assert props["title"]["maxLength"] == ledger.TITLE_MAX
    add = registered["ledger_add"].parameters["properties"]
    assert add["kind"]["enum"] == list(db.LEDGER_KINDS)
    assert registered["ledger_add"].parameters["required"] == ["kind", "title"]
    assert registered["ledger_update"].parameters["properties"]["details"]["properties"]["certainty"]["type"] == [
        "string", "null"]


async def test_the_guide_still_answers_without_the_database(ctx, monkeypatch):
    assert await tools._ledger_guide(None) == ""



async def test_a_kind_field_beside_details_is_refused_with_where_it_goes(ctx):
    """The agent test's case: `subtype` passed at the top level got pydantic's
    bare unknown-argument error."""
    from mcp.server.fastmcp.exceptions import ToolError

    from src.mcp_server.server import mcp

    with pytest.raises(ToolError, match="subtype goes inside details"):
        await mcp.call_tool("ledger_add", {"kind": "feedback", "title": "t", "subtype": "friction"})
    with pytest.raises(ToolError, match="certainty, sources go inside details"):
        await mcp.call_tool("ledger_update", {"ref": "Q1", "certainty": "tested", "sources": "x"})
    with pytest.raises(ToolError, match="Extra inputs"):
        await mcp.call_tool("ledger_add", {"kind": "quirk", "title": "t", "colour": "red"})


# ── Rule cases ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, parsed", [("C3", ("case", 3)), ("c003", ("case", 3))])
def test_case_refs_parse(raw, parsed):
    assert ledger.parse_ref(raw) == parsed
    assert ledger.new_ref("case", 3) == "C3"


def test_a_cases_details():
    assert ledger.validate_details("case", {
        "outcome": "slip", "expected": " Answer the literal question first. ", "expected_by": "owner",
        "owner_words": "c'est une question, pas une demande", "sources": "Conversation feel examples #4",
    }, for_add=True) == {
        "outcome": "slip", "expected": "Answer the literal question first.", "expected_by": "owner",
        "owner_words": "c'est une question, pas une demande", "sources": "Conversation feel examples #4"}
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.validate_details("case", {"outcome": "failed", "certainty": "tested"}, for_add=True)
    fields = {p["field"] for p in exc.value.problems}
    assert {"details", "details.outcome", "details.expected", "details.expected_by"} <= fields
    with pytest.raises(ledger.LedgerError, match="cannot be cleared"):
        ledger.validate_details("case", {"expected": None}, for_add=False)
    with pytest.raises(ledger.LedgerError, match="at most 4000"):
        ledger.validate_details("case", {"agent_did": "x" * 4001}, for_add=False)


def test_a_case_is_searched_and_embedded_by_its_own_words():
    entry = db.LedgerEntry(title="T", body="B", kind="case", outcome="slip", expected="E", expected_by="owner",
                           owner_words="O", agent_did="A")
    assert ledger.embed_text(entry) == "T\n\nB\n\nE\n\nO\n\nA"
    assert {"expected", "owner_words", "agent_did"} <= set(ledger.TEXT_FIELDS)


def test_retired_is_closed_and_cases_start_current():
    assert "retired" in ledger.CLOSED_STATUSES and "retired" in db.LEDGER_KIND_STATUSES["case"]
    assert ledger.ADD_STATUSES["case"] == ("current",)


def test_the_case_fields_are_published_with_their_values():
    registered = _registered()
    for name in ("ledger_add", "ledger_update"):
        props = registered[name].parameters["properties"]
        details = props["details"]["properties"]
        assert [v for v in details["outcome"]["enum"] if v] == list(db.LEDGER_CASE_OUTCOMES)
        assert [v for v in details["expected_by"]["enum"] if v] == list(db.LEDGER_CASE_EXPECTED_BY)
        assert details["owner_words"]["maxLength"] == 4000
        assert "verbatim" in details["owner_words"]["description"]
        assert details["sources"]["description"].startswith("quirk or case only")
    assert "rules" in registered["ledger_add"].parameters["properties"]
    assert {"rules_add", "rules_remove"} <= set(registered["ledger_update"].parameters["properties"])
    assert "rule" in registered["ledger_search"].parameters["properties"]
    assert "case" in registered["ledger_add"].parameters["properties"]["kind"]["enum"]


def _cases(before: dict, since: dict) -> dict:
    """A `current_cases` item from the two parts, with the compatibility totals."""
    outcomes: dict = {}
    for part in (before, since):
        for outcome, n in part["outcomes"].items():
            outcomes[outcome] = outcomes.get(outcome, 0) + n
    refs = sorted(before["refs"] + since["refs"], key=lambda r: int(r[1:]))[:10]
    return {"total": before["total"] + since["total"], "outcomes": outcomes, "refs": refs,
            "before": before, "since": since}


_NONE = {"total": 0, "outcomes": {}, "refs": []}


def test_a_proposals_cases_follow_it_in_a_note_writers_result_split_before_and_since():
    line = tools._ledger_state_line({
        "ref": "P41", "kind": "proposal", "status": "adjusted", "title": "Answer first", "updated": "2026-11-09",
        "cases": _cases({"total": 3, "outcomes": {"good": 2, "slip": 1}, "refs": ["C3", "C7", "C12"]},
                        {"total": 2, "outcomes": {"slip": 2}, "refs": ["C40", "C41"]})})
    assert line == ('- P41 (proposal, adjusted, updated 2026-11-09): "Answer first" — current cases: 5; '
                    "before it: 3 (C3, C7, C12); since: 2 slip (C40, C41)")
    assert tools._ledger_state_line({"ref": "C3", "kind": "case", "status": "current", "outcome": "slip",
                                     "title": "t"}) == '- C3 (case, current, slip): "t"'


def test_a_proposals_case_line_says_none_for_an_empty_part_and_caps_each_part():
    since = {"total": 12, "outcomes": {"slip": 2, "good": 10}, "refs": [f"C{i}" for i in range(1, 11)]}
    line = tools._ledger_state_line({"ref": "P41", "kind": "proposal", "status": "accepted", "title": "t",
                                     "cases": _cases(dict(_NONE), since)})
    assert line.endswith("— current cases: 12; before it: none; since: 2 slip, 10 good "
                         "(C1, C2, C3, C4, C5, C6, C7, C8, C9, C10, +2)")
    before = {"total": 11, "outcomes": {"good": 11}, "refs": [f"C{i}" for i in range(1, 11)]}
    line = tools._ledger_state_line({"ref": "P41", "kind": "proposal", "status": "accepted", "title": "t",
                                     "cases": _cases(before, dict(_NONE))})
    assert line.endswith("— current cases: 11; before it: 11 (C1, C2, C3, C4, C5, C6, C7, C8, C9, C10, +1); "
                         "since: none")


def test_case_refs_in_a_note_are_capped():
    refs = [f"C{i}" for i in range(1, 14)]
    assert ledger._capped_refs(refs) == ", ".join(f"C{i}" for i in range(1, 11)) + ", +3"
    assert ledger._capped_refs(["C01", "C2"]) == "C1, C2"
