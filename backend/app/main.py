"""API process: accepts agent telemetry onto the stream, serves REST, relays dashboard events.

Nothing slow happens on the request path: persistence and anomaly detection run in separate
worker processes (app.worker) that consume the Redis stream. See docs/architecture.md.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import RedisError
from sqlalchemy import text

from app import config
from app.api import agents, alerts, metrics
from app.api.utils.logger import setup_logger
from app.database import SessionLocal
from app.pipeline import streams
from app.pipeline.hub import Admission, DashboardHub
from app.pipeline.streams import AGENT_ID_RE, InvalidTelemetry

logger = setup_logger()

# Close codes (RFC 6455 / IANA registry)
WS_POLICY_VIOLATION = 1008
WS_TRY_AGAIN_LATER = 1013


class AgentConnections:
    """Agent sockets held by *this* process, used to push commands. Cross-replica command
    routing is Phase 3; until then a command only reaches agents connected to this replica."""

    def __init__(self):
        self.sockets: Dict[str, WebSocket] = {}

    async def send(self, agent_id: str, message: dict) -> bool:
        ws = self.sockets.get(agent_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(message))
            return True
        except Exception:
            self.sockets.pop(agent_id, None)
            return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting System Monitor API...")
    redis = streams.redis_factory()
    app.state.redis = redis
    app.state.hub = DashboardHub(redis)
    app.state.admission = Admission(redis)
    app.state.agents = AgentConnections()
    tasks = [asyncio.create_task(app.state.hub.run()), asyncio.create_task(app.state.admission.run())]
    logger.info("System Monitor API started")
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await redis.aclose()
    logger.info("System Monitor API stopped")


app = FastAPI(
    title="System Monitor & Auto-Healing Platform",
    description="AI-powered system monitoring with intelligent auto-remediation",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- WebSockets ---------------------------------------------------------------------
@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    await websocket.accept()
    hub: DashboardHub = websocket.app.state.hub
    hub.add(websocket)
    try:
        while True:
            message = json.loads(await websocket.receive_text())
            if message.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except (WebSocketDisconnect, json.JSONDecodeError):
        pass
    finally:
        hub.discard(websocket)


@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    if not AGENT_ID_RE.match(agent_id):
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    state = websocket.app.state
    state.agents.sockets[agent_id] = websocket
    logger.info("Agent %s connected", agent_id)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = streams.validate_agent_message(agent_id, raw)
            except InvalidTelemetry as e:
                await websocket.send_text(json.dumps({"type": "error", "reason": str(e)}))
                continue

            if "type" in payload:
                # Control traffic (remediation results, config acks, pongs) isn't telemetry:
                # relay it to dashboards instead of storing it as a metrics sample.
                await streams.publish(state.redis, {**payload, "agent_id": agent_id})
                continue

            if state.admission.overloaded:
                state.admission.rejected += 1
                await websocket.send_text(json.dumps({"type": "throttle", "retry_after_s": 5,
                                                      "reason": "ingest backlog above limit"}))
                continue
            await streams.enqueue(state.redis, agent_id, raw)
            state.admission.accepted += 1
    except WebSocketDisconnect:
        pass
    except RedisError as e:
        logger.error("Redis unavailable, closing agent %s: %s", agent_id, e)
        await websocket.close(code=WS_TRY_AGAIN_LATER)
    finally:
        if state.agents.sockets.get(agent_id) is websocket:
            del state.agents.sockets[agent_id]
        logger.info("Agent %s disconnected", agent_id)


# --- health -------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "System Monitor & Auto-Healing Platform API", "version": app.version,
            "status": "operational"}


@app.get("/health")
async def health_check():
    """Liveness: the process is up and its event loop is responsive. No I/O on purpose."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ready")
async def readiness():
    """Readiness: dependencies reachable. A load balancer should only route here when 200."""
    checks = {}
    try:
        await app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"
    try:
        def ping_db():
            with SessionLocal() as db:
                db.execute(text("SELECT 1"))
        await asyncio.to_thread(ping_db)
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=checks)
    return {"status": "ready", "checks": checks}


# --- REST ---------------------------------------------------------------------------
app.include_router(metrics.router, prefix="/api/v1/metrics", tags=["Metrics"])
app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agents"])
app.include_router(alerts.router, prefix="/api/v1/alerts", tags=["Alerts"])


@app.post("/api/v1/agents/{agent_id}/restart")
async def restart_agent(agent_id: str):
    """Send restart command to specific agent"""
    if await app.state.agents.send(agent_id, {"type": "restart",
                                              "timestamp": datetime.now(timezone.utc).isoformat()}):
        return {"message": f"Restart command sent to agent {agent_id}"}
    raise HTTPException(status_code=404, detail=f"Agent {agent_id} not connected")


@app.post("/api/v1/agents/{agent_id}/remediate")
async def trigger_remediation(agent_id: str, issue_type: str = "general"):
    """Trigger an allowlisted remediation action on an agent"""
    if await app.state.agents.send(agent_id, {"type": "remediate", "issue_type": issue_type,
                                              "timestamp": datetime.now(timezone.utc).isoformat()}):
        return {"message": f"Remediation triggered for {issue_type} on agent {agent_id}"}
    raise HTTPException(status_code=404, detail=f"Agent {agent_id} not connected")


@app.get("/api/v1/system/pipeline")
async def pipeline_status():
    """Backlog per consumer group, dead-letter count and admission counters."""
    redis = app.state.redis
    try:
        groups = await redis.xinfo_groups(config.TELEMETRY_STREAM)
        length = await redis.xlen(config.TELEMETRY_STREAM)
    except RedisError:
        groups, length = [], 0
    return {
        "stream_length": length,
        "groups": {g["name"]: {"lag": g.get("lag"), "pending": g.get("pending"),
                               "consumers": g.get("consumers")} for g in groups},
        "dead_letters": await redis.xlen(config.DLQ_STREAM),
        "admission": {"overloaded": app.state.admission.overloaded,
                      "persist_lag": app.state.admission.persist_lag,
                      "accepted": app.state.admission.accepted,
                      "rejected": app.state.admission.rejected,
                      "max_lag": config.PERSIST_MAX_LAG},
    }


@app.get("/api/v1/system/status")
async def system_status():
    """Overall status as seen from this API replica"""
    return {
        "connected_agents_this_replica": len(app.state.agents.sockets),
        "active_dashboards_this_replica": len(app.state.hub.sockets),
        "ingest_overloaded": app.state.admission.overloaded,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
