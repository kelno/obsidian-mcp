"""The conventions of kelno's vault, as a plugin (`PLUGINS=["kelno_vault"]`).

Fully specific to that vault and not configurable: a different vault writes
its own plugin rather than adapting this one. It holds conventions only (folder
names, naming and linking rules), never the vault's content.

Its behaviours move here from the core one at a time; see
docs/architecture/plugins.md.
"""

from src.plugins import Plugin

plugin = Plugin(name="kelno_vault")
