
# backend/app/main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import asyncio
import json
import uvicorn
from typing import List, Dict, Any
from datetime import datetime
import logging
from contextlib import asynccontextmanager


from app.database import engine, get_db
from app.models import Agent, SystemMetrics, Alert
from app.api import agents, metrics, alerts
from app.services.ai_engine import AIEngine
from app.services.alert_manager import AlertManager
from app.api.utils.logger import setup_logger

# Setup logging
logger = setup_logger()

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.agent_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_type: str = "dashboard", agent_id: str = None):
        await websocket.accept()
        if client_type == "agent" and agent_id:
            self.agent_connections[agent_id] = websocket
            logger.info(f"Agent {agent_id} connected")
        else:
            self.active_connections.append(websocket)
            logger.info("Dashboard client connected")

    def disconnect(self, websocket: WebSocket, agent_id: str = None):
        if agent_id and agent_id in self.agent_connections:
            del self.agent_connections[agent_id]
            logger.info(f"Agent {agent_id} disconnected")
        elif websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("Dashboard client disconnected")

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_text(json.dumps(message))
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")

    async def broadcast_to_dashboards(self, message: dict):
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except:
                disconnected.append(connection)
        
        # Remove disconnected clients
        for connection in disconnected:
            self.active_connections.remove(connection)

    async def send_to_agent(self, agent_id: str, message: dict):
        if agent_id in self.agent_connections:
            try:
                await self.agent_connections[agent_id].send_text(json.dumps(message))
                return True
            except:
                del self.agent_connections[agent_id]
        return False

# Initialize services
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting System Monitor API...")
    from app.database import Base
    Base.metadata.create_all(bind=engine)
    # Initialize AI Engine
    app.state.ai_engine = AIEngine()
    await app.state.ai_engine.initialize()
    
    # Initialize Alert Manager
    app.state.alert_manager = AlertManager()
    
    # Start background tasks
    asyncio.create_task(background_monitoring())
    
    logger.info("System Monitor API started successfully!")
    yield
    
    # Shutdown
    logger.info("Shutting down System Monitor API...")

# Create FastAPI app
app = FastAPI(
    title="System Monitor & Auto-Healing Platform",
    description="AI-powered system monitoring with intelligent auto-remediation",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize connection manager
manager = ConnectionManager()

# Security
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    # Simple token validation - in production, use proper JWT
    if credentials.credentials != "demo_token":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"user_id": "demo_user"}

# WebSocket endpoints
@app.websocket("/ws/dashboard")
async def dashboard_websocket(websocket: WebSocket):
    await manager.connect(websocket, "dashboard")
    try:
        while True:
            # Keep connection alive and handle incoming messages
            data = await websocket.receive_text()
            message = json.loads(data)
            
            if message.get("type") == "ping":
                await manager.send_personal_message({"type": "pong"}, websocket)
            
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.websocket("/ws/agent/{agent_id}")
async def agent_websocket(websocket: WebSocket, agent_id: str):
    await manager.connect(websocket, "agent", agent_id)
    
    # ✅ ADD THIS: Save agent to database
    try:
        db = next(get_db())
        
        # Check if agent exists
        existing_agent = db.query(Agent).filter(Agent.agent_id == agent_id).first()
        
        if not existing_agent:
            # Create new agent
            new_agent = Agent(
                agent_id=agent_id,
                hostname=agent_id,
                platform="Unknown",
                status="healthy",
                last_seen=datetime.now()
            )
            db.add(new_agent)
            db.commit()
            logger.info(f"New agent {agent_id} registered in database")
        else:
            # Update existing agent
            existing_agent.status = "healthy"
            existing_agent.last_seen = datetime.now()
            db.commit()
            logger.info(f"Agent {agent_id} status updated")
        
        db.close()
    except Exception as e:
        logger.error(f"Error saving agent to DB: {e}")
    
    try:
        while True:
            # Receive metrics from agent
            data = await websocket.receive_text()
            metrics_data = json.loads(data)
            
            # ✅ ALSO ADD: Save metrics to database
            try:
                db = next(get_db())
                new_metric = SystemMetrics(
                    agent_id=agent_id,
                    raw_data=metrics_data
                )
                db.add(new_metric)
                db.commit()
                db.close()
            except Exception as e:
                logger.error(f"Error saving metrics to DB: {e}")
            
            # Process metrics with AI engine
            if hasattr(app.state, 'ai_engine'):
                anomalies = await app.state.ai_engine.detect_anomalies(metrics_data)
                
                if anomalies:
                    # Broadcast anomalies to dashboards
                    await manager.broadcast_to_dashboards({
                        "type": "anomaly_detected",
                        "agent_id": agent_id,
                        "anomalies": anomalies,
                        "timestamp": datetime.now().isoformat()
                    })
            
            # Broadcast real-time metrics to dashboards
            await manager.broadcast_to_dashboards({
                "type": "metrics_update",
                "agent_id": agent_id,
                "metrics": metrics_data,
                "timestamp": datetime.now().isoformat()
            })
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, agent_id)


# REST API endpoints
@app.get("/")
async def root():
    return {
        "message": "System Monitor & Auto-Healing Platform API",
        "version": "1.0.0",
        "status": "operational",
        "features": [
            "Real-time system monitoring",
            "AI-powered anomaly detection",
            "Cross-platform agent support",
            "Intelligent auto-remediation",
            "Predictive maintenance"
        ]
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "database": "operational",
            "ai_engine": "operational",
            "alert_manager": "operational"
        }
    }

# Include API routers
app.include_router(metrics.router, prefix="/api/v1/metrics", tags=["Metrics"])
app.include_router(agents.router, prefix="/api/v1/agents", tags=["Agents"])      
app.include_router(alerts.router, prefix="/api/v1/alerts", tags=["Alerts"])      

# Background monitoring task
async def background_monitoring():
    """Background task for continuous system monitoring"""
    while True:
        try:
            # Check system health
            if hasattr(app.state, 'ai_engine'):
                health_status = await app.state.ai_engine.system_health_check()
                
                if health_status.get("critical_issues"):
                    await manager.broadcast_to_dashboards({
                        "type": "critical_alert",
                        "issues": health_status["critical_issues"],
                        "timestamp": datetime.now().isoformat()
                    })
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            logger.error(f"Background monitoring error: {e}")
            await asyncio.sleep(60)

# Agent management endpoints
@app.post("/api/v1/agents/{agent_id}/restart")
async def restart_agent(agent_id: str):  
    """Send restart command to specific agent"""
    success = await manager.send_to_agent(agent_id, {
        "type": "command",
        "action": "restart",
        "timestamp": datetime.now().isoformat()
    })
    
    if success:
        return {"message": f"Restart command sent to agent {agent_id}"}
    else:
        raise HTTPException(
            status_code=404,
            detail=f"Agent {agent_id} not connected"
        )

@app.post("/api/v1/agents/{agent_id}/remediate")
async def trigger_remediation(agent_id: str, issue_type: str = "general"):  
    """Trigger auto-remediation for specific issue"""
    success = await manager.send_to_agent(agent_id, {
        "type": "remediate",
        "issue_type": issue_type,
        "timestamp": datetime.now().isoformat()
    })
    
    if success:
        return {"message": f"Remediation triggered for {issue_type} on agent {agent_id}"}
    else:
        raise HTTPException(
            status_code=404,
            detail=f"Agent {agent_id} not connected"
        )

@app.get("/api/v1/system/status")
async def system_status():  
    """Get overall system status"""
    return {
        "connected_agents": len(manager.agent_connections),
        "active_dashboards": len(manager.active_connections),
        "healthy_agents": len(manager.agent_connections),  
        "anomalies_24h": 0,  
        "system_health": "operational",
        "ai_engine_status": "active",
        "last_updated": datetime.now().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )