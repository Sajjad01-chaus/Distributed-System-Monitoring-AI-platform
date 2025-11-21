from sqlalchemy import Column, Integer, String, Float, DateTime, JSON
from sqlalchemy.sql import func
from ..database import Base

class SystemMetrics(Base):
    __tablename__ = "system_metrics"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(100), index=True, nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    raw_data = Column(JSON)