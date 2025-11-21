
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import pandas as pd
from config import AUTO_REFRESH_INTERVAL, COLORS, THRESHOLDS, MAX_METRICS_DISPLAY
from utils import *

st.set_page_config(
    page_title="System Monitoring Dashboard",
    page_icon="🖥️",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ==================== CUSTOM CSS ====================

st.markdown("""
<style>
    .main-header {
        font-size: 2.8rem;
        font-weight: bold;
        color: #1f77b4;
        text-align: center;
        padding: 1rem 0;
        margin-bottom: 1rem;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    
    .metric-card {
        background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
        padding: 1.5rem;
        border-radius: 15px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        margin: 0.5rem 0;
    }
    
    .status-badge {
        padding: 0.3rem 0.8rem;
        border-radius: 12px;
        font-weight: bold;
        font-size: 0.9rem;
        display: inline-block;
    }
    
    .status-healthy { background-color: #d4edda; color: #155724; }
    .status-warning { background-color: #fff3cd; color: #856404; }
    .status-critical { background-color: #f8d7da; color: #721c24; }
    .status-offline { background-color: #e2e3e5; color: #383d41; }
    
    .stButton>button {
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    
    .stButton>button:hover {
        transform: translateY(-2px);
        box-shadow: 0 4px 8px rgba(0,0,0,0.2);
    }
    
    div[data-testid="stExpander"] {
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        margin: 0.5rem 0;
    }
    
    .info-box {
        background-color: #e7f3ff;
        padding: 1rem;
        border-left: 4px solid #2196F3;
        border-radius: 4px;
        margin: 1rem 0;
        color: #000000;
    }
</style>
""", unsafe_allow_html=True)

# ==================== AUTO REFRESH ====================

count = st_autorefresh(interval=AUTO_REFRESH_INTERVAL, key="datarefresh")

# ==================== SIDEBAR ====================

with st.sidebar:
    st.image("https://via.placeholder.com/200x80/667eea/ffffff?text=SYSTEM+MONITOR", 
             width=200)
    st.markdown("---")
    
    # Backend health check
    is_healthy = check_health()
    if is_healthy:
        st.success("✅ Backend Online")
    else:
        st.error("❌ Backend Offline")
        st.warning("Please start the backend server at http://localhost:8000")
    
    st.markdown("---")
    
    # Quick Stats
    st.subheader("📊 Quick Stats")
    
    if is_healthy:
        try:
            system_status = get_system_status()
            agents = get_agents()
            active_alerts = get_alerts(status="active")
            
            st.metric("🖥️ Total Agents", len(agents))
            st.metric("✅ Healthy", system_status.get('healthy_agents', 0))
            st.metric("⚠️ Active Alerts", len(active_alerts))
            st.metric("🤖 Anomalies (24h)", system_status.get('anomalies_24h', 0))
        except:
            st.warning("Unable to fetch stats")
    
    st.markdown("---")
    
    # Settings
    st.subheader("⚙️ Settings")
    auto_refresh = st.checkbox("Auto Refresh", value=True)
    
    if not auto_refresh:
        if st.button("🔄 Manual Refresh"):
            st.rerun()
    
    st.markdown("---")
    
    # Info
    st.info("""
    **📖 Navigation**
    
    Use tabs above to navigate:
    - 📊 Dashboard
    - 📈 Metrics
    - 🤖 Anomalies
    - ⚠️ Alerts
    - 🔮 AI Insights
    - 📉 Analytics
    """)

# ==================== MAIN HEADER ====================

st.markdown('<h1 class="main-header">🖥️ Distributed System Monitoring Dashboard</h1>', 
            unsafe_allow_html=True)

if not is_healthy:
    st.error("⚠️ Cannot connect to backend. Please ensure the backend is running on http://localhost:8000")
    st.stop()

# ==================== TABS ====================

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 Dashboard", 
    "📈 Real-Time Metrics", 
    "🤖 Anomaly Detection",
    "⚠️ Alerts",
    "🔮 AI Insights",
    "📉 Historical Analytics"
])

# ==================== TAB 1: DASHBOARD ====================

with tab1:
    st.header("System Overview Dashboard")
    
    # Get data
    agents = get_agents()
    system_status = get_system_status()
    alerts = get_alerts(status="active")
    
    # Top metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.metric("🖥️ Total Agents", len(agents), 
                 delta=f"{system_status.get('healthy_agents', 0)} healthy")
        st.markdown('</div>', unsafe_allow_html=True)
    
    with col2:
        healthy_count = system_status.get('healthy_agents', 0)
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.metric("✅ Healthy Agents", healthy_count,
                 delta=f"{(healthy_count/len(agents)*100):.1f}%" if agents else "0%")
        st.markdown('</div>', unsafe_allow_html=True)
    
    with col3:
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.metric("⚠️ Active Alerts", len(alerts),
                 delta="Critical" if any(a.get('severity') == 'critical' for a in alerts) else "Normal",
                 delta_color="inverse")
        st.markdown('</div>', unsafe_allow_html=True)
    
    with col4:
        anomaly_count = system_status.get('anomalies_24h', 0)
        st.markdown('<div class="metric-card">', unsafe_allow_html=True)
        st.metric("🤖 Anomalies (24h)", anomaly_count,
                 delta="Detection Active")
        st.markdown('</div>', unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Visualizations
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("Agent Status Distribution")
        fig = create_status_pie(agents)
        st.plotly_chart(fig, width='stretch', key='chart_1')
    
    with col2:
        st.subheader("Alerts by Severity")
        fig = create_alert_bar_chart(alerts)
        st.plotly_chart(fig, width='stretch', key='chart_2')
    
    st.markdown("---")
    
    # Agent Management
    st.subheader("🎛️ Connected Agents Management")
    
    if agents:
        for agent in agents:
            with st.expander(f"🖥️ **{agent.get('agent_id', 'Unknown')}** - {agent.get('hostname', 'N/A')}"):
                col1, col2, col3, col4 = st.columns([2, 2, 1, 1])
                
                with col1:
                    status = agent.get('status', 'unknown')
                    status_class = f"status-{status}"
                    st.markdown(f'<span class="status-badge {status_class}">{status.upper()}</span>', 
                               unsafe_allow_html=True)
                
                with col2:
                    st.write(f"**Last Seen:** {agent.get('last_seen', 'Unknown')}")
                
                with col3:
                    if st.button("🔄 Restart", key=f"restart_{agent.get('agent_id')}"):
                        result = restart_agent(agent.get('agent_id'))
                        if result:
                            st.success("✅ Restart initiated!")
                            st.rerun()
                
                with col4:
                    if st.button("🔧 Fix", key=f"fix_{agent.get('agent_id')}"):
                        result = remediate_agent(agent.get('agent_id'))
                        if result:
                            st.success("✅ Remediation started!")
                            st.rerun()
                
                # Agent details
                st.write(f"**Platform:** {agent.get('platform', 'Unknown')}")
                st.write(f"**Version:** {agent.get('version', 'Unknown')}")
    else:
        st.info("🔍 No agents connected. Deploy agents on your systems to start monitoring.")

# ==================== TAB 2: REAL-TIME METRICS ====================

with tab2:
    st.header("📈 Real-Time System Metrics")
    
    agents = get_agents()
    
    if not agents:
        st.warning("⚠️ No agents connected. Please deploy agents to start collecting metrics.")
    else:
        # Agent selector
        agent_ids = [a.get('agent_id') for a in agents]
        selected_agent = st.selectbox("🖥️ Select Agent", agent_ids)
        
        # Get metrics for selected agent
        metrics = get_metrics(agent_id=selected_agent, limit=MAX_METRICS_DISPLAY)
        
        if metrics:
            latest = metrics[0]
            
            st.subheader("Current Status")
            
            # Gauges
            col1, col2, col3 = st.columns(3)
            
            with col1:
                cpu_value = latest.get('cpu_usage', 0)
                fig = create_gauge(cpu_value, "CPU Usage (%)", 100, THRESHOLDS['cpu'])
                st.plotly_chart(fig, width='stretch', key='chart_3')
            
            with col2:
                memory_value = latest.get('memory_usage', 0)
                fig = create_gauge(memory_value, "Memory Usage (%)", 100, THRESHOLDS['memory'])
                st.plotly_chart(fig, width='stretch', key='chart_4')
            
            with col3:
                disk_value = latest.get('disk_usage', 0)
                fig = create_gauge(disk_value, "Disk Usage (%)", 100, THRESHOLDS['disk'])
                st.plotly_chart(fig, width='stretch', key='chart_5')
            
            st.markdown("---")
            
            st.subheader("Trend Analysis")
            
            # Multi-metric chart
            fig = create_multi_line_chart(
                metrics[:30],  # Last 30 points
                ['cpu_usage', 'memory_usage', 'disk_usage'],
                "Resource Usage Trends"
            )
            st.plotly_chart(fig, width='stretch', key='chart_6')
            
            st.markdown("---")
            
            # Individual charts
            col1, col2 = st.columns(2)
            
            with col1:
                fig = create_line_chart(metrics[:30], 'cpu_usage', 'CPU Usage History', COLORS['critical'])
                st.plotly_chart(fig, width='stretch', key='chart_7')
            
            with col2:
                fig = create_line_chart(metrics[:30], 'memory_usage', 'Memory Usage History', COLORS['high'])
                st.plotly_chart(fig, width='stretch', key='chart_8')
            
            # Network metrics if available
            if latest.get('network_latency'):
                st.subheader("Network Performance")
                col1, col2 = st.columns(2)
                
                with col1:
                    latency = latest.get('network_latency', 0)
                    fig = create_gauge(latency, "Network Latency (ms)", 300, THRESHOLDS['network_latency'])
                    st.plotly_chart(fig, width='stretch', key='chart_9')
                
                with col2:
                    fig = create_line_chart(metrics[:30], 'network_latency', 'Latency History', COLORS['medium'])
                    st.plotly_chart(fig, width='stretch', key='chart_10')
            
            # Raw data table
            with st.expander("📋 View Raw Metrics Data"):
                df = pd.DataFrame(metrics[:20])
                st.dataframe(df, width='stretch')
        else:
            st.info("📊 No metrics available for this agent yet. Metrics will appear once the agent starts collecting data.")

# ==================== TAB 3: ANOMALY DETECTION ====================

with tab3:
    st.header("🤖 AI-Powered Anomaly Detection")
    
    st.markdown("""
    <div class="info-box">
    <b>🧠 Multi-Layer Detection System:</b><br>
    • <b>Layer 1:</b> Machine Learning (Isolation Forest)<br>
    • <b>Layer 2:</b> Threshold-based detection<br>
    • <b>Layer 3:</b> Pattern-based analysis (CPU spikes, memory leaks)<br>
    • <b>Layer 4:</b> Temporal context analysis
    </div>
    """, unsafe_allow_html=True)
    
    # Get metrics and filter anomalies
    all_metrics = get_metrics(limit=100)
    anomalies = [m for m in all_metrics if m.get('is_anomaly', False)]
    
    # Stats
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("🔍 Total Scanned", len(all_metrics))
    
    with col2:
        st.metric("🚨 Anomalies Found", len(anomalies))
    
    with col3:
        critical_anomalies = sum(1 for a in anomalies if a.get('severity') == 'critical')
        st.metric("🔴 Critical", critical_anomalies)
    
    with col4:
        detection_rate = (len(anomalies) / len(all_metrics) * 100) if all_metrics else 0
        st.metric("📊 Detection Rate", f"{detection_rate:.1f}%")
    
    st.markdown("---")
    
    # Anomaly timeline
    st.subheader("Anomaly Timeline")
    fig = create_anomaly_timeline(anomalies)
    st.plotly_chart(fig, width='stretch', key='chart_11')
    
    st.markdown("---")
    
    # Anomaly details
    st.subheader("Detected Anomalies")
    
    if anomalies:
        # Severity filter
        severity_filter = st.multiselect(
            "Filter by Severity",
            ["critical", "high", "medium", "low"],
            default=["critical", "high"]
        )
        
        filtered_anomalies = [a for a in anomalies if a.get('severity') in severity_filter]
        
        for anomaly in filtered_anomalies[:20]:  # Show top 20
            severity = anomaly.get('severity', 'unknown')
            
            # Severity icon
            icons = {
                'critical': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '🟢'
            }
            icon = icons.get(severity, '⚪')
            
            with st.expander(f"{icon} **{severity.upper()}** - Agent: {anomaly.get('agent_id')} - {anomaly.get('timestamp', 'Unknown')}"):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**Type:** {anomaly.get('anomaly_type', 'Unknown')}")
                    st.write(f"**Description:** {anomaly.get('description', 'No description available')}")
                    st.write(f"**Detection Method:** {anomaly.get('detection_method', 'ML-based')}")
                    
                    if anomaly.get('remediation_suggestion'):
                        st.info(f"💡 **Suggested Action:** {anomaly.get('remediation_suggestion')}")
                
                with col2:
                    st.metric("ML Confidence Score", f"{anomaly.get('anomaly_score', 0):.2f}")
                    st.metric("Affected Metric", anomaly.get('metric_type', 'N/A'))
                    
                    # Action button
                    if st.button("🔧 Auto-Fix", key=f"fix_anomaly_{anomaly.get('id', anomaly.get('timestamp'))}"):
                        result = remediate_agent(anomaly.get('agent_id'))
                        if result:
                            st.success("✅ Remediation initiated!")
    else:
        st.success("✅ **No anomalies detected!** Your systems are running smoothly.")

# ==================== TAB 4: ALERTS ====================

with tab4:
    st.header("⚠️ System Alerts Management")
    
    # Filters
    col1, col2, col3 = st.columns(3)
    
    with col1:
        status_filter = st.selectbox("Status", ["all", "active", "resolved"])
    
    with col2:
        severity_filter = st.selectbox("Severity", ["all", "critical", "high", "medium", "low"])
    
    with col3:
        st.write("")  # Spacing
        if st.button("🔄 Refresh Alerts"):
            st.rerun()
    
    # Get alerts based on filters
    alerts = get_alerts(
        status=None if status_filter == "all" else status_filter,
        severity=None if severity_filter == "all" else severity_filter
    )
    
    # Stats
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("📊 Total Alerts", len(alerts))
    
    with col2:
        active = sum(1 for a in alerts if a.get('status') == 'active')
        st.metric("🔴 Active", active)
    
    with col3:
        resolved = sum(1 for a in alerts if a.get('status') == 'resolved')
        st.metric("✅ Resolved", resolved)
    
    with col4:
        critical = sum(1 for a in alerts if a.get('severity') == 'critical')
        st.metric("⚠️ Critical", critical)
    
    st.markdown("---")
    
    # Alert visualization
    col1, col2 = st.columns(2)
    
    with col1:
        fig = create_alert_bar_chart(alerts)
        st.plotly_chart(fig, width='stretch', key='chart_12')
    
    with col2:
        # Alert status pie
        if alerts:
            status_counts = {}
            for alert in alerts:
                status = alert.get('status', 'unknown')
                status_counts[status] = status_counts.get(status, 0) + 1
            
            fig = go.Figure(data=[go.Pie(
                labels=list(status_counts.keys()),
                values=list(status_counts.values()),
                hole=0.4
            )])
            fig.update_layout(title="Alert Status Distribution", height=300)
            st.plotly_chart(fig, width='stretch', key='chart_13')
    
    st.markdown("---")
    
    # Alert list
    st.subheader("Alert Details")
    
    if alerts:
        for alert in alerts:
            severity = alert.get('severity', 'unknown')
            status = alert.get('status', 'unknown')
            
            # Icons
            severity_icons = {
                'critical': '🔴',
                'high': '🟠',
                'medium': '🟡',
                'low': '🟢'
            }
            
            status_emoji = '🔴' if status == 'active' else '✅'
            
            icon = severity_icons.get(severity, '⚪')
            
            with st.expander(f"{icon} {status_emoji} **{alert.get('title', 'Alert')}** - {alert.get('timestamp', '')}"):
                col1, col2, col3 = st.columns([2, 1, 1])
                
                with col1:
                    st.write(f"**Agent:** {alert.get('agent_id', 'Unknown')}")
                    st.write(f"**Description:** {alert.get('description', 'No description')}")
                    st.write(f"**Type:** {alert.get('alert_type', 'Unknown')}")
                
                with col2:
                    st.markdown(f'<span class="status-badge status-{severity}">{severity.upper()}</span>', 
                               unsafe_allow_html=True)
                    st.write(f"**Status:** {status}")
                
                with col3:
                    if status == 'active':
                        if st.button("✅ Resolve", key=f"resolve_{alert.get('id')}"):
                            result = resolve_alert(alert.get('id'))
                            if result:
                                st.success("Alert resolved!")
                                st.rerun()
                    else:
                        st.success("Resolved ✓")
                
                # Additional info
                if alert.get('resolved_at'):
                    st.info(f"✅ Resolved at: {alert.get('resolved_at')}")
    else:
        st.success("✅ No alerts matching the current filters!")

# ==================== TAB 5: AI INSIGHTS ====================

with tab5:
    st.header("🔮 AI-Powered Insights & Predictions")
    
    st.markdown("""
    <div class="info-box">
    <b>🧠 AI Capabilities:</b><br>
    • Failure prediction using ML models<br>
    • Performance optimization recommendations<br>
    • Trend analysis and forecasting<br>
    • System health scoring
    </div>
    """, unsafe_allow_html=True)
    
    # Get system data
    system_status = get_system_status()
    agents = get_agents()
    metrics = get_metrics(limit=100)
    anomalies = [m for m in metrics if m.get('is_anomaly', False)]
    
    # System Health Score
    st.subheader("🏥 System Health Score")
    
    # Calculate health score (simple algorithm)
    if agents:
        healthy_ratio = system_status.get('healthy_agents', 0) / len(agents)
        anomaly_ratio = 1 - (len(anomalies) / len(metrics)) if metrics else 1
        health_score = (healthy_ratio * 0.6 + anomaly_ratio * 0.4) * 100
        
        col1, col2, col3 = st.columns([2, 1, 1])
        
        with col1:
            fig = create_gauge(health_score, "Overall System Health", 100, {"warning": 70, "critical": 50})
            st.plotly_chart(fig, width='stretch', key='chart_14')
        
        with col2:
            st.metric("Health Rating", f"{health_score:.1f}%")
            if health_score >= 80:
                st.success("Excellent 🌟")
            elif health_score >= 60:
                st.warning("Good ⚠️")
            else:
                st.error("Needs Attention 🔴")
        
        with col3:
            st.metric("Agents Monitored", len(agents))
            st.metric("Data Points", len(metrics))
    
    st.markdown("---")
    
    # Failure Predictions
    st.subheader("🔮 Failure Predictions")
    
    predictions = []
    
    # Simple prediction logic based on trends
    for agent in agents:
        agent_metrics = [m for m in metrics if m.get('agent_id') == agent.get('agent_id')]
        
        if len(agent_metrics) >= 5:
            recent = agent_metrics[:5]
            
            # Check for increasing trend in CPU
            cpu_trend = [m.get('cpu_usage', 0) for m in recent]
            if len(cpu_trend) >= 3 and cpu_trend[0] > cpu_trend[-1] + 20:
                predictions.append({
                    'agent_id': agent.get('agent_id'),
                    'type': 'CPU Overload',
                    'probability': 'High',
                    'time_estimate': '2-4 hours',
                    'recommendation': 'Consider scaling up CPU resources or optimizing running processes'
                })
            
            # Check for memory leak pattern
            memory_trend = [m.get('memory_usage', 0) for m in recent]
            if all(memory_trend[i] < memory_trend[i+1] for i in range(len(memory_trend)-1)):
                predictions.append({
                    'agent_id': agent.get('agent_id'),
                    'type': 'Memory Leak',
                    'probability': 'Medium',
                    'time_estimate': '6-12 hours',
                    'recommendation': 'Investigate memory-intensive processes and consider restart'
                })
    
    if predictions:
        for pred in predictions:
            with st.expander(f"⚠️ Predicted Issue: {pred['type']} on {pred['agent_id']}"):
                col1, col2 = st.columns([2, 1])
                
                with col1:
                    st.write(f"**Probability:** {pred['probability']}")
                    st.write(f"**Estimated Time to Failure:** {pred['time_estimate']}")
                    st.info(f"💡 **Recommendation:** {pred['recommendation']}")
                
                with col2:
                    if st.button("🔧 Apply Fix", key=f"pred_fix_{pred['agent_id']}"):
                        remediate_agent(pred['agent_id'])
                        st.success("Remediation initiated!")
    else:
        st.success("✅ No potential failures predicted. Systems are stable!")
    
    st.markdown("---")
    
    # Performance Recommendations
    st.subheader("💡 Performance Recommendations")
    
    recommendations = []
    
    # Generate recommendations based on current state
    for agent in agents:
        agent_metrics = [m for m in metrics if m.get('agent_id') == agent.get('agent_id')]
        
        if agent_metrics:
            latest = agent_metrics[0]
            
            if latest.get('cpu_usage', 0) > 80:
                recommendations.append({
                    'agent': agent.get('agent_id'),
                    'category': 'CPU Optimization',
                    'priority': 'High',
                    'recommendation': 'CPU usage is consistently high. Consider load balancing or upgrading CPU.'
                })
            
            if latest.get('memory_usage', 0) > 85:
                recommendations.append({
                    'agent': agent.get('agent_id'),
                    'category': 'Memory Management',
                    'priority': 'High',
                    'recommendation': 'Memory usage is high. Review running processes and consider increasing RAM.'
                })
            
            if latest.get('disk_usage', 0) > 90:
                recommendations.append({
                    'agent': agent.get('agent_id'),
                    'category': 'Storage',
                    'priority': 'Critical',
                    'recommendation': 'Disk space is critically low. Clean up unnecessary files or expand storage.'
                })
    
    if recommendations:
        for idx, rec in enumerate(recommendations):
            priority_colors = {
                'Critical': 'critical',
                'High': 'warning',
                'Medium': 'warning',
                'Low': 'healthy'
            }
            
            st.markdown(f"""
            **{idx+1}. {rec['category']}** - Agent: `{rec['agent']}`  
            <span class="status-badge status-{priority_colors.get(rec['priority'], 'healthy')}">{rec['priority']} Priority</span>  
            💡 {rec['recommendation']}
            """, unsafe_allow_html=True)
            st.markdown("---")
    else:
        st.success("✅ No immediate recommendations. Your system is well-optimized!")
    
    st.markdown("---")
    
    # Trend Analysis
    st.subheader("📊 Trend Analysis")
    
    if metrics:
        st.write("**Key Observations:**")
        
        # Calculate averages
        avg_cpu = sum(m.get('cpu_usage', 0) for m in metrics) / len(metrics)
        avg_memory = sum(m.get('memory_usage', 0) for m in metrics) / len(metrics)
        avg_disk = sum(m.get('disk_usage', 0) for m in metrics) / len(metrics)
        
        col1, col2, col3 = st.columns(3)
        
        with col1:
            st.metric("Avg CPU Usage", f"{avg_cpu:.1f}%")
            if avg_cpu > 70:
                st.warning("↗️ Above normal")
            else:
                st.success("✓ Normal range")
        
        with col2:
            st.metric("Avg Memory Usage", f"{avg_memory:.1f}%")
            if avg_memory > 75:
                st.warning("↗️ Above normal")
            else:
                st.success("✓ Normal range")
        
        with col3:
            st.metric("Avg Disk Usage", f"{avg_disk:.1f}%")
            if avg_disk > 80:
                st.warning("↗️ Above normal")
            else:
                st.success("✓ Normal range")

# ==================== TAB 6: HISTORICAL ANALYTICS ====================

with tab6:
    st.header("📉 Historical Analytics & Trends")
    
    # Time range selector
    col1, col2 = st.columns([1, 3])
    
    with col1:
        time_range = st.selectbox("Time Range", ["Last 50 points", "Last 100 points", "All data"])
    
    limit_map = {
        "Last 50 points": 50,
        "Last 100 points": 100,
        "All data": 1000
    }
    
    metrics = get_metrics(limit=limit_map[time_range])
    
    if not metrics:
        st.info("📊 No historical data available yet. Data will accumulate as agents collect metrics.")
    else:
        # Data overview
        st.subheader("📊 Data Overview")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Data Points", len(metrics))
        
        with col2:
            unique_agents = len(set(m.get('agent_id') for m in metrics))
            st.metric("Agents Tracked", unique_agents)
        
        with col3:
            anomaly_count = sum(1 for m in metrics if m.get('is_anomaly', False))
            st.metric("Anomalies Found", anomaly_count)
        
        with col4:
            if metrics:
                time_span = "Unknown"
                if metrics[0].get('timestamp') and metrics[-1].get('timestamp'):
                    time_span = f"{len(metrics)} points"
                st.metric("Time Span", time_span)
        
        st.markdown("---")
        
        # Comparative analysis
        st.subheader("📊 Multi-Agent Comparison")
        
        # Agent selector for comparison
        agents = get_agents()
        if agents:
            selected_agents = st.multiselect(
                "Select agents to compare",
                [a.get('agent_id') for a in agents],
                default=[a.get('agent_id') for a in agents[:2]]  # Default first 2
            )
            
            if selected_agents:
                # Create comparison charts
                comparison_data = {agent_id: [] for agent_id in selected_agents}
                
                for metric in metrics:
                    agent_id = metric.get('agent_id')
                    if agent_id in selected_agents:
                        comparison_data[agent_id].append(metric)
                
                # CPU Comparison
                st.write("**CPU Usage Comparison**")
                fig = go.Figure()
                
                for agent_id, agent_metrics in comparison_data.items():
                    if agent_metrics:
                        fig.add_trace(go.Scatter(
                            x=list(range(len(agent_metrics))),
                            y=[m.get('cpu_usage', 0) for m in agent_metrics],
                            mode='lines+markers',
                            name=agent_id,
                            line=dict(width=2)
                        ))
                
                fig.update_layout(
                    title="CPU Usage Comparison",
                    xaxis_title="Time Points",
                    yaxis_title="CPU Usage (%)",
                    height=400,
                    hovermode='x unified'
                )
                st.plotly_chart(fig, width='stretch', key='chart_15')
                
                # Memory Comparison
                st.write("**Memory Usage Comparison**")
                fig = go.Figure()
                
                for agent_id, agent_metrics in comparison_data.items():
                    if agent_metrics:
                        fig.add_trace(go.Scatter(
                            x=list(range(len(agent_metrics))),
                            y=[m.get('memory_usage', 0) for m in agent_metrics],
                            mode='lines+markers',
                            name=agent_id,
                            line=dict(width=2)
                        ))
                
                fig.update_layout(
                    title="Memory Usage Comparison",
                    xaxis_title="Time Points",
                    yaxis_title="Memory Usage (%)",
                    height=400,
                    hovermode='x unified'
                )
                st.plotly_chart(fig, width='stretch', key='chart_16')
        
        st.markdown("---")
        
        # Anomaly patterns over time
        st.subheader("🤖 Anomaly Patterns Over Time")
        
        anomalies = [m for m in metrics if m.get('is_anomaly', False)]
        
        if anomalies:
            # Anomaly frequency chart
            anomaly_indices = [i for i, m in enumerate(metrics) if m.get('is_anomaly', False)]
            
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(len(metrics))),
                y=[1 if m.get('is_anomaly', False) else 0 for m in metrics],
                mode='markers',
                name='Anomalies',
                marker=dict(
                    size=10,
                    color=[COLORS['critical'] if m.get('is_anomaly') else COLORS['healthy'] for m in metrics]
                )
            ))
            
            fig.update_layout(
                title="Anomaly Detection Timeline",
                xaxis_title="Time Points",
                yaxis_title="Anomaly Detected",
                height=300,
                yaxis=dict(tickmode='array', tickvals=[0, 1], ticktext=['Normal', 'Anomaly'])
            )
            st.plotly_chart(fig, width='stretch', key='chart_17')
            
            # Anomaly statistics
            col1, col2, col3 = st.columns(3)
            
            with col1:
                st.metric("Total Anomalies", len(anomalies))
            
            with col2:
                anomaly_rate = (len(anomalies) / len(metrics) * 100) if metrics else 0
                st.metric("Anomaly Rate", f"{anomaly_rate:.2f}%")
            
            with col3:
                critical = sum(1 for a in anomalies if a.get('severity') == 'critical')
                st.metric("Critical Anomalies", critical)
        else:
            st.success("✅ No anomalies detected in the selected time range!")
        
        st.markdown("---")
        
        # Resource utilization summary
        st.subheader("📊 Resource Utilization Summary")
        
        df = pd.DataFrame(metrics)
        
        if not df.empty:
            summary_stats = df[['cpu_usage', 'memory_usage', 'disk_usage']].describe()
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.write("**Statistical Summary**")
                st.dataframe(summary_stats, width='stretch')
            
            with col2:
                # Box plot
                fig = go.Figure()
                fig.add_trace(go.Box(y=df['cpu_usage'], name='CPU'))
                fig.add_trace(go.Box(y=df['memory_usage'], name='Memory'))
                fig.add_trace(go.Box(y=df['disk_usage'], name='Disk'))
                
                fig.update_layout(
                    title="Resource Usage Distribution",
                    yaxis_title="Usage (%)",
                    height=400
                )
                st.plotly_chart(fig, width='stretch', key='chart_18')
        
        # Export data
        st.markdown("---")
        st.subheader("💾 Export Data")
        
        col1, col2 = st.columns(2)
        
        with col1:
            if st.button("📊 Download CSV"):
                df = pd.DataFrame(metrics)
                csv = df.to_csv(index=False)
                st.download_button(
                    label="💾 Download Metrics CSV",
                    data=csv,
                    file_name="system_metrics.csv",
                    mime="text/csv"
                )
        
        with col2:
            if st.button("📋 View Raw JSON"):
                with st.expander("Raw Metrics Data"):
                    st.json(metrics[:10])  # Show first 10

# ==================== FOOTER ====================

st.markdown("---")
st.markdown("""
<div style='text-align: center; color: #666; padding: 2rem 0;'>
    <p style='font-size: 1.1rem;'><b>Distributed System Monitoring AI Platform</b></p>
    <p>Powered by FastAPI, Streamlit & Machine Learning</p>
    <p style='font-size: 0.9rem;'>Backend: {}</p>
</div>
""".format(API_BASE), unsafe_allow_html=True)
