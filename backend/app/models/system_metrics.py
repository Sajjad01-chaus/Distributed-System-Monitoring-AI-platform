from sqlalchemy import JSON, BigInteger, Column, DateTime, Float, Index, String
from sqlalchemy.sql import func

from ..database import Base


class SystemMetrics(Base):
    """One telemetry sample. The primary key is the message's idempotency key: redelivered or
    duplicated messages collide on it and are dropped by INSERT ... ON CONFLICT DO NOTHING.
    `ts` is part of the key because TimescaleDB requires it in every unique index."""
    __tablename__ = "system_metrics"

    agent_id = Column(String(100), primary_key=True)
    boot_id = Column(String(64), primary_key=True)     # changes when the agent restarts
    seq = Column(BigInteger, primary_key=True)          # monotonic per (agent_id, boot_id)
    ts = Column(DateTime(timezone=True), primary_key=True)   # agent-reported sample time
    received_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    cpu_usage = Column(Float)
    memory_usage = Column(Float)
    disk_usage = Column(Float)
    network_latency = Column(Float)
    raw_data = Column(JSON)

    __table_args__ = (Index("ix_system_metrics_agent_ts", "agent_id", "ts"),)
