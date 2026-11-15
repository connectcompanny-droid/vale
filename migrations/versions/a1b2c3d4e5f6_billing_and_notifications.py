"""billing (Stripe), day-before reminder and pause notice

Revision ID: a1b2c3d4e5f6
Revises: c66dc969e49e
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "c66dc969e49e"
branch_labels = None
depends_on = None

COLUMNS = [
    sa.Column("founder", sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column("activation_link_url", sa.Text(), nullable=True),
    sa.Column("activation_paid_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("success_link_url", sa.Text(), nullable=True),
    sa.Column("success_paid_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("pause_notified_at", sa.DateTime(timezone=True), nullable=True),
]


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # New enum value; must be committed before use, so outside the migration transaction.
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE requeststate ADD VALUE IF NOT EXISTS 'awaiting_payment' AFTER 'awaiting_documents'")
    with op.batch_alter_table("cita_requests") as batch:
        for col in COLUMNS:
            batch.add_column(col.copy())


def downgrade() -> None:
    with op.batch_alter_table("cita_requests") as batch:
        for col in reversed(COLUMNS):
            batch.drop_column(col.name)
    # Postgres cannot drop a single enum value; 'awaiting_payment' stays in the type (harmless).
