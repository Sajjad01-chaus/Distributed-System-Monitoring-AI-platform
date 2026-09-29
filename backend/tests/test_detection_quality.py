"""Detection quality against the simulator's host model, replayed sample by sample the way the
detect worker sees it. Seeds differ from the published benchmark (seed 42), so the benchmark
remains a held-out check rather than the data the thresholds were checked against."""
import random

import pytest

from app.detection.detectors import evaluate
from scenarios import EXPECTED_DETECTIONS, SyntheticHost

HOSTS, SAMPLES, WINDOW = 200, 30, 30


def replay(scenario: str, seed: int) -> set:
    host = SyntheticHost(f"h{seed}", scenario, random.Random(seed))
    window, fired = [], set()
    for i in range(SAMPLES):
        p = host.payload("run", sent_at=1_700_000_000 + 10 * i)
        window.append({"t": 1_700_000_000 + 10 * i, "cpu": p["cpu_usage"], "mem": p["memory_usage"],
                       "disk": p["disk_usage"], "lat": p["network_latency"],
                       "loss": p["network"]["packet_loss_percent"]})
        fired |= evaluate(window[-WINDOW:]).keys()
    return fired


def rate(scenario: str, hit) -> float:
    return sum(bool(hit(replay(scenario, 1000 + i))) for i in range(HOSTS)) / HOSTS


def test_healthy_hosts_rarely_alert():
    assert rate("normal", lambda fired: fired) <= 0.02


@pytest.mark.parametrize("scenario,min_recall", [
    ("memory_leak", 0.95), ("disk_fill", 0.95), ("cpu_spike", 0.6), ("network_degradation", 0.6)])
def test_injected_faults_are_caught_within_five_minutes(scenario, min_recall):
    assert rate(scenario, lambda fired: fired & EXPECTED_DETECTIONS[scenario]) >= min_recall
