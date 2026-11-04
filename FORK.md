# Fork notes

What this note is for: tell agents and the owner how this fork differs from upstream and how to work on it here. Agents wrote it and the owner hasn't reviewed it yet, so don't hesitate to slash through it, restructure it or change what's here when it's wrong, outdated or in the way. (owner)

This is `kelno/obsidian-mcp`, a fork of
[`maxkuminov/obsidian-mcp`](https://github.com/maxkuminov/obsidian-mcp). It
runs on a home server, deployed by the `obsidian_mcp` task of `sgl-pyinfra`,
which follows the image `ghcr.io/kelno/obsidian-mcp:main` with podman
auto-update.

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
| `image-no-tests` workflow: builds and publishes the image on every push to `main`, without the test suite | `4ee2d8a`, `8f540b4` | Fork-only, never proposed |

Upstream's `ci.yml` and `image.yml` are disabled in this fork's Actions
settings instead of edited, so syncing never conflicts with them. Run the
test suite locally, or through a pull request to upstream, before relying on
a change.

## Running the test suite from a Windows checkout

The suite takes about 22 minutes. Run it as CI does: Python 3.12 with a
`pgvector/pgvector:pg16` server, `PGVECTOR_TEST_ADMIN_URL` and
`TEST_DATABASE_URL` pointing at it, in a privileged Linux container (for
`unshare -Urm`). Two differences from CI's Ubuntu runner make tests fail
that have nothing to do with the code:

- **No `O_TMPFILE` on overlayfs.** The container's `/tmp` must be a tmpfs
  (`--tmpfs /tmp`), or every no-clobber write refuses.
- **No `psql`.** `record-backup.sh` calls it; install `postgresql-client` in
  the container.

The CRLF line endings that used to break the `docker/*.sh` scripts on a
Windows checkout are fixed by `.gitattributes` (#342). A checkout made before
it needs `rm docker/*.sh && git checkout -- docker/` once.

For the offline suite only (everything outside `tests/integration/`, about
5 minutes), a Python 3.12 venv in WSL is enough, with no container and no
database: `pytest --ignore=tests/integration tests`.

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
