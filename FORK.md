# Fork notes

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
| `fork-image` workflow: builds and publishes the image on every push to `main`, without the test suite | `4ee2d8a`, `8f540b4` | Fork-only, never proposed |

Upstream's `ci.yml` and `image.yml` are disabled in this fork's Actions
settings instead of edited, so syncing never conflicts with them. Run the
test suite locally, or through a pull request to upstream, before relying on
a change.

## Running the test suite from a Windows checkout

The suite takes about 22 minutes. Run it as CI does: Python 3.12 with a
`pgvector/pgvector:pg16` server, `PGVECTOR_TEST_ADMIN_URL` and
`TEST_DATABASE_URL` pointing at it, in a privileged Linux container (for
`unshare -Urm`). Three differences from CI's Ubuntu runner make tests fail
that have nothing to do with the code:

- **CRLF line endings.** Git on Windows checks the `docker/*.sh` scripts out
  with CRLF, and bash refuses to run them. Convert copies to LF inside the
  container (`sed -i 's/\r$//'`) before running.
- **No `O_TMPFILE` on overlayfs.** The container's `/tmp` must be a tmpfs
  (`--tmpfs /tmp`), or every no-clobber write refuses.
- **No `psql`.** `record-backup.sh` calls it; install `postgresql-client` in
  the container.

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
