# Deploying: Render (API) + Vercel (dashboard)

Free tiers run the platform in **single-service mode**: one Render web service runs the API
*and* the persist / detect / liveness loops in-process (`EMBEDDED_WORKERS`), plus an optional
30-agent synthetic demo fleet (`DEMO_AGENTS`), so the live dashboard always has data. It's the
same code and pipeline as the multi-container setup (agent → stream → consumer groups → DB).
Scaling out is a configuration change: run `python -m app.worker persist|detect|liveness` as
separate services and unset `EMBEDDED_WORKERS`.

## 1. Backend on Render (Blueprint)

1. Push this repo to GitHub.
2. Render dashboard → **New → Blueprint** → select the repo. `render.yaml` creates:
   - `dsm-api`: web service from `deploy/Dockerfile.render` (runs migrations, then uvicorn)
   - `dsm-db`: PostgreSQL (free)
   - `dsm-redis`: Key Value / Redis (free, `noeviction`)
3. When prompted (or later under *dsm-api → Environment*), set:
   - `ADMIN_PASSWORD`: password for the dashboard's admin actions
   - `VIEWER_PASSWORD`: optional
   - `CORS_ORIGINS`: your dashboard origin, e.g. `https://fleet-monitor.vercel.app`
     (set it after step 2 below; `*` works while testing)
4. Wait for the deploy, then check `https://<your-api>.onrender.com/ready` → `{"status":"ready"}`.

Free-tier notes:
- The free web service **sleeps after ~15 min without traffic** and takes about a minute to
  wake. The demo fleet runs inside the service, so it sleeps too, and data resumes on wake.
- Render Postgres has no TimescaleDB. The migration detects that and skips the hypertable, and
  `METRICS_RETENTION_HOURS=6` has the liveness leader prune old samples instead.
- Redis is 25 MB, so the blueprint caps the stream (`STREAM_MAXLEN=5000`) and admission
  (`PERSIST_MAX_LAG=2000`) accordingly.

## 2. Dashboard on Vercel

1. Vercel → **Add New → Project** → import the repo.
2. **Root Directory:** `dashboard` (framework auto-detected as Next.js).
3. **Environment variable:** `NEXT_PUBLIC_API_URL = https://<your-api>.onrender.com`
4. Deploy, then put the Vercel URL into the API's `CORS_ORIGINS` (step 1.3).

The dashboard is a static export (`output: "export"`), so any static host works:
`npm run build` produces `dashboard/out/`.

## 3. Connect a real agent (optional)

```bash
cd agent
SERVER_URL=wss://<your-api>.onrender.com AGENT_ID=my-laptop python agent.py
```

## Full stack locally (all features, including nginx, 3 replicas and Grafana)

```bash
cp .env.example .env        # fill in the passwords/secrets
docker compose up -d --build --scale backend=3
# API via the load balancer: http://localhost:8000 · Grafana: http://localhost:3001
cd dashboard && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev   # http://localhost:3000
```
