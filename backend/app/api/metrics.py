"""
Metrics API endpoints (sync handlers: FastAPI runs them in a threadpool, off the event loop)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SystemMetrics

router = APIRouter()


def _serialize(m: SystemMetrics) -> dict:
    return {
        "agent_id": m.agent_id,
        "seq": m.seq,
        "timestamp": m.ts.isoformat() if m.ts else None,
        "cpu_usage": m.cpu_usage,
        "memory_usage": m.memory_usage,
        "disk_usage": m.disk_usage,
        "network_latency": m.network_latency,
    }


@router.get("/")
def get_metrics(agent_id: str | None = None, limit: int = Query(100, ge=1, le=1000),
                db: Session = Depends(get_db)):
    """Most recent samples, optionally for one agent"""
    query = db.query(SystemMetrics)
    if agent_id:
        query = query.filter(SystemMetrics.agent_id == agent_id)
    return {"metrics": [_serialize(m) for m in query.order_by(SystemMetrics.ts.desc()).limit(limit)]}


@router.get("/{agent_id}/latest")
def get_latest_metrics(agent_id: str, db: Session = Depends(get_db)):
    """Latest sample for an agent"""
    metric = (db.query(SystemMetrics).filter(SystemMetrics.agent_id == agent_id)
              .order_by(SystemMetrics.ts.desc()).first())
    if not metric:
        raise HTTPException(status_code=404, detail="No metrics found for this agent")
    return _serialize(metric)
