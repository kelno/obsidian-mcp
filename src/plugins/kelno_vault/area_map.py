"""`open_area`'s map of an area: the notes in its folder and its subfolders, with their descriptions.

The area's folder is the main note's (`Areas/<Name>/<Name>.md` lives in
`Areas/<Name>/`). Each note directly in it is one line with its
`description` frontmatter, each direct subfolder one line with how many notes
it holds and its folder note's description (`<sub>/<sub>.md`), so an agent
sees what the area holds without opening every note. Built from the index
(`notes_metadata`), never from disk. See docs/architecture/open-area.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.services import note_description, private_folders

#: The frontmatter field read for each note's description, the one the area's own description comes from.
DESCRIPTION_FIELD = "description"
#: Notes and subfolders listed (the rest: `list_notes(folder=…)`).
OPEN_AREA_MAP_MAX = 30


def area_folder(main_note: str) -> str | None:
    """The area's folder with a trailing `/`, when the main note is its folder's folder note; else None."""
    folder, _, name = main_note.rpartition("/")
    if not folder or name.casefold() != folder.rpartition("/")[2].casefold() + ".md":
        return None
    return folder + "/"


@dataclass
class _Subfolder:
    notes: int = 0
    has_folder_note: bool = False
    description: str | None = None


@dataclass
class _Map:
    notes: list[tuple[str, str | None, bool]] = field(default_factory=list)   # (name, description, private)
    subfolders: dict[str, _Subfolder] = field(default_factory=dict)


def _gather(folder: str, main_note: str, rows) -> _Map:
    found = _Map()
    for path, raw in rows:
        if path == main_note or not path.startswith(folder):
            continue
        rel = path[len(folder):]
        head, sep, rest = rel.partition("/")
        if not sep:
            found.notes.append((rel, note_description.one_line(raw), private_folders.is_private(path)))
            continue
        sub = found.subfolders.setdefault(head, _Subfolder())
        sub.notes += 1
        if rest.casefold() == head.casefold() + ".md":
            sub.has_folder_note = True
            sub.description = note_description.one_line(raw)
    return found


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def build(folder: str, main_note: str, rows, *, list_notes_describes: bool) -> list[str] | None:
    """The map's lines, or None when the folder holds no note but the main note.

    `rows` are `(path, description frontmatter value)` for every indexed note
    under `folder`, any depth. Notes first, then subfolders, each by name; at
    most `OPEN_AREA_MAP_MAX` of them, then a line for the rest, a count of
    the notes without a description, and one line saying how to list a
    subfolder's notes. A note in a private folder is listed by name only:
    its description is note text and is not shown, as for `open_area`'s
    tasks. `list_notes_describes`: `list_notes` shows descriptions on this
    deployment (`LIST_NOTES_DESCRIPTION_FIELD` is set), so the last line can
    say so.
    """
    found = _gather(folder, main_note, rows)
    if not found.notes and not found.subfolders:
        return None
    items: list[tuple[bool, str]] = []   # (a subfolder, its line)
    undescribed = 0
    for name, description, private in sorted(found.notes, key=lambda n: (n[0].casefold(), n[0])):
        if private:
            items.append((False, f"{name} — in a private folder, description not shown"))
        elif description:
            items.append((False, f"{name} — {note_description.render(description)}"))
        else:
            undescribed += 1
            items.append((False, f"{name} — no description"))
    first_open_subfolder = None
    for name in sorted(found.subfolders, key=lambda n: (n.casefold(), n)):
        sub = found.subfolders[name]
        head = f"{name}/ — {_count(sub.notes, 'note')}"
        if private_folders.is_private(f"{folder}{name}/{name}.md"):
            items.append((True, f"{head}, a private folder: description not shown"))
            continue
        first_open_subfolder = first_open_subfolder or name
        if not sub.has_folder_note:
            items.append((True, f"{head}, no folder note ({name}/{name}.md)"))
        elif sub.description:
            items.append((True, f"{head} — {note_description.render(sub.description)}"))
        else:
            undescribed += 1
            items.append((True, f"{head}, its folder note {name}/{name}.md has no description"))

    lines = [line for _sub, line in items[:OPEN_AREA_MAP_MAX]]
    left = items[OPEN_AREA_MAP_MAX:]
    if left:
        left_folders = sum(1 for sub, _line in left if sub)
        left_notes = len(left) - left_folders
        parts = [_count(n, w) for n, w in ((left_notes, "note"), (left_folders, "subfolder")) if n]
        lines.append(f"{len(left)} more not listed ({', '.join(parts)}): list_notes(folder=\"{folder}\") lists "
                     "the area's notes.")
    if undescribed:
        lines.append(f"{_count(undescribed, 'note')} without a description: add one when you work on "
                     f"{'it' if undescribed == 1 else 'them'}.")
    if found.subfolders:
        example = first_open_subfolder or sorted(found.subfolders, key=str.casefold)[0]
        what = "a subfolder's notes with their descriptions" if list_notes_describes else "a subfolder's notes"
        lines.append(f"To list {what}: list_notes(folder=\"{folder}{example}/\"), for example.")
    return lines
