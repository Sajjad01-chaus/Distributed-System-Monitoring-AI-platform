"""Runs a small fleet against the real multi-process backend (API + persist + detect workers)
to prove the harness measures correctly. Needs a real Redis at REDIS_URL; skipped otherwise."""
import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

import fleet

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _redis_available() -> bool:
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason=f"needs a Redis server at {REDIS_URL}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "e2e.db"
    db_url = f"sqlite:///{db.as_posix()}"
    tag = uuid.uuid4().hex[:8]  # private streams/channel: safe to run against a shared Redis
    env = {**os.environ, "DATABASE_URL": db_url, "REDIS_URL": REDIS_URL,
           "TELEMETRY_STREAM": f"e2e-{tag}", "DLQ_STREAM": f"e2e-{tag}:dlq", "DASHBOARD_CHANNEL": f"e2e-{tag}",
           "WORKER_BLOCK_MS": "200"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND_DIR, env=env, check=True)

    ports = [_free_port(), _free_port()]   # two API replicas, like two containers behind the LB
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                               "--port", str(port), "--log-level", "warning"], cwd=BACKEND_DIR,
                              env={**env, "REPLICA_ID": f"replica-{i}"})
             for i, port in enumerate(ports)]
    procs += [subprocess.Popen([sys.executable, "-m", "app.worker", kind], cwd=BACKEND_DIR, env=env)
              for kind in ("persist", "detect")]
    try:
        deadline = time.time() + 60
        for port in ports:
            while True:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1)
                    break
                except OSError as err:
                    if time.time() > deadline or any(p.poll() is not None for p in procs):
                        raise RuntimeError("backend did not start") from err
                    time.sleep(0.5)
        yield {"url": f"ws://127.0.0.1:{ports[0]}", "other_replica": f"http://127.0.0.1:{ports[1]}",
               "db_url": db_url}
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=15)
        import redis
        redis.Redis.from_url(REDIS_URL).delete(env["TELEMETRY_STREAM"], env["DLQ_STREAM"])


def test_small_fleet_is_fully_delivered_and_persisted(backend):
    url, db_url = backend["url"], backend["db_url"]
    [stage] = fleet.main(["--url", url, "--agents", "5", "--interval", "0.5", "--duration", "3",
                          "--ramp-up", "0.5", "--drain", "3", "--mix", "normal=1",
                          "--database-url", db_url, "--run-prefix", "t"])

    assert stage["sent_unique"] >= 25
    assert stage["delivery_ratio"] == 1.0
    assert stage["persisted_rows"] == stage["sent_unique"]
    assert stage["e2e_latency_ms"]["p50"] is not None
    assert stage["agents_connected"] == 5
    assert stage["connect_failures"] == 0 and stage["disconnects"] == 0
    assert not stage["generator_saturated"] and not stage["stall_detected"]
    assert stage["api_probe_errors"] == 0 and stage["throttled"] == 0


def test_duplicates_are_persisted_once(backend):
    url, db_url = backend["url"], backend["db_url"]
    [stage] = fleet.main(["--url", url, "--agents", "5", "--interval", "0.5", "--duration", "3",
                          "--ramp-up", "0.5", "--drain", "3", "--mix", "duplicates=1",
                          "--database-url", db_url, "--run-prefix", "d"])

    assert stage["duplicates_sent"] > 0
    assert stage["persisted_rows"] == stage["sent_unique"] < stage["sent_frames"]
    assert stage["duplicates_delivered"] == 0


def test_commands_issued_on_another_replica_reach_agents(backend):
    [stage] = fleet.main(["--url", backend["url"], "--api-url", backend["other_replica"],
                          "--agents", "5", "--interval", "0.5", "--duration", "4", "--ramp-up", "0.5",
                          "--drain", "3", "--mix", "normal=1", "--commands-per-s", "5", "--run-prefix", "c"])

    cmds = stage["commands"]
    assert cmds["issued"] >= 10
    assert cmds["by_http_status"] == {"202": cmds["issued"]}, "every command routed and delivered"
    assert cmds["received_by_agents"] == cmds["issued"]
    assert cmds["completed"] == cmds["issued"], "every result made it back to the dashboard"
