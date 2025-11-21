"""
Metrics API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.models import SystemMetrics

router = APIRouter()

@router.get("/")
async def get_metrics(
    agent_id: str = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Get system metrics"""
    query = db.query(SystemMetrics)
    
    if agent_id:
        query = query.filter(SystemMetrics.agent_id == agent_id)
    
    metrics = query.order_by(SystemMetrics.timestamp.desc()).limit(limit).all()
    
    return {
        "metrics": [
            {
                "id": m.id,
                "agent_id": m.agent_id,
                "timestamp": m.timestamp.isoformat() if m.timestamp else None,
                # Extract from raw_data JSON
                "cpu_usage": m.raw_data.get("cpu_usage", 0) if m.raw_data else 0,
                "memory_usage": m.raw_data.get("memory_usage", 0) if m.raw_data else 0,
                "disk_usage": m.raw_data.get("disk_usage", 0) if m.raw_data else 0,
                "network_latency": m.raw_data.get("network_latency", 0) if m.raw_data else 0,
                "is_anomaly": m.raw_data.get("is_anomaly", False) if m.raw_data else False,
                "anomaly_score": m.raw_data.get("anomaly_score", 0) if m.raw_data else 0,
                "severity": m.raw_data.get("severity", "normal") if m.raw_data else "normal"
            }
            for m in metrics
        ]
    }

@router.get("/{agent_id}/latest")
async def get_latest_metrics(agent_id: str, db: Session = Depends(get_db)):
    """Get latest metrics for an agent"""
    metric = db.query(SystemMetrics)\
        .filter(SystemMetrics.agent_id == agent_id)\
        .order_by(SystemMetrics.timestamp.desc())\
        .first()
    
    if not metric:
        raise HTTPException(status_code=404, detail="No metrics found for this agent")
    
    return {
        "id": metric.id,
        "agent_id": metric.agent_id,
        "timestamp": metric.timestamp.isoformat() if metric.timestamp else None,
        # Extract from raw_data JSON
        "cpu_usage": metric.raw_data.get("cpu_usage", 0) if metric.raw_data else 0,
        "memory_usage": metric.raw_data.get("memory_usage", 0) if metric.raw_data else 0,
        "disk_usage": metric.raw_data.get("disk_usage", 0) if metric.raw_data else 0,
        "network_latency": metric.raw_data.get("network_latency", 0) if metric.raw_data else 0
    }