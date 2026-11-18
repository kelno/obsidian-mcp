# Obsidian MCP Server

A self-hosted MCP server that gives agents an Obsidian vault: search by words and by meaning, reading, writing, the link graph. Python 3.12, FastAPI, SQLAlchemy async on PostgreSQL 16 with pgvector, the MCP Python SDK. What it offers and how to set it up: `README.md` and `DEPLOYMENT.md`; `make help` lists the targets.

This checkout is the fork `kelno/obsidian-mcp`; its own rules come first: @CLAUDE.fork.md

## Public repo — host paths live outside the tree

This repo is published on GitHub: anything host-specific (paths, secrets, hostnames) stays out of tracked files. Host overrides go in the gitignored `Makefile.local`.

## Checks

- **A migration:** `make db-check` (`alembic check`) must print "No new upgrade operations detected.", and `make test-schema` runs before the push; not alongside `make test-integration`, which shares its container and port. Why: [schema and migrations](docs/architecture/schema-and-migrations.md).
- **A search-path change** runs the search evaluation: [search](docs/architecture/search.md), "Search evaluation harness (#356)".
- **A dependency change** runs `make audit`.

## Key decisions

The reasons behind the code live in `docs/architecture/`, one note per area (table below). **Read the area's note before changing it, and update it in the same change.** These are the decisions an agent is most likely to undo by "fixing" them:

- Panel and OAuth passwords: hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes, so existing hashes stay valid; don't "fix" the truncation. ([control panel](docs/architecture/control-panel.md))
- `--workers 1` is part of the contract: rate limits, concurrency admission and the body-memory budget live in the worker's memory, and a second worker multiplies every rate. ([rate limits](docs/architecture/rate-limits.md))
- uvicorn's own proxy-header layer stays off (`--no-proxy-headers` in the Dockerfile and every compose `command:`); `TRUSTED_PROXY_IPS` is the one control of forwarded-header trust. Don't re-enable it or set `FORWARDED_ALLOW_IPS`. ([rate limits](docs/architecture/rate-limits.md))
- New API keys' daily limit is applied in application code, never as a column default. ([rate limits](docs/architecture/rate-limits.md))
- The plaintext-HTTP refusal's `ipAllowList` on `192.0.2.0/32` is the mechanism, not a placeholder. (`DEPLOYMENT.md`)
- The panel's CSP is nonce-based: no inline `on*=` handlers, `javascript:` URLs or `style=` attributes; a control is a `data-*` attribute plus a delegated listener in `panel.js`; htmx stays out. ([control panel](docs/architecture/control-panel.md))
- `/health` is the liveness probe: indexer trouble makes it `degraded`, never a non-200. ([indexing and embeddings](docs/architecture/indexing-and-embeddings.md))
- The app is never the PostgreSQL superuser: a new extension goes in the init scripts, never in a migration. ([schema and migrations](docs/architecture/schema-and-migrations.md))
- A change to the heading scan, the chunk-length constants or the embedded-text formatting bumps `CHUNKING_VERSION`; a version's formatter is frozen, never edited. ([indexing and embeddings](docs/architecture/indexing-and-embeddings.md))

**Adding a tool:** register it in `src/mcp_server/server.py` with `title=`, `annotations=` and a description of at most 4,000 characters (`tests/test_issue_343_tool_metadata.py`); a tool that writes goes in `WRITE_REQUIRED_TOOLS` (`src/mcp_server/tool_permissions.py`); `tests/fixtures/tools_list_full_v3.json` changes only deliberately. Every tool runs inside `_tracked`, which resolves the caller's vault root before the body runs: [vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md).

## Architecture notes

| Note | Read it before you touch |
| --- | --- |
| [schema-and-migrations.md](docs/architecture/schema-and-migrations.md) | any alembic migration; `alembic check` is the cheap gate, not the whole gate |
| [oauth-and-grants.md](docs/architecture/oauth-and-grants.md) | `src/oauth/`, the consent page, anything minting, rotating or revoking a token |
| [vault-roots-and-tenancy.md](docs/architecture/vault-roots-and-tenancy.md) | `APIKeyMiddleware`, `_vault_root`, owner predicates, publication confirmation, the vault-root overlap guard |
| [vault-tools.md](docs/architecture/vault-tools.md) | any note or file tool: frontmatter, symlinks, anchored writes, published file permissions, section addressing, size caps; upstream's task tools |
| [file-transfer.md](docs/architecture/file-transfer.md) | `src/transfer/`, `src/services/vault_fs.py`, the publish gate, SSRF policy |
| [search.md](docs/architecture/search.md) | the search tools and every `SET LOCAL` they issue, the reranker, `scripts/search_eval/` |
| [indexing-and-embeddings.md](docs/architecture/indexing-and-embeddings.md) | the indexer and embed loops, chunking, tsvector writers, providers, `/health`'s indexer status, task derivation |
| [security-event-logging.md](docs/architecture/security-event-logging.md) | `src/logging_setup.py`, `src/services/security_events.py`, any call site that logs a refusal |
| [rate-limits.md](docs/architecture/rate-limits.md) | rate limits, concurrency admission, the body-memory budget, the pool budget, the gate order in `_tracked`, proxy trust, key creation and daily limits, and every setting for them |
| [usage-attribution.md](docs/architecture/usage-attribution.md) | `usage_logs`, `_log_usage`, actor columns, tool-body outcomes |
| [control-panel.md](docs/architecture/control-panel.md) | panel templates and CSP, flash messages, admin guards, passwords, native OIDC's invariants, the Danger zone |
| [native-oidc.md](docs/native-oidc.md) | the OIDC login and its settings (operator guide) |

Formal requirements live in `openspec/specs/`; the change history and its reasoning in `openspec/changes/archive/`.
