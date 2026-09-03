"""Add runs.seed_message

Lets a new `phase=conversation` run resuming a task's session start with
something specific rather than the generic "someone is here" of
`continuation_prompt` — free text (the Discuss dialog) or one of the two
canned shortcuts (Split, Check CI). Nullable with no default: unset means
"no seed", which is exactly how every conversation started before this.

Revision ID: a7036a6337d2
Revises: e29fd55e07f0
Create Date: 2026-09-03 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7036a6337d2"
down_revision: str | None = "e29fd55e07f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seed_message", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("seed_message")
