"""Smoke tests pinning the current API behaviour before the ingestion pipeline is refactored."""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_monitor.db")

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
    if os.path.exists("test_monitor.db"):
        os.remove("test_monitor.db")


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_agent_metrics_are_persisted_and_broadcast(client):
    sample = {"agent_id": "agent-test-1", "cpu_usage": 42.5, "memory_usage": 61.0,
              "disk_usage": 70.0, "network_latency": 12.0}

    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect("/ws/agent/agent-test-1") as agent:
            agent.send_json(sample)
            # The dashboard broadcast happens after the DB write, so it doubles as a sync point.
            update = dashboard.receive_json()
            while update["type"] != "metrics_update":
                update = dashboard.receive_json()

    assert update["agent_id"] == "agent-test-1"
    assert update["metrics"]["cpu_usage"] == 42.5

    agents = client.get("/api/v1/agents/").json()["agents"]
    assert any(a["agent_id"] == "agent-test-1" for a in agents)

    latest = client.get("/api/v1/metrics/agent-test-1/latest").json()
    assert latest["cpu_usage"] == 42.5
    assert latest["memory_usage"] == 61.0


def test_latest_metrics_unknown_agent_404(client):
    assert client.get("/api/v1/metrics/nope/latest").status_code == 404


def test_remediate_unconnected_agent_404(client):
    assert client.post("/api/v1/agents/ghost/remediate").status_code == 404


def test_anomaly_detection_handles_real_agent_payload(client):
    """Regression: nested cpu/memory dicts produce numpy features; `if features:` used to raise."""
    payload = {
        "agent_id": "agent-test-2",
        "cpu_usage": 20.0, "memory_usage": 91.3, "disk_usage": 50.0, "network_latency": 10.0,
        "cpu": {"usage_percent": 20.0, "load_avg_1m": 0.5, "core_count": 8},
        "memory": {"usage_percent": 91.3, "swap_usage_percent": 10.0},
        "disk": {"usage_percent": 50.0},
        "network": {"latency_ms": 10.0, "bytes_sent_per_sec": 100.0, "bytes_recv_per_sec": 200.0},
    }

    with client.websocket_connect("/ws/dashboard") as dashboard:
        with client.websocket_connect("/ws/agent/agent-test-2") as agent:
            agent.send_json(payload)
            msg = dashboard.receive_json()

    assert msg["type"] == "anomaly_detected"
    assert any(a["type"] == "memory_threshold_breach" for a in msg["anomalies"])
