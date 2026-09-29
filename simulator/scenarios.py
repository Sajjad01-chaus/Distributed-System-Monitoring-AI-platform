"""Synthetic host telemetry.

Each SyntheticHost evolves a small state machine per tick and renders a payload in the
same shape the real agent sends (flat headline keys + nested cpu/memory/disk/network),
including every field the backend's AI engine reads, so load tests exercise the real
detection path rather than a cheap one.

Telemetry scenarios change the numbers. Transport scenarios (flapping, duplicates,
out-of-order) are applied by the fleet runner, which reads them from `host.scenario`.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

TELEMETRY_SCENARIOS = ("normal", "cpu_spike", "memory_leak", "disk_fill", "network_degradation")
TRANSPORT_SCENARIOS = ("flapping", "duplicates", "out_of_order")
SCENARIOS = TELEMETRY_SCENARIOS + TRANSPORT_SCENARIOS

DEFAULT_MIX = {
    "normal": 0.70, "cpu_spike": 0.06, "memory_leak": 0.06, "disk_fill": 0.04,
    "network_degradation": 0.04, "flapping": 0.04, "duplicates": 0.03, "out_of_order": 0.03,
}


def parse_mix(spec: str | None) -> Dict[str, float]:
    """'normal=0.8,memory_leak=0.2' -> normalised weights. None/'' -> DEFAULT_MIX."""
    if not spec:
        return dict(DEFAULT_MIX)
    mix: Dict[str, float] = {}
    for part in spec.split(","):
        name, _, weight = part.partition("=")
        name = name.strip()
        if name not in SCENARIOS:
            raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
        mix[name] = float(weight) if weight else 1.0
    total = sum(mix.values())
    if total <= 0:
        raise ValueError("scenario weights must sum to > 0")
    return {k: v / total for k, v in mix.items()}


def assign_scenarios(n: int, mix: Dict[str, float], rng: random.Random) -> List[str]:
    """Deterministic assignment matching the mix as closely as integer counts allow."""
    counts = {name: int(n * w) for name, w in mix.items()}
    # Hand out the remainder by largest fractional part so totals add up to n.
    remainder = sorted(mix, key=lambda k: n * mix[k] - counts[k], reverse=True)
    for name in remainder[: n - sum(counts.values())]:
        counts[name] += 1
    assigned = [name for name, c in counts.items() for _ in range(c)]
    rng.shuffle(assigned)
    return assigned


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class SyntheticHost:
    agent_id: str
    scenario: str
    rng: random.Random
    cores: int = 8
    mem_total_mb: float = 16384.0
    disk_total_gb: float = 512.0
    seq: int = 0
    # Mean-reverting baselines; the live values random-walk around them.
    base_cpu: float = field(init=False)
    base_mem: float = field(init=False)
    base_disk: float = field(init=False)
    base_latency: float = field(init=False)
    cpu: float = field(init=False)
    mem: float = field(init=False)
    disk: float = field(init=False)
    latency: float = field(init=False)
    packet_loss: float = 0.0
    _spike_left: int = 0

    def __post_init__(self) -> None:
        r = self.rng
        self.cores = r.choice([2, 4, 8, 16, 32])
        self.mem_total_mb = r.choice([4096, 8192, 16384, 32768, 65536])
        self.base_cpu = r.uniform(10, 45)
        self.base_mem = r.uniform(30, 65)
        self.base_disk = r.uniform(20, 70)
        self.base_latency = r.uniform(5, 40)
        self.cpu, self.mem, self.disk, self.latency = (
            self.base_cpu, self.base_mem, self.base_disk, self.base_latency)

    # --- dynamics -------------------------------------------------------------
    def step(self) -> None:
        r = self.rng
        # Ornstein-Uhlenbeck-style pull back to baseline plus noise.
        self.cpu = _clamp(self.cpu + 0.3 * (self.base_cpu - self.cpu) + r.gauss(0, 4))
        self.mem = _clamp(self.mem + 0.2 * (self.base_mem - self.mem) + r.gauss(0, 1))
        self.disk = _clamp(self.disk + r.gauss(0, 0.05))
        self.latency = max(0.5, self.latency + 0.3 * (self.base_latency - self.latency) + r.gauss(0, 3))
        self.packet_loss = max(0.0, self.packet_loss * 0.7 + max(0.0, r.gauss(0, 0.05)))

        if self.scenario == "cpu_spike":
            if self._spike_left == 0 and r.random() < 0.08:
                self._spike_left = r.randint(3, 10)
            if self._spike_left:
                self._spike_left -= 1
                self.cpu = _clamp(r.uniform(88, 100))
        elif self.scenario == "memory_leak":
            self.base_mem += r.uniform(1.5, 3.5)       # steady climb the trend detector should see
            if self.base_mem >= 98:                    # "OOM kill + restart"
                self.base_mem = r.uniform(30, 50)
            self.mem = _clamp(self.base_mem + r.gauss(0, 0.5))
        elif self.scenario == "disk_fill":
            self.base_disk = min(99.5, self.base_disk + r.uniform(0.2, 0.8))
            self.disk = _clamp(self.base_disk)
        elif self.scenario == "network_degradation":
            self.base_latency = min(900.0, self.base_latency * r.uniform(1.02, 1.10))
            self.packet_loss = min(25.0, self.packet_loss + r.uniform(0.0, 0.8))

    # --- rendering ------------------------------------------------------------
    def payload(self, run_id: str, sent_at: float | None = None) -> Dict[str, Any]:
        """Advance one tick and render. `_sim` carries load-test bookkeeping only."""
        self.step()
        self.seq += 1
        r = self.rng
        now = time.time() if sent_at is None else sent_at
        available_mb = self.mem_total_mb * (1 - self.mem / 100)
        load1 = self.cpu / 100 * self.cores * r.uniform(0.8, 1.2)
        bytes_recv = r.uniform(5e4, 5e6)
        return {
            "agent_id": self.agent_id,
            # Protocol idempotency key, same as the real agent: (agent_id, boot_id, seq).
            "boot_id": run_id,
            "seq": self.seq,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
            "platform": {"system": "Linux", "node": self.agent_id, "release": "6.8-sim", "machine": "x86_64"},
            "cpu_usage": round(self.cpu, 2),
            "memory_usage": round(self.mem, 2),
            "disk_usage": round(self.disk, 2),
            "network_latency": round(self.latency, 2),
            "cpu": {
                "usage_percent": round(self.cpu, 2),
                "load_avg_1m": round(load1, 2),
                "load_avg_5m": round(load1 * r.uniform(0.85, 1.05), 2),
                "load_avg_15m": round(load1 * r.uniform(0.7, 1.0), 2),
                "core_count": self.cores,
                "context_switches": int(self.cpu * 1e4 * self.cores + r.uniform(0, 1e4)),
            },
            "memory": {
                "total_mb": self.mem_total_mb,
                "used_mb": round(self.mem_total_mb - available_mb, 1),
                "available_mb": round(available_mb, 1),
                "usage_percent": round(self.mem, 2),
                "swap_usage_percent": round(_clamp((self.mem - 80) * 3), 2),
            },
            "disk": {
                "usage_percent": round(self.disk, 2),
                "total_gb": self.disk_total_gb,
                "used_gb": round(self.disk_total_gb * self.disk / 100, 1),
                "free_gb": round(self.disk_total_gb * (1 - self.disk / 100), 1),
                "read_bytes_per_sec": round(r.uniform(1e4, 5e7), 0),
                "write_bytes_per_sec": round(r.uniform(1e4, 3e7), 0),
                "io_wait_percent": round(_clamp(r.gauss(2, 1.5) + (self.disk > 95) * 20), 2),
            },
            "network": {
                "latency_ms": round(self.latency, 2),
                "packet_loss_percent": round(self.packet_loss, 3),
                "bandwidth_usage_percent": round(_clamp(bytes_recv / 1.25e7 * 100), 2),
                "connections_count": r.randint(20, 800),
                "bytes_sent_per_sec": round(bytes_recv * r.uniform(0.2, 0.9), 0),
                "bytes_recv_per_sec": round(bytes_recv, 0),
            },
            "processes": [
                {"pid": 1000 + i, "name": name, "cpu_percent": round(self.cpu * share, 1),
                 "memory_percent": round(self.mem * share, 1)}
                for i, (name, share) in enumerate(
                    (("postgres", 0.35), ("python", 0.25), ("nginx", 0.1), ("java", 0.2), ("sshd", 0.01)))
            ],
            # (agent_id, seq) is unique per message and stable across retries/duplicates:
            # exactly the idempotency key the Phase 2 pipeline will dedupe on.
            "_sim": {"run_id": run_id, "msg_id": f"{self.agent_id}:{self.seq}", "seq": self.seq,
                     "sent_at": now, "scenario": self.scenario},
        }


# Which backend anomaly types count as "caught it" for each injected scenario. Hosts whose
# scenario is transport-only behave like normal hosts telemetry-wise.
EXPECTED_DETECTIONS = {
    "cpu_spike": {"cpu_threshold_breach", "cpu_trend_anomaly"},
    "memory_leak": {"memory_leak_pattern", "memory_threshold_breach"},
    "disk_fill": {"disk_threshold_breach"},
    "network_degradation": {"network_latency_high"},
}


def detection_quality(host_scenarios: Dict[str, str], detections: Dict[str, set]) -> Dict[str, Any]:
    """Score the backend's anomaly detection against the ground truth we injected.

    recall: share of faulty hosts with at least one matching anomaly.
    false_positive_rate: share of healthy hosts that got any anomaly at all.
    Only pass hosts that actually connected, so connectivity failures aren't scored as misses.
    """
    by_scenario: Dict[str, Dict[str, Any]] = {}
    for scenario, expected in EXPECTED_DETECTIONS.items():
        hosts = [h for h, s in host_scenarios.items() if s == scenario]
        caught = sum(1 for h in hosts if detections.get(h, set()) & expected)
        by_scenario[scenario] = {"hosts": len(hosts), "detected": caught,
                                 "recall": round(caught / len(hosts), 3) if hosts else None}
    healthy = [h for h, s in host_scenarios.items() if s not in EXPECTED_DETECTIONS]
    flagged = sum(1 for h in healthy if detections.get(h))
    return {"by_scenario": by_scenario, "healthy_hosts": len(healthy), "healthy_flagged": flagged,
            "false_positive_rate": round(flagged / len(healthy), 3) if healthy else None}
