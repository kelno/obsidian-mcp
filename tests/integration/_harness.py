"""Throwaway-pgvector harness shared by the opt-in integration modules.

Extracted from `tests/integration/test_pgvector_search.py`, which keeps its own
copy so that module stays self-contained. Same contract:

`PGVECTOR_TEST_ADMIN_URL` names a **server** to create a throwaway database on.
The harness never touches the database in the URL itself beyond using it as a
maintenance connection: it `CREATE DATABASE test_<something>_<uuid>`, migrates
*that*, hands it to the test, and drops it in teardown — so a mistyped URL
cannot cost data. Pointing it at the production database name is a hard
failure, not a skip.

    docker run --rm -d --name pgvector-search-test -e POSTGRES_PASSWORD=test \\
        -p 55433:5432 pgvector/pgvector:pg16
    PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@localhost:55433/postgres \\
        pytest -q tests/integration/
    docker rm -f pgvector-search-test

Note the harness lets a module choose `EMBEDDING_DIMENSIONS` for the migration
subprocess. The benchmarks need thousands of vectors for the planner to prefer
the HNSW index at all; at the production 1024 dims that is minutes of insert
time for no extra coverage, since none of the code under test reads the width.
"""
import asyncio
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import pytest
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

PGVECTOR_TEST_ADMIN_URL = os.environ.get("PGVECTOR_TEST_ADMIN_URL")

# The production database name. Refusing it is a backstop against someone
# exporting the live URL here: the harness would otherwise open a maintenance
# connection to it (and, on a bad edit, run migrations against it).
FORBIDDEN_DB_NAMES = {"obsidian_mcp"}

ROOT = Path(__file__).resolve().parent.parent.parent

requires_pgvector = pytest.mark.skipif(
    not PGVECTOR_TEST_ADMIN_URL,
    reason="set PGVECTOR_TEST_ADMIN_URL to run pgvector integration tests",
)


class Explain(Executable, ClauseElement):
    """`EXPLAIN <statement>`, with the statement's parameters bound normally.

    The obvious alternative — compiling with `literal_binds` and interpolating
    into `text("EXPLAIN …")` — cannot render every type the production
    statements carry: a `frontmatter @> :jsonb` filter has no literal renderer
    and raises `CompileError`. Silently skipping that shape would leave the one
    filter whose plan nobody had ever looked at unasserted, which is the exact
    failure mode these EXPLAIN assertions exist to prevent. Going through the
    driver instead lets JSONB, `vector`, and arrays bind the way they do in
    production.
    """

    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain, "postgresql")
def _compile_explain(element, compiler, **kw):  # pragma: no cover - via tests
    return "EXPLAIN " + compiler.process(element.statement, **kw)


async def explain(session, statement) -> str:
    """The plan for `statement`, as one string, on `session`'s transaction."""
    rows = (await session.execute(Explain(statement))).fetchall()
    return "\n".join(row[0] for row in rows)


def _with_database(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URL → a DSN asyncpg.connect() accepts."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(scheme=parts.scheme.split("+", 1)[0]))


def _admin_database_name(url: str) -> str:
    """The database an admin URL points at, normalized for comparison.

    A URL path is percent-encoded (`/obsidian%5Fmcp`) and Postgres identifiers
    fold case, so the raw path is not what the server will resolve.
    """
    return unquote(urlsplit(url).path).lstrip("/").casefold()


async def _run_maintenance(admin_url: str, statement: str) -> None:
    """Run a CREATE/DROP DATABASE on the admin URL's own database.

    asyncpg is autocommit outside an explicit transaction, which is what
    CREATE/DROP DATABASE requires.
    """
    import asyncpg

    conn = await asyncpg.connect(asyncpg_dsn(admin_url))
    try:
        await conn.execute(statement)
    finally:
        await conn.close()


def run_alembic(
    url: str,
    *args: str,
    dimensions: int = 64,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run `python -m alembic <args>` against `url` in a subprocess.

    A subprocess rather than `alembic.command`: `alembic/env.py` reads
    `DATABASE_URL` from the environment and drives its own event loop, neither
    of which survives being called from inside a test process that already has
    `src.config` imported with different settings.

    `check=False` returns the failed result instead of asserting, for the cases
    that assert on *how* a migration fails.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "DATABASE_URL": url,
            "EMBEDDING_DIMENSIONS": str(dimensions),
            # Several offline test modules `os.environ.setdefault(
            # "SECRET_KEY", "test")` at import time, and that sticks for the
            # whole process. `Settings` rejects placeholder secrets, so the
            # subprocess would fail to import `src.config` depending on which
            # modules pytest collected first. Give it a real one.
            "SECRET_KEY": secrets.token_hex(32),
            "EMBEDDING_ALLOW_PLAINTEXT": "true",  # #185: default OLLAMA_URL is plaintext
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    if check:
        assert result.returncode == 0, (
            f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
        )
    return result


# Migrated templates, one per (revision, dimensions), built at most once per
# process and copied with CREATE DATABASE … TEMPLATE for each throwaway
# database. A copy is the template's catalogue byte for byte, so a test gets
# exactly the schema a fresh `alembic upgrade` produced earlier in the same run;
# what it no longer gets is its *own* fresh run of the migrations. The tests
# whose subject is the migration run itself say `fresh=True`, and
# `OMCP_TEST_NO_DB_TEMPLATE=1` turns the cache off for the whole run.
#
# The names carry a per-process id, so pytest-xdist workers and two concurrent
# runs against the same server never share or drop each other's templates.
# `drop_templates` drops them at the end of the session (tests/conftest.py
# calls it from `pytest_sessionfinish`, which every xdist worker runs; an
# atexit hook left every worker's templates behind when tried). A killed
# run leaves `tmpl_*` databases behind, which the throwaway server's removal
# takes with it.
_TEMPLATES: dict[tuple[str, int], str] = {}
_PROCESS_ID = uuid.uuid4().hex[:12]
_USE_TEMPLATES = os.environ.get("OMCP_TEST_NO_DB_TEMPLATE", "") not in ("1", "true")


def drop_templates() -> None:
    """Drop every template this process built. Best effort, like teardown."""
    names = list(_TEMPLATES.values())
    _TEMPLATES.clear()
    for name in names:
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL, f'DROP DATABASE IF EXISTS "{name}" (FORCE)'
                )
            )
        except Exception as e:
            print(f"warning: could not drop template database {name}: {e}")


def _template_for(revision: str, dimensions: int) -> str:
    """The name of a database migrated to `revision`, built on first use.

    Nothing ever connects to it after the migration subprocess exits, which is
    what CREATE DATABASE … TEMPLATE requires of its source.
    """
    key = (revision, dimensions)
    name = _TEMPLATES.get(key)
    if name is not None:
        return name
    name = f"tmpl_{_PROCESS_ID}_{revision}_{dimensions}"
    try:
        asyncio.run(
            _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{name}"')
        )
        run_alembic(
            _with_database(PGVECTOR_TEST_ADMIN_URL, name),
            "upgrade",
            revision,
            dimensions=dimensions,
        )
    except BaseException:
        asyncio.run(
            _run_maintenance(
                PGVECTOR_TEST_ADMIN_URL, f'DROP DATABASE IF EXISTS "{name}" (FORCE)'
            )
        )
        raise
    _TEMPLATES[key] = name
    return name


def throwaway_database(
    prefix: str, dimensions: int, revision: str = "head", *, fresh: bool = False
):
    """Generator body for a module-scoped fixture: yields a migrated URL.

    `revision` is the alembic target — `head` for the usual "give me the
    current schema", or an explicit revision for tests that need to stand a
    database up at an older shape and migrate it forward themselves.

    The database is a copy of a template migrated once per process (see
    `_TEMPLATES`). `fresh=True` runs the migrations into this database itself,
    for a test whose subject is that run.
    """
    admin_db = _admin_database_name(PGVECTOR_TEST_ADMIN_URL)
    if admin_db in FORBIDDEN_DB_NAMES:
        pytest.fail(
            "PGVECTOR_TEST_ADMIN_URL points at the production database name "
            f"{admin_db!r}. Point it at a throwaway server (see the module "
            "docstring); this harness creates and drops databases."
        )

    dbname = f"test_{prefix}_{uuid.uuid4().hex}"
    from_template = _USE_TEMPLATES and not fresh
    try:
        # CREATE sits inside the try so the DROP below runs even if creation is
        # interrupted: the name is generated, `IF EXISTS` makes the drop a
        # no-op when it never got made, and leaving a stray database behind is
        # the one outcome this harness must not have.
        if from_template:
            template = _template_for(revision, dimensions)
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'CREATE DATABASE "{dbname}" TEMPLATE "{template}"',
                )
            )
        else:
            asyncio.run(
                _run_maintenance(PGVECTOR_TEST_ADMIN_URL, f'CREATE DATABASE "{dbname}"')
            )
        url = _with_database(PGVECTOR_TEST_ADMIN_URL, dbname)
        if not from_template:
            run_alembic(url, "upgrade", revision, dimensions=dimensions)
        yield url
    finally:
        # FORCE terminates any connection the test left behind, so a failing
        # test still cleans up. Best effort: a drop that itself fails must not
        # mask the test's own error.
        try:
            asyncio.run(
                _run_maintenance(
                    PGVECTOR_TEST_ADMIN_URL,
                    f'DROP DATABASE IF EXISTS "{dbname}" (FORCE)',
                )
            )
        except Exception as e:  # pragma: no cover - cleanup best effort
            print(f"warning: could not drop throwaway database {dbname}: {e}")
