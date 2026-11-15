"""website chat messages

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

import sqlalchemy as sa
from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("web_messages"):
        return  # created by AUTO_CREATE_TABLES on a local install
    op.create_table(
        "web_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_key", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("body_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_web_messages_user_key", "web_messages", ["user_key"])
    op.create_index("ix_web_messages_created_at", "web_messages", ["created_at"])


def downgrade() -> None:
    op.drop_table("web_messages")
