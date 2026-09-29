"""persist group: batch-writes telemetry to the database and announces new rows to dashboards."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Sequence

from app import config
from app.database import SessionLocal, upsert
from app.models import Agent, SystemMetrics
from app.pipeline.consumer import StreamConsumer
from app.pipeline.streams import Envelope, publish

log = logging.getLogger(__name__)

# Keeps each INSERT well under SQLite's bound-parameter limit; Postgres doesn't mind either way.
CHUNK = 200


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def to_row(env: Envelope) -> Dict:
    p = env.payload
    return {"agent_id": env.agent_id, "boot_id": env.boot_id, "seq": env.seq, "ts": env.ts,
            "cpu_usage": _num(p.get("cpu_usage")), "memory_usage": _num(p.get("memory_usage")),
            "disk_usage": _num(p.get("disk_usage")), "network_latency": _num(p.get("network_latency")),
            "raw_data": p}


class PersistConsumer(StreamConsumer):
    group = config.PERSIST_GROUP

    async def handle(self, envelopes: Sequence[Envelope]) -> None:
        # Duplicates inside one batch would conflict with each other; keep the first.
        unique: Dict[tuple, Envelope] = {}
        for env in envelopes:
            unique.setdefault(env.key, env)
        batch = list(unique.values())

        # DB work runs in a thread: harmless in a dedicated worker, and essential when the
        # consumer is embedded in the API process (app.embedded), whose event loop must stay free.
        inserted = await asyncio.to_thread(self._write, batch)

        # Only after commit, and only for rows that were new: replays and duplicates stay silent.
        fresh = [e for e in batch if e.key in inserted]
        for env in fresh:
            await publish(self.redis, {"type": "metrics_update", "agent_id": env.agent_id,
                                       "metrics": env.payload, "timestamp": env.ts.isoformat()})
        if len(fresh) < len(envelopes):
            log.debug("persist: %d of %d entries were duplicates", len(envelopes) - len(fresh), len(envelopes))

    def _write(self, batch: List[Envelope]) -> set:
        inserted = set()
        with SessionLocal() as db:
            for i in range(0, len(batch), CHUNK):
                stmt = (upsert(SystemMetrics).values([to_row(e) for e in batch[i:i + CHUNK]])
                        .on_conflict_do_nothing()
                        .returning(SystemMetrics.agent_id, SystemMetrics.boot_id, SystemMetrics.seq))
                inserted.update(tuple(r) for r in db.execute(stmt))
            self._touch_agents(db, batch)
            db.commit()
        return inserted

    @staticmethod
    def _touch_agents(db, batch: List[Envelope]) -> None:
        now = datetime.now(timezone.utc)
        latest: Dict[str, Envelope] = {}
        for env in batch:
            latest[env.agent_id] = env
        rows = []
        # Sorted: concurrent workers then lock agent rows in the same order and can't deadlock.
        for agent_id in sorted(latest):
            platform = latest[agent_id].payload.get("platform") or {}
            rows.append({"agent_id": agent_id, "hostname": str(platform.get("node") or agent_id)[:255],
                         "platform": str(platform.get("system") or "unknown")[:50],
                         "status": "healthy", "last_seen": now})
        stmt = upsert(Agent).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Agent.agent_id],
            set_={c: getattr(stmt.excluded, c) for c in ("hostname", "platform", "status", "last_seen")})
        db.execute(stmt)
