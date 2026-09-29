"""Single-process deployment mode, for hosts without separate worker processes (e.g. a free
PaaS tier). The same consumers that normally run as `python -m app.worker ...` run here as
background tasks of the API process:

    EMBEDDED_WORKERS=persist,detect,liveness   which loops to run in-process
    DEMO_AGENTS=30                             optional synthetic fleet (simulator host model)
    DEMO_INTERVAL_S=5                          seconds between each demo agent's samples

Nothing about the pipeline changes: data still goes agent -> WebSocket -> Redis Stream ->
consumer groups -> database, so scaling out later is a configuration change, not a rewrite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import uuid
from pathlib import Path
from typing import List

log = logging.getLogger(__name__)


def _worker(kind: str, redis):
    from app.liveness import LivenessMonitor
    from app.pipeline.detect import DetectConsumer
    from app.pipeline.persist import PersistConsumer
    return {"persist": PersistConsumer, "detect": DetectConsumer, "liveness": LivenessMonitor}[kind](redis)


async def _demo_agent(host, url: str, interval: float, boot_id: str) -> None:
    """One synthetic agent speaking the real agent protocol, answering commands like the agent."""
    import websockets
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(f"{url}/ws/agent/{host.agent_id}", open_timeout=10) as ws:
                backoff = 1.0

                async def answer_commands():
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("command_id"):
                            await ws.send(json.dumps({"type": "remediation_result", "command_id": msg["command_id"],
                                                      "issue_type": msg.get("issue_type"), "success": True,
                                                      "dry_run": True, "simulated": True}))
                reader = asyncio.create_task(answer_commands())
                try:
                    while not reader.done():
                        payload = host.payload(boot_id)
                        payload.pop("_sim", None)
                        await ws.send(json.dumps(payload))
                        await asyncio.sleep(interval * random.uniform(0.9, 1.1))
                finally:
                    reader.cancel()
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(backoff * random.uniform(0.5, 1.5))
            backoff = min(backoff * 2, 30.0)


def _demo_fleet(count: int, port: int) -> List:
    sim_dir = os.getenv("SIMULATOR_DIR", str(Path(__file__).resolve().parents[2] / "simulator"))
    if sim_dir not in sys.path:
        sys.path.append(sim_dir)
    from scenarios import SyntheticHost, assign_scenarios, parse_mix

    rng = random.Random(42)
    # Faults are over-represented so a demo shows alerts within minutes; transport-level
    # scenarios are left to the load tests.
    mix = parse_mix("normal=0.6,cpu_spike=0.1,memory_leak=0.1,disk_fill=0.1,network_degradation=0.1")
    boot_id = uuid.uuid4().hex[:12]
    interval = float(os.getenv("DEMO_INTERVAL_S", "5"))
    hosts = [SyntheticHost(f"demo-{i:03d}", s, random.Random(f"demo:{i}"))
             for i, s in enumerate(assign_scenarios(count, mix, rng))]
    url = f"ws://127.0.0.1:{port}"
    return [_demo_agent(h, url, interval, boot_id) for h in hosts]


def start(redis) -> List[asyncio.Task]:
    tasks = []
    for kind in filter(None, (k.strip() for k in os.getenv("EMBEDDED_WORKERS", "").split(","))):
        tasks.append(asyncio.create_task(_worker(kind, redis).run(), name=f"embedded-{kind}"))
        log.info("embedded %s worker started", kind)
    count = int(os.getenv("DEMO_AGENTS", "0"))
    if count:
        port = int(os.getenv("PORT", "8000"))
        tasks += [asyncio.create_task(coro) for coro in _demo_fleet(count, port)]
        log.info("demo fleet of %d agents started", count)
    return tasks
