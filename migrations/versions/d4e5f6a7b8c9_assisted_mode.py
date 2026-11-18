"""assisted mode: who books, booking kit and nudges

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
"""

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    have = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("cita_requests")}
    with op.batch_alter_table("cita_requests") as b:
        if "mode" not in have:
            b.add_column(sa.Column("mode", sa.String(10), nullable=False, server_default="self"))
        if "kit_sent_at" not in have:
            b.add_column(sa.Column("kit_sent_at", sa.DateTime(timezone=True), nullable=True))
        if "last_nudge_at" not in have:
            b.add_column(sa.Column("last_nudge_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cita_requests") as b:
        b.drop_column("last_nudge_at")
        b.drop_column("kit_sent_at")
        b.drop_column("mode")
