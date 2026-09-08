"""Let a project name where to go when the window runs out

Adds `projects.fallback_backend`: which agent to use when the chosen one has
reported its rate-limit window exhausted. Null means "nowhere", which is the
right default — the backends are not interchangeable, one bills a subscription
and one spends a GPU, and quietly moving work between them is a decision a
person should make rather than discover.

Nullable, and null is exactly today's behaviour.

Revision ID: 16e3b7264e02
Revises: bb845570b4dc
Create Date: 2026-09-07 20:13:22.612808

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "16e3b7264e02"
down_revision: str | None = "bb845570b4dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fallback_backend", sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("fallback_backend")
