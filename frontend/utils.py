"""Utility functions for API calls and chart generation"""

import requests
import streamlit as st
import plotly.graph_objects as go
from config import API_BASE, COLORS, THRESHOLDS


# ==================== API FUNCTIONS ====================

def api_call(method, endpoint, data=None, params=None):
    """Make API call to backend"""
    try:
        url = f"{API_BASE}{endpoint}"
        if method == "GET":
            response = requests.get(url, params=params, timeout=10)
        elif method == "POST":
            response = requests.post(url, json=data, timeout=10)
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        st.error(f"API Error: {str(e)}")
        return {}


def get_agents():
    """Get all agents"""
    result = api_call("GET", "/agents")
    return result.get("agents", [])


def get_metrics(agent_id=None, limit=50):
    """Get metrics"""
    params = {"limit": limit}
    if agent_id:
        params["agent_id"] = agent_id
    result = api_call("GET", "/metrics", params=params)
    return result.get("metrics", [])


def get_alerts(status=None, severity=None):
    """Get alerts"""
    params = {}
    if status:
        params["status"] = status
    if severity:
        params["severity"] = severity
    result = api_call("GET", "/alerts", params=params)
    return result.get("alerts", [])


def get_system_status():
    """Get system status"""
    return api_call("GET", "/system/status")


def restart_agent(agent_id):
    """Restart an agent"""
    return api_call("POST", f"/agents/{agent_id}/restart")


def remediate_agent(agent_id):
    """Trigger remediation"""
    return api_call("POST", f"/agents/{agent_id}/remediate")


def resolve_alert(alert_id):
    """Resolve an alert"""
    return api_call("POST", f"/alerts/{alert_id}/resolve")


def check_health():
    """Check backend health"""
    try:
        from config import BACKEND_URL
        response = requests.get(f"{BACKEND_URL}/health", timeout=5)
        return response.status_code == 200
    except Exception as e:
        print(f"Health check failed: {e}")  # Debug
        return False


# ==================== CHART FUNCTIONS ====================

def create_gauge(value, title, max_value=100, thresholds=None):
    """Create a gauge chart"""
    if thresholds is None:
        thresholds = {"warning": 80, "critical": 95}
    
    # Determine color
    if value >= thresholds["critical"]:
        color = COLORS["critical"]
    elif value >= thresholds["warning"]:
        color = COLORS["high"]
    else:
        color = COLORS["healthy"]
    
    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=value,
        title={'text': title, 'font': {'size': 20}},
        delta={'reference': thresholds["warning"], 'increasing': {'color': COLORS["critical"]}},
        gauge={
            'axis': {'range': [None, max_value], 'tickwidth': 1},
            'bar': {'color': color},
            'bgcolor': "white",
            'borderwidth': 2,
            'bordercolor': "gray",
            'steps': [
                {'range': [0, thresholds["warning"]], 'color': '#E8F5E9'},
                {'range': [thresholds["warning"], thresholds["critical"]], 'color': '#FFF3E0'},
                {'range': [thresholds["critical"], max_value], 'color': '#FFEBEE'}
            ],
            'threshold': {
                'line': {'color': COLORS["critical"], 'width': 4},
                'thickness': 0.75,
                'value': thresholds["critical"]
            }
        }
    ))
    
    fig.update_layout(
        height=280,
        margin=dict(l=20, r=20, t=50, b=20),
        paper_bgcolor="white",
        font={'size': 14}
    )
    
    return fig


def create_line_chart(data, y_column, title, color="blue"):
    """Create a line chart"""
    fig = go.Figure()
    
    fig.add_trace(go.Scatter(
        x=list(range(len(data))),
        y=[d.get(y_column, 0) for d in data],
        mode='lines+markers',
        name=title,
        line=dict(color=color, width=3),
        marker=dict(size=6),
        fill='tozeroy',
        fillcolor=f'rgba({int(color[1:3], 16)}, {int(color[3:5], 16)}, {int(color[5:7], 16)}, 0.1)' if color.startswith('#') else None
    ))
    
    fig.update_layout(
        title=title,
        xaxis_title="Time Points",
        yaxis_title="Value (%)",
        height=300,
        hovermode='x unified',
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='white',
        margin=dict(l=40, r=20, t=40, b=40)
    )
    
    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
    
    return fig


def create_multi_line_chart(data, columns, title):
    """Create multi-line chart"""
    fig = go.Figure()
    
    colors = [COLORS["critical"], COLORS["high"], COLORS["medium"], COLORS["low"]]
    
    for idx, col in enumerate(columns):
        fig.add_trace(go.Scatter(
            x=list(range(len(data))),
            y=[d.get(col, 0) for d in data],
            mode='lines+markers',
            name=col.replace('_', ' ').title(),
            line=dict(color=colors[idx % len(colors)], width=2),
            marker=dict(size=5)
        ))
    
    fig.update_layout(
        title=title,
        xaxis_title="Time Points",
        yaxis_title="Usage (%)",
        height=350,
        hovermode='x unified',
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='white'
    )
    
    fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
    fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
    
    return fig


def create_anomaly_timeline(anomalies):
    """Create anomaly timeline scatter plot"""
    if not anomalies:
        fig = go.Figure()
        fig.add_annotation(
            text="✅ No Anomalies Detected",
            xref="paper", yref="paper",
            x=0.5, y=0.5, showarrow=False,
            font=dict(size=20, color=COLORS["healthy"])
        )
        fig.update_layout(height=300)
        return fig
    
    severity_map = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    
    fig = go.Figure()
    
    for severity in ['critical', 'high', 'medium', 'low']:
        severity_data = [a for a in anomalies if a.get('severity') == severity]
        if severity_data:
            fig.add_trace(go.Scatter(
                x=list(range(len(severity_data))),
                y=[severity_map[severity]] * len(severity_data),
                mode='markers',
                name=severity.upper(),
                marker=dict(
                    size=15,
                    color=COLORS[severity],
                    line=dict(width=2, color='white'),
                    symbol='circle'
                ),
                text=[a.get('description', 'No description') for a in severity_data],
                hovertemplate='<b>%{text}</b><br>Severity: ' + severity + '<extra></extra>'
            ))
    
    fig.update_layout(
        title="Anomaly Timeline",
        xaxis_title="Occurrence",
        yaxis_title="Severity Level",
        yaxis=dict(
            tickmode='array',
            tickvals=[1, 2, 3, 4],
            ticktext=['Low', 'Medium', 'High', 'Critical']
        ),
        height=350,
        hovermode='closest',
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='white'
    )
    
    return fig


def create_alert_bar_chart(alerts):
    """Create bar chart for alerts by severity"""
    if not alerts:
        fig = go.Figure()
        fig.add_annotation(
            text="✅ No Active Alerts",
            xref="paper", yref="paper",
            x=0.5, y=0.5, showarrow=False,
            font=dict(size=20, color=COLORS["healthy"])
        )
        fig.update_layout(height=300)
        return fig
    
    severity_counts = {}
    for alert in alerts:
        sev = alert.get('severity', 'unknown')
        severity_counts[sev] = severity_counts.get(sev, 0) + 1
    
    fig = go.Figure(data=[
        go.Bar(
            x=list(severity_counts.keys()),
            y=list(severity_counts.values()),
            marker_color=[COLORS.get(s, 'gray') for s in severity_counts.keys()],
            text=list(severity_counts.values()),
            textposition='auto',
        )
    ])
    
    fig.update_layout(
        title="Alerts by Severity",
        xaxis_title="Severity",
        yaxis_title="Count",
        height=300,
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='white'
    )
    
    return fig


def create_status_pie(agents):
    """Create pie chart for agent status"""
    if not agents:
        fig = go.Figure()
        fig.add_annotation(
            text="No Agents Connected",
            xref="paper", yref="paper",
            x=0.5, y=0.5, showarrow=False,
            font=dict(size=16)
        )
        fig.update_layout(height=300)
        return fig
    
    status_counts = {}
    for agent in agents:
        status = agent.get('status', 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
    
    fig = go.Figure(data=[go.Pie(
        labels=list(status_counts.keys()),
        values=list(status_counts.values()),
        marker=dict(colors=[COLORS.get(s, 'gray') for s in status_counts.keys()]),
        hole=0.4,
        textinfo='label+percent+value',
        textfont_size=14
    )])
    
    fig.update_layout(
        title="Agent Status Distribution",
        height=350,
        showlegend=True
    )
    
    return fig