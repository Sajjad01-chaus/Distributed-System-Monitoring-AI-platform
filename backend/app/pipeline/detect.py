"""detect group: per-agent anomaly detection off the request path, with alert lifecycle.

State lives in Redis, not in the worker, so any detect worker can handle any agent's
messages and workers can be added or killed freely:
  det:w:{agent}  list   the agent's last DETECT_WINDOW samples (compact JSON)
  det:s:{agent}  hash   active:{type} = 1 for open alerts, clear:{type} = consecutive clear evaluations

Alerts change state on transitions only: an alert opens when its detector starts firing and
resolves after the condition has been clear for DETECT_CLEAR_AFTER consecutive evaluations
(hysteresis, so a value hovering at a threshold can't flap). Steady-state firing causes no
DB writes and no dashboard traffic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from sqlalchemy import text, update

from app import config
from app.database import SessionLocal, upsert
from app.detection.detectors import DEFAULT, Thresholds, evaluate
from app.detection.outliers import FleetOutlierModel, features
from app.models import Alert
from app.pipeline.consumer import StreamConsumer
from app.pipeline.streams import Envelope, publish

log = logging.getLogger(__name__)

STATE_TTL_S = 24 * 3600   # forget agents that went silent a day ago


def _dedup_key(env: Envelope) -> str:
    return f"detected:{env.agent_id}:{env.boot_id}:{env.seq}"


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def to_sample(env: Envelope) -> Dict[str, float]:
    p = env.payload
    net = p.get("network") if isinstance(p.get("network"), dict) else {}
    return {"t": env.ts.timestamp(), "cpu": _num(p.get("cpu_usage")), "mem": _num(p.get("memory_usage")),
            "disk": _num(p.get("disk_usage")), "lat": _num(p.get("network_latency")),
            "loss": _num(net.get("packet_loss_percent"))}


class DetectConsumer(StreamConsumer):
    group = config.DETECT_GROUP

    def __init__(self, redis, model: FleetOutlierModel | None = None, thresholds: Thresholds = DEFAULT, **kw):
        super().__init__(redis, **kw)
        self.model = model or FleetOutlierModel()
        self.thresholds = thresholds

    async def handle(self, envelopes: Sequence[Envelope]) -> None:
        first: Dict[tuple, Envelope] = {}
        for env in envelopes:                   # duplicates within this batch
            first.setdefault(env.key, env)
        unique = list(first.values())
        seen = await self.redis.mget([_dedup_key(e) for e in unique])   # ...and from earlier batches
        todo = [e for e, s in zip(unique, seen) if s is None]
        if not todo:
            return

        samples = [to_sample(e) for e in todo]
        rows = [features(s) for s in samples]
        for sample, flag in zip(samples, self.model.flags(rows)):   # one vectorised scoring call
            sample["out"] = flag
        self.model.observe(rows)
        self.model.maybe_refit()

        by_agent: Dict[str, List[Dict]] = defaultdict(list)
        for env, sample in zip(todo, samples):
            by_agent[env.agent_id].append(sample)
        agents = sorted(by_agent)

        # One round trip: append to every agent's window, read windows + alert state back.
        pipe = self.redis.pipeline()
        for agent in agents:
            wkey, skey = f"det:w:{agent}", f"det:s:{agent}"
            pipe.rpush(wkey, *(json.dumps(s) for s in by_agent[agent]))
            pipe.ltrim(wkey, -config.DETECT_WINDOW, -1)
            pipe.expire(wkey, STATE_TTL_S)
            pipe.lrange(wkey, 0, -1)
            pipe.hgetall(skey)
        results = await pipe.execute()

        opened: List[tuple[str, Dict[str, Any]]] = []
        resolved: List[tuple[str, str]] = []
        state_updates: Dict[str, Dict[str, Any]] = {}
        for i, agent in enumerate(agents):
            window = sorted((json.loads(x) for x in results[i * 5 + 3]), key=lambda s: s["t"])
            state = results[i * 5 + 4]
            firing = evaluate(window, self.thresholds)
            active = {k.split(":", 1)[1] for k in state if k.startswith("active:")}
            sets, dels = {}, []
            for kind, anomaly in firing.items():
                if kind not in active:
                    opened.append((agent, anomaly))
                    sets[f"active:{kind}"] = 1
                if state.get(f"clear:{kind}"):
                    dels.append(f"clear:{kind}")
            for kind in active - firing.keys():
                clear = int(state.get(f"clear:{kind}", 0)) + 1
                if clear >= config.DETECT_CLEAR_AFTER:
                    resolved.append((agent, kind))
                    dels += [f"active:{kind}", f"clear:{kind}"]
                else:
                    sets[f"clear:{kind}"] = clear
            if sets or dels:
                state_updates[agent] = {"set": sets, "del": dels}

        # Database first; Redis state and notifications only once it is committed.
        if opened or resolved:
            await asyncio.to_thread(self._write_alerts, opened, resolved)   # off the event loop

        pipe = self.redis.pipeline()
        for agent, change in state_updates.items():
            skey = f"det:s:{agent}"
            if change["set"]:
                pipe.hset(skey, mapping=change["set"])
            if change["del"]:
                pipe.hdel(skey, *change["del"])
            pipe.expire(skey, STATE_TTL_S)
        for env in todo:
            pipe.set(_dedup_key(env), 1, ex=config.DEDUP_TTL_S)
        await pipe.execute()

        by_agent_opened: Dict[str, List[Dict]] = defaultdict(list)
        for agent, anomaly in opened:
            by_agent_opened[agent].append(anomaly)
        ts = datetime.now(timezone.utc).isoformat()
        for agent, anomalies in by_agent_opened.items():
            await publish(self.redis, {"type": "anomaly_detected", "agent_id": agent,
                                       "anomalies": anomalies, "timestamp": ts})
        for agent, kind in resolved:
            await publish(self.redis, {"type": "alert_resolved", "agent_id": agent,
                                       "alert_type": kind, "timestamp": ts})

    def _write_alerts(self, opened, resolved) -> None:
        now = datetime.now(timezone.utc)
        with SessionLocal() as db:
            for agent, anomaly in opened:
                self._open_alert(db, agent, anomaly, now)
            for agent, kind in resolved:
                db.execute(update(Alert).where(Alert.agent_id == agent, Alert.alert_type == kind,
                                               Alert.status == "active")
                           .values(status="resolved", last_seen=now))
            db.commit()

    @staticmethod
    def _open_alert(db, agent: str, anomaly: Dict[str, Any], now: datetime) -> None:
        kind = anomaly["type"]
        stmt = upsert(Alert).values(
            alert_id=f"{agent}:{kind}:{uuid.uuid4().hex[:12]}", agent_id=agent, alert_type=kind,
            severity=anomaly["severity"], description=anomaly["description"],
            details={k: v for k, v in anomaly.items() if k not in ("type", "severity", "description")},
            status="active", occurrences=1, first_seen=now, last_seen=now)
        # Two workers racing on the same agent (or a replay after a crash) bump the count
        # instead of creating a second active alert.
        stmt = stmt.on_conflict_do_update(
            index_elements=[Alert.agent_id, Alert.alert_type], index_where=text("status = 'active'"),
            set_={"occurrences": Alert.occurrences + 1, "last_seen": now,
                  "severity": stmt.excluded.severity, "description": stmt.excluded.description,
                  "details": stmt.excluded.details})
        db.execute(stmt)
