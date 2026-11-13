# Task tools: the Tasks plugin's format, read and written server-side

> Read before touching `list_tasks`, `create_task`, `edit_task`, `set_task_status`, `src/services/fork_tasks/`, `fork_note_tasks` or migration 1001. The write tools are on the destructive-write surface: [vault tools](vault-tools.md) applies to them in full.

Four tools let an agent read and change the to-dos in the vault, written in the [Obsidian Tasks plugin](https://publish.obsidian.md/tasks/)'s emoji format (`- [ ] Call the bank ⏫ 📅 2026-11-05`). Fork-only (see `FORK.md`).

## The requirements this design answers to

Set by the owner, and they outweigh any technical choice below:

1. **Use the plugin's own interface rather than imitating it.** The format is the plugin's documented emoji format, and each tool is mapped to the official command or API function it mirrors (table below).
2. **It works with the owner's PC off.** Everything is server-side: no running Obsidian is needed or contacted.
3. **Same question, same answer as the plugin in Obsidian.** `list_tasks` returns the tasks a Tasks query block asking the same question returns; the write tools write the lines the plugin itself would write.
4. **Writes never guess.** Every write is checked against the line and the note's hash it was based on, and refused if either changed.
5. **Recurrence is out of scope for now.** A `🔁` rule is read and returned as text; changing a recurring task is refused, so a task is never ticked without its next occurrence.

## Where the code is

| File | What it holds |
| --- | --- |
| `src/services/fork_tasks/format.py` | The port of the plugin's parser, serializer, status change, urgency and default sort. Pure. |
| `src/services/fork_tasks/scan.py` | Which lines of a note are task lines, and which only look like one (problems). |
| `src/services/fork_tasks/settings.py` | Reading the plugin's `data.json` from the vault. |
| `src/services/fork_tasks/query.py` | `list_tasks`' filters, each one a Tasks query line, and areas. |
| `src/services/fork_tasks/ops.py` | What a write does to a note's text, without I/O; "today". |
| `src/services/fork_tasks/index.py` | Writing `fork_note_tasks` rows: the pass and the tools share it. |
| `src/mcp_server/tools.py` (end), `server.py`, `task_result.py` | The four tools and their structured results. |
| `alembic/versions/1001_fork_note_tasks.py` | The table. |

## Ported from the plugin's source, not its documentation

The plugin's documentation does not say enough to reproduce it exactly (field order on write, what happens to trailing tags, which status types count as done, the default sort). So `fork_tasks/format.py` is a function-by-function port of the plugin's TypeScript at **version 8.6.0**, the version installed in the reference vault, with each function naming what it mirrors (`DefaultTaskSerializer.deserialize`, `Task.handleNewStatus`, `Urgency.calculate`, `Sort.defaultSorters`, …). JavaScript semantics that differ from Python's are reproduced on purpose: ECMAScript whitespace for `\s` and `trim()`, `.` not matching U+2028/U+2029, ASCII-only `\d`, first-match `replace`.

**When the plugin is upgraded**, re-read these files in its source and compare: `src/Task/TaskRegularExpressions.ts`, `src/TaskSerializer/DefaultTaskSerializer.ts`, `src/Layout/TaskLayoutOptions.ts` (field order), `src/Task/Task.ts` (`fromLine`, `handleNewStatus`), `src/Task/OnCompletion.ts`, `src/Task/Urgency.ts`, `src/Query/Sort/Sort.ts`, `src/Statuses/StatusRegistry.ts`, `src/Query/Filter/*` for the filters in `fork_tasks/query.py`, `src/ui/EditableTask.ts` and `src/Commands/CreateOrEditTaskParser.ts` (the dialog).

What the port says that is easy to get wrong:

- **Fields are read from the end of the line**, in a loop (at most 20 rounds), in this order per round: priority, ✅, ❌, 📅 (also 📆 🗓), ⏳ (also ⌛), 🛫, ➕, 🔁, 🏁, a trailing `#tag` (put back into the description), 🆔, ⛔. A field written before ordinary text stays in the description as text (reported as the warning `field_in_description`).
- **Every plugin write re-serializes the whole line** in its fixed order — description, 🆔, ⛔, priority, 🔁, 🏁, ➕, 🛫, ⏳, 📅, ❌, ✅, block link — whatever order the line was in, with two or more trailing spaces (a Markdown hard break) kept. So do these tools: after `edit_task` or `set_task_status`, a line whose fields were out of order comes back in canonical order. That is the plugin's own behaviour, not a side effect of ours.
- **An unregistered status symbol is type TODO** (`createUnknownStatus`), so `[X]` is not done unless `X` is set up as a status.
- **`done` means DONE, CANCELLED or NON_TASK**; `not done` the rest.
- **A done date is added only when the status type becomes DONE from another type**, kept while it stays DONE, removed when it leaves DONE; the same for ❌ and CANCELLED; each only when its setting is on.
- **`🏁 delete` removes the line** only when the type becomes DONE from another type (not on cancel).

## Official counterparts

| Tool | Mirrors | How parameters map |
| --- | --- | --- |
| `list_tasks` | Obsidian CLI `tasks` | `path=`/`file=` → `path`; `todo`/`done`/`status=` → `status`, `status_types`; `verbose` → `ref`; `format=json` → the structured result. Tags, dates, priority, text, sort and limit have no CLI counterpart; each is the Tasks query line it equals. The plugin's API has no search. |
| `create_task` | Tasks API `createTaskLineModal()` | The dialog's fields → arguments; the line written is the dialog's `toFileLineString()`. No CLI counterpart. |
| `edit_task` | Tasks API `editTaskLineModal(line)` | Same line in, same line out (`EditableTask.applyEdits`), with the status unchanged. No CLI counterpart. |
| `set_task_status` | Obsidian CLI `task ref=<path:line> done\|todo\|status=<char>`; plugin `handleNewStatusWithRecurrenceInUsersOrder` | `ref` → `path` + `line`; `done`/`todo` → `status`; `status=<char>` → `symbol`. `executeToggleTaskDoneCommand(line, path)` is the same change with the next status in the cycle. |

The API functions run inside Obsidian (the dialogs need its window), so they are the reference for behaviour, not something the server calls. If Obsidian ever runs on the server, this table says what could be swapped in.

## Which lines are tasks

The plugin reads only list items Obsidian's own parser marks as tasks, then applies its line pattern (indentation of spaces, tabs or `>`; a marker `-` `*` `+` `1.` `1)`; one or more spaces, not tabs; `[c]` with any one character). Obsidian's parser is not open source, so `fork_tasks/scan.py` rebuilds the part of its decision that matters:

- frontmatter is skipped, through the repo's one partition;
- fenced code is skipped, through the repo's one fence grammar (`links.scan_fences`), never a private one. Its inline-code masking is not used, since a task may hold inline code;
- an escaped `- \[ ] text` matches nothing and is not a problem: the vault's legacy checkboxes were converted to that form on purpose;
- what might or might not be a task is a **problem** row with a reason, reported by `list_tasks` and never dropped: `inside_comment` (`%% … %%`), `inside_html_block` (CommonMark types 1–6), `inside_math_block` (`$$ … $$`), `inside_frontmatter` (a line-1 block whose fences close but whose YAML does not load: still frontmatter to Obsidian), `indented_outside_list` (4+ columns outside a list: code or paragraph text in CommonMark), `unusual_indentation` (NBSP or another non-space whitespace before the marker), `no_space_after_checkbox` (`- [ ]text`), `line_separator_in_line` (a U+2028 / U+2029 inside the line: the plugin reads only what comes before it, so a rewrite would drop the rest), and the near misses `whitespace_after_marker` (a tab or NBSP between marker and box), `no_space_before_checkbox` (`-[ ]`), `empty_checkbox` (`- []`); plus the bounds `line_too_long` and `rows_capped`.

**To check once in Obsidian** (by eye; the plugin has no query API): put each case in a note with the query `not done` and confirm what it lists. If Obsidian disagrees on one, it becomes a declared divergence here.

| Case | The tools say |
| --- | --- |
| `- [ ] a` inside `%%` … `%%` | problem `inside_comment` |
| `- [ ] a` inside `<div>` … `</div>` (no blank line) | problem `inside_html_block` |
| `    - [ ] a` after a blank line, no list above | problem `indented_outside_list` |
| `\t- [ ] a` at the top of a note | problem `indented_outside_list` |
| `- [ ]text` | problem `no_space_after_checkbox` |
| `- [ ] a` between `$$` lines | problem `inside_math_block` |
| `- [ ] a` preceded by an NBSP | problem `unusual_indentation` |
| `- [ ] a` inside a frontmatter block whose YAML does not parse | problem `inside_frontmatter` |
| `- [ ] a` in a callout `> - [ ] a` | task |
| `- [ ]` with nothing after | task |
| a task after an indented fence opener nothing closes | task, but every write to the note is refused |

## The owner's settings

Read from the vault on every call (`TASKS_PLUGIN_SETTINGS_PATH`, default `.obsidian/plugins/obsidian-tasks-plugin/data.json`), through the root's anchored descriptor with no symlink followed; never written. Used: global filter, global query, created/done/cancelled date settings, statuses (core then custom, the first per symbol), use-filename-as-scheduled-date and its format and folders. An absent file means the plugin's own defaults, said in every `list_tasks` result (`settings.source`). A file that is not JSON, has a field of the wrong type, or sets a format other than the emoji format refuses every task tool (`task_settings_invalid`). A file-name date format other than `YYYY-MM-DD` / `YYYYMMDD` is refused the same way.

**The global filter**: a line without it is not a task at all; it is removed from the tag list; `create_task` puts it in front of the description, as the dialog does; `edit_task` puts it back in front when the dialog would. In both, whole-word copies of it in the `description` argument are dropped first (T12), so it is written once. **The global query**: applied to every `list_tasks` call, line by line, through the same parser as the tool's own filters; one line the tools cannot apply refuses the call, naming it.

## `list_tasks`

Each argument is one Tasks query line (the table is in `fork_tasks/query.py`), and the result's `query` field is the whole block, so the same question can be pasted into Obsidian. `tags include` is a case-insensitive substring of any tag (`#area/x` matches `#area/x-y`); `path includes` a case-insensitive substring anywhere in the path, `.md` included; dates follow `DateField` (a missing date matches only `starts`; an invalid one never compares); `happens` is any of start, scheduled and due. Tags in a note's frontmatter do not count: the plugin's tags are the line's.

The default sort is the plugin's (status type, urgency, due, priority, path), so "the first five" agree; `limit` applies after it.

**Private folders** (fork, F63, owner's decisions 2026-11-12 and 2026-11-13, P98): with private folders configured (`PRIVATE_FOLDER_NAMES`, one definition for every tool: "Private folders" in [search](search.md)), the content of their notes stays out of what agents come across, and a query not limited to a path returns tasks from the whole vault. So a task in such a note is listed by location only (`ref`, `path`, `line`, `status_type`, `private: true`), a problem without `text`, a stale task without `indexed_text`, and a `notes` line says why, as `open_area` and the ledger's citing tasks do. `include_private=true` lists them in full; naming a private folder in `path` no longer does (it did until P98: one mechanism, the argument, everywhere). Without the setting nothing is private and the argument does not exist. Filters still apply to the hidden text, so a `text` match shows that a private task contains the word: accepted, as for the searches' count. The write tools act on a note the agent names, so their results keep the line.

**Freshness**: the index finds the candidates; then each returned task's note is read from disk (at most `limit` notes), the line compared, and the note's real `content_hash` returned. A line that changed since the index read it goes to `stale`, never returned as current. The hash is the wire digest of the raw bytes (vault-tools L6: not `notes_metadata.content_hash`).

## The write tools

Each one runs the repo's write ladder unchanged — `_require_write`, `_precondition_syntax_error`, `open_mutable`, `_leaf_state_error`, the bounded read, `_precondition_error`, `_note_size_error`, `_confirmed_publication(write_file_at(…, expected=…))` — so the anchored-write guarantees, the two windows (`stale_precondition`, `concurrent_write`) and the vault-assignment confirmation all hold as for `edit_note`. On top:

- `edit_task` and `set_task_status` require `expected_hash` (a required argument: without it the call is a protocol argument error) and `expected_line`; the line at `line` must equal it (`line_mismatch`) and must be a task the plugin reads (`not_a_task`).
- **Only that line changes, one line is added, or (`🏁 delete`) that line is removed.** Every other byte is copied, terminators included (CRLF stays CRLF). Removing the last line of a note with no final terminator also removes the terminator before it, as the plugin's `lines.splice(…).join('\n')` does.
- **The written line is read back** before anything is published: the new text is scanned again, the line must be a task there and parse to exactly the intended task. This catches a description ending in something the plugin reads as a field, and a task appended inside an unclosed code fence.
- **Every other line must read exactly as before.** The note is scanned before and after the write, and every other task or problem line must keep its kind, reason and text at its (shifted) number. A written line holding an unpaired `%%` is refused outright, since it would open an Obsidian comment hiding the rest of the note even where no task follows.
- **A line the plugin would damage is refused**, not rewritten: an invalid date (the plugin writes `Invalid date`), an unknown `🏁` value (its parser drops it), the same field given twice (`duplicate_field`: the plugin keeps one and drops the other).
- **A note with an indented fence opener nothing closes is refused**, as `edit_note(section=…)` refuses it: whether the lines below are code cannot be decided.
- **Recurring tasks**: `set_task_status` and `edit_task` refuse a task with a `🔁` rule (`recurring_task`), telling the agent to tick or edit it in Obsidian.

**Task lines are written by the task tools only** (fork, owner's decision, 2026-11-12; the `kelno_vault` plugin's `check`, `src/plugins/kelno_vault/task_lines.py`, since 2026-11-13: without that plugin the note writers don't check task lines). `edit_note` (every mode, `dry_run` included), `create_note` and `write_file` on a `.md` refuse, with `task_line_by_hand` and nothing written, a write whose new text holds a task line the old text did not hold as it is. A task line is what the index pass would store as one: a line `task_scan.scan_note` finds (outside fences and frontmatter) that `task_ops.parse_line`, under the owner's Tasks settings and global filter, reads as a task. Old and new are compared as a multiset of such lines, without surrounding whitespace, so deleting a task line or moving one unchanged within the note passes; a changed line is a removal plus an addition, and is refused. Bytes that are not UTF-8 are read leniently, so they cannot switch the check off. The check runs before the agent-edit stamp, the size cap and the dry-run diff, after the core's note-name refusal; like every plugin check, one that raises is set aside with a `plugin_error:` line and the write goes ahead ([plugins](plugins.md)); it skips the scan when the global filter does not occur in the new text, and lets the write through (logged at debug) when the Tasks settings cannot be read or applied (a file-name date format the task tools don't implement), or when the vault has no Tasks settings file (the plugin's defaults have no global filter, which would make every checkbox a task in a vault that may not use the plugin). Not checked, so that the existing answer wins: `create_note` or `write_file(overwrite=False)` on an existing note (`already_exists`), and a `write_file` overwrite whose old text could not be read (every task in it would look new). Not covered: `move_note`'s link rewrites (the server's own change, inside task lines too), `import_from_url`, and uploads (`request_upload` to a `.md`). Limitations found by the adversarial review (2026-11-12), accepted: lines the scanner reports as problems rather than tasks (`- [ ]#task`, a BOM or NBSP lead, a task inside an HTML block) pass, as they do in `list_tasks`; tasks past the scanner's 10,000 rows or longer than 10,000 characters are not compared; closing a fence that hid task lines makes them new tasks and is refused (delete and recreate them with `create_task`); and no tool moves a task to another note (recreate it there, then delete the old line). The refusal names up to ten lines and the task tool to use. Accepted with the move into the plugin (adversarial review, 2026-11-13): a bug inside the check now lets the write through with `plugin_error:` instead of failing it, and a plugin listed before `kelno_vault` whose check refuses wins over `task_line_by_hand`.

**Ledger citations.** After the write, the three tools read the written line (for a `🏁 delete` removal, the removed one) for agent-ledger citations (`[F12]`, `[Q14, P3]`) and return `ledger_refs`: each cited entry, any kind, as it stands now (kind, title, status, a quirk's certainty, the day it last changed) with the state of every task citing it (the written note counted from the text just published, so even when the immediate re-index could not run); a bare `F12` naming an existing entry gets a note. A write that closes a task also says, in `notes`, what that means for each cited entry (ledger F51: feedback whose tasks are now all done is to be closed as `handled`; see "Task links" in [agent ledger](agent-ledger.md)). A failure there is a note, never a failed write. The task format itself is untouched: the citation is ordinary description text. See [agent ledger](agent-ledger.md), "Task links".

`create_task` writes the dialog's line: `-` marker, no indent, status todo unless given, ➕ today if `setCreatedDate`, area tags appended to the description. Placement: after the last non-blank line of the `heading` section's own lines — above its first subheading, so a task added under `## Tasks` never lands under `### Done` — (the repo's section resolver, over the frontmatter-stripped body), or of the note. A scheduled date the plugin takes from the file name (`useFilenameAsScheduledDate`) is not on the line and is left out of the read-back comparison. `expected_hash` is optional there.

## Areas

An area's tag is declared once, in its main note's frontmatter: `area_tag: area/<slug>` (owner's decision). The tools read the declarations from the index (`notes_metadata.frontmatter`). `list_tasks(area=…)` resolves a slug, a tag or the main note's name to `tags include #area/<slug>`; the folder never counts. `create_task(areas=…)` writes `#area/<slug>` tags; with `areas` omitted, a note inside an area's folder (the main note's folder; the deepest wins; a main note at the vault root claims nothing) gets that area's tag. An unknown, invalid or doubly declared area is refused, never turned into a new tag.

## Dates

"Today" is the owner's local date in `TASKS_TIMEZONE` (default `Europe/Brussels`), never the server clock's day; the server refuses to start if the zone cannot be loaded. The image's own timezone database is used (the Python base image carries one); no extra package. Date arguments are `YYYY-MM-DD`, `today`, `tomorrow` or `yesterday`; the plugin's natural-language dates are not accepted, so an agent never depends on a parser it cannot see.

## The index

`fork_note_tasks` (migration 1001): one row per task line or problem of an indexed note — `note_id` (cascades with the note), `line_number` (1-based over the whole file), `kind`, the exact `line`, `reason`. The pass writes a changed or moved note's rows next to its links, in the same transaction, each note's insert in its own poison savepoint (site `fork-tasks`), a skip withholding the re-derive stamp exactly as a link skip does. `CURRENT_EXTRACTION_VERSION` 4 derived every existing note's rows once; it shares the version-3 cleaner, so nothing was re-embedded. Owner scoping is through `notes_metadata.user_id`, as for `note_links`.

## Choices made by the agent that built this (not requirements)

Recorded so they are never mistaken for the owner's rules; each can be changed.

1. **Rows store a line's text, not its meaning.** Status types, the global filter, file-name dates and "today" come from the settings and the clock, so `list_tasks` parses at query time; a settings change needs no reindex.
2. **Immediate re-index of the written note's task rows only.** After a write, the tool replaces that note's `fork_note_tasks` rows from the bytes it just published, in its own transaction under the index generation lock, waiting at most 5 s (`IMMEDIATE_INDEX_WAIT_SECONDS`); the note's other derived data waits for the next pass. The result says `indexed` and why not. **Until that pass, the note row's `notes_metadata.content_hash` lags the file.** No write precondition ever uses it: every write compares the caller's `expected_hash` with the hash of the bytes it reads from disk in that call, `list_tasks` returns the hash of the bytes it reads from disk, and a write returns the hash of the bytes it published. A note not yet in the index (a new inbox) gets its rows at the next pass. Change 2 (a file watcher with per-path passes) is where the tools would switch to a real per-note pass.
3. **`list_tasks` verifies returned lines on disk** (freshness, above).
4. **`create_task` creates a missing note only when it is the inbox** (`TASKS_INBOX_PATH`) with no heading, so a typo does not create a note.
5. **`create_task`'s `expected_hash` is optional**; it only adds a line, and the in-call compare covers its own read-to-write window.
6. **`edit_task` refuses recurring tasks too**, because the plugin rewrites the rule through its own parser on save (`Recurrence.toText()`), which is not ported.
7. **`list_tasks` defaults to `status="not_done"`**, shown in `query`; the plugin's own default is no filter.
8. **Writing 🆔 / ⛔ / 🔁 is not offered** (read only): the plugin's dialog edits other tasks' lines for dependencies, and recurrence is out of scope.
9. **Statuses by name** (`todo`, `done`, `cancelled`, `in_progress`) mean the first registered status of that type; any other is by `symbol`; an unregistered symbol is refused (the plugin would invent an Unknown status).
10. **A global query is applied only when every line is one the tools implement**; otherwise `list_tasks` refuses.
11. **A description holding a field emoji is refused** on create (and on edit, unless the line already had one), because the plugin reads it as text, which is almost always an agent mistake.
12. **Writes refuse what changes another line's reading** (`verify_other_lines`), and an unpaired `%%` in a written line, though the plugin itself would write both.
13. **`area` overlap is reported, not refused**: `tags include #area/x` also matches `#area/x-y` (the plugin's meaning); `list_tasks` names the overlapping declared tags in `notes`.
14. **Path sorting approximates JS `localeCompare`** (case-insensitive, numeric runs as numbers) without ICU.

## Declared divergences and accepted limitations

| # | What | Why accepted |
| --- | --- | --- |
| T1 | Obsidian's list-item parser is rebuilt, not run: the uncertain shapes are problems (above) until checked in Obsidian. | Its parser is not available server-side. Uncertain lines are reported, never silently counted or dropped. |
| T2 | `random` sort is the server's shuffle, not the plugin's `sort by random`. | By nature; said in the result's `notes`. |
| T3 | Path tie-break order can differ from ICU collation for names differing only in accents or punctuation. | Fifth tie-breaker; no ICU dependency. |
| T4 | The index can be one pass behind the disk, and a device's unsynced edits are invisible. `stale` covers returned lines whose text changed; a task added on disk since the last pass is not listed until that pass, and a task whose text and line number are unchanged but which has since been wrapped in a code fence or `%%` is still returned until that pass (a write to it is refused, since writes re-scan the note). | Two copies of the vault; change 2 narrows it. |
| T5 | A pass that read a note just before a task write and commits after it writes the older task rows back; the next pass corrects them. A write based on them is refused by its hash check meanwhile. | Same staleness class as the index today; the generation lock prevents interleaved rows. |
| T6 | A line holding U+0000 is stored without it (the indexer's decode point) and so always reads as `stale`. | Vanishingly rare; never served as current. |
| T7 | A year `0000` date is invalid here and valid to moment.js. | No real task has one. |
| T8 | CommonMark HTML block type 7 (any complete tag alone on a line) is not recognised. | Cannot interrupt a paragraph; rare in notes. |
| T9 | Recurrence is not implemented: recurring tasks are read but not changed. | Owner's decision for the first version. |
| T10 | A task write to a note the index does not know yet (the inbox, just created by `create_task`) cannot update the task rows at once: they hang off the note's `notes_metadata` row, which only the index pass creates. The result says `indexed: false` with the reason, and the task is listed after the next pass. | Creating the note's row outside the pass would bypass the indexer's ordering and stale-row rules; it only affects the first write to a new note. Change 2 narrows it to seconds. Found in the live check, 2026-11-01. |
| T11 | Syncthing conflict copies (`*.sync-conflict-*`) are not indexed, so their tasks are never listed; the Tasks plugin in Obsidian lists them like any note. | They are stale duplicates whose tasks showed up as real ones (live check, 2026-11-01); the walk skips them like dot-files (`indexer.is_sync_conflict_copy`), and they stay readable by path. |
| T12 | A `description` argument holding the global filter as a whole word loses it before the tools put it in front, so `#task a` and `a #task` both write `- [ ] #task a`; typed into the plugin's dialog, the same text would be saved with the filter twice. A description that is only the filter is refused. Whole word only: `#tasks`, `#task/x` or `#task,` is kept. Accepted: a copy meant as text mid-sentence is dropped too (`rename tag #task to #todo` → `#task rename tag to #todo`), as the dialog would drop it from a person's text. | The dialog hides the filter, so a person never types it; an agent does, usually by passing back `list_tasks`' `description`, and wrote `#task #task` lines twice in live use (2026-11-03, 2026-11-04). |

## Refusal codes added

`line_mismatch`, `not_a_task`, `recurring_task`, `task_settings_invalid` (`src/services/refusals.py` `BODY_CODES`); the rest are the repo's existing codes (`stale_precondition`, `concurrent_write`, `invalid_argument`, `content_unsafe`, `not_found`, `selector_unresolved`, …).
