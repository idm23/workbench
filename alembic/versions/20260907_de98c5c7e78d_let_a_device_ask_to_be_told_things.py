"""Let a device ask to be told things

Adds `device_subscriptions`: one row per browser that has agreed to be
notified, holding the Web Push endpoint and the two keys that encrypt a payload
only that browser can read. Written by the device itself when someone presses
"Enable notifications" there, which is the only place a subscription can be
created — a browser mints it, and nothing here can do so on its behalf.

`enabled` rather than deleting, because silencing a device remotely is possible
and re-subscribing it remotely is not. Nothing existing changes, and an install
with no subscriptions sends nothing.

Revision ID: de98c5c7e78d
Revises: 796ef5fe6dc3
Create Date: 2026-09-07 14:57:30.539532

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "de98c5c7e78d"
down_revision: str | None = "796ef5fe6dc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("p256dh", sa.String(length=200), nullable=False),
        sa.Column("auth", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("event_kinds", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_device_subscriptions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_subscriptions")),
    )
    with op.batch_alter_table("device_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_device_subscriptions_endpoint"), ["endpoint"], unique=True
        )
        batch_op.create_index(
            batch_op.f("ix_device_subscriptions_user_id"), ["user_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("device_subscriptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_device_subscriptions_user_id"))
        batch_op.drop_index(batch_op.f("ix_device_subscriptions_endpoint"))

    op.drop_table("device_subscriptions")
