"""Self-serve profiles and Stripe subscriptions.

Revision ID: 008_self_serve_billing
Revises: 007_meeting_directory
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "008_self_serve_billing"
down_revision = "007_meeting_directory"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("counties") as t:
        t.add_column(sa.Column("self_serve", sa.Boolean(), nullable=False,
                               server_default=sa.false()))

    op.create_table(
        "signups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=True),
        sa.Column("per_week", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("starts_on", UTC, nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("expires_at", UTC, nullable=False),
        sa.Column("used_at", UTC, nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_signups_user_id"),
                  nullable=True),
    )
    op.create_index("ix_signups_phone", "signups", ["phone"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("county_id", sa.Integer(),
                  sa.ForeignKey("counties.id", name="fk_subscriptions_county_id"), nullable=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_subscriptions_user_id"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
        sa.Column("price_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="unpaid_yet"),
        sa.Column("current_period_end", UTC, nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_event_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
    )
    op.create_index("ix_subscriptions_county_id", "subscriptions", ["county_id"])
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=True)
    op.create_index("ix_subscriptions_stripe_customer_id", "subscriptions",
                    ["stripe_customer_id"], unique=True)
    op.create_index("ix_subscriptions_stripe_subscription_id", "subscriptions",
                    ["stripe_subscription_id"], unique=True)


def downgrade() -> None:
    for ix in ("ix_subscriptions_stripe_subscription_id", "ix_subscriptions_stripe_customer_id",
               "ix_subscriptions_user_id", "ix_subscriptions_county_id"):
        op.drop_index(ix, table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_signups_phone", table_name="signups")
    op.drop_table("signups")
    with op.batch_alter_table("counties") as t:
        t.drop_column("self_serve")
