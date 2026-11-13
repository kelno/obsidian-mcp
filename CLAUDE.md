# Obsidian MCP Server

Self-hosted MCP server exposing an Obsidian vault (~2,577 markdown files) via semantic search, full-text search, and agentic exploration.

## Stack
- Python 3.12 / FastAPI / uvicorn
- SQLAlchemy async + asyncpg + pgvector
- MCP Python SDK (Streamable HTTP)
- PostgreSQL 16 with pgvector (shared instance — see `.env`)
- Embedding provider abstraction (`src/services/embeddings.py`):
  - `OllamaProvider` — bge-m3 by default, set `OLLAMA_URL` in `.env`
  - `OpenAIProvider` — `text-embedding-3-{small,large}` over httpx, supports
    Azure OpenAI / OpenAI-compatible base URLs
- Jinja2 control panel; Chart.js vendored under
  `src/control_panel/static/vendor/`, no CDN, hand-written CSS, **nonce-based
  CSP** (`PANEL_CSP`) — see [control panel](docs/architecture/control-panel.md).

## Project Layout
- `src/main.py` — FastAPI app, lifespan, MCP mount
- `src/config.py` — pydantic-settings
- `src/database.py` — async SQLAlchemy engine/session
- `src/models/db.py` — ORM models (api_keys, usage_logs, notes_metadata, note_embeddings, note_links, indexer_runs, transfer_tokens, oauth_clients/codes/tokens, the `ledger_*` agent-ledger tables)
- `src/mcp_server/` — MCP server, tools, auth middleware
- `src/services/` — vault ops, search, embeddings, indexer, transfer, anchored FS
- `src/transfer/` — public `/transfer/*` capability-redemption routes
- `src/api/` — control panel REST endpoints
- `src/control_panel/` — Jinja2 templates + static assets
- `alembic/` — database migrations

## Infrastructure
- Container: `obsidian-mcp`, listens on `:8000`
- Traefik routes: hostname driven by `MCP_HOSTNAME` in `.env`
  - Panel routes: OAuth protected via `chain-oauth@file`
  - MCP routes (`/mcp/*`): API key auth at app level
  - Transfer routes (`/transfer/*`): same router as `/mcp` (no OAuth chain);
    capability-token auth at app level
- Registry: `localhost:5000` (or change in `Makefile`)
- Production deploy: merge to `main` → CI → `image` workflow (GHCR, tag
  `main-<sha>-<run_number>`) → Flux image automation on the homelab k3s
  (maxkuminov/infra `docs/homelab-image-automation.md`). Until the app is cut
  over there, a digest-bump PR in maxkuminov/infra. Docs-only merges build
  nothing.
- `make deploy` (build → scan → push → backup → migrate → recreate) is the
  compose path, not production.

## Public repo — host paths live outside the tree
This repo is published on GitHub. Anything host-specific (paths, secrets,
hostnames) must stay out of tracked files. The mechanism:
- `Makefile.local` (gitignored) overrides `DEPLOY_DIR` and `DATA_DIR`. On
  the production host both point at `/storage/docker/data/obsidian-mcp/`,
  which holds the real `docker-compose.yml`, `.env`, and `backups/`.
- The compose project that owns the running container is rooted at
  `$(DEPLOY_DIR)`, not the repo. Always invoke `make` from the repo so
  `Makefile.local` loads — `cd /storage/docker/data/obsidian-mcp && docker
  compose ...` works but skips the build/push pipeline.
- The repo's `docker-compose.yml` and the deploy-dir copy are kept
  identical; if you change one, copy it over.

## The core serves any vault
This fork's core must serve any Obsidian vault as it is. Behaviour that follows one vault's own conventions (its folder names and layout, its private folders, its naming or linking rules, an agent setup like the ledger) goes in a plugin that a deployment loads, never in the core; the core only offers the hook points. A feature that would help other vaults too, but not every one, goes behind a setting, off by default. The test before adding behaviour: would someone running this on their own vault want it without asking? If not, it belongs in a plugin. Until the plugin system exists, such behaviour goes behind a setting, off by default.

## Commands
- `make init` — first-time setup
- `make deploy` — compose-host build, backup, migration, and deploy (production
  is merge to `main`, see Infrastructure)
- `make db-init` — create database + pgvector extension
- `make db-migrate` — run alembic migrations
- `make db-check` — `alembic check`: schema vs. ORM models (must be clean)
- `make test-schema` — schema gate: migrations vs. models on a throwaway
  pgvector container (run before any deploy that carries a migration)
- `make audit` — pip-audit dependency scan
- `make reindex` / `make reset-embeddings` / `make rebuild-tsvectors` — index
  maintenance (see [indexing and embeddings](docs/architecture/indexing-and-embeddings.md))
- `make db-backup` / `make db-restore` — database dump and restore
- `make logs` — tail container logs
- `make status` — check health

`make help` lists the rest. Deployment detail lives in `DEPLOYMENT.md`, the
user-facing documentation in `README.md`.

## Git

This fork is an AI-managed repo: its owner, kelno, has their own AI agents maintain it for kelno's personal setup, under rules kept in kelno's private agent setup, including checks for interface changes. Those rules don't apply to anyone else's agents. Much of this file came with the upstream project. Where its process rules (checks, reviews, caution) are stricter than the AI-managed rules, the AI-managed rules win, and such a line can be cut. Its technical decisions ("Key decisions", `docs/architecture/`) still hold.

The AI-managed rules above set the pace here. Anything on `main` may be deployed at any time (see `FORK.md`), so push `main` when a piece of work is ready to deploy, never just to save it; work in progress stays on a branch. Before that push, the full checks must have passed once for the piece of work: the full suite including `make test-integration`, plus `make test-schema` and `make db-check` for a migration. A fix after them, even right before the push, needs only the tests for what changed (for a tool description or the guide, the test files that name it), not another full run. The report says what changed since the full run.

### Following upstream

This fork follows `maxkuminov/obsidian-mcp` under kelno's rules for forks, kept in kelno's private agent setup; they include an upstream check at the start of a session. What is specific here:

- The record of how the fork differs is `FORK.md`, "What differs from upstream": one row per difference, saying what differs and why.
- An upstream tool the owner hasn't decided on is switched off by naming it in a `SWITCHED_OFF` list in `src/mcp_server/server.py`: its code stays, it is not registered, and its tests run with it registered. The first resync that leaves a tool off adds the list. Any other upstream feature stays off through its own setting.
- A fork migration's revision id comes from 1001 up (file `1001_<name>.py`, revision `"1001"`), chained after the current head, never upstream's next number. Migrations 031 and 032 predate this and share their ids with upstream's 031 and 032; the next resync renumbers them, with a one-time fix of the live database's migration record.
- A resync is a piece of work like any other here: the full checks above before its push to `main`, and the adversarial review below, since it changes the write tools.

## Review and checks

**Review budget:** triage adversarial findings on plausible input and real impact, accepted limitations recorded in the relevant architecture note rather than polished away, and say which findings were declined. Rigor on what matters; ship in a day what can ship in a day.

**Local gates**

| Gate | Command |
| --- | --- |
| Dependency audit | `make audit` (pip-audit) |
| Migrations | `make db-migrate` (alembic) |
| Schema drift | `make db-check` (`docker exec obsidian-mcp alembic check`) |
| Schema gate (any change carrying a migration) | `make test-schema` |
| Integration suite (real Postgres) | `make test-integration` |
| Deploy | merge to `main` → CI → `image` workflow → Flux (homelab k3s); `make deploy` for a compose host |

**`tests/integration/` skips itself without a database.** Those modules are
guarded on `PGVECTOR_TEST_ADMIN_URL`, so a local `pytest tests` reports green having executed none of them. Nothing runs them on a push in this fork (see `FORK.md`): a claim that "the suite passes" is about the offline subset unless `make test-integration`, or the container run in `FORK.md`, ran them. It shares `SCHEMA_TEST_CONTAINER` / `SCHEMA_TEST_PORT` with
`test-schema`, so do not run the two concurrently.

**`alembic check` must be clean** — "No new upgrade operations detected." Run
it after any migration and after any deploy that ran one (`make db-check`, or
`docker exec obsidian-mcp alembic check` directly). A dirty check means the
database and the models disagree, which makes the next autogenerate emit noise
and, worse, hides a real missing constraint in it — see
[schema and migrations](docs/architecture/schema-and-migrations.md).

**Adversarial review for this product.** The consumer here is an **agent**, not
a person, and the vault is the owner's single source of truth for every
project. The expensive failures are **destructive writes** (an edit or append
that clobbers a note — this has actually happened) and **silently wrong search
results**, which an agent will act on without a human ever seeing the query.
A piece of work that changes the write tools, section addressing, or the chunking/embedding path gets one adversarial review before its push to `main`: a separate agent, run in the background alongside the full checks, that tries to break it on plausible input. Its findings are triaged under the review budget above; fixing them needs no second review.

**End-to-end check, not a browser pass**, for MCP-side changes — the consumer
is an agent. Once the change is deployed, call the affected MCP tools against the live server, and say in the report which tools were called, or that the check waits for the deploy. A change to `panel.js`, or to the scripts, styles or `data-*` controls of the panel, auth or consent templates, gets a browser pass: the panel sits behind SSO, so the owner opens the affected page with devtools open, and zero CSP violations is the bar (see the `panel-csp` change, tasks §6). Text-only template changes don't need one.

## Key decisions

The load-bearing rationale — the "this was a bug once, do not undo it"
material — lives in [`docs/architecture/`](docs/architecture/), indexed at the
bottom of this file. **Read the relevant note before changing that area, and
update it in the same change.** What stays here is the short list:

- API keys use `omcp_` prefix, stored as SHA-256 hashes
- Panel/OAuth passwords: direct `bcrypt` (`$2b$`, cost 12); both hash and verify truncate the UTF-8 encoding at 72 bytes and reject NUL bytes — passlib's historical semantics — so existing hashes stay valid; don't "fix" the truncation.
- Vault mounted read-write at /obsidian in container
- Read responses are capped in characters (`MAX_READ_RESPONSE_CHARS`, default
  40,000) independently of the byte caps on disk I/O — see "Three kinds of size cap"
  in [vault tools](docs/architecture/vault-tools.md). Tool output is model
  input; the server must bound it.
- Two active users' vault roots may not overlap. `src/services/vault_overlap.py`
  compares each pair by inode identity `(st_dev, st_ino)` and by a
  **component-wise** canonical-containment test, at assignment time and before
  every pass (five entry points, `detect_and_publish`); an affected pair is
  refused by every MCP tool, pass stage and transfer redemption, and nothing is
  deleted. **Until a snapshot is published, `_vault_root` refuses every
  multi-user caller** — the lifespan publishes synchronously before serving.
  A bind-mount *graft* is undetected **by decision** (L1/L2, owner-accepted
  2026-09-21, #262: never mount one tenant's directory inside another's root)
  and the consequence is cross-tenant read/overwrite/delete — see
  [vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md).
- **`/mcp` rate control is in-process, and `--workers 1` is part of the
  contract.** Two per-principal token buckets in `_tracked` (general 600/min
  burst 120; write 120/min burst 30 on the write-budget tools — the
  vault-mutating ones and the four agent-ledger writers — *and*
  `PUT /transfer/upload`, charged to the principal that **minted** the
  capability) plus a per-address budget on failed `/mcp` authentication. The
  principal is `("api_key", id)` or `("oauth", grant_id)` — the **grant**, so a
  refresh does not mint a fresh allowance. State is in the worker's memory and
  deliberately not persisted; a second uvicorn worker multiplies every rate by
  the worker count. See [rate limits](docs/architecture/rate-limits.md).
- **The buckets are the first gates; the daily quota stays the last pre-body
  gate.** Nothing durable is consumed by a call that does not run — a
  rate-refused call spends no `quota_counters` slot. Every refusal raised
  inside `_tracked` ends with one parseable line,
  `MCP-REFUSAL {"code":…,"retry_after_seconds":…}`, carried identically into a
  structured tool's error field; the transport 429s (failed-auth budget,
  transfer redemption) are deliberately outside that contract. `rate_limited` and enforced `slot_timeout`
  rows are coalesced — a row stands for `1 + suppressed` refusals.
- **New API keys get `DEFAULT_DAILY_REQUEST_LIMIT` (5,000); existing keys are
  grandfathered** — applied in application code, never as a column default.
  A blank panel field or an omitted JSON field gets the default; **only an
  administrator creates or restores an unlimited key**, by the panel's
  admin-only Unlimited box or an explicit JSON `null` (a non-admin's is a 403 /
  flash plus `panel_forbidden` `unlimited_requires_admin`); a blank edit is an
  error; and with the default null a create must name its limit (#323). OAuth
  grants (#327), pre-existing NULL-limit keys and admin-made unlimited keys
  therefore have **velocity bounds only**, owner-accepted.
- **Key creation is one budget across both routes, plus a stock cap** (#323).
  `POST /api/keys` and `POST /admin/keys/create` charge one in-process
  `try_charge_key_creation` with two counters that must both admit — exact
  account (10/h) and trusted address (20/h) — charged last, after validation,
  the unlimited rule and the cap, so a refused request spends nothing.
  slowapi cannot share a bucket across two routes, which is why it is not
  slowapi. A non-admin holds at most `KEY_MAX_ACTIVE_PER_ACCOUNT` (25) active
  keys, counted under `FOR UPDATE` on the `users` row; admins exempt, nothing
  revoked. Refusal event `key_creation_throttled`, subject = the exact
  account identity, never the address. The rules live in
  `src/services/api_keys.py` — see [rate limits](docs/architecture/rate-limits.md).
- **Concurrency admission has four modes — `off | shadow | queue | enforce` —
  and defaults to `shadow`** (#261, #188): observe, never wait or refuse.
  `queue` waits like `enforce` but admits with an `overrun` mark where enforce
  would refuse. Promotion is shadow → queue → enforce by a one-line `.env`
  edit, each step gated by `make concurrency-report TARGET=…` PASS over
  covered, single-epoch evidence (migration 028's counters and run
  watermark); never automatic. Transport admission (request envelope, auth
  permit) waits on one bounded deadline and is disconnect-aware — every body
  message it consumes is replayed intact, under a process-wide replay budget.
  Tool admission follows argument checks and precedes quota; leases remain
  held through telemetry (early release lost audit rows). A concurrency
  refusal consumes no quota, but the rate tokens already spent stay spent.
  The pool budget leaves four shared connections of headroom, not a
  reservation. See [rate limits](docs/architecture/rate-limits.md) before
  changing settings.
- **The `/mcp` body-memory budget is always on** (#322): no shadow mode, no
  off switch, every concurrency mode. Each authenticated POST (GET/DELETE
  bypass) reserves its declared `Content-Length` — the full 61 MiB
  per-request limit when unknown — after auth and before the app reads a
  byte, against the process's own cgroup limit × 0.5 ÷ 8 (128 MiB on 2 GiB:
  a 16 MiB small lane for ≤ 1 MiB requests, a strict-FIFO large lane). It
  waits up to 15 s, then answers a transport 429 `code: body_memory`
  outside `MCP-REFUSAL`. Overrides only go the safe way (multiplier ≥ 8,
  fraction ≤ 0.5, budget ≤ the limit's safe allocation), and the **lifespan**
  — never `Settings`, which the alembic init container imports — refuses to
  start when one maximum write cannot fit (≈ 1.1 GiB minimum). See
  [rate limits](docs/architecture/rate-limits.md).
- **`TRUSTED_PROXY_IPS` is the single control of forwarded-header trust**
  (#189). It is validated at boot, stored **canonicalised** (`192.168.0.10/24`
  → `192.168.0.0/24`, because uvicorn's middleware silently matches nothing on
  a host-bit CIDR), logged once at startup, and it drives the app's
  `ProxyHeadersMiddleware`. uvicorn's own layer is switched **off**
  (`--no-proxy-headers` in the Dockerfile and every compose `command:`
  override) — it rewrites the client address *before* the app's middleware
  runs, so a narrowed setting could not undo it. Do not re-enable it or set
  `FORWARDED_ALLOW_IPS`.
- **The panel login has a per-account failed-login budget** (10 / 15 min,
  `PANEL_LOGIN_FAILURE_*`), additive to the 5/min address limit. It is an
  exact in-process map keyed by `users.id`, consulted *before*
  `verify_password`, counts only failures against an existing row (an unknown
  username gets no account budget), and refuses with a response that is
  content-identical to an ordinary failed login — not a 429, not a lockout;
  the window expires on its own. The new timing signal (attempt 11 skips
  bcrypt) is an accepted limitation.
- **Unused dynamically registered OAuth clients expire** (#194).
  `oauth_clients.last_used_at` is stamped by consent approval, code exchange
  and refresh; migration 025 stamped every pre-existing row, so **NULL means
  "registered after 025 and never used"** and nothing registered before 025 is
  ever swept. The periodic cleanup deletes NULL-marked, unclaimed clients older
  than `OAUTH_CLIENT_UNUSED_EXPIRY_DAYS` (30; `null` disables, `0` is refused)
  with no child rows, via `FOR UPDATE SKIP LOCKED` and a re-check *inside* the
  lock — a single `DELETE … WHERE NOT EXISTS` cascades away a just-issued code
  under READ COMMITTED. See [oauth and grants](docs/architecture/oauth-and-grants.md).
- **OAuth grants expire absolutely, and a replayed code revokes its family**
  (#325, #326, migration 029). `oauth_tokens.grant_issued_at` is set at the
  code exchange and inherited verbatim by every rotation, like `grant_id`;
  the deadline is that plus `OAUTH_GRANT_ABSOLUTE_LIFETIME_DAYS` (90, 1–365,
  **not disable-able**), derived from the *current* setting and enforced at
  both mint sites (clamped), the refresh (a live token past it: `invalid_grant`,
  nothing revoked; a rotated-away one is still #182 reuse), the middleware, the
  panel and the transfer credential predicate. Every `OAuthToken(` in `src/`
  passes `grant_issued_at=` (AST guard, Core inserts too; the server default
  is only for a rolling deploy). A spent code keeps its `grant_id` lineage and
  is retained 7 days past expiry; its replay revokes the family **only after
  full revalidation** (client, redirect URI, PKCE), with a response identical
  to an unknown code — and so does **every failed revalidation of a spent
  code** (reason only in the log). A live code keeps its specific refusals
  and the pre-#325 check order (expiry first). See [oauth and grants](docs/architecture/oauth-and-grants.md).
- **Machine-facing paths refuse plaintext HTTP** (#196): a priority-200
  Traefik router on the `http` entrypoint, in `docker-compose.yml`'s labels,
  answers 403 via `ipAllowList` on `192.0.2.0/32` for `/mcp`, `/transfer`,
  `/health`, `/token`, `/register`, `/revoke`, `/.well-known` (except the ACME
  challenge) and the Bearer-qualified root. Browser paths keep redirecting.
  The documentation range is the mechanism, not a placeholder — do not "fix" it.
- **The panel has a nonce-based CSP** (#195). No inline `on*=` handlers and no
  `javascript:` URLs — a control is a `data-*` attribute plus a delegated
  listener in `panel.js`; every `<script>`/`<style>` carries
  `nonce="{{ csp_nonce }}"`; confirm buttons are `type="button"` and fail
  closed. No inline `style=` anywhere (#289): `style-src-attr 'none'`, dynamic
  presentation through `el.style.*` or classes, SVG colours via classes, no
  `!important`; `_utilities.html` is generated. The consent page's
  `form-action` is `'self' https:` by owner decision (multi-hop OAuth
  callbacks). Scope is a marker set by the context
  processor on the four panel/auth/consent template instances — not a path, so
  transfer pages and `/docs` are untouched. `PANEL_CSP=report-only|off` is the
  rollback (recreate, no rebuild). htmx is deliberately absent: its `hx-*`
  attributes are a nonce-bypass gadget. See [control panel](docs/architecture/control-panel.md).
- **Tool-body outcomes are typed internally.** Only the terminal `BodyOutcome`
  (or `ReadNoteResult` private outcome) drives usage classification; never
  infer failure from prose or a note's forged `MCP-REFUSAL` text. Partial writes
  retain publication uncertainty and cannot claim `nothing_written`. Body
  refusals remain executed work; shadow pressure cannot replace their actual
  outcome. See [usage attribution](docs/architecture/usage-attribution.md).
- **Internal hops have one transport policy each** (#184, #185). Database TLS
  is `DATABASE_SSL_MODE` only (default `prefer`; a TLS key in `DATABASE_URL`
  or any `PGSSL*` variable is refused); strict modes are an explicit
  `SSLContext` in both engines, refuse every Unix-socket route before
  connecting, and the lifespan exits on an unencrypted session. The active
  embedding URL must be `https` or loopback unless `EMBEDDING_ALLOW_PLAINTEXT`,
  and every embedding HTTP client comes from `embedding_http_client`
  (`trust_env=False`, no redirects). See
  [schema and migrations](docs/architecture/schema-and-migrations.md) and
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- **The app is never the PostgreSQL superuser** (#324). The lifespan reads
  `rolsuper` for `current_user` after the transport assertion and exits
  unless `DATABASE_ALLOW_SUPERUSER=true` — every deployment, by owner
  decision; k3s (`obsidian_mcp`, non-superuser) passes. The compose bundles
  bootstrap as `postgres` (password in gitignored `postgres.env`, postgres
  service only; the app service blanks `POSTGRES_PASSWORD`) and create
  `obsidian_mcp` NOSUPERUSER owning its database, validated before `initdb`
  by `docker/postgres-entrypoint.sh`. Migrations need only database
  ownership + `TEMP`; `test_nonsuperuser_migrations_pg.py` gates it, so a
  new extension goes in the init scripts, never a migration. Pre-#324
  compose volumes convert by hand with `docker/upgrade-split-db-roles.sql`
  (nothing automatic). See
  [schema and migrations](docs/architecture/schema-and-migrations.md).
- **Bookkeeping writes commit asynchronously; credential writes never do**
  (#279). `SET LOCAL synchronous_commit = off` is issued in exactly three
  places — the `api_keys.last_used_at` UPDATE (itself skipped when the stamp
  is < 60 s old), `_insert_usage`, and quota admission — so a Postgres crash
  can lose up to ~600 ms of usage rows or quota increments (L1/L2, the latter
  undercounting in the caller's favour). Token rotation/revocation, code
  exchange and transfer-token writes stay synchronous; an AST test pins the
  allow-list. The async commits were sized for PGDATA on a spinning disk
  (~40–100 ms per fsync'd commit); since 2026-09-23 the reference deployment's
  PGDATA is on NVMe (~2.5 ms), so they now matter far less, but they stay.
- **The indexer trusts `(size, mtime_ns, ctime_ns, inode)` and runs off the
  loop** (#278, #282). The scan runs in `asyncio.to_thread`, ahead of the
  generation lock (C1–C8 in `indexing-and-embeddings.md`); an unchanged stat
  skips the read. Stats inside the 2 s racy window of read start are not
  recorded. A full-hash pass runs at process start, every
  `INDEX_FULL_HASH_INTERVAL_HOURS` (24) and on panel Reindex, and the scope
  stays due until one commits with no read failure — so an edit that keeps all
  four stat fields is found within that interval (perf-L3).
  `INDEX_STAT_SHORTCUT=false` for network/FAT vault mounts and FUSE ones
  without stable inodes; gocryptfs measured fine (2026-11-09), and a wrong
  `false` costs a full read of the vault every pass.
- **Indexer failure is counted and reaches `/health`; HTTP stays 200** (#308).
  `src/services/indexer_health.py` holds per-scope consecutive index/embed
  failures and incomplete re-derives, a tick-level enumeration counter and
  "task not running", updated from **every** entrypoint (startup, periodic in
  both modes, panel Reindex). Any counter at `INDEXER_DEGRADED_AFTER_FAILURES`
  (3), a dead task, a quarantined note, a stalled loop (no index stage or
  tick finished for `INDEXER_STALLED_AFTER_SECONDS`, 1800, fork) or embedding
  without progress (a scope's embed stages ending with notes pending and none
  embedded for `EMBED_STUCK_AFTER_SECONDS`, 900, whatever the cause; fork,
  2026-11-13) makes
  `/health` `status: "degraded"` — counts only, no path/error/user id — and
  logs one CRITICAL, re-armed on reset. Never turn it into a non-200: `/health` is the liveness probe, and a
  restart cannot fix vault content. In single-user mode an index failure no
  longer skips the embed stage, and `cleanup_expired_tokens` runs in the
  tick's `finally`. A note whose own write fails with SQLSTATE class 22 or
  54000 (move, upsert row, keyword vector at the floor, links) is
  **quarantined**: the attempt rolls back, the pass re-runs without it (≤
  `INDEXER_QUARANTINE_RETRIES_PER_TICK`, 5), and while its hash is unchanged
  it is absent from the index — row deleted, never served stale — without
  being a read failure or failing the pass. In-process; the full keyword
  rebuild is excluded and stays atomic.
- **Re-derive progress is per row** (#311, migration 030).
  `notes_metadata.derived_under` is a digest of the root facts a re-deriving
  pass observed, bound to the row's path, hash and extraction version; only
  the re-derive tail writes it non-NULL, for rows it fully derived, and any
  writer that mutates a row's extracted link state without changing those
  fields (`move_note`, the move branch, the link backfill) clears it. A
  repeated re-derive rewrites only rows not derived under the current root;
  the stamp records only when an in-transaction re-read finds every surviving
  row current, after re-resolving every link target. A skip (including an
  unlisted directory) withholds only over a row that is not current.
- **The vector index is a `halfvec` expression index** (#283, migration 027),
  defined once in `src/services/vector_index.py` and excluded from `alembic
  check` by `include_object`; queries cast to match it and re-rank by
  full-precision distance. Recall measured 1.00 → 1.00; ~146 MB → 45 MB.
  Embedding reuses a stored vector only on exact chunk text and a matching
  model fingerprint, re-verified under the generation lock (#281).
- **A pass does not prune what it could not see** (#309). Rows at or beneath
  a directory the walk could not list (or type) are neither pruned nor
  move-paired, and the run is recorded `walk incomplete` (failed); under a
  re-derive it withholds the stamp only over a row not yet re-derived (#311). An
  unlistable root, or an empty root over an existing index, raises
  `IndexIndeterminate` with nothing deleted — unless an admin confirmed the
  emptying in the panel: a single-use, 15-minute, in-process permission
  (`src/services/empty_prune.py`) taken by the scope's next pass either way.
  The provenance discard is the stated exception. A wrong but non-empty
  mount is not detected (L1) — see
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- **The embed stage cannot hold the index stage back** (fork, 2026-11-01).
  Until the stages got separate loops (2026-11-09) the next index stage ran
  only after the embed stage returned, so an unbounded embed stage froze
  keyword search and the link graph (1h50 on 2026-11-01); the budget still
  bounds how long an embed stage holds `embed_pass_lock`, the pre-warm and a
  panel reset. The `EMBED_*_BUDGET_PER_USER` budget therefore applies to a
  single scope too, and a budget stop re-ticks after
  `EMBED_BACKLOG_RETICK_SECONDS` (5) instead of the interval; the stage also
  stops after `EMBED_STOP_AFTER_PROVIDER_FAILURES` (3) notes in a row fail at
  the provider, and the loop then waits the ordinary interval. Do not restore
  the "only with more than one scope" clause. See
  [indexing and embeddings](docs/architecture/indexing-and-embeddings.md).
- **The index stage and the embed stage have separate in-process locks** (fork, owner's decision, 2026-11-09): `index_pass_lock` and `embed_pass_lock`, order index → embed for a holder of both (the panel's resets). They may run side by side because `certify_embedded` is already safe against a concurrent writer; the shared clean-sweep record is guarded by `_sweep_epoch`. They run in **two loops**: `run_indexer_loop` (startup index pass, then the index stage every `INDEX_INTERVAL_SECONDS`) and `run_embed_loop` (its first tick at once, then every `EMBED_INTERVAL_SECONDS`, budget re-tick and pre-warm), supervised together so either ending ends the indexer task; an idle index tick writes no `indexer_runs` row; `/health` has a stall clock per loop. See [indexing and embeddings](docs/architecture/indexing-and-embeddings.md), "Two stage locks".
- **`move_note(rewrite_links=True)` refreshes the index first, or refuses** (fork, owner's decision, 2026-11-09): it runs the index stage for the caller's scope (`indexer.refresh_index_for_move`) before reading `note_links`, and refuses with `index_not_ready`, nothing moved, if the lock is busy past `MOVE_INDEX_REFRESH_WAIT_SECONDS`, indexing is paused, or the pass could not see every note. A link written since the last pass was otherwise silently stranded. See [vault tools](docs/architecture/vault-tools.md).
- **The agent-edit stamp is a line edit, and it never stamps over a block it cannot edit safely** (fork, owner's design, 2026-11-10). With `LAST_AI_EDIT_FIELD` set, every tool that writes a note's text sets that property to the write's date, time and offset (`src/services/ai_edit_stamp.py`), except paths matching `LAST_AI_EDIT_EXCLUDE`; a malformed block, or a result that does not re-parse to the old mapping plus the stamp, is written unstamped. The task writers report the task's line moved by the lines the stamp added. With `ARCHIVE_FOLDER` set, `move_note` says when the folder a note left has a counterpart under the archive, and never moves archive content. See [vault tools](docs/architecture/vault-tools.md).
- **Private folders are one definition and one argument** (fork, owner's decision P98, 2026-11-13). `PRIVATE_FOLDER_NAMES` (folder-name globs, empty by default) is read only through `src/services/private_folders.py`; `keyword_search`, `semantic_search` and `find_related` leave private notes out inside the query (so the limit fills) and end with the owner's line, "2 matches in private folders not shown."; `list_tasks`, `ledger_get`, `ledger_update` and `open_area` list their lines without text. `include_private` on the first six opens them, and without the setting no tool shows it (`_drop_include_private`). Private notes are embedded and filtered at search time. See "Private folders" in [search](docs/architecture/search.md).
- Wikilink graph extracted from note bodies into `note_links`; resolved at index time with same-folder-first preference
- **The agent ledger is primary data: backed up, never rebuilt from the
  vault** (fork, owner's design, migration 032). The `ledger_*` tables hold
  what agents write for each other (quirks, feedback, proposals); nothing in
  the vault can re-derive them, so a restore of last night's dump loses the
  day's entries — backups protect them. The database itself enforces the
  structure: per-kind columns and CHECKs, composite foreign keys over a
  generated `owner_key` (`coalesce(user_id, 0)`, so single-user mode is
  checked too), and triggers refusing DELETE (entries, topics, views, lines,
  counters, history), UPDATE of the history and TRUNCATE, plus a deferred
  trigger refusing at commit any change to an entry, topic or view line that
  did not bump its revision and keep the old version — unless the session
  sets `SET LOCAL ledger.allow_delete = 'on'`, the owner's way to remove
  something for good from `psql`. No foreign key points into the
  index tables, so a reindex or prune never reaches the ledger; a user with
  a ledger cannot be deleted. Writes never wait for an embedding (background
  queue, then the embed stage's sweep within its budget); the reset and the
  startup dimension check cover `ledger_embeddings`. See
  [agent ledger](docs/architecture/agent-ledger.md).
- **A plugin never fails a write, and never touches what the caller kept** (fork, owner's design 2026-11-12). `PLUGINS` names the plugins a deployment loads (`src/plugins/`), in run order; a name that does not load stops the server at startup. On the note writers, every plugin's `check` sees the caller's own text and the first refusal wins (`check_name`, on a new note's name or a move's destination, likewise); `rewrite`s chain in order after the core's own. A hook that raises or returns something unusable, or a rewrite that alters a non-blank line the caller kept or the frontmatter, is set aside with a `plugin_error:` line and an ERROR log, and the write goes ahead. See [plugins](docs/architecture/plugins.md).
- **Agents read the guide** (fork, owner's design assumption). `get_vault_guide` is the agent's manual; an agent that skips it is a bug to fix (in the guide's description or the client's setup), not a case to design around. claude.ai shows an agent only about the first 80 characters of a tool's description until it searches its tools, so every tool's first line must stand on its own, and the guide's must say to read it first.
- `MCP_SANDBOX_MODE=true` is a registry-eval-only switch: lifespan skips `_check_embedding_dim` and the indexer, and `APIKeyMiddleware` bypasses auth on `/mcp/*`. Lets Glama's sandbox build the image and validate MCP introspection without external deps. Never enable in production — tools register but cannot run.

## MCP tools

42 tools (41 in the core, `open_area` from the `kelno_vault` plugin), all
registered in `src/mcp_server/server.py` and all wrapped by
`_tracked`, which resolves the caller's vault root *before* the tool body runs
— that decorator is the whole enforcement of "this user has no vault"
([vault roots and tenancy](docs/architecture/vault-roots-and-tenancy.md)).

**Search and read** — `keyword_search`, `semantic_search`, `read_note`,
`list_notes`, `get_tags`, `get_recent`, `get_vault_guide`.

**`index_status`** — how far indexing and embedding have got for the caller's vault (embedded, pending, excluded, no text; a folder, private folders, the ledger; the last hour's rate and time left; the indexer's health), counted by `src/services/index_status.py`, which also holds the one definition of "pending" the panel uses. Class `scan`. See [indexing and embeddings](docs/architecture/indexing-and-embeddings.md), "Reporting progress to agents".

**Graph**
- `get_backlinks(path, limit)` — notes that link TO `path` (resolved links only).
- `get_links(path)` — outgoing links from `path`, both resolved and dangling.
- `get_neighborhood(path, depth=1, limit=50)` — undirected BFS over the resolved-link graph; capped at `depth ≤ 5` and `limit ≤ 200`.
- `find_related(path, limit=10)` — semantic neighbors via averaged chunk embeddings; pgvector cosine distance, deduped per note.
- `find_orphans(folder, limit)` — notes with no incoming or outgoing resolved links; vault-hygiene tool.

Link extraction lives in `src/services/links.py`. The extractor strips fenced/inline code before regex matching for `[[wikilink]]`, `![[embed]]`, and `[md](path.md)` forms. Targets are resolved at index time and stored in `note_links`. On startup, if `note_links` is empty the indexer runs a one-shot backfill across all notes (logged with progress and surfaced on the dashboard).

**Write** — `create_note`, `edit_note`, `move_note`, `delete_note`,
`set_frontmatter`. Markdown only, all publishing through
`vault._atomic_write_at` against a descriptor opened at validation. The
destructive-write surface: read
[vault tools](docs/architecture/vault-tools.md) first.

**Raw files (non-markdown)** — `read_file`, `write_file`, `list_files`,
`delete_file`. Pure byte transport, same traversal and dot-dir guards, no
indexing.

**Transfer** — `request_upload`, `check_upload`, `request_download`,
`import_from_url`, plus the public `/transfer/*` routes. A capability pins
everything it may do at mint time; see
[file transfer](docs/architecture/file-transfer.md).

**Web** — `fetch_page`: the same guarded fetch as `import_from_url`, but the
page's text goes back to the agent, never into the vault. Structured result
(`src/mcp_server/fetch_result.py`) so the page cannot forge server text;
conversion in `src/services/page_text.py`.

**Tasks** (fork) — `list_tasks`, `create_task`, `edit_task`,
`set_task_status`: the Obsidian Tasks plugin's emoji format, ported from the
plugin's 8.6.0 source (`src/services/task_format.py`) and following the
plugin's settings file in the vault. `list_tasks` answers from `note_tasks`
(migration 031, filled by the index pass next to the links) and checks each
returned line on disk; the writers change one line through the same ladder as
`edit_note`, read it back first, and re-index that note's task rows at once.
Recurring tasks are read, never changed. Read
[task tools](docs/architecture/tasks.md) first.

**`open_area`** (fork; a tool of the `kelno_vault` plugin, served only with it loaded): the one call an agent makes when it opens an area (by name or by a folder outside the vault, through the list of areas `Areas/Areas.md`): the main note without its Tasks section, related folders, active ledger entries and open tasks, then two instruction lines. Class `scan`. Read [open_area](docs/architecture/open-area.md) first.

**Agent ledger** (fork) — `ledger_search`, `ledger_get`, `ledger_add`,
`ledger_update`, `ledger_digest`, `ledger_digest_edit`, `ledger_topics`,
`ledger_topic_edit`: what agents log for each other, in the `ledger_*`
tables (migration 032), never in the vault. Every change names the revision
it read and a reason, and keeps the previous version; nothing is deleted.
What an agent needs is where it already is (fork, owner's decision 2026-11-10, ledger F34): `ledger_add` and `ledger_update` publish each rule and its allowed values in the parameter schema (describing only; the server decides), one refusal lists every problem (`problems`), and `ledger_search` results, a topic-less `ledger_add` and `get_vault_guide` carry the area and topic names.
Search returns two separate lists, by words and by meaning (an area search also finds entries tied to the area by a topic). A vault task
citing `[F12]` (any kind, several as `[P35, P48]`; `src/services/ledger_citations.py`) is the only link between an entry and the work on it: the ledger and task tools derive the other direction from
`note_tasks` at read time (`src/services/ledger_tasks.py`), nothing stored. Writes show the current state of every entry their text cites, and an update what cites the entry (fork, owner's tenet 2026-11-11, F60: compute is cheap, so the tool shows an agent what it needs at the moment of choice);
feedback's ideas live in `leads` (stored in the column `fix`, which a
032 stamp-back rerun requires). The four
writers draw on the write budget (`write_class`) though they change no
vault byte. Structured results (`src/mcp_server/ledger_result.py`), services
in `src/services/ledger*.py`, import and export in `scripts/ledger_*.py`.
Read [agent ledger](docs/architecture/agent-ledger.md) first.

## Architecture notes

One file per area, each holding the decisions that must not be silently
reversed. They are the extracted "why" of this codebase — verbatim, not
summaries.

| Note | Read it before you touch |
| --- | --- |
| [schema-and-migrations.md](docs/architecture/schema-and-migrations.md) | any alembic migration; `alembic check` is the cheap gate, not the whole gate |
| [oauth-and-grants.md](docs/architecture/oauth-and-grants.md) | `src/oauth/`, the consent page, anything minting/rotating/revoking a token |
| [vault-roots-and-tenancy.md](docs/architecture/vault-roots-and-tenancy.md) | `APIKeyMiddleware`, `_vault_root`, owner predicates, publication confirmation, the vault-root overlap guard (`src/services/vault_overlap.py`, the snapshot, the five pass entry points) |
| [open-area.md](docs/architecture/open-area.md) | `open_area`, `src/plugins/kelno_vault/open_area.py` and `areas_index.py` (fork) |
| [vault-tools.md](docs/architecture/vault-tools.md) | any note or file tool: frontmatter, symlinks, anchored writes, section addressing, size caps |
| [file-transfer.md](docs/architecture/file-transfer.md) | `src/transfer/`, `src/services/vault_fs.py`, the publish gate, SSRF policy, the write-bucket charge at redemption |
| [search.md](docs/architecture/search.md) | `semantic_search` / `keyword_search` / `find_related` and every `SET LOCAL` they issue |
| [indexing-and-embeddings.md](docs/architecture/indexing-and-embeddings.md) | the indexer loop, the embed pass, tsvector writers, provider abstraction, `index_status` |
| [security-event-logging.md](docs/architecture/security-event-logging.md) | `src/logging_setup.py`, `src/services/security_events.py`, and any call site that logs a refusal: the field allow-list, the event catalogue, the suppressor |
| [rate-limits.md](docs/architecture/rate-limits.md) | `src/services/rate_limits.py`, `src/services/concurrency.py`, `src/services/pool_budget.py`, `src/services/concurrency_counters.py`, `src/services/concurrency_readiness.py`, `src/services/refusals.py`, the pool subclass in `src/database.py`, the failed-auth budget in `APIKeyMiddleware`, the gate order in `_tracked`, the worker count, the key-creation budget and active-key cap (`src/services/api_keys.py`), and every `MCP_RATE_LIMIT_*` / `MCP_AUTH_FAILURE_*` / `MCP_CONCURRENCY_*` / `KEY_CREATION_*` / `KEY_MAX_ACTIVE_PER_ACCOUNT` / `DEFAULT_DAILY_REQUEST_LIMIT` setting |
| [usage-attribution.md](docs/architecture/usage-attribution.md) | `usage_logs`, `_log_usage`, actor columns |
| [control-panel.md](docs/architecture/control-panel.md) | panel templates, flash messages, admin guards, the Danger zone |
| [tasks.md](docs/architecture/tasks.md) | the four task tools, `src/services/task_*.py`, `note_tasks` / migration 031, and any Tasks plugin upgrade |
| [plugins.md](docs/architecture/plugins.md) | `src/plugins/`, the `PLUGINS` setting, the plugin calls in the note writers, and moving a vault-specific behaviour out of the core |
| [agent-ledger.md](docs/architecture/agent-ledger.md) | the eight `ledger_*` tools, `src/services/ledger*.py`, the `ledger_*` tables / migration 032, `scripts/ledger_import.py` / `ledger_export.py`, and the ledger's vectors in the reset and the embed stage |

Formal requirements live in `openspec/specs/`; the change history and its
reasoning in `openspec/changes/archive/`.
