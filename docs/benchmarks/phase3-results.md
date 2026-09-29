# Phase 3 results: load balancer, replicas, command routing, liveness

Setup: the full compose stack on one laptop VM, with **3 API replicas** behind nginx, 1 persist + 1
detect worker, and 2 liveness monitors. The fleet connects **through the load balancer**, and 5
remediation commands per second are issued through it too, each landing on an arbitrary replica.
Reports: `phase3-*.json`. Design: [../architecture.md](../architecture.md#control-plane-phase-3).

## Steady state: 300 agents, commands across replicas

[phase3-steady.json](phase3-steady.json): 300 agents × 1 msg/s for 150 s, default fault mix, authenticated commands.

| Metric | Result |
|---|---|
| Telemetry sent / persisted | 46,528 / **46,528** |
| E2E latency p50 | 24 ms |
| `/health` p99 through the LB | 42 ms |
| Commands issued | 740: **738 × 202**, 2 × 404 (target agent was mid-reconnect: "flapping" scenario) |
| Commands completed (agent result seen on a dashboard) | **738 / 738** |
| Command ack (POST → routed → delivered → 202) p50 / p99 | **8.7 ms** / 72 ms |
| Command completion (POST → agent's result) p50 / p99 | **9.3 ms** / 138 ms |

The E2E p99 (1.3 s) is dominated by the out-of-order scenario, which deliberately holds messages
for one interval; see [methodology](methodology.md).

## Failover: lose one of three replicas mid-load

300 agents × 1 msg/s, healthy hosts only, 5 commands/s. At t = 55 s one replica is removed, and
40 s later it is started again. Reports: [graceful](phase3-failover-graceful.json) · [kill](phase3-failover-kill.json).

| | `docker stop` (graceful: SIGTERM) | `docker kill` (crash) |
|---|---|---|
| Agents that had to move | 100 | 100 |
| Reconnect time p50 / max | **12 ms / 67 ms** | **138 ms / 403 ms** |
| Telemetry lost | **0** of 37,645 | **5 of 37,634 (0.013%)** |
| Commands | 608 × 202, 1 × 503 | 608 × 202 |
| `/health` errors through the LB | 0 | 0 |

- **Graceful:** the draining replica closes its agents' sockets with 1012 ("service restart"). The
  agents reconnect right away and nginx sends them to the surviving replicas. Nothing is lost. The
  single 503 was a command routed to the draining replica in the instant its presence still
  pointed there; the API answered "undeliverable" instead of hanging, and the caller can retry.
- **Crash:** the agents notice when nginx drops their upstream (p50 138 ms), then reconnect.
  **5 messages were lost**: frames already written into the dead replica's socket. That was
  predicted before the run: telemetry has no application-level ack, so in-flight frames die with
  the process. The fix is known and cheap because storage is already idempotent: the server
  acks per `seq`, and the agent re-sends anything unacked after a reconnect. It's listed as
  future work in [architecture.md](../architecture.md#known-limits-next-phases).

## Liveness and leader election

- **One leader, even under a heavy sweep.** The first sweep after deploy had to mark 7,360 stale
  agents offline. An earlier version ran that synchronously and let the 15 s lease lapse, which
  produced **two leaders for ~3 s**. With renewal in its own task, DB work in a thread, and
  sweeps bounded to 500 agents, the same load ran as 15 passes of < 1 s each, with one leader
  throughout and no leadership change.
- **Silent agents are detected in 93 s (median), 96 s (max)**, measured over the 600 agents from
  the two steady runs that went silent when their runs ended: `AGENT_STALE_S` (90 s) plus at most
  one sweep interval (10 s). Their open alerts become `stale`, `agent_offline` opens, and both resolve
  automatically when data resumes. All of this is covered by tests in `backend/tests/test_control.py`.
