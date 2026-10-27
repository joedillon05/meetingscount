"""The invoice sent for a timesheet: its number and payment link.

Revision ID: 012_team_invoice
Revises: 011_team
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "012_team_invoice"
down_revision = "011_team"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    with op.batch_alter_table("team_timesheets") as t:
        t.add_column(sa.Column("invoice_ref", sa.String(length=60), nullable=True))
        t.add_column(sa.Column("invoice_url", sa.String(length=500), nullable=True))
        t.add_column(sa.Column("invoiced_at", UTC, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("team_timesheets") as t:
        t.drop_column("invoiced_at")
        t.drop_column("invoice_url")
        t.drop_column("invoice_ref")
