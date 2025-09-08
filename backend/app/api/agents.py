from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import get_db
from ..models.agent import Agent

router = APIRouter()

@router.get("/")
async def list_agents(db: Session = Depends(get_db)):
    return db.query(Agent).all()