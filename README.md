# Distributed System Monitoring & AI Auto-Remediation Platform

Cross-platform monitoring agents stream OS-level telemetry (CPU, memory, disk, network, processes)
into a Redis Streams pipeline. Horizontally scalable workers persist it idempotently to
PostgreSQL/TimescaleDB and run per-agent anomaly detection (statistical detectors plus a
fleet-level Isolation Forest), with alerts that open and resolve on their own. Dashboards get
live updates over WebSockets, and operators can trigger allowlisted remediation actions on agents.

This repository is being evolved, phase by phase, from a single-process prototype into a horizontally
scalable, secured observability control plane — with every change measured against a synthetic
agent fleet rather than asserted. See [Roadmap](#roadmap).

## Architecture (Phase 2)

```text
 Agent ──WS──► API ──XADD──► Redis Stream ─┬─ group "persist" → N workers → TimescaleDB (idempotent)
                ▲                          └─ group "detect"  → M workers → per-agent detection → alerts
 Dashboard ◄─WS─┘◄──────── Redis pub/sub ◄── workers publish metrics_update / anomaly_detected / alert_resolved
```

The API does nothing slow: it validates, applies admission control, and enqueues. Storage and
detection run in separate consumer groups, so detection can fall behind without delaying
storage. Delivery is at-least-once with idempotent writes (effectively once), crashed workers'
messages are re-claimed, poison messages go to a dead-letter stream, and detector state lives
in Redis so workers stay stateless. **Why each piece exists: [docs/architecture.md](docs/architecture.md).**

| Component | Path | Notes |
|---|---|---|
| Agent | `agent/` | psutil collectors; `(boot_id, seq)` idempotency key; honours server throttling; allowlisted remediation, dry-run by default |
| API | `backend/app/main.py` | WebSocket ingest → stream, admission control, dashboard relay, `/health` + `/ready`, pipeline status |
| Workers | `backend/app/pipeline/`, `python -m app.worker persist\|detect` | Reliable consumers: batching, XAUTOCLAIM recovery, DLQ, transient-error backoff |
| Detection | `backend/app/detection/` | Sustained thresholds, leak trend, disk-full forecast, latency degradation, fleet outliers; alert lifecycle with hysteresis |
| Schema | `backend/migrations/` | Alembic; Timescale hypertable with 7-day retention |
| Simulator | `simulator/` | Synthetic fleet with fault injection and ground-truth scoring |
| Dashboard | `frontend/` | Streamlit (to be replaced by a React/Next.js app) |

## Quickstart

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD
docker compose up --build       # API on http://localhost:8000, docs at /docs
docker compose up -d --scale worker-persist=3 --scale worker-detect=2   # scale workers independently
```

Run an agent against it:

```bash
cd agent
pip install -r requirements.txt
cp config.example.yaml config.yaml
python agent.py                 # or: AGENT_ID=my-host SERVER_URL=ws://localhost:8000 python agent.py
```

Dashboard (optional): `cd frontend && pip install -r requirements.txt && streamlit run app.py`

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

## Known limitations

These are deliberate starting points for the roadmap — each will be fixed and measured, not hidden:

- **Commands to agents are replica-local.** `/remediate` and `/restart` only reach agents connected to
  the API replica that receives the request (Phase 3: cross-replica routing behind a load balancer).
- **No authentication** on REST/WebSocket endpoints; CORS allows `*` (Phase 4).
- **Pipeline metrics are JSON only** (`/api/v1/system/pipeline`); Prometheus/Grafana come in Phase 5.
- **Detection thresholds are calibrated for the simulator's time scale** (10 s samples, faults developing
  over minutes); in a real deployment they're per-environment configuration.

## Roadmap

| Phase | Focus | Outcome |
|---|---|---|
| 0 ✅ | Repo cleanup, working agent, config via env, tests + CI | Reproducible baseline |
| 1 ✅ | Synthetic agent fleet (failure scenarios) + load harness | [Baseline](docs/benchmarks/phase1-baseline.md): ~8 msg/s ceiling |
| 2 ✅ | Redis Streams pipeline, consumer-group workers, idempotency, DLQ, Timescale, Alembic; per-agent detection with alert lifecycle | [Results](docs/benchmarks/phase2-results.md): ≥770 msg/s, 0% false positives |
| 3 | Scale-out: Nginx LB, N API replicas, Redis pub/sub WebSocket fan-out, heartbeat liveness | Horizontal scaling |
| 4 | Security: JWT + RBAC, per-agent credentials, signed expiring commands, rate limiting, audit log | Secure control plane |
| 5 | Self-observability: Prometheus, Grafana, OpenTelemetry, liveness/readiness | Operability |
| 6 | Chaos tests (kill workers/replicas/Redis under load) + published results; React dashboard | Evidence |
