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
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from scenarios import SyntheticHost, assign_scenarios, detection_quality, parse_mix
from stats import StageStats, summarize_ms

COMMAND_GRACE_S = 2.0
CONNECT_ERRORS = (OSError, asyncio.TimeoutError, WebSocketException)


# --- simulated agent ----------------------------------------------------------------
async def _drain(ws, stats: StageStats, backoff: Dict[str, float]) -> None:
    """Consume server->agent messages so the receive buffer never fills; honour throttles
    the way the real agent does (pause sending for retry_after_s)."""
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if msg.get("command_id"):
                # Behave like the real agent: execute (here: pretend to) and report back.
                stats.commands_received += 1
                await ws.send(json.dumps({"type": "remediation_result", "command_id": msg["command_id"],
                                          "issue_type": msg.get("issue_type"), "success": True,
                                          "dry_run": True, "simulated": True}))
            elif msg.get("type") == "throttle":
                stats.throttled += 1
                backoff["until"] = time.monotonic() + float(msg.get("retry_after_s", 5))
            elif msg.get("type") == "error":
                stats.server_errors += 1
    except ConnectionClosed:
        pass


async def run_agent(host: SyntheticHost, args, run_id: str, stats: StageStats,
                    stop: asyncio.Event, start_delay: float) -> None:
    rng = host.rng
    await asyncio.sleep(start_delay)
    uri = f"{args.url}/ws/agent/{host.agent_id}"
    backoff = 0.5
    held: Optional[str] = None  # out_of_order: a frame deliberately sent late
    lost_at: Optional[float] = None  # when the last connection dropped (for reconnect gaps)

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
        if lost_at is not None:
            stats.reconnect_gap_s.append(time.monotonic() - lost_at)
            lost_at = None
        backoff = 0.5
        throttle = {"until": 0.0}
        drainer = asyncio.create_task(_drain(ws, stats, throttle))
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
                if time.monotonic() < throttle["until"]:
                    continue   # server said back off: skip this sample, like the real agent

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
            lost_at = time.monotonic()
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
                    elif kind == "remediation_result" and msg.get("command_id") in stats.pending_commands:
                        issued = stats.pending_commands.pop(msg["command_id"])
                        stats.command_complete_s.append(time.monotonic() - issued)
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


def _http_base(args) -> str:
    return args.url.replace("ws://", "http://").replace("wss://", "https://")


async def pipeline_snapshot(args) -> Optional[Dict[str, Any]]:
    """Backlog/DLQ/admission state from backends that expose it (Phase 2+), else None."""
    def fetch():
        with urllib.request.urlopen(_http_base(args) + "/api/v1/system/pipeline", timeout=10) as r:
            return json.loads(r.read())
    try:
        return await asyncio.to_thread(fetch)
    except Exception:
        return None


async def wait_until_idle(args) -> float:
    """Block until /health answers fast 3 times running and (on pipeline backends) the persist
    backlog is empty, so a previous stage's backlog doesn't leak into the next stage's
    numbers. The detect group may still be behind; that is reported, not waited on."""
    url = _http_base(args) + "/health"
    start, fast = time.time(), 0
    while fast < 3 and time.time() - start < args.cooldown:
        t0 = time.perf_counter()
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=10).read())
            snap = await pipeline_snapshot(args)
            persist_lag = ((snap or {}).get("groups", {}).get("persist") or {}).get("lag") or 0
            fast = fast + 1 if time.perf_counter() - t0 < 0.1 and persist_lag == 0 else 0
        except Exception:
            fast = 0
        await asyncio.sleep(1)
    return time.time() - start


async def run_commander(args, hosts, stats: StageStats, stop: asyncio.Event, rng: random.Random) -> None:
    """Issue remediation commands through the API (i.e. the load balancer) at a fixed rate to
    random connected agents. Each lands on an arbitrary replica and must be routed to the one
    holding the agent's socket."""
    if not args.commands_per_s:
        return
    base = (args.api_url or _http_base(args)).rstrip("/")

    def post(agent_id: str):
        req = urllib.request.Request(f"{base}/api/v1/agents/{agent_id}/remediate?issue_type=cpu_threshold_breach",
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:
            return 0, None

    while not stop.is_set():
        connected = [h.agent_id for h in hosts if h.agent_id in stats.connected_agents]
        if connected:
            agent_id = rng.choice(connected)
            t0 = time.monotonic()
            code, body = await asyncio.to_thread(post, agent_id)
            stats.commands_issued += 1
            stats.command_status[str(code)] += 1
            if code == 202 and body:
                stats.command_ack_s.append(time.monotonic() - t0)
                stats.pending_commands[body["command_id"]] = t0
        await asyncio.sleep(1 / args.commands_per_s)


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
    # Random suffix: reusing a --run-prefix must never merge two runs' rows in the persisted count.
    run_id = f"{args.run_prefix}{secrets.token_hex(3)}s{stage_idx}n{n_agents}"
    master = random.Random(f"{args.seed}:{n_agents}")
    scenarios = assign_scenarios(n_agents, parse_mix(args.mix), master)
    hosts = [SyntheticHost(f"sim-{run_id}-{i:05d}", scenarios[i], random.Random(f"{args.seed}:{n_agents}:{i}"))
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

    # Commands stop a little before the agents do, so no command is measured against an agent
    # that is shutting down (its result would be cut off with the socket).
    cmd_stop = asyncio.Event()
    commander = asyncio.create_task(run_commander(args, hosts, stats, cmd_stop, random.Random(f"{args.seed}:cmd")))
    started = time.time()
    started_mono = time.monotonic()
    agents = [asyncio.create_task(run_agent(h, args, run_id, stats, stop,
                                            start_delay=args.ramp_up * i / max(1, n_agents)))
              for i, h in enumerate(hosts)]
    await asyncio.sleep(max(0.0, args.ramp_up + args.duration - COMMAND_GRACE_S))
    cmd_stop.set()
    await asyncio.sleep(min(COMMAND_GRACE_S, args.ramp_up + args.duration))
    stop.set()
    send_window = time.time() - started
    # Wall clock jumping ahead of the monotonic clock means the machine/VM was suspended mid-stage.
    clock_skew = send_window - (time.monotonic() - started_mono)
    await asyncio.wait(agents + [commander], timeout=15)
    for t in agents:
        t.cancel()

    await asyncio.sleep(args.drain)          # let in-flight messages arrive
    done.set()
    await asyncio.gather(observer, probe, monitor, return_exceptions=True)

    persisted = count_persisted(args.database_url, run_id) if args.database_url else None
    pipeline = await pipeline_snapshot(args)
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
        "throttled": stats.throttled,
        "reconnect_gap_ms": summarize_ms(stats.reconnect_gap_s),
        "commands": {"issued": stats.commands_issued, "by_http_status": dict(stats.command_status),
                     "received_by_agents": stats.commands_received,
                     "completed": len(stats.command_complete_s),
                     "ack_ms": summarize_ms(stats.command_ack_s),
                     "completion_ms": summarize_ms(stats.command_complete_s)},
        "server_errors": stats.server_errors,
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
        "pipeline_after_drain": pipeline,
        # A busy generator loop (>50 ms p99) means the harness, not the backend, may be the limit.
        "generator_saturated": bool(gen_lag["p99"] and gen_lag["p99"] > 50),
        # A freeze (host sleep, VM pause, stopped process) invalidates the stage's numbers. Small
        # proportional skew is normal VM clock drift (~5% observed under Docker Desktop), so the
        # wall/monotonic check only trips on gaps well beyond that.
        "clock_skew_s": round(clock_skew, 1),
        "stall_detected": bool(abs(clock_skew) > max(15.0, 0.1 * send_window)
                               or (gen_lag["max"] or 0) > 5000),
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
        row.append("STALL" if r["stall_detected"] else "no" if r["generator_saturated"] else "yes")
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
    p.add_argument("--api-url", default=None,
                   help="HTTP base for issued commands (default: derived from --url); lets a test "
                        "issue commands on a different replica than the agents are connected to")
    p.add_argument("--commands-per-s", type=float, default=0,
                   help="issue remediation commands through the API at this rate during each stage")
    p.add_argument("--mix", default=None, help="scenario weights, e.g. normal=0.8,memory_leak=0.2")
    p.add_argument("--seed", default="42", help="seed for reproducible fleets")
    p.add_argument("--database-url", default=os.getenv("SIM_DATABASE_URL"),
                   help="optional; if set, counts rows actually persisted per stage")
    p.add_argument("--report", help="write JSON results here")
    p.add_argument("--run-prefix", default="r", help="label prefixed to generated run ids")
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
