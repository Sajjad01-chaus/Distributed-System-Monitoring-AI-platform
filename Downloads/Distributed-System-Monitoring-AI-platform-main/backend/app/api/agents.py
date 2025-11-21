from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Agent

router = APIRouter()

@router.get("/")
async def list_agents(db: Session = Depends(get_db)):
    """Get all agents"""
    agents = db.query(Agent).all()
    
    return {
        "agents": [
            {
                "agent_id": agent.agent_id,
                "hostname": agent.hostname,
                "platform": agent.platform,
                "status": agent.status,
                "last_seen": agent.last_seen.isoformat() if agent.last_seen else None,
            }
            for agent in agents
        ]
    }