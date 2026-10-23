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
| `delete_folder` tool: remove an empty folder with one `rmdir`; new refusal code `folder_not_empty` | `bcfc113` | Not proposed yet |
| `move_file` and `move_note` precondition refusals say "move" instead of "write" | `f3cfef6` | Not proposed yet; depends on `move_file` |
| `delete_file` names `.trash/` entries in UTC, like `delete_note` | `5744b33` | [#339](https://github.com/maxkuminov/obsidian-mcp/pull/339), open |
| Higher default MCP rate limits (600/120 general, 120/30 write) | `f3618a0` | Upstream's own [#337](https://github.com/maxkuminov/obsidian-mcp/pull/337), open; merged here early |
| `fork-image` workflow: builds and publishes the image on every push to `main`, without the test suite | `4ee2d8a`, `8f540b4` | Fork-only, never proposed |

Upstream's `ci.yml` and `image.yml` are disabled in this fork's Actions
settings instead of edited, so syncing never conflicts with them. Run the
test suite locally, or through a pull request to upstream, before relying on
a change.

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
