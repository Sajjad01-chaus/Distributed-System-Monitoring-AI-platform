"""API replica: accepts agent telemetry onto the stream, routes commands, serves REST, relays
dashboard events. Several replicas run behind a load balancer (see docker-compose.yml, nginx/).

Nothing slow happens on the request path: persistence and anomaly detection run in separate
worker processes (app.worker) that consume the Redis stream. See docs/architecture.md.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from redis.exceptions import RedisError
from sqlalchemy import func, select, text

from app import config
from app.api import agents, alerts, metrics
from app.api.utils.logger import setup_logger
from app.control import CommandBus, Presence
from app.database import SessionLocal
from app.models import Agent, Alert
from app.pipeline import streams
from app.pipeline.hub import Admission, DashboardHub
from app.pipeline.streams import AGENT_ID_RE, InvalidTelemetry

logger = setup_logger()

# Close codes (RFC 6455 / IANA registry)
WS_POLICY_VIOLATION = 1008
WS_SERVICE_RESTART = 1012   # "reconnect now": sent to agents when this replica shuts down
WS_TRY_AGAIN_LATER = 1013


class AgentConnections:
    """Agent sockets held by *this* replica. Other replicas reach them via the CommandBus."""

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

    async def close_all(self, code: int) -> None:
        for ws in list(self.sockets.values()):
            try:
                await ws.close(code=code)
            except Exception:
                pass
        self.sockets.clear()


# --- WebSockets ---------------------------------------------------------------------
ws_router = APIRouter()


@ws_router.websocket("/ws/dashboard")
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


@ws_router.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    if not AGENT_ID_RE.match(agent_id):
        await websocket.close(code=WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    state = websocket.app.state
    state.agents.sockets[agent_id] = websocket
    await state.presence.claim(agent_id)
    presence_refreshed = time.monotonic()
    logger.info("Agent %s connected to %s", agent_id, state.replica)
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
                # record command outcomes, then relay to dashboards instead of storing a sample.
                if payload.get("command_id"):
                    await state.commands.record_result(agent_id, payload)
                await streams.publish(state.redis, {**payload, "agent_id": agent_id})
                continue

            if state.admission.overloaded:
                state.admission.rejected += 1
                await websocket.send_text(json.dumps({"type": "throttle", "retry_after_s": 5,
                                                      "reason": "ingest backlog above limit"}))
                continue
            await streams.enqueue(state.redis, agent_id, raw)
            state.admission.accepted += 1
            # Keep presence alive without an extra write per message: refresh a few times per TTL.
            if time.monotonic() - presence_refreshed > config.PRESENCE_TTL_S / 3:
                await state.presence.refresh(agent_id)
                presence_refreshed = time.monotonic()
    except WebSocketDisconnect:
        pass
    except RedisError as e:
        logger.error("Redis unavailable, closing agent %s: %s", agent_id, e)
        await websocket.close(code=WS_TRY_AGAIN_LATER)
    finally:
        if state.agents.sockets.get(agent_id) is websocket:
            del state.agents.sockets[agent_id]
            try:
                await state.presence.release(agent_id)
            except RedisError:
                pass   # TTL expires it
        logger.info("Agent %s disconnected from %s", agent_id, state.replica)


# --- health -------------------------------------------------------------------------
health_router = APIRouter()


@health_router.get("/")
async def root(request: Request):
    return {"message": "System Monitor & Auto-Healing Platform API", "version": request.app.version,
            "status": "operational", "replica": request.app.state.replica}


@health_router.get("/health")
async def health_check():
    """Liveness: the process is up and its event loop is responsive. No I/O on purpose."""
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


@health_router.get("/ready")
async def readiness(request: Request):
    """Readiness: dependencies reachable. A load balancer should only route here when 200."""
    checks = {}
    try:
        await request.app.state.redis.ping()
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
    if not all(v == "ok" for v in checks.values()):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=checks)
    return {"status": "ready", "checks": checks}


# --- control plane ------------------------------------------------------------------
control_router = APIRouter()

_COMMAND_HTTP_STATUS = {"delivered": 202, "not_connected": 404, "undeliverable": 503, "unacknowledged": 504}


async def _command(request: Request, agent_id: str, command: dict):
    result = await request.app.state.commands.send(agent_id, command)
    code = _COMMAND_HTTP_STATUS[result["status"]]
    if code >= 400:
        raise HTTPException(status_code=code, detail=result)
    return result


@control_router.post("/api/v1/agents/{agent_id}/restart", status_code=202)
async def restart_agent(request: Request, agent_id: str):
    """Restart an agent, wherever (on whichever replica) it is connected"""
    return await _command(request, agent_id, {"type": "restart"})


@control_router.post("/api/v1/agents/{agent_id}/remediate", status_code=202)
async def trigger_remediation(request: Request, agent_id: str, issue_type: str = "general"):
    """Trigger an allowlisted remediation action on an agent, wherever it is connected"""
    return await _command(request, agent_id, {"type": "remediate", "issue_type": issue_type})


@control_router.get("/api/v1/commands/{command_id}")
async def command_status(request: Request, command_id: str):
    """Lifecycle of a command: pending -> delivered -> completed (with the agent's result)"""
    record = await request.app.state.commands.get(command_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown or expired command")
    if "result" in record:
        record["result"] = json.loads(record["result"])
    return record


@control_router.get("/api/v1/system/pipeline")
async def pipeline_status(request: Request):
    """Backlog per consumer group, dead-letter count and this replica's admission counters."""
    state = request.app.state
    try:
        groups = await state.redis.xinfo_groups(config.TELEMETRY_STREAM)
        length = await state.redis.xlen(config.TELEMETRY_STREAM)
    except RedisError:
        groups, length = [], 0
    return {
        "stream_length": length,
        "groups": {g["name"]: {"lag": g.get("lag"), "pending": g.get("pending"),
                               "consumers": g.get("consumers")} for g in groups},
        "dead_letters": await state.redis.xlen(config.DLQ_STREAM),
        "admission": {"replica": state.replica, "overloaded": state.admission.overloaded,
                      "persist_lag": state.admission.persist_lag, "accepted": state.admission.accepted,
                      "rejected": state.admission.rejected, "max_lag": config.PERSIST_MAX_LAG},
    }


@control_router.get("/api/v1/system/status")
def system_status(request: Request):
    """Fleet-wide status from the database (identical on every replica), plus this replica's
    own connection counts. Sync handler: runs in the threadpool, off the event loop."""
    state = request.app.state
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=config.AGENT_STALE_S)
    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(Agent))
        reporting = db.scalar(select(func.count()).select_from(Agent).where(Agent.last_seen >= stale))
        active = db.scalar(select(func.count()).select_from(Alert).where(Alert.status == "active"))
        live = select(Agent.agent_id).where(Agent.last_seen >= stale)
        unhealthy = db.scalar(select(func.count(func.distinct(Alert.agent_id)))
                              .where(Alert.status == "active", Alert.severity.in_(("high", "critical")),
                                     Alert.agent_id.in_(live)))   # silent agents aren't "unhealthy", just gone
        offline = db.scalar(select(func.count()).select_from(Agent).where(Agent.status == "offline"))
        last_24h = db.scalar(select(func.count()).select_from(Alert)
                             .where(Alert.first_seen >= now - timedelta(hours=24)))
    return {
        "total_agents": total,
        "connected_agents": reporting,                       # reported within AGENT_STALE_S
        "healthy_agents": max(reporting - unhealthy, 0),
        "offline_agents": offline,
        "active_alerts": active,
        "anomalies_24h": last_24h,
        "system_health": "degraded" if state.admission.overloaded else "operational",
        "this_replica": {"id": state.replica, "agent_sockets": len(state.agents.sockets),
                         "dashboard_sockets": len(state.hub.sockets)},
        "last_updated": now.isoformat(),
    }


# --- app factory --------------------------------------------------------------------
def create_app(replica_id: str | None = None) -> FastAPI:
    """One FastAPI app per replica. Tests build two to exercise cross-replica behaviour."""
    replica = replica_id or config.REPLICA_ID

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis = streams.redis_factory()
        state = app.state
        state.replica = replica
        state.redis = redis
        state.hub = DashboardHub(redis)
        state.admission = Admission(redis)
        state.agents = AgentConnections()
        state.presence = Presence(redis, replica)
        state.commands = CommandBus(redis, replica)
        tasks = [asyncio.create_task(state.hub.run()), asyncio.create_task(state.admission.run()),
                 asyncio.create_task(state.commands.listen(state.agents.send))]
        logger.info("API replica %s started", replica)
        yield
        # Tell agents to reconnect now (the load balancer sends them to a surviving replica)
        # rather than letting them discover a dead socket on their next send.
        await state.agents.close_all(WS_SERVICE_RESTART)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await redis.aclose()
        logger.info("API replica %s stopped", replica)

    app = FastAPI(title="System Monitor & Auto-Healing Platform",
                  description="AI-powered system monitoring with intelligent auto-remediation",
                  version="3.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def replica_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Replica-Id"] = replica   # makes load balancing observable
        return response

    app.include_router(ws_router)
    app.include_router(health_router)
    app.include_router(control_router)
    app.include_router(metrics.router, prefix="/api/v1/metrics", tags=["Metrics"])
    app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agents"])
    app.include_router(alerts.router, prefix="/api/v1/alerts", tags=["Alerts"])
    return app


app = create_app()
