"""The agent ledger tools' structured results.

Structured, like the task tools', because an agent chains these calls: a
search hands back each entry's `ref` and `revision`, which an update must be
given back, and a refusal names the valid values (`allowed`) or the entry's
current revision so the next call can be right. Every outcome rides on
`_body_outcome`; an error result carries `error` and those fields only
(`allowed`, `current_revision`, `problems`).
"""

from __future__ import annotations

from pydantic import PrivateAttr

from src.mcp_server.read_result import _OmitNone
from src.services.tool_outcomes import BodyOutcome


class _LedgerResult(_OmitNone):
    _body_outcome: BodyOutcome | None = PrivateAttr(default=None)

    error: str | None = None
    #: On a refusal for an unknown value: the valid ones, with descriptions.
    allowed: list[dict] | None = None
    #: On `stale_revision`: the revision the entry (or line, or topic) is at now.
    current_revision: int | None = None
    #: On a refusal with more than one problem: each one, as `{field, code,
    #: message, allowed?}`, so the next call can fix them all at once.
    problems: list[dict] | None = None
    #: Plain-language remarks the caller should read (a status that moved, a hint).
    notes: list[str] | None = None


class EntrySummary(_OmitNone):
    """One line of a list: never the body."""

    ref: str
    kind: str
    status: str
    title: str
    revision: int | None = None
    area: str | None = None
    date: str | None = None
    origin: str | None = None
    seen: bool | None = None
    #: In a "by meaning" list: cosine similarity of the best chunk.
    similarity: float | None = None
    #: True when the other list of the same search also holds this entry.
    in_both_lists: bool | None = None
    #: Feedback cited by vault tasks (`ledger F12`): open (one is not done or
    #: cancelled), done (all closed, one done) or cancelled (all cancelled).
    tasks: str | None = None


class LedgerSearchResult(_LedgerResult):
    #: Without a query: the filtered list, newest change first.
    entries: list[EntrySummary] | None = None
    #: With a query: entries sharing words with it (a ref counts), best first.
    by_words: list[EntrySummary] | None = None
    #: With a query: entries closest in meaning, best first.
    by_meaning: list[EntrySummary] | None = None
    #: Why there is no meaning list this time (provider down or slow).
    meaning_unavailable: str | None = None
    #: Filtered entries with no current vector yet: found by words only.
    not_embedded: int | None = None
    truncated: bool | None = None
    filters: dict | None = None
    #: The area and topic names an entry can carry, for the entry you may be
    #: about to log (descriptions: `ledger_topics`).
    areas_and_topics: dict | None = None


class LedgerEntryFull(_OmitNone):
    ref: str
    kind: str
    status: str
    revision: int
    title: str
    body: str
    origin: str
    observed_on: str
    created_at: str
    updated_at: str
    created_by: str
    area: str | None = None
    topics: list[str] | None = None
    related: list[str] | None = None
    details: dict | None = None
    seen: bool | None = None
    superseded_by: str | None = None
    handled_account: str | None = None
    handled_at: str | None = None
    author: str | None = None
    #: Summary-view lines that cite this entry.
    cited_by: list[dict] | None = None
    #: Feedback: the vault tasks citing it (`ledger F12`), each with its
    #: `ref` (path:line), `status` type and `text`, and their overall state.
    tasks: list[dict] | None = None
    tasks_state: str | None = None
    #: Every past version, oldest first, with who, when and why.
    history: list[dict] | None = None
    #: Whether its vector is current (else it is found by words only).
    embedded: bool | None = None


class LedgerGetResult(_LedgerResult):
    entries: list[LedgerEntryFull] | None = None
    not_found: list[str] | None = None
    #: Refs left out to keep the answer within the response cap; read them next.
    not_returned: list[str] | None = None


class FlaggedLine(_OmitNone):
    line: int
    view: str
    text: str
    reason: str


class LedgerWriteResult(_LedgerResult):
    ref: str | None = None
    revision: int | None = None
    status: str | None = None
    changed: list[str] | None = None
    #: `ledger_add`: active entries sharing words with the new one (fold a duplicate in now).
    similar: list[dict] | None = None
    #: Summary-view lines this change flagged for review: check each one now.
    flagged_lines: list[FlaggedLine] | None = None
    #: Topics where a summary view is due.
    view_hints: list[dict] | None = None
    #: Bulk seen: what was marked, already seen, not feedback, not found.
    seen: dict | None = None
    #: `ledger_add` with no area or no topics: the names it could have had.
    areas_and_topics: dict | None = None


class LedgerDigestResult(_LedgerResult):
    #: Without slug: every view, with what it is for.
    views: list[dict] | None = None
    slug: str | None = None
    title: str | None = None
    layout: str | None = None
    #: The view as Markdown (a table for a matrix), each line with its sources.
    markdown: str | None = None
    lines_under_review: int | None = None
    #: Lines left out to keep the answer within the response cap.
    lines_not_shown: int | None = None
    columns: list[str] | None = None
    #: With `with_ids`: each line's id and revision, to edit it.
    lines: list[dict] | None = None


class LedgerDigestEditResult(_LedgerResult):
    slug: str | None = None
    created: bool | None = None
    applied: list[dict] | None = None


class LedgerTopicsResult(_LedgerResult):
    areas: list[dict] | None = None
    topics: list[dict] | None = None
    entries_without_area: dict | None = None
    areas_no_longer_declared: list[str] | None = None
    view_hints: list[dict] | None = None


class LedgerTopicEditResult(_LedgerResult):
    topic: dict | None = None
    #: `merge`: the entries that moved to the target topic.
    moved: list[str] | None = None
