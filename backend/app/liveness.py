"""Agent liveness: mark silent agents offline, and bring them back.

Runs as several `python -m app.worker liveness` replicas for availability; a Redis lease elects
one leader, and only the leader sweeps. Every sweep action is an idempotent UPDATE guarded by
the current state, so a brief overlap of two leaders (e.g. after a GC or network pause)
repeats harmless work instead of corrupting anything. That's why no fencing token is needed.

On going offline:  agent.status='offline', an `agent_offline` alert opens, the agent's other
                   active alerts become 'stale' (we can no longer tell whether they still hold),
                   and its detector state is cleared so detection restarts clean on return.
On coming back:    the persist worker already sets status='healthy' on new data; the sweep
                   resolves the `agent_offline` alert and announces `agent_online`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import redis.asyncio as aioredis
from redis.exceptions import WatchError
from sqlalchemy import select, text, update

from app import config
from app.database import SessionLocal, upsert
from app.models import Agent, Alert
from app.pipeline.streams import group_backlog

log = logging.getLogger(__name__)

OFFLINE_ALERT = "agent_offline"
# Upper bound on agents handled per pass, so one pass can never run long.
SWEEP_BATCH = 500


class LeaderLease:
    """SET NX PX lease with owner-checked renew/release (WATCH/MULTI, so only the holder can)."""

    def __init__(self, redis: aioredis.Redis, name: str, ttl_ms: int = config.LEADER_LEASE_MS,
                 owner: str | None = None):
        self.redis = redis
        self.key = f"leader:{name}"
        self.ttl_ms = ttl_ms
        self.owner = owner or f"{config.CONSUMER_NAME}-{uuid.uuid4().hex[:6]}"

    async def acquire_or_renew(self) -> bool:
        if await self.redis.set(self.key, self.owner, nx=True, px=self.ttl_ms):
            return True
        return await self._if_owner(lambda pipe: pipe.pexpire(self.key, self.ttl_ms))

    async def release(self) -> bool:
        return await self._if_owner(lambda pipe: pipe.delete(self.key))

    async def _if_owner(self, action) -> bool:
        async with self.redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self.key)
                if await pipe.get(self.key) != self.owner:
                    await pipe.reset()
                    return False
                pipe.multi()
                action(pipe)
                await pipe.execute()
                return True
            except WatchError:
                return False


class LivenessMonitor:
    def __init__(self, redis: aioredis.Redis, lease: LeaderLease | None = None):
        self.redis = redis
        self.lease = lease or LeaderLease(redis, "liveness")
        self.stopping = False
        self.is_leader = False

    async def sweep(self, now: datetime | None = None) -> Dict[str, List[str]]:
        """One bounded pass (at most SWEEP_BATCH agents each way). Returns the agents that went
        offline / came back. DB work runs in a thread so it can't starve lease renewal."""
        now = now or datetime.now(timezone.utc)
        lag = await group_backlog(self.redis, config.PERSIST_GROUP)
        if lag > config.LIVENESS_MAX_LAG:
            # last_seen is stale because *we* are behind, not because agents are silent.
            log.warning("liveness: persist lag %d > %d, skipping sweep", lag, config.LIVENESS_MAX_LAG)
            return {"offline": [], "online": [], "skipped": ["persist_lag"]}

        went_offline, came_back = await asyncio.to_thread(self._sweep_db, now)

        ts = now.isoformat()
        pipe = self.redis.pipeline(transaction=False)
        if went_offline:
            pipe.delete(*(f"det:s:{a}" for a in went_offline))
        for agent_id in went_offline:
            pipe.publish(config.DASHBOARD_CHANNEL, json.dumps({"type": "agent_offline", "agent_id": agent_id,
                                                               "timestamp": ts}))
        for agent_id in came_back:
            pipe.publish(config.DASHBOARD_CHANNEL, json.dumps({"type": "agent_online", "agent_id": agent_id,
                                                               "timestamp": ts}))
        await pipe.execute()
        if went_offline or came_back:
            log.info("liveness: %d offline, %d back online", len(went_offline), len(came_back))
        return {"offline": list(went_offline), "online": list(came_back), "skipped": []}

    @staticmethod
    def _sweep_db(now: datetime) -> tuple[List[str], List[str]]:
        cutoff = now - timedelta(seconds=config.AGENT_STALE_S)
        with SessionLocal() as db:
            batch = (select(Agent.agent_id).where(Agent.status != "offline", Agent.last_seen < cutoff)
                     .order_by(Agent.agent_id).limit(SWEEP_BATCH))
            went_offline = db.scalars(
                update(Agent).where(Agent.agent_id.in_(batch), Agent.status != "offline")
                .values(status="offline").returning(Agent.agent_id)).all()
            if went_offline:
                stmt = upsert(Alert).values([{
                    "alert_id": f"{agent_id}:{OFFLINE_ALERT}:{uuid.uuid4().hex[:12]}", "agent_id": agent_id,
                    "alert_type": OFFLINE_ALERT, "severity": "critical",
                    "description": f"No telemetry for over {config.AGENT_STALE_S}s",
                    "details": {"stale_after_s": config.AGENT_STALE_S}, "status": "active", "occurrences": 1,
                    "first_seen": now, "last_seen": now} for agent_id in went_offline])
                db.execute(stmt.on_conflict_do_nothing(index_elements=[Alert.agent_id, Alert.alert_type],
                                                       index_where=text("status = 'active'")))
                db.execute(update(Alert).where(Alert.agent_id.in_(went_offline), Alert.status == "active",
                                               Alert.alert_type != OFFLINE_ALERT)
                           .values(status="stale", last_seen=now))

            # Back online: fresh data again while an agent_offline alert is still open.
            came_back = db.scalars(
                select(Alert.agent_id).join(Agent, Agent.agent_id == Alert.agent_id)
                .where(Alert.alert_type == OFFLINE_ALERT, Alert.status == "active", Agent.last_seen >= cutoff)
                .limit(SWEEP_BATCH)).all()
            if came_back:
                db.execute(update(Alert).where(Alert.agent_id.in_(came_back), Alert.alert_type == OFFLINE_ALERT,
                                               Alert.status == "active").values(status="resolved", last_seen=now))
            db.commit()
        return list(went_offline), list(came_back)

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake promptly on shutdown (SIGTERM must not wait out a sweep interval)."""
        deadline = asyncio.get_running_loop().time() + seconds
        while not self.stopping:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25))

    async def _keep_lease(self) -> None:
        """Renew on its own schedule, independent of how long a sweep takes: a busy leader
        must not lose its lease just because it is busy."""
        while not self.stopping:
            try:
                leader = await self.lease.acquire_or_renew()
            except Exception as e:
                log.warning("liveness: lease renewal failed: %s", e)
                leader = False
            if leader != self.is_leader:
                log.info("liveness %s: %s leadership", self.lease.owner, "acquired" if leader else "lost")
                self.is_leader = leader
            await self._sleep(config.LEADER_LEASE_MS / 3000)   # 3 renewals per lease period

    async def run(self) -> None:
        log.info("liveness monitor %s started", self.lease.owner)
        keeper = asyncio.create_task(self._keep_lease())
        try:
            while not self.stopping:
                if self.is_leader:
                    try:
                        result = await self.sweep()
                        busy = SWEEP_BATCH in (len(result["offline"]), len(result["online"]))
                    except Exception:
                        log.exception("liveness sweep failed")
                        busy = False
                    # A full batch means there's more to do: continue promptly, still in bounded steps.
                    await self._sleep(0.5 if busy else config.LIVENESS_SWEEP_S)
                else:
                    await self._sleep(1)
        finally:
            self.stopping = True
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
            if self.is_leader:
                await self.lease.release()   # hand over immediately instead of waiting for expiry
            log.info("liveness monitor %s stopped", self.lease.owner)
