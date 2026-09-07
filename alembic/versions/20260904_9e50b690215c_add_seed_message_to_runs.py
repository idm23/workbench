"""Add seed_message to runs

Adds `runs.seed_message`, nullable and read once by the runner when it
prepares a conversation run. `RunPhase.CONVERSATION` already exists and is
stored as a bare string (`native_enum=False`, no CHECK constraint), so a
seeded conversation needs no phase migration — only somewhere to keep the
message a person typed (or a canned shortcut) until the runner turns it into
the first prompt of the resumed session.

Revision ID: 9e50b690215c
Revises: e29fd55e07f0
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e50b690215c"
down_revision: str | None = "e29fd55e07f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("seed_message", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_column("seed_message")
