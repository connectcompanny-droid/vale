"""appointments clients book themselves (reminders only)

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("external_appointments"):
        return
    op.create_table(
        "external_appointments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("what", sa.String(120), nullable=False),
        sa.Column("starts_at", sa.String(60), nullable=False),
        sa.Column("place", sa.String(200), nullable=False, server_default=""),
        sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_external_appointments_user_id", "external_appointments", ["user_id"])


def downgrade() -> None:
    op.drop_table("external_appointments")
