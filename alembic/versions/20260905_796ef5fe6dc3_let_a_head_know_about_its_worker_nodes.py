"""Let a head know about its worker nodes

Adds `nodes`: one row per machine that lends this one a capability, written by
that machine at the end of its install and on every deploy rather than typed in
here. `addresses` is an ordered list — LAN first, tailnet second — because
which route works is a property of where you are asking from, and
`last_good_address` records which one actually answered so the common case is
one connection rather than a walk down the list.

Nothing existing changes, and an install with no nodes behaves exactly as it
did: an empty table reads as "this machine serves its own inference, or none".

Revision ID: 796ef5fe6dc3
Revises: e29fd55e07f0
Create Date: 2026-09-05 14:19:40.612765

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "796ef5fe6dc3"
down_revision: str | None = "e29fd55e07f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "nodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("addresses", sa.JSON(), nullable=False),
        sa.Column("last_good_address", sa.String(length=200), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("gpu", sa.String(length=200), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_nodes")),
    )
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_nodes_name"), ["name"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("nodes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_nodes_name"))

    op.drop_table("nodes")
