# Plugins

The core serves any Obsidian vault as it is (`CLAUDE.md`, "The core serves any vault"). Behaviour that follows one vault's own conventions lives in a plugin: a module under `src/plugins/<name>/` exposing one `plugin` object, a `src.plugins.Plugin`. A deployment loads plugins by naming them in `PLUGINS` (JSON list or comma-separated), in the order they run. With none, the server is the plain core.

The interface, the loader and the runner are all in `src/plugins/__init__.py`. Plugins live in this repo and change with it; there is no versioned interface, no packaging, no third-party loading.

## Hooks

A plugin fills only the hooks it needs; every field of `Plugin` but `name` is optional.

| Hook | Called by | Receives | Returns |
| --- | --- | --- | --- |
| `check` | `create_note`, `edit_note` (dry runs too), `write_file` on a `.md` | a `NoteWrite`: tool, vault path, old text (None for a new note), new text, dry run, user id | None, or a `Refusal(code, message)`: nothing is written |
| `rewrite` | the same writers | a `NoteWrite` whose `new` is the text so far | a `Rewrite(text, report)`; `report(shift, dry_run)` renders the plugin's block for the result |

When the writers call them:

- Only on a write that changes the note, and whose old and new text are readable UTF-8 (`write_file` with bytes that are not leaves the plugins out, as it leaves out the link rewrite). Not the task writers, `move_note` or `set_frontmatter`.
- `check` runs right after the core's own refusals (task lines, note names) and before any rewrite, so every plugin's check sees the caller's own text. The first refusal, in plugin order, wins. A `check` is skipped where the core's `already_exists` answer will win (an existing note for `create_note`, an existing file without `overwrite` for `write_file`).
- `rewrite` runs after the core's own rewrites (title heading, full-path links) and before the agent-edit stamp and the size cap. Each plugin gets the previous one's output; its `NoteWrite.old` stays the note's old text, so "the lines this write adds or changes" keeps meaning the caller's change. Two plugins rewriting one line: the later one sees the earlier one's result, and both reports are kept.
- The reports go after the core's own blocks, in plugin order, then any `plugin_error:` lines. `shift` is the number of lines the stamp added above the body, so a report can give the written note's line numbers. A rewrite that adds or removes lines moves the line numbers of reports made before it: a plugin with several rewrites orders them itself, as the core runs the title heading before the links.
- A refusal's code must be in `src/services/refusals.py`'s `BODY_CODES`, the catalogue every refusal is checked against: a plugin's codes are listed there.
- A hook may import the core's services (the database session, `link_paths`, the Tasks settings); there is no separate vault view.

## Failure

A plugin bug never fails a write. Each of these is set aside, the write goes ahead without it, the result gets a `plugin_error:` line naming the plugin and the step, and the server logs it at ERROR:

- a hook that raises;
- a `check` that returns something other than None or a `Refusal` with a listed code;
- a `rewrite` that returns something other than a `Rewrite`;
- a `rewrite` that removes or alters a non-blank line the caller kept from the old note (lines compared as `link_paths.changed_lines` compares them), or changes the frontmatter block. This guards against the expensive failure here, a write that clobbers a note: a plugin may change the caller's own lines, add lines and drop blank ones, nothing else. Lines are compared as a multiset, so a rewrite that moves a kept line elsewhere is not caught (accepted: no plugin moves lines).

A name in `PLUGINS` that does not load (no module, an import error, no `plugin` of type `Plugin` named like its module, a repeated name) stops the server at startup (`main._load_plugins`, first in the lifespan, sandbox mode included): running without a vault's guards by accident is worse than not starting. `Settings` checks only that each name is an identifier, because the alembic init container imports the settings.

## The plugins here

- `kelno_vault`: the conventions of the fork owner's vault, fully specific to it and not configurable. It holds no hook yet; the behaviours below are still in the core and move into it one at a time:
  - title-heading removal and full-path link rewriting (`rewrite`);
  - the hand-written task-line refusal (`check`);
  - `rule_change_unlogged`, with `RULE_FILE_PATTERNS` becoming its constant;
  - `note_names` (a name hook for `create_note`, `write_file` and `move_note`);
  - private folders (` (private)`), through an `is_private(path)` hook;
  - `open_area` and the task tools' area handling (a hook adding tools, wrapped by the core in `_tracked`);
  - the "(this server)" sections of the guide and the tool descriptions about these behaviours (a text hook), so a deployment without the plugin does not describe them.
- The agent ledger moves last into a built-in plugin, `ledger`; its tables stay in the core's migration chain.

New hooks are added with the first behaviour that needs them.

## Tests

`tests/test_plugins.py`: loading and the setting, and the runner's contract on the three writers through test-double plugins (a refusal, a rewrite and its report, the stamp's shift, plugin order, every failure above). A plugin's own behaviour is tested with the plugin loaded.
