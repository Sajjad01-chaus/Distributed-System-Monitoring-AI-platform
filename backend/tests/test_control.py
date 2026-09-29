"""Control plane: presence, cross-replica command routing, leader election, liveness."""
import json
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.control import Presence
from app.database import SessionLocal
from app.liveness import LeaderLease, LivenessMonitor
from app.main import create_app
from app.models import Agent, Alert


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def replicas(redis_server):
    """Two API replicas sharing one Redis, like two containers behind the load balancer."""
    sync = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    with TestClient(create_app("replica-a")) as a, TestClient(create_app("replica-b")) as b:
        # Both command listeners subscribed before any test publishes.
        wait_for(lambda: all(dict(sync.pubsub_numsub(f"replica:{r}:commands"))[f"replica:{r}:commands"] == 1
                             for r in ("replica-a", "replica-b")))
        yield a, b, sync


def test_command_issued_on_one_replica_reaches_agent_on_another(replicas):
    a, b, sync = replicas
    with a.websocket_connect("/ws/agent/agent-x") as agent:
        wait_for(lambda: sync.get("presence:agent-x") == "replica-a")

        resp = b.post("/api/v1/agents/agent-x/remediate", params={"issue_type": "cpu_threshold_breach"})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "delivered" and body["routed_to"] == "replica-a"
        assert body["issued_via"] == "replica-b"
        assert resp.headers["X-Replica-Id"] == "replica-b"

        command = agent.receive_json()
        assert command["type"] == "remediate" and command["command_id"] == body["command_id"]
        agent.send_json({"type": "remediation_result", "command_id": command["command_id"],
                         "success": True, "dry_run": True})
        wait_for(lambda: sync.hget(f"cmd:{command['command_id']}", "status") == "completed")

    record = b.get(f"/api/v1/commands/{body['command_id']}").json()
    assert record["status"] == "completed" and record["result"]["success"] is True


def test_command_to_unknown_agent_is_404(replicas):
    a, _, _ = replicas
    resp = a.post("/api/v1/agents/nobody/restart")
    assert resp.status_code == 404 and resp.json()["detail"]["status"] == "not_connected"


def test_stale_presence_for_a_dead_replica_is_503_not_a_hang(replicas):
    a, _, sync = replicas
    sync.set("presence:orphan", "replica-that-died", ex=60)
    started = time.time()
    resp = a.post("/api/v1/agents/orphan/restart")
    assert resp.status_code == 503 and resp.json()["detail"]["status"] == "undeliverable"
    assert time.time() - started < 1.0, "no subscriber -> answer immediately, don't wait for an ack"


def test_disconnect_releases_presence(replicas):
    a, _, sync = replicas
    with a.websocket_connect("/ws/agent/agent-y"):
        wait_for(lambda: sync.get("presence:agent-y") == "replica-a")
    wait_for(lambda: sync.get("presence:agent-y") is None)


@pytest.mark.asyncio
async def test_old_socket_closing_does_not_erase_a_newer_claim(redis_client):
    old, new = Presence(redis_client, "replica-a"), Presence(redis_client, "replica-b")
    await old.claim("agent-z")
    await new.claim("agent-z")            # agent reconnected via the load balancer to replica B
    assert not await old.release("agent-z")
    assert await new.where("agent-z") == "replica-b"
    assert await new.release("agent-z") and await new.where("agent-z") is None


@pytest.mark.asyncio
async def test_leader_lease_single_holder_and_handover(redis_client):
    first = LeaderLease(redis_client, "liveness", ttl_ms=10_000, owner="one")
    second = LeaderLease(redis_client, "liveness", ttl_ms=10_000, owner="two")
    assert await first.acquire_or_renew()
    assert not await second.acquire_or_renew()
    assert await first.acquire_or_renew(), "holder renews"
    assert not await second.release(), "non-holder can't release someone else's lease"
    assert await first.release()
    assert await second.acquire_or_renew(), "handover right after release, no TTL wait"


@pytest.mark.asyncio
async def test_lease_expires_if_holder_dies(redis_client):
    import asyncio
    dead = LeaderLease(redis_client, "liveness", ttl_ms=100, owner="dead")
    standby = LeaderLease(redis_client, "liveness", ttl_ms=100, owner="standby")
    assert await dead.acquire_or_renew()
    await asyncio.sleep(0.2)                  # holder stops renewing
    assert await standby.acquire_or_renew()


def _seed_agents(now):
    with SessionLocal() as db:
        db.add_all([
            Agent(agent_id="silent", status="healthy", last_seen=now - timedelta(minutes=10)),
            Agent(agent_id="alive", status="healthy", last_seen=now),
            Alert(alert_id="m1", agent_id="silent", alert_type="memory_leak_pattern", severity="high",
                  status="active", occurrences=1, first_seen=now, last_seen=now),
        ])
        db.commit()


@pytest.mark.asyncio
async def test_liveness_marks_silent_agents_offline_and_back(redis_client):
    now = datetime.now(timezone.utc)
    _seed_agents(now)
    await redis_client.hset("det:s:silent", "active:memory_leak_pattern", 1)
    monitor = LivenessMonitor(redis_client)

    result = await monitor.sweep(now)
    assert result["offline"] == ["silent"]
    with SessionLocal() as db:
        assert db.get(Agent, db.scalar(select(Agent.id).where(Agent.agent_id == "silent"))).status == "offline"
        alerts = {a.alert_type: a.status for a in db.scalars(select(Alert).where(Alert.agent_id == "silent"))}
    assert alerts == {"memory_leak_pattern": "stale", "agent_offline": "active"}
    assert not await redis_client.exists("det:s:silent"), "detection restarts clean on return"
    assert (await monitor.sweep(now))["offline"] == [], "idempotent: nothing new to do"

    with SessionLocal() as db:                 # telemetry resumes (persist worker does this)
        db.query(Agent).filter(Agent.agent_id == "silent").update({"status": "healthy", "last_seen": now})
        db.commit()
    assert (await monitor.sweep(now))["online"] == ["silent"]
    with SessionLocal() as db:
        offline = db.scalar(select(Alert).where(Alert.agent_id == "silent", Alert.alert_type == "agent_offline"))
    assert offline.status == "resolved"


@pytest.mark.asyncio
async def test_liveness_skips_sweep_when_our_own_backlog_is_the_reason(redis_client, monkeypatch):
    now = datetime.now(timezone.utc)
    _seed_agents(now)
    monkeypatch.setattr(config, "LIVENESS_MAX_LAG", 0)
    await redis_client.xgroup_create(config.TELEMETRY_STREAM, config.PERSIST_GROUP, id="0", mkstream=True)
    await redis_client.xadd(config.TELEMETRY_STREAM, {"agent_id": "silent", "data": json.dumps({})})
    # Read but not yet persisted (in flight in a persist worker): counts as backlog.
    await redis_client.xreadgroup(config.PERSIST_GROUP, "w1", {config.TELEMETRY_STREAM: ">"}, count=1)

    result = await LivenessMonitor(redis_client).sweep(now)
    assert result == {"offline": [], "online": [], "skipped": ["persist_lag"]}


@pytest.mark.asyncio
async def test_sweep_is_bounded_per_pass(redis_client, monkeypatch):
    import app.liveness as liveness
    monkeypatch.setattr(liveness, "SWEEP_BATCH", 2)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add_all([Agent(agent_id=f"gone-{i}", status="healthy", last_seen=now - timedelta(hours=1))
                    for i in range(5)])
        db.commit()
    monitor = LivenessMonitor(redis_client)
    passes = [len((await monitor.sweep(now))["offline"]) for _ in range(4)]
    assert passes == [2, 2, 1, 0]


@pytest.mark.asyncio
async def test_slow_sweep_does_not_cost_the_leader_its_lease(redis_client, monkeypatch):
    """Regression: a sweep longer than the lease let a second instance become leader."""
    import asyncio

    import app.liveness as liveness
    monkeypatch.setattr(config, "LEADER_LEASE_MS", 300)

    def slow_sweep(now):
        time.sleep(1.0)            # 3x the lease, blocking, like a big synchronous DB pass
        return [], []
    monkeypatch.setattr(liveness.LivenessMonitor, "_sweep_db", staticmethod(slow_sweep))

    leader = LivenessMonitor(redis_client, LeaderLease(redis_client, "liveness", ttl_ms=300, owner="leader"))
    rival = LeaderLease(redis_client, "liveness", ttl_ms=300, owner="rival")
    task = asyncio.create_task(leader.run())
    await asyncio.sleep(0.2)
    assert leader.is_leader
    for _ in range(12):                      # ~1.2 s, spanning the slow sweep
        assert not await rival.acquire_or_renew(), "lease lapsed during a long sweep"
        await asyncio.sleep(0.1)
    leader.stopping = True
    await asyncio.wait_for(task, timeout=5)
