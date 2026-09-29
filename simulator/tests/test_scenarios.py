import random

import numpy as np
import pytest

from scenarios import SCENARIOS, SyntheticHost, assign_scenarios, parse_mix
from stats import StageStats, percentile

# Every field backend/app/services/ai_engine.py reads; if the simulator omits one the
# load test would silently skip part of the detection path.
AI_FIELDS = {
    "cpu": ["usage_percent", "load_avg_1m", "load_avg_5m", "context_switches"],
    "memory": ["usage_percent", "available_mb", "swap_usage_percent"],
    "disk": ["usage_percent", "read_bytes_per_sec", "write_bytes_per_sec", "io_wait_percent"],
    "network": ["latency_ms", "packet_loss_percent", "bandwidth_usage_percent", "connections_count",
                "bytes_sent_per_sec", "bytes_recv_per_sec"],
}


def test_parse_mix_normalises_and_rejects_unknown():
    assert parse_mix("normal=3,memory_leak=1") == {"normal": 0.75, "memory_leak": 0.25}
    with pytest.raises(ValueError):
        parse_mix("normal=1,meteor_strike=1")


def test_assign_scenarios_exact_counts_and_deterministic():
    mix = parse_mix("normal=0.7,cpu_spike=0.2,flapping=0.1")
    a = assign_scenarios(101, mix, random.Random(1))
    assert len(a) == 101
    assert a.count("normal") in (70, 71) and a.count("cpu_spike") in (20, 21)
    assert a == assign_scenarios(101, mix, random.Random(1))


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_payload_shape_and_bounds(scenario):
    host = SyntheticHost("sim-x-00001", scenario, random.Random(7))
    for _ in range(300):
        p = host.payload("run", sent_at=1_700_000_000.0)
        for section, keys in AI_FIELDS.items():
            for key in keys:
                assert key in p[section], f"{scenario}: missing {section}.{key}"
        for key in ("cpu_usage", "memory_usage", "disk_usage"):
            assert 0 <= p[key] <= 100
        assert p["network_latency"] > 0
    assert p["_sim"]["seq"] == 300 and p["_sim"]["msg_id"] == "sim-x-00001:300"


def test_memory_leak_trips_the_backend_trend_threshold():
    """ai_engine flags a leak when the fitted slope over 15 samples exceeds 2%/interval."""
    host = SyntheticHost("sim-x-00002", "memory_leak", random.Random(3))
    host.base_mem = host.mem = 30.0
    mem = [host.payload("run")["memory_usage"] for _ in range(15)]
    assert np.polyfit(range(15), mem, 1)[0] > 2


def test_same_seed_same_telemetry():
    def series(seed):
        h = SyntheticHost("sim-x-00003", "cpu_spike", random.Random(seed))
        return [h.payload("run", sent_at=0.0)["cpu_usage"] for _ in range(50)]
    assert series(11) == series(11)
    assert series(11) != series(12)


def test_percentile_and_duplicate_tracking():
    assert percentile([], 99) is None
    assert percentile([1, 2, 3, 4], 50) == 2
    assert percentile(list(range(1, 101)), 99) == 99
    s = StageStats()
    s.record_delivery("a:1", 0.01)
    s.record_delivery("a:1", 0.02)
    assert len(s.delivered_ids) == 1 and s.duplicates_delivered == 1 and s.e2e_latency_s == [0.01]


def test_detection_quality_scores_against_injected_faults():
    from scenarios import detection_quality
    hosts = {"a": "memory_leak", "b": "memory_leak", "c": "normal", "d": "flapping", "e": "cpu_spike"}
    detections = {"a": {"memory_leak_pattern"}, "b": {"cpu_threshold_breach"}, "d": {"system_anomaly"}}
    q = detection_quality(hosts, detections)
    assert q["by_scenario"]["memory_leak"] == {"hosts": 2, "detected": 1, "recall": 0.5}
    assert q["by_scenario"]["cpu_spike"]["recall"] == 0.0
    assert q["by_scenario"]["disk_fill"]["recall"] is None
    assert (q["healthy_hosts"], q["healthy_flagged"], q["false_positive_rate"]) == (2, 1, 0.5)
