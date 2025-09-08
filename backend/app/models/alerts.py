from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Float
from sqlalchemy.sql import func
from ..database import Base

class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(String(200), unique=True, index=True)
    agent_id = Column(String(100), index=True)
    alert_type = Column(String(100), index=True)
    severity = Column(String(20))
    description = Column(Text)
    status = Column(String(20), default="active")
    first_seen = Column(DateTime(timezone=True), server_default=func.now())
    last_seen = Column(DateTime(timezone=True), onupdate=func.now())