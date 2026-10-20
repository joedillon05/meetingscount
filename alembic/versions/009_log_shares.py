"""A participant's log, shared by text.

Revision ID: 009_log_shares
Revises: 008_self_serve_billing
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "009_log_shares"
down_revision = "008_self_serve_billing"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "log_shares",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(),
                  sa.ForeignKey("counties.id", name="fk_log_shares_county_id"), nullable=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_log_shares_user_id"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("revoked_at", UTC, nullable=True),
        sa.Column("last_opened_at", UTC, nullable=True),
        sa.Column("opens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_log_shares_county_id", "log_shares", ["county_id"])
    op.create_index("ix_log_shares_user_id", "log_shares", ["user_id"])
    op.create_index("ix_log_shares_token_hash", "log_shares", ["token_hash"], unique=True)


def downgrade() -> None:
    for ix in ("ix_log_shares_token_hash", "ix_log_shares_user_id", "ix_log_shares_county_id"):
        op.drop_index(ix, table_name="log_shares")
    op.drop_table("log_shares")
