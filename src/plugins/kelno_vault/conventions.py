"""Where the vault states the conventions this plugin enforces.

The plugin's `get_vault_guide` sections say what the tools enforce and point
here for the convention itself, which the vault states once; its tool texts
name the conventions note. The vault's
`_/Outside dependencies.md` lists these names: renaming one of the notes or
sections means changing it here too.
"""

#: The vault's conventions note (also the note `get_vault_guide` serves after the plugin's sections).
CONVENTIONS_NOTE = "_/agent/Vault conventions.md"
#: The note on writing the vault's rules for agents.
RULES_NOTE = "_/agent/Where agent instructions go.md"

LINKS = "Links and paths"
TITLES = "Titles"
TASKS = "Tasks"
DATES = "Dates in note names"
#: In `RULES_NOTE`: a bullet's bold lead, under the heading `RULE_REFS_UNDER`, not a heading of its own.
RULE_REFS = "Ledger refs on rules"
RULE_REFS_UNDER = "Working principles"


def see_below(section: str) -> str:
    """The guide's pointer: the conventions note follows the plugin's sections in `get_vault_guide`."""
    return f"The convention is \"{section}\" in the vault's conventions note, below (`{CONVENTIONS_NOTE}`)."
