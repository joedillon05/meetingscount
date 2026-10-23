"""The founders' own back office: tasks, hours, timesheets, expenses.

Revision ID: 011_team
Revises: 010_end_dates_and_email
"""
import sqlalchemy as sa
from alembic import op

import app.models

revision = "011_team"
down_revision = "010_end_dates_and_email"
branch_labels = None
depends_on = None

UTC = app.models.UTCDateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "team_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("open", "waiting", "done", name="teamtaskstatus"), nullable=False),
        sa.Column("assignee_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_tasks_assignee_id"), nullable=True),
        sa.Column("created_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_tasks_created_by_id"), nullable=False),
        sa.Column("county_id", sa.Integer(),
                  sa.ForeignKey("counties.id", name="fk_team_tasks_county_id"), nullable=True),
        sa.Column("due_on", sa.Date(), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
        sa.Column("done_at", UTC, nullable=True),
    )
    op.create_index("ix_team_tasks_status", "team_tasks", ["status"])
    op.create_index("ix_team_tasks_assignee_id", "team_tasks", ["assignee_id"])

    op.create_table(
        "team_task_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(),
                  sa.ForeignKey("team_tasks.id", name="fk_team_task_notes_task_id"), nullable=False),
        sa.Column("author_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_task_notes_author_id"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
    )
    op.create_index("ix_team_task_notes_task_id", "team_task_notes", ["task_id"])

    op.create_table(
        "team_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_rates_user_id"), nullable=False),
        sa.Column("cents_per_hour", sa.Integer(), nullable=False),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("set_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_rates_set_by_id"), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
    )
    op.create_index("ix_team_rates_user_id", "team_rates", ["user_id"])

    # Timesheets before hours, because an hours row points at its timesheet.
    op.create_table(
        "team_timesheets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_timesheets_user_id"), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("status", sa.Enum("submitted", "approved", "returned", "paid",
                                    name="teamtimesheetstatus"), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=400), nullable=True),
        sa.Column("submitted_at", UTC, nullable=False),
        sa.Column("decided_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_timesheets_decided_by_id"), nullable=True),
        sa.Column("decided_at", UTC, nullable=True),
        sa.Column("decision_note", sa.String(length=400), nullable=True),
        sa.Column("paid_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_timesheets_paid_by_id"), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("paid_cents", sa.Integer(), nullable=True),
        sa.Column("paid_note", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_team_timesheets_user_id", "team_timesheets", ["user_id"])
    op.create_index("ix_team_timesheets_status", "team_timesheets", ["status"])

    op.create_table(
        "team_hours",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_hours_user_id"), nullable=False),
        sa.Column("worked_on", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=False),
        sa.Column("task_id", sa.Integer(),
                  sa.ForeignKey("team_tasks.id", name="fk_team_hours_task_id"), nullable=True),
        sa.Column("timesheet_id", sa.Integer(),
                  sa.ForeignKey("team_timesheets.id", name="fk_team_hours_timesheet_id"), nullable=True),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("updated_at", UTC, nullable=False),
    )
    op.create_index("ix_team_hours_user_id", "team_hours", ["user_id"])
    op.create_index("ix_team_hours_worked_on", "team_hours", ["worked_on"])
    op.create_index("ix_team_hours_timesheet_id", "team_hours", ["timesheet_id"])

    op.create_table(
        "team_expenses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spent_on", sa.Date(), nullable=False),
        sa.Column("vendor", sa.String(length=120), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("paid_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_expenses_paid_by_id"), nullable=True),
        sa.Column("created_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_expenses_created_by_id"), nullable=False),
        sa.Column("created_at", UTC, nullable=False),
        sa.Column("reimbursed_at", UTC, nullable=True),
        sa.Column("reimbursed_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_team_expenses_reimbursed_by_id"), nullable=True),
        sa.Column("reimbursed_note", sa.String(length=200), nullable=True),
    )
    op.create_index("ix_team_expenses_spent_on", "team_expenses", ["spent_on"])
    op.create_index("ix_team_expenses_paid_by_id", "team_expenses", ["paid_by_id"])


def downgrade() -> None:
    for table, indexes in (
        ("team_expenses", ("ix_team_expenses_paid_by_id", "ix_team_expenses_spent_on")),
        ("team_hours", ("ix_team_hours_timesheet_id", "ix_team_hours_worked_on", "ix_team_hours_user_id")),
        ("team_timesheets", ("ix_team_timesheets_status", "ix_team_timesheets_user_id")),
        ("team_rates", ("ix_team_rates_user_id",)),
        ("team_task_notes", ("ix_team_task_notes_task_id",)),
        ("team_tasks", ("ix_team_tasks_assignee_id", "ix_team_tasks_status")),
    ):
        for ix in indexes:
            op.drop_index(ix, table_name=table)
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS teamtimesheetstatus")
        op.execute("DROP TYPE IF EXISTS teamtaskstatus")
