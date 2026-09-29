"""Detectors on hand-built series: each must catch its fault and ignore healthy noise."""
import random

from app.detection.detectors import evaluate, linear_trend
from app.detection.outliers import FleetOutlierModel, features


def series(n=20, step_s=10, **fields):
    """fields: name -> callable(i) giving the value of sample i."""
    base = {"cpu": lambda i: 30.0, "mem": lambda i: 50.0, "disk": lambda i: 40.0,
            "lat": lambda i: 20.0, "loss": lambda i: 0.0}
    base.update(fields)
    return [{"t": 1_700_000_000 + i * step_s, **{k: f(i) for k, f in base.items()}} for i in range(n)]


def noisy(level, sd, seed=1):
    rng = random.Random(seed)
    return lambda i: level + rng.gauss(0, sd)


def test_healthy_noisy_host_fires_nothing():
    for seed in range(20):
        s = series(30, cpu=noisy(45, 5, seed), mem=noisy(60, 1.5, seed + 1), disk=noisy(70, 0.05, seed + 2),
                   lat=noisy(35, 3, seed + 3), loss=lambda i: 0.05)
        assert evaluate(s) == {}, f"false positive with seed {seed}"


def test_single_cpu_spike_is_ignored_but_sustained_saturation_fires():
    spike = series(cpu=lambda i: 99.0 if i == 19 else 30.0)
    assert "cpu_threshold_breach" not in evaluate(spike)
    sustained = series(cpu=lambda i: 97.0 if i >= 16 else 30.0)
    assert evaluate(sustained)["cpu_threshold_breach"]["severity"] == "critical"


def test_memory_leak_detected_with_eta():
    leak = series(mem=lambda i: 40 + 2.0 * i + random.Random(i).gauss(0, 0.3))   # +12 %/min
    found = evaluate(leak)["memory_leak_pattern"]
    assert 10 < found["slope_pct_per_min"] < 14 and found["r2"] > 0.95
    assert found["eta_minutes"] < 10


def test_disk_full_is_forecast_before_threshold():
    filling = series(disk=lambda i: 60 + 0.5 * i)                     # 3 %/min, now ~70%
    forecast = evaluate(filling)["disk_full_forecast"]
    assert "disk_threshold_breach" not in evaluate(filling)
    assert 8 < forecast["eta_minutes"] < 12
    assert "disk_threshold_breach" in evaluate(series(disk=lambda i: 95.0))


def test_relative_latency_degradation_caught_below_absolute_threshold():
    degrading = series(lat=lambda i: 10 * 1.08 ** i)                  # 10 ms -> ~43 ms
    found = evaluate(degrading)["network_latency_degradation"]
    assert found["latency_ms"] < 200 and found["latency_ms"] / found["baseline_ms"] > 1.5
    assert "network_latency_high" in evaluate(series(lat=lambda i: 350.0))
    assert "packet_loss_high" in evaluate(series(loss=lambda i: 8.0))


def test_out_of_order_input_is_callers_job_but_trend_uses_time():
    t = linear_trend([{"t": 0, "m": 0.0}, {"t": 60, "m": 1.0}, {"t": 120, "m": 2.0}], "m")
    assert abs(t.slope_per_min - 1.0) < 1e-9 and abs(t.r2 - 1.0) < 1e-9


def test_fleet_model_flags_only_the_odd_host_and_nothing_before_training():
    rng = random.Random(0)
    healthy = [{"cpu": rng.gauss(30, 5), "mem": rng.gauss(50, 5), "disk": rng.gauss(50, 5),
                "lat": rng.gauss(20, 3), "loss": 0.0} for _ in range(2000)]
    model = FleetOutlierModel(min_train=500)
    assert model.flags([features(healthy[0])]) == [0]
    model.observe([features(s) for s in healthy])
    assert model.maybe_refit(now=0)
    odd = features({"cpu": 99, "mem": 97, "disk": 99, "lat": 900, "loss": 20})
    assert model.flags([odd]) == [1]
    flagged = sum(model.flags([features(s) for s in healthy]))
    assert flagged / len(healthy) < 0.01, "cut-off is an extreme tail, not a fixed 10%"


def test_fleet_outlier_needs_persistence():
    s = series()
    for i, sample in enumerate(s):
        sample["out"] = 1 if i == 19 else 0
    assert "fleet_outlier" not in evaluate(s)
    for sample in s[-4:]:
        sample["out"] = 1
    assert "fleet_outlier" in evaluate(s)
