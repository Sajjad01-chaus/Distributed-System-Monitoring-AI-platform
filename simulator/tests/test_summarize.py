import json

from summarize import summarize


def _stage(agents, delivered, p99, stall=False):
    return {"agents": agents, "agents_connected": agents, "target_msgs_per_s": agents / 10,
            "delivered_rate": delivered, "connect_failures": 0,
            "e2e_latency_ms": {"p50": p99 / 2, "p99": p99}, "api_probe_ms": {"p99": 10.0},
            "detection_quality": {"false_positive_rate": 0.25}, "stall_detected": stall}


def test_median_range_and_stall_exclusion(tmp_path):
    runs = [[_stage(25, 2.0, 100), _stage(50, 4.0, 900)],
            [_stage(25, 2.4, 300), _stage(50, 1.0, 99999, stall=True)],
            [_stage(25, 2.2, 200)]]
    paths = []
    for i, stages in enumerate(runs):
        p = tmp_path / f"run{i}.json"
        p.write_text(json.dumps({"stages": stages}))
        paths.append(str(p))

    table = summarize(paths)

    assert "| Delivered msg/s | 2.2 [2.0–2.4] | 4.0 |" in table
    assert "| E2E p99 (ms) | 200 [100–300] | 900 |" in table
    assert "stalled stages excluded: 1" in table
