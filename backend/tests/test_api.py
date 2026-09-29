"""API behaviour: the agent socket only validates and enqueues; workers do the rest."""
import asyncio
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.pipeline import streams
from app.pipeline.persist import PersistConsumer

SAMPLE = {"agent_id": "agent-t1", "boot_id": "b1", "seq": 1, "timestamp": "2026-09-29T10:00:00+00:00",
          "cpu_usage": 42.5, "memory_usage": 61.0, "disk_usage": 70.0, "network_latency": 12.0}


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def sync_redis(redis_server):
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def client(redis_server, sync_redis):
    with TestClient(app) as c:
        # Don't publish before the dashboard relay has subscribed, or the event is simply missed.
        wait_for(lambda: dict(sync_redis.pubsub_numsub(config.DASHBOARD_CHANNEL))[config.DASHBOARD_CHANNEL] > 0)
        yield c


def stream_len(r) -> int:
    return r.xlen(config.TELEMETRY_STREAM) if r.exists(config.TELEMETRY_STREAM) else 0


def run_persist_worker():
    async def go():
        consumer = PersistConsumer(streams.redis_factory(), consumer_name="test")
        await consumer.ensure_group()
        await consumer.run_once(block_ms=1)
    asyncio.run(go())


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "healthy"
    ready = client.get("/ready")
    assert ready.status_code == 200 and ready.json()["checks"] == {"redis": "ok", "database": "ok"}


def test_telemetry_flows_socket_to_stream_to_db_to_dashboard(client, sync_redis):
    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect("/ws/agent/agent-t1") as agent:
            agent.send_json(SAMPLE)
            wait_for(lambda: stream_len(sync_redis) == 1)
        # Nothing is stored by the API itself...
        assert client.get("/api/v1/metrics/agent-t1/latest").status_code == 404
        # ...until a persist worker consumes the stream.
        run_persist_worker()
        update = dashboard.receive_json()

    assert update["type"] == "metrics_update" and update["metrics"]["cpu_usage"] == 42.5
    latest = client.get("/api/v1/metrics/agent-t1/latest").json()
    assert latest["cpu_usage"] == 42.5 and latest["seq"] == 1
    assert any(a["agent_id"] == "agent-t1" for a in client.get("/api/v1/agents/").json()["agents"])
    status = client.get("/api/v1/system/pipeline").json()
    assert status["groups"]["persist"]["pending"] == 0 and status["dead_letters"] == 0


def test_invalid_messages_are_rejected_at_the_edge(client, sync_redis, monkeypatch):
    monkeypatch.setattr(config, "MAX_MESSAGE_BYTES", 200)
    with client.websocket_connect("/ws/agent/agent-t1") as agent:
        agent.send_text("{not json")
        assert agent.receive_json() == {"type": "error", "reason": "invalid JSON"}
        agent.send_json({**SAMPLE, "agent_id": "someone-else"})
        assert "does not match" in agent.receive_json()["reason"]
        agent.send_json({**SAMPLE, "padding": "x" * 500})
        assert "exceeds" in agent.receive_json()["reason"]
    assert stream_len(sync_redis) == 0


def test_bad_agent_id_is_refused(redis_server):
    with TestClient(app) as c, pytest.raises(Exception):
        with c.websocket_connect("/ws/agent/bad id with spaces"):
            pass


def test_backlog_above_limit_throttles_instead_of_enqueueing(client, sync_redis, monkeypatch):
    monkeypatch.setattr(config, "PERSIST_MAX_LAG", -1)      # any backlog counts as overload
    with client.websocket_connect("/ws/agent/agent-t1") as agent:
        agent.send_json(SAMPLE)
        reply = agent.receive_json()
    assert reply["type"] == "throttle" and reply["retry_after_s"] > 0
    assert stream_len(sync_redis) == 0
    assert client.get("/api/v1/system/pipeline").json()["admission"]["rejected"] == 1


def test_control_messages_are_relayed_not_stored(client, sync_redis):
    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect("/ws/agent/agent-t1") as agent:
            agent.send_json({"type": "remediation_result", "issue_type": "cpu_threshold_breach",
                             "success": True})
            event = dashboard.receive_json()
    assert event["type"] == "remediation_result" and event["agent_id"] == "agent-t1"
    assert stream_len(sync_redis) == 0


def test_commands_to_unconnected_agents_404(client):
    from conftest import token_headers
    assert client.post("/api/v1/agents/ghost/remediate", headers=token_headers(client)).status_code == 404
    assert client.get("/api/v1/metrics/nope/latest").status_code == 404


def test_system_status_reports_real_fleet_numbers(client):
    from datetime import datetime, timedelta, timezone

    from app.database import SessionLocal
    from app.models import Agent, Alert
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add_all([Agent(agent_id="fresh-ok", status="healthy", last_seen=now),
                    Agent(agent_id="fresh-sick", status="healthy", last_seen=now),
                    Agent(agent_id="stale", status="healthy", last_seen=now - timedelta(hours=1)),
                    Alert(alert_id="a1", agent_id="fresh-sick", alert_type="memory_leak_pattern",
                          severity="high", status="active", occurrences=1, first_seen=now, last_seen=now),
                    Alert(alert_id="a2", agent_id="stale", alert_type="disk_threshold_breach",
                          severity="critical", status="active", occurrences=1, first_seen=now, last_seen=now)])
        db.commit()
    status = client.get("/api/v1/system/status").json()
    assert (status["total_agents"], status["connected_agents"], status["healthy_agents"]) == (3, 2, 1)
    assert status["active_alerts"] == 2 and status["anomalies_24h"] == 2


def test_database_url_from_hosting_providers_is_normalized():
    from app.database import _normalize
    assert _normalize("postgres://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"
    assert _normalize("postgresql://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert _normalize("postgresql+psycopg2://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert _normalize("sqlite:///x.db") == "sqlite:///x.db"
