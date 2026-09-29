"""Per-agent anomaly detectors: pure functions over one agent's recent samples.

Design rules, each a response to a Phase 1 finding (docs/benchmarks/phase1-baseline.md):
- every detector looks at *one agent's* history, never a fleet-wide mix;
- level alerts need the condition to persist (k of the last n samples), so a single noisy
  sample never pages anyone;
- trend alerts need both a meaningful slope *and* a good linear fit (R^2), so noise around a
  steady level doesn't look like a leak;
- everything is O(window) arithmetic: well under a millisecond per agent.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# A sample as stored in the per-agent window.
#   t: epoch seconds, cpu/mem/disk: percent, lat: ms, loss: percent, out: fleet-outlier flag
Sample = Dict[str, float]


@dataclass(frozen=True)
class Thresholds:
    cpu_high: float = 90.0
    mem_high: float = 90.0
    disk_high: float = 90.0
    latency_high_ms: float = 200.0
    loss_high_pct: float = 5.0
    sustain_k: int = 3                  # condition must hold in k ...
    sustain_n: int = 5                  # ... of the last n samples
    trend_min_samples: int = 8
    trend_min_r2: float = 0.8
    leak_min_pct_per_min: float = 1.0   # memory growth that counts as a leak ...
    leak_min_rise_pct: float = 8.0      # ... and it must have actually risen this much in the window
    leak_min_r2: float = 0.9            # leaks are near-linear; wandering usage fits worse
    disk_min_rise_pct: float = 1.0      # a forecast needs a real rise, not a noise wiggle
    forecast_horizon_min: float = 24 * 60
    max_extrapolation: float = 10.0     # never forecast further than 10x the observed time span
    degradation_ratio: float = 1.5      # latency vs the agent's own earlier baseline ...
    degradation_min_z: float = 4.0      # ... and far outside that baseline's own noise (robust z)
    degradation_min_delta_ms: float = 20.0  # ... and big enough to matter (1 -> 11 ms is not an incident)


DEFAULT = Thresholds()


@dataclass(frozen=True)
class Trend:
    slope_per_min: float
    r2: float


def linear_trend(samples: Sequence[Sample], key: str) -> Optional[Trend]:
    """Least-squares slope (units per minute) and R^2 of `key` against time."""
    points = [(s["t"], s[key]) for s in samples if s.get(key) is not None]
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 60.0 for t, _ in points]
    ys = [y for _, y in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return Trend(slope, r2)


def _span_min(samples: Sequence[Sample]) -> float:
    return (samples[-1]["t"] - samples[0]["t"]) / 60.0 if len(samples) > 1 else 0.0


def _sustained(samples: Sequence[Sample], key: str, above: float, th: Thresholds) -> Optional[List[float]]:
    recent = [s[key] for s in samples[-th.sustain_n:] if s.get(key) is not None]
    hits = [v for v in recent if v > above]
    return recent if len(hits) >= th.sustain_k else None


def _anomaly(kind: str, severity: str, description: str, **evidence) -> Dict:
    return {"type": kind, "severity": severity, "description": description,
            **{k: round(v, 3) if isinstance(v, float) else v for k, v in evidence.items()}}


def cpu_saturation(samples, th=DEFAULT):
    recent = _sustained(samples, "cpu", th.cpu_high, th)
    if recent:
        peak = max(recent)
        breaching = [v for v in recent if v > th.cpu_high]   # severity from the breach, not the dip
        return _anomaly("cpu_threshold_breach", "critical" if statistics.fmean(breaching) >= 95 else "high",
                        f"CPU above {th.cpu_high:g}% in {th.sustain_k}+ of the last {th.sustain_n} samples "
                        f"(peak {peak:.0f}%)", peak=peak)
    return None


def memory_pressure(samples, th=DEFAULT):
    recent = _sustained(samples, "mem", th.mem_high, th)
    if recent:
        breaching = [v for v in recent if v > th.mem_high]
        return _anomaly("memory_threshold_breach", "critical" if statistics.fmean(breaching) >= 95 else "high",
                        f"Memory above {th.mem_high:g}% in {th.sustain_k}+ of the last {th.sustain_n} samples",
                        latest=recent[-1])
    return None


def memory_leak(samples, th=DEFAULT):
    window = samples[-max(th.trend_min_samples, 20):]
    if len(window) < th.trend_min_samples:
        return None
    trend = linear_trend(window, "mem")
    if (trend and trend.slope_per_min >= th.leak_min_pct_per_min and trend.r2 >= th.leak_min_r2
            and trend.slope_per_min * _span_min(window) >= th.leak_min_rise_pct):
        headroom = 100 - window[-1]["mem"]
        eta = headroom / trend.slope_per_min
        return _anomaly("memory_leak_pattern", "critical" if eta < 30 else "high",
                        f"Memory rising steadily at {trend.slope_per_min:.1f}%/min "
                        f"(R²={trend.r2:.2f}); exhausted in ~{eta:.0f} min",
                        slope_pct_per_min=trend.slope_per_min, r2=trend.r2, eta_minutes=eta)
    return None


def disk_space(samples, th=DEFAULT):
    latest = samples[-1].get("disk")
    if latest is not None and latest > th.disk_high:
        return _anomaly("disk_threshold_breach", "critical" if latest > 98 else "high",
                        f"Disk {latest:.1f}% full", latest=latest)
    window = samples  # the whole retained window: forecasts need as much history as we have
    if len(window) < th.trend_min_samples or latest is None:
        return None
    trend = linear_trend(window, "disk")
    span = _span_min(window)
    if (trend and trend.slope_per_min > 0 and trend.r2 >= th.trend_min_r2
            and trend.slope_per_min * span >= th.disk_min_rise_pct):
        eta = (100 - latest) / trend.slope_per_min
        if eta <= min(th.forecast_horizon_min, th.max_extrapolation * span):
            return _anomaly("disk_full_forecast", "critical" if eta < 60 else "medium",
                            f"Disk filling at {trend.slope_per_min:.2f}%/min; full in ~{eta:.0f} min",
                            slope_pct_per_min=trend.slope_per_min, r2=trend.r2, eta_minutes=eta)
    return None


def network_health(samples, th=DEFAULT):
    lat = [s["lat"] for s in samples if s.get("lat") is not None]
    loss = [s["loss"] for s in samples[-3:] if s.get("loss") is not None]
    if len(lat) >= 3:
        now = statistics.median(lat[-3:])
        if now > th.latency_high_ms:
            return _anomaly("network_latency_high", "high",
                            f"Latency {now:.0f} ms (median of last 3)", latency_ms=now)
        if len(lat) >= th.trend_min_samples:
            # Relative degradation against this agent's own earlier baseline: catches a 5 ms
            # link turning into 15 ms long before any absolute threshold would.
            early = lat[: len(lat) // 2]
            baseline = statistics.median(early)
            # Robust spread of the baseline (MAD scaled to a standard deviation).
            spread = 1.4826 * statistics.median(abs(v - baseline) for v in early) or 1.0
            trend = linear_trend(samples[-th.trend_min_samples:], "lat")
            if (baseline > 0 and now >= baseline * th.degradation_ratio
                    and now - baseline >= th.degradation_min_delta_ms
                    and (now - baseline) / spread >= th.degradation_min_z and trend
                    and trend.slope_per_min > 0 and trend.r2 >= th.trend_min_r2):
                return _anomaly("network_latency_degradation", "medium",
                                f"Latency up {now / baseline:.1f}x vs baseline ({baseline:.0f} → {now:.0f} ms)",
                                baseline_ms=baseline, latency_ms=now, r2=trend.r2)
    if len(loss) >= 3 and statistics.fmean(loss) > th.loss_high_pct:
        return _anomaly("packet_loss_high", "high", f"Packet loss {statistics.fmean(loss):.1f}%",
                        loss_pct=statistics.fmean(loss))
    return None


def fleet_outlier(samples, th=DEFAULT):
    flags = [s.get("out", 0) for s in samples[-th.sustain_n:]]
    if sum(flags) >= th.sustain_k:
        return _anomaly("fleet_outlier", "medium",
                        f"Behaves unlike the rest of the fleet in {int(sum(flags))} of the last "
                        f"{len(flags)} samples (IsolationForest)")
    return None


DETECTORS = (cpu_saturation, memory_pressure, memory_leak, disk_space, network_health, fleet_outlier)

def evaluate(samples: Sequence[Sample], th: Thresholds = DEFAULT) -> Dict[str, Dict]:
    """All anomalies currently firing for one agent, keyed by alert type. Samples must be
    in time order; the caller sorts, since the stream can deliver out of order."""
    if not samples:
        return {}
    firing = {}
    for detector in DETECTORS:
        anomaly = detector(samples, th)
        if anomaly:
            firing[anomaly["type"]] = anomaly
    return firing
