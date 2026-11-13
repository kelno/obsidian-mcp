"""`index_status`'s wording: the summary and the notes follow the state they are given.

The counts themselves need a database (`integration/test_index_status_pg.py`);
what an agent reads aloud from them is checked here.
"""
from src.mcp_server import tools
from src.mcp_server.index_status_result import Health, LedgerCounts, NoteCounts, Progress
from src.services import index_status


def counts(**kw) -> NoteCounts:
    base = dict(notes=100, embedded=100, pending=0, excluded=0, no_text=0, beginning_only=0)
    base.update(kw)
    return NoteCounts(**base)


def progress(**kw) -> Progress:
    base = dict(embedded_last_hour=0, estimated_minutes_left=0, embedding_now=False, paused=False,
                embed_interval_seconds=300)
    base.update(kw)
    return Progress(**base)


def health(**kw) -> Health:
    base = dict(status="ok", task_running=True, embedding_failing=False, embedding_stalled=False,
                index_stalled=False, quarantined_notes=0)
    base.update(kw)
    return Health(**base)


NO_LEDGER = LedgerCounts(entries=10, pending=0)


def test_minutes_left():
    assert index_status.minutes_left(0, 0) == 0
    assert index_status.minutes_left(5, 0) is None
    assert index_status.minutes_left(1200, 350) == 206
    assert index_status.minutes_left(1, 10_000) == 1  # never "0 min" while something is pending


def test_embedding_excluded_matches_like_the_embed_stage():
    assert index_status.embedding_excluded("_/agent/x.md", ["_/*"])
    assert not index_status.embedding_excluded("Areas/x.md", ["_/*"])
    assert not index_status.embedding_excluded("Areas/x.md", [])


def test_summary_and_notes_follow_the_state():
    done = tools._index_status_summary(counts(), NO_LEDGER, progress())
    assert done == "100 notes indexed: 100 embedded, 0 pending. Every note that can be embedded is up to date."
    assert tools._index_status_notes(counts(), progress(), health()) == []

    backlog = counts(embedded=60, pending=30, excluded=8, no_text=2)
    moving = progress(embedded_last_hour=350, estimated_minutes_left=206)
    text = tools._index_status_summary(backlog, LedgerCounts(entries=10, pending=4), moving)
    assert text == ("100 notes indexed: 60 embedded, 30 pending, 10 never embedded (excluded or empty). "
                    "350 embedded in the last hour; about 3 h 26 min to go at that rate. "
                    "Agent ledger: 4 of 10 entries pending.")
    notes = tools._index_status_notes(backlog, moving, health())
    assert len(notes) == 1 and "keyword_search" in notes[0]

    stuck = progress(estimated_minutes_left=None)
    assert "Nothing was embedded in the last hour." in tools._index_status_summary(backlog, NO_LEDGER, stuck)
    assert any("embedding server may be down" in n for n in tools._index_status_notes(backlog, stuck, health()))
    failing = tools._index_status_notes(backlog, stuck, health(embedding_failing=True))
    assert any("failed repeatedly" in n for n in failing)
    assert not any("may be down or very slow" in n for n in failing)  # one cause, said once

    paused = progress(estimated_minutes_left=None, paused=True)
    assert "Indexing is paused." in tools._index_status_summary(backlog, NO_LEDGER, paused)
    paused_notes = tools._index_status_notes(backlog, paused, health())
    assert "paused" in paused_notes[0] and not any("none was embedded" in n for n in paused_notes)

    # Owner's report, 2026-11-13: pending notes no stage embeds are said
    # plainly, in the summary too, without the "call again" hedge.
    stuck_health = health(embedding_stuck=True, status="degraded")
    stuck_notes = tools._index_status_notes(backlog, stuck, stuck_health, 25)
    assert any(n.startswith("Embedding is failing: notes have been pending for 25 min") and "Tell the owner" in n
               for n in stuck_notes)
    assert not any("Call again" in n for n in stuck_notes)
    assert tools._index_status_summary(backlog, NO_LEDGER, stuck, stuck_health).endswith(
        " Embedding is failing: tell the owner.")
    assert "failing" not in tools._index_status_summary(backlog, NO_LEDGER, stuck, health())
    assert any(n.startswith("Embedding is failing") for n in failing)

    dead = tools._index_status_notes(counts(), progress(), health(task_running=False, status="degraded"))
    assert any("not running" in n for n in dead)
    assert tools._index_status_notes(counts(), progress(), health(task_running=False, status="disabled")) == []
