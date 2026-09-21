"""
Record which login a run used.

Adds a nullable `runs.login` column to store the login string that ran the task;
null means the backend's default login.

Revision ID: 3a5c7e1d9b4f
Revises: 16e3b7264e02
Create Date: 2026-09-21 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3a5c7e1d9b4f"
down_revision: str | None = "16e3b7264e02"
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("login", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("login")
