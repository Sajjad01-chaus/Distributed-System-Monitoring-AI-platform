# Architecture

## Data flow

```text
                        ┌──────────────────────────── API process(es) ─────────────────────────────┐
 Agent ──WS /ws/agent──►│ validate (size, JSON, identity) → admission check → XADD telemetry        │
                        │ /ws/dashboard ◄── DashboardHub ◄── SUBSCRIBE dashboard                    │
                        │ /health (liveness) · /ready (Redis+DB) · /api/v1/system/pipeline           │
                        └───────────────────────────────┬──────────────────────────────────────────┘
                                                        ▼
                                          Redis Stream "telemetry" (AOF on)
                                  ┌─────────────────────┴─────────────────────┐
                        consumer group "persist"                    consumer group "detect"
                        N × worker-persist                          M × worker-detect
                        batch INSERT … ON CONFLICT DO NOTHING       per-agent windows + alert state in Redis
                        RETURNING → publish metrics_update          detectors + fleet IsolationForest
                        upsert agent liveness                        open/resolve alerts → publish events
                                  │                                           │
                                  └──────────► PostgreSQL + TimescaleDB ◄──────┘
                                              system_metrics (hypertable, 7-day retention)
                                              alerts (one active per agent+type) · agents
```

## Why each piece exists

| Problem (measured in [Phase 1](benchmarks/phase1-baseline.md)) | Design | Where |
|---|---|---|
| AI ran on the request path; `/health` p99 reached 2–5 s | The API only validates and enqueues; persistence and detection run in worker processes | `app/main.py`, `app/worker.py` |
| Slow detection would also block storage | Two consumer groups read the same stream independently; detection can lag without delaying persistence | `pipeline/persist.py`, `pipeline/detect.py` |
| Overload showed up as silent WebSocket lockout | Admission control on the *persist* backlog: agents get an explicit `throttle` reply and back off; the rejects are counted | `pipeline/hub.py::Admission` |
| A crash between reading and writing would lose data | At-least-once delivery: XACK only after the DB commit; entries left pending by a dead worker are re-claimed with XAUTOCLAIM | `pipeline/consumer.py` |
| At-least-once creates duplicates, and agents re-send | Idempotency key `(agent_id, boot_id, seq)` is the primary key; `INSERT … ON CONFLICT DO NOTHING RETURNING` stores it once and only announces new rows | `models/system_metrics.py` |
| One bad message could block a partition forever | Unparseable entries, and entries that fail more than `MAX_DELIVERIES` times, go to a dead-letter stream; a failing batch is retried entry by entry | `pipeline/consumer.py` |
| A DB or Redis outage must not turn good data into "poison" | Transient errors leave the batch pending and back off exponentially; only data errors are dead-lettered | `consumer.TRANSIENT_ERRORS` |
| Dashboards were tied to the process an agent happened to hit | Workers publish to Redis pub/sub; every API replica relays to its own dashboards, and slow dashboards are dropped after 1 s | `pipeline/hub.py::DashboardHub` |
| Per-message model refit (~133 ms) and one global history | Per-agent statistical detectors (<1 ms); a fleet IsolationForest refit every 5 min, batch-scored | `detection/` |
| Detector state in a worker would pin agents to workers | Windows and alert state live in Redis, so workers are stateless and any worker can take any agent | `pipeline/detect.py` |
| Alerts piled up and flapped | Alerts open on transitions and resolve after N clear evaluations (hysteresis); one active alert per (agent, type) enforced by a partial unique index | `models/alerts.py` |
| Raw telemetry grows without bound | TimescaleDB hypertable (1-day chunks) with a 7-day retention policy | `migrations/versions/0001_*` |
| `create_all()` at startup can't evolve a schema | Alembic migrations run as a one-shot `migrate` service; legacy tables are renamed, never dropped | `docker-compose.yml`, `migrations/` |
| Workers deadlocking on concurrent agent upserts | Rows are upserted in sorted `agent_id` order, so locks are always taken in the same order | `persist._touch_agents` |

## Delivery semantics

- **Persistence: effectively once.** At-least-once delivery combined with an idempotent insert.
  A message is either stored exactly once or, after `MAX_DELIVERIES` failures, dead-lettered with its reason.
- **Detection: at-least-once, best effort under overload.** Replays are skipped with a
  per-message dedup key. If the detect group falls far behind, stream trimming can drop
  detection work, but never unpersisted data: admission control rejects new telemetry long
  before the stream cap is reached.
- **Dashboards: at-most-once.** Pub/sub is fire-and-forget. A dashboard that reconnects
  re-reads current state over REST.

## Known limits (next phases)

- Commands to agents (`/remediate`, `/restart`) only reach agents connected to the same API
  replica. Cross-replica command routing is Phase 3.
- Alerts on an agent that goes silent stay active, because nothing evaluates that agent anymore.
  Heartbeat-based liveness (mark agents offline and handle their alerts) is Phase 3.
  `/api/v1/system/status` already excludes silent agents from its health counts.
- No authentication yet (Phase 4). Pipeline metrics are exposed as JSON; Prometheus/Grafana come in Phase 5.
- The detection thresholds are calibrated for the simulator's time scale (10 s samples, faults
  developing over minutes). In a real deployment they are configuration, set per environment.
