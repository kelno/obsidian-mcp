"""The agent ledger's entries: refs, validation, add, update, get, search.

The ledger is **primary data** (see docs/architecture/agent-ledger.md): what
agents write for each other, which nothing in the vault can rebuild. So every
write here is one transaction that either lands whole or not at all, every
change keeps the previous version in `ledger_revisions` with a reason, and an
update names the revision it was based on and is refused if the entry moved
since. The database enforces the structure (per-kind fields, statuses, owner
checks on every link, no deletes, append-only history); this module enforces
what depends on the request: known areas and topics, the expected revision,
reasons, and the response bounds.

Summary views and topics live in `ledger_views.py`, vectors and the meaning
search in `ledger_embed.py`. The tool layer (`src/mcp_server/tools.py`, end)
only maps arguments and results.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from functools import reduce
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, delete, func, insert, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.session import current_actor
from src.config import settings
from src.models import db
from src.models.db import (
    LedgerDigest,
    LedgerDigestLine,
    LedgerDigestSource,
    LedgerEntry,
    LedgerEntryLink,
    LedgerEntryTopic,
    LedgerRefCounter,
    LedgerRevision,
    LedgerTopic,
    NoteMetadata,
)
from src.services import task_ops, task_query

# ── Limits ───────────────────────────────────────────────────────────────────

TITLE_MAX = 150
BODY_MAX = 16000
REASON_MAX = 2000
AUTHOR_MAX = 100
#: `ledger_get` reads at most this many refs per call.
GET_MAX_REFS = 20
#: `ledger_update(refs=…, seen=true)` marks at most this many per call.
SEEN_MAX_REFS = 100
SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 100
#: Similar entries `ledger_add` returns, so a duplicate is caught at once.
SIMILAR_MAX = 5
#: A list or a related/topic argument holds at most this many items.
LIST_ARG_MAX = 20

#: Per-kind fields, the ones a kind must have, and their caps (characters).
KIND_FIELDS: dict[str, dict[str, int]] = {
    "quirk": {"certainty": 16, "certainty_note": 2000, "sources": 4000},
    "feedback": {"subtype": 32, "component": 300, "fix": 4000},
    "proposal": {
        "target": 500, "proposed_text": 16000, "final_text": 16000, "changes": 4000, "owner_reason": 4000,
    },
}
REQUIRED_FIELDS = {"quirk": ("certainty",), "feedback": ("subtype",), "proposal": ("target", "proposed_text")}
ENUM_FIELDS = {"certainty": db.LEDGER_CERTAINTIES, "subtype": db.LEDGER_FEEDBACK_SUBTYPES}

#: Statuses a search with `status="active"` (the default) returns. A proposal's
#: outcome is its record, so every outcome stays active; what leaves the
#: default searches is a closed lifecycle: stale, handled, dismissed,
#: superseded, retracted.
ACTIVE_STATUSES = ("current", "to_triage", "open", "accepted", "adjusted", "rejected", "withdrawn")
CLOSED_STATUSES = ("stale", "handled", "dismissed", "superseded", "retracted")
ALL_STATUSES = ACTIVE_STATUSES + CLOSED_STATUSES
#: Statuses `ledger_add` may set; the closing ones need an update with a reason.
ADD_STATUSES = {
    "quirk": ("current", "stale"),
    "feedback": ("open", "to_triage"),
    "proposal": ("accepted", "adjusted", "rejected", "withdrawn"),
}

_REF = re.compile(r"\s*([QqFfPp])0*([0-9]{1,6})\s*\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_LETTER_KIND = {letter: kind for kind, letter in db.LEDGER_KIND_LETTERS.items()}


class LedgerError(Exception):
    """A refusal: nothing was written. `code` is a declared refusal code.

    `allowed` lists the valid values (with descriptions) when the caller named
    an unknown one, so the next call can be right; `current_revision` is set
    for `stale_revision`.
    """

    def __init__(self, code: str, message: str, *, allowed: list | None = None,
                 current_revision: int | None = None):
        super().__init__(message)
        self.code = code
        self.allowed = allowed
        self.current_revision = current_revision


def invalid(message: str, **kw) -> LedgerError:
    return LedgerError("invalid_argument", message, **kw)


# ── Small pure helpers ───────────────────────────────────────────────────────

def owner_key(uid: int | None) -> int:
    """The `owner_key` value for a caller: user ids start at 1, single-user mode is 0."""
    return 0 if uid is None else int(uid)


def parse_ref(raw: str) -> tuple[str, int]:
    """`(kind, number)` for `Q14`, `q014`, `F3`; `LedgerError` otherwise."""
    match = _REF.match(raw or "") if isinstance(raw, str) else None
    if match is None:
        raise invalid(f"{raw!r} is not a ledger ref: a letter Q, F or P and a number, such as Q14 or F3.")
    return _LETTER_KIND[match.group(1).upper()], int(match.group(2))


def new_ref(kind: str, number: int) -> str:
    return f"{db.LEDGER_KIND_LETTERS[kind]}{number}"


def today() -> date:
    """The owner's local date (`TASKS_TIMEZONE`), as the task tools use it."""
    return task_ops.owner_today(settings.tasks_timezone)


def parse_day(raw: str, name: str) -> date:
    value = (raw or "").strip()
    if value == "today":
        return today()
    if not _DATE.match(value):
        raise invalid(f"{name} must be YYYY-MM-DD or 'today' (got {raw!r}).")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise invalid(f"{name} {raw!r} is not a real calendar date.") from None


def day_start_utc(day: date) -> datetime:
    """Midnight of `day` in the owner's zone, as a UTC instant."""
    zone = ZoneInfo(settings.tasks_timezone)
    return datetime.combine(day, time(0), zone).astimezone(timezone.utc)


def clean_title(raw: str) -> str:
    title = (raw or "").strip()
    if not title:
        raise invalid("title is required: one line saying what the entry is about.")
    if "\n" in title or "\r" in title:
        raise invalid("title must be one line; put the detail in body.")
    if len(title) > TITLE_MAX:
        raise invalid(f"title is {len(title)} characters; at most {TITLE_MAX}. Put the detail in body.")
    return title


def check_body(body: str) -> str:
    if len(body) > BODY_MAX:
        raise LedgerError("size_limit", f"body is {len(body)} characters; at most {BODY_MAX}. Nothing was written.")
    return body


def clean_reason(raw: str | None) -> str:
    reason = (raw or "").strip()
    if not reason:
        raise invalid("reason is required: say why this change is made. It is kept in the history.")
    if len(reason) > REASON_MAX:
        raise invalid(f"reason is {len(reason)} characters; at most {REASON_MAX}.")
    return reason


def clean_author(raw: str | None) -> str | None:
    if raw is None:
        return None
    author = raw.strip()
    if not author:
        return None
    if len(author) > AUTHOR_MAX or "\n" in author:
        raise invalid(f"author is your own name, one line of at most {AUTHOR_MAX} characters.")
    return author


def bounded_list(values, name: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise invalid(f"{name} must be a list of strings.")
    if len(values) > LIST_ARG_MAX:
        raise invalid(f"{name} holds {len(values)} items; at most {LIST_ARG_MAX}.")
    return values


def created_by() -> str:
    """The calling credential's label (`api_key:<name>`, `oauth:<client>`), set by the server."""
    actor = current_actor.get()
    if actor is None:
        return "server"
    kind, label, _ref = actor
    return f"{kind}:{label or '?'}"[:255]


def embed_text(entry) -> str:
    """The text an entry is searched and embedded by: its title, body and own fields."""
    parts = [entry.title, entry.body]
    for name in ("component", "fix", "certainty_note", "target", "proposed_text", "final_text", "changes"):
        value = getattr(entry, name, None)
        if value:
            parts.append(value)
    return "\n\n".join(p for p in parts if p)


def content_hash(text_: str) -> str:
    return hashlib.sha256(text_.encode("utf-8")).hexdigest()


def _owner(model, uid: int | None):
    return model.owner_key == owner_key(uid)


# ── Areas and topics (validation) ────────────────────────────────────────────

async def area_index(session: AsyncSession, uid: int | None) -> task_query.AreaIndex:
    """Every `area_tag` the caller's indexed notes declare, with descriptions."""
    owner = NoteMetadata.user_id.is_(None) if uid is None else NoteMetadata.user_id == uid
    rows = (await session.execute(
        select(NoteMetadata.file_path, NoteMetadata.frontmatter).where(
            owner, NoteMetadata.frontmatter.has_key("area_tag")
        )
    )).all()
    return task_query.AreaIndex.from_rows([(r.file_path, r.frontmatter) for r in rows])


def areas_allowed(index: task_query.AreaIndex) -> list[dict]:
    return [{"name": tag, "description": desc, "main_note": path} for tag, path, desc in index.described()]


def resolve_area(index: task_query.AreaIndex, name: str) -> str:
    """`area/<slug>` for a slug, tag or main note name; refused with the list otherwise."""
    try:
        return index.lookup(name)
    except task_query.QueryError as exc:
        raise invalid(f"{exc}. Nothing was written.", allowed=areas_allowed(index)) from None


async def topics_allowed(session: AsyncSession, uid: int | None) -> list[dict]:
    rows = (await session.execute(
        select(LedgerTopic.slug, LedgerTopic.kind, LedgerTopic.description)
        .where(_owner(LedgerTopic, uid), LedgerTopic.status == "active")
        .order_by(LedgerTopic.slug)
    )).all()
    return [{"name": r.slug, "kind": r.kind, "description": r.description} for r in rows]


async def resolve_topics(session: AsyncSession, uid: int | None, slugs: list[str]) -> dict[str, int]:
    """`{slug: id}` for active topics; refuses unknown or merged ones with the list."""
    wanted = [s.strip().lower() for s in slugs]
    if not wanted:
        return {}
    rows = (await session.execute(
        select(LedgerTopic.id, LedgerTopic.slug, LedgerTopic.status, LedgerTopic.merged_into)
        .where(_owner(LedgerTopic, uid), LedgerTopic.slug.in_(wanted))
    )).all()
    found = {r.slug: r for r in rows}
    merged = [r for r in rows if r.status == "merged"]
    if merged:
        targets = dict((await session.execute(
            select(LedgerTopic.id, LedgerTopic.slug).where(LedgerTopic.id.in_([r.merged_into for r in merged]))
        )).all())
        names = ", ".join(f"{r.slug} (merged into {targets.get(r.merged_into, '?')})" for r in merged)
        raise invalid(f"topic {names} was merged; use the topic it was merged into. Nothing was written.",
                      allowed=await topics_allowed(session, uid))
    unknown = [s for s in wanted if s not in found]
    if unknown:
        raise invalid(
            f"unknown topic(s) {', '.join(unknown)}. Use one of the listed topics, or create one first "
            "with ledger_topic_edit. Nothing was written.",
            allowed=await topics_allowed(session, uid),
        )
    return {slug: found[slug].id for slug in wanted}


def validate_details(kind: str, details: dict | None, *, for_add: bool) -> dict:
    """The kind's own fields from `details`; unknown keys and bad values refused.

    On update a `None` value clears an optional field.
    """
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise invalid("details must be an object of the kind's own fields.")
    allowed = KIND_FIELDS[kind]
    unknown = sorted(set(details) - set(allowed))
    if unknown:
        raise invalid(
            f"{', '.join(unknown)} is not a field of a {kind}. A {kind}'s fields: {', '.join(allowed)}. "
            "Nothing was written.",
            allowed=[{"name": k} for k in allowed],
        )
    out = {}
    for name, value in details.items():
        if value is None:
            if name in REQUIRED_FIELDS[kind]:
                raise invalid(f"{name} is required for a {kind} and cannot be cleared.")
            out[name] = None
            continue
        if not isinstance(value, str):
            raise invalid(f"details.{name} must be text.")
        value = value.strip()
        if name in ENUM_FIELDS:
            if value not in ENUM_FIELDS[name]:
                raise invalid(f"details.{name} must be one of {', '.join(ENUM_FIELDS[name])} (got {value!r}).",
                              allowed=[{"name": v} for v in ENUM_FIELDS[name]])
        elif not value:
            raise invalid(f"details.{name} is empty; leave it out instead.")
        if len(value) > allowed[name]:
            raise invalid(f"details.{name} is {len(value)} characters; at most {allowed[name]}.")
        out[name] = value
    if for_add:
        missing = [n for n in REQUIRED_FIELDS[kind] if not out.get(n)]
        if missing:
            raise invalid(f"a {kind} needs details.{' and details.'.join(missing)}. Nothing was written.")
    return out


# ── Reading rows ─────────────────────────────────────────────────────────────

async def find_entries(session: AsyncSession, uid: int | None, refs: list[str], *, lock: bool = False):
    """`({ref_as_given: entry}, [not found])` for the caller's entries."""
    parsed = [(raw, *parse_ref(raw)) for raw in refs]
    if not parsed:
        return {}, []
    stmt = select(LedgerEntry).where(
        _owner(LedgerEntry, uid),
        or_(*[and_(LedgerEntry.kind == kind, LedgerEntry.number == number) for _r, kind, number in parsed]),
    )
    if lock:
        stmt = stmt.with_for_update()
    rows = (await session.execute(stmt)).scalars().all()
    by_key = {(e.kind, e.number): e for e in rows}
    found, missing = {}, []
    for raw, kind, number in parsed:
        entry = by_key.get((kind, number))
        if entry is None:
            missing.append(raw)
        else:
            found[raw] = entry
    return found, missing


async def entry_topics(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[str]]:
    rows = (await session.execute(
        select(LedgerEntryTopic.entry_id, LedgerTopic.slug)
        .join(LedgerTopic, LedgerTopic.id == LedgerEntryTopic.topic_id)
        .where(LedgerEntryTopic.entry_id.in_(entry_ids))
        .order_by(LedgerTopic.slug)
    )).all()
    out: dict[int, list[str]] = {i: [] for i in entry_ids}
    for r in rows:
        out[r.entry_id].append(r.slug)
    return out


async def entry_related(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[str]]:
    """See-also refs per entry, both directions (a link is stored once)."""
    a = select(LedgerEntryLink.entry_id.label("me"), LedgerEntryLink.related_id.label("other")).where(
        LedgerEntryLink.entry_id.in_(entry_ids))
    b = select(LedgerEntryLink.related_id.label("me"), LedgerEntryLink.entry_id.label("other")).where(
        LedgerEntryLink.related_id.in_(entry_ids))
    pairs = (await session.execute(a.union_all(b))).all()
    others = {p.other for p in pairs}
    refs = dict((await session.execute(
        select(LedgerEntry.id, LedgerEntry.ref).where(LedgerEntry.id.in_(others))
    )).all()) if others else {}
    out: dict[int, list[str]] = {i: [] for i in entry_ids}
    for p in pairs:
        out[p.me].append(refs[p.other])
    for refs_ in out.values():
        refs_.sort(key=_ref_sort_key)
    return out


def _ref_sort_key(ref: str):
    kind, number = parse_ref(ref)
    return (db.LEDGER_KINDS.index(kind), number)


def snapshot(entry: LedgerEntry, topics: list[str], related: list[str], superseded_ref: str | None) -> dict:
    """The entry as it is, for the history: every field an update can change."""
    out = {
        "title": entry.title, "body": entry.body, "status": entry.status, "origin": entry.origin,
        "area": entry.area_tag, "observed_on": entry.observed_on.isoformat(), "author": entry.author,
        "topics": topics, "related": related,
    }
    if entry.kind == "feedback":
        out["seen"] = entry.seen
    for name in KIND_FIELDS[entry.kind]:
        out[name] = getattr(entry, name)
    if superseded_ref is not None:
        out["superseded_by"] = superseded_ref
    if entry.handled_account is not None:
        out["handled_account"] = entry.handled_account
        out["handled_at"] = entry.handled_at.isoformat() if entry.handled_at else None
    return out


async def superseding_refs(session: AsyncSession, ids: list[int]) -> dict[int, str]:
    ids = [i for i in ids if i is not None]
    if not ids:
        return {}
    return dict((await session.execute(select(LedgerEntry.id, LedgerEntry.ref).where(LedgerEntry.id.in_(ids)))).all())


# ── Summary-view flags (shared with ledger_views) ────────────────────────────

@dataclass
class Flagged:
    line_id: int
    digest: str
    text: str
    reason: str


async def flag_lines(session: AsyncSession, uid: int | None, line_ids: list[int], reason: str,
                     who: str, author: str | None) -> list[Flagged]:
    """Mark live lines for review, each with a history row; returns what was flagged."""
    if not line_ids:
        return []
    reason = reason[:REASON_MAX]
    lines = (await session.execute(
        select(LedgerDigestLine, LedgerDigest.slug)
        .join(LedgerDigest, LedgerDigest.id == LedgerDigestLine.digest_id)
        .where(_owner(LedgerDigestLine, uid), LedgerDigestLine.id.in_(line_ids),
               LedgerDigestLine.removed_at.is_(None))
        .order_by(LedgerDigestLine.id)
        .with_for_update(of=LedgerDigestLine)
    )).all()
    out = []
    for line, slug in lines:
        session.add(LedgerRevision(
            user_id=line.user_id, line_id=line.id, revision=line.revision, action="flag", reason=reason,
            before=line_snapshot(line, None), created_by=who, author=author,
        ))
        line.needs_review = True
        line.review_reason = reason
        line.revision += 1
        line.updated_at = func.now()
        out.append(Flagged(line.id, slug, line.text, reason))
    return out


def line_snapshot(line: LedgerDigestLine, sources: list[str] | None) -> dict:
    out = {"row": line.row_label, "column": line.column_label, "position": line.position, "text": line.text,
           "needs_review": line.needs_review, "review_reason": line.review_reason,
           "removed": line.removed_at is not None}
    if sources is not None:
        out["sources"] = sources
    return out


async def lines_citing(session: AsyncSession, entry_ids: list[int]) -> list[int]:
    rows = (await session.execute(
        select(LedgerDigestSource.line_id).distinct()
        .join(LedgerDigestLine, LedgerDigestLine.id == LedgerDigestSource.line_id)
        .where(LedgerDigestSource.entry_id.in_(entry_ids), LedgerDigestLine.removed_at.is_(None))
    )).scalars().all()
    return list(rows)


async def check_challenges(session: AsyncSession, uid: int | None, raw) -> list[int]:
    """Line ids an entry says it contradicts; each must be a live line of the caller's."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in raw):
        raise invalid("challenges must be a list of summary-view line ids (ledger_digest with with_ids=true).")
    if len(raw) > LIST_ARG_MAX:
        raise invalid(f"challenges holds {len(raw)} lines; at most {LIST_ARG_MAX}.")
    ids = sorted(set(raw))
    live = set((await session.execute(
        select(LedgerDigestLine.id).where(_owner(LedgerDigestLine, uid), LedgerDigestLine.id.in_(ids),
                                          LedgerDigestLine.removed_at.is_(None))
    )).scalars().all())
    unknown = [i for i in ids if i not in live]
    if unknown:
        raise LedgerError("not_found", f"summary-view line(s) {unknown} do not exist or were removed. Nothing was written.")
    return ids


# ── View-due hints ───────────────────────────────────────────────────────────

async def view_hints(session: AsyncSession, uid: int | None, topic_ids: list[int] | None = None) -> list[dict]:
    """Topics whose current quirks are numerous and mostly cited by no view line."""
    threshold = int(settings.ledger_view_hint_min_quirks)
    cited = (
        select(LedgerDigestSource.entry_id)
        .join(LedgerDigestLine, LedgerDigestLine.id == LedgerDigestSource.line_id)
        .where(LedgerDigestLine.removed_at.is_(None))
    )
    is_cited = LedgerEntry.id.in_(cited)
    stmt = (
        select(LedgerTopic.slug, func.count().label("n"),
               func.count().filter(~is_cited).label("uncited"))
        .select_from(LedgerEntryTopic)
        .join(LedgerTopic, LedgerTopic.id == LedgerEntryTopic.topic_id)
        .join(LedgerEntry, LedgerEntry.id == LedgerEntryTopic.entry_id)
        .where(_owner(LedgerTopic, uid), LedgerEntry.kind == "quirk", LedgerEntry.status == "current")
        .group_by(LedgerTopic.slug)
    )
    if topic_ids is not None:
        if not topic_ids:
            return []
        stmt = stmt.where(LedgerTopic.id.in_(topic_ids))
    rows = (await session.execute(stmt)).all()
    return [
        {"topic": r.slug, "current_quirks": r.n, "not_in_any_view": r.uncited,
         "hint": (f"Topic {r.slug} holds {r.n} current quirks and {r.uncited} of them are in no summary "
                  "view: extend a view that covers it, or create one (ledger_digest_edit), and tell the owner.")}
        for r in rows if r.n >= threshold and r.uncited * 2 > r.n
    ]


# ── Keyword matching ─────────────────────────────────────────────────────────

def search_document():
    """The words an entry is found by: its ref, title, body and own fields.

    Computed at query time rather than stored: the ledger is small, and a
    stored vector would go stale silently when `FTS_CONFIGS` changes.
    """
    doc = func.concat_ws(
        " ", LedgerEntry.ref, LedgerEntry.title, LedgerEntry.body, LedgerEntry.component, LedgerEntry.fix,
        LedgerEntry.certainty_note, LedgerEntry.sources, LedgerEntry.target, LedgerEntry.proposed_text,
        LedgerEntry.final_text, LedgerEntry.changes, LedgerEntry.owner_reason, LedgerEntry.handled_account,
    )
    parts = [func.to_tsvector(literal(cfg).cast(text_regconfig()), doc) for cfg in settings.fts_configs]
    return reduce(lambda a, b: a.op("||")(b), parts)


def text_regconfig():
    from sqlalchemy.dialects.postgresql import REGCONFIG

    return REGCONFIG()


def tsquery_for(query: str, *, any_word: bool = False):
    """The query as a tsquery under every configured FTS config (OR-ed)."""
    if any_word:
        words = re.findall(r"\w{3,}", query)[:12]
        query = " or ".join(words) if words else query
    parts = [func.websearch_to_tsquery(literal(cfg).cast(text_regconfig()), query) for cfg in settings.fts_configs]
    return reduce(lambda a, b: a.op("||")(b), parts)


# ── Filters ──────────────────────────────────────────────────────────────────

@dataclass
class Filters:
    kinds: list[str] = field(default_factory=list)
    area: str | None = None
    no_area: bool = False
    topic_ids: list[int] = field(default_factory=list)
    statuses: tuple[str, ...] = ACTIVE_STATUSES
    origin: str | None = None
    unseen: bool = False
    since: date | None = None

    def where(self, uid: int | None) -> list:
        clauses = [_owner(LedgerEntry, uid), LedgerEntry.status.in_(self.statuses)]
        if self.kinds:
            clauses.append(LedgerEntry.kind.in_(self.kinds))
        if self.area is not None:
            clauses.append(LedgerEntry.area_tag == self.area)
        if self.no_area:
            clauses.append(LedgerEntry.area_tag.is_(None))
        for topic_id in self.topic_ids:
            clauses.append(LedgerEntry.id.in_(
                select(LedgerEntryTopic.entry_id).where(LedgerEntryTopic.topic_id == topic_id)))
        if self.origin is not None:
            clauses.append(LedgerEntry.origin == self.origin)
        if self.unseen:
            clauses.append(and_(LedgerEntry.kind == "feedback", LedgerEntry.seen.is_(False)))
        if self.since is not None:
            clauses.append(LedgerEntry.updated_at >= day_start_utc(self.since))
        return clauses


def parse_statuses(status) -> tuple[str, ...]:
    if status is None or status == "active":
        return ACTIVE_STATUSES
    if status == "closed":
        return CLOSED_STATUSES
    if status == "any":
        return ALL_STATUSES
    values = [status] if isinstance(status, str) else status
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise invalid("status must be active, closed, any, a status, or a list of statuses.")
    unknown = [v for v in values if v not in ALL_STATUSES]
    if unknown:
        raise invalid(
            f"unknown status {', '.join(unknown)}. Statuses: active (default), closed, any, or any of "
            f"{', '.join(ALL_STATUSES)}.",
            allowed=[{"name": s} for s in ("active", "closed", "any", *ALL_STATUSES)],
        )
    return tuple(values)


async def build_filters(session: AsyncSession, uid: int | None, *, kinds=None, area=None, topics=None,
                        status=None, origin=None, unseen=False, since=None) -> Filters:
    f = Filters(statuses=parse_statuses(status), unseen=bool(unseen))
    for kind in bounded_list(kinds, "kinds"):
        if kind not in db.LEDGER_KINDS:
            raise invalid(f"unknown kind {kind!r}; kinds: {', '.join(db.LEDGER_KINDS)}.")
        f.kinds.append(kind)
    if area is not None:
        if area.strip().lower() == "none":
            f.no_area = True
        else:
            f.area = resolve_area(await area_index(session, uid), area)
    topics = bounded_list(topics, "topics")
    if topics:
        # A filter may name a merged topic: its entries moved to the target.
        rows = (await session.execute(
            select(LedgerTopic.slug, LedgerTopic.id, LedgerTopic.merged_into)
            .where(_owner(LedgerTopic, uid), LedgerTopic.slug.in_([t.strip().lower() for t in topics]))
        )).all()
        known = {r.slug: (r.merged_into or r.id) for r in rows}
        unknown = [t for t in topics if t.strip().lower() not in known]
        if unknown:
            raise invalid(f"unknown topic(s) {', '.join(unknown)}.", allowed=await topics_allowed(session, uid))
        f.topic_ids = sorted(set(known.values()))
    if origin is not None:
        if origin not in db.LEDGER_ORIGINS:
            raise invalid("origin must be agent or owner.")
        f.origin = origin
    if since is not None:
        f.since = parse_day(since, "since")
    return f


# ── Add ──────────────────────────────────────────────────────────────────────

@dataclass
class AddOutcome:
    entry: LedgerEntry
    topics: list[str]
    related: list[str]
    similar: list[dict]
    flagged: list[Flagged]
    hints: list[dict]
    notes: list[str]


async def _next_number(session: AsyncSession, uid: int | None, kind: str) -> int:
    """The kind's next number for this owner, from its counter row (locked).

    A missing counter starts from the highest number in use, so entries
    imported without one are never collided with.
    """
    highest = select(func.coalesce(func.max(LedgerEntry.number), 0)).where(
        _owner(LedgerEntry, uid), LedgerEntry.kind == kind).scalar_subquery()
    stmt = pg_insert(LedgerRefCounter).values(user_id=uid, kind=kind, last_number=highest + 1)
    stmt = stmt.on_conflict_do_update(
        index_elements=["owner_key", "kind"],
        set_={"last_number": func.greatest(LedgerRefCounter.last_number, highest) + 1},
    ).returning(LedgerRefCounter.last_number)
    return int((await session.execute(stmt)).scalar_one())


async def add_entry(
    session: AsyncSession, uid: int | None, *, kind: str, title: str, body: str = "", area: str | None = None,
    topics=None, details=None, related=None, origin: str = "agent", observed_on: str | None = None,
    seen: bool | None = None, status: str | None = None, author: str | None = None, challenges=None,
) -> AddOutcome:
    if kind not in db.LEDGER_KINDS:
        raise invalid(f"kind must be one of {', '.join(db.LEDGER_KINDS)} (got {kind!r}).",
                      allowed=[{"name": k} for k in db.LEDGER_KINDS])
    title = clean_title(title)
    body = check_body(body or "")
    author = clean_author(author)
    if origin not in db.LEDGER_ORIGINS:
        raise invalid("origin must be agent or owner.")
    fields = validate_details(kind, details, for_add=True)
    notes: list[str] = []

    area_tag = resolve_area(await area_index(session, uid), area) if area else None
    if status is None:
        status = {"quirk": "current", "feedback": "open" if area_tag else "to_triage"}.get(kind)
        if status is None:
            raise invalid("a proposal needs status: its outcome, accepted, adjusted, rejected or withdrawn.",
                          allowed=[{"name": s} for s in ADD_STATUSES["proposal"]])
    if status not in ADD_STATUSES[kind]:
        raise invalid(
            f"a new {kind} can be {', '.join(ADD_STATUSES[kind])} (got {status!r}). Closing an entry is an "
            "update with a reason.", allowed=[{"name": s} for s in ADD_STATUSES[kind]])
    if kind == "feedback":
        if status == "open" and area_tag is None:
            raise invalid("open feedback needs an area; feedback with no area is to_triage.")
        if status == "to_triage" and area_tag is not None:
            raise invalid("feedback with an area is open, not to_triage.")
        if seen is None:
            seen = origin == "owner"
        if origin == "owner" and not seen:
            raise invalid("feedback from the owner is seen from the start (seen cannot be false).")
    elif seen is not None:
        raise invalid("only feedback has seen.")
    if status in ("accepted", "adjusted") and not fields.get("final_text"):
        raise invalid(f"an {status} proposal needs details.final_text: the version that went in.")

    topic_ids = await resolve_topics(session, uid, bounded_list(topics, "topics"))
    related_entries, missing = await find_entries(session, uid, bounded_list(related, "related"))
    if missing:
        raise LedgerError("not_found", f"related entr{'y' if len(missing) == 1 else 'ies'} {', '.join(missing)} "
                          "not found. Nothing was written.")
    challenged = await check_challenges(session, uid, challenges)
    day = parse_day(observed_on, "observed_on") if observed_on else today()

    number = await _next_number(session, uid, kind)
    entry = LedgerEntry(
        user_id=uid, kind=kind, number=number, ref=new_ref(kind, number), status=status, origin=origin,
        title=title, body=body, area_tag=area_tag, seen=seen if kind == "feedback" else None,
        observed_on=day, created_by=created_by(), author=author, revision=1, content_hash="", **fields,
    )
    entry.content_hash = content_hash(embed_text(entry))
    session.add(entry)
    await session.flush()

    for topic_id in sorted(set(topic_ids.values())):
        session.add(LedgerEntryTopic(user_id=uid, entry_id=entry.id, topic_id=topic_id))
    for other in {e.id: e for e in related_entries.values()}.values():
        if other.id != entry.id:
            low, high = sorted((entry.id, other.id))
            session.add(LedgerEntryLink(user_id=uid, entry_id=low, related_id=high))
    flagged = await flag_lines(
        session, uid, challenged, f"{entry.ref} says it contradicts this line: {title}", entry.created_by, author)
    await session.flush()
    similar = await similar_entries(session, uid, entry)
    hints = await view_hints(session, uid, sorted(set(topic_ids.values()))) if kind == "quirk" else []
    await session.commit()
    return AddOutcome(
        entry=entry, topics=sorted(topic_ids), related=sorted({e.ref for e in related_entries.values()},
                                                              key=_ref_sort_key),
        similar=similar, flagged=flagged, hints=hints, notes=notes,
    )


async def similar_entries(session: AsyncSession, uid: int | None, entry: LedgerEntry) -> list[dict]:
    """Up to five active entries sharing words with `entry`'s title, best first.

    Words only: a new entry has no vector yet, and the write never waits for
    one. Not a refusal: a fuzzy score must not block logging.
    """
    query = tsquery_for(entry.title, any_word=True)
    doc = search_document()
    rank = func.ts_rank_cd(doc, query)
    rows = (await session.execute(
        select(LedgerEntry.ref, LedgerEntry.kind, LedgerEntry.status, LedgerEntry.title, rank.label("rank"))
        .where(_owner(LedgerEntry, uid), LedgerEntry.id != entry.id, LedgerEntry.status.in_(ACTIVE_STATUSES),
               doc.op("@@")(query))
        .order_by(rank.desc(), LedgerEntry.id).limit(SIMILAR_MAX)
    )).all()
    return [{"ref": r.ref, "kind": r.kind, "status": r.status, "title": r.title} for r in rows]


# ── Update ───────────────────────────────────────────────────────────────────

_UNSET = object()


@dataclass
class UpdateOutcome:
    entry: LedgerEntry
    changed: list[str]
    flagged: list[Flagged]
    notes: list[str]


def _append_paragraph(body: str, text_: str) -> str:
    addition = text_.strip()
    if not addition:
        raise invalid("append is empty.")
    stamp = today().isoformat()
    return (body.rstrip() + "\n\n" if body.strip() else "") + f"{stamp}: {addition}"


async def update_entry(
    session: AsyncSession, uid: int | None, *, ref: str, expected_revision: int | None, reason: str | None,
    title=None, body=None, append=None, status=None, area=None, topics_add=None, topics_remove=None,
    details=None, superseded_by=None, handled_account=None, related=None, author=None, challenges=None,
    seen=None, observed_on=None, origin=None,
) -> UpdateOutcome:
    if expected_revision is None:
        raise invalid("expected_revision is required: the revision you read (ledger_get or ledger_search). "
                      "A change based on an outdated version is refused.")
    reason = clean_reason(reason)
    author = clean_author(author)
    found, missing = await find_entries(session, uid, [ref], lock=True)
    if missing:
        raise LedgerError("not_found", f"entry {ref} not found. Nothing was written.")
    entry = found[ref]
    if entry.revision != expected_revision:
        raise LedgerError(
            "stale_revision",
            f"{entry.ref} is at revision {entry.revision}, not {expected_revision}: it changed since you read "
            "it. Read it again (ledger_get, history=true shows what changed), then decide. Nothing was written.",
            current_revision=entry.revision,
        )
    if body is not None and append is not None:
        raise invalid("give body (replace it) or append (add a dated paragraph), not both.")

    ids = [entry.id]
    topics_before = (await entry_topics(session, ids))[entry.id]
    related_before = (await entry_related(session, ids))[entry.id]
    sup = await superseding_refs(session, [entry.superseded_by])
    before = snapshot(entry, topics_before, related_before, sup.get(entry.superseded_by))
    changed: list[str] = []
    notes: list[str] = []

    if title is not None:
        new = clean_title(title)
        if new != entry.title:
            entry.title = new
            changed.append("title")
    if body is not None:
        new = check_body(body)
        if new != entry.body:
            entry.body = new
            changed.append("body")
    if append is not None:
        entry.body = check_body(_append_paragraph(entry.body, append))
        changed.append("body (appended)")
    if author is not None:
        entry.author = author
    if observed_on is not None:
        day = parse_day(observed_on, "observed_on")
        if day != entry.observed_on:
            entry.observed_on = day
            changed.append("observed_on")
    if origin is not None:
        if origin not in db.LEDGER_ORIGINS:
            raise invalid("origin must be agent or owner.")
        if origin != entry.origin:
            entry.origin = origin
            changed.append("origin")
            if entry.kind == "feedback" and origin == "owner":
                entry.seen = True
    if details is not None:
        for name, value in validate_details(entry.kind, details, for_add=False).items():
            if getattr(entry, name) != value:
                setattr(entry, name, value)
                changed.append(name)
    if area is not None:
        new_area = None if area.strip() in ("", "none") else resolve_area(await area_index(session, uid), area)
        if new_area != entry.area_tag:
            entry.area_tag = new_area
            changed.append("area")
            if entry.kind == "feedback" and status is None:
                if new_area and entry.status == "to_triage":
                    entry.status = "open"
                    changed.append("status")
                    notes.append(f"{entry.ref} now has an area, so it moved from to_triage to open.")
                elif new_area is None and entry.status == "open":
                    entry.status = "to_triage"
                    changed.append("status")
                    notes.append(f"{entry.ref} has no area any more, so it moved from open to to_triage.")
    if seen is not None:
        if entry.kind != "feedback":
            raise invalid("only feedback has seen.")
        if entry.seen != bool(seen):
            if entry.origin == "owner" and not seen:
                raise invalid("feedback from the owner is always seen.")
            entry.seen = bool(seen)
            changed.append("seen")

    if superseded_by is not None and status not in (None, "superseded"):
        raise invalid("superseded_by goes with status superseded.")
    if status is not None:
        await _apply_status(session, uid, entry, status, superseded_by, handled_account, changed)
    elif handled_account is not None:
        if entry.status != "handled":
            raise invalid("handled_account goes with status handled.")
        new = handled_account.strip()
        if not new or len(new) > BODY_MAX:
            raise invalid(f"handled_account must be 1 to {BODY_MAX} characters.")
        if new != entry.handled_account:
            entry.handled_account = new
            changed.append("handled_account")
    elif superseded_by is not None:
        await _apply_status(session, uid, entry, "superseded", superseded_by, None, changed)
    if entry.kind == "proposal" and entry.status in ("accepted", "adjusted") and not entry.final_text:
        raise invalid(f"an {entry.status} proposal needs details.final_text: the version that went in.")

    if topics_add or topics_remove:
        add_ids = await resolve_topics(session, uid, bounded_list(topics_add, "topics_add"))
        remove = [t.strip().lower() for t in bounded_list(topics_remove, "topics_remove")]
        not_on = [t for t in remove if t not in topics_before]
        if not_on:
            raise invalid(f"{entry.ref} has no topic {', '.join(not_on)} to remove (it has: "
                          f"{', '.join(topics_before) or 'none'}).")
        for slug, topic_id in add_ids.items():
            if slug not in topics_before:
                await session.execute(pg_insert(LedgerEntryTopic).values(
                    user_id=entry.user_id, entry_id=entry.id, topic_id=topic_id).on_conflict_do_nothing())
                changed.append(f"topic +{slug}")
        if remove:
            remove_ids = (await session.execute(select(LedgerTopic.id).where(
                _owner(LedgerTopic, uid), LedgerTopic.slug.in_(remove)))).scalars().all()
            await session.execute(delete(LedgerEntryTopic).where(
                LedgerEntryTopic.entry_id == entry.id, LedgerEntryTopic.topic_id.in_(remove_ids)))
            changed.extend(f"topic -{slug}" for slug in remove)
    if related is not None:
        others, missing = await find_entries(session, uid, bounded_list(related, "related"))
        if missing:
            raise LedgerError("not_found", f"related entr{'y' if len(missing) == 1 else 'ies'} "
                              f"{', '.join(missing)} not found. Nothing was written.")
        for other in {e.id: e for e in others.values()}.values():
            if other.id == entry.id or other.ref in related_before:
                continue
            low, high = sorted((entry.id, other.id))
            await session.execute(pg_insert(LedgerEntryLink).values(
                user_id=entry.user_id, entry_id=low, related_id=high).on_conflict_do_nothing())
            changed.append(f"related +{other.ref}")

    challenged = await check_challenges(session, uid, challenges)
    if not changed and not challenged:
        await session.rollback()
        raise invalid(f"nothing to change: every value given is what {entry.ref} already holds. "
                      "Nothing was written.")

    who = created_by()
    flagged: list[Flagged] = []
    if changed:
        session.add(LedgerRevision(
            user_id=entry.user_id, entry_id=entry.id, revision=entry.revision, action="update",
            reason=reason, before=before, created_by=who, author=author,
        ))
        entry.revision += 1
        entry.updated_at = func.now()
        entry.content_hash = content_hash(embed_text(entry))
        citing = await lines_citing(session, [entry.id])
        flagged = await flag_lines(session, uid, citing, f"{entry.ref} changed: {reason}", who, author)
    flagged += await flag_lines(
        session, uid, [i for i in challenged if i not in {f.line_id for f in flagged}],
        f"{entry.ref} says it contradicts this line: {reason}", who, author)
    await session.commit()
    await session.refresh(entry)
    return UpdateOutcome(entry=entry, changed=changed, flagged=flagged, notes=notes)


async def _apply_status(session, uid, entry: LedgerEntry, status: str, superseded_by, handled_account,
                        changed: list[str]) -> None:
    allowed = db.LEDGER_KIND_STATUSES[entry.kind] + db.LEDGER_CLOSING_STATUSES
    if status not in allowed:
        raise invalid(f"a {entry.kind} can be {', '.join(allowed)} (got {status!r}).",
                      allowed=[{"name": s} for s in allowed])
    if status == "to_triage" and entry.area_tag is not None:
        raise invalid("feedback with an area is open, not to_triage; clear the area (area=\"\") first.")
    if status == "open" and entry.kind == "feedback" and entry.area_tag is None:
        raise invalid("open feedback needs an area: give area as well.")
    if status == "superseded":
        if superseded_by is None:
            raise invalid("status superseded needs superseded_by: the ref of the entry that holds the content.")
        others, missing = await find_entries(session, uid, [superseded_by])
        if missing:
            raise LedgerError("not_found", f"entry {superseded_by} not found. Nothing was written.")
        other = others[superseded_by]
        if other.id == entry.id:
            raise invalid("an entry cannot be superseded by itself.")
        entry.superseded_by = other.id
    elif superseded_by is not None:
        raise invalid("superseded_by goes with status superseded.")
    else:
        entry.superseded_by = None
    if status == "handled":
        account = (handled_account or "").strip() or (entry.handled_account if entry.status == "handled" else "")
        if not account:
            raise invalid("status handled needs handled_account: what was done about it, or why nothing is "
                          "left to do.")
        if len(account) > BODY_MAX:
            raise invalid(f"handled_account is {len(account)} characters; at most {BODY_MAX}.")
        entry.handled_account = account
        if entry.status != "handled":
            entry.handled_at = func.now()
    else:
        if handled_account is not None:
            raise invalid("handled_account goes with status handled.")
        entry.handled_account = None
        entry.handled_at = None
    if status != entry.status:
        entry.status = status
        changed.append("status")


async def mark_seen(session: AsyncSession, uid: int | None, refs: list[str]) -> dict:
    """Mark feedback the owner has heard of; no revision, no reason, nothing overwritten."""
    if not refs:
        raise invalid("refs is empty.")
    if len(refs) > SEEN_MAX_REFS:
        raise invalid(f"refs holds {len(refs)} entries; at most {SEEN_MAX_REFS} per call.")
    found, missing = await find_entries(session, uid, refs)
    marked, already, not_feedback = [], [], []
    ids = []
    for entry in {e.id: e for e in found.values()}.values():
        if entry.kind != "feedback":
            not_feedback.append(entry.ref)
        elif entry.seen:
            already.append(entry.ref)
        else:
            marked.append(entry.ref)
            ids.append(entry.id)
    if ids:
        await session.execute(update(LedgerEntry).where(LedgerEntry.id.in_(ids), LedgerEntry.seen.is_(False))
                              .values(seen=True))
    await session.commit()
    return {"marked": sorted(marked, key=_ref_sort_key), "already_seen": sorted(already, key=_ref_sort_key),
            "not_feedback": sorted(not_feedback, key=_ref_sort_key), "not_found": missing}


# ── Get ──────────────────────────────────────────────────────────────────────

async def history(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[dict]]:
    rows = (await session.execute(
        select(LedgerRevision).where(LedgerRevision.entry_id.in_(entry_ids)).order_by(LedgerRevision.id)
    )).scalars().all()
    out: dict[int, list[dict]] = {i: [] for i in entry_ids}
    for r in rows:
        out[r.entry_id].append({
            "revision": r.revision, "action": r.action, "reason": r.reason, "at": r.created_at.isoformat(),
            "by": r.created_by, "author": r.author, "before": r.before,
        })
    return out


async def lines_for_entries(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[dict]]:
    rows = (await session.execute(
        select(LedgerDigestSource.entry_id, LedgerDigestLine.id, LedgerDigestLine.text,
               LedgerDigestLine.row_label, LedgerDigestLine.column_label, LedgerDigestLine.needs_review,
               LedgerDigest.slug)
        .join(LedgerDigestLine, LedgerDigestLine.id == LedgerDigestSource.line_id)
        .join(LedgerDigest, LedgerDigest.id == LedgerDigestLine.digest_id)
        .where(LedgerDigestSource.entry_id.in_(entry_ids), LedgerDigestLine.removed_at.is_(None))
        .order_by(LedgerDigest.slug, LedgerDigestLine.position)
    )).all()
    out: dict[int, list[dict]] = {i: [] for i in entry_ids}
    for r in rows:
        out[r.entry_id].append({"view": r.slug, "line": r.id, "row": r.row_label, "column": r.column_label,
                                "text": r.text, "needs_review": r.needs_review})
    return out


def entry_fields(entry: LedgerEntry) -> dict:
    """The kind's own fields that are set."""
    return {name: getattr(entry, name) for name in KIND_FIELDS[entry.kind] if getattr(entry, name) is not None}


def json_size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))
