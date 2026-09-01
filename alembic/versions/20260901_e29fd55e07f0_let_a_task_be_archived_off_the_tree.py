"""Let a task be archived off the tree

Adds `tasks.archived_at`, nullable and orthogonal to `status` — archiving a
task takes it off the project page without touching whether it is open,
done, or cancelled, and without deleting its branch, runs, or events. Null
means "on the tree", which every existing task already is.

Revision ID: e29fd55e07f0
Revises: 2cae59778400
Create Date: 2026-09-01 08:15:34.024494

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e29fd55e07f0"
down_revision: str | None = "2cae59778400"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("archived_at")
