"""Where the vault states the conventions this plugin enforces.

The plugin's `get_vault_guide` sections say what the tools enforce and point
here for the convention itself, which the vault states once; its tool texts
name the conventions note. The vault's
`_/Outside dependencies.md` lists these names: renaming one of the notes or
sections means changing it here too.
"""

#: The vault's conventions note (also the note `get_vault_guide` serves after the plugin's sections).
CONVENTIONS_NOTE = "_/agent/Vault conventions.md"
#: The vault note saying where a new note, or a moved one, goes (out of the conventions note, read at its
#: moment: ledger F111); named in `create_note`'s and `move_note`'s descriptions.
FILING_NOTE = "_/agent/Where notes go.md"
#: The note on writing the vault's rules for agents.
RULES_NOTE = "_/agent/Where agent instructions go.md"

LINKS = "Links and paths"
TITLES = "Titles"
TASKS = "Tasks"
DATES = "Dates in note names"
#: In `RULES_NOTE`: a bullet's bold lead, under the heading `RULE_REFS_UNDER`, not a heading of its own.
RULE_REFS = "Ledger refs on rules"
RULE_REFS_UNDER = "Working principles"
#: In `RULES_NOTE`: the heading of how a rule is written, the pointer shapes among it.
RULE_WRITING = "How to write an instruction"
#: In `RULES_NOTE`: the heading of checking a rule against the agent paths, teaching the path tools a new way in.
RULE_PATHS = "Checking a rule against agent paths"


def see_below(section: str) -> str:
    """The guide's pointer: the conventions note follows the plugin's sections in `get_vault_guide`."""
    return f"The convention is \"{section}\" in the vault's conventions note, below (`{CONVENTIONS_NOTE}`)."
