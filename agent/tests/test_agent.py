import json

import pytest

from agent import SystemMonitorAgent, load_config


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the agent writes <agent_id>.log to cwd
    a = SystemMonitorAgent({"agent_id": "agent-unit"})
    a.websocket = FakeWebSocket()
    return a


async def _no_latency():
    return {"latency_ms": 0}


@pytest.mark.asyncio
async def test_collect_all_metrics_has_flat_keys(agent, monkeypatch):
    monkeypatch.setattr(agent.network_collector, "measure_latency", _no_latency)
    monkeypatch.setattr(agent, "check_network_connectivity", lambda: _async({}))

    metrics = await agent.collect_all_metrics()

    for key in ("cpu_usage", "memory_usage", "disk_usage", "network_latency", "processes"):
        assert key in metrics
    assert 0 <= metrics["cpu_usage"] <= 100


@pytest.mark.asyncio
async def test_remediation_is_dry_run_by_default(agent):
    await agent.process_command({"type": "remediate", "issue_type": "cpu_threshold_breach"})

    [result] = agent.websocket.sent
    assert result["type"] == "remediation_result"
    assert result["dry_run"] is True
    assert "would_inspect" in result["output"]


@pytest.mark.asyncio
async def test_run_script_command_is_rejected(agent):
    await agent.process_command({"type": "run_script", "script": "echo pwned"})
    assert agent.websocket.sent == []


@pytest.mark.asyncio
async def test_remote_config_cannot_disable_dry_run(agent):
    await agent.process_command({"type": "update_config",
                                 "config": {"collection_interval": 5, "remediation_dry_run": False}})

    assert agent.collection_interval == 5
    assert agent.remediation_dry_run is True
    assert agent.websocket.sent[0]["rejected"] == ["remediation_dry_run"]


def test_env_overrides_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("agent_id: from-file\nserver_url: ws://file:8000\n")
    monkeypatch.setenv("SERVER_URL", "ws://env:9000")

    config = load_config(str(cfg))

    assert config == {"agent_id": "from-file", "server_url": "ws://env:9000"}


async def _async(value):
    return value
