"""Runs a tiny fleet against a real uvicorn process to prove the harness measures correctly."""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import fleet

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "e2e.db"
    db_url = f"sqlite:///{db.as_posix()}"
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=BACKEND_DIR, env={**os.environ, "DATABASE_URL": db_url},
    )
    try:
        deadline = time.time() + 60
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError as err:
                if time.time() > deadline or proc.poll() is not None:
                    raise RuntimeError("backend did not start") from err
                time.sleep(0.5)
        yield f"ws://127.0.0.1:{port}", db_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_small_fleet_is_fully_delivered_and_persisted(backend):
    url, db_url = backend
    [stage] = fleet.main(["--url", url, "--agents", "5", "--interval", "0.5", "--duration", "3",
                          "--ramp-up", "0.5", "--drain", "2", "--mix", "normal=1",
                          "--database-url", db_url, "--run-prefix", "t"])

    assert stage["sent_unique"] >= 25
    assert stage["delivery_ratio"] == 1.0
    assert stage["persisted_rows"] == stage["sent_unique"]
    assert stage["e2e_latency_ms"]["p50"] is not None
    assert stage["agents_connected"] == 5
    assert stage["connect_failures"] == 0 and stage["disconnects"] == 0
    assert not stage["generator_saturated"] and not stage["stall_detected"]
    assert stage["api_probe_errors"] == 0
