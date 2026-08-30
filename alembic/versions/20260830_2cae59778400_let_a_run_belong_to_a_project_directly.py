"""Let a run belong to a project directly

Adds `runs.project_id` and drops the NOT NULL on `runs.task_id`, so a run
can belong to exactly one of the two — a task-scoped plan/execute attempt as
before, or an open-ended project conversation with neither task nor
worktree. Every other run table (`run_events`, `run_inputs`) already keys
off `run_id` alone and needs no change.

Revision ID: 2cae59778400
Revises: b5fc356e63c3
Create Date: 2026-08-30 12:26:56.516412

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2cae59778400"
down_revision: str | None = "b5fc356e63c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        batch_op.alter_column("task_id", existing_type=sa.INTEGER(), nullable=True)
        batch_op.create_index(batch_op.f("ix_runs_project_id"), ["project_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_runs_project_id_projects"),
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_runs_project_id_projects"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_runs_project_id"))
        batch_op.alter_column("task_id", existing_type=sa.INTEGER(), nullable=False)
        batch_op.drop_column("project_id")
