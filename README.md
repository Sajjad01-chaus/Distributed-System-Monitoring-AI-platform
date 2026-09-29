# Distributed System Monitoring & AI Auto-Remediation Platform

Cross-platform monitoring agents stream OS-level telemetry (CPU, memory, disk, network, processes)
to a FastAPI backend that stores it in PostgreSQL/TimescaleDB, runs ML anomaly detection
(Isolation Forest + threshold and trend rules), and pushes live updates to dashboards over WebSockets.
Operators can trigger allowlisted remediation actions on agents.

This repository is being evolved, phase by phase, from a single-process prototype into a horizontally
scalable, secured observability control plane — with every change measured against a synthetic
agent fleet rather than asserted. See [Roadmap](#roadmap).

## Current architecture (Phase 0)

```text
 Agent (psutil collectors) ──WebSocket──►  FastAPI (single process)
                                             ├─ writes raw telemetry → PostgreSQL/TimescaleDB
                                             ├─ AIEngine (in-process, IsolationForest)
                                             └─ broadcasts → dashboard WebSockets
 Streamlit dashboard ──REST──► /api/v1/{agents,metrics,alerts}
```

| Component | Path | Notes |
|---|---|---|
| Agent | `agent/` | Collectors for system, network, filesystem, processes; allowlisted remediation, dry-run by default |
| Backend API | `backend/app/` | FastAPI, SQLAlchemy models, WebSocket ingestion, AI engine |
| Dashboard | `frontend/` | Streamlit (to be replaced by a React/Next.js app) |

## Quickstart

```bash
cp .env.example .env            # then set POSTGRES_PASSWORD
docker compose up --build       # API on http://localhost:8000, docs at /docs
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
pytest -q          # backend tests run against SQLite; no services needed
```

CI (`.github/workflows/ci.yml`) runs lint, tests and a backend image build on every push and PR.

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

## Known limitations

These are deliberate starting points for the roadmap — each will be fixed and measured, not hidden:

- **Ingestion is synchronous and single-process.** Each telemetry message does a blocking DB write and
  an ML pass (including an Isolation Forest re-fit, ~133 ms) inside the WebSocket receive loop.
- **AI engine state is global.** One shared history window mixes data from all agents.
- **WebSocket connections live in process memory**, so the API cannot run as more than one replica.
- **No authentication** on REST/WebSocket endpoints; CORS allows `*`.
- **Alerts are not persisted** — the alert manager is in-memory and nothing writes the `alerts` table.
- **Schema is created with `create_all()`**, not migrations; Redis is provisioned but unused.

## Roadmap

| Phase | Focus | Outcome |
|---|---|---|
| 0 ✅ | Repo cleanup, working agent, config via env, tests + CI | Reproducible baseline |
| 1 ✅ | Synthetic agent fleet (failure scenarios) + load harness | [Baseline](docs/benchmarks/phase1-baseline.md): ~8 msg/s ceiling |
| 2 | Ingestion pipeline: Redis Streams, consumer-group workers, idempotency, DLQ, async DB, Timescale hypertables, Alembic | Backpressure & delivery guarantees |
| 3 | Scale-out: Nginx LB, N API replicas, Redis pub/sub WebSocket fan-out, heartbeat liveness | Horizontal scaling |
| 4 | Security: JWT + RBAC, per-agent credentials, signed expiring commands, rate limiting, audit log | Secure control plane |
| 5 | Self-observability: Prometheus, Grafana, OpenTelemetry, liveness/readiness | Operability |
| 6 | Chaos tests (kill workers/replicas/Redis under load) + published results; React dashboard | Evidence |
