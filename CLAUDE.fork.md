# The fork's own rules

This checkout is `kelno/obsidian-mcp`, a fork of `maxkuminov/obsidian-mcp`. `CLAUDE.md` is upstream's file, cut down; the fork's own rules are here, kept out of it so a resync never mixes the two (owner). How the fork differs, and how to run its tests: `FORK.md`.

## The core serves any vault

This fork's core must serve any Obsidian vault as it is. Behaviour that follows one vault's own conventions (its folder names and layout, its private folders, its naming or linking rules, an agent setup like the ledger) goes in a plugin that a deployment loads (`src/plugins/`, see [plugins](docs/architecture/plugins.md)), never in the core; the core only offers the hook points. A feature that would help other vaults too, but not every one, goes behind a setting, off by default. The test before adding behaviour: would someone running this on their own vault want it without asking? If not, it belongs in a plugin.

## Git

This fork is AI-managed by its owner, kelno, under kelno's own rules, which don't apply to anyone else's agents. Much of this repo's text came with the upstream project. Its process lines that are stricter than the AI-managed rules (checks, reviews, caution) are upstream's and don't bind kelno's agents: cut such a line when you meet it. [P234] Its technical decisions (`docs/architecture/`) still hold.

The AI-managed rules above set the pace here. A push to `main` publishes the image, and a deployment may follow it (see `FORK.md`), so push `main` when a piece of work is ready to deploy, never just to save it; work in progress stays on a branch. Before that push, the full checks must have passed once for the piece of work: the full run in `FORK.md` ("Running the test suite"), plus `make test-schema` and `make db-check` for a migration. A fix after them, even right before the push, needs only the tests for what changed (for a tool description or the guide, the test files that name it), not another full run. The report says what changed since the full run.

### Following upstream

This fork follows `maxkuminov/obsidian-mcp`. What is specific here:

- The record of how the fork differs is `FORK.md`, "What differs from upstream": one row per difference, saying what differs and why.
- An upstream tool the owner hasn't decided on is switched off by naming it in the `SWITCHED_OFF` list at the end of `src/mcp_server/server.py`: its code stays, it is not registered, and its tests run with it registered. Any other upstream feature stays off through its own setting.
- A fork migration's revision id comes from 1001 up (file `1001_<name>.py`, revision `"1001"`), never upstream's next number; a resync that brings upstream migrations joins the two branches with a merge revision. See [schema and migrations](docs/architecture/schema-and-migrations.md), "Fork migrations".
- `CLAUDE.md` is upstream's file, cut down, and every session here loads it whole; upstream writes a lot into it. A resync reads everything upstream changed in it since the last sync (`git diff <merge base> upstream/main -- CLAUDE.md`), whether or not git reports a conflict there, and cuts it down carefully rather than taking it whole: each addition goes to the architecture note of its area (upstream usually writes it there too), and `CLAUDE.md` takes at most one line, only for a trap an agent would otherwise "fix". The fork's own rules go in this file, never in `CLAUDE.md`. (owner)
- A pull request to upstream follows upstream's own `CLAUDE.md`, read whole from `upstream/main` (`git show upstream/main:CLAUDE.md`), not this checkout's cut copy: its process lines, cut here, bind a contribution there. (owner)
- A resync is a piece of work like any other here: the full checks above before its push to `main`, and the adversarial review below, since it changes the write tools.

## Review and checks

**Review budget:** triage adversarial findings on plausible input and real impact, accepted limitations recorded in the relevant architecture note rather than polished away, and say which findings were declined. Rigor on what matters; ship in a day what can ship in a day.

- **`tests/integration/` skips itself without a database.** Those modules are guarded on `PGVECTOR_TEST_ADMIN_URL`, so a `pytest tests` without one reports green having executed none of them: a claim that "the suite passes" is about the offline subset unless the full run in `FORK.md` ran them.
- **Adversarial review.** The consumer here is an agent, not a person, and the vault is the owner's single source of truth for every project. The expensive failures are destructive writes (an edit or append that clobbers a note: this has actually happened) and silently wrong search results, which an agent will act on without a human ever seeing the query. A piece of work that changes the write tools, section addressing, or the chunking/embedding path gets one adversarial review before its push to `main`: a separate agent, run in the background alongside the full checks, that tries to break it on plausible input. Its findings are triaged under the review budget above; fixing them needs no second review.
- **End-to-end check, not a browser pass**, for MCP-side changes. Once the change is deployed, call the affected MCP tools against the live server, and say in the report which tools were called, or that the check waits for the deploy. A change to `panel.js`, or to the scripts, styles or `data-*` controls of the panel, auth or consent templates, gets a browser pass: the affected page opened with devtools open, zero CSP violations being the bar. Text-only template changes don't need one.

## Decisions of the fork

- The agent ledger is primary data: backed up, never rebuilt from the vault, deletes refused by the database. ([agent ledger](docs/architecture/agent-ledger.md))
- A plugin never fails a write, and never touches what the caller kept. ([plugins](docs/architecture/plugins.md))
- Agents read the guide: `get_vault_guide` is the agent's manual, and an agent that skips it is a bug to fix (in the guide's description or the client's setup), not a case to design around. claude.ai shows an agent only about the first 80 characters of a tool's description until it searches its tools, so every tool's first line stands on its own, and the guide's says to read it first. ([vault tools](docs/architecture/vault-tools.md))
- A tool's description is at most 4,000 characters as served with the deployment's plugins loaded, their text included. A new tool, or one switched on, gets a `FORK.md` row.

| Note | Read it before you touch |
| --- | --- |
| [tasks.md](docs/architecture/tasks.md) | the fork's four task tools, `src/services/fork_tasks/`, `fork_note_tasks` / migration 1001, any Tasks plugin upgrade |
| [plugins.md](docs/architecture/plugins.md) | `src/plugins/`, the `PLUGINS` setting, the plugin calls in the note writers, moving a vault-specific behaviour out of the core |
| [agent-ledger.md](docs/architecture/agent-ledger.md) | the `ledger_*` tools, tables and migrations 1002 and 1004, `scripts/ledger_import.py` / `ledger_export.py` |
| [open-area.md](docs/architecture/open-area.md) | `open_area` and the area index (`kelno_vault`) |
| [agent-paths.md](docs/architecture/agent-paths.md) | `agent_path`, `agent_path_reached_by` (`kelno_vault`) |
| [search.md](docs/architecture/search.md), "Private folders" | `PRIVATE_FOLDER_NAMES`, `include_private` |
| [vault-tools.md](docs/architecture/vault-tools.md), "The agent-edit stamp" | `src/services/ai_edit_stamp.py`, `LAST_AI_EDIT_FIELD`, `ARCHIVE_FOLDER` |
