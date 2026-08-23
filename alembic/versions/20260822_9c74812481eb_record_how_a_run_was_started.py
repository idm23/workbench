"""Record how a run was started

Adds `executor` and `handle` to runs, and drops `pid`.

`pid` assumed the answer to "how do I stop this" was always a process id. With
one systemd unit per run it is a unit name, and on a remote node later it will
be something else again — so the pair that replaces it is deliberately shaped
like `backend`/`resume_token`: a name for the mechanism, and an opaque handle
that means nothing without it.

`executor` is NOT NULL and therefore carries a server default. Autogenerate
emitted it without one, which works against the empty table this schema has
today and fails the moment staging restores a snapshot with real rows in it.

Revision ID: 9c74812481eb
Revises: 6bd6c351d9c7
Create Date: 2026-08-22 18:46:35.307349

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c74812481eb"
down_revision: str | None = "6bd6c351d9c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "executor",
                sa.String(length=50),
                nullable=False,
                # Any row that predates this column was started by the only
                # mechanism that existed then: a plain child process.
                server_default="local-process",
            )
        )
        batch_op.add_column(sa.Column("handle", sa.String(length=200), nullable=True))
        batch_op.drop_column("pid")


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pid", sa.INTEGER(), nullable=True))
        batch_op.drop_column("handle")
        batch_op.drop_column("executor")
