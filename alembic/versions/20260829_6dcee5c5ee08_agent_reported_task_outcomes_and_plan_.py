"""Agent-reported task outcomes and plan decomposition

Adds `tasks.entry_phase` (what phase a task's first run should be, unset
meaning plan) and three columns on `runs`: `agent_outcome`/`outcome_detail`
(what the agent itself reported via the live outcome API, as opposed to
whether the backend process merely exited without crashing), and
`proposed_subtasks` (a plan run's structured decomposition, consumed once by
the approve route). All nullable with no default — every one of these is
meaningful only once something writes it, and a row that predates this
migration should read as "nothing reported" rather than any specific value.

Revision ID: 6dcee5c5ee08
Revises: 10e33e370c7d
Create Date: 2026-08-29 16:20:00.676884

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6dcee5c5ee08"
down_revision: str | None = "10e33e370c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "agent_outcome",
                sa.Enum(
                    "finished",
                    "failed",
                    "needs_replanning",
                    name="runoutcome",
                    native_enum=False,
                    length=20,
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("outcome_detail", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("proposed_subtasks", sa.JSON(), nullable=True))

    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "entry_phase",
                sa.Enum("plan", "execute", name="runphase", native_enum=False, length=20),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as batch_op:
        batch_op.drop_column("entry_phase")

    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("proposed_subtasks")
        batch_op.drop_column("outcome_detail")
        batch_op.drop_column("agent_outcome")
