from sqlalchemy import Column, Integer, String, DateTime, Boolean, Float, Text
from sqlalchemy.sql import func
from ..database import Base

class Agent(Base):
    __tablename__ = "agents"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(100), unique=True, index=True)
    hostname = Column(String(255))
    platform = Column(String(50))
    status = Column(String(20), default="offline")
    last_seen = Column(DateTime(timezone=True), onupdate=func.now())
    first_connected = Column(DateTime(timezone=True), server_default=func.now())

class AgentLog(Base):
    __tablename__ = "agent_logs"
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(String(100), index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    level = Column(String(20))
    message = Column(Text)