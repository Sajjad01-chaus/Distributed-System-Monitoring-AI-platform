# Architecture

## Data flow

```text
                 ┌─────────────── nginx (lb) ───────────────┐
 Agents ──WS────►│ least_conn · WebSocket upgrade · DNS      │──► API replica 1 ┐
 Dashboards ─WS─►│ re-resolve · idempotent-only retries      │──► API replica 2 ├─ each: validate → admission → XADD
 Operators ─HTTP►│                                           │──► API replica N ┘        presence · command listener
                 └───────────────────────────────────────────┘                            dashboard relay
                                                        │
                                                        ▼
                                          Redis ── Stream "telemetry" (AOF on)
                                            │      presence:{agent} → replica
                                            │      pub/sub: dashboard · replica:{id}:commands
                                            │      leader:liveness lease
                                  ┌─────────┴─────────────────────────┐
                        consumer group "persist"             consumer group "detect"          liveness × 2
                        N × worker-persist                   M × worker-detect                (one leader)
                        batch INSERT … ON CONFLICT           per-agent windows in Redis        offline/online sweeps
                        DO NOTHING RETURNING                 detectors + fleet IsolationForest
                                  │                                   │                              │
                                  └──────────────► PostgreSQL + TimescaleDB ◄────────────────────────┘
                                              system_metrics (hypertable, app-run retention)
                                              alerts (one active per agent+type) · agents
```

## Control plane (Phase 3)

**Commands reach an agent no matter which replica receives the request.** An agent's socket lives
on one replica; the request can land on any of them:

```text
 POST /api/v1/agents/X/remediate ──► replica B
   B: GET presence:X → "replica-A"                      (who holds X's socket?)
   B: PUBLISH replica:replica-A:commands {command_id…}  (0 receivers → 503: stale presence)
   A: writes the command to X's socket, RPUSH cmdack:{id}
   B: BLPOP cmdack:{id} (3 s) → 202 delivered           (timeout → 504 unacknowledged)
 X: remediation_result {command_id} ──► A ──► cmd:{id} = completed, and relayed to dashboards
 GET /api/v1/commands/{id} on any replica ──► pending / delivered / completed + result
```

Presence is claimed on connect, refreshed a few times per TTL (piggy-backing on traffic, no
per-message write), and released on disconnect only if it still points at this replica
(WATCH/MULTI), so a reconnect that moved the agent to another replica is never undone by the old
socket's cleanup.

**Liveness runs with leader election.** Two `liveness` replicas compete for a Redis lease
(`SET NX PX`, owner-checked renew/release). The holder sweeps: agents silent for `AGENT_STALE_S`
become `offline` with an `agent_offline` alert, their other alerts become `stale`, and their
detector state is cleared. On return, the alert resolves. Three details keep it correct:
- lease renewal runs in its own task and sweep DB work in a thread, so a long sweep can't let the
  lease lapse (it did, before this was fixed: two leaders for ~3 s during a 7,000-agent sweep);
- each sweep is bounded (500 agents), so no single pass runs long;
- every action is an idempotent UPDATE guarded by current state, so even a brief two-leader overlap
  only repeats work. That's why no fencing token is needed here.

It also refuses to declare agents dead when the persist backlog is high: stale `last_seen` values
then mean *we* are behind, not that the agents are silent.

**Replicas drain gracefully.** On SIGTERM a replica closes agent sockets with 1012 ("service
restart"); agents reconnect right away with exponential backoff and jitter, and the load balancer
sends them to a surviving replica.

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
| Raw telemetry grows without bound | TimescaleDB hypertable (1-day chunks); the liveness leader deletes samples older than `METRICS_RETENTION_HOURS` in batches, the same on any Postgres | `liveness.prune_old_metrics` |
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

- Agent telemetry has no application-level acknowledgement: frames in flight when a replica
  crashes are lost (measured in [phase3-results](benchmarks/phase3-results.md)). Idempotent storage already
  makes resending safe; an ack/resend protocol is the fix.

- No authentication yet (Phase 4). Pipeline metrics are exposed as JSON; Prometheus/Grafana come in Phase 5.
- The detection thresholds are calibrated for the simulator's time scale (10 s samples, faults
  developing over minutes). In a real deployment they are configuration, set per environment.
