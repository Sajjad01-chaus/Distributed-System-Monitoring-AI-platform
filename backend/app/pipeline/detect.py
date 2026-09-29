"""detect group: runs anomaly detection off the request path and persists alerts."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from sqlalchemy import text

from app import config
from app.database import SessionLocal, upsert
from app.models import Alert
from app.pipeline.consumer import StreamConsumer
from app.pipeline.streams import Envelope, publish
from app.services.ai_engine import AIEngine

log = logging.getLogger(__name__)


def _dedup_key(env: Envelope) -> str:
    return f"detected:{env.agent_id}:{env.boot_id}:{env.seq}"


class DetectConsumer(StreamConsumer):
    group = config.DETECT_GROUP

    def __init__(self, redis, engine: AIEngine | None = None, **kw):
        super().__init__(redis, **kw)
        self.engine = engine or AIEngine()

    async def ensure_group(self) -> None:
        await super().ensure_group()
        if not self.engine.initialized:
            await self.engine.initialize()

    async def handle(self, envelopes: Sequence[Envelope]) -> None:
        # Skip messages already analysed (duplicates, or a replay after a crash between
        # handling and XACK) so detector state and alert counts aren't inflated.
        first: Dict[tuple, Envelope] = {}
        for env in envelopes:                   # duplicates within this batch
            first.setdefault(env.key, env)
        unique = list(first.values())
        seen = await self.redis.mget([_dedup_key(e) for e in unique])   # ...and from earlier batches
        todo = [e for e, s in zip(unique, seen) if s is None]

        findings: List[tuple[Envelope, List[Dict[str, Any]]]] = []
        for env in todo:
            anomalies = await self.engine.detect_anomalies(env.payload)
            if anomalies:
                findings.append((env, anomalies))

        if findings:
            with SessionLocal() as db:
                for env, anomalies in findings:
                    for a in anomalies:
                        self._upsert_alert(db, env, a)
                db.commit()
            for env, anomalies in findings:
                await publish(self.redis, {"type": "anomaly_detected", "agent_id": env.agent_id,
                                           "anomalies": anomalies, "timestamp": env.ts.isoformat()})

        if todo:
            pipe = self.redis.pipeline()
            for env in todo:
                pipe.set(_dedup_key(env), 1, ex=config.DEDUP_TTL_S)
            await pipe.execute()

    @staticmethod
    def _upsert_alert(db, env: Envelope, anomaly: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        alert_type = str(anomaly.get("type", "unknown"))[:100]
        stmt = upsert(Alert).values(
            alert_id=f"{env.agent_id}:{alert_type}:{uuid.uuid4().hex[:12]}",
            agent_id=env.agent_id, alert_type=alert_type,
            severity=str(anomaly.get("severity", "medium"))[:20],
            description=str(anomaly.get("description", "")),
            details={k: v for k, v in anomaly.items() if k not in ("type", "severity", "description")},
            status="active", occurrences=1, first_seen=now, last_seen=now)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Alert.agent_id, Alert.alert_type],
            index_where=text("status = 'active'"),
            set_={"occurrences": Alert.occurrences + 1, "last_seen": now,
                  "severity": stmt.excluded.severity, "description": stmt.excluded.description,
                  "details": stmt.excluded.details})
        db.execute(stmt)
