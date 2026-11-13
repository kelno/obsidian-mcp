# Plugins

The core serves any Obsidian vault as it is (`CLAUDE.md`, "The core serves any vault"). Behaviour that follows one vault's own conventions lives in a plugin: a module under `src/plugins/<name>/` exposing one `plugin` object, a `src.plugins.Plugin`. A deployment loads plugins by naming them in `PLUGINS` (JSON list or comma-separated), in the order they run. With none, the server is the plain core.

The interface, the loader and the runner are all in `src/plugins/__init__.py`. Plugins live in this repo and change with it; there is no versioned interface, no packaging, no third-party loading.

## Hooks

A plugin fills only the hooks it needs; every field of `Plugin` but `name` is optional.

| Hook | Called by | Receives | Returns |
| --- | --- | --- | --- |
| `check` | `create_note`, `edit_note` (dry runs too), `write_file` on a `.md` | a `NoteWrite`: tool, vault path, old text (None for a new note), new text, dry run, user id | None, or a `Refusal(code, message)`: nothing is written |
| `rewrite` | the same writers | a `NoteWrite` whose `new` is the text so far | a `Rewrite(text, report)`; `report(shift, dry_run)` renders the plugin's block for the result |
| `tool_text` | `@plugins.tool_text(name)` under `@mcp.tool()`, at import | (a mapping, not a call) | tool name → paragraphs that replace the `{plugin_text}` line of that tool's docstring; with none, the line goes |
| `guide_text` | `get_vault_guide` | (a string) | sections appended after the primer, each under a `## … (this server)` heading |

When the writers call them:

- Only on a write that changes the note. Not the task writers, `move_note` or `set_frontmatter`.
- `write_file` gives `check` the text decoded leniently (an undecodable byte becomes U+FFFD), as the core's task-line check reads it, so bytes that are not UTF-8 cannot switch a check off; it skips `check`, like that check, on an overwrite whose old bytes it could not read. It runs `rewrite` only when the old and new bytes are both UTF-8, as it does the link rewrite.
- `check` runs right after the core's own refusals (task lines, note names) and before any rewrite, so every plugin's check sees the caller's own text. The first refusal, in plugin order, wins. A `check` is skipped where the core's `already_exists` answer will win (an existing note for `create_note`, an existing file without `overwrite` for `write_file`).
- `rewrite` runs before the core's link rewrite (so a plugin that drops lines leaves the link report's line numbers right) and before the agent-edit stamp and the size cap. When the rewrites undo `edit_note`'s whole change (a dropped title heading that was all of it), it answers "No changes" with the plugins' blocks. Each plugin gets the previous one's output; its `NoteWrite.old` stays the note's old text, so "the lines this write adds or changes" keeps meaning the caller's change. Two plugins rewriting one line: the later one sees the earlier one's result, and both reports are kept.
- The reports go after `rule_change_unlogged:` and before the link report, in plugin order, then any `plugin_error:` lines. `shift` is the number of lines the stamp added above the body, so a report can give the written note's line numbers. A rewrite that adds or removes lines moves the line numbers of reports made before it: a plugin with several rewrites orders them itself.
- A refusal's code must be in `src/services/refusals.py`'s `BODY_CODES`, the catalogue every refusal is checked against: a plugin's codes are listed there.
- A hook may import the core's services (the database session, `link_paths`, the Tasks settings); there is no separate vault view.

## Failure

A plugin bug never fails a write. Each of these is set aside, the write goes ahead without it, the result gets a `plugin_error:` line naming the plugin and the step, and the server logs it at ERROR:

- a hook that raises;
- a `check` that returns something other than None or a `Refusal` with a listed code;
- a `rewrite` that returns something other than a `Rewrite`;
- a report that raises or returns something other than a string (None means nothing to say);
- a `rewrite` that removes or alters a non-blank line the caller kept from the old note, or changes the frontmatter block, adding one included. This guards against the expensive failure here, a write that clobbers a note: a plugin may change the caller's own lines, add lines and drop blank ones, nothing else. Which lines the caller kept is decided as `link_paths.changed_lines` decides it for the core's own rewrites; whether the rewrite kept them is exact, only the line ending aside, so re-indenting or trimming a kept line is caught. CRLF, LF and lone-CR notes are all read line by line. Lines are compared as a multiset, so a rewrite that moves a kept line elsewhere is not caught (accepted: no plugin moves lines).

Accepted, from the adversarial review (2026-11-13): a `plugin_error:` line is lost when the write then ends early (another plugin's refusal, the size cap, `edit_note`'s "No changes"), though it is still logged; a rewrite that undoes the caller's whole change is written (a no-op write that still stamps); hooks have no timeout; a plugin's guide sections come after the whole primer, not next to the core's sections on the same tools.

A name in `PLUGINS` that does not load (no module, any exception while importing it, no `plugin` of type `Plugin` named like its module, a repeated name) stops the server at startup (`main._load_plugins`, first in the lifespan, sandbox mode included): running without a vault's guards by accident is worse than not starting. `Settings` checks only that each name is an identifier, because the alembic init container imports the settings. The tool descriptions are built while `src/mcp_server/server.py` is imported, before the lifespan: there, a name that does not load gives the tools no plugin text instead of crashing the import, and the lifespan then refuses to start.

## The plugins here

- `kelno_vault`: the conventions of the fork owner's vault, fully specific to it and not configurable. It drops a first heading repeating the note's name (`rewrite`, `title_heading.py`; its description paragraphs and the guide's "Titles" section are its `tool_text` and `guide_text`). The behaviours below are still in the core and move into it one at a time:
  - full-path link rewriting (`rewrite`, ordered after the title heading);
  - the hand-written task-line refusal (`check`);
  - `rule_change_unlogged`, with `RULE_FILE_PATTERNS` becoming its constant;
  - `note_names` (a name hook for `create_note`, `write_file` and `move_note`);
  - private folders (` (private)`), through an `is_private(path)` hook;
  - `open_area` and the task tools' area handling (a hook adding tools, wrapped by the core in `_tracked`);
  - with each, its "(this server)" section of the guide and its paragraphs in the tool descriptions.
- The agent ledger moves last into a built-in plugin, `ledger`; its tables stay in the core's migration chain.

New hooks are added with the first behaviour that needs them.

## Tests

`tests/test_plugins.py`: loading and the setting, and the runner's contract on the three writers through test-double plugins (a refusal, a rewrite and its report, the stamp's shift, plugin order, every failure above). A plugin's own behaviour is tested with the plugin loaded.
