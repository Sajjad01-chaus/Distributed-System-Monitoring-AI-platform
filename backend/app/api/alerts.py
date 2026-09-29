"""
Alerts API endpoints (sync handlers: FastAPI runs them in a threadpool, off the event loop)
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import require
from app.database import get_db
from app.models import Alert

router = APIRouter()


def _serialize(a: Alert) -> dict:
    return {
        "id": a.id,
        "agent_id": a.agent_id,
        "title": a.alert_type,
        "alert_type": a.alert_type,
        "description": a.description,
        "severity": a.severity,
        "status": a.status,
        "occurrences": a.occurrences,
        "details": a.details,
        "timestamp": a.first_seen.isoformat() if a.first_seen else None,
        "last_seen": a.last_seen.isoformat() if a.last_seen else None,
    }


@router.get("/")
def get_alerts(status: str | None = None, severity: str | None = None,
               limit: int = Query(100, ge=1, le=1000), db: Session = Depends(get_db)):
    """Get alerts"""
    query = db.query(Alert)
    if status:
        query = query.filter(Alert.status == status)
    if severity:
        query = query.filter(Alert.severity == severity)
    return {"alerts": [_serialize(a) for a in query.order_by(Alert.last_seen.desc()).limit(limit)]}


@router.get("/{alert_id}")
def get_alert(alert_id: int, db: Session = Depends(get_db)):
    """Get specific alert"""
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    return _serialize(alert)


@router.post("/{alert_id}/resolve", dependencies=[Depends(require("admin"))])
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    """Resolve an alert. A later occurrence of the same problem opens a new active alert."""
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "resolved"
    alert.last_seen = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Alert resolved successfully", "alert_id": alert_id}
