"""
Alerts API endpoints
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from app.database import get_db
from app.models import Alert

router = APIRouter()

@router.get("/")
async def get_alerts(
    status: str = None,
    severity: str = None,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Get alerts"""
    query = db.query(Alert)
    
    if status:
        query = query.filter(Alert.status == status)
    
    if severity:
        query = query.filter(Alert.severity == severity)
    
    alerts = query.order_by(Alert.first_seen.desc()).limit(limit).all()
    
    return {
        "alerts": [
            {
                "id": a.id,
                "agent_id": a.agent_id,
                "title": a.alert_type,
                "description": a.description,
                "severity": a.severity,
                "status": a.status,
                "alert_type": a.alert_type,
                "timestamp": a.first_seen.isoformat() if a.first_seen else None,
                "resolved_at": a.last_seen.isoformat() if a.last_seen else None
            }
            for a in alerts
        ]
    }

@router.get("/{alert_id}")
async def get_alert(alert_id: int, db: Session = Depends(get_db)):
    """Get specific alert"""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    return {
        "id": alert.id,
        "agent_id": alert.agent_id,
        "title": alert.alert_type,
        "description": alert.description,
        "severity": alert.severity,
        "status": alert.status,
        "timestamp": alert.first_seen.isoformat() if alert.first_seen else None
    }

@router.post("/{alert_id}/resolve")
async def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    """Resolve an alert"""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    from datetime import datetime
    alert.status = "resolved"
    alert.last_seen = datetime.now()
    db.commit()
    
    return {"message": "Alert resolved successfully", "alert_id": alert_id}