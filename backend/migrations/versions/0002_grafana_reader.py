"""Read-only database role for Grafana.

Dashboards only need SELECT; giving them the application's credentials would let a dashboard
(or anyone who compromises it) modify telemetry and alerts. Created only on PostgreSQL and only
when GRAFANA_DB_PASSWORD is set.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""
import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

ROLE = "grafana_reader"


def upgrade() -> None:
    password = os.getenv("GRAFANA_DB_PASSWORD")
    if op.get_bind().dialect.name != "postgresql" or not password:
        return
    literal = "'" + password.replace("'", "''") + "'"   # DDL can't take bind parameters
    op.execute(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} LOGIN;
            END IF;
        END $$;""")
    op.execute(f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD {literal}")
    op.execute(f"GRANT CONNECT ON DATABASE {op.get_bind().engine.url.database} TO {ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM {ROLE};
                REVOKE USAGE ON SCHEMA public FROM {ROLE};
                DROP ROLE {ROLE};
            END IF;
        END $$;""")
