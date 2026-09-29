from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from ..database import Base

class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(100), unique=True, index=True)
    hostname = Column(String(255))
    platform = Column(String(50))
    status = Column(String(20), default="offline")
    # When telemetry was last received; set only by the persist worker. (No onupdate: any other
    # UPDATE of the row, e.g. marking it offline, must not look like fresh data.)
    last_seen = Column(DateTime(timezone=True))
    first_connected = Column(DateTime(timezone=True), server_default=func.now())

class AgentLog(Base):
    __tablename__ = "agent_logs"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(100), index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    level = Column(String(20))
    message = Column(Text)