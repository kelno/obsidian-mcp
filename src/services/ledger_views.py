"""The agent ledger's topics and summary views (`ledger_topics`, `ledger_topic_edit`,
`ledger_digest`, `ledger_digest_edit`).

A summary view ("digest" in code and table names) is a short page agents read
instead of every entry: a list of lines, or a matrix whose cells are lines.
Each line cites the entries it rests on, and is flagged for review when one of
them changes (`ledger.flag_lines`). Agents create and edit views and topics
themselves and tell the owner (owner's decision); every change carries a
reason and lands in `ledger_revisions`. Nothing is deleted: a removed line
keeps its row, a merged topic keeps its row and points to its target.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import db
from src.models.db import (
    LedgerDigest,
    LedgerDigestColumn,
    LedgerDigestLine,
    LedgerDigestSource,
    LedgerEntry,
    LedgerEntryTopic,
    LedgerRevision,
    LedgerTopic,
)
from src.services import ledger
from src.services.ledger import LedgerError, invalid

_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SLUG_MAX = 64
TOPIC_DESCRIPTION_MAX = 300
DIGEST_TITLE_MAX = 150
DIGEST_DESCRIPTION_MAX = 500
LINE_TEXT_MAX = 2000
ROW_LABEL_MAX = 150
COLUMN_LABEL_MAX = 100
#: Operations one `ledger_digest_edit` call may carry, applied all or none.
MAX_OPERATIONS = 50
#: Sources one line may cite.
MAX_SOURCES = 20


def _owner(model, uid):
    return model.owner_key == ledger.owner_key(uid)


def clean_slug(raw, name: str = "slug") -> str:
    slug = (raw or "").strip().lower() if isinstance(raw, str) else ""
    if not slug or len(slug) > SLUG_MAX or not _SLUG.match(slug):
        raise invalid(f"{name} must be lowercase letters, digits and hyphens, at most {SLUG_MAX} characters "
                      f"(got {raw!r}).")
    return slug


def one_line(raw, name: str, cap: int) -> str:
    value = (raw or "").strip() if isinstance(raw, str) else ""
    if not value:
        raise invalid(f"{name} is required.")
    if "\n" in value or "\r" in value:
        raise invalid(f"{name} must be one line.")
    if len(value) > cap:
        raise invalid(f"{name} is {len(value)} characters; at most {cap}.")
    return value


# ── Topics ───────────────────────────────────────────────────────────────────

async def list_topics(session: AsyncSession, uid: int | None) -> dict:
    """Areas (from the vault) and topics, with counts of active and all entries."""
    index = await ledger.area_index(session, uid)
    active = LedgerEntry.status.in_(ledger.ACTIVE_STATUSES)
    area_counts = {
        r.area_tag: (r.active, r.total) for r in (await session.execute(
            select(LedgerEntry.area_tag, func.count().filter(active).label("active"), func.count().label("total"))
            .where(_owner(LedgerEntry, uid)).group_by(LedgerEntry.area_tag)
        )).all()
    }
    areas = [
        {"name": tag, "description": desc, "main_note": path,
         "entries_active": area_counts.get(tag, (0, 0))[0], "entries_total": area_counts.get(tag, (0, 0))[1]}
        for tag, path, desc in index.described()
    ]
    known = {a["name"] for a in areas}
    orphan_areas = sorted(t for t in area_counts if t is not None and t not in known)
    no_area = area_counts.get(None, (0, 0))

    topic_counts = {
        r.topic_id: (r.active, r.total) for r in (await session.execute(
            select(LedgerEntryTopic.topic_id, func.count().filter(active).label("active"),
                   func.count().label("total"))
            .join(LedgerEntry, LedgerEntry.id == LedgerEntryTopic.entry_id)
            .where(_owner(LedgerEntry, uid)).group_by(LedgerEntryTopic.topic_id)
        )).all()
    }
    rows = (await session.execute(
        select(LedgerTopic).where(_owner(LedgerTopic, uid)).order_by(LedgerTopic.status, LedgerTopic.slug)
    )).scalars().all()
    slugs = {t.id: t.slug for t in rows}
    topics = [
        {"name": t.slug, "kind": t.kind, "description": t.description, "area": t.area_tag, "status": t.status,
         "merged_into": slugs.get(t.merged_into), "revision": t.revision,
         "entries_active": topic_counts.get(t.id, (0, 0))[0], "entries_total": topic_counts.get(t.id, (0, 0))[1]}
        for t in rows
    ]
    return {
        "areas": areas, "topics": topics, "hints": await ledger.view_hints(session, uid),
        "entries_without_area": {"active": no_area[0], "total": no_area[1]},
        "areas_no_longer_declared": orphan_areas,
    }


@dataclass
class TopicOutcome:
    topic: dict
    moved: list[str] = field(default_factory=list)


def _topic_snapshot(topic: LedgerTopic, merged_into_slug: str | None = None) -> dict:
    return {"slug": topic.slug, "kind": topic.kind, "description": topic.description, "area": topic.area_tag,
            "status": topic.status, "merged_into": merged_into_slug}


def _topic_out(topic: LedgerTopic, merged_into_slug: str | None = None) -> dict:
    return {**_topic_snapshot(topic, merged_into_slug), "name": topic.slug, "revision": topic.revision}


async def edit_topic(session: AsyncSession, uid: int | None, *, action: str, slug: str, reason: str,
                     kind=None, description=None, area=None, into=None, expected_revision=None,
                     author=None) -> TopicOutcome:
    reason = ledger.clean_reason(reason)
    author = ledger.clean_author(author)
    who = ledger.created_by()
    slug = clean_slug(slug)
    if action == "create":
        if kind not in db.LEDGER_TOPIC_KINDS:
            raise invalid("a topic's kind is system (a part of the system) or surface (where Claude runs).",
                          allowed=[{"name": k} for k in db.LEDGER_TOPIC_KINDS])
        description = one_line(description, "description", TOPIC_DESCRIPTION_MAX)
        area_tag = ledger.resolve_area(await ledger.area_index(session, uid), area) if area else None
        if into is not None or expected_revision is not None:
            raise invalid("create takes slug, kind, description and optionally area.")
        exists = (await session.execute(select(LedgerTopic).where(
            _owner(LedgerTopic, uid), LedgerTopic.slug == slug))).scalar_one_or_none()
        if exists is not None:
            raise LedgerError("already_exists", f"topic {slug} already exists ({exists.status}). Nothing was written.")
        topic = LedgerTopic(user_id=uid, slug=slug, kind=kind, description=description, area_tag=area_tag)
        session.add(topic)
        await session.flush()
        session.add(LedgerRevision(user_id=uid, topic_id=topic.id, revision=None, action="create",
                                   reason=reason, before=None, created_by=who, author=author))
        await session.commit()
        await session.refresh(topic)
        return TopicOutcome(_topic_out(topic))

    if action not in ("describe", "merge"):
        raise invalid("action is create, describe or merge.", allowed=[{"name": a} for a in ("create", "describe", "merge")])
    topic = (await session.execute(select(LedgerTopic).where(
        _owner(LedgerTopic, uid), LedgerTopic.slug == slug).with_for_update())).scalar_one_or_none()
    if topic is None:
        raise LedgerError("not_found", f"topic {slug} not found. Nothing was written.",
                          allowed=await ledger.topics_allowed(session, uid))
    if topic.status == "merged":
        raise invalid(f"topic {slug} was merged and can no longer change. Nothing was written.")
    if expected_revision is not None and expected_revision != topic.revision:
        raise LedgerError("stale_revision", f"topic {slug} is at revision {topic.revision}, not {expected_revision}. "
                          "Read it again with ledger_topics. Nothing was written.", current_revision=topic.revision)
    before = _topic_snapshot(topic)

    if action == "describe":
        changed = False
        if description is not None:
            new = one_line(description, "description", TOPIC_DESCRIPTION_MAX)
            changed |= new != topic.description
            topic.description = new
        if kind is not None:
            if kind not in db.LEDGER_TOPIC_KINDS:
                raise invalid("kind is system or surface.")
            changed |= kind != topic.kind
            topic.kind = kind
        if area is not None:
            new_area = None if area.strip() in ("", "none") else ledger.resolve_area(
                await ledger.area_index(session, uid), area)
            changed |= new_area != topic.area_tag
            topic.area_tag = new_area
        if into is not None:
            raise invalid("into goes with action merge.")
        if not changed:
            raise invalid(f"nothing to change on topic {slug}. Nothing was written.")
        session.add(LedgerRevision(user_id=topic.user_id, topic_id=topic.id, revision=topic.revision,
                                   action="describe", reason=reason, before=before, created_by=who, author=author))
        topic.revision += 1
        topic.updated_at = func.now()
        await session.commit()
        await session.refresh(topic)
        return TopicOutcome(_topic_out(topic))

    # merge: every entry of `slug` moves to `into`; `slug` keeps its row, marked merged.
    if description is not None or kind is not None or area is not None:
        raise invalid("merge takes slug, into and reason only.")
    target_slug = clean_slug(into, "into")
    if target_slug == slug:
        raise invalid("a topic cannot be merged into itself.")
    target = (await session.execute(select(LedgerTopic).where(
        _owner(LedgerTopic, uid), LedgerTopic.slug == target_slug).with_for_update())).scalar_one_or_none()
    if target is None or target.status != "active":
        raise LedgerError("not_found", f"topic {target_slug} not found or not active. Nothing was written.",
                          allowed=await ledger.topics_allowed(session, uid))
    entries = (await session.execute(
        select(LedgerEntry.id, LedgerEntry.ref).join(LedgerEntryTopic, LedgerEntryTopic.entry_id == LedgerEntry.id)
        .where(LedgerEntryTopic.topic_id == topic.id).order_by(LedgerEntry.id)
    )).all()
    for e in entries:
        await session.execute(pg_insert(LedgerEntryTopic).values(
            user_id=topic.user_id, entry_id=e.id, topic_id=target.id).on_conflict_do_nothing())
    await session.execute(delete(LedgerEntryTopic).where(LedgerEntryTopic.topic_id == topic.id))
    moved = sorted((e.ref for e in entries), key=ledger._ref_sort_key)
    session.add(LedgerRevision(
        user_id=topic.user_id, topic_id=topic.id, revision=topic.revision, action="merge",
        reason=f"merged into {target_slug}: {reason}"[:ledger.REASON_MAX],
        before={**before, "entries": moved}, created_by=who, author=author,
    ))
    topic.status = "merged"
    topic.merged_into = target.id
    topic.revision += 1
    topic.updated_at = func.now()
    await session.commit()
    await session.refresh(topic)
    return TopicOutcome(_topic_out(topic, target_slug), moved)


# ── Reading a summary view ───────────────────────────────────────────────────

async def list_digests(session: AsyncSession, uid: int | None) -> list[dict]:
    live = LedgerDigestLine.removed_at.is_(None)
    counts = {
        r.digest_id: (r.lines, r.review) for r in (await session.execute(
            select(LedgerDigestLine.digest_id, func.count().filter(live).label("lines"),
                   func.count().filter(live & LedgerDigestLine.needs_review).label("review"))
            .where(_owner(LedgerDigestLine, uid)).group_by(LedgerDigestLine.digest_id)
        )).all()
    }
    rows = (await session.execute(
        select(LedgerDigest).where(_owner(LedgerDigest, uid)).order_by(LedgerDigest.slug))).scalars().all()
    return [{"slug": d.slug, "title": d.title, "description": d.description, "layout": d.layout,
             "lines": counts.get(d.id, (0, 0))[0], "lines_under_review": counts.get(d.id, (0, 0))[1]}
            for d in rows]


async def _digest(session, uid, slug, *, lock=False) -> LedgerDigest | None:
    stmt = select(LedgerDigest).where(_owner(LedgerDigest, uid), LedgerDigest.slug == slug)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _columns(session, digest_id) -> list[str]:
    return list((await session.execute(
        select(LedgerDigestColumn.label).where(LedgerDigestColumn.digest_id == digest_id)
        .order_by(LedgerDigestColumn.position, LedgerDigestColumn.id))).scalars().all())


async def _sources(session, line_ids: list[int]) -> dict[int, list[dict]]:
    if not line_ids:
        return {}
    rows = (await session.execute(
        select(LedgerDigestSource.line_id, LedgerEntry.ref, LedgerEntry.status, LedgerEntry.observed_on,
               LedgerEntry.updated_at)
        .join(LedgerEntry, LedgerEntry.id == LedgerDigestSource.entry_id)
        .where(LedgerDigestSource.line_id.in_(line_ids))
    )).all()
    out: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        newest = max(r.observed_on, r.updated_at.date())
        out[r.line_id].append({"ref": r.ref, "status": r.status, "date": newest})
    for refs in out.values():
        refs.sort(key=lambda s: ledger._ref_sort_key(s["ref"]))
    return out


def _cite(sources: list[dict]) -> str:
    if not sources:
        return "(unsourced)"
    refs = ", ".join(s["ref"] + ("" if s["status"] in ledger.ACTIVE_STATUSES else f" {s['status']}")
                     for s in sources)
    return f"({refs}; newest {max(s['date'] for s in sources).isoformat()})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


async def read_digest(session: AsyncSession, uid: int | None, slug: str, *, with_ids: bool) -> dict:
    digest = await _digest(session, uid, clean_slug(slug))
    if digest is None:
        raise LedgerError("not_found", f"summary view {slug} not found. Call ledger_digest without slug for the list.",
                          allowed=[{"name": d["slug"], "description": d["description"]}
                                   for d in await list_digests(session, uid)])
    lines = (await session.execute(
        select(LedgerDigestLine).where(LedgerDigestLine.digest_id == digest.id, LedgerDigestLine.removed_at.is_(None))
        .order_by(LedgerDigestLine.position, LedgerDigestLine.id))).scalars().all()
    sources = await _sources(session, [l.id for l in lines])
    columns = await _columns(session, digest.id)
    review = [l for l in lines if l.needs_review]
    md = [f"# {digest.title}", "", digest.description, ""]

    def tag(line):
        mark = f" [line {line.id} r{line.revision}]" if with_ids else ""
        return mark + (f" ⚠ under review: {line.review_reason}" if line.needs_review else "")

    if digest.layout == "matrix":
        rows: dict[str, dict[str, LedgerDigestLine]] = {}
        for line in lines:
            rows.setdefault(line.row_label or "", {})[line.column_label or ""] = line
        md.append("| | " + " | ".join(_cell(c) for c in columns) + " |")
        md.append("| --- |" + " --- |" * len(columns))
        for row, cells in rows.items():
            out = []
            for column in columns:
                line = cells.get(column)
                out.append("" if line is None else _cell(f"{line.text} {_cite(sources.get(line.id, []))}{tag(line)}"))
            md.append(f"| {_cell(row)} | " + " | ".join(out) + " |")
    else:
        for line in lines:
            lead = f"**{line.row_label}**: " if line.row_label else ""
            md.append(f"- {lead}{line.text} {_cite(sources.get(line.id, []))}{tag(line)}")
    if review:
        md += ["", f"{len(review)} line(s) under review: whoever changed the entry they cite checks each one "
                   "(ledger_digest_edit: replace or resolve)."]
    result = {"slug": digest.slug, "title": digest.title, "layout": digest.layout, "markdown": "\n".join(md),
              "lines_under_review": len(review)}
    if with_ids:
        result["columns"] = columns
        result["lines"] = [
            {"line": l.id, "revision": l.revision, "row": l.row_label, "column": l.column_label,
             "position": l.position, "text": l.text, "sources": [s["ref"] for s in sources.get(l.id, [])],
             "needs_review": l.needs_review, "review_reason": l.review_reason}
            for l in lines
        ]
    return result


# ── Editing a summary view ───────────────────────────────────────────────────

@dataclass
class DigestEditOutcome:
    slug: str
    created: bool
    applied: list[dict]


async def _source_ids(session, uid, refs) -> dict[str, int]:
    refs = ledger.bounded_list(refs, "sources")
    if len(refs) > MAX_SOURCES:
        raise invalid(f"a line cites at most {MAX_SOURCES} entries.")
    found, missing = await ledger.find_entries(session, uid, refs)
    if missing:
        raise LedgerError("not_found", f"source entr{'y' if len(missing) == 1 else 'ies'} {', '.join(missing)} not "
                          "found. Nothing was written.")
    return {e.ref: e.id for e in found.values()}


async def _line(session, uid, digest: LedgerDigest, op: dict) -> LedgerDigestLine:
    line_id = op.get("line")
    if not isinstance(line_id, int) or isinstance(line_id, bool):
        raise invalid(f"{op.get('op')} needs line: the line id (ledger_digest with with_ids=true).")
    line = (await session.execute(select(LedgerDigestLine).where(
        _owner(LedgerDigestLine, uid), LedgerDigestLine.id == line_id, LedgerDigestLine.digest_id == digest.id
    ).with_for_update())).scalar_one_or_none()
    if line is None or line.removed_at is not None:
        raise LedgerError("not_found", f"line {line_id} is not a live line of {digest.slug}. Nothing was written.")
    expected = op.get("expected_revision")
    if expected is None:
        raise invalid(f"{op.get('op')} needs expected_revision: the line's revision you read.")
    if expected != line.revision:
        raise LedgerError("stale_revision", f"line {line_id} is at revision {line.revision}, not {expected}: it "
                          "changed since you read it. Read the view again. Nothing was written.",
                          current_revision=line.revision)
    return line


def _check_cell(digest: LedgerDigest, columns: list[str], row, column):
    if digest.layout == "matrix":
        row = one_line(row, "row", ROW_LABEL_MAX)
        if column not in columns:
            raise invalid(f"column must be one of {digest.slug}'s columns: {', '.join(columns) or 'none yet'} "
                          "(add one with add_column).", allowed=[{"name": c} for c in columns])
        return row, column
    if column is not None:
        raise invalid(f"{digest.slug} is a list; its lines have no column.")
    return (one_line(row, "row", ROW_LABEL_MAX) if row is not None else None), None


async def edit_digest(session: AsyncSession, uid: int | None, *, slug: str, reason: str, operations,
                      author=None) -> DigestEditOutcome:
    reason = ledger.clean_reason(reason)
    author = ledger.clean_author(author)
    who = ledger.created_by()
    slug = clean_slug(slug)
    if not isinstance(operations, list) or not operations or not all(isinstance(o, dict) for o in operations):
        raise invalid("operations must be a non-empty list of objects, each with op: create, describe, "
                      "add_column, add, replace, remove or resolve.")
    if len(operations) > MAX_OPERATIONS:
        raise invalid(f"at most {MAX_OPERATIONS} operations per call.")

    digest = await _digest(session, uid, slug, lock=True)
    created = False
    applied: list[dict] = []
    for index, op in enumerate(operations):
        kind = op.get("op")
        where = f"operations[{index}] ({kind})"
        try:
            if kind == "create":
                if digest is not None:
                    raise LedgerError("already_exists", f"summary view {slug} already exists.")
                layout = op.get("layout")
                if layout not in db.LEDGER_DIGEST_LAYOUTS:
                    raise invalid("layout is list or matrix.")
                digest = LedgerDigest(user_id=uid, slug=slug, layout=layout,
                                      title=one_line(op.get("title"), "title", DIGEST_TITLE_MAX),
                                      description=one_line(op.get("description"), "description",
                                                           DIGEST_DESCRIPTION_MAX))
                session.add(digest)
                await session.flush()
                session.add(LedgerRevision(user_id=uid, digest_id=digest.id, action="create", reason=reason,
                                           before=None, created_by=who, author=author))
                for position, label in enumerate(ledger.bounded_list(op.get("columns"), "columns")):
                    if layout != "matrix":
                        raise invalid("only a matrix has columns.")
                    session.add(LedgerDigestColumn(user_id=uid, digest_id=digest.id, position=position,
                                                   label=one_line(label, "column", COLUMN_LABEL_MAX)))
                await session.flush()
                created = True
                applied.append({"op": "create", "slug": slug})
                continue
            if digest is None:
                raise LedgerError("not_found", f"summary view {slug} not found; start with a create operation.",
                                  allowed=[{"name": d["slug"], "description": d["description"]}
                                           for d in await list_digests(session, uid)])
            columns = await _columns(session, digest.id)
            if kind == "describe":
                before = {"title": digest.title, "description": digest.description}
                if op.get("title") is not None:
                    digest.title = one_line(op.get("title"), "title", DIGEST_TITLE_MAX)
                if op.get("description") is not None:
                    digest.description = one_line(op.get("description"), "description", DIGEST_DESCRIPTION_MAX)
                session.add(LedgerRevision(user_id=digest.user_id, digest_id=digest.id, action="describe",
                                           reason=reason, before=before, created_by=who, author=author))
                digest.updated_at = func.now()
                applied.append({"op": "describe"})
            elif kind == "add_column":
                if digest.layout != "matrix":
                    raise invalid("only a matrix has columns.")
                label = one_line(op.get("label"), "label", COLUMN_LABEL_MAX)
                if label in columns:
                    raise LedgerError("already_exists", f"column {label!r} already exists.")
                session.add(LedgerRevision(user_id=digest.user_id, digest_id=digest.id, action="add_column",
                                           reason=reason, before={"columns": columns}, created_by=who, author=author))
                session.add(LedgerDigestColumn(user_id=digest.user_id, digest_id=digest.id, label=label,
                                               position=len(columns)))
                await session.flush()
                applied.append({"op": "add_column", "label": label})
            elif kind == "add":
                row, column = _check_cell(digest, columns, op.get("row"), op.get("column"))
                text_ = one_line(op.get("text"), "text", LINE_TEXT_MAX)
                if digest.layout == "matrix":
                    taken = (await session.execute(select(LedgerDigestLine.id).where(
                        LedgerDigestLine.digest_id == digest.id, LedgerDigestLine.removed_at.is_(None),
                        LedgerDigestLine.row_label == row, LedgerDigestLine.column_label == column))).first()
                    if taken is not None:
                        raise LedgerError("already_exists", f"cell ({row}, {column}) already holds line {taken[0]}; "
                                          "replace it instead.")
                sources = await _source_ids(session, uid, op.get("sources"))
                position = op.get("position")
                if position is None:
                    position = (await session.execute(select(func.coalesce(func.max(LedgerDigestLine.position), -1))
                                .where(LedgerDigestLine.digest_id == digest.id))).scalar_one() + 1
                elif not isinstance(position, int) or isinstance(position, bool):
                    raise invalid("position must be a whole number.")
                line = LedgerDigestLine(user_id=digest.user_id, digest_id=digest.id, row_label=row,
                                        column_label=column, position=position, text=text_)
                session.add(line)
                await session.flush()
                for entry_id in sorted(set(sources.values())):
                    session.add(LedgerDigestSource(user_id=digest.user_id, line_id=line.id, entry_id=entry_id))
                session.add(LedgerRevision(user_id=digest.user_id, line_id=line.id, action="create",
                                           reason=reason, before=None, created_by=who, author=author))
                applied.append({"op": "add", "line": line.id, "revision": 1})
            elif kind in ("replace", "remove", "resolve"):
                line = await _line(session, uid, digest, op)
                current = [r for r, in (await session.execute(
                    select(LedgerEntry.ref).join(LedgerDigestSource, LedgerDigestSource.entry_id == LedgerEntry.id)
                    .where(LedgerDigestSource.line_id == line.id))).all()]
                before = ledger.line_snapshot(line, sorted(current, key=ledger._ref_sort_key))
                if kind == "replace":
                    if op.get("text") is not None:
                        line.text = one_line(op.get("text"), "text", LINE_TEXT_MAX)
                    if op.get("row") is not None or op.get("column") is not None:
                        line.row_label, line.column_label = _check_cell(
                            digest, columns, op.get("row", line.row_label), op.get("column", line.column_label))
                    if op.get("position") is not None:
                        if not isinstance(op["position"], int) or isinstance(op["position"], bool):
                            raise invalid("position must be a whole number.")
                        line.position = op["position"]
                    if op.get("sources") is not None:
                        sources = await _source_ids(session, uid, op.get("sources"))
                        await session.execute(delete(LedgerDigestSource).where(LedgerDigestSource.line_id == line.id))
                        for entry_id in sorted(set(sources.values())):
                            session.add(LedgerDigestSource(user_id=line.user_id, line_id=line.id, entry_id=entry_id))
                    line.needs_review = False
                    line.review_reason = None
                elif kind == "remove":
                    line.removed_at = func.now()
                else:
                    if not line.needs_review:
                        raise invalid(f"line {line.id} is not under review; nothing to resolve.")
                    line.needs_review = False
                    line.review_reason = None
                session.add(LedgerRevision(user_id=line.user_id, line_id=line.id, revision=line.revision,
                                           action=kind, reason=reason, before=before, created_by=who, author=author))
                line.revision += 1
                line.updated_at = func.now()
                await session.flush()
                applied.append({"op": kind, "line": line.id, "revision": line.revision})
            else:
                raise invalid("op is create, describe, add_column, add, replace, remove or resolve.")
        except LedgerError as exc:
            raise LedgerError(exc.code, f"{where}: {exc} Nothing was written (operations apply all or none).",
                              allowed=exc.allowed, current_revision=exc.current_revision) from None
    await session.commit()
    return DigestEditOutcome(slug=slug, created=created, applied=applied)
