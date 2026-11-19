"""`keyword_search`'s service: the conjunctive block, the disjunctive fill and
the page-bounded snippet (#344; design D2–D4, D8).

Three kinds of statement, all in the one transaction that opens with
`SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` and issued
`SET LOCAL random_page_cost = 1.1` — one snapshot for every statement, so a
concurrent index commit between the conjunctive and the disjunctive statement
cannot drop a note from both blocks (Codex C-1):

1. **The conjunctive statement** — `websearch_to_tsquery` per config OR'd,
   `@@`, the shared filters, `rank DESC, file_path ASC`, `LIMIT limit`. Its
   plan shape is the one `tests/integration/test_keyword_plan.py` pins; it
   gained `id` in the projection and `RANK_WEIGHTS` in the rank, nothing else.
   Rows are tagged `match: "all"`.
2. **The disjunctive statement**, only when the first under-fills the page and
   at least one config's parse is eligible (`fts.fallback_plan`): the same
   statement with the combined fallback tsquery, `AND id <> ALL(<the
   conjunctive block's ids>)`, `LIMIT limit - n`. The block was not full, so
   it holds every note the conjunctive query matches under the same filters
   in the same snapshot: excluding its ids excludes exactly what `AND NOT (v
   @@ <and query>)` did, without testing that query again on every row (it
   was 30-45 % of the statement on the owner's server, 2026-11-19). Rows are
   tagged `match: "any"` and appended after every `all` row; the two blocks
   are never interleaved (an `any` row's rank is against a different query).
3. **The snippet statements**, over the ids of the first
   `KEYWORD_SNIPPET_ROWS` rows of the assembled page and nothing else: one per
   block for `matched_in`, the per-config `matched_terms` counts, the first
   config's highlighted headline for the rows whose body part matched and the
   `head:` text for the rows whose body part did not; then, only for the rows
   whose body matched but whose headline held no highlight, one statement per
   further config for its headline, and one for the `head:` text of those
   still without one. Each headline is computed only where its result can be
   used: before 2026-11-19 every config's headline was computed for every
   row, and most were thrown away. Their cost is a function of the row cap,
   the excerpt cap and the config count — never of `limit` — and is recorded
   as `snippet_ms`.
"""
import time
from dataclasses import dataclass, field

from sqlalchemy import Integer, and_, case, cast, func, literal_column, or_, select, text, union_all
from sqlalchemy.dialects.postgresql import REGCONFIG, TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import bindparam

from src.config import settings
from src.models.db import NoteMetadata
from src.services import private_folders, timing
from src.services.filters import apply_note_filters
from src.services.fts import (
    HEAD_OPTIONS,
    HEADLINE_OPTIONS,
    HEADLINE_START,
    HEADLINE_STOP,
    FallbackPlan,
    strip_sentinels,
    best_matched_terms,
    combined_tsquery,
    fallback_plan,
    fallback_tsquery,
    matched_terms_units,
    rank_expression,
    tsquery_text_sql,
)

#: How the tool renders PostgreSQL's highlight sentinels.
RENDER_START = "«"
RENDER_STOP = "»"


async def _ranked_block(
    session, tsquery, *, folder, tags, frontmatter, user_id, limit, exclude_ids=None,
    with_excerpt_flag=False, private=None,
):
    """One ranking statement: the conjunctive one (`exclude_ids=None`) or the
    disjunctive one (`exclude_ids=<the conjunctive block's ids>`).

    `with_excerpt_flag` (hybrid only, #354 D1) adds one boolean column,
    `(body_excerpt IS NULL) AS no_excerpt`, to the projection — a flag, never
    the excerpt text. `full_text_search` leaves it off, so its statement (and
    the GIN plan `test_keyword_plan.py` pins) is byte-identical.
    """
    rank = rank_expression(NoteMetadata.content_tsvector, tsquery).label("rank")
    # Explicit projection (#280, design D6): `id` for the snippet statement
    # and the four columns the result renders. `ts_rank_cd` and `@@` still
    # read the tsvector server-side; what is never shipped is the vector, the
    # excerpt or `frontmatter`. Both are deferred with raiseload on the model.
    columns = [
        NoteMetadata.id,
        NoteMetadata.file_path,
        NoteMetadata.title,
        NoteMetadata.tags,
        rank,
    ]
    if with_excerpt_flag:
        columns.append(NoteMetadata.body_excerpt.is_(None).label("no_excerpt"))
    stmt = select(*columns).where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    if exclude_ids:
        # The conjunctive block was not full, so every row matching the
        # conjunctive query under these filters, in this snapshot, is already
        # in it: excluding its ids excludes exactly those, as `NOT (v @@ <and
        # query>)` did, without running that query again on every candidate.
        stmt = stmt.where(NoteMetadata.id.not_in(exclude_ids))
    stmt = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id, private=private
    )
    # `file_path` breaks rank ties deterministically. `ts_rank_cd` produces
    # plenty of them, and which tied row survived the LIMIT otherwise depended
    # on the plan — so the planner hint would have changed *results*, not just
    # speed. With the tie-break, membership and order are stable across plans.
    stmt = stmt.order_by(rank.desc(), NoteMetadata.file_path.asc()).limit(limit)
    return (await session.execute(stmt)).all()


async def _parsed_texts(session, query: str) -> list[str | None]:
    """The text form of each config's websearch parse, in config order. No
    table access; padded with None if the driver returns fewer rows."""
    cfg_params = {f"fts_cfg_{i}": cfg for i, cfg in enumerate(settings.fts_configs)}
    rows = (await session.execute(text(tsquery_text_sql()), {"q": query, **cfg_params})).all()
    parsed: list[str | None] = [None] * len(settings.fts_configs)
    for row in rows:
        try:
            ord_, value = row[0], row[1]
        except (TypeError, IndexError):
            continue
        if isinstance(ord_, int) and 0 <= ord_ < len(parsed):
            parsed[ord_] = value
    return parsed


def _part(column, weights: str):
    """`ts_filter(column, '<weights>')` — `weights` is one of the six fixed
    literals of `MATCHED_IN_FLAGS`, never caller input."""
    return func.ts_filter(column, literal_column(f"'{weights}'"))


def _a_part(column):
    return _part(column, "{a}")


def _d_part(column):
    return _part(column, "{d}")


#: The six `matched_in` flags the snippet statement projects (#391, design
#: D4): `ts_filter(v, W) @@ q` per label. `{a}` is the title or aliases,
#: `{b}` the tags, `{d}` the body.
MATCHED_IN_FLAGS = (
    ("matched_title", "{a}"),
    ("matched_tags", "{b}"),
    ("matched_body", "{d}"),
    ("matched_title_tags", "{a,b}"),
    ("matched_title_body", "{a,d}"),
    ("matched_tags_body", "{b,d}"),
)

_SINGLE_PARTS = (
    ("matched_title", "title"),
    ("matched_tags", "tags"),
    ("matched_body", "body"),
)
_PAIR_PARTS = (
    ("matched_title_tags", "title+tags"),
    ("matched_title_body", "title+body"),
    ("matched_tags_body", "tags+body"),
)


def matched_in_from_flags(flags) -> list[str]:
    """`matched_in` from the six flags (design D4): every single part whose
    flag is true, in the order `title`, `tags`, `body` (rendered
    comma-joined); when none is, the first true pair in the order
    `title+tags`, `title+body`, `tags+body`; when none is, `title+tags+body`.
    Every id handed to the snippet statement matched the block's query over
    the whole vector, so "no part and no pair" means the match needs all
    three parts. `flags` is any mapping; a missing flag reads as false."""
    singles = [name for key, name in _SINGLE_PARTS if flags.get(key)]
    if singles:
        return singles
    for key, name in _PAIR_PARTS:
        if flags.get(key):
            return [name]
    return ["title+tags+body"]


def _headline(cfg_param: str, cfg: str, tsquery, opts_param: str, opts: str):
    """`ts_headline(<config>, body_excerpt, <query>, <options>)`, the config
    and the options as bound parameters."""
    return func.ts_headline(
        cast(bindparam(cfg_param, value=cfg), REGCONFIG),
        NoteMetadata.body_excerpt,
        tsquery,
        bindparam(opts_param, value=opts),
    )


def _rendered_snippet(hl) -> str | None:
    """A headline as the result shows it, or None when it holds no highlight
    (with no full cover `ts_headline` may mark nothing)."""
    if hl and HEADLINE_START in hl:
        return hl.replace(HEADLINE_START, RENDER_START).replace(HEADLINE_STOP, RENDER_STOP)
    return None


async def _snippets(session, ids: list[int], tsquery, plan: FallbackPlan | None) -> dict:
    """Headlines, `matched_in` and per-config `matched_terms` for `ids`.

    Returns `{id: {"snippet", "head", "matched_in", "matched_terms",
    "has_excerpt"}}`. `body_excerpt` is read only inside `ts_headline`; the
    headline is what is projected.

    A row's snippet is the first config's headline (in `fts_configs` order)
    that holds a highlight, and only when the body part matched; a row with an
    excerpt and no snippet gets the `head:` text instead (design D4). Each
    headline is computed only where that rule can use it (2026-11-19, the
    result unchanged): the first statement computes the first config's
    headline for the rows whose body matched and the head for the rows whose
    body did not; then, only for the rows whose body matched with no
    highlight yet, one statement per further config, and finally one for the
    head of those still without a snippet. Before, every config's headline was
    computed for every row and a separate statement computed the heads.

    `head` is `None` when no head was computed (a snippet was shown, or the
    excerpt is NULL) and a string when it was — the **empty string** for a
    note whose stored excerpt is empty (no body, #391 Codex r1 m1): the
    renderer shows no preview line for it and does not count it as a row
    without an excerpt, and tells the cases apart by `is None`, never by
    truthiness.
    """
    if not ids:
        return {}
    v = NoteMetadata.content_tsvector
    configs = settings.fts_configs
    body_matched = _d_part(v).op("@@")(tsquery)
    columns = [NoteMetadata.id.label("id")]
    for label, weights in MATCHED_IN_FLAGS:
        columns.append(_part(v, weights).op("@@")(tsquery).label(label))
    columns.append((NoteMetadata.body_excerpt.is_not(None)).label("has_excerpt"))
    # A headline is the matching span only when the body part itself matched
    # the block's query: with no full cover `ts_headline` still marks single
    # query words, so a title-only or split match would otherwise render a
    # body "highlight" that is not the match (V-B1). A NULL flag reads as no
    # match, as the flag column does in Python.
    columns.append(case(
        (body_matched, _headline("hl_cfg_0", configs[0], tsquery, "hl_opts_0", HEADLINE_OPTIONS)),
        else_=None,
    ).label("hl_0"))
    columns.append(case(
        (body_matched, None),
        (NoteMetadata.body_excerpt.is_not(None),
         _headline("head_cfg", configs[0], tsquery, "head_opts", HEAD_OPTIONS)),
        else_=None,
    ).label("head"))
    units_by_config = matched_terms_units(plan) if plan is not None else []
    for i, units in units_by_config:
        total = None
        for j, unit in enumerate(units):
            term = cast(
                v.op("@@")(cast(bindparam(f"mt_{i}_{j}", value=unit), TSQUERY)),
                Integer,
            )
            total = term if total is None else total + term
        columns.append(total.label(f"mt_{i}"))
    stmt = select(*columns).where(NoteMetadata.id.in_(ids))
    rows = (await session.execute(stmt)).all()

    out: dict = {}
    # The rows whose body matched and that have no snippet yet: only they can
    # use a further config's headline.
    unhighlighted: list[int] = []
    for row in rows:
        m = row._mapping
        snippet = _rendered_snippet(m.get("hl_0")) if m.get("matched_body") else None
        # Six flags since #391 (design D4): `tags` joins `title` and `body`
        # as a part, and a split match names its first minimal pair (V-N5).
        matched_in = matched_in_from_flags(m)
        pairs = [
            (int(m.get(f"mt_{i}") or 0), len(units)) for i, units in units_by_config
        ]
        has_excerpt = bool(m.get("has_excerpt"))
        head = None
        if snippet is None and has_excerpt and not m.get("matched_body"):
            # Same sentinels as the fragment mode (`HEAD_OPTIONS`); the head
            # is never highlighted, so they are removed, not rendered.
            # PostgreSQL returns '' for an empty excerpt; a NULL here is read
            # as the empty headline too (the row had an excerpt).
            head = "" if m["head"] is None else strip_sentinels(m["head"])
        out[m["id"]] = {
            "snippet": snippet,
            "head": head,
            "matched_in": matched_in,
            "matched_terms": best_matched_terms(pairs),
            "has_excerpt": has_excerpt,
        }
        if snippet is None and m.get("matched_body"):
            unhighlighted.append(m["id"])

    for i in range(1, len(configs)):
        if not unhighlighted:
            break
        hl_stmt = select(
            NoteMetadata.id.label("id"),
            _headline(f"hl_cfg_{i}", configs[i], tsquery, f"hl_opts_{i}", HEADLINE_OPTIONS).label("hl"),
        ).where(NoteMetadata.id.in_(unhighlighted))
        for row in (await session.execute(hl_stmt)).all():
            m = row._mapping
            snippet = _rendered_snippet(m["hl"])
            if snippet is not None and m["id"] in out:
                out[m["id"]]["snippet"] = snippet
        unhighlighted = [i_ for i_ in unhighlighted if out[i_]["snippet"] is None]

    need_head = [i_ for i_ in unhighlighted if out[i_]["has_excerpt"]]
    if need_head:
        head_stmt = select(
            NoteMetadata.id.label("id"),
            _headline("head_cfg", configs[0], tsquery, "head_opts", HEAD_OPTIONS).label("head"),
        ).where(NoteMetadata.id.in_(need_head))
        for row in (await session.execute(head_stmt)).all():
            m = row._mapping
            if m["id"] in out:
                out[m["id"]]["head"] = "" if m["head"] is None else strip_sentinels(m["head"])
    return out


def _row_dict(row, match: str, with_excerpt_flag: bool = False) -> dict:
    out = {
        "id": row.id,
        "path": row.file_path,
        "title": row.title,
        "tags": row.tags,
        "rank": float(row.rank),
        "match": match,
    }
    if with_excerpt_flag:
        out["no_excerpt"] = bool(row.no_excerpt)
    return out


_NO_SNIPPET = {
    "snippet": None, "head": None, "matched_in": [],
    "matched_terms": None, "has_excerpt": False,
}


@dataclass
class KeywordArm:
    """The keyword ranking without its transaction preamble, snippets or
    telemetry (#354 D1): the assembled `all`-then-`any` rows plus what the
    snippet statements and the telemetry need."""

    rows: list[dict] = field(default_factory=list)
    and_query: object = None
    any_query: object = None
    plan: FallbackPlan | None = None
    fallback: str = "not_needed"


async def keyword_arm_rows(
    session,
    query: str,
    *,
    limit: int,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
    with_excerpt_flag: bool = False,
    private: str | None = None,
) -> KeywordArm:
    """The conjunctive block, PostgreSQL's parse and the disjunctive fill, in
    that order — the statements `full_text_search` has always issued, and
    nothing else: no `SET TRANSACTION`, no `SET LOCAL`, no telemetry. The
    caller owns the transaction (one snapshot for every statement, Codex
    C-1) and the hint."""
    and_query = combined_tsquery(query)
    filters = dict(folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id, private=private)
    all_rows = await _ranked_block(
        session, and_query, limit=limit, with_excerpt_flag=with_excerpt_flag,
        **filters,
    )
    results = [_row_dict(r, "all", with_excerpt_flag) for r in all_rows]

    snippet_rows = settings.keyword_snippet_rows
    plan: FallbackPlan | None = None
    fallback = "not_needed"
    any_query = None
    if len(results) < limit or snippet_rows > 0:
        # PostgreSQL's parse of the caller's query, never the caller's string
        # (design D3). Needed for the fallback and for `matched_terms`.
        plan = fallback_plan(await _parsed_texts(session, query))
    if len(results) < limit:
        if plan is not None and plan.eligible:
            any_query = fallback_tsquery(plan, query)
            any_rows = await _ranked_block(
                session, any_query, limit=limit - len(results),
                exclude_ids=[r.id for r in all_rows],
                with_excerpt_flag=with_excerpt_flag, **filters,
            )
            results.extend(_row_dict(r, "any", with_excerpt_flag) for r in any_rows)
            fallback = "ran"
        else:
            fallback = "skipped"
    return KeywordArm(
        rows=results, and_query=and_query, any_query=any_query, plan=plan,
        fallback=fallback,
    )


async def _ranked_block_split(
    session, tsquery, *, folder, tags, frontmatter, user_id, limit_all, limit_public,
    exclude_ids=None, with_excerpt_flag=False,
):
    """`_ranked_block` for two arms in one statement (fork, P98): the block
    without the private filter, cut at `limit_all`, and the block with it
    (`private="exclude"`), cut at `limit_public`. Returns the two lists of
    rows, each in its own block's order.

    The matching rows, their rank and their `public` flag are computed once,
    in a materialized CTE; each block is then that CTE ordered and cut as
    `_ranked_block` orders and cuts it (`rank DESC, file_path ASC`, a total
    order: `file_path` is unique within the owner scope every statement is
    held to), the filtered one over the rows whose flag is true. `public` is
    the very predicate `apply_note_filters(private="exclude")` puts in the
    WHERE clause, projected instead of applied, so the database decides it as
    it decided the filter, and the order with its collation tie-break is the
    database's too. Each block is therefore the rows its own statement would
    have returned, in the same order; the rank (detoasting every matching
    vector) is paid once instead of twice."""
    rank = rank_expression(NoteMetadata.content_tsvector, tsquery).label("rank")
    public = (~private_folders.sql_predicate(NoteMetadata.file_path)).label("public")
    columns = [
        NoteMetadata.id,
        NoteMetadata.file_path,
        NoteMetadata.title,
        NoteMetadata.tags,
        rank,
        public,
    ]
    if with_excerpt_flag:
        columns.append(NoteMetadata.body_excerpt.is_(None).label("no_excerpt"))
    stmt = select(*columns).where(NoteMetadata.content_tsvector.op("@@")(tsquery))
    if exclude_ids:
        # As in `_ranked_block`: the unfiltered conjunctive block was not
        # full, so its ids are every conjunctive match under these filters,
        # private or not, which is what both blocks' fills leave out.
        stmt = stmt.where(NoteMetadata.id.not_in(exclude_ids))
    matches = apply_note_filters(
        stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id
    ).cte("matches").prefix_with("MATERIALIZED", dialect="postgresql")
    order = (matches.c.rank.desc(), matches.c.file_path.asc())
    unfiltered = (
        select(*matches.c, literal_column("0").label("block")).order_by(*order).limit(limit_all)
    )
    filtered = (
        select(*matches.c, literal_column("1").label("block"))
        .where(matches.c.public).order_by(*order).limit(limit_public)
    )
    both = union_all(unfiltered, filtered).subquery("blocks")
    stmt = select(*both.c).order_by(both.c.block, both.c.rank.desc(), both.c.file_path.asc())
    rows = (await session.execute(stmt)).all()
    return [r for r in rows if r.block == 0], [r for r in rows if r.block == 1]


async def keyword_arm_rows_split(
    session,
    query: str,
    *,
    limit: int,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
    with_excerpt_flag: bool = False,
) -> tuple[KeywordArm, list[str]]:
    """`hybrid_search`'s keyword arm with private notes left out, and the
    paths of the same arm unfiltered, for its count (fork, P98): exactly
    `keyword_arm_rows(..., private="exclude")` and the paths of
    `keyword_arm_rows(...)`, from one ranking statement per block instead of
    one per block and arm. Only with private folders configured.

    The conjunctive block is one `_ranked_block_split`. The parse runs when
    the filtered arm needs it (the unfiltered block is never shorter, so it
    needs it only then too). The disjunctive fill runs as in each arm alone:
    when the unfiltered block is short too, one split statement serves both,
    each with its own remaining limit; when only the filtered block is short,
    the filtered fill runs alone, `_ranked_block` as `keyword_arm_rows` runs
    it. The exclusion of conjunctive matches is the same in both, since
    neither arm runs the fill unless its conjunctive block held every match
    it could: the split fill leaves out the unfiltered block's ids (every
    conjunctive match, private ones included), the filtered fill alone the
    filtered block's (every public one)."""
    and_query = combined_tsquery(query)
    filters = dict(folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id)
    all_unfiltered, all_public = await _ranked_block_split(
        session, and_query, limit_all=limit, limit_public=limit,
        with_excerpt_flag=with_excerpt_flag, **filters,
    )
    results = [_row_dict(r, "all", with_excerpt_flag) for r in all_public]
    unfiltered = [r.file_path for r in all_unfiltered]

    snippet_rows = settings.keyword_snippet_rows
    plan: FallbackPlan | None = None
    fallback = "not_needed"
    any_query = None
    if len(results) < limit or snippet_rows > 0:
        plan = fallback_plan(await _parsed_texts(session, query))
    if len(results) < limit:
        if plan is not None and plan.eligible:
            any_query = fallback_tsquery(plan, query)
            if len(unfiltered) < limit:
                any_unfiltered, any_public = await _ranked_block_split(
                    session, any_query, limit_all=limit - len(unfiltered),
                    limit_public=limit - len(results),
                    exclude_ids=[r.id for r in all_unfiltered],
                    with_excerpt_flag=with_excerpt_flag, **filters,
                )
                unfiltered.extend(r.file_path for r in any_unfiltered)
            else:
                any_public = await _ranked_block(
                    session, any_query, limit=limit - len(results),
                    exclude_ids=[r.id for r in all_public],
                    with_excerpt_flag=with_excerpt_flag, private="exclude", **filters,
                )
            results.extend(_row_dict(r, "any", with_excerpt_flag) for r in any_public)
            fallback = "ran"
        else:
            fallback = "skipped"
    arm = KeywordArm(
        rows=results, and_query=and_query, any_query=any_query, plan=plan,
        fallback=fallback,
    )
    return arm, unfiltered


async def keyword_snippets(session, rows: list[dict], arm: KeywordArm) -> None:
    """The per-block snippet statements for `rows` (which the caller has
    already cut to its snippet cap), applied in place. The `all` block's
    statements first, then the `any` block's — `full_text_search`'s order."""
    details: dict = {}
    all_ids = [r["id"] for r in rows if r["match"] == "all"]
    any_ids = [r["id"] for r in rows if r["match"] == "any"]
    if all_ids:
        details.update(await _snippets(session, all_ids, arm.and_query, arm.plan))
    if any_ids and arm.any_query is not None:
        details.update(await _snippets(session, any_ids, arm.any_query, arm.plan))
    for r in rows:
        r.update(details.get(r["id"], dict(_NO_SNIPPET, matched_in=[])))


async def _count_private_hidden(session, arm: KeywordArm, *, limit, folder, tags, frontmatter, user_id) -> int:
    """How many private matches would have been on this page (fork, P98):
    every one when the page is not full, else those ranked ahead of the last
    result shown, in the page's own order (the `all` block, then the `any`
    block; rank, then path within a block); at most `limit`."""

    async def count(tsquery, *, exclude=None, after=None) -> int:
        stmt = select(func.count()).select_from(NoteMetadata).where(
            NoteMetadata.content_tsvector.op("@@")(tsquery))
        if exclude is not None:
            stmt = stmt.where(~NoteMetadata.content_tsvector.op("@@")(exclude))
        if after is not None:
            rank = rank_expression(NoteMetadata.content_tsvector, tsquery)
            stmt = stmt.where(or_(rank > after["rank"], and_(
                rank == after["rank"], NoteMetadata.file_path < after["path"])))
        stmt = apply_note_filters(
            stmt, folder=folder, tags=tags, frontmatter=frontmatter, user_id=user_id, private="only")
        return int((await session.execute(stmt)).scalar_one())

    rows = arm.rows
    full = len(rows) >= limit
    last = rows[-1] if full else None
    if last is not None and last["match"] == "all":
        hidden = await count(arm.and_query, after=last)
    else:
        hidden = await count(arm.and_query)
        if arm.any_query is not None:
            hidden += await count(arm.any_query, exclude=arm.and_query, after=last)
    return min(hidden, limit)


async def full_text_search(
    session: AsyncSession,
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
    hide_private: bool = False,
    counts: dict | None = None,
) -> list[dict]:
    """Full-text search over notes_metadata using tsvector.

    The tsquery is built from `settings.fts_configs` (see `src/services/fts.py`)
    so query-time configs match the index-time configs in `indexer.py`. Each
    returned dict carries `path`, `title`, `tags`, `rank`, `match`
    (`"all"` | `"any"`), and — for the first `KEYWORD_SNIPPET_ROWS` rows —
    `snippet`, `head`, `matched_in`, `matched_terms` and `has_excerpt`.

    The preamble, then `keyword_arm_rows`, then `keyword_snippets`, then the
    telemetry: the statements and their order are what they were before the
    #354 extraction, which `hybrid_search` reuses inside its own transaction.

    `hide_private` (with private folders configured) leaves notes under a
    private folder out inside the query, so the limit is still filled, and
    sets `counts["private_hidden"]` to the number of private matches that
    would have been among these results: every one when the results did not
    fill the limit, else those ranked ahead of the last result shown; at
    most `limit`, as for the vector searches.
    """
    limit = max(1, min(limit, 500))

    # The same transaction-scoped hint the vector path uses, for the same
    # reason: the planner's cost model does not see detoast I/O. It costs the
    # `notes_metadata` heap at `relpages` (~481) and happily picks a Seq Scan,
    # which then detoasts every one of ~3,800 tsvectors out of a 36 MB TOAST
    # table — 13,086 buffers per query. Lowering `random_page_cost` flips it to
    # a GIN bitmap scan: 1,146 buffers, 6–9 ms warm instead of 26–37 ms.
    # Lifetime counters before this: 5 index scans vs 3,655 sequential ones.
    # SET LOCAL scopes to this transaction only — the database is shared with
    # other tenants, so no global setting is touched. Every statement of this
    # call runs on this session, inside this transaction.
    #
    # And under one snapshot (Codex C-1): the disjunctive statement excludes
    # `v @@ <and query>` on the premise that every such row is already in the
    # conjunctive block, which holds only if both statements see the same
    # data. At READ COMMITTED an index commit between them can drop a note
    # from both blocks. `SET TRANSACTION` must precede the first statement that
    # takes a snapshot; `SET LOCAL` takes none, and the caller hands over a
    # session with no statement run (`search_notes_impl`). READ ONLY: this
    # path never writes, and a read-only REPEATABLE READ transaction cannot
    # raise a serialization failure.
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )
    await session.execute(text("SET LOCAL random_page_cost = 1.1"))

    private = "exclude" if hide_private and private_folders.enabled() else None
    arm = await keyword_arm_rows(
        session, query, limit=limit, folder=folder, tags=tags,
        frontmatter=frontmatter, user_id=user_id, private=private,
    )
    results = arm.rows
    if private is not None and counts is not None:
        counts["private_hidden"] = await _count_private_hidden(
            session, arm, limit=limit, folder=folder, tags=tags, frontmatter=frontmatter,
            user_id=user_id,
        )

    snippet_rows = settings.keyword_snippet_rows
    if snippet_rows > 0:
        started = time.monotonic()
        await keyword_snippets(session, results[:snippet_rows], arm)
        timing.add_ms("snippet_ms", time.monotonic() - started)

    for r in results:
        r.pop("id", None)
    # Result telemetry, recorded where the result set is final (#161). The
    # bounds live in `timing.record_results`, which is the record site the
    # `_tracked` merge cannot truncate — see `src/services/timing.py`. Outside
    # a tracked tool call (the panel, the benchmarks) there is no holder and
    # this is a no-op.
    timing.record("fallback", arm.fallback)
    timing.record("any_rows", sum(1 for r in results if r["match"] == "any"))
    timing.record_results(r["path"] for r in results)
    return results
