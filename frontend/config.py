"""Configuration for the monitoring dashboard"""

# Backend Configuration
BACKEND_URL = "http://localhost:8000"
API_BASE = f"{BACKEND_URL}/api/v1"

# Dashboard Settings
AUTO_REFRESH_INTERVAL = 5000  # milliseconds (5 seconds)
MAX_METRICS_DISPLAY = 50      # Number of metric points to show

# Severity Colors
COLORS = {
    "critical": "#FF4B4B",
    "high": "#FFA500", 
    "medium": "#FFD700",
    "low": "#90EE90",
    "healthy": "#28a745",
    "warning": "#ffc107",
    "offline": "#6c757d"
}

# Thresholds for metrics
THRESHOLDS = {
    "cpu": {"warning": 80, "critical": 95},
    "memory": {"warning": 85, "critical": 95},
    "disk": {"warning": 90, "critical": 98},
    "network_latency": {"warning": 100, "critical": 200}
}