"""Equity alongside cash on a team rate, and the valuation it is measured against.

Revision ID: 013_team_equity
Revises: 012_team_invoice
"""
import sqlalchemy as sa
from alembic import op

revision = "013_team_equity"
down_revision = "012_team_invoice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("team_rates") as t:
        t.add_column(sa.Column("equity_cents_per_hour", sa.Integer(), nullable=False, server_default="0"))
        t.add_column(sa.Column("valuation_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("team_rates") as t:
        t.drop_column("valuation_cents")
        t.drop_column("equity_cents_per_hour")
