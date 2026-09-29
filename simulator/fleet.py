"""Synthetic agent fleet + load harness.

Spins up N simulated agents that speak the real agent protocol (WebSocket
/ws/agent/{id}), a dashboard observer that timestamps every broadcast it receives,
and an HTTP probe that measures API responsiveness while the fleet is running.

    python fleet.py --agents 100 --interval 1 --duration 60
    python fleet.py --agents 50,100,250,500 --duration 45 --report ../docs/benchmarks/baseline.json

Multiple --agents values run as consecutive stages (a ramp) so you can see where
the backend stops keeping up.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from scenarios import SyntheticHost, assign_scenarios, detection_quality, parse_mix
from stats import StageStats, summarize_ms

CONNECT_ERRORS = (OSError, asyncio.TimeoutError, WebSocketException)


# --- simulated agent ----------------------------------------------------------------
async def _drain(ws) -> None:
    """Consume server->agent commands so the socket's receive buffer never fills."""
    try:
        async for _ in ws:
            pass
    except ConnectionClosed:
        pass


async def run_agent(host: SyntheticHost, args, run_id: str, stats: StageStats,
                    stop: asyncio.Event, start_delay: float) -> None:
    rng = host.rng
    await asyncio.sleep(start_delay)
    uri = f"{args.url}/ws/agent/{host.agent_id}"
    backoff = 0.5
    held: Optional[str] = None  # out_of_order: a frame deliberately sent late

    while not stop.is_set():
        try:
            ws = await asyncio.wait_for(websockets.connect(uri, open_timeout=10, close_timeout=2), timeout=15)
        except CONNECT_ERRORS:
            stats.connect_failures += 1
            await asyncio.sleep(backoff * rng.uniform(0.5, 1.5))   # exponential backoff with jitter
            backoff = min(backoff * 2, 10.0)
            continue

        stats.connects += 1
        stats.connected_agents.add(host.agent_id)
        backoff = 0.5
        drainer = asyncio.create_task(_drain(ws))
        loop = asyncio.get_running_loop()
        flap_at = loop.time() + rng.uniform(5, 15) if host.scenario == "flapping" else None
        next_send = loop.time()
        try:
            while not stop.is_set():
                now = loop.time()
                if next_send > now:
                    await asyncio.sleep(next_send - now)
                    if stop.is_set():
                        break
                stats.sender_lag_s.append(max(0.0, loop.time() - next_send))
                next_send += args.interval * rng.uniform(0.9, 1.1)

                frame = json.dumps(host.payload(run_id, sent_at=time.time()))

                if host.scenario == "out_of_order" and held is None and rng.random() < 0.2:
                    held = frame           # send it after the next one
                    continue
                frames = [frame]
                if held is not None:
                    frames.append(held)
                    held = None
                    stats.reordered += 1
                if host.scenario == "duplicates" and rng.random() < 0.2:
                    frames.append(frame)
                    stats.duplicates_sent += 1

                for i, f in enumerate(frames):
                    t0 = time.perf_counter()
                    await ws.send(f)
                    stats.send_call_s.append(time.perf_counter() - t0)
                    stats.sent_frames += 1
                    is_dup = host.scenario == "duplicates" and i > 0 and f == frames[0]
                    if not is_dup:
                        stats.sent_unique += 1

                if flap_at is not None and loop.time() >= flap_at:
                    stats.flaps += 1
                    break
        except ConnectionClosed:
            stats.disconnects += 1
            stats.send_errors += 1
        finally:
            drainer.cancel()
            try:
                await ws.close()
            except Exception:
                pass
        if flap_at is not None and not stop.is_set():
            await asyncio.sleep(rng.uniform(0.5, 3))   # offline for a moment, then reconnect


# --- observers ----------------------------------------------------------------------
async def run_observer(args, run_id: str, stats: StageStats, ready: asyncio.Event, done: asyncio.Event) -> None:
    """Dashboard client timestamping every broadcast. Reconnects rather than dying, because
    an overloaded backend refusing handshakes is a result to record, not a harness crash."""
    prefix = f"sim-{run_id}-"
    while not done.is_set():
        try:
            async with websockets.connect(f"{args.url}/ws/dashboard", max_size=None, open_timeout=30) as ws:
                ready.set()
                while not done.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    received = time.time()
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "metrics_update":
                        sim = (msg.get("metrics") or {}).get("_sim") or {}
                        if sim.get("run_id") == run_id:
                            stats.record_delivery(sim["msg_id"], received - sim["sent_at"])
                    elif kind == "anomaly_detected" and str(msg.get("agent_id", "")).startswith(prefix):
                        seen = stats.anomalies_by_agent.setdefault(msg["agent_id"], set())
                        for a in msg.get("anomalies", []):
                            stats.anomalies[a.get("type", "unknown")] += 1
                            seen.add(a.get("type", "unknown"))
        except CONNECT_ERRORS:
            stats.observer_errors += 1
            await asyncio.sleep(1)


async def run_loop_monitor(stats: StageStats, done: asyncio.Event) -> None:
    """Measures the simulator's own event-loop delay. If this is high, the generator is the
    bottleneck and the stage's numbers say more about the harness than the backend."""
    loop = asyncio.get_running_loop()
    while not done.is_set():
        t0 = loop.time()
        await asyncio.sleep(0.1)
        stats.generator_loop_lag_s.append(max(0.0, loop.time() - t0 - 0.1))


async def wait_until_idle(args) -> float:
    """Block until /health answers fast 3 times running, so a previous stage's backlog
    doesn't leak into the next stage's numbers. Returns seconds waited."""
    url = args.url.replace("ws://", "http://").replace("wss://", "https://") + "/health"
    start, fast = time.time(), 0
    while fast < 3 and time.time() - start < args.cooldown:
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).read())
            fast = fast + 1 if time.perf_counter() - t0 < 0.1 else 0
        except Exception:
            fast = 0
        await asyncio.sleep(1)
    return time.time() - start


async def run_probe(args, stats: StageStats, done: asyncio.Event) -> None:
    """GET /health once a second. It does no I/O, so its latency is pure event-loop delay."""
    url = args.url.replace("ws://", "http://").replace("wss://", "https://") + "/health"
    while not done.is_set():
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).read())
            stats.probe_latency_s.append(time.perf_counter() - t0)
        except Exception:
            stats.probe_errors += 1
        await asyncio.sleep(1)


def count_persisted(database_url: str, run_id: str) -> Optional[int]:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        print("  (sqlalchemy not installed; skipping persisted-row count)", file=sys.stderr)
        return None
    engine = create_engine(database_url)
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM system_metrics WHERE agent_id LIKE :p"),
                            {"p": f"sim-{run_id}-%"}).scalar_one()


# --- stage orchestration ------------------------------------------------------------
async def run_stage(args, n_agents: int, stage_idx: int) -> Dict[str, Any]:
    run_id = f"{args.run_prefix}{stage_idx}n{n_agents}"
    master = random.Random(f"{args.seed}:{n_agents}")
    scenarios = assign_scenarios(n_agents, parse_mix(args.mix), master)
    hosts = [SyntheticHost(f"sim-{run_id}-{i:05d}", scenarios[i], random.Random(f"{args.seed}:{run_id}:{i}"))
             for i in range(n_agents)]

    cooldown_s = await wait_until_idle(args)
    stats = StageStats()
    stop, done, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observer = asyncio.create_task(run_observer(args, run_id, stats, ready, done))
    try:
        await asyncio.wait_for(ready.wait(), timeout=60)
    except asyncio.TimeoutError:
        print("  warning: dashboard observer could not connect; delivery numbers will be missing",
              file=sys.stderr)
    probe = asyncio.create_task(run_probe(args, stats, done))
    monitor = asyncio.create_task(run_loop_monitor(stats, done))

    started = time.time()
    agents = [asyncio.create_task(run_agent(h, args, run_id, stats, stop,
                                            start_delay=args.ramp_up * i / max(1, n_agents)))
              for i, h in enumerate(hosts)]
    await asyncio.sleep(args.ramp_up + args.duration)
    stop.set()
    send_window = time.time() - started
    await asyncio.wait(agents, timeout=15)
    for t in agents:
        t.cancel()

    await asyncio.sleep(args.drain)          # let in-flight messages arrive
    done.set()
    await asyncio.gather(observer, probe, monitor, return_exceptions=True)

    persisted = count_persisted(args.database_url, run_id) if args.database_url else None
    delivered = len(stats.delivered_ids)
    gen_lag = summarize_ms(stats.generator_loop_lag_s)
    connected = {h.agent_id: h.scenario for h in hosts if h.agent_id in stats.connected_agents}
    result = {
        "run_id": run_id,
        "agents": n_agents,
        "interval_s": args.interval,
        "target_msgs_per_s": round(n_agents / args.interval, 1),
        "send_window_s": round(send_window, 1),
        "sent_unique": stats.sent_unique,
        "sent_frames": stats.sent_frames,
        "achieved_send_rate": round(stats.sent_unique / send_window, 1),
        "delivered_unique": delivered,
        "delivered_rate": round(delivered / send_window, 1),
        "delivery_ratio": round(delivered / stats.sent_unique, 4) if stats.sent_unique else None,
        "duplicates_sent": stats.duplicates_sent,
        "duplicates_delivered": stats.duplicates_delivered,
        "reordered": stats.reordered,
        "persisted_rows": persisted,
        "e2e_latency_ms": summarize_ms(stats.e2e_latency_s),
        "api_probe_ms": summarize_ms(stats.probe_latency_s),
        "api_probe_errors": stats.probe_errors,
        "send_call_ms": summarize_ms(stats.send_call_s),
        "sender_lag_ms": summarize_ms(stats.sender_lag_s),
        "generator_loop_lag_ms": gen_lag,
        "agents_connected": len(stats.connected_agents),
        "connects": stats.connects,
        "connect_failures": stats.connect_failures,
        "disconnects": stats.disconnects,
        "flaps": stats.flaps,
        "anomalies": dict(stats.anomalies),
        "detection_quality": detection_quality(connected, stats.anomalies_by_agent),
        "observer_connected": ready.is_set(),
        "observer_errors": stats.observer_errors,
        "pre_stage_cooldown_s": round(cooldown_s, 1),
        # A busy generator loop (>50 ms p99) means the harness, not the backend, may be the limit.
        "generator_saturated": bool(gen_lag["p99"] and gen_lag["p99"] > 50),
    }
    return result


def print_table(results: List[Dict[str, Any]]) -> None:
    cols = [("agents", "agents"), ("connected", "agents_connected"), ("target/s", "target_msgs_per_s"),
            ("sent/s", "achieved_send_rate"),
            ("delivered/s", "delivered_rate"), ("delivery", "delivery_ratio"), ("persisted", "persisted_rows")]
    lat = [("e2e p50", "e2e_latency_ms", "p50"), ("e2e p95", "e2e_latency_ms", "p95"),
           ("e2e p99", "e2e_latency_ms", "p99"), ("api p99", "api_probe_ms", "p99")]
    header = [c[0] for c in cols] + [c[0] + " ms" for c in lat] + ["gen ok"]
    rows = []
    for r in results:
        row = [str(r[k]) for _, k in cols] + [str(r[k][s]) for _, k, s in lat]
        row.append("no" if r["generator_saturated"] else "yes")
        rows.append(row)
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(header)]
    print("  ".join(h.rjust(w) for h, w in zip(header, widths)))
    for row in rows:
        print("  ".join(v.rjust(w) for v, w in zip(row, widths)))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.getenv("SIM_URL", "ws://localhost:8000"), help="backend base ws:// URL")
    p.add_argument("--agents", default="100", help="agent count, or comma list for a ramp (e.g. 50,100,250)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between messages per agent")
    p.add_argument("--duration", type=float, default=60, help="steady-state seconds per stage (after ramp-up)")
    p.add_argument("--ramp-up", type=float, default=5, help="seconds over which agents connect (avoids a herd)")
    p.add_argument("--drain", type=float, default=10, help="seconds to wait for in-flight messages after sending")
    p.add_argument("--cooldown", type=float, default=180,
                   help="max seconds to wait for the backend to go idle before each stage")
    p.add_argument("--mix", default=None, help="scenario weights, e.g. normal=0.8,memory_leak=0.2")
    p.add_argument("--seed", default="42", help="seed for reproducible fleets")
    p.add_argument("--database-url", default=os.getenv("SIM_DATABASE_URL"),
                   help="optional; if set, counts rows actually persisted per stage")
    p.add_argument("--report", help="write JSON results here")
    p.add_argument("--run-prefix", default=f"r{int(time.time()) % 1_000_000}s",
                   help="prefix making agent ids unique per invocation")
    return p.parse_args(argv)


async def main_async(args) -> List[Dict[str, Any]]:
    results = []
    for i, n in enumerate(int(x) for x in args.agents.split(",")):
        print(f"stage {i + 1}: {n} agents @ {1 / args.interval:g} msg/s each for {args.duration:g}s ...", flush=True)
        results.append(await run_stage(args, n, i))
        print_table(results[-1:])
    return results


def main(argv=None) -> List[Dict[str, Any]]:
    args = parse_args(argv)
    results = asyncio.run(main_async(args))
    print("\nsummary")
    print_table(results)
    if args.report:
        report = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "environment": {"python": platform.python_version(), "platform": platform.platform(),
                            "cpu_count": os.cpu_count()},
            "args": {k: v for k, v in vars(args).items() if k != "database_url"},
            "stages": results,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\nreport written to {args.report}")
    return results


if __name__ == "__main__":
    main()
