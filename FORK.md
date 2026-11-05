# Fork notes

What this note is for: tell agents and the owner how this fork differs from upstream and how to work on it here. Agents wrote it and the owner hasn't reviewed it yet, so don't hesitate to slash through it, restructure it or change what's here when it's wrong, outdated or in the way. (owner)

This is `kelno/obsidian-mcp`, a fork of
[`maxkuminov/obsidian-mcp`](https://github.com/maxkuminov/obsidian-mcp). Every
push to `main` builds and publishes the image `ghcr.io/kelno/obsidian-mcp:main`
(the `image-no-tests` workflow, below); a deployment may follow that image
automatically, so a push can reach production. How and where the fork is
deployed is not part of this repo.

Last checked against `upstream/main` at `564e9e0` (#338) on 2026-10-23.

## What differs from upstream

| Change | Commit | Upstream status |
| --- | --- | --- |
| `move_file` tool: move or rename a non-markdown file with one non-replacing rename | `edf0394` | Not proposed yet |
| `delete_folder` tool: remove an empty folder with one `rmdir`; new refusal code `folder_not_empty` | `bcfc113`, `9c4f5ee` | Not proposed yet |
| `list_files` reports a missing folder as `not_found` ("Folder not found"), not `invalid_path` | `d0d67c1` | Not proposed yet |
| `move_file` and `move_note` precondition refusals say "move" instead of "write" | `f3cfef6` | Not proposed yet; depends on `move_file` |
| `delete_file` names `.trash/` entries in UTC, like `delete_note` | `5744b33` | [#339](https://github.com/maxkuminov/obsidian-mcp/pull/339), open |
| Embedding per-request timeouts as settings (`OLLAMA_REQUEST_TIMEOUT_SECONDS`, `OPENAI_REQUEST_TIMEOUT_SECONDS`), `OPENAI_EMBED_BATCH_SIZE`; the OpenAI provider no longer retries a read timeout | `312d740` | Not proposed yet |
| Higher default MCP rate limits (600/120 general, 120/30 write) | `f3618a0` | Upstream's own [#337](https://github.com/maxkuminov/obsidian-mcp/pull/337), open; merged here early |
| `fetch_page` tool: fetch a public page under `import_from_url`'s SSRF policy and return its text (HTML → Markdown via markdownify, main content only) to the agent in structured fields marked untrusted; new deps `markdownify`, `beautifulsoup4`; setting `FETCH_PAGE_MAX_BYTES` | `4aa8987` | Not proposed yet |
| Outbound fetches send an identifiable `User-Agent` (`OUTBOUND_USER_AGENT`); httpx's default was refused with a 403 by Wikipedia | `e20e5cd` | Not proposed yet |
| Tests pass on Windows checkouts and outside UTC: `.gitattributes` keeps `*.sh` LF; `test_a_naive_expires_at_does_not_raise` builds its naive time in UTC | `f81f2c8`, `9cccebd` | [#342](https://github.com/maxkuminov/obsidian-mcp/pull/342), open |
| Task tools `list_tasks`, `create_task`, `edit_task`, `set_task_status`: the Obsidian Tasks plugin's emoji format ported from its 8.6.0 source, following its settings file in the vault; table `note_tasks` (migration 031) filled by the index pass, extraction version 4; new refusal codes `line_mismatch`, `not_a_task`, `recurring_task`, `task_settings_invalid`; settings `TASKS_INBOX_PATH`, `TASKS_TIMEZONE`, `TASKS_PLUGIN_SETTINGS_PATH`. See `docs/architecture/tasks.md` | `da0ab22` | Fork-only, never proposed |
| The index walk skips Syncthing conflict copies (`*.sync-conflict-*`) like dot-files, so their checkboxes never show up as tasks; the vault guide's primer says to manage tasks with the task tools, which add the vault's global filter themselves | `55e1d79`, `0bb51d1` | Fork-only, never proposed |
| `CLAUDE.md` / `AGENTS.md` no longer point to upstream's machine-local `~/.claude/rules/engineering-workflow.md` (OpenSpec flow, Codex audit gates); the review budget, the adversarial review of write-tool changes and the end-to-end check stay as repo rules | `d9cf6cf` | Fork-only, never proposed |
| `request_upload` (description, result text) and the vault guide tell an agent that has the file in its own shell or code sandbox, such as a chat attachment, to upload it itself with `curl`; the README gives the claude.ai setup step (the server's hostname in the sandbox's allowed domains) | `054a0c9` | Not proposed yet |
| `get_vault_guide`'s first line tells agents to read it before any other tool (claude.ai shows only about 80 characters of a description); `CLAUDE.md` records the design assumption that agents read the guide and every tool's first line stands on its own | `1bbbf61` | Not proposed yet |
| The embed stage cannot hold the index stage back: the embed budget applies to a single scope too and a budget stop re-ticks after 5 s; the stage stops after `EMBED_STOP_AFTER_PROVIDER_FAILURES` notes in a row fail at the provider; `/health` reports `index_stalled` after `INDEXER_STALLED_AFTER_SECONDS`; time-based embed progress lines. After the 2026-11-01 incident (1h50 with no index stage) | `cc6980c` | Not proposed yet |
| `create_task` / `edit_task` write the Tasks global filter once when the description already has it (`#task #task` lines in live use); declared divergence T12 in `docs/architecture/tasks.md` | `aef6623` | Fork-only, never proposed |
| Agent ledger: eight tools `ledger_search`, `ledger_get`, `ledger_add`, `ledger_update`, `ledger_digest`, `ledger_digest_edit`, `ledger_topics`, `ledger_topic_edit` over the `ledger_*` tables (migration 032: owner-checked composite foreign keys, per-kind CHECKs, no-delete and append-only triggers with the `ledger.allow_delete` session switch); background embedding plus a sweep in the embed stage; the reset and the startup dimension check cover `ledger_embeddings`; the panel refuses to delete a user with a ledger; `scripts/ledger_import.py`, `scripts/ledger_export.py`; refusal code `stale_revision`; settings `LEDGER_VIEW_HINT_MIN_QUIRKS`, `LEDGER_QUERY_EMBED_TIMEOUT_SECONDS`, `LEDGER_EMBED_QUEUE_MAX`. See `docs/architecture/agent-ledger.md` | `ee83708`..`f297491`, `c41e5ed`..`4aecdf8` | Fork-only, never proposed |
| `image-no-tests` workflow: builds and publishes the image on every push to `main`, without the test suite | `4ee2d8a`, `8f540b4` | Fork-only, never proposed |
| Faster test runs: integration databases copied from per-process migrated templates (`fresh=True` and `OMCP_TEST_NO_DB_TEMPLATE=1` opt out), test modules kept whole per pytest-xdist worker (`--dist loadgroup`, `XDIST_PER_TEST`), `tests/Containerfile`; see "Running the test suite" below | `c2c1dee` | Not proposed yet |

Upstream's `ci.yml` and `image.yml` are disabled in this fork's Actions
settings instead of edited, so syncing never conflicts with them. Run the
test suite locally, or through a pull request to upstream, before relying on
a change.

## Running the test suite

A push to `main` builds and publishes the image without running the tests (the `image-no-tests` workflow, above), so the full local run is the only gate. Day to day, run the test files your change affects; run the full suite before every push.

### The full run

Run it as CI does: Python 3.12 with a `pgvector/pgvector:pg16` server, `PGVECTOR_TEST_ADMIN_URL` and `TEST_DATABASE_URL` pointing at it, in a privileged Linux container (for `unshare -Urm`), with `/tmp` a tmpfs (overlayfs has no `O_TMPFILE`, so every no-clobber write would refuse) and `psql` installed (`record-backup.sh` calls it). `tests/Containerfile` builds that image. With Docker (Podman takes the same commands):

```bash
docker build -t omcp-test -f tests/Containerfile .
docker network create omcp-test
docker run -d --name omcp-test-pg --network omcp-test -e POSTGRES_PASSWORD=test pgvector/pgvector:pg16 postgres -c max_connections=300
docker run --rm --privileged --tmpfs /tmp --network omcp-test \
  -e PGVECTOR_TEST_ADMIN_URL=postgresql+asyncpg://postgres:test@omcp-test-pg:5432/postgres \
  -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@omcp-test-pg:5432/postgres \
  -v "<the checkout>:/repo:ro" omcp-test sh -c \
  'mkdir /tmp/r && tar -C /repo --exclude=.git -cf - . | tar -C /tmp/r -xf - && cd /tmp/r && python -m pytest -q -n auto --dist loadgroup tests'
```

Rebuild the image when `requirements*.txt` change. Afterwards, remove the Postgres container and the network, or keep them for the next run.

Why each part is there:

- **`-n auto --dist loadgroup`** (pytest-xdist): one worker per CPU. The hook in `tests/conftest.py` keeps each test module whole on one worker, in file order, because module-scoped fixtures and tests that rely on an earlier test in the same module assume that. A module whose tests are each self-contained sets `XDIST_PER_TEST = True` and is shared out test by test. `tests/integration/test_schema_check.py` does, or it alone would take minutes. All workers must collect the same test names, so a parametrize id can't come from a random value: give it `pytest.param(..., id=...)`.
- **The copy into `/tmp`**: a checkout on a Windows drive reaches the container through a slow file bridge, and the integration tests start many `alembic` subprocesses, each importing the app again. Copying first saved about a fifth of the run.
- **A network, not a shared pod or `localhost`**: the pgvector image trusts loopback connections without a password, so `test_wrong_password_prints_the_upgrade_hint_once` fails when the tests reach Postgres over loopback.
- **`max_connections=300`**: each parallel worker holds its own connection pools, and the default of 100 leaves too little headroom.

Measured on a 12-CPU Windows PC with Podman, 2026-11-05: about 3½ minutes for the full run (the offline suite alone, about 1½), against about 29 minutes run serially from the mounted checkout (offline 4½, integration 24½).

### Integration databases come from templates

Each integration module used to build its database by running every migration. `tests/integration/_harness.py` now migrates one template per revision and embedding width, once per process, and copies it (`CREATE DATABASE … TEMPLATE`) for each module. A copy has exactly the schema the migrations produced earlier in the same run, so the tests check the same schema; what they no longer do is each run the migrations themselves. The tests whose subject is that run still run it: the schema gate's fresh-database test (`fresh=True`), the stamp-back, upgrade and downgrade tests (they run `alembic` on their copy), and `test_nonsuperuser_migrations_pg.py` (it builds its own databases). `OMCP_TEST_NO_DB_TEMPLATE=1` turns the templates off for a whole run. The templates are dropped at the end of the session; a killed run leaves `tmpl_*` databases on the throwaway server.

### Single files, and the offline suite without a container

`python -m pytest -q tests/test_task_tools.py tests/integration/test_task_tools_pg.py` runs just those files. An integration file needs the database URLs above (in the container, or with any reachable server), or it skips itself. The offline suite (everything outside `tests/integration/`) needs no container and no database: a Python 3.12 venv with `requirements-dev.txt` is enough, `pytest -q -n auto --dist loadgroup --ignore=tests/integration tests`. In WSL, run it from a copy on WSL's own disk rather than from `/mnt/...`, for the same file-bridge reason (for example `rsync -a --delete --exclude .git <the checkout's WSL path>/ ~/omcp-tests/` before each run).

On Windows, call `wsl` from PowerShell, for example `wsl -e bash -lc "cd <the copy> && <the venv's python> -m pytest -q tests/test_task_tools.py"`, not from Git Bash. Git Bash expands `$variables` inside the quoted command before WSL sees it, and rewrites `/mnt/...` paths to `C:/Program Files/Git/mnt/...`.

The CRLF line endings that used to break the `docker/*.sh` scripts on a Windows checkout are fixed by `.gitattributes` (#342). A checkout made before that fix needs `rm docker/*.sh && git checkout -- docker/` once.

## Embedding on a slow CPU

On a CPU-only Ollama the default 30 s per request is too short, every
request times out, and the same notes are re-selected every pass while
Ollama keeps computing the abandoned requests. Talk to Ollama directly
(`EMBEDDING_PROVIDER=ollama`, not an OpenAI-compatible gateway in front of
it), then size one request:

1. Measure N, the seconds Ollama takes to embed one 512-token chunk on that
   machine.
2. Set `OLLAMA_EMBED_BATCH_SIZE` to 1 or 2.
3. Set `OLLAMA_REQUEST_TIMEOUT_SECONDS` to about 3 × batch × N, at most
   3600.

The timeout bounds one request, never a note: a long note is many requests,
each under the full timeout. A `semantic_search` query waits under the same
bound, and on a single-slot Ollama (`-np 1`) it queues behind any indexing
request already running, so a query during indexing can take up to one
request's time. Changing the provider or model changes the embedding
fingerprint and needs a re-embed; the URL does not, so the bulk embed can
run against a fast machine first and `OLLAMA_URL` be switched afterwards.

## Pending pull requests

- [#339](https://github.com/maxkuminov/obsidian-mcp/pull/339), ours, open.
  When upstream merges it, the next sync makes `5744b33` redundant.
- [#337](https://github.com/maxkuminov/obsidian-mcp/pull/337), upstream's,
  open. Merged here at its current head `f3618a0`. If upstream changes it
  before merging, the next sync brings the final version, which may conflict
  with the early merge.

## Syncing with upstream

```bash
git fetch upstream
git merge upstream/main
```

Then update the table above: drop rows that upstream has merged, and move
"Last checked" to the new `upstream/main`.
