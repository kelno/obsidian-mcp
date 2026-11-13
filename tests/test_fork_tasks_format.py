"""The Tasks plugin's emoji format, ported (`src/services/fork_tasks/format.py`).

Every expectation here is the behaviour of the Tasks plugin 8.6.0 source, the
version the reference deployment's vault runs. The golden lines are the
plugin's own documentation samples (docs/Reference/Task Formats/Tasks Emoji
Format.md), which its approval tests generate from the serializer, so each one
is a line the plugin itself writes: parse, write back, unchanged.
"""

from datetime import date

import pytest

from src.services.fork_tasks import format as tf
from src.services.fork_tasks.settings import TasksSettings

REGISTRY = tf.StatusRegistry.from_settings(TasksSettings.plugin_defaults())

GOLDEN = [
    # Dates
    "- [ ] #task Has a created date ➕ 2023-04-13",
    "- [ ] #task Has a scheduled date ⏳ 2023-04-14",
    "- [ ] #task Has a start date 🛫 2023-04-15",
    "- [ ] #task Has a due date 📅 2023-04-16",
    "- [x] #task Has a done date ✅ 2023-04-17",
    "- [-] #task Has a cancelled date ❌ 2023-04-18",
    # Priorities
    "- [ ] #task Lowest priority ⏬",
    "- [ ] #task Low priority 🔽",
    "- [ ] #task Normal priority",
    "- [ ] #task Medium priority 🔼",
    "- [ ] #task High priority ⏫",
    "- [ ] #task Highest priority 🔺",
    # Recurrence
    "- [ ] #task Is a recurring task 🔁 every day when done",
    # On completion
    "- [ ] #task Keep this task when done",
    "- [ ] #task Keep this task when done too 🏁 keep",
    "- [ ] #task Remove this task when done 🏁 delete",
    "- [ ] #task Remove completed instance of this recurring task when done 🔁 every day 🏁 delete",
    # Dependencies
    "- [ ] #task do this first 🆔 dcf64c",
    "- [ ] #task do this after first and some other task ⛔ dcf64c,0h17ye",
    # Every field at once, in the canonical order
    "- [ ] Everything 🆔 abc ⛔ x,y 🔼 🔁 every week 🏁 keep ➕ 2026-01-01 🛫 2026-01-02 ⏳ 2026-01-03 📅 2026-01-04 ❌ 2026-01-05 ✅ 2026-01-06 ^block-1",
    # Containers and markers the plugin reads
    "  - [ ] nested",
    "\t- [ ] tab-indented",
    "> - [ ] in a quote",
    "> > - [/] in a nested quote",
    "1. [ ] numbered",
    "12) [x] numbered with a parenthesis ✅ 2026-10-01",
    "* [ ] star marker",
    "+ [ ] plus marker",
]


@pytest.mark.parametrize("line", GOLDEN)
def test_golden_line_round_trips_unchanged(line):
    task = tf.parse_task_line(line, REGISTRY)
    assert task is not None
    assert tf.to_file_line(task) == line


def test_fields_are_read_from_the_end_in_any_order():
    task = tf.parse_task_line("- [ ] Do it 📅 2026-01-04 🔼 ⏳ 2026-01-03", REGISTRY)
    assert task.description == "Do it"
    assert task.due.text == "2026-01-04"
    assert task.scheduled.text == "2026-01-03"
    assert task.priority == tf.Priority.MEDIUM
    # The plugin writes every changed line back in its canonical order.
    assert tf.to_file_line(task) == "- [ ] Do it 🔼 ⏳ 2026-01-03 📅 2026-01-04"


def test_trailing_tags_mixed_with_fields_go_back_into_the_description():
    task = tf.parse_task_line("- [ ] Do something #tag1 📅 2026-01-04 #tag2 🛫 2026-01-01", REGISTRY)
    assert task.description == "Do something #tag1 #tag2"
    assert task.tags == ("#tag1", "#tag2")
    assert task.start.text == "2026-01-01"


def test_a_field_before_text_stays_in_the_description():
    task = tf.parse_task_line("- [ ] 📅 2026-01-04 then text", REGISTRY)
    assert task.due is None
    assert task.description == "📅 2026-01-04 then text"
    assert "field_in_description" in task.warnings


def test_alternative_symbols_and_variation_selector_are_read_and_written_canonically():
    task = tf.parse_task_line("- [ ] a ⏫️ 🗓 2026-01-04 ⌛ 2026-01-03", REGISTRY)
    assert task.priority == tf.Priority.HIGH
    assert task.due.text == "2026-01-04"
    assert task.scheduled.text == "2026-01-03"
    assert tf.to_file_line(task) == "- [ ] a ⏫ ⏳ 2026-01-03 📅 2026-01-04"


def test_hard_break_spaces_are_kept_single_trailing_space_is_not():
    two = tf.parse_task_line("- [ ] a 📅 2026-01-04   ", REGISTRY)
    assert tf.to_file_line(two) == "- [ ] a 📅 2026-01-04   "
    one = tf.parse_task_line("- [ ] a ", REGISTRY)
    assert tf.to_file_line(one) == "- [ ] a"


def test_invalid_date_is_kept_as_text_and_flagged():
    task = tf.parse_task_line("- [ ] a 📅 2026-02-30", REGISTRY)
    assert task.due.text == "2026-02-30"
    assert task.due.value is None
    assert "invalid_date" in task.warnings


def test_unknown_on_completion_value_is_dropped_by_the_parser_and_flagged():
    # The plugin's extractField removes `🏁 foo` and parseOnCompletionValue
    # answers Ignore, so a rewrite by the plugin would lose it.
    task = tf.parse_task_line("- [ ] a 🏁 foo", REGISTRY)
    assert task.on_completion == ""
    assert task.description == "a"
    assert "unknown_on_completion" in task.warnings


def test_block_link_is_split_off_and_written_last():
    task = tf.parse_task_line("- [ ] a ^abc-1 ", REGISTRY)
    assert task.block_link == " ^abc-1"
    assert task.description == "a"


@pytest.mark.parametrize("line", [
    "- \\[ ] escaped legacy checkbox",
    "-\t[ ] tab after the marker",
    "-[ ] no space after the marker",
    "- [] empty brackets",
    "plain text [ ] not a list item",
    "- plain list item",
    "",
])
def test_not_a_task_line(line):
    assert tf.parse_task_line(line, REGISTRY) is None


def test_tags_are_read_like_the_plugin():
    task = tf.parse_task_line("- [ ] see http://x/y#frag #area/agent-personnel #next, done", REGISTRY)
    # `#frag` has no whitespace before it; the comma ends `#next`.
    assert task.tags == ("#area/agent-personnel", "#next")


def test_global_filter_line_without_it_is_not_a_task_and_it_is_removed_from_tags():
    assert tf.parse_task_line("- [ ] a #other", REGISTRY, global_filter="#task") is None
    task = tf.parse_task_line("- [ ] #task a #other", REGISTRY, global_filter="#task")
    assert task.tags == ("#other",)
    assert task.description == "#task a #other"


# --- statuses -----------------------------------------------------------------

OWNER_STATUSES = TasksSettings.from_mapping({
    "statusSettings": {
        "coreStatuses": [
            {"symbol": " ", "name": "Todo", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "TODO"},
            {"symbol": "x", "name": "Done", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "DONE"},
        ],
        "customStatuses": [
            {"symbol": "/", "name": "In Progress", "nextStatusSymbol": "x", "availableAsCommand": True, "type": "IN_PROGRESS"},
            {"symbol": "-", "name": "Cancelled", "nextStatusSymbol": " ", "availableAsCommand": True, "type": "CANCELLED"},
        ],
    },
})
OWNER = tf.StatusRegistry.from_settings(OWNER_STATUSES)


@pytest.mark.parametrize("symbol,stype,done", [
    (" ", "TODO", False),
    ("x", "DONE", True),
    ("/", "IN_PROGRESS", False),
    ("-", "CANCELLED", True),
    # Not registered: the plugin's createUnknownStatus gives type TODO, so an
    # upper-case X is *not done* for this owner.
    ("X", "TODO", False),
    ("?", "TODO", False),
])
def test_status_type_comes_from_the_registry(symbol, stype, done):
    task = tf.parse_task_line(f"- [{symbol}] a", OWNER)
    assert task.status.type == stype
    assert task.is_done is done


def test_unknown_status_is_named_unknown():
    assert tf.parse_task_line("- [?] a", OWNER).status.name == "Unknown"


TODAY = date(2026, 10, 31)


def _status(line, symbol, settings=OWNER_STATUSES, today=TODAY):
    task = tf.parse_task_line(line, OWNER)
    return [tf.to_file_line(t) for t in tf.apply_status(task, OWNER.by_symbol_or_create(symbol), today, settings)]


def test_done_adds_done_date():
    assert _status("- [ ] a", "x") == ["- [x] a ✅ 2026-10-31"]


def test_done_date_not_added_when_setting_off():
    off = TasksSettings.from_mapping({**OWNER_STATUSES.raw, "setDoneDate": False})
    assert _status("- [ ] a", "x", settings=off) == ["- [x] a"]


def test_leaving_done_removes_done_date():
    assert _status("- [x] a ✅ 2026-10-01", " ") == ["- [ ] a"]


def test_cancel_adds_cancelled_date_and_keeps_order():
    assert _status("- [ ] a 📅 2026-11-01", "-") == ["- [-] a 📅 2026-11-01 ❌ 2026-10-31"]


def test_done_to_cancelled_swaps_the_dates():
    assert _status("- [x] a ✅ 2026-10-01", "-") == ["- [-] a ❌ 2026-10-31"]


def test_in_progress_has_no_date():
    assert _status("- [ ] a", "/") == ["- [/] a"]


def test_same_status_changes_nothing():
    task = tf.parse_task_line("- [x] a ✅ 2026-10-01", OWNER)
    out = tf.apply_status(task, OWNER.by_symbol_or_create("x"), TODAY, OWNER_STATUSES)
    assert out == [task]


def test_on_completion_delete_removes_the_line():
    assert _status("- [ ] a 🏁 delete", "x") == []


def test_on_completion_delete_does_not_fire_on_cancel():
    assert _status("- [ ] a 🏁 delete", "-") == ["- [-] a 🏁 delete ❌ 2026-10-31"]


def test_on_completion_keep_keeps_the_line():
    assert _status("- [ ] a 🏁 keep", "x") == ["- [x] a 🏁 keep ✅ 2026-10-31"]


def test_recurring_task_status_change_is_refused():
    task = tf.parse_task_line("- [ ] a 🔁 every day 📅 2026-10-31", OWNER)
    with pytest.raises(tf.RecurrenceUnsupported):
        tf.apply_status(task, OWNER.by_symbol_or_create("x"), TODAY, OWNER_STATUSES)


def test_toggle_follows_next_status_symbol():
    task = tf.parse_task_line("- [/] a", OWNER)
    assert OWNER.next_status(task.status).symbol == "x"


# --- urgency and the default sort ---------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("- [ ] none", 1.95),
    ("- [ ] low 🔽", 0.0),
    ("- [ ] lowest ⏬", -1.8),
    ("- [ ] medium 🔼", 3.9),
    ("- [ ] high ⏫", 6.0),
    ("- [ ] highest 🔺", 9.0),
    # due today: daysOverdue 0 -> multiplier 14*0.8/21+0.2
    ("- [ ] due today 📅 2026-10-31", (14 * 0.8 / 21 + 0.2) * 12 + 0.325 * 6),
    ("- [ ] due long ago 📅 2026-01-01", 12 + 0.325 * 6),
    ("- [ ] due far ahead 📅 2027-01-01", 0.2 * 12 + 0.325 * 6),
    ("- [ ] scheduled today ⏳ 2026-10-31", 5 + 0.325 * 6),
    ("- [ ] scheduled tomorrow ⏳ 2026-11-01", 0.325 * 6),
    ("- [ ] starts tomorrow 🛫 2026-11-01", -3 + 0.325 * 6),
    ("- [ ] started today 🛫 2026-10-31", 0.325 * 6),
])
def test_urgency_matches_the_plugin(line, expected):
    task = tf.parse_task_line(line, OWNER)
    assert tf.urgency(task, TODAY) == pytest.approx(expected)


def test_default_sort_order():
    lines = [
        ("b.md", 1, "- [x] done ✅ 2026-10-01"),
        ("b.md", 2, "- [ ] plain"),
        ("b.md", 3, "- [/] in progress"),
        ("a.md", 1, "- [ ] high ⏫"),
        ("a.md", 2, "- [ ] due soon 📅 2026-11-01"),
        ("a.md", 3, "- [-] cancelled ❌ 2026-10-01"),
        ("a.md", 4, "- [ ] plain too"),
    ]
    tasks = [(p, n, tf.parse_task_line(text, OWNER)) for p, n, text in lines]
    ordered = sorted(tasks, key=lambda item: tf.default_sort_key(item[2], item[0], item[1], TODAY))
    assert [t.description for _, _, t in ordered] == [
        "in progress",       # status type group 1
        "due soon",          # todo, highest urgency
        "high",
        "plain too",         # equal urgency: path a.md before b.md
        "plain",
        "done",              # status type group 4
        "cancelled",         # group 5
    ]


def test_due_sort_puts_invalid_first_and_missing_last():
    a = tf.parse_task_line("- [ ] a 📅 2026-02-30", OWNER)
    b = tf.parse_task_line("- [ ] b 📅 2026-01-01", OWNER)
    c = tf.parse_task_line("- [ ] c", OWNER)
    keys = sorted([(tf.due_sort_key(t), t.description) for t in (c, b, a)])
    assert [d for _, d in keys] == ["a", "b", "c"]
