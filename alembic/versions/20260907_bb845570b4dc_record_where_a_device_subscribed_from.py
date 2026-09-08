"""Record where a device subscribed from

Adds `device_subscriptions.site_url`, the address the browser was on when it
subscribed. Used as the VAPID `sub` claim — the contact URL a push service is
entitled to — because Apple rejects a token whose `sub` it does not consider a
valid URL, and the server cannot work out its own public address: it binds
loopback and is published by a reverse proxy it was never told about.

Nullable, and null keeps the previous behaviour of a `mailto:` fallback.

Revision ID: bb845570b4dc
Revises: de98c5c7e78d
Create Date: 2026-09-07 19:49:49.800827

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bb845570b4dc"
down_revision: str | None = "de98c5c7e78d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("device_subscriptions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("site_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_subscriptions", schema=None) as batch_op:
        batch_op.drop_column("site_url")
