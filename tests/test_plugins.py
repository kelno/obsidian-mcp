"""The plugin hooks: loading `PLUGINS`, and what the note writers do with a plugin's check and rewrite.

Offline: the vault is a tmp dir and usage logging is stubbed. The plugins here
are small test doubles patched in as `plugins.active()`; the texts written
carry no wikilink, task line or ledger ref, so no database is touched.
"""
import asyncio
import json

import pytest
from pydantic import ValidationError

from src import main, plugins
from src.auth.session import current_user_id
from src.config import Settings
from src.mcp_server import tools
from src.mcp_server.auth import current_permission
from src.services import vault as vault_service


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools.settings, "multi_user_mode", False)
    monkeypatch.setattr(tools.settings, "write_precondition_required", False)
    monkeypatch.setattr(tools.settings, "last_ai_edit_field", None)
    vault_service.clear_user_vault_cache()

    async def no_log(*args, **kwargs):
        pass

    monkeypatch.setattr(tools, "_log_usage", no_log)
    permission = current_permission.set("readwrite")
    user = current_user_id.set(None)
    yield tmp_path
    current_user_id.reset(user)
    current_permission.reset(permission)


#: A non-placeholder SECRET_KEY, which `Settings` requires.
SECRET = "0123456789abcdef0123456789abcdef"


def use(monkeypatch, *loaded):
    monkeypatch.setattr(plugins, "active", lambda: loaded)


def note(root, rel, text):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(text.encode("utf-8"))


def read(root, rel):
    return (root / rel).read_bytes().decode("utf-8")


def text_of(result):
    return result if isinstance(result, str) else str(result)


def code(result):
    last = str(getattr(result, "error", None) or result).rsplit("\n", 1)[-1]
    assert last.startswith("MCP-REFUSAL "), result
    return json.loads(last.removeprefix("MCP-REFUSAL "))["code"]


async def refuse_shouting(write):
    if "SHOUT" in write.new:
        return plugins.Refusal("validation_failed", "No shouting in this vault.")
    return None


async def sign(write):
    """Appends a line, reporting it with the line shift it is given."""
    signed = write.new.rstrip("\n") + "\n-- signed\n"
    return plugins.Rewrite(signed, lambda shift, dry: f"\n\nsigned: shift {shift}, dry {dry}")


async def boom(write):
    raise RuntimeError("plugin bug")


# --- loading ------------------------------------------------------------------


def test_this_vaults_plugin_loads():
    (loaded,) = plugins.load(("kelno_vault",))
    assert loaded.name == "kelno_vault" and loaded.rewrite is not None


@pytest.mark.parametrize("names,message", [
    (("no_such_plugin",), "could not be imported"),
    (("kelno_vault", "kelno_vault"), "twice"),
])
def test_a_name_that_does_not_load_is_refused(names, message):
    with pytest.raises(plugins.PluginLoadError, match=message):
        plugins.load(names)


def test_the_server_does_not_start_with_a_plugin_that_does_not_load(monkeypatch):
    monkeypatch.setattr(main.settings, "plugins", ["no_such_plugin"])
    plugins.active.cache_clear()
    try:
        with pytest.raises(SystemExit):
            main._load_plugins()
    finally:
        plugins.active.cache_clear()


@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("kelno_vault", ["kelno_vault"]),
    (" a , b ,", ["a", "b"]),
    ('["a", "b"]', ["a", "b"]),
])
def test_plugins_setting(raw, expected):
    assert Settings(plugins=raw, secret_key=SECRET, _env_file=None).plugins == expected


def test_plugins_setting_refuses_a_name_that_is_no_module_name():
    with pytest.raises(ValidationError, match="not a plugin name"):
        Settings(plugins="kelno-vault", secret_key=SECRET, _env_file=None)


# --- check ---------------------------------------------------------------------


def test_a_refusal_writes_nothing_in_every_note_writer(vault, monkeypatch):
    use(monkeypatch, plugins.Plugin("p", check=refuse_shouting))
    assert code(asyncio.run(tools.create_note_impl("a.md", "SHOUT\n"))) == "validation_failed"
    assert not (vault / "a.md").exists()
    note(vault, "b.md", "calm\n")
    assert code(asyncio.run(tools.edit_note_impl("b.md", "SHOUT\n"))) == "validation_failed"
    assert code(asyncio.run(tools.edit_note_impl("b.md", "SHOUT\n", dry_run=True))) == "validation_failed"
    assert read(vault, "b.md") == "calm\n"
    assert code(asyncio.run(tools.write_file_impl("c.md", "SHOUT\n", encoding="text"))) == "validation_failed"
    assert not (vault / "c.md").exists()


def test_a_check_is_not_asked_about_a_write_that_changes_nothing(vault, monkeypatch):
    use(monkeypatch, plugins.Plugin("p", check=refuse_shouting))
    note(vault, "b.md", "SHOUT\n")
    assert "SHOUT" in read(vault, "b.md")
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "SHOUT\n")))
    assert "MCP-REFUSAL" not in result


def test_a_check_that_raises_lets_the_write_through_and_says_so(vault, monkeypatch):
    use(monkeypatch, plugins.Plugin("p", check=boom))
    result = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n"
    assert "plugin_error: the server's `p` plugin failed in its check step" in result


def test_a_refusal_with_an_unlisted_code_lets_the_write_through(vault, monkeypatch):
    async def odd(write):
        return plugins.Refusal("made_up_code", "No.")

    use(monkeypatch, plugins.Plugin("p", check=odd))
    result = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n" and "returned something unusable" in result


def test_the_first_refusal_wins_and_later_checks_see_the_callers_text(vault, monkeypatch):
    seen = []

    async def watch(write):
        seen.append(write.new)
        return None

    use(monkeypatch, plugins.Plugin("w", check=watch, rewrite=sign), plugins.Plugin("p", check=refuse_shouting))
    assert code(asyncio.run(tools.create_note_impl("a.md", "SHOUT\n"))) == "validation_failed"
    assert seen == ["SHOUT\n"]


# --- rewrite -------------------------------------------------------------------


def test_a_rewrite_is_written_and_reported_in_every_note_writer(vault, monkeypatch):
    use(monkeypatch, plugins.Plugin("p", rewrite=sign))
    created = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n-- signed\n" and "signed: shift 0, dry False" in created
    note(vault, "b.md", "old\n")
    dry = text_of(asyncio.run(tools.edit_note_impl("b.md", "new\n", dry_run=True)))
    assert "+-- signed" in dry and "signed: shift 0, dry True" in dry and read(vault, "b.md") == "old\n"
    asyncio.run(tools.edit_note_impl("b.md", "new\n"))
    assert read(vault, "b.md") == "new\n-- signed\n"
    asyncio.run(tools.write_file_impl("c.md", "text\n", encoding="text"))
    assert read(vault, "c.md") == "text\n-- signed\n"


def test_the_report_gets_the_lines_the_stamp_added(vault, monkeypatch):
    monkeypatch.setattr(tools.settings, "last_ai_edit_field", "last_ai_edit")
    use(monkeypatch, plugins.Plugin("p", rewrite=sign))
    created = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md").startswith("---\nlast_ai_edit:")
    assert "signed: shift 3" in created


def test_rewrites_run_in_plugin_order_on_each_others_output(vault, monkeypatch):
    async def upper(write):
        return plugins.Rewrite(write.new.upper())

    use(monkeypatch, plugins.Plugin("a", rewrite=sign), plugins.Plugin("b", rewrite=upper))
    asyncio.run(tools.create_note_impl("a.md", "text\n"))
    assert read(vault, "a.md") == "TEXT\n-- SIGNED\n"


def test_a_rewrite_that_raises_is_set_aside(vault, monkeypatch):
    use(monkeypatch, plugins.Plugin("p", rewrite=boom))
    result = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n" and "failed in its rewrite step" in result


@pytest.mark.parametrize("bad", [
    lambda new: new.replace("kept line", "changed"),        # alters a line the caller kept
    lambda new: new.replace("kept line\n", ""),             # removes one
    lambda new: new.replace("a: 1", "a: 2"),                # touches the frontmatter
])
def test_a_rewrite_outside_the_callers_change_is_set_aside(vault, monkeypatch, bad):
    async def overreach(write):
        return plugins.Rewrite(bad(write.new))

    use(monkeypatch, plugins.Plugin("p", rewrite=overreach))
    note(vault, "b.md", "---\na: 1\n---\nkept line\nold\n")
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "new", find="old")))
    assert read(vault, "b.md") == "---\na: 1\n---\nkept line\nnew\n"
    assert "tried to change text the write did not touch" in result


@pytest.mark.parametrize("bad", [
    lambda new: new.replace("  - nested", "- nested"),       # re-indents a kept line
    lambda new: new.replace("hard break  ", "hard break"),   # trims a kept line's trailing spaces
])
def test_the_guard_compares_kept_lines_exactly(vault, monkeypatch, bad):
    async def overreach(write):
        return plugins.Rewrite(bad(write.new))

    use(monkeypatch, plugins.Plugin("p", rewrite=overreach))
    before = "- top\n  - nested\nhard break  \nold\n"
    note(vault, "b.md", before)
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "new", find="old")))
    assert read(vault, "b.md") == before.replace("old", "new") and "plugin_error" in result


def test_the_guard_reads_a_lone_cr_note_line_by_line(vault, monkeypatch):
    async def truncate(write):
        return plugins.Rewrite(write.new.split("\r")[-2] + "\r")

    use(monkeypatch, plugins.Plugin("p", rewrite=truncate))
    note(vault, "b.md", "---\ra: 1\r---\rkept\rold\r")
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "new", find="old")))
    assert read(vault, "b.md") == "---\ra: 1\r---\rkept\rnew\r" and "plugin_error" in result


def test_a_rewrite_adding_frontmatter_is_set_aside(vault, monkeypatch):
    async def add_block(write):
        return plugins.Rewrite("---\n" + write.new)

    use(monkeypatch, plugins.Plugin("p", rewrite=add_block))
    note(vault, "b.md", "kept\n---\nold\n")
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "new", find="old")))
    assert read(vault, "b.md") == "kept\n---\nnew\n" and "plugin_error" in result


def test_bytes_that_are_not_utf8_do_not_switch_the_checks_off(vault, monkeypatch):
    import base64

    use(monkeypatch, plugins.Plugin("p", check=refuse_shouting))
    payload = base64.b64encode(b"SHOUT \xff\n").decode()
    assert code(asyncio.run(tools.write_file_impl("c.md", payload))) == "validation_failed"
    assert not (vault / "c.md").exists()


def test_a_report_returning_none_says_nothing_and_a_bad_one_does_not_fail_the_write(vault, monkeypatch):
    async def quiet(write):
        return plugins.Rewrite(write.new, lambda shift, dry: None)

    async def odd(write):
        return plugins.Rewrite(write.new, lambda shift, dry: 42)

    use(monkeypatch, plugins.Plugin("q", rewrite=quiet))
    assert "plugin" not in text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    use(monkeypatch, plugins.Plugin("o", rewrite=odd))
    result = text_of(asyncio.run(tools.create_note_impl("b.md", "text\n")))
    assert read(vault, "b.md") == "text\n" and "report on this write failed" in result


def test_a_refusal_with_a_code_of_the_wrong_type_lets_the_write_through(vault, monkeypatch):
    async def odd(write):
        return plugins.Refusal(["validation_failed"], "No.")

    use(monkeypatch, plugins.Plugin("p", check=odd))
    result = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n" and "returned something unusable" in result


def test_a_rewrite_may_change_the_callers_lines_and_drop_blank_ones(vault, monkeypatch):
    async def tidy(write):
        return plugins.Rewrite(write.new.replace("new", "NEW").replace("\n\n", "\n"))

    use(monkeypatch, plugins.Plugin("p", rewrite=tidy))
    note(vault, "b.md", "kept line\n\nold\n")
    result = text_of(asyncio.run(tools.edit_note_impl("b.md", "new", find="old")))
    assert read(vault, "b.md") == "kept line\nNEW\n" and "plugin_error" not in result


def test_without_plugins_nothing_changes(vault, monkeypatch):
    use(monkeypatch)
    result = text_of(asyncio.run(tools.create_note_impl("a.md", "text\n")))
    assert read(vault, "a.md") == "text\n" and "plugin" not in result


# --- text: tool descriptions and the guide ---------------------------------------


DOC = "First line.\n\n    Para one.\n\n    {plugin_text}\n\n    Args:\n        x: y\n"


def test_describe_puts_each_plugins_text_in_place_of_the_marker(monkeypatch):
    use(monkeypatch, plugins.Plugin("a", tool_text={"t": "From a."}), plugins.Plugin("b", tool_text={"t": "From b."}))
    assert plugins.describe("t", DOC) == "First line.\n\n    Para one.\n\n    From a.\n\n    From b.\n\n    Args:\n        x: y\n"


def test_describe_without_text_leaves_no_trace(monkeypatch):
    use(monkeypatch, plugins.Plugin("a", tool_text={"other": "Elsewhere."}))
    assert plugins.describe("t", DOC) == "First line.\n\n    Para one.\n\n    Args:\n        x: y\n"


def test_describe_with_a_plugin_that_does_not_load_leaves_the_refusal_to_the_lifespan(monkeypatch):
    def broken():
        raise plugins.PluginLoadError("no such plugin")

    monkeypatch.setattr(plugins, "active", broken)
    assert plugins.describe("t", DOC) == "First line.\n\n    Para one.\n\n    Args:\n        x: y\n"


def test_describe_needs_exactly_one_marker():
    with pytest.raises(ValueError, match="exactly once"):
        plugins.describe("t", "No marker here.")


def test_guide_text_joins_the_plugins_sections(monkeypatch):
    use(monkeypatch, plugins.Plugin("a", guide_text="## A (this server)\n\nText."), plugins.Plugin("b"))
    assert plugins.guide_text() == "\n\n## A (this server)\n\nText."


def test_the_served_descriptions_hold_no_marker_and_no_plugin_rule_without_the_plugin():
    """The suite runs with PLUGINS empty: the plain core describes no vault convention."""
    from src.mcp_server import server

    for name in ("create_note", "edit_note", "write_file"):
        doc = getattr(server, name).__doc__ or ""
        assert plugins.TOOL_TEXT_MARKER not in doc and "title_heading_removed" not in doc, name
    assert "Titles (this server)" not in tools._VAULT_GUIDE_PRIMER


def test_this_vaults_plugin_describes_its_rule_on_every_writer():
    (loaded,) = plugins.load(("kelno_vault",))
    assert set(loaded.tool_text) == {"create_note", "edit_note", "write_file"}
    assert all("title_heading_removed" in text for text in loaded.tool_text.values())
    assert loaded.guide_text.startswith("## Titles (this server)")


def test_without_this_vaults_plugin_a_title_heading_is_written_as_sent(vault, monkeypatch):
    use(monkeypatch)
    asyncio.run(tools.create_note_impl("Dog food.md", "# Dog food\n\nKibble.\n"))
    assert read(vault, "Dog food.md") == "# Dog food\n\nKibble.\n"
