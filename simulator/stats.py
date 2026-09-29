"""Counters and latency summaries for a load-test stage."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (p in 0..100). None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize_ms(values_s: List[float]) -> Dict[str, Optional[float]]:
    """Seconds in, milliseconds out, rounded for reports."""
    def ms(v: Optional[float]) -> Optional[float]:
        return None if v is None else round(v * 1000, 1)
    return {"count": len(values_s), "p50": ms(percentile(values_s, 50)), "p95": ms(percentile(values_s, 95)),
            "p99": ms(percentile(values_s, 99)), "max": ms(max(values_s) if values_s else None)}


@dataclass
class StageStats:
    # Sender side
    sent_unique: int = 0            # distinct messages that left the client at least once
    sent_frames: int = 0            # including deliberate duplicates
    duplicates_sent: int = 0
    reordered: int = 0
    send_errors: int = 0
    connects: int = 0
    connect_failures: int = 0
    disconnects: int = 0            # unexpected, i.e. not a scripted flap
    flaps: int = 0
    connected_agents: set = field(default_factory=set)
    sender_lag_s: List[float] = field(default_factory=list)   # scheduled vs actual send time
    generator_loop_lag_s: List[float] = field(default_factory=list)  # the simulator's own event-loop delay
    send_call_s: List[float] = field(default_factory=list)    # time blocked in ws.send (backpressure)

    # Observer side
    observer_errors: int = 0
    delivered_ids: set = field(default_factory=set)
    duplicates_delivered: int = 0
    e2e_latency_s: List[float] = field(default_factory=list)
    anomalies: Counter = field(default_factory=Counter)
    anomalies_by_agent: Dict[str, set] = field(default_factory=dict)

    # API probe
    probe_latency_s: List[float] = field(default_factory=list)
    probe_errors: int = 0

    def record_delivery(self, msg_id: str, latency_s: float) -> None:
        if msg_id in self.delivered_ids:
            self.duplicates_delivered += 1
            return
        self.delivered_ids.add(msg_id)
        self.e2e_latency_s.append(latency_s)
