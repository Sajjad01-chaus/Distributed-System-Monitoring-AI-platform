# Load-test methodology

All numbers in this folder come from `simulator/fleet.py` runs against a real backend
process. Nothing here is estimated; every table links to the JSON report it came from.

## What the harness does

```text
 fleet.py
 ├── N simulated agents ──WS /ws/agent/{id}──►┐
 │     (real payload shape, per-host scenario) │
 ├── dashboard observer ◄──WS /ws/dashboard────┤  backend under test
 │     timestamps every metrics_update          │  (DB write → AI engine → broadcast)
 ├── HTTP probe: GET /health once a second ────►┘
 └── optional: SELECT count(*) of persisted rows per stage
```

- **Payloads** match the real agent's shape and include every field the AI engine reads, so
  the expensive detection path runs exactly as it would with real hosts.
- **Scenarios** (default mix): 70% normal, plus CPU spikes, memory leaks, disk fill, network
  degradation, connection flapping, duplicate sends and out-of-order sends.
- **Reproducible:** fleets are seeded (`--seed`); the same seed gives the same hosts, scenarios
  and telemetry.
- **Stages:** `--agents 25,50,100` runs a ramp. Before each stage the harness waits until
  `/health` is fast again, so one stage's backlog doesn't leak into the next.

## Metrics

| Metric | Meaning |
|---|---|
| `achieved_send_rate` | unique messages/s the fleet actually sent |
| `delivered_rate`, `delivery_ratio` | unique messages that reached a dashboard within the drain window |
| `persisted_rows` | rows in `system_metrics` for this stage's agents (with `--database-url`) |
| `e2e_latency_ms` | agent send → dashboard receive: WS ingest + DB write + AI + broadcast |
| `api_probe_ms` | latency of `GET /health`, which does no I/O, so this is event-loop delay |
| `send_call_ms` | time blocked in `ws.send`; grows when the server stops reading (TCP backpressure) |
| `generator_loop_lag_ms` / `generator_saturated` | the load generator's own event-loop delay; p99 > 50 ms means the harness may be the bottleneck |
| `clock_skew_s` / `stall_detected` | wall clock vs monotonic clock over the stage, plus max loop lag; a freeze beyond normal VM clock drift (host sleep, VM pause) flags the stage as invalid |
| `detection_quality` | the AI engine scored against injected faults: per-scenario recall and the false-positive rate on healthy hosts |

## Caveats

- Generator and backend share one machine unless stated, so they compete for CPU.
- Stages flagged `stall_detected` are discarded, not published. (A development run was frozen
  for 435 s mid-stage; this check exists so that can never silently skew a number.)
- Docker Desktop's VM clock drifts ~5% against the monotonic clock, so wall-clock-based rates can
  read up to ~5% low. Stall detection tolerates up to max(15 s, 10% of the stage window).
- "Lost" means *not delivered within the drain window*. For an overloaded backend some of
  those messages are still queued and arrive later; `persisted_rows` at the end of the drain
  makes the gap visible.
