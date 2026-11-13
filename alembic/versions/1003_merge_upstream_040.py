"""Join the fork's migrations with upstream's 031 to 040 (resync 2026-11-13).

The fork's own migrations (1001 `fork_note_tasks`, 1002 the agent ledger)
are a branch of their own from 030, beside upstream's 031 onward; this merge
revision makes them one head again. It changes nothing in the database. See
docs/architecture/schema-and-migrations.md, "Fork migrations".

Revision ID: 1003
Revises: 040, 1002
Create Date: 2026-11-13
"""
from typing import Sequence, Union

revision: str = "1003"
down_revision: Union[str, Sequence[str], None] = ("040", "1002")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
