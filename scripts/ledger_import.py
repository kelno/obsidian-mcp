"""Import agent-ledger entries, topics and summary views from a JSON file.

    python -m scripts.ledger_import FILE (--user NAME | --single-user) [--dry-run] [--skip-area-check]

Generic on purpose: the data lives in the file named on the command line and
never in this repository (which is public). It runs wherever the server's
configuration is — inside the app container, a one-off job, a shell with
`DATABASE_URL` set — and reads only `DATABASE_URL` and the embedding
settings, so it assumes no deploy path.

**One transaction**: everything lands or nothing does. Refs, dates and
statuses are kept as given (`Q01` stays `Q01`); each kind's ref counter is
set to its highest number so a new entry never reuses one. Nothing is merged:
an entry, topic or view that already exists for the owner refuses the whole
import. Entries are stored unembedded; the indexer's embed stage sweeps them
in (within its budget), so a large import takes a few passes to become
searchable by meaning — by words at once.

`--dry-run` validates and writes everything, then rolls back, printing the
same summary: the rehearsal check. `--skip-area-check` accepts any well-formed
`area/<slug>` (for a rehearsal on a database with no indexed vault); without
it every area must be declared by an indexed main note, as the tools require.

Suggested order: rehearse on a throwaway database (`--skip-area-check`, then
the same without `--dry-run` and read the result back with
`scripts/ledger_export.py`); then `--dry-run` against the real database,
whose indexed vault must already declare every area the file names; then the
import itself. Imported refs keep their zero padding (`Q01`), and every tool
also finds that entry as `Q1`.

## The file

    {
      "topics": [{"slug": "voice", "kind": "surface", "description": "…", "area": "area/x"}],
      "entries": [{
        "ref": "Q01", "status": "current", "title": "…", "body": "…",
        "area": "area/agent-personnel", "topics": ["voice"],
        "details": {"certainty": "tested", "sources": "…"},
        "origin": "agent", "seen": true, "observed_on": "2026-10-02",
        "created_at": "2026-10-02T10:00:00+00:00", "updated_at": "…",
        "author": "…", "related": ["Q02"], "superseded_by": "Q03",
        "handled_account": "…", "handled_at": "…"
      }],
      "digests": [{
        "slug": "claude-surfaces", "title": "…", "description": "…", "layout": "matrix",
        "columns": ["text", "voice", "claude-code"],
        "lines": [{"row": "Images", "column": "voice", "text": "no", "sources": ["Q14"],
                   "review_reason": "only when a line is flagged for review"}]
      }]
    }

`kind` is taken from the ref's letter. `details` holds the kind's own fields
(see `ledger_add`). `seen` is for feedback only (default: true for origin
owner, false otherwise). Dates are ISO; `created_at`/`updated_at` default to
now, `observed_on` to `created_at`'s day. `scripts/ledger_export.py --format
json` writes this same shape.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database import async_session, engine
from src.models import db
from src.models.db import (
    LedgerDigest,
    LedgerDigestColumn,
    LedgerDigestLine,
    LedgerDigestSource,
    LedgerEntry,
    LedgerEntryLink,
    LedgerEntryTopic,
    LedgerRefCounter,
    LedgerTopic,
    User,
)
from src.services import ledger, ledger_views
from src.services.ledger import LedgerError

_AREA = ledger_views._SLUG  # `area/<slug>`: the slug part's pattern


class ImportError_(Exception):
    """A problem in the file; the message names the item."""


def _when(value, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ImportError_(f"{name}: {value!r} is not an ISO date-time") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _day(value, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ImportError_(f"{name}: {value!r} is not a YYYY-MM-DD date") from None


async def _owner(session, args) -> int | None:
    if args.single_user:
        return None
    user = (await session.execute(select(User).where(User.username == args.user))).scalar_one_or_none()
    if user is None:
        raise ImportError_(f"no user named {args.user!r}")
    return user.id


def _check_area(value, known: set[str] | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("area/") or not _AREA.match(value[5:]):
        raise ImportError_(f"{name}: area {value!r} is not of the form area/<slug>")
    if known is not None and value not in known:
        raise ImportError_(f"{name}: area {value} is declared by no indexed main note "
                           f"(known: {', '.join(sorted(known)) or 'none'}); --skip-area-check for a rehearsal")
    return value


async def run(args) -> dict:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"topics", "entries", "digests"}:
        raise ImportError_("the file must be an object with topics, entries and digests")
    counts: Counter = Counter()
    async with async_session() as session:
        uid = await _owner(session, args)
        known = None if args.skip_area_check else set((await ledger.area_index(session, uid)).known())
        okey = ledger.owner_key(uid)

        topic_ids: dict[str, int] = {}
        for i, t in enumerate(data.get("topics", [])):
            name = f"topics[{i}]"
            slug = ledger_views.clean_slug(t.get("slug"))
            if t.get("kind") not in db.LEDGER_TOPIC_KINDS:
                raise ImportError_(f"{name}: kind must be system or surface")
            topic = LedgerTopic(user_id=uid, slug=slug, kind=t["kind"],
                                description=ledger_views.one_line(t.get("description"), "description",
                                                                  ledger_views.TOPIC_DESCRIPTION_MAX),
                                area_tag=_check_area(t.get("area"), known, name))
            session.add(topic)
            await session.flush()
            topic_ids[slug] = topic.id
            counts["topics"] += 1

        entries: dict[tuple[str, int], LedgerEntry] = {}
        pending: list[tuple[LedgerEntry, dict]] = []
        now = datetime.now(timezone.utc)
        for i, e in enumerate(data.get("entries", [])):
            name = f"entries[{i}] ({e.get('ref')})"
            kind, number = ledger.parse_ref(e.get("ref"))
            if (kind, number) in entries:
                raise ImportError_(f"{name}: ref given twice")
            fields = ledger.validate_details(kind, e.get("details"), for_add=True)
            status = e.get("status")
            if status not in db.LEDGER_KIND_STATUSES[kind] + db.LEDGER_CLOSING_STATUSES:
                raise ImportError_(f"{name}: status {status!r} is not one a {kind} can have")
            origin = e.get("origin", "agent")
            seen = e.get("seen", origin == "owner") if kind == "feedback" else None
            created = _when(e.get("created_at"), f"{name}.created_at") or now
            entry = LedgerEntry(
                user_id=uid, kind=kind, number=number, ref=e["ref"].strip(), status=status, origin=origin,
                title=ledger.clean_title(e.get("title")), body=ledger.check_body(e.get("body") or ""),
                area_tag=_check_area(e.get("area"), known, name), seen=seen,
                observed_on=_day(e["observed_on"], f"{name}.observed_on") if e.get("observed_on") else created.date(),
                created_at=created, updated_at=_when(e.get("updated_at"), f"{name}.updated_at") or created,
                created_by=(e.get("created_by") or "import")[:255], author=ledger.clean_author(e.get("author")),
                handled_account=e.get("handled_account"),
                handled_at=_when(e.get("handled_at"), f"{name}.handled_at") or (created if status == "handled" else None),
                content_hash="", **fields,
            )
            entry.content_hash = ledger.content_hash(ledger.embed_text(entry))
            entries[(kind, number)] = entry
            pending.append((entry, e))
            counts[kind] += 1

        def resolve(ref, name):
            entry = entries.get(ledger.parse_ref(ref))
            if entry is None:
                raise ImportError_(f"{name}: {ref} is not in the file")
            return entry

        # A superseded entry names its replacement in the same row (a CHECK
        # ties the status to it), so each is inserted after the one it points to.
        waiting = list(pending)
        while waiting:
            ready = [(en, e) for en, e in waiting
                     if not e.get("superseded_by") or resolve(e["superseded_by"], f"entry {en.ref}").id is not None]
            if not ready:
                raise ImportError_("superseded_by forms a cycle among: " + ", ".join(en.ref for en, _ in waiting))
            for entry, e in ready:
                if e.get("superseded_by"):
                    entry.superseded_by = resolve(e["superseded_by"], f"entry {entry.ref}").id
                session.add(entry)
            await session.flush()
            waiting = [item for item in waiting if item not in ready]

        for entry, e in pending:
            name = f"entry {entry.ref}"
            for slug in e.get("topics", []):
                if slug not in topic_ids:
                    raise ImportError_(f"{name}: topic {slug} is not in the file")
                session.add(LedgerEntryTopic(user_id=uid, entry_id=entry.id, topic_id=topic_ids[slug]))
            for ref in e.get("related", []):
                other = resolve(ref, name)
                if other.id != entry.id:
                    low, high = sorted((entry.id, other.id))
                    await session.execute(pg_insert(LedgerEntryLink).values(
                        user_id=uid, entry_id=low, related_id=high).on_conflict_do_nothing())
        for kind in db.LEDGER_KINDS:
            highest = max((n for (k, n) in entries if k == kind), default=0)
            if highest:
                stmt = pg_insert(LedgerRefCounter).values(user_id=uid, kind=kind, last_number=highest)
                await session.execute(stmt.on_conflict_do_update(
                    index_elements=["owner_key", "kind"],
                    set_={"last_number": func.greatest(LedgerRefCounter.last_number, highest)}))

        for i, d in enumerate(data.get("digests", [])):
            name = f"digests[{i}]"
            layout = d.get("layout")
            if layout not in db.LEDGER_DIGEST_LAYOUTS:
                raise ImportError_(f"{name}: layout must be list or matrix")
            digest = LedgerDigest(user_id=uid, slug=ledger_views.clean_slug(d.get("slug")), layout=layout,
                                  title=ledger_views.one_line(d.get("title"), "title", ledger_views.DIGEST_TITLE_MAX),
                                  description=ledger_views.one_line(d.get("description"), "description",
                                                                    ledger_views.DIGEST_DESCRIPTION_MAX))
            session.add(digest)
            await session.flush()
            columns = d.get("columns", [])
            if layout == "list" and columns:
                raise ImportError_(f"{name}: a list has no columns")
            for position, label in enumerate(columns):
                session.add(LedgerDigestColumn(user_id=uid, digest_id=digest.id, label=label, position=position))
            await session.flush()
            for j, line_data in enumerate(d.get("lines", [])):
                lname = f"{name}.lines[{j}]"
                if layout == "matrix" and line_data.get("column") not in columns:
                    raise ImportError_(f"{lname}: column {line_data.get('column')!r} is not one of {columns}")
                line = LedgerDigestLine(
                    user_id=uid, digest_id=digest.id, row_label=line_data.get("row"),
                    column_label=line_data.get("column") if layout == "matrix" else None,
                    position=line_data.get("position", j),
                    text=ledger_views.one_line(line_data.get("text"), "text", ledger_views.LINE_TEXT_MAX),
                    needs_review=bool(line_data.get("review_reason")),
                    review_reason=line_data.get("review_reason") or None)
                session.add(line)
                await session.flush()
                for entry_id in sorted({resolve(r, lname).id for r in line_data.get("sources", [])}):
                    session.add(LedgerDigestSource(user_id=uid, line_id=line.id, entry_id=entry_id))
                counts["lines"] += 1
            counts["digests"] += 1
        await session.flush()
        counts["existing_entries_after"] = (await session.execute(
            select(func.count()).select_from(LedgerEntry).where(LedgerEntry.owner_key == okey))).scalar_one()
        if args.dry_run:
            await session.rollback()
        else:
            await session.commit()
    await engine.dispose()
    return dict(counts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Import agent-ledger data from a JSON file (one transaction).")
    parser.add_argument("file")
    owner = parser.add_mutually_exclusive_group(required=True)
    owner.add_argument("--user", help="the owner's username (multi-user mode)")
    owner.add_argument("--single-user", action="store_true", help="single-user mode (no users table owner)")
    parser.add_argument("--dry-run", action="store_true", help="validate and write, then roll everything back")
    parser.add_argument("--skip-area-check", action="store_true",
                        help="accept any well-formed area/<slug> (rehearsal without an indexed vault)")
    args = parser.parse_args(argv)
    try:
        counts = asyncio.run(run(args))
    except (ImportError_, LedgerError) as exc:
        print(f"ledger_import refused, nothing was written: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a database refusal (duplicate ref, constraint): named, nothing written
        print(f"ledger_import failed, nothing was written: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    verb = "would import (dry run, rolled back)" if args.dry_run else "imported"
    print(f"ledger_import {verb}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
