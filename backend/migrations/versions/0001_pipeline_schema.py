"""Pipeline schema: idempotent metrics hypertable, deduplicated alerts.

Replaces the create_all() schema. Pre-migration `system_metrics` / `alerts` tables (from the
single-process version) are renamed to *_legacy_v1, never dropped.

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(name: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(name)}


def upgrade() -> None:
    bind = op.get_bind()

    # --- keep pre-pipeline data out of the way instead of dropping it -------------------
    if _has_table("system_metrics") and "seq" not in _columns("system_metrics"):
        op.rename_table("system_metrics", "system_metrics_legacy_v1")
    if _has_table("alerts") and "occurrences" not in _columns("alerts"):
        op.rename_table("alerts", "alerts_legacy_v1")
    # A renamed table keeps its index/constraint/sequence names; free the ones we reuse.
    legacy_names = {
        "system_metrics_legacy_v1": ["system_metrics_pkey", "ix_system_metrics_id", "ix_system_metrics_agent_id"],
        "alerts_legacy_v1": ["alerts_pkey", "ix_alerts_id", "ix_alerts_alert_id", "ix_alerts_agent_id",
                             "ix_alerts_alert_type"],
    }
    for legacy, names in legacy_names.items():
        if not _has_table(legacy):
            continue
        for name in names:
            if bind.dialect.name == "postgresql":
                op.execute(f'ALTER INDEX IF EXISTS "{name}" RENAME TO "{name}_legacy_v1"')
            elif not name.endswith("_pkey"):
                op.execute(f'DROP INDEX IF EXISTS "{name}"')  # SQLite can't rename indexes
        if bind.dialect.name == "postgresql":
            seq = legacy.replace("_legacy_v1", "_id_seq")
            op.execute(f'ALTER SEQUENCE IF EXISTS "{seq}" RENAME TO "{legacy}_id_seq"')

    # --- unchanged tables (created only on fresh databases) -----------------------------
    if not _has_table("agents"):
        op.create_table(
            "agents",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("agent_id", sa.String(100), nullable=True),
            sa.Column("hostname", sa.String(255)),
            sa.Column("platform", sa.String(50)),
            sa.Column("status", sa.String(20)),
            sa.Column("last_seen", sa.DateTime(timezone=True)),
            sa.Column("first_connected", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_agents_id", "agents", ["id"])
        op.create_index("ix_agents_agent_id", "agents", ["agent_id"], unique=True)
    if not _has_table("agent_logs"):
        op.create_table(
            "agent_logs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("agent_id", sa.String(100)),
            sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("level", sa.String(20)),
            sa.Column("message", sa.Text),
        )
        op.create_index("ix_agent_logs_id", "agent_logs", ["id"])
        op.create_index("ix_agent_logs_agent_id", "agent_logs", ["agent_id"])

    # --- telemetry: primary key == idempotency key ----------------------------------------
    op.create_table(
        "system_metrics",
        sa.Column("agent_id", sa.String(100), primary_key=True),
        sa.Column("boot_id", sa.String(64), primary_key=True),
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("cpu_usage", sa.Float),
        sa.Column("memory_usage", sa.Float),
        sa.Column("disk_usage", sa.Float),
        sa.Column("network_latency", sa.Float),
        sa.Column("raw_data", sa.JSON),
    )
    op.create_index("ix_system_metrics_agent_ts", "system_metrics", ["agent_id", "ts"])

    # --- alerts: one active alert per (agent, type) -----------------------------------------
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("alert_id", sa.String(200)),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("alert_type", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20)),
        sa.Column("description", sa.Text),
        sa.Column("details", sa.JSON),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("occurrences", sa.Integer, nullable=False, server_default="1"),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_alerts_id", "alerts", ["id"])
    op.create_index("ix_alerts_alert_id", "alerts", ["alert_id"], unique=True)
    op.create_index("ix_alerts_agent_id", "alerts", ["agent_id"])
    op.create_index("ix_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("uq_alerts_active", "alerts", ["agent_id", "alert_type"], unique=True,
                    postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"))

    # --- TimescaleDB, when available: time partitioning + retention -------------------------
    if bind.dialect.name == "postgresql":
        available = bind.execute(sa.text(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")).scalar()
        if available:
            op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            op.execute("SELECT create_hypertable('system_metrics', 'ts', chunk_time_interval => INTERVAL '1 day')")
            # Hosted Postgres (e.g. Render) often ships TimescaleDB's Apache-licensed edition:
            # hypertables work, retention policies don't. There, set METRICS_RETENTION_HOURS and
            # the liveness leader prunes old samples instead.
            edition = bind.execute(sa.text("SELECT current_setting('timescaledb.license', true)")).scalar()
            if edition != "apache":
                op.execute("SELECT add_retention_policy('system_metrics', INTERVAL '7 days')")


def downgrade() -> None:
    op.drop_table("alerts")
    op.drop_table("system_metrics")
    if _has_table("alerts_legacy_v1"):
        op.rename_table("alerts_legacy_v1", "alerts")
    if _has_table("system_metrics_legacy_v1"):
        op.rename_table("system_metrics_legacy_v1", "system_metrics")
