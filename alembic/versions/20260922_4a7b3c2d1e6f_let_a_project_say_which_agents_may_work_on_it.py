"""
Let a project say which agents may work on it.

Adds a nullable `projects.allowed_agents` JSON column to store the list of
agents allowed to work on a project. Null or empty means any agent may
run it.

Revision ID: 4a7b3c2d1e6f
Revises: 3a5c7e1d9b4f
Create Date: 2026-09-22 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4a7b3c2d1e6f"
down_revision: str | None = "3a5c7e1d9b4f"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed_agents", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("allowed_agents")
