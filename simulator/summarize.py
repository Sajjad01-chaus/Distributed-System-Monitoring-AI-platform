"""Merge repeated fleet.py reports into one markdown table: median [min-max] per stage.

    python summarize.py ../docs/benchmarks/phase1-baseline-run*.json

Stages flagged stall_detected are excluded (and counted) rather than averaged in.
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, List

METRICS: List[tuple[str, Callable[[Dict[str, Any]], Any], str]] = [
    ("Agents connected", lambda s: s["agents_connected"], "{:.0f}"),
    ("Target msg/s", lambda s: s["target_msgs_per_s"], "{:.1f}"),
    ("Delivered msg/s", lambda s: s["delivered_rate"], "{:.1f}"),
    ("Failed connects", lambda s: s["connect_failures"], "{:.0f}"),
    ("E2E p50 (ms)", lambda s: s["e2e_latency_ms"]["p50"], "{:,.0f}"),
    ("E2E p99 (ms)", lambda s: s["e2e_latency_ms"]["p99"], "{:,.0f}"),
    ("`/health` p99 (ms)", lambda s: s["api_probe_ms"]["p99"], "{:,.0f}"),
    ("Healthy hosts falsely flagged", lambda s: s["detection_quality"]["false_positive_rate"], "{:.0%}"),
]


def fmt_cell(values: List[float], pattern: str) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "n/a"
    med = pattern.format(statistics.median(values))
    if len(values) == 1 or min(values) == max(values):
        return med
    return f"{med} [{pattern.format(min(values))}–{pattern.format(max(values))}]"


def summarize(paths: List[str]) -> str:
    by_agents: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    excluded = 0
    for path in paths:
        with open(path) as f:
            for stage in json.load(f)["stages"]:
                if stage.get("stall_detected"):
                    excluded += 1
                    continue
                by_agents[stage["agents"]].append(stage)

    agents = sorted(by_agents)
    lines = ["| Metric | " + " | ".join(f"{a} agents" for a in agents) + " |",
             "|---|" + "---|" * len(agents)]
    for label, get, pattern in METRICS:
        cells = [fmt_cell([get(s) for s in by_agents[a]], pattern) for a in agents]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    runs = {a: len(by_agents[a]) for a in agents}
    lines.append("")
    lines.append(f"Median [min–max] over {len(paths)} runs; valid stages per column: {runs}; "
                 f"stalled stages excluded: {excluded}.")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")  # en-dashes; Windows consoles default to cp1252
    print(summarize(sys.argv[1:]))
