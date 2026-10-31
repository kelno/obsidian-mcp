"""`list_tasks` filters, each the Tasks query line it equals (`task_query.py`).

Parity cases: each test names the Tasks query and the tasks the plugin returns
for it, from the plugin's 8.6.0 filter code.
"""

from datetime import date
import random

import pytest

from src.services import task_format as tf
from src.services import task_query as tq
from src.services.task_settings import TasksSettings

SETTINGS = TasksSettings.plugin_defaults()
REG = tf.StatusRegistry.from_settings(SETTINGS)
TODAY = date(2026, 10, 31)


def entry(path, line_number, text, global_filter=""):
    task = tf.parse_task_line(text, REG, global_filter=global_filter)
    assert task is not None, text
    return tq.Entry(path=path, line_number=line_number, task=task, text=text)


def select(lines, entries, *, sort="default", limit=None, global_filter=""):
    filters = [tq.parse_filter_line(line, TODAY, global_filter=global_filter) for line in lines]
    matched, total = tq.run_query(entries, filters, sort=sort, limit=limit, today=TODAY, rng=random.Random(1))
    return [e.task.description for e in matched], total


ENTRIES = [
    entry("projects/Area X/Area X.md", 3, "- [ ] one #area/x"),
    entry("projects/Area X/Area X.md", 4, "- [ ] two #area/x-other"),
    entry("projects/Area X/Area X.md", 5, "- [ ] three #Area/X/sub"),
    entry("Tasks inbox.md", 1, "- [ ] four #next"),
    entry("Tasks inbox.md", 2, "- [x] five #area/x ✅ 2026-10-01"),
    entry("Tasks inbox.md", 3, "- [/] six #nextweek"),
    entry("Tasks inbox.md", 4, "- [-] seven ❌ 2026-10-01"),
]


def test_tags_include_is_a_case_insensitive_substring():
    # tags include #area/x  -> one, two (substring), three (case, nested), five
    got, _ = select(["tags include #area/x"], ENTRIES)
    assert sorted(got) == ["five #area/x", "one #area/x", "three #Area/X/sub", "two #area/x-other"]


def test_tag_without_hash_matches_too():
    got, _ = select(["tags include next"], ENTRIES)
    assert sorted(got) == ["four #next", "six #nextweek"]


def test_tags_do_not_include():
    got, _ = select(["not done", "tags do not include #area"], ENTRIES)
    assert sorted(got) == ["four #next", "six #nextweek"]


def test_path_includes_is_a_case_insensitive_substring_anywhere_with_extension():
    got, _ = select(["path includes area x/"], ENTRIES)
    assert len(got) == 3
    got, _ = select(["path includes inbox.md"], ENTRIES)
    assert len(got) == 4
    got, _ = select(["path includes .md"], ENTRIES)
    assert len(got) == 7


def test_not_done_and_done_work_on_status_types():
    # not done -> todo and in progress; done -> done and cancelled
    got, _ = select(["not done"], ENTRIES)
    assert sorted(got) == ["four #next", "one #area/x", "six #nextweek", "three #Area/X/sub", "two #area/x-other"]
    got, _ = select(["done"], ENTRIES)
    assert sorted(got) == ["five #area/x", "seven"]


def test_status_type_is():
    got, _ = select(["status.type is IN_PROGRESS"], ENTRIES)
    assert got == ["six #nextweek"]
    got, _ = select(["(status.type is CANCELLED) OR (status.type is IN_PROGRESS)"], ENTRIES)
    assert sorted(got) == ["seven", "six #nextweek"]


DATED = [
    entry("a.md", 1, "- [ ] due before 📅 2026-10-30"),
    entry("a.md", 2, "- [ ] due today 📅 2026-10-31"),
    entry("a.md", 3, "- [ ] due later 📅 2026-11-30"),
    entry("a.md", 4, "- [ ] invalid 📅 2026-02-30"),
    entry("a.md", 5, "- [ ] no dates"),
    entry("a.md", 6, "- [ ] starts later 🛫 2026-11-05"),
    entry("a.md", 7, "- [ ] scheduled 2026-10-01 ⏳ 2026-10-01"),
]


def test_due_before_today_excludes_missing_and_invalid_dates():
    assert select(["due before today"], DATED)[0] == ["due before"]


def test_due_on_or_before_today():
    assert sorted(select(["due on or before today"], DATED)[0]) == ["due before", "due today"]


def test_due_on_a_date():
    assert select(["due on 2026-10-31"], DATED)[0] == ["due today"]
    assert select(["due 2026-10-31"], DATED)[0] == ["due today"]


def test_due_after():
    assert select(["due after today"], DATED)[0] == ["due later"]


def test_starts_before_counts_tasks_without_a_start_date():
    # `starts` is the one date filter that matches a missing date.
    got = select(["starts before 2026-11-05"], DATED)[0]
    assert "starts later" not in got and "no dates" in got and len(got) == 6


def test_has_and_no_dates():
    assert len(select(["has due date"], DATED)[0]) == 4
    assert len(select(["no due date"], DATED)[0]) == 3
    assert len(select(["no happens date"], DATED)[0]) == 1
    assert sorted(select(["happens before 2026-10-02"], DATED)[0]) == ["scheduled 2026-10-01"]


PRIO = [
    entry("p.md", 1, "- [ ] highest 🔺"),
    entry("p.md", 2, "- [ ] high ⏫"),
    entry("p.md", 3, "- [ ] medium 🔼"),
    entry("p.md", 4, "- [ ] none"),
    entry("p.md", 5, "- [ ] low 🔽"),
]


def test_priority_filters():
    assert select(["priority is high"], PRIO)[0] == ["high"]
    assert sorted(select(["priority is above none"], PRIO)[0]) == ["high", "highest", "medium"]
    assert select(["priority is below none"], PRIO)[0] == ["low"]
    assert len(select(["priority is not none"], PRIO)[0]) == 4


def test_description_includes_ignores_case_and_global_filter():
    es = [entry("d.md", 1, "- [ ] #task Buy Milk", global_filter="#task"),
          entry("d.md", 2, "- [ ] #task other", global_filter="#task")]
    assert select(["description includes milk"], es, global_filter="#task")[0] == ["#task Buy Milk"]
    assert select(["description includes #task"], es, global_filter="#task")[0] == []


def test_limit_applies_after_the_default_sort():
    got, total = select(["not done"], PRIO, limit=2)
    assert got == ["highest", "high"] and total == 5


def test_sort_by_due_then_default():
    got, _ = select(["has due date"], DATED, sort="due")
    assert got == ["invalid", "due before", "due today", "due later"]


def test_random_sort_is_a_permutation():
    got, _ = select([], PRIO, sort="random")
    assert sorted(got) == sorted(e.task.description for e in PRIO)


@pytest.mark.parametrize("line", [
    "due before next week",
    "sort by urgency",
    "limit 5",
    "group by path",
    "heading includes x",
    "is recurring",
    "explain",
])
def test_unsupported_query_line_is_refused(line):
    with pytest.raises(tq.QueryError):
        tq.parse_filter_line(line, TODAY)


def test_comment_lines_are_ignored_in_a_global_query():
    filters = tq.parse_global_query("# my comment\nnot done\n\n", TODAY)
    assert [f.line for f in filters] == ["not done"]


def test_query_lines_from_parameters():
    lines = tq.query_lines(
        path="projects/", status="not_done", status_types=None, tags=["next"], exclude_tags=["waiting"],
        area_tag="#area/x", due="before tomorrow", scheduled=None, starts=None, happens=None,
        priority="above none", text="milk",
    )
    assert lines == [
        "not done",
        "path includes projects/",
        "tags include #area/x",
        "tags include next",
        "tags do not include waiting",
        "due before tomorrow",
        "priority is above none",
        "description includes milk",
    ]


@pytest.mark.parametrize("value", ["2026-13-01", "soon", "", "2026-1-1"])
def test_bad_date_word_is_refused(value):
    with pytest.raises(tq.QueryError):
        tq.resolve_date(value, TODAY)


def test_date_words():
    assert tq.resolve_date("today", TODAY) == TODAY
    assert tq.resolve_date("Tomorrow", TODAY) == date(2026, 11, 1)
    assert tq.resolve_date("yesterday", TODAY) == date(2026, 10, 30)
    assert tq.resolve_date("2026-02-28", TODAY) == date(2026, 2, 28)


# --- areas --------------------------------------------------------------------

AREA_ROWS = [
    ("projects/Arrêt maladie/Arrêt maladie.md", {"area_tag": "area/arret-maladie"}),
    ("projects/Agent personnel/Agent personnel.md", {"area_tag": "area/agent-personnel"}),
    ("projects/Agent personnel/Sub/Sub.md", {"area_tag": "area/agent-sub"}),
    ("projects/Broken/Broken.md", {"area_tag": "Area/Bad Slug"}),
    ("Root area.md", {"area_tag": "area/root"}),
]


@pytest.mark.parametrize("name", ["arret-maladie", "area/arret-maladie", "#area/arret-maladie", "Arrêt maladie", "arrêt MALADIE"])
def test_area_lookup_accepts_slug_tag_or_note_name(name):
    assert tq.AreaIndex.from_rows(AREA_ROWS).lookup(name) == "area/arret-maladie"


def test_unknown_area_is_refused_naming_the_known_ones():
    with pytest.raises(tq.QueryError) as err:
        tq.AreaIndex.from_rows(AREA_ROWS).lookup("nope")
    assert "area/agent-personnel" in str(err.value)


def test_invalid_declaration_is_refused_when_named():
    with pytest.raises(tq.QueryError) as err:
        tq.AreaIndex.from_rows(AREA_ROWS).lookup("Broken")
    assert "projects/Broken/Broken.md" in str(err.value)


def test_duplicate_declaration_is_ambiguous():
    rows = AREA_ROWS + [("elsewhere/Other.md", {"area_tag": "area/arret-maladie"})]
    with pytest.raises(tq.QueryError):
        tq.AreaIndex.from_rows(rows).lookup("arret-maladie")


def test_area_from_folder_deepest_wins_and_root_notes_do_not_claim_the_vault():
    idx = tq.AreaIndex.from_rows(AREA_ROWS)
    assert idx.area_for_path("projects/Agent personnel/notes/x.md") == "area/agent-personnel"
    assert idx.area_for_path("projects/Agent personnel/Sub/deeper/y.md") == "area/agent-sub"
    assert idx.area_for_path("Tasks inbox.md") is None
    assert idx.area_for_path("elsewhere/z.md") is None


def test_area_overlap_is_named():
    rows = AREA_ROWS + [("projects/Agent/Agent.md", {"area_tag": "area/agent"})]
    idx = tq.AreaIndex.from_rows(rows)
    assert idx.overlapping("area/agent") == ["area/agent-personnel", "area/agent-sub"]
    assert idx.overlapping("area/arret-maladie") == []
