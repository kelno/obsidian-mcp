"""Export one owner's agent ledger, as Markdown or as the import's JSON.

    python -m scripts.ledger_export (--user NAME | --single-user) [--format markdown|json] [--history] [--out FILE]

For a direct look at the ledger without the tools, for leaving the server one
day (the ledger is not locked into this server), and for a copy before
anything is removed by hand. Read-only: one read transaction, nothing
written. Like the import, it reads only the server's own configuration
(`DATABASE_URL`), so it runs wherever that is set.

`--format json` writes the shape `scripts/ledger_import.py` reads, so an
export imports back as it was (refs, dates, statuses, topics, links, views);
the history is not part of that shape. `--history` adds every past version
to the Markdown.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

from src.database import async_session, engine
from src.models import db
from src.models.db import (
    LedgerDigest,
    LedgerDigestColumn,
    LedgerDigestLine,
    LedgerDigestSource,
    LedgerEntry,
    LedgerTopic,
    User,
)
from src.services import ledger, ledger_views


async def _owner(session, args) -> int | None:
    if args.single_user:
        return None
    user = (await session.execute(select(User).where(User.username == args.user))).scalar_one_or_none()
    if user is None:
        raise SystemExit(f"no user named {args.user!r}")
    return user.id


async def collect(args) -> dict:
    async with async_session() as session:
        uid = await _owner(session, args)
        okey = ledger.owner_key(uid)
        topics = (await session.execute(select(LedgerTopic).where(LedgerTopic.owner_key == okey)
                                        .order_by(LedgerTopic.slug))).scalars().all()
        topic_slugs = {t.id: t.slug for t in topics}
        entries = (await session.execute(select(LedgerEntry).where(LedgerEntry.owner_key == okey)
                                         .order_by(LedgerEntry.kind, LedgerEntry.number))).scalars().all()
        ids = [e.id for e in entries]
        refs = {e.id: e.ref for e in entries}
        entry_topics = await ledger.entry_topics(session, ids) if ids else {}
        related = await ledger.entry_related(session, ids) if ids else {}
        history = await ledger.history(session, ids) if (ids and args.history) else {}
        digests = (await session.execute(select(LedgerDigest).where(LedgerDigest.owner_key == okey)
                                         .order_by(LedgerDigest.slug))).scalars().all()
        views = []
        for d in digests:
            columns = (await session.execute(select(LedgerDigestColumn.label).where(
                LedgerDigestColumn.digest_id == d.id).order_by(LedgerDigestColumn.position))).scalars().all()
            lines = (await session.execute(select(LedgerDigestLine).where(
                LedgerDigestLine.digest_id == d.id, LedgerDigestLine.removed_at.is_(None))
                .order_by(LedgerDigestLine.position, LedgerDigestLine.id))).scalars().all()
            sources = {}
            for line in lines:
                sources[line.id] = sorted(
                    (refs[i] for i in (await session.execute(select(LedgerDigestSource.entry_id).where(
                        LedgerDigestSource.line_id == line.id))).scalars().all()),
                    key=ledger._ref_sort_key)
            rendered = await ledger_views.read_digest(session, uid, d.slug, with_ids=False)
            views.append({"digest": d, "columns": list(columns), "lines": lines, "sources": sources,
                          "markdown": rendered["markdown"]})
        # No rollback: it would expire the loaded rows. Closing the session
        # ends the read transaction and keeps them readable.
    await engine.dispose()
    return {"topics": topics, "topic_slugs": topic_slugs, "entries": entries, "refs": refs,
            "entry_topics": entry_topics, "related": related, "history": history, "views": views}


def as_json(data: dict) -> str:
    out = {"topics": [], "entries": [], "digests": []}
    for t in data["topics"]:
        if t.status == "merged":
            continue  # its entries already carry the topic it was merged into
        out["topics"].append({"slug": t.slug, "kind": t.kind, "description": t.description, "area": t.area_tag})
    for e in data["entries"]:
        item = {
            "ref": e.ref, "status": e.status, "title": e.title, "body": e.body, "area": e.area_tag,
            "topics": data["entry_topics"].get(e.id, []), "details": ledger.entry_fields(e), "origin": e.origin,
            "seen": e.seen, "observed_on": e.observed_on.isoformat(), "created_at": e.created_at.isoformat(),
            "updated_at": e.updated_at.isoformat(), "author": e.author, "created_by": e.created_by,
            "related": data["related"].get(e.id, []),
            "superseded_by": data["refs"].get(e.superseded_by), "handled_account": e.handled_account,
            "handled_at": e.handled_at.isoformat() if e.handled_at else None,
        }
        out["entries"].append({k: v for k, v in item.items() if v not in (None, [], {})})
    for v in data["views"]:
        d = v["digest"]
        out["digests"].append({
            "slug": d.slug, "title": d.title, "description": d.description, "layout": d.layout,
            "columns": v["columns"],
            "lines": [{k: val for k, val in {"row": l.row_label, "column": l.column_label, "position": l.position,
                                              "text": l.text, "sources": v["sources"][l.id]}.items()
                       if val is not None} for l in v["lines"]],
        })
    return json.dumps(out, ensure_ascii=False, indent=2)


def as_markdown(data: dict) -> str:
    md = ["# Agent ledger export", ""]
    md += ["## Topics", ""]
    for t in data["topics"]:
        merged = f", merged into {data['topic_slugs'].get(t.merged_into)}" if t.merged_into else ""
        area = f", {t.area_tag}" if t.area_tag else ""
        md.append(f"- **{t.slug}** ({t.kind}{area}{merged}): {t.description}")
    titles = {"quirk": "Quirks", "feedback": "Feedback", "proposal": "Proposals"}
    for kind in db.LEDGER_KINDS:
        md += ["", f"## {titles[kind]}"]
        for e in (e for e in data["entries"] if e.kind == kind):
            md += ["", f"### {e.ref} · {e.title}", ""]
            facts = [f"status {e.status}", f"observed {e.observed_on.isoformat()}",
                     f"revision {e.revision}", f"origin {e.origin}"]
            if e.area_tag:
                facts.append(e.area_tag)
            if e.seen is not None:
                facts.append("seen" if e.seen else "not seen")
            md.append("- " + " · ".join(facts))
            if data["entry_topics"].get(e.id):
                md.append("- topics: " + ", ".join(data["entry_topics"][e.id]))
            if data["related"].get(e.id):
                md.append("- related: " + ", ".join(data["related"][e.id]))
            for name, value in ledger.entry_fields(e).items():
                md.append(f"- {name}: {value}")
            if e.superseded_by:
                md.append(f"- superseded by {data['refs'].get(e.superseded_by)}")
            if e.handled_account:
                md.append(f"- handled ({e.handled_at.date().isoformat()}): {e.handled_account}")
            md.append(f"- by {e.created_by}" + (f" ({e.author})" if e.author else "")
                      + f", created {e.created_at.isoformat()}, updated {e.updated_at.isoformat()}")
            if e.body:
                md += ["", e.body]
            for h in data["history"].get(e.id, []):
                md += ["", f"> revision {h['revision']} → {h['action']} on {h['at']} by {h['by']}: {h['reason']}",
                       "> before: " + json.dumps(h["before"], ensure_ascii=False)]
    md += ["", "## Summary views"]
    for v in data["views"]:
        md += ["", v["markdown"].replace("\n# ", "\n### ").replace("# ", "### ", 1)]
    return "\n".join(md) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export one owner's agent ledger (read-only).")
    owner = parser.add_mutually_exclusive_group(required=True)
    owner.add_argument("--user", help="the owner's username (multi-user mode)")
    owner.add_argument("--single-user", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--history", action="store_true", help="Markdown only: every past version too")
    parser.add_argument("--out", help="write here instead of standard output")
    args = parser.parse_args(argv)
    data = asyncio.run(collect(args))
    rendered = as_json(data) if args.format == "json" else as_markdown(data)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
        print(f"ledger_export: {len(data['entries'])} entries, {len(data['views'])} views -> {args.out}")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
