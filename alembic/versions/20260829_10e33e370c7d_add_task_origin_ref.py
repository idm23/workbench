"""Add task origin_ref

Adds `tasks.origin_ref`: what a task's branch should be created from — "main",
"staging", or another task's own branch — chosen when the first run is
started rather than assumed. Nullable with no default, because "unset" is a
real, meaningful state: a task nobody has made a choice for yet, not one that
silently inherited main.

Revision ID: 10e33e370c7d
Revises: 9c74812481eb
Create Date: 2026-08-29 15:23:42.372617

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "10e33e370c7d"
down_revision: str | None = "9c74812481eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("origin_ref", sa.String(length=300), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("origin_ref")
