"""Add run_inputs for typing into a live run

A message typed into a run while it is still going is written to two
places: `run_events` (kind `input`, no migration needed there — the enum has
no DB-level constraint) for display, and this table, which the runner polls
as a queue of what still needs delivering to the backend. Mirrors
`run_events` exactly, including the `(run_id, seq)` unique constraint the
runner's poll resumes from.

Revision ID: b5fc356e63c3
Revises: 6dcee5c5ee08
Create Date: 2026-08-30 11:05:19.222618

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b5fc356e63c3"
down_revision: str | None = "6dcee5c5ee08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_inputs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_run_inputs_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_inputs")),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_inputs_run_id_seq"),
    )


def downgrade() -> None:
    op.drop_table("run_inputs")
