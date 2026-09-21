"""Row-level security so one county cannot read another's data.

Application code already filters on county_id (see auth.scope_to_county). This
migration makes the database enforce it too, so a forgotten WHERE clause is a
bug rather than a breach. Postgres only; skipped on SQLite for local dev.

The app connects as a non-superuser role and sets `app.county_id` per request
(and `app.is_superuser` for support access). Wire that up in db.py when you
move to Postgres:

    SET LOCAL app.county_id = '<the signed-in user county>';

Revision ID: 002_rls
Revises: e891626cccec
"""
from alembic import op

revision = "002_rls"
down_revision = "e891626cccec"
branch_labels = None
depends_on = None

TABLES = ["programs", "participants", "requirements", "meetings", "checkins",
          "reviews", "disputes", "audit_log"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY {t}_tenant_isolation ON {t}
            USING (
                county_id = NULLIF(current_setting('app.county_id', true), '')::int
                OR current_setting('app.is_superuser', true) = 'on'
            )
        """)
    # The audit log is append-only: no UPDATE or DELETE policy is ever created,
    # so those operations are refused for every non-superuser role.
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for t in TABLES:
        op.execute(f"DROP POLICY IF EXISTS {t}_tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
