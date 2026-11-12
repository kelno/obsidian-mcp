# open_area: the one call an agent makes when it opens an area

> Read before touching `open_area` (`src/mcp_server/tools.py`, end; `server.py`; `area_result.py`) or `src/services/areas_index.py`. Fork-only (see `FORK.md`).

An agent starting work in an area, or answering a question about one, used to make several calls (read the main note, `ledger_search(area=…)`, `list_tasks(area=…)`) and could skip any of them; one skipped call is how a postponed decision recorded elsewhere went unseen (ledger F60). `open_area` returns all of it in one result, with two lines saying what to do with it. Owner's design (2026-11-12), following the tenet that a tool should give an agent what it needs at the moment of choice rather than rely on the agent remembering a rule.

## Input

- `area`: a slug, a tag (`#area/x`) or the main note's name, resolved like `list_tasks(area=…)` (`task_query.AreaIndex.lookup`): an area is a note whose frontmatter declares `area_tag`.
- `folder`: an absolute path outside the vault (a repo, a working directory). Resolved through the vault's list of areas (`AREAS_INDEX_PATH`, default `Areas/Areas.md`): a Markdown table whose "Related folders" column lists absolute paths in code spans and whose "Vault folder" column links the main note. Paths compare case-insensitively with `\` and `/` alike, and a subfolder of a listed folder matches (the deepest listed folder wins). The list is the only link between a folder and an area: repos never point into the vault.

Exactly one of the two. A folder no row lists: `found: false` and a note to ask the owner which area it belongs to. A folder listed under two areas at the same depth: `found: false`, naming both. A row whose area has no main note yet (or whose note declares no `area_tag`): `found: true` with the row's name and folders, and a note.

## Output, in order

1. `area`, `tag`, `name` (the row's), `description` (frontmatter), `main_note`, `main_note_open_url` (a link opening the main note in Obsidian, with `OPEN_NOTE_URL` set; see "Open-note links" in [vault tools](vault-tools.md)), `archived` (the main note is under `ARCHIVE_FOLDER`; unset: not reported), `matched_folder`.
2. `main_note_text`: the main note without its frontmatter and its `## Tasks` section (headings inside fenced code are not headings), so the agent rules at the top and `## Current position` reach the agent; cut at 15,000 characters with `main_note_chars` and a note.
3. `related_folders`: from the area's row.
4. `entries`: the active ledger entries as `ledger_search(area=…)` lists them without a query (the same helper, `_ledger_listed`), entries tied by a topic marked `via_topic`, each with its task state; at most 40, newest change first, with a note pointing to `ledger_search` for the rest.
5. `tasks`: open tasks (status type not DONE, CANCELLED or NON_TASK, read with the owner's Tasks settings) carrying the area's tag or a tag under it, in any note: `#next` first, then by urgency, at most 30 with `tasks_total` and a note pointing to `list_tasks(area=…)`. Each listed line is checked on disk like `list_tasks` (a changed line is left out and counted in a note). A task in a folder whose name ends in ` (private)` (any case, any depth) shows `path:line` and status only, as in the ledger's `cited_by`.
6. `instructions`: two lines, one for starting work (check each task and entry against it: related, for the owner, done or made stale, the event a `#when` task waits for), one for a status question (report the Current position and the `#next` tasks).
7. `notes`: first, the area's open feedback whose citing tasks are all done (the owner may have ticked them in Obsidian, where no tool runs), with the next step: close each as `handled`, unless something in it is still open, and tell the owner (ledger F51; see "Task links" in [agent ledger](agent-ledger.md)). Then anything that could not be read (the list of areas, the main note, the tasks, the entries) is a note, never a refusal: the rest still answers.

The tag match is exact (`#area/x` or `#area/x/…`), unlike `list_tasks(area=…)`, which follows the plugin's substring match and says when it overlaps another area.

## Later parts

The function is one helper per part (`_open_area_main_note`, `_open_area_entries`, `_open_area_tasks`), so later parts go beside them: notes changed since an agent last looked, hints for tasks that look stale, and matching a "When …" task's trigger against the work being started.

## Accepted limitations

| # | What | Why accepted |
| --- | --- | --- |
| A1 | Tasks come from the task index, minutes behind a hand edit; a listed line is checked on disk, but a task added by hand since the last pass is missing. | Same as `list_tasks`. |
| A2 | The list of areas is read as written: a folder outside a code span, or a row in a second table, is not seen. | The note is kept by agents under the vault's rules; a missing match says to ask the owner. |
