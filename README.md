# Distributed System Monitoring & AI Auto-Remediation Platform

Cross-platform monitoring agents stream OS-level telemetry (CPU, memory, disk, network, processes)
into a Redis Streams pipeline. Horizontally scalable workers persist it idempotently to
PostgreSQL/TimescaleDB and run per-agent anomaly detection (statistical detectors plus a
fleet-level Isolation Forest), with alerts that open and resolve on their own. Dashboards get
live updates over WebSockets, and operators can trigger allowlisted remediation actions on agents.

This repository is being evolved, phase by phase, from a single-process prototype into a horizontally
scalable, secured observability control plane — with every change measured against a synthetic
agent fleet rather than asserted. See [Roadmap](#roadmap).

## Architecture

```text
 Agents ─────WS──┐                              ┌─ group "persist" → N workers → TimescaleDB (idempotent)
 Dashboard ──WS──┼─► nginx ─► API replicas ─XADD─► Redis Stream ─┤
 Operators ─HTTP─┘  (LB)     (validate, admit,  │                └─ group "detect"  → M workers → per-agent detection → alerts
                              route commands)   │
                        ◄──── Redis pub/sub ◄───┘  presence · commands · events      liveness × 2 (leader-elected)
```

The API does nothing slow: it validates, applies admission control, and enqueues. Storage and
detection run in separate consumer groups, so detection can fall behind without delaying
storage. Delivery is at-least-once with idempotent writes (effectively once), crashed workers'
messages are re-claimed, and poison messages go to a dead-letter stream. Any API replica can serve
any request: agent presence and command routing live in Redis, and a leader-elected liveness
monitor marks silent agents offline. **Why each piece exists: [docs/architecture.md](docs/architecture.md).**

| Component | Path | Notes |
|---|---|---|
| Agent | `agent/` | psutil collectors; `(boot_id, seq)` idempotency key; backoff with jitter; honours throttling; allowlisted remediation, dry-run by default |
| Load balancer | `nginx/` | `least_conn` for long-lived WebSockets, DNS re-resolution (scale without reload), idempotent-only retries |
| API | `backend/app/main.py` | Ingest → stream, admission control, presence + command routing, dashboard relay, JWT (`auth.py`), `/health` + `/ready` |
| Workers | `backend/app/pipeline/`, `python -m app.worker persist\|detect\|liveness` | Reliable consumers: batching, XAUTOCLAIM recovery, DLQ, transient-error backoff; liveness with a Redis lease |
| Detection | `backend/app/detection/` | Sustained thresholds, leak trend, disk-full forecast, latency degradation, fleet outliers; alert lifecycle with hysteresis |
| Schema | `backend/migrations/` | Alembic; Timescale hypertable with 7-day retention; read-only Grafana role |
| Dashboard | `dashboard/` | Next.js (static export): live fleet stats, pipeline health, event feed, admin actions |
| Grafana | `grafana/` | Provisioned as code on TimescaleDB: ingest rate, CPU/memory percentiles, alerts |
| Simulator | `simulator/` | Synthetic fleet with fault injection, ground-truth scoring, command/failover measurement |

## Quickstart

```bash
cp .env.example .env            # fill in the passwords and secrets
docker compose up -d --build --scale backend=3
#   API (through nginx): http://localhost:8000/docs · Grafana: http://localhost:3001
cd dashboard && npm install && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev   # http://localhost:3000
```

Scale any tier independently: `docker compose up -d --scale backend=3 --scale worker-persist=3`.
Admin actions (remediate, resolve) need a token: `POST /api/v1/auth/token` with `ADMIN_USERNAME` /
`ADMIN_PASSWORD` from `.env`, or sign in on the dashboard. **Deploying to Render + Vercel: [docs/deploy.md](docs/deploy.md).**

Run an agent against it:

```bash
cd agent
pip install -r requirements.txt
cp config.example.yaml config.yaml
python agent.py                 # or: AGENT_ID=my-host SERVER_URL=ws://localhost:8000 python agent.py
```

## Development

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
ruff check .
pytest -q          # unit tests use SQLite + fakeredis; the multi-process e2e tests also
                   # need a Redis at $REDIS_URL (e.g. `docker compose up -d redis`) and skip without one
```

CI (`.github/workflows/ci.yml`) runs lint and tests (with a Redis service), then brings up the whole
compose stack on real TimescaleDB and runs a synthetic fleet against it, on every push and PR.

## Load testing with a synthetic fleet

`simulator/` runs hundreds of simulated agents that speak the real agent protocol, with
per-host failure scenarios (CPU spikes, memory leaks, disk fill, network degradation,
flapping connections, duplicate and out-of-order sends). It measures end-to-end latency,
delivery, persisted rows and API responsiveness while the backend is under load.

```bash
cd simulator
pip install -r requirements.txt
python fleet.py --agents 25,50,100,200 --interval 10 --duration 60 \
    --database-url postgresql+psycopg2://monitor_user:<password>@localhost:5432/system_monitor \
    --report ../docs/benchmarks/my-run.json
```

Or run it inside the compose network: `docker compose --profile loadtest run --rm fleet --agents 25,50,100`.
Methodology: [`docs/benchmarks/methodology.md`](docs/benchmarks/methodology.md).

**Phase 1 baseline** ([full results](docs/benchmarks/phase1-baseline.md)): the single-process backend
tops out at **~8 msg/s** (~240 hosts at a 30 s interval). The AI engine refits a model on every message
on the event loop, so `/health` p99 reaches 2–5 s under load, most of a 400-agent fleet can't even
connect, duplicates are stored, and only 1 of 81 leaking hosts is detected.

**Phase 2** ([full results](docs/benchmarks/phase2-results.md)), same laptop, workload and seed:

| At 400 agents | Phase 1 | Phase 2 |
|---|---|---|
| Agents connected | 163 / 400 | **400 / 400** |
| E2E latency p50 / p99 | 817 / 3,115 ms | **10 / 27 ms** |
| `/health` p99 | 5,088 ms | **5 ms** |
| Healthy hosts falsely flagged | 22% | **0%** |
| Duplicates stored | all | **none** |

It sustains **770 msg/s** with every message delivered (~100× the old ceiling, which still wasn't
reached on one laptop), and three workers per group cut p50 latency at that rate from 53 to 17 ms.
Killing a worker that holds unacked messages loses nothing: the survivor re-claims them
([`scripts/chaos-kill-persist-worker.sh`](scripts/chaos-kill-persist-worker.sh)). Detection catches
92–100% of injected leaks, disk fills, network degradations and CPU spikes, up from 1% for leaks.

**Phase 3** ([full results](docs/benchmarks/phase3-results.md)): 300 agents across 3 API replicas behind nginx.
Commands issued on any replica reach the agent wherever it's connected: **738/738 completed**, with a
p50 of 8.7 ms to delivery and 9.3 ms to the agent's result. Losing one of three replicas mid-load:
a graceful stop moves its 100 agents in **≤ 67 ms with zero loss**; a hard kill moves them in ≤ 403 ms
and loses **5 of 37,634** in-flight messages (0.013%, predicted before the run: telemetry has no
app-level ack yet).

## Known limitations

- **Telemetry has no application-level ack**, so frames in flight when an API replica crashes are lost
  (0.013% in the kill test). Storage is idempotent, so an ack/resend protocol is the straightforward fix.
- **Auth covers the control plane only.** Commands and alert resolution need an admin JWT; telemetry
  reads, the dashboard stream and agent connections are open (demo choice). Per-agent credentials and
  audit logging are next.
- **Pipeline internals** (stream lag, DLQ) are exposed as JSON (`/api/v1/system/pipeline`) and on the
  dashboard; Grafana charts the database. A Prometheus exporter for the pipeline would complete the picture.
- **Detection thresholds are calibrated for the simulator's time scale** (10 s samples, faults developing
  over minutes); in a real deployment they're per-environment configuration.

## Roadmap

| Phase | Focus | Outcome |
|---|---|---|
| 0 ✅ | Repo cleanup, working agent, config via env, tests + CI | Reproducible baseline |
| 1 ✅ | Synthetic agent fleet (failure scenarios) + load harness | [Baseline](docs/benchmarks/phase1-baseline.md): ~8 msg/s ceiling |
| 2 ✅ | Redis Streams pipeline, consumer-group workers, idempotency, DLQ, Timescale, Alembic; per-agent detection with alert lifecycle | [Results](docs/benchmarks/phase2-results.md): ≥770 msg/s, 0% false positives |
| 3 ✅ | nginx LB, API replicas, presence + cross-replica command routing, leader-elected liveness | [Results](docs/benchmarks/phase3-results.md): failover ≤ 67 ms, zero loss when graceful |
| 3+ ✅ | JWT roles, Grafana on TimescaleDB, Next.js dashboard, Render/Vercel deploy | [docs/deploy.md](docs/deploy.md) |
| next | Telemetry ack/resend, per-agent credentials + audit log, Prometheus exporter, rate limiting | |
