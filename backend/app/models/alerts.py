from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Text, text
from sqlalchemy.sql import func

from ..database import Base


class Alert(Base):
    """At most one *active* alert per (agent, type): repeats bump `occurrences`/`last_seen`.
    The partial unique index makes that hold even with several detect workers racing."""
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(String(200), unique=True, index=True)
    agent_id = Column(String(100), index=True, nullable=False)
    alert_type = Column(String(100), index=True, nullable=False)
    severity = Column(String(20))
    description = Column(Text)
    details = Column(JSON)
    status = Column(String(20), default="active", nullable=False)
    occurrences = Column(Integer, default=1, nullable=False)
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("uq_alerts_active", "agent_id", "alert_type", unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
    )
