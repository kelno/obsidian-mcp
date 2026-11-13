# Plugins

The core serves any Obsidian vault as it is (`CLAUDE.md`, "The core serves any vault"). Behaviour that follows one vault's own conventions lives in a plugin: a module under `src/plugins/<name>/` exposing one `plugin` object, a `src.plugins.Plugin`. A deployment loads plugins by naming them in `PLUGINS` (JSON list or comma-separated), in the order they run. With none, the server is the plain core.

The interface, the loader and the runner are all in `src/plugins/__init__.py`. Plugins live in this repo and change with it; there is no versioned interface, no packaging, no third-party loading.

## Hooks

A plugin fills only the hooks it needs; every field of `Plugin` but `name` is optional.

| Hook | Called by | Receives | Returns |
| --- | --- | --- | --- |
| `check` | `create_note`, `edit_note` (dry runs too), `write_file` on a `.md` | a `NoteWrite`: tool, vault path, old text (None for a new note), new text, dry run, user id | None, or a `Refusal(code, message)`: nothing is written |
| `rewrite` | the same writers | a `NoteWrite` whose `new` is the text so far | a `Rewrite(text, report)`; `report(shift, dry_run)` renders the plugin's block for the result |
| `check_name` | `create_note` and `write_file` on a new `.md`, `move_note` (the destination) | a `NoteName`: tool, vault path, `from_path` (`move_note`'s source, else None), user id | None, or a `NameCheck(refusal, note)`: a refusal writes or moves nothing; a `note` is added to the result |
| `tools` | the server, at import, after the core's tools (`tools.plugin_tool_impls`) | (a tuple of `PluginTool`, not a call) | each tool is wrapped in `_tracked` (vault root, rate buckets, quota, concurrency admission, usage row) and registered; its `result_type` joins the typed results whose `_body_outcome` decides the usage outcome. Its name must have an admission class in `concurrency.TOOL_CLASSES` (versioned with the admission epoch, so the core lists the tools plugins may add), checked when the plugin loads. Its module must not use `from __future__ import annotations`: the server reads the signature through the core's wrapper |
| `tool_text` | `@plugins.tool_text(name)` under `@mcp.tool()`, at import | (a mapping, not a call) | tool name → paragraphs that replace the `{plugin_text}` line of that tool's docstring; with none, the line goes |
| `guide_text` | `get_vault_guide` | (a string) | sections appended after the primer, each under a `## … (this server)` heading |

When the writers call them:

- `check` and `rewrite`: only on a write that changes the note. Not the task writers, `move_note` or `set_frontmatter`.
- `write_file` gives `check` the text decoded leniently (an undecodable byte becomes U+FFFD), so bytes that are not UTF-8 cannot switch a check off; it skips `check` on an overwrite whose old bytes it could not read (every line would look new). It runs `rewrite` only when the old and new bytes are both UTF-8.
- `check_name` runs first, before every other check of the call (the plugins' `check` included) and, for `move_note`, before the precondition's index queries, the link inventory and the rename. Only for a `.md` path; for `create_note` and `write_file` only when nothing is at the path yet (an existing note answers `already_exists`, or is overwritten without a name check). The first refusal, in plugin order, wins; the `note`s go in the result after the tool's first line. A hook that raises or returns something unusable is set aside with a `plugin_error:` line in the result and an ERROR log, and the call goes ahead.
- `check` runs right after the core's own refusals and before any rewrite, so every plugin's check sees the caller's own text. The first refusal, in plugin order, wins. A `check` is skipped where the core's `already_exists` answer will win (an existing note for `create_note`, an existing file without `overwrite` for `write_file`).
- `rewrite` runs before the agent-edit stamp and the size cap. When the rewrites undo `edit_note`'s whole change (a dropped title heading that was all of it, or a bare-name link given back the full path the note already had), it answers "No changes" with the plugins' blocks. Each plugin gets the previous one's output; its `NoteWrite.old` stays the note's old text, so "the lines this write adds or changes" keeps meaning the caller's change. Two plugins rewriting one line: the later one sees the earlier one's result, and both reports are kept.
- The reports go after the core's own blocks and before the ledger refs (`cites:`), in plugin order, then any `plugin_error:` lines. `shift` is the number of lines the stamp added above the body, so a report can give the written note's line numbers. A rewrite that adds or removes lines moves the line numbers of reports made before it: a plugin with several rewrites orders them itself.
- A refusal's code must be in `src/services/refusals.py`'s `BODY_CODES`, the catalogue every refusal is checked against: a plugin's codes are listed there.
- A hook may import the core's services (the database session through `src.database`, `note_lines`, the Tasks settings); there is no separate vault view.

## Failure

A plugin bug never fails a write. Each of these is set aside, the write goes ahead without it, the result gets a `plugin_error:` line naming the plugin and the step, and the server logs it at ERROR:

- a hook that raises;
- a `check` that returns something other than None or a `Refusal` with a listed code;
- a `rewrite` that returns something other than a `Rewrite`;
- a report that raises or returns something other than a string (None means nothing to say);
- a `rewrite` that removes or alters a non-blank line the caller kept from the old note, or changes the frontmatter block, adding one included. This guards against the expensive failure here, a write that clobbers a note: a plugin may change the caller's own lines, add lines and drop blank ones, nothing else. Which lines the caller kept is decided as `note_lines.changed_lines` decides it for every rewrite; whether the rewrite kept them is exact, only the line ending aside, so re-indenting or trimming a kept line is caught. CRLF, LF and lone-CR notes are all read line by line. Lines are compared as a multiset, so a rewrite that moves a kept line elsewhere is not caught (accepted: no plugin moves lines).

Accepted, from the adversarial review (2026-11-13): a `plugin_error:` line is lost when the write then ends early (another plugin's refusal, the size cap, `edit_note`'s "No changes", `move_note`'s concurrent-write conflict, which also drops a `check_name` note), though it is still logged; a rewrite that undoes the caller's whole change is written (a no-op write that still stamps); hooks have no timeout; a plugin has one `rewrite`, so when the guard sets it aside, everything it does on that write goes (`kelno_vault`'s title heading and links together, rare); `edit_note`'s "No changes" after a rewrite still shows the plugins' blocks, worded as if written; a plugin's guide sections come after the whole primer, not next to the core's sections on the same tools.

A name in `PLUGINS` that does not load (no module, any exception while importing it, no `plugin` of type `Plugin` named like its module, a repeated name) stops the server at startup (`main._load_plugins`, first in the lifespan, sandbox mode included): running without a vault's guards by accident is worse than not starting. `Settings` checks only that each name is an identifier, because the alembic init container imports the settings. The tool descriptions are built while `src/mcp_server/server.py` is imported, before the lifespan: there, a name that does not load gives the tools no plugin text instead of crashing the import, and the lifespan then refuses to start.

## The plugins here

- `kelno_vault`: the conventions of the fork owner's vault, fully specific to it and not configurable. It adds the tool `open_area` (`open_area.py`, with `area_result.py` and `areas_index.py`; it reads the core's task index, ledger and helpers from `src.mcp_server.tools`, a coupling the ledger's move to its own plugin will revisit). Its `check_name` checks a new note's name, or a move's destination, against its folder's `note_names` setting (`note_names.py`, `note_name_date`, folder notes read from disk). Its `check` refuses a task line written by hand (`task_lines.py`, `task_line_by_hand`, reading the Tasks settings from the caller's vault root). Its `rewrite` drops a first heading repeating the note's name (`title_heading.py`), then gives bare-name wikilinks the note's full path (`link_paths.py`, reading the caller's note list through `src.database`), in that order so the link report's line numbers are the written note's. Last, on the text as written, its `rewrite` points out a rule-note change whose lines gain no proposal ref (`rule_notes.py`, `rule_change_unlogged:`, the rule notes a constant there); it only reports. Their description paragraphs and the guide's "Task lines", "Agent rules", "Note names", "Links" and "Titles" sections are its `tool_text` and `guide_text`. The behaviours below are still in the core and move into it one at a time:
  - the task tools' area handling (`list_tasks(area=…)`, `create_task(areas=…)` and its area inferred from the folder);
  - with each, its "(this server)" section of the guide and its paragraphs in the tool descriptions.

  Private folders stay in the core (decided 2026-11-13, step 7 of the plan): `PRIVATE_FOLDER_NAMES` is already a setting, empty by default, that holds this vault's naming (` (private)`) outside the code, and the searches filter by it inside their own queries so the limit fills, which an `is_private(path)` hook could not do.
- The agent ledger moves last into a built-in plugin, `ledger`; its tables stay in the core's migration chain.

New hooks are added with the first behaviour that needs them.

## Tests

`tests/test_plugins.py`: loading and the setting, and the runner's contract on the three writers through test-double plugins (a refusal, a rewrite and its report, the stamp's shift, plugin order, every failure above). A plugin's own behaviour is tested with the plugin loaded.
