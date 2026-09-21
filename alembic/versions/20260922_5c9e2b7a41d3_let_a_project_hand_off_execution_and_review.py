"""Let a project hand off execution and review

Adds `projects.execute_agent` — who carries out an approved plan, when it
should not be the agent that wrote it — and `projects.review_agent`, who
rechecks finished work before a pull request opens. Both nullable, and null
is exactly how every project behaved before: the planner executes, nobody
reviews.

Revision ID: 5c9e2b7a41d3
Revises: 4a7b3c2d1e6f
Create Date: 2026-09-22 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c9e2b7a41d3"
down_revision: str | None = "4a7b3c2d1e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("execute_agent", sa.String(length=250), nullable=True))
        batch_op.add_column(sa.Column("review_agent", sa.String(length=250), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("review_agent")
        batch_op.drop_column("execute_agent")
